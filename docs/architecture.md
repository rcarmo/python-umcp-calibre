# Architecture

## Keeping Calibre In Charge

Calibre keeps database, cache and filesystem state in memory. Running `calibredb` from another process against the same open library invites races that SQLite locking alone does not fix, so the MCP server lives where the authoritative state already is: inside the Calibre GUI process.

```text
MCP client
  -> Streamable HTTP at /mcp
Calibre µMCP plugin (umcp.MCPServer)
  -> serialised worker -> GUI-thread dispatch
active Calibre database and library
```

For the released plugin path there is no sidecar between the MCP client and the live library. The repository still carries a read-only compatibility server and an older JSON-RPC helper, but those sit outside the released mutation catalogue.

## On The Wire

The published plugin surface is Streamable HTTP at `POST /mcp`, with a small unauthenticated `GET /health` route returning bridge status and version. The Calibre action owns the HTTP server lifecycle: it starts µMCP one second after plugin initialisation, offers Status, Configure, Stop and Start actions in the menu, and performs the same cleanup during Calibre shutdown.

The server listens on `127.0.0.1:9000` unless configured otherwise. A non-loopback bind is rejected unless `CALIBRE_UMCP_BRIDGE_TOKEN` is configured. If a token is configured at all, clients authenticate with `Authorization: Bearer <token>` on `/mcp`, even on loopback.

## GUI Thread And Job Boundaries

µMCP handles HTTP on worker threads, but Calibre database calls, Qt objects and Interface Action state belong on the GUI thread. The bridge therefore serialises incoming work and dispatches one operation at a time back onto the GUI thread before touching `gui.current_db`.

Short mutations stay synchronous on that GUI-thread path: metadata updates, format add/remove, cover replace/remove, duplicate merge and trash deletion. They are intentionally not cancellable once the database call starts. Longer work uses native Calibre jobs instead:

* conversion goes through Calibre's JobManager worker path;
* import, copy/move, save-to-disk and e-mail use `ThreadedJob`.

Bridge records mirror native progress, cancellation and terminal errors without modifying Calibre's own job objects. That matters most for cancellation: the bridge reports the next safe boundary truthfully, which means a killed copy or move can still surface as partial destination work with the source retained.

## Keeping Agent Context Small

MCP still requires `initialize` and `tools/list`, but agents should begin their actual work with `capabilities_readonly`. A typical lookup is:

```text
capabilities_readonly
  -> search_books_readonly(query, limit=20)
  -> get_book_metadata_readonly(book_id)
```

`describe_tool_readonly(tool_name)` provides focused guidance when an agent needs more than the compact capability summary. The point is not to hide functionality; it is to avoid spending context on tool descriptions when the next useful step is usually one small search.

## Mutation Gate

The default advertised surface is read-only. Mutation discovery appears only when a token was saved in the Calibre UI, mutations were explicitly enabled there, the runtime is exactly Calibre 9.12.0, and any `CALIBRE_UMCP_BRIDGE_TOKEN` override still matches the UI-saved token. An environment-only token can require HTTP auth, but it cannot unlock mutations on its own. In container deployments that means the environment token gets you authenticated requests, not mutation discovery, until the same token is saved in the UI and the mutation checkbox is enabled.

Paths and destinations come from UI allowlists rather than request-time host access. Import and cover files must live under configured import roots, exports must stay under configured export roots, destination libraries must match the UI allowlist exactly, and e-mail can only target Calibre-configured recipients with formats already enabled for that recipient.

## Content URLs And Other Deliberate Gaps

`content_server_status_readonly()` reports only an existing authenticated content-server base URL. If the content server is stopped, authentication is disabled, or it listens on a wildcard address such as `0.0.0.0`, the bridge declines to guess at a safe public URL.

Permanent deletion, arbitrary recipients, automatic e-mail conversion, temporary public links and device actions remain deliberately unavailable. Singular legacy `copy_book` and `move_book` bridge calls are rejected in favour of the plural verified operations.

## Compatibility Paths

`src/calibre_umcp/server.py` remains a compatibility read-only server for older deployments and tests. Its own tool surface stays read-only by default, its legacy mutator names fail closed, and it is not the in-Calibre mutation catalogue shipped by the plugin ZIP.

The older helper in `plugins/calibre_umcp_plugin/bridge.py` exposes JSON-RPC at `/rpc` plus `GET /health`. It survives for tests and older wiring, not as the published plugin transport.
