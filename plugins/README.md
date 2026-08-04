# Calibre µMCP Plugin

This Interface Action plugin runs `umcp.MCPServer` inside the Calibre GUI process, which lets MCP clients read the active library without a sidecar or a second writer touching `metadata.db`.

## Building The ZIP

```sh
sh plugins/build-plugin.sh
```

The resulting `plugins/calibre-umcp-plugin.zip` contains the four plugin files (`__init__.py`, `ui.py`, `mcp.py` and `bridge.py`) plus `umcp.py` and `umcp_shared.py`. The latter two come from the canonical runtime under `src/calibre_umcp` and are removed from the plugin source directory after packaging.

## Installing In A Container

linuxserver/calibre runs the profile as `abc`, so install the ZIP with matching ownership:

```sh
s6-setuidgid abc calibre-customize -a plugins/calibre-umcp-plugin.zip
```

Restart or reload Calibre. The plugin starts MCP automatically after the active library loads; the `µMCP Bridge` menu can inspect, stop or restart it. MCP is at `http://<host>:9000/mcp`, with health status at `http://<host>:9000/health`.

For installations without a checkout, `plugins/install-from-gitea.sh` fetches the six files from the public repository, builds `/tmp/calibre-umcp-plugin.zip` and installs it with `calibre-customize`.

Override `SOURCE_BASE`, `WORK` or `OUT` when installing from another source or temporary directory.

## Binding And Authentication

The safe default is loopback-only:

```sh
CALIBRE_UMCP_BRIDGE_HOST=127.0.0.1
CALIBRE_UMCP_PORT=9000
```

Docker or LAN clients need an explicit bind and token:

```sh
CALIBRE_UMCP_BRIDGE_HOST=0.0.0.0
CALIBRE_UMCP_PORT=9000
CALIBRE_UMCP_BRIDGE_TOKEN=<long-random-token>
```

The server refuses a non-loopback bind without that token. `CALIBRE_UMCP_AUDIT_PATH` may point to a JSONL file for rejected-operation audit records.

## Current Limits

The MCP tools cover discovery, status, library listing, search, metadata, duplicate detection and audit records. Conversion, copying, moving and e-mail are omitted until they can use Calibre's own job APIs rather than an ad-hoc worker.
