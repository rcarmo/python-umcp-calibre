# Design

## Decision

Run MCP directly inside the Calibre Interface Action plugin using `umcp.MCPServer`. Do not deploy a sidecar and do not maintain a second MCP protocol implementation.

## Components

- `plugins/calibre_umcp_plugin/ui.py` — Calibre Start/Status/Stop actions
- `plugins/calibre_umcp_plugin/mcp.py` — Calibre-specific `MCPServer` subclass and tool methods
- `plugins/calibre_umcp_plugin/bridge.py` — serialized access to `gui.current_db`
- `src/calibre_umcp/umcp.py` — canonical µMCP runtime, copied into the plugin ZIP at build time
- `src/calibre_umcp/umcp_shared.py` — shared µMCP transport types/helpers

## Scope

Implemented:

- µMCP Streamable HTTP transport at `/mcp`
- `/health` auxiliary route
- bearer authentication
- progressive discovery
- active-library status/listing
- book search and metadata
- duplicate detection
- audit/job record inspection

Not implemented or advertised:

- conversion
- copy between libraries
- destructive move
- email

Those mutations require explicit Calibre `JobManager`/`ThreadedJob` mappings and are outside the current YAGNI read-only scope.

## Packaging

`plugins/build-plugin.sh` copies the canonical µMCP runtime into the plugin package, builds the ZIP, and removes the temporary copies from the source tree. `plugins/install-from-gitea.sh` fetches the same six source files when installing from the Gitea mirror.

## Authentication

Loopback use may omit a token. Any non-loopback bind is rejected unless `CALIBRE_UMCP_BRIDGE_TOKEN` is configured. µMCP's authentication hook validates `Authorization: Bearer <token>` for `/mcp`.

## Lifecycle

The plugin starts µMCP in a daemon thread. A small `server_ready` callback added to the canonical synchronous µMCP transport returns the underlying HTTP server to the Calibre action, allowing Stop and Calibre shutdown to call `shutdown()`, `server_close()`, and join the thread.
