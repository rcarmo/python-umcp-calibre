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
