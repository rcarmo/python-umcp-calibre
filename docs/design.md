# Design notes

## Calibre extension feasibility


- the existing deployment is container-based;
- a plugin-hosted HTTP server would be coupled to Calibre GUI lifecycle and permissions;

The pragmatic architecture is therefore:

1. **Primary path:** `calibre-umcp` as a container sidecar exposing µMCP Streamable HTTP on a fixed port.
2. **Compatibility path:** a minimal Calibre plugin shim that can launch the same Python package and point it at the active Calibre library.

## Required tools

The server uses Calibre's supported CLI tools:

- `calibredb` for library metadata, copy/move, email and database operations;
- `ebook-convert` for format conversion;
- optional future `calibre-smtp` direct SMTP support if `calibredb email` is not enough.

## Initial MCP tools

- `list_libraries`
- `search_books_readonly`
- `get_book_metadata_readonly`
- `find_duplicates_readonly`
- `convert_book`
- `copy_book`
- `move_book_destructive`
- `email_book`

`umcp` derives annotations from names, so read-only/destructive hints are reflected in tool metadata.



```yaml
calibre-umcp:
  ports:
    - "9000:9000"
  volumes:
  environment:
    - CALIBRE_LIBRARIES=main=/books,articles=/books/Articles
    - CALIBRE_DEFAULT_LIBRARY=main
```
