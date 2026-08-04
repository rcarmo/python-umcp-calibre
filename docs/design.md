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

The default MCP surface is read-only: progressive discovery, server status, active-library listing, book search, metadata lookup, duplicate detection, authenticated content-server status and audit records.

Mutation discovery has a second gate owned by the Calibre UI. A saved UI token plus explicit enablement exposes metadata, format, cover, import, conversion, export, copy/move, configured-recipient e-mail, trash deletion and conservative duplicate-merge tools. Paths and destination libraries come from UI allowlists rather than request-time host access.

Long work uses Calibre's job machinery. Conversion maps to a `ParallelJob`; import, export, copy/move and e-mail map to `ThreadedJob`. The bridge records its own stable ID alongside Calibre's job ID, then mirrors progress, cancellation and terminal errors without modifying Calibre job objects.

Permanent deletion, arbitrary e-mail recipients, automatic e-mail conversion, temporary public links and device actions fail closed. Those operations either need a stronger policy boundary or depend on live device/server state that Calibre 9.11 does not expose as a safe scoped primitive.

## Authentication And Binding

Loopback use may omit a token. Binding to `0.0.0.0`, a LAN address or a container-facing interface requires `CALIBRE_UMCP_BRIDGE_TOKEN`; µMCP's authentication hook then checks `Authorization: Bearer <token>` for `/mcp`.

`GET /health` is intentionally small and unauthenticated. It returns status and plugin version, not library contents.

## Shutdown Without Debris

The synchronous µMCP transport accepts a `server_ready` callback that hands its `ThreadingHTTPServer` to the plugin after binding. The Calibre action can therefore call `shutdown()`, `server_close()` and join the daemon thread instead of abandoning it during plugin reload or application shutdown.

## Packaging Paths

`plugins/build-plugin.sh` builds from the local source tree. `plugins/install-from-gitea.sh` can fetch the same seven files from a remote source and assemble the ZIP inside a Calibre container, where a full checkout is unnecessary.
