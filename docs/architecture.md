# Architecture

## Keeping Calibre In Charge

Calibre keeps database, cache and filesystem state in memory. Running `calibredb` from another process against the same open library introduces races that SQLite locking alone cannot fix, so the MCP server lives where the authoritative state already is: inside the Calibre GUI process.

```text
MCP client
  -> Streamable HTTP at /mcp
Calibre µMCP plugin (umcp.MCPServer)
  -> serialised in-process calls
active Calibre database and library
```

There is no sidecar and no second MCP stack. The plugin packages the canonical runtime from `src/calibre_umcp/umcp.py`, then routes tool calls through a single worker before touching `gui.current_db`.

## On The Wire

µMCP provides the Streamable HTTP transport at `POST /mcp`, including MCP requests and notifications for protocol versions `2025-03-26` and `2024-11-05`. The plugin adds a small `GET /health` route that returns its status and version.

The server listens on `127.0.0.1:9000` unless configured otherwise. A non-loopback bind is rejected without `CALIBRE_UMCP_BRIDGE_TOKEN`; once set, clients authenticate with `Authorization: Bearer <token>`.

The Calibre action owns the HTTP server lifecycle. It starts µMCP one second after plugin initialisation, giving Calibre time to load the active library; the menu can still stop or restart it. Stop shuts down the underlying HTTP server and joins that thread, and Calibre shutdown performs the same cleanup.

## Keeping Agent Context Small

MCP still requires `initialize` and `tools/list`, but agents should begin their actual work with `capabilities_readonly`. A typical lookup is:

```text
capabilities_readonly
  -> search_books_readonly(query, limit=20)
  -> get_book_metadata_readonly(book_id)
```

`describe_tool_readonly(tool_name)` provides focused guidance when an agent needs more than the compact capability summary. This is mostly about avoiding large search results--seven small tool schemas are not the expensive part.

## Mutation Boundary

The default advertised surface is read-only. Mutation discovery appears only when a token was saved in the Calibre UI, mutations were explicitly enabled there, and the runtime is exactly Calibre 9.11.0. Short database changes run on the GUI thread; conversion maps to `ParallelJob`, while import, copy/move, save-to-disk and e-mail map to `ThreadedJob`. Bridge IDs mirror native progress and cancellation without changing Calibre job objects.

Permanent deletion, arbitrary recipients, implicit e-mail conversion, temporary public links and device actions remain deliberately unavailable. Singular legacy `copy_book` and `move_book` calls are rejected in favour of the plural verified operations.

`src/calibre_umcp/server.py` remains a compatibility read-only CLI surface for older deployments and tests. It is not the released in-Calibre mutation catalogue: its legacy mutator names fail closed and are not advertised by the plugin ZIP.
