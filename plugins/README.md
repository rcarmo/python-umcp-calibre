# Calibre µMCP plugin

This Interface Action plugin runs a native `umcp.MCPServer` inside the Calibre GUI process. No sidecar is required.

## Build

```sh
sh plugins/build-plugin.sh
```

The resulting `plugins/calibre-umcp-plugin.zip` contains:

- `__init__.py`
- `ui.py`
- `mcp.py`
- `bridge.py`
- `umcp.py`
- `umcp_shared.py`

The last two files are copied from the canonical runtime under `src/calibre_umcp` during packaging.

## Install

```sh
s6-setuidgid abc calibre-customize -a plugins/calibre-umcp-plugin.zip
```

Restart/reload Calibre, then select `µMCP Bridge → Start bridge`.

The plugin exposes:

- MCP Streamable HTTP: `http://<host>:9000/mcp`
- health: `http://<host>:9000/health`

## Install from Gitea mirror

```sh
plugins/install-from-gitea.sh
```

Defaults:

- `CALIBRE_USER=abc`
- `CALIBRE_GROUP=users`

The installer fetches the plugin files plus the canonical µMCP runtime, creates `/tmp/calibre-umcp-plugin.zip`, and installs it with `calibre-customize`.

## Configuration

```sh
CALIBRE_UMCP_BRIDGE_HOST=127.0.0.1
CALIBRE_UMCP_PORT=9000
CALIBRE_UMCP_BRIDGE_TOKEN=<optional-on-loopback-required-otherwise>
CALIBRE_UMCP_AUDIT_PATH=<optional-jsonl-path>
```

For Docker/LAN access, bind `0.0.0.0` and set a long random token. MCP clients must send `Authorization: Bearer <token>`.

## Scope

Implemented MCP tools are read-only: progressive discovery, status, library listing, search, metadata, duplicate detection, and audit record inspection. Conversion, copy, move, and email are deliberately not advertised until safe Calibre job mappings exist.
