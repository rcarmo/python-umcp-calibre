from __future__ import annotations

import json
import threading
from typing import Any

from .bridge import (
    BRIDGE_VERSION,
    SCHEMA_VERSION,
    TOOLSET_VERSION,
    STABLE_MUTATION_ERRORS,
    STABLE_READ_ERRORS,
    BridgeMethodError,
    CalibreRpcBridge,
    is_loopback_bind,
    mutation_runtime_supported,
)

try:
    from .umcp import MCPServer
    from .umcp_shared import MCPHTTPResponse, MCPPrincipal
except ImportError:  # Source-tree tests use the canonical runtime before plugin packaging.
    from calibre_umcp.umcp import MCPServer
    from calibre_umcp.umcp_shared import MCPHTTPResponse, MCPPrincipal


class CalibrePluginMCPServer(MCPServer):
    """µMCP server running inside the active Calibre GUI process."""

    MUTATION_TOOLS = frozenset({
        "capabilities_mutation",
        "update_book_metadata_mutation",
        "add_book_format_mutation",
        "delete_book_format_mutation",
        "set_book_cover_mutation",
        "add_book_mutation",
        "delete_books_mutation",
        "merge_duplicates_mutation",
        "convert_book_mutation",
        "copy_books_to_library_mutation",
        "move_books_to_library_mutation",
        "save_book_to_disk_mutation",
        "email_book_mutation",
        "cancel_bridge_job_mutation",
        "switch_library_mutation",
    })

    def __init__(
        self,
        gui,
        token: str | None = None,
        audit_path: str | None = None,
        audit_retention: int = 500,
        ui_token_configured: bool = False,
        mutations_enabled: bool = False,
        policy: Any = None,
    ):
        super().__init__()
        self.token = token
        self.ui_token_configured = bool(ui_token_configured)
        self.mutation_runtime_supported = mutation_runtime_supported()
        self.mutations_enabled = bool(
            token and ui_token_configured and mutations_enabled and self.mutation_runtime_supported
        )
        self.policy = policy
        self.bridge = CalibreRpcBridge(
            gui,
            token=token,
            audit_path=audit_path,
            audit_retention=audit_retention,
            import_roots=tuple(getattr(policy, "import_roots", ()) or ()),
            export_roots=tuple(getattr(policy, "export_roots", ()) or ()),
            destination_libraries=tuple(getattr(policy, "destination_libraries", ()) or ()),
            library_registry=tuple(getattr(policy, "library_registry", ()) or ()),
            library_switching_enabled=bool(getattr(policy, "library_switching_enabled", False)),
            content_server_advertised_host=str(getattr(policy, "content_server_advertised_host", "") or ""),
        )

    def get_config(self) -> dict[str, Any]:
        config = super().get_config()
        config["serverInfo"] = {
            "name": "calibre-umcp",
            "version": BRIDGE_VERSION,
            "schemaVersion": str(SCHEMA_VERSION),
            "toolsetVersion": str(TOOLSET_VERSION),
        }
        config["capabilities"] = {"tools": {"listChanged": False}}
        return config

    def get_instructions(self) -> str:
        guidance = (
            "Use capabilities_readonly first. Search with a small limit, then fetch "
            "metadata for one selected book id."
        )
        if self.mutations_enabled:
            guidance += " Use capabilities_mutation before requesting a metadata or format change."
        return guidance

    def discover_tools(self) -> dict[str, Any]:
        discovered = super().discover_tools()
        if not self.mutations_enabled:
            discovered["tools"] = [
                tool for tool in discovered["tools"] if tool["name"] not in self.MUTATION_TOOLS
            ]
        return discovered

    def authenticate_request(self, *, method: str, path: str, headers, peer: str | None) -> MCPPrincipal | None:
        if self.token and headers.get("authorization") != f"Bearer {self.token}":
            return None
        return MCPPrincipal(name="calibre-user")

    def authorize_request(self, principal, *, rpc_method: str | None, tool_name: str | None) -> bool:
        if rpc_method == "tools/call" and tool_name in self.MUTATION_TOOLS:
            return bool(principal and self.mutations_enabled)
        return principal is not None

    def handle_http_request(self, *, method: str, path: str, headers, body: bytes, peer: str | None) -> MCPHTTPResponse | None:
        if method == "GET" and path == "/health":
            payload = json.dumps({
                "ok": True,
                "version": BRIDGE_VERSION,
                "schema_version": SCHEMA_VERSION,
                "toolset_version": TOOLSET_VERSION,
            }).encode("utf-8")
            return MCPHTTPResponse(status=200, body=payload, content_type="application/json")
        return None

    def tool_capabilities_mutation(self) -> dict[str, Any]:
        """List only implemented and policy-enabled metadata and format mutations."""
        self._require_mutations()
        return {
            "policy": "UI-configured token and explicit mutation enablement verified",
            "stable_errors": sorted(STABLE_MUTATION_ERRORS),
            "tools": [
                {"name": "update_book_metadata_mutation", "summary": "Update validated metadata with rollback."},
                {"name": "add_book_format_mutation", "summary": "Import a format from a configured root."},
                {"name": "delete_book_format_mutation", "summary": "Remove one explicit format with final-format confirmation."},
                {"name": "set_book_cover_mutation", "summary": "Replace or remove a cover with rollback."},
                {"name": "add_book_mutation", "summary": "Queue confined book import through Calibre ThreadedJob."},
                {"name": "delete_books_mutation", "summary": "Dry-run then move confirmed books to Calibre trash."},
                {"name": "merge_duplicates_mutation", "summary": "Merge missing formats and metadata into an explicit survivor while retaining sources."},
                {"name": "convert_book_mutation", "summary": "Queue one native Calibre conversion job."},
                {"name": "copy_books_to_library_mutation", "summary": "Copy and hash-verify books in an allowlisted library."},
                {"name": "move_books_to_library_mutation", "summary": "Preview, copy, verify, then move confirmed sources to Calibre trash."},
                {"name": "save_book_to_disk_mutation", "summary": "Queue confined Calibre-template export with staged publication."},
                {"name": "email_book_mutation", "summary": "Submit one existing format to a Calibre-configured recipient."},
                {"name": "cancel_bridge_job_mutation", "summary": "Request native Calibre job cancellation."},
                {"name": "switch_library_mutation", "summary": "Explicitly switch the visible Calibre GUI library."},
            ],
        }

    def tool_capabilities_readonly(self) -> dict[str, Any]:
        """Compact progressive-discovery entrypoint; call before listing or invoking detailed tools."""
        library_status = self._call_read("list_libraries", {})
        return {
            "schema_version": SCHEMA_VERSION,
            "toolset_version": TOOLSET_VERSION,
            "strategy": "progressive-discovery",
            "start_here": ["bridge_status_readonly", "list_libraries_readonly", "search_books_readonly"],
            "guidance": "Discover library aliases, search with limit <=20, then fetch one selected library-scoped id. Reconnect after a server toolset_version change.",
            "cross_library_reads": True,
            "cross_library_configured": library_status["cross_library_configured"],
            "cross_library_available": library_status["cross_library_available"],
            "readable_target_count": library_status["readable_target_count"],
            "cross_library_reason_code": library_status["cross_library_reason_code"],
            "inactive_library_mutations": False,
            "stable_errors": sorted(STABLE_READ_ERRORS),
            "limits": {"search": 500, "duplicates": 5000, "cross_library_source": 500, "cross_library_candidates": 2000},
            "tools": [
                {"name": "bridge_status_readonly", "summary": "Plugin and active-library status."},
                {"name": "list_libraries_readonly", "summary": "Redacted configured aliases and current cross-library availability."},
                {"name": "search_books_readonly", "summary": "Bounded Calibre search."},
                {"name": "get_book_metadata_readonly", "summary": "Metadata for one book id."},
                {"name": "get_book_formats_readonly", "summary": "Safe file size and modification metadata for one book's formats."},
                {"name": "inspect_book_format_readonly", "summary": "Bounded EPUB container, metadata, structure, and content signals."},
                {"name": "assess_book_quality_readonly", "summary": "Explainable quality score for one book and its formats."},
                {"name": "compare_book_quality_readonly", "summary": "MCP-only quality comparison for two candidate books."},
                {"name": "find_duplicates_readonly", "summary": "Probable duplicate groups in one selected library."},
                {"name": "find_cross_library_duplicates_readonly", "summary": "Compare selected source books against configured target libraries."},
                {"name": "content_server_status_readonly", "summary": "Existing authenticated content-server base URL, when safe."},
                {"name": "list_bridge_jobs_readonly", "summary": "Bridge audit records."},
                {"name": "get_bridge_job_status_readonly", "summary": "One bridge audit record."},
            ],
        }

    def tool_describe_tool_readonly(self, tool_name: str) -> dict[str, Any]:
        """Describe one implemented tool without loading every detailed description into context."""
        details = {
            "bridge_status_readonly": {"arguments": {}, "returns": "version and active library"},
            "list_libraries_readonly": {"arguments": {}, "returns": "redacted configured library aliases and active generation"},
            "search_books_readonly": {"arguments": {"query": "Calibre query", "limit": "default 20, max 500", "library": "configured alias or current", "cursor": "opaque continuation"}},
            "get_book_metadata_readonly": {"arguments": {"book_id": "integer Calibre id", "library": "configured alias or current"}},
            "find_duplicates_readonly": {"arguments": {"limit": "default 1000, max 5000", "library": "configured alias or current", "cursor": "opaque continuation"}},
            "find_cross_library_duplicates_readonly": {"arguments": {"source_library": "configured alias", "target_libraries": "one to sixteen configured aliases", "source_query": "optional Calibre query", "limit": "default 100, max 500", "candidate_limit_per_book": "default 20, max 100"}},
            "content_server_status_readonly": {"arguments": {}, "returns": "running/auth status and a base URL only for authenticated concrete binds"},
            "list_bridge_jobs_readonly": {"arguments": {}},
            "get_bridge_job_status_readonly": {"arguments": {"job_id": "bridge audit id"}},
        }
        mutation_details = {
            "update_book_metadata_mutation": {
                "arguments": {"book_id": "integer Calibre id", "changes": "validated metadata fields", "expected_active_library": "optional current alias guard", "expected_active_generation": "optional generation guard from discovery"},
                "returns": "completed bridge job record",
            },
            "add_book_format_mutation": {
                "arguments": {"book_id": "integer Calibre id", "path": "file below configured import root", "format": "optional extension", "replace": "explicit replacement", "expected_active_library": "optional current alias guard", "expected_active_generation": "optional generation guard from discovery"},
                "returns": "completed bridge job record",
            },
            "delete_book_format_mutation": {
                "arguments": {"book_id": "integer Calibre id", "format": "explicit extension", "allow_last_format": "required for final format", "expected_active_library": "optional current alias guard", "expected_active_generation": "optional generation guard from discovery"},
                "returns": "completed bridge job record",
            },
            "set_book_cover_mutation": {
                "arguments": {"book_id": "integer Calibre id", "path": "cover below configured import root", "remove": "remove existing cover", "expected_active_library": "optional current alias guard", "expected_active_generation": "optional generation guard from discovery"},
                "returns": "completed bridge job record",
            },
            "add_book_mutation": {
                "arguments": {"path": "book below configured import root", "format": "optional extension", "duplicate_policy": "reject, skip or add", "expected_active_library": "optional current alias guard", "expected_active_generation": "optional generation guard from discovery"},
                "returns": "queued bridge job record linked to Calibre ThreadedJob",
            },
            "delete_books_mutation": {
                "arguments": {"book_ids": "non-empty id list", "dry_run": "default true", "confirmation": "exact value returned by dry-run", "expected_active_library": "optional current alias guard", "expected_active_generation": "optional generation guard from discovery"},
                "returns": "preview or Calibre-trash result",
            },
            "merge_duplicates_mutation": {
                "arguments": {"survivor_id": "explicit survivor", "source_ids": "records retained after merge", "confirmation": "exact MERGE_KEEP_SOURCES value", "expected_active_library": "optional current alias guard", "expected_active_generation": "optional generation guard from discovery"},
                "returns": "completed conservative merge record",
            },
            "convert_book_mutation": {
                "arguments": {"book_id": "integer Calibre id", "output_format": "target extension", "replace_existing": "explicit replacement", "options": "bounded scalar overrides", "store_result": "attach to book or export", "export_path": "required configured-root path when exporting", "overwrite_export": "explicit collision policy", "expected_active_library": "optional current alias guard", "expected_active_generation": "optional generation guard from discovery"},
                "returns": "queued bridge job record linked to Calibre JobManager",
            },
            "copy_books_to_library_mutation": {
                "arguments": {"book_ids": "source ids", "destination_library": "exact UI-allowlisted library", "duplicate_policy": "reject, skip, add, merge_missing or replace", "destination_book_ids": "required source-to-destination map for merge policies", "expected_active_library": "optional current alias guard", "expected_active_generation": "optional generation guard from discovery"},
                "returns": "queued, independently verified Calibre ThreadedJob record",
            },
            "move_books_to_library_mutation": {
                "arguments": {"book_ids": "source ids", "destination_library": "exact UI-allowlisted library", "dry_run": "default true", "confirmation": "exact preview value", "duplicate_policy": "explicit policy", "expected_active_library": "optional current alias guard", "expected_active_generation": "optional generation guard from discovery"},
                "returns": "preview or queued verified-copy-then-trash job record",
            },
            "save_book_to_disk_mutation": {
                "arguments": {"book_id": "source id", "destination_directory": "directory below configured export root", "options": "bounded Calibre save options", "overwrite": "explicit collision replacement", "expected_active_library": "optional current alias guard", "expected_active_generation": "optional generation guard from discovery"},
                "returns": "queued Calibre ThreadedJob with bounded artefact paths",
            },
            "email_book_mutation": {
                "arguments": {"book_id": "source id", "recipient": "exact Calibre-configured account", "format": "existing format allowed for recipient", "auto_convert": "unsupported; queue conversion separately", "expected_active_library": "optional current alias guard", "expected_active_generation": "optional generation guard from discovery"},
                "returns": "queued native email ThreadedJob; SMTP acceptance is separate from delivery",
            },
            "cancel_bridge_job_mutation": {
                "arguments": {"job_id": "bridge job id"},
                "returns": "updated bridge job record",
            },
            "switch_library_mutation": {
                "arguments": {"library": "configured switchable alias", "expected_active_library": "current alias", "expected_active_generation": "generation from discovery", "confirmation": "SWITCH_LIBRARY:<alias>"},
                "returns": "new active alias and generation",
            },
        }
        if tool_name in mutation_details:
            self._require_mutations()
            details.update(mutation_details)
        if tool_name not in details:
            raise ValueError(f"Unknown implemented tool: {tool_name}")
        detail = dict(details[tool_name])
        if "arguments" in detail and "args" not in detail:
            detail["args"] = dict(detail["arguments"])
        return {"name": tool_name, **detail}

    def _call_read(self, method: str, params: dict[str, Any]) -> Any:
        try:
            return self.bridge.call_serialized(method, params)
        except BridgeMethodError as exc:
            raise ValueError(str(exc)) from exc

    def tool_bridge_status_readonly(self) -> dict[str, Any]:
        """Report plugin version and active Calibre library."""
        status = self._call_read("ping", {})
        status.pop("library_path", None)
        status.update({
            "schema_version": SCHEMA_VERSION,
            "toolset_version": TOOLSET_VERSION,
            "active_library": self.bridge._active_alias(),
            "active_generation": self.bridge.active_generation,
        })
        return status

    def tool_list_libraries_readonly(self) -> dict[str, Any]:
        """List redacted configured libraries and active state."""
        return self._call_read("list_libraries", {})

    def tool_search_books_readonly(self, query: str = "", limit: int = 20, library: str = "current", cursor: str = "") -> dict[str, Any]:
        """Search one selected configured library without changing the GUI."""
        return self._call_read("search_books", {"query": query, "limit": limit, "library": library, "cursor": cursor})

    def tool_get_book_metadata_readonly(self, book_id: int, library: str = "current") -> dict[str, Any]:
        """Return metadata for one library-scoped Calibre book id."""
        return self._call_read("get_book_metadata", {"book_id": book_id, "library": library})

    def tool_get_book_formats_readonly(self, book_id: int, library: str = "current") -> dict[str, Any]:
        """Return bounded, path-free format size and modification metadata for one book."""
        return self._call_read("get_book_formats", {"book_id": book_id, "library": library})

    def tool_inspect_book_format_readonly(
        self, book_id: int, format: str, library: str = "current", include_text_sample: bool = False
    ) -> dict[str, Any]:
        """Inspect one EPUB without returning paths or book text; other formats fail with a stable code."""
        return self._call_read("inspect_book_format", {
            "book_id": book_id,
            "format": format,
            "library": library,
            "include_text_sample": include_text_sample,
        })

    def tool_assess_book_quality_readonly(
        self, book_id: int, library: str = "current", formats: list[str] | None = None
    ) -> dict[str, Any]:
        """Return a conservative, explainable quality score using bounded EPUB inspection."""
        params: dict[str, Any] = {"book_id": book_id, "library": library}
        if formats is not None:
            params["formats"] = formats
        return self._call_read("assess_book_quality", params)

    def tool_compare_book_quality_readonly(
        self, left: dict[str, Any], right: dict[str, Any], policy: str = "prefer_epub_then_metadata"
    ) -> dict[str, Any]:
        """Compare two library-scoped books and recommend which candidate to keep without mutation."""
        return self._call_read("compare_book_quality", {"left": left, "right": right, "policy": policy})

    def tool_find_duplicates_readonly(self, limit: int = 1000, library: str = "current", cursor: str = "") -> dict[str, Any]:
        """Find probable duplicate books in one selected library."""
        return self._call_read("find_duplicates", {"limit": limit, "library": library, "cursor": cursor})

    def tool_find_cross_library_duplicates_readonly(
        self,
        source_library: str,
        target_libraries: list[str],
        source_query: str = "",
        limit: int = 100,
        candidate_limit_per_book: int = 20,
    ) -> dict[str, Any]:
        """Compare source books with selected configured libraries without switching the GUI."""
        return self._call_read("find_cross_library_duplicates", {
            "source_library": source_library,
            "target_libraries": target_libraries,
            "source_query": source_query,
            "limit": limit,
            "candidate_limit_per_book": candidate_limit_per_book,
        })

    def tool_content_server_status_readonly(self) -> dict[str, Any]:
        """Return only an existing authenticated server base URL; never mint temporary links."""
        return self.bridge.call_serialized("content_server_status", {})

    def tool_list_bridge_jobs_readonly(self) -> dict[str, Any]:
        """List bridge audit records."""
        return {"items": self._call_read("list_jobs", {})}

    def tool_get_bridge_job_status_readonly(self, job_id: str) -> dict[str, Any]:
        """Return one bridge audit record."""
        return self._call_read("get_job_status", {"job_id": job_id})

    def _require_mutations(self) -> None:
        if not self.mutations_enabled:
            raise ValueError("POLICY_DENIED: mutation discovery is disabled by Calibre UI policy")

    def _call_mutation(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._require_mutations()
        try:
            return self.bridge.call_serialized(method, params)
        except BridgeMethodError as exc:
            raise ValueError(str(exc)) from exc

    @staticmethod
    def _with_active_guards(
        params: dict[str, Any],
        *,
        expected_active_library: str | None = None,
        expected_active_generation: int | None = None,
    ) -> dict[str, Any]:
        guarded = dict(params)
        if expected_active_library is not None:
            guarded["expected_active_library"] = expected_active_library
        if expected_active_generation is not None:
            guarded["expected_active_generation"] = expected_active_generation
        return guarded

    def tool_update_book_metadata_mutation(
        self,
        book_id: int,
        changes: dict[str, Any],
        expected_active_library: str | None = None,
        expected_active_generation: int | None = None,
    ) -> dict[str, Any]:
        """Update validated metadata through a short serialised GUI-thread operation with rollback."""
        return self._call_mutation(
            "update_book_metadata",
            self._with_active_guards(
                {"book_id": book_id, "changes": changes},
                expected_active_library=expected_active_library,
                expected_active_generation=expected_active_generation,
            ),
        )

    def tool_add_book_format_mutation(
        self,
        book_id: int,
        path: str,
        format: str = "",
        replace: bool = False,
        expected_active_library: str | None = None,
        expected_active_generation: int | None = None,
    ) -> dict[str, Any]:
        """Import one format from a UI-configured root, preserving an old format on failure."""
        return self._call_mutation(
            "add_book_format",
            self._with_active_guards(
                {"book_id": book_id, "path": path, "format": format, "replace": replace},
                expected_active_library=expected_active_library,
                expected_active_generation=expected_active_generation,
            ),
        )

    def tool_delete_book_format_mutation(
        self,
        book_id: int,
        format: str,
        allow_last_format: bool = False,
        expected_active_library: str | None = None,
        expected_active_generation: int | None = None,
    ) -> dict[str, Any]:
        """Delete one explicit format; final-format removal requires explicit confirmation."""
        return self._call_mutation(
            "delete_book_format",
            self._with_active_guards(
                {"book_id": book_id, "format": format, "allow_last_format": allow_last_format},
                expected_active_library=expected_active_library,
                expected_active_generation=expected_active_generation,
            ),
        )

    def tool_set_book_cover_mutation(
        self,
        book_id: int,
        path: str = "",
        remove: bool = False,
        expected_active_library: str | None = None,
        expected_active_generation: int | None = None,
    ) -> dict[str, Any]:
        """Replace a cover from a configured import root, or remove it, with rollback."""
        return self._call_mutation(
            "set_book_cover",
            self._with_active_guards(
                {"book_id": book_id, "path": path, "remove": remove},
                expected_active_library=expected_active_library,
                expected_active_generation=expected_active_generation,
            ),
        )

    def tool_add_book_mutation(
        self,
        path: str,
        format: str = "",
        duplicate_policy: str = "reject",
        expected_active_library: str | None = None,
        expected_active_generation: int | None = None,
    ) -> dict[str, Any]:
        """Queue a confined single-book import with explicit duplicate policy."""
        return self._call_mutation(
            "add_book",
            self._with_active_guards(
                {"path": path, "format": format, "duplicate_policy": duplicate_policy},
                expected_active_library=expected_active_library,
                expected_active_generation=expected_active_generation,
            ),
        )

    def tool_delete_books_mutation(
        self,
        book_ids: list[int],
        dry_run: bool = True,
        confirmation: str = "",
        permanent: bool = False,
        expected_active_library: str | None = None,
        expected_active_generation: int | None = None,
    ) -> dict[str, Any]:
        """Preview deletion, then move exactly confirmed books to Calibre trash."""
        return self._call_mutation(
            "delete_books",
            self._with_active_guards(
                {
                    "book_ids": book_ids,
                    "dry_run": dry_run,
                    "confirmation": confirmation,
                    "permanent": permanent,
                },
                expected_active_library=expected_active_library,
                expected_active_generation=expected_active_generation,
            ),
        )

    def tool_merge_duplicates_mutation(
        self,
        survivor_id: int,
        source_ids: list[int],
        confirmation: str,
        replace_cover: bool = False,
        save_alternate_cover: bool = False,
        expected_active_library: str | None = None,
        expected_active_generation: int | None = None,
    ) -> dict[str, Any]:
        """Conservatively merge into an explicit survivor without deleting source records."""
        return self._call_mutation(
            "merge_duplicates",
            self._with_active_guards(
                {
                    "survivor_id": survivor_id,
                    "source_ids": source_ids,
                    "confirmation": confirmation,
                    "replace_cover": replace_cover,
                    "save_alternate_cover": save_alternate_cover,
                },
                expected_active_library=expected_active_library,
                expected_active_generation=expected_active_generation,
            ),
        )

    def tool_convert_book_mutation(
        self,
        book_id: int,
        output_format: str,
        replace_existing: bool = False,
        options: dict[str, Any] | None = None,
        store_result: bool = True,
        export_path: str = "",
        overwrite_export: bool = False,
        expected_active_library: str | None = None,
        expected_active_generation: int | None = None,
    ) -> dict[str, Any]:
        """Queue conversion through Calibre's worker JobManager and return its bridge record immediately."""
        return self._call_mutation(
            "convert_book",
            self._with_active_guards(
                {
                    "book_id": book_id,
                    "output_format": output_format,
                    "replace_existing": replace_existing,
                    "options": options or {},
                    "store_result": store_result,
                    "export_path": export_path,
                    "overwrite_export": overwrite_export,
                },
                expected_active_library=expected_active_library,
                expected_active_generation=expected_active_generation,
            ),
        )

    def tool_copy_books_to_library_mutation(
        self,
        book_ids: list[int],
        destination_library: str,
        duplicate_policy: str = "reject",
        destination_book_ids: dict[str, int] | None = None,
        expected_active_library: str | None = None,
        expected_active_generation: int | None = None,
    ) -> dict[str, Any]:
        """Queue a non-switching, hash-verified copy to one UI-allowlisted library."""
        return self._call_mutation(
            "copy_books_to_library",
            self._with_active_guards(
                {
                    "book_ids": book_ids,
                    "destination_library": destination_library,
                    "duplicate_policy": duplicate_policy,
                    "destination_book_ids": destination_book_ids or {},
                },
                expected_active_library=expected_active_library,
                expected_active_generation=expected_active_generation,
            ),
        )

    def tool_move_books_to_library_mutation(
        self,
        book_ids: list[int],
        destination_library: str,
        dry_run: bool = True,
        confirmation: str = "",
        duplicate_policy: str = "reject",
        destination_book_ids: dict[str, int] | None = None,
        expected_active_library: str | None = None,
        expected_active_generation: int | None = None,
    ) -> dict[str, Any]:
        """Preview or queue verified copy followed by source removal to Calibre trash."""
        return self._call_mutation(
            "move_books_to_library",
            self._with_active_guards(
                {
                    "book_ids": book_ids,
                    "destination_library": destination_library,
                    "dry_run": dry_run,
                    "confirmation": confirmation,
                    "duplicate_policy": duplicate_policy,
                    "destination_book_ids": destination_book_ids or {},
                },
                expected_active_library=expected_active_library,
                expected_active_generation=expected_active_generation,
            ),
        )

    def tool_save_book_to_disk_mutation(
        self,
        book_id: int,
        destination_directory: str,
        options: dict[str, Any] | None = None,
        overwrite: bool = False,
        expected_active_library: str | None = None,
        expected_active_generation: int | None = None,
    ) -> dict[str, Any]:
        """Queue a confined, staged export using Calibre's save-to-disk engine."""
        return self._call_mutation(
            "save_book_to_disk",
            self._with_active_guards(
                {
                    "book_id": book_id,
                    "destination_directory": destination_directory,
                    "options": options or {},
                    "overwrite": overwrite,
                },
                expected_active_library=expected_active_library,
                expected_active_generation=expected_active_generation,
            ),
        )

    def tool_email_book_mutation(
        self,
        book_id: int,
        recipient: str,
        format: str,
        auto_convert: bool = False,
        expected_active_library: str | None = None,
        expected_active_generation: int | None = None,
    ) -> dict[str, Any]:
        """Queue SMTP submission to an existing Calibre-configured recipient."""
        return self._call_mutation(
            "email_book",
            self._with_active_guards(
                {"book_id": book_id, "recipient": recipient, "format": format, "auto_convert": auto_convert},
                expected_active_library=expected_active_library,
                expected_active_generation=expected_active_generation,
            ),
        )

    def tool_cancel_bridge_job_mutation(self, job_id: str) -> dict[str, Any]:
        """Request cancellation of a queued or running native Calibre job."""
        return self._call_mutation("cancel_job", {"job_id": job_id})

    def tool_switch_library_mutation(
        self,
        library: str,
        expected_active_library: str,
        expected_active_generation: int,
        confirmation: str,
    ) -> dict[str, Any]:
        """Explicitly switch the visible Calibre GUI library after guarded confirmation."""
        return self._call_mutation("switch_library", {
            "library": library,
            "expected_active_library": expected_active_library,
            "expected_active_generation": expected_active_generation,
            "confirmation": confirmation,
        })


def serve_mcp(
    gui,
    host: str | None = None,
    port: int | None = None,
    token: str | None = None,
    audit_path: str | None = None,
    *,
    settings: Any = None,
):
    if settings is not None:
        host = settings.host
        port = settings.port
        token = settings.token
        audit_path = settings.audit_path
    host = host or "127.0.0.1"
    port = 9000 if port is None else int(port)
    if not token and not is_loopback_bind(host):
        raise ValueError("A bridge token is required when binding MCP outside loopback")

    mcp = CalibrePluginMCPServer(
        gui,
        token=token,
        audit_path=audit_path,
        audit_retention=getattr(settings, "audit_retention", 500),
        ui_token_configured=getattr(settings, "ui_token_configured", False),
        mutations_enabled=getattr(settings, "mutations_enabled", False),
        policy=settings,
    )
    ready = threading.Event()
    holder: dict[str, Any] = {}

    def server_ready(httpd) -> None:
        holder["httpd"] = httpd
        ready.set()

    def run_server() -> None:
        try:
            mcp.run_streamable_http(host=host, port=port, endpoint="/mcp", server_ready=server_ready)
        except Exception as exc:
            holder["error"] = exc
            ready.set()

    thread = threading.Thread(
        target=run_server,
        name="calibre-umcp-mcp",
        daemon=True,
    )
    thread.start()
    if not ready.wait(timeout=5):
        mcp.bridge.close()
        raise RuntimeError("Timed out starting embedded µMCP server")
    if "error" in holder:
        mcp.bridge.close()
        thread.join(timeout=1)
        raise RuntimeError(f"Failed to start embedded µMCP server: {holder['error']}") from holder["error"]
    httpd = holder["httpd"]
    original_server_close = httpd.server_close

    def server_close_with_bridge() -> None:
        try:
            original_server_close()
        finally:
            mcp.bridge.close()

    httpd.server_close = server_close_with_bridge  # type: ignore[method-assign]
    httpd.thread = thread
    httpd.bridge = mcp.bridge
    httpd.mcp = mcp
    return httpd
