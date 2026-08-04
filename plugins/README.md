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
work=/tmp/calibre-umcp-plugin-src
rm -rf "$work"
mkdir -p "$work"
for f in __init__.py bridge.py ui.py; do
done
python3 - <<'PY'
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
work = Path('/tmp/calibre-umcp-plugin-src')
out = Path('/tmp/calibre-umcp-plugin.zip')
with ZipFile(out, 'w', ZIP_DEFLATED) as zf:
    for name in ('__init__.py', 'bridge.py', 'ui.py'):
        zf.write(work / name, name)
print(out, out.stat().st_size)
PY
chown abc:users /tmp/calibre-umcp-plugin.zip
s6-setuidgid abc calibre-customize -a /tmp/calibre-umcp-plugin.zip
```

## Runtime configuration

Useful environment variables:

- `CALIBRE_UMCP_PORT` — bridge port, default `9000`.
- `CALIBRE_UMCP_BRIDGE_HOST` — bind host, default `127.0.0.1`.
- `CALIBRE_UMCP_BRIDGE_TOKEN` — optional bearer token for `/rpc`.
- `CALIBRE_UMCP_AUDIT_PATH` — optional JSONL file for bridge audit records.

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
