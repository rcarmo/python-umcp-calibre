from __future__ import annotations

import json
import threading
from typing import Any

from .bridge import BRIDGE_VERSION, CalibreRpcBridge, is_loopback_bind

try:
    from .umcp import MCPServer
    from .umcp_shared import MCPHTTPResponse, MCPPrincipal
except ImportError:  # Source-tree tests use the canonical runtime before plugin packaging.
    from calibre_umcp.umcp import MCPServer
    from calibre_umcp.umcp_shared import MCPHTTPResponse, MCPPrincipal


class CalibrePluginMCPServer(MCPServer):
    """µMCP server running inside the active Calibre GUI process."""

    def __init__(self, gui, token: str | None = None, audit_path: str | None = None):
        super().__init__()
        self.token = token
        self.bridge = CalibreRpcBridge(gui, token=token, audit_path=audit_path)

    def get_config(self) -> dict[str, Any]:
        config = super().get_config()
        config["serverInfo"] = {"name": "calibre-umcp", "version": BRIDGE_VERSION}
        config["capabilities"] = {"tools": {"listChanged": False}}
        return config

    def get_instructions(self) -> str:
        return (
            "Use capabilities_readonly first. Search with a small limit, then fetch "
            "metadata for one selected book id. This server currently exposes read-only tools."
        )

    def authenticate_request(self, *, method: str, path: str, headers, peer: str | None) -> MCPPrincipal | None:
        if self.token and headers.get("authorization") != f"Bearer {self.token}":
            return None
        return MCPPrincipal(name="calibre-user")

    def handle_http_request(self, *, method: str, path: str, headers, body: bytes, peer: str | None) -> MCPHTTPResponse | None:
        if method == "GET" and path == "/health":
            payload = json.dumps({"ok": True, "version": BRIDGE_VERSION}).encode("utf-8")
            return MCPHTTPResponse(status=200, body=payload, content_type="application/json")
        return None

    def tool_capabilities_readonly(self) -> dict[str, Any]:
        """Compact progressive-discovery entrypoint; call before listing or invoking detailed tools."""
        return {
            "strategy": "progressive-discovery",
            "start_here": ["bridge_status_readonly", "search_books_readonly"],
            "guidance": "Search with limit <=20, then fetch one selected id.",
            "tools": [
                {"name": "bridge_status_readonly", "summary": "Plugin and active-library status."},
                {"name": "list_libraries_readonly", "summary": "Active Calibre library."},
                {"name": "search_books_readonly", "summary": "Bounded Calibre search."},
                {"name": "get_book_metadata_readonly", "summary": "Metadata for one book id."},
                {"name": "find_duplicates_readonly", "summary": "Probable duplicate groups."},
                {"name": "list_bridge_jobs_readonly", "summary": "Bridge audit records."},
                {"name": "get_bridge_job_status_readonly", "summary": "One bridge audit record."},
            ],
        }

    def tool_describe_tool_readonly(self, tool_name: str) -> dict[str, Any]:
        """Describe one implemented tool without loading every detailed description into context."""
        details = {
            "bridge_status_readonly": {"arguments": {}, "returns": "version and active library"},
            "list_libraries_readonly": {"arguments": {}, "returns": "active library mapping"},
            "search_books_readonly": {"arguments": {"query": "Calibre query", "limit": "default 20, max 500"}},
            "get_book_metadata_readonly": {"arguments": {"book_id": "integer Calibre id"}},
            "find_duplicates_readonly": {"arguments": {"limit": "default 1000, max 5000"}},
            "list_bridge_jobs_readonly": {"arguments": {}},
            "get_bridge_job_status_readonly": {"arguments": {"job_id": "bridge audit id"}},
        }
        if tool_name not in details:
            raise ValueError(f"Unknown implemented tool: {tool_name}")
        return {"name": tool_name, **details[tool_name]}

    def tool_bridge_status_readonly(self) -> dict[str, Any]:
        """Report plugin version and active Calibre library."""
        return self.bridge.call_serialized("ping", {})

    def tool_list_libraries_readonly(self) -> dict[str, str]:
        """List the active Calibre library."""
        return self.bridge.call_serialized("list_libraries", {})

    def tool_search_books_readonly(self, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        """Search books in the active library; keep limit small for low context use."""
        return self.bridge.call_serialized("search_books", {"query": query, "limit": limit})

    def tool_get_book_metadata_readonly(self, book_id: int) -> dict[str, Any]:
        """Return metadata for one Calibre book id."""
        return self.bridge.call_serialized("get_book_metadata", {"book_id": book_id})

    def tool_find_duplicates_readonly(self, limit: int = 1000) -> list[dict[str, Any]]:
        """Find probable duplicate books in the active library."""
        return self.bridge.call_serialized("find_duplicates", {"limit": limit})

    def tool_list_bridge_jobs_readonly(self) -> list[dict[str, Any]]:
        """List bridge audit records."""
        return self.bridge.call_serialized("list_jobs", {})

    def tool_get_bridge_job_status_readonly(self, job_id: str) -> dict[str, Any]:
        """Return one bridge audit record."""
        return self.bridge.call_serialized("get_job_status", {"job_id": job_id})


def serve_mcp(gui, host: str, port: int, token: str | None = None, audit_path: str | None = None):
    if not token and not is_loopback_bind(host):
        raise ValueError("CALIBRE_UMCP_BRIDGE_TOKEN is required when binding MCP outside loopback")

    mcp = CalibrePluginMCPServer(gui, token=token, audit_path=audit_path)
    ready = threading.Event()
    holder: dict[str, Any] = {}

    def server_ready(httpd) -> None:
        holder["httpd"] = httpd
        ready.set()

    thread = threading.Thread(
        target=mcp.run_streamable_http,
        kwargs={"host": host, "port": port, "endpoint": "/mcp", "server_ready": server_ready},
        name="calibre-umcp-mcp",
        daemon=True,
    )
    thread.start()
    if not ready.wait(timeout=5):
        raise RuntimeError("Timed out starting embedded µMCP server")
    httpd = holder["httpd"]
    httpd.thread = thread
    httpd.bridge = mcp.bridge
    httpd.mcp = mcp
    return httpd
