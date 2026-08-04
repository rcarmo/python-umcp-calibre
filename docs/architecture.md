# Safe architecture

The safe architecture is **plugin-first**.

## Why not direct sidecar writes?


## Chosen design

```text
MCP client
  ↓ Streamable HTTP /mcp
calibre-umcp façade, optional sidecar
  ↓ local HTTP JSON-RPC, authenticated/shared LAN only
Calibre µMCP Bridge plugin, inside Calibre process
  ↓ Calibre GUI/database APIs, serialized by plugin worker
active Calibre library/database/files
```

The plugin is the authority for all library mutation. The sidecar is allowed to expose MCP on a fixed port and provide a stable network endpoint, but it delegates any operation that reads or mutates a live Calibre library to the plugin bridge.

## Safety rules

1. The plugin owns all writes to an open Calibre library.
2. Sidecar direct `calibredb` mode is read-only by default.
3. Mutating tools fail closed unless `CALIBRE_UMCP_BRIDGE_URL` points to a plugin bridge.
4. The plugin serializes operations through one queue/worker.
5. Initial bridge binds to `127.0.0.1` inside the Calibre container unless explicitly configured.



- install the plugin in the `linuxserver/calibre` container/profile;
- have the plugin listen on an internal port, e.g. `127.0.0.1:9100` or a compose-only bridge network address;
- run `calibre-umcp` on exposed port `9000` with `CALIBRE_UMCP_BRIDGE_URL=http://calibre:9100/rpc`;
- do not grant the sidecar write access to `/books` unless needed for conversion scratch space.

## Plugin feasibility

Calibre supports Python Interface Action plugins. Those plugins run in the Calibre GUI process and can access the active database object via Calibre APIs. That is the correct place to manipulate the active library safely, rather than editing `metadata.db` from a second process.
