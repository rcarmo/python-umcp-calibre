# Design

## One MCP Runtime

The plugin subclasses `umcp.MCPServer` and exposes MCP directly from the Calibre process. This avoids both a sidecar and a smaller, subtly different protocol implementation--the sort of duplication that behaves perfectly until a client sends a notification or negotiates another protocol version.

Four files contain the Calibre-specific code:

* `plugins/calibre_umcp_plugin/ui.py` owns automatic startup, the Calibre actions and server lifecycle.
* `plugins/calibre_umcp_plugin/mcp.py` defines the MCP server, authentication hook and tools.
* `plugins/calibre_umcp_plugin/bridge.py` serialises access to `gui.current_db`.
* `plugins/calibre_umcp_plugin/__init__.py` provides Calibre's plugin metadata.

The build copies `src/calibre_umcp/umcp.py` and `src/calibre_umcp/umcp_shared.py` into the plugin ZIP. Those copies are temporary packaging inputs, not another source tree.

## A Narrow Tool Surface

The MCP surface covers progressive discovery, server status, active-library listing, book search, metadata lookup, duplicate detection and audit-record inspection. Every advertised tool is read-only.

The old internal bridge has names for conversion, copy, move and e-mail, but they only create a rejected audit record. They are not advertised through MCP because there is no safe implementation behind them yet.

Adding one of those operations means mapping it to Calibre's own jobs, including progress, cancellation and errors in the GUI. A generic background thread is not an adequate substitute.

## Authentication And Binding

Loopback use may omit a token. Binding to `0.0.0.0`, a LAN address or a container-facing interface requires `CALIBRE_UMCP_BRIDGE_TOKEN`; µMCP's authentication hook then checks `Authorization: Bearer <token>` for `/mcp`.

`GET /health` is intentionally small and unauthenticated. It returns status and plugin version, not library contents.

## Shutdown Without Debris

The synchronous µMCP transport accepts a `server_ready` callback that hands its `ThreadingHTTPServer` to the plugin after binding. The Calibre action can therefore call `shutdown()`, `server_close()` and join the daemon thread instead of abandoning it during plugin reload or application shutdown.

## Packaging Paths

`plugins/build-plugin.sh` builds from the local source tree. `plugins/install-from-gitea.sh` fetches the same six files from the Gitea mirror and assembles the ZIP inside the Calibre container, where a full checkout is unnecessary.
