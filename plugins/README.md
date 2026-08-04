# Calibre µMCP Bridge plugin

This directory contains the Calibre Interface Action plugin that runs inside the Calibre GUI process and exposes a small local JSON-RPC bridge for `calibre-umcp`.

## Build locally

```sh
sh plugins/build-plugin.sh
```

This produces `plugins/calibre-umcp-plugin.zip`. The build script removes/excludes `__pycache__` and `.pyc` files.

## Install in a Calibre profile

Inside a Calibre container/profile, install with:

```sh
calibre-customize -a /path/to/calibre-umcp-plugin.zip
```

For linuxserver/calibre, install as the profile owner where possible:

```sh
s6-setuidgid abc calibre-customize -a /path/to/calibre-umcp-plugin.zip
```

After installing, restart Calibre or reload the GUI. The plugin action appears as `µMCP Bridge` and exposes menu items to start, inspect, or stop the bridge.


Discovered live profile:

- container: `calibre`
- image: `linuxserver/calibre:latest`
- profile/config mount: `/config`
- plugin directory: `/config/.config/calibre/plugins`
- library mount: `/books`
- container user: `abc` (`uid=1032`, `gid=100`)
- helper: `/usr/bin/s6-setuidgid`
- installer: `/usr/bin/calibre-customize`
- Python: `/lsiopy/bin/python3`

Live install was completed successfully from the pushed source:

```text
/tmp/calibre-umcp-plugin.zip 4796
Plugin added: Calibre µMCP Bridge (0, 1, 0)

User interface action Calibre µMCP Bridge (0, 1, 0) False
  Expose a local JSON-RPC bridge for safe calibre-umcp live-library access.
```

A GUI/container restart is still required for Calibre to load the newly installed Interface Action.

## Installing from Gitea inside the container


```sh
plugins/install-from-gitea.sh
```

When copied into the Calibre container, it fetches `__init__.py`, `bridge.py`, and `ui.py` from Gitea, builds `/tmp/calibre-umcp-plugin.zip` with Python `zipfile`, and installs it with `calibre-customize`. Override these environment variables if needed:

- `WORK` — default `/tmp/calibre-umcp-plugin-src`
- `OUT` — default `/tmp/calibre-umcp-plugin.zip`
- `CALIBRE_USER` — default `abc`
- `CALIBRE_GROUP` — default `users`

## Runtime configuration


```yaml
environment:
  - CALIBRE_UMCP_BRIDGE_HOST=0.0.0.0
  - CALIBRE_UMCP_PORT=9000
  - CALIBRE_UMCP_BRIDGE_TOKEN=<long-random-token>
```

Use the same token in the MCP sidecar/facade:

```sh
CALIBRE_UMCP_BRIDGE_URL=http://calibre:9000/rpc
CALIBRE_UMCP_BRIDGE_TOKEN=<long-random-token>
```

Useful environment variables:

- `CALIBRE_UMCP_PORT` — bridge port, default `9000`.
- `CALIBRE_UMCP_BRIDGE_HOST` — bind host, default `127.0.0.1`.
- `CALIBRE_UMCP_BRIDGE_TOKEN` — bearer token for `/rpc`. Optional only for loopback binds; required by the bridge when `CALIBRE_UMCP_BRIDGE_HOST` is not loopback.
- `CALIBRE_UMCP_AUDIT_PATH` — optional JSONL file for bridge audit records.

Authentication policy:

- default bind is `127.0.0.1`, where the token is optional for local-only use;
- binding to `0.0.0.0`, a LAN IP, or a Docker DNS name without `CALIBRE_UMCP_BRIDGE_TOKEN` is refused at bridge startup;
- authenticated clients must send `Authorization: Bearer <token>` to `/rpc`;
- `/health` remains unauthenticated and returns only `{"ok": true}`.

## Bridge methods

Implemented read/status methods:

- `ping` — returns `ok`, bridge `version`, and current `library_path`
- `list_libraries`
- `search_books`
- `get_book_metadata`
- `find_duplicates`
- `list_jobs`
- `get_job_status`

Mutating methods are recognized but deliberately rejected until implemented with Calibre's in-process job APIs:

- `convert_book`
- `copy_book`
- `move_book`
- `email_book`

Rejected mutation attempts create bridge job/audit records, and can be persisted to JSONL via `CALIBRE_UMCP_AUDIT_PATH`.
