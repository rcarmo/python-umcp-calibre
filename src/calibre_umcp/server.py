from __future__ import annotations

from typing import Any

from .config import load_config
from .calibre import CalibreCLI
from .umcp import MCPServer


class CalibreMCPServer(MCPServer):
    """MCP server for Calibre library automation."""

    def __init__(self) -> None:
        super().__init__()
        self.calibre = CalibreCLI(load_config())

    def tool_list_libraries(self) -> dict[str, str]:
        """List configured Calibre libraries."""
        return {name: str(lib.path) for name, lib in self.calibre.config.libraries.items()}

    def tool_search_books_readonly(self, query: str = "", library: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Search/list books in a Calibre library."""
        return self.calibre.list_books(library, search=query, limit=limit)

    def tool_get_book_metadata_readonly(self, book_id: int, library: str | None = None) -> dict[str, Any]:
        """Return Calibre metadata for one book."""
        return self.calibre.show_metadata(book_id, library)

    def tool_find_duplicates_readonly(self, library: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        """Find probable duplicate books using title/author/identifier grouping."""
        return self.calibre.find_duplicates(library, limit=limit)

    def tool_convert_book(self, input_path: str, output_path: str, extra_args: list[str] | None = None) -> dict[str, Any]:
        """Convert a book using Calibre's ebook-convert."""
        return self.calibre.convert_book(input_path, output_path, extra_args)

    def tool_copy_book(self, book_id: int, target_library: str, source_library: str | None = None) -> dict[str, Any]:
        """Copy a book to another configured library."""
        return self.calibre.copy_or_move(book_id, target_library, source_library, move=False)

    def tool_move_book_destructive(self, book_id: int, target_library: str, source_library: str | None = None) -> dict[str, Any]:
        """Move a book to another configured library."""
        return self.calibre.copy_or_move(book_id, target_library, source_library, move=True)

    def tool_email_book(self, book_id: int, to: str, library: str | None = None) -> dict[str, Any]:
        """Email a book using Calibre's configured mail support."""
        return self.calibre.email_book(book_id, to, library)


# Explicit MCP annotation override: moving between libraries removes the source copy.
CalibreMCPServer.tool_move_book_destructive._mcp_annotations = {  # type: ignore[attr-defined]
    "readOnlyHint": False,
    "destructiveHint": True,
}


def main() -> None:
    CalibreMCPServer().run()


if __name__ == "__main__":
    main()
