# Architecture

## Deployed topology

```text
MCP client
  ↓ Streamable HTTP: /mcp
Calibre µMCP plugin (umcp.MCPServer)
  ↓ serialized in-process calls
active Calibre database and library
```

There is no sidecar. The plugin packages and uses the canonical µMCP runtime from `src/calibre_umcp/umcp.py` and exposes MCP directly from the Calibre GUI process.

## Why in-process

Calibre maintains database/cache state and filesystem layout assumptions in memory. A second process writing through `calibredb` can race the active GUI. The plugin therefore owns access to the active library.

## Transport

The plugin uses µMCP's native Streamable HTTP implementation:

- `POST /mcp` — MCP JSON-RPC requests and notifications
- `GET /health` — minimal health/version response
- MCP protocol versions supported by µMCP: `2025-03-26` and `2024-11-05`
- default bind: `127.0.0.1:9000`
- non-loopback binds require `CALIBRE_UMCP_BRIDGE_TOKEN`

The Calibre GUI action starts and stops the embedded µMCP HTTP server cleanly.

## Context-efficient discovery

Recommended flow:

1. MCP `initialize`
2. MCP `tools/list`
3. `capabilities_readonly()`
4. `bridge_status_readonly()` when status is needed
5. `search_books_readonly(query, limit<=20)`
6. `get_book_metadata_readonly(book_id)` for one selected result

`describe_tool_readonly(tool_name)` returns focused guidance for one tool.

## Safety

Only implemented read-only operations are advertised through MCP. Conversion, copy, move, and email are not MCP tools until they have safe Calibre in-process job implementations.

All database operations are serialized through the existing bridge worker before touching `gui.current_db`.


- container: `calibre`
- image: `linuxserver/calibre:latest`
- config: `/config`
- library: `/books`
- plugin directory: `/config/.config/calibre/plugins`
- Calibre profile user: `abc` (`uid=1032`, `gid=100`)

After installing/upgrading the plugin, restart or reload Calibre and select `µMCP Bridge → Start bridge`.
