from __future__ import annotations

from typing import Any

from .config import load_config
from .calibre import CalibreCLI
from .bridge import CalibreBridgeClient, BridgeError
from .umcp import MCPServer


class CalibreMCPServer(MCPServer):
    """MCP server for Calibre library automation."""

    def __init__(self) -> None:
        super().__init__()
        self.calibre = CalibreCLI(load_config())
        self.bridge = CalibreBridgeClient()

    def tool_capabilities_readonly(self, include_mutating: bool = False) -> dict[str, Any]:
        """Compact progressive-discovery entrypoint for available Calibre tools."""
        tools = [
            {
                "name": "bridge_status_readonly",
                "summary": "Check whether the plugin bridge is configured/reachable.",
                "read_only": True,
                "next": "Call first; if reachable, live-library reads use the plugin bridge.",
            },
            {
                "name": "list_libraries_readonly",
                "summary": "List configured/active Calibre libraries.",
                "read_only": True,
                "next": "Use before search when the library name/path is unknown.",
            },
            {
                "name": "search_books_readonly",
                "summary": "Search or list books with a small limit; returns compact metadata rows.",
                "read_only": True,
                "next": "Use get_book_metadata_readonly for one selected id.",
            },
            {
                "name": "get_book_metadata_readonly",
                "summary": "Fetch full metadata for one book id.",
                "read_only": True,
                "next": "Call only after search_books_readonly identifies a candidate id.",
            },
            {
                "name": "find_duplicates_readonly",
                "summary": "Find probable duplicates by title/authors/identifiers.",
                "read_only": True,
                "next": "Use with a bounded limit, then inspect individual ids as needed.",
            },
            {
                "name": "list_bridge_jobs_readonly",
                "summary": "List plugin bridge audit/job records.",
                "read_only": True,
                "next": "Use get_bridge_job_status_readonly for one job id.",
            },
        ]
        mutating = [
            {
                "name": "convert_book",
                "summary": "Fail-closed mutator placeholder; requires future Calibre JobManager mapping.",
                "read_only": False,
            },
            {
                "name": "copy_book",
                "summary": "Fail-closed mutator placeholder; requires plugin bridge safe implementation.",
                "read_only": False,
            },
            {
                "name": "move_book_destructive",
                "summary": "Fail-closed destructive placeholder; requires plugin bridge safe implementation.",
                "read_only": False,
            },
            {
                "name": "email_book",
                "summary": "Fail-closed mutator placeholder; requires plugin bridge safe implementation.",
                "read_only": False,
            },
        ]
        if include_mutating:
            tools.extend(mutating)
        return {
            "strategy": "progressive-discovery",
            "start_here": ["bridge_status_readonly", "list_libraries_readonly", "search_books_readonly"],
            "token_guidance": "Use capabilities_readonly first, search with small limits, then fetch one item/job with a detail tool.",
            "tools": tools,
        }

    def tool_describe_tool_readonly(self, tool_name: str) -> dict[str, Any]:
        """Return details for one Calibre MCP tool, avoiding large all-tools descriptions."""
        details: dict[str, dict[str, Any]] = {
            "bridge_status_readonly": {
                "purpose": "Check plugin bridge configuration/reachability.",
                "args": {},
                "returns": "enabled/reachable flags plus plugin ping status or error.",
                "usage": "Call before live-library operations.",
            },
            "list_libraries_readonly": {
                "purpose": "List configured libraries or the plugin's current live library.",
                "args": {},
                "returns": "Mapping of library name to path.",
            },
            "search_books_readonly": {
                "purpose": "Search/list books with compact metadata.",
                "args": {"query": "Calibre search string, default empty", "library": "optional configured library", "limit": "default 50; keep small"},
                "returns": "List of metadata rows including id/title/authors/formats.",
                "usage": "Prefer limit <= 20 unless a larger scan is required.",
            },
            "get_book_metadata_readonly": {
                "purpose": "Fetch full metadata for one book.",
                "args": {"book_id": "integer Calibre book id", "library": "optional configured library"},
                "returns": "One metadata object.",
            },
            "find_duplicates_readonly": {
                "purpose": "Find likely duplicate groups.",
                "args": {"library": "optional configured library", "limit": "max books to scan; default 1000"},
                "returns": "Duplicate groups with counts and compact book metadata.",
            },
            "list_bridge_jobs_readonly": {
                "purpose": "List bridge audit/job records.",
                "args": {},
                "returns": "Sorted job records; use detail lookup for one job when possible.",
            },
            "get_bridge_job_status_readonly": {
                "purpose": "Get one bridge audit/job record.",
                "args": {"job_id": "bridge job id"},
                "returns": "One job/audit record.",
            },
            "convert_book": {"purpose": "Mutator placeholder.", "status": "fail-closed until safe Calibre job mapping is implemented."},
            "copy_book": {"purpose": "Mutator placeholder.", "status": "fail-closed until safe plugin implementation exists."},
            "move_book_destructive": {"purpose": "Destructive mutator placeholder.", "status": "fail-closed until safe plugin implementation exists."},
            "email_book": {"purpose": "Mutator placeholder.", "status": "fail-closed until safe plugin implementation exists."},
        }
        try:
            return {"name": tool_name, **details[tool_name]}
        except KeyError as exc:
            raise BridgeError(f"Unknown calibre-umcp tool: {tool_name}") from exc

    def tool_bridge_status_readonly(self) -> dict[str, Any]:
        """Report whether the safe in-process Calibre plugin bridge is configured and reachable."""
        if not self.bridge.enabled:
            return {"enabled": False, "reachable": False, "message": "CALIBRE_UMCP_BRIDGE_URL is not set"}
        try:
            return {"enabled": True, "reachable": True, "status": self.bridge.call("ping")}
        except Exception as exc:
            return {"enabled": True, "reachable": False, "error": str(exc)}

    def tool_list_libraries_readonly(self) -> dict[str, str]:
        """List configured Calibre libraries."""
        if self.bridge.enabled:
            return self.bridge.call("list_libraries")
        return {name: str(lib.path) for name, lib in self.calibre.config.libraries.items()}

    def tool_list_libraries(self) -> dict[str, str]:
        """Backward-compatible alias for older MCP clients."""
        return self.tool_list_libraries_readonly()

    def tool_list_bridge_jobs_readonly(self) -> list[dict[str, Any]]:
        """List plugin bridge job/audit records."""
        if not self.bridge.enabled:
            raise BridgeError("CALIBRE_UMCP_BRIDGE_URL is required to list plugin bridge jobs")
        return self.bridge.call("list_jobs")

    def tool_get_bridge_job_status_readonly(self, job_id: str) -> dict[str, Any]:
        """Return one plugin bridge job/audit record."""
        if not self.bridge.enabled:
            raise BridgeError("CALIBRE_UMCP_BRIDGE_URL is required to inspect plugin bridge jobs")
        return self.bridge.call("get_job_status", job_id=job_id)

    def tool_search_books_readonly(self, query: str = "", library: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Search/list books in a Calibre library."""
        if self.bridge.enabled:
            return self.bridge.call("search_books", query=query, library=library, limit=limit)
        return self.calibre.list_books(library, search=query, limit=limit)

    def tool_get_book_metadata_readonly(self, book_id: int, library: str | None = None) -> dict[str, Any]:
        """Return Calibre metadata for one book."""
        if self.bridge.enabled:
            return self.bridge.call("get_book_metadata", book_id=book_id, library=library)
        return self.calibre.show_metadata(book_id, library)

    def tool_find_duplicates_readonly(self, library: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        """Find probable duplicate books using title/author/identifier grouping."""
        if self.bridge.enabled:
            return self.bridge.call("find_duplicates", library=library, limit=limit)
        return self.calibre.find_duplicates(library, limit=limit)

    def tool_convert_book(self, input_path: str, output_path: str, extra_args: list[str] | None = None) -> dict[str, Any]:
        """Convert a book using Calibre's ebook-convert."""
        if self.bridge.enabled:
            return self.bridge.call("convert_book", input_path=input_path, output_path=output_path, extra_args=extra_args or [])
        raise BridgeError("convert_book requires the Calibre plugin bridge for safe operation")

    def tool_copy_book(self, book_id: int, target_library: str, source_library: str | None = None) -> dict[str, Any]:
        """Copy a book to another configured library."""
        if self.bridge.enabled:
            return self.bridge.call("copy_book", book_id=book_id, target_library=target_library, source_library=source_library)
        raise BridgeError("copy_book requires the Calibre plugin bridge for safe operation")

    def tool_move_book_destructive(self, book_id: int, target_library: str, source_library: str | None = None) -> dict[str, Any]:
        """Move a book to another configured library."""
        if self.bridge.enabled:
            return self.bridge.call("move_book", book_id=book_id, target_library=target_library, source_library=source_library)
        raise BridgeError("move_book requires the Calibre plugin bridge for safe operation")

    def tool_email_book(self, book_id: int, to: str, library: str | None = None) -> dict[str, Any]:
        """Email a book using Calibre's configured mail support."""
        if self.bridge.enabled:
            return self.bridge.call("email_book", book_id=book_id, to=to, library=library)
        raise BridgeError("email_book requires the Calibre plugin bridge for safe operation")


# Explicit MCP annotation override: moving between libraries removes the source copy.
CalibreMCPServer.tool_move_book_destructive._mcp_annotations = {  # type: ignore[attr-defined]
    "readOnlyHint": False,
    "destructiveHint": True,
}


def main() -> None:
    CalibreMCPServer().run()


if __name__ == "__main__":
    main()
