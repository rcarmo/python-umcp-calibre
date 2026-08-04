# python-umcp-calibre

An MCP server that runs **inside Calibre** as an Interface Action plugin. It uses Rui Carmo's [`umcp`](https://github.com/rcarmo/umcp) runtime and accesses the active library through Calibre's in-process database APIs.

There is no sidecar in the deployed architecture.

## Implemented

- Native µMCP Streamable HTTP endpoint: `POST /mcp`
- Health endpoint: `GET /health`
- Calibre GUI actions: Start, Status, Stop
- Optional bearer authentication; mandatory for non-loopback binds
- Serialized access to the active Calibre database
- Progressive discovery:
  - `capabilities_readonly`
  - `describe_tool_readonly`
- Read-only MCP tools:
  - `bridge_status_readonly`
  - `list_libraries_readonly`
  - `search_books_readonly`
  - `get_book_metadata_readonly`
  - `find_duplicates_readonly`
  - `list_bridge_jobs_readonly`
  - `get_bridge_job_status_readonly`

Conversion, copy, move, and email are not exposed as MCP tools because safe Calibre job mappings are not implemented. The older internal bridge recognizes those names only to reject and audit them.

## Build and test

```sh
PYTHONPATH=.:src python3 -W error::ResourceWarning -m unittest discover -s tests -v
sh plugins/build-plugin.sh
```

The plugin ZIP includes `umcp.py` and `umcp_shared.py` from the canonical runtime in `src/calibre_umcp`.

## Install

```sh
calibre-customize -a plugins/calibre-umcp-plugin.zip
```

For linuxserver/calibre, install as the profile owner:

```sh
s6-setuidgid abc calibre-customize -a plugins/calibre-umcp-plugin.zip
```

Restart/reload Calibre after installation, then use `µMCP Bridge → Start bridge`.

## Runtime configuration

Defaults:

```sh
CALIBRE_UMCP_BRIDGE_HOST=127.0.0.1
CALIBRE_UMCP_PORT=9000
```

For network access:

```sh
CALIBRE_UMCP_BRIDGE_HOST=0.0.0.0
CALIBRE_UMCP_PORT=9000
CALIBRE_UMCP_BRIDGE_TOKEN=<long-random-token>
```

Connect an MCP client to:

```text
http://<calibre-host>:9000/mcp
```

When configured, send:

```text
Authorization: Bearer <token>
```

See [`docs/architecture.md`](docs/architecture.md), [`docs/design.md`](docs/design.md), and [`plugins/README.md`](plugins/README.md).
