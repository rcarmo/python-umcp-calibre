# Safe architecture

The safe architecture is **plugin-first**.

## Why not direct sidecar writes?


## Chosen design

```text
MCP client
  ↓ Streamable HTTP /mcp
calibre-umcp facade, optional sidecar
  ↓ local HTTP JSON-RPC, optional bearer token
Calibre µMCP Bridge plugin, inside Calibre GUI process
  ↓ Calibre GUI/database APIs, serialized bridge worker and Calibre jobs where appropriate
active Calibre library/database/files
```

The plugin is the authority for live library reads and all future writes. The sidecar is allowed to expose MCP on a fixed port and provide a stable network endpoint, but it delegates live-library operations to the plugin bridge.

## Safety rules

1. The plugin owns all writes to an open Calibre library.
2. Sidecar direct `calibredb` mode is read-only by default.
3. Mutating tools fail closed unless `CALIBRE_UMCP_BRIDGE_URL` points to a plugin bridge.
4. Current mutating bridge methods still reject work until implemented through Calibre's in-process APIs.
5. HTTP handler concurrency is serialized through the bridge worker before touching Calibre state.
6. Long-running mutations should be queued through Calibre `JobManager` / `ThreadedJob` APIs for operator-visible progress and logs.
7. The bridge binds to `127.0.0.1` by default unless `CALIBRE_UMCP_BRIDGE_HOST` is explicitly configured.
8. The bridge refuses to bind beyond loopback unless `CALIBRE_UMCP_BRIDGE_TOKEN` is set.
9. Clients authenticate to `/rpc` with `Authorization: Bearer <token>` when a token is configured; `/health` is intentionally minimal and unauthenticated.

## Bridge API

Implemented JSON-RPC methods:

- `ping`
- `list_libraries`
- `search_books`
- `get_book_metadata`
- `find_duplicates`
- `list_jobs`
- `get_job_status`

Known mutating method names are recognized but rejected with an audit/job record:

- `convert_book`
- `copy_book`
- `move_book`
- `email_book`

## Calibre Jobs mapping

Planned safe mappings for mutators:

- Conversion: reuse Calibre's existing conversion path, which queues `ParallelJob` instances through `gui.job_manager.run_job()`.
- Generic long-running in-process mutations: create `calibre.gui2.threaded_jobs.ThreadedJob` instances and enqueue them with `gui.job_manager.run_threaded_job()`, using `type_="umcp-bridge"` and `max_concurrent_count=1`.
- Device/email flows: prefer existing GUI/device APIs where Calibre already creates `DeviceJob` or conversion/email jobs.

Calibre Jobs are an operational queue/progress/log surface, not a durable structured audit log. The bridge therefore keeps MCP-visible job records and can append JSONL records via `CALIBRE_UMCP_AUDIT_PATH`.



- container: `calibre`
- image: `linuxserver/calibre:latest`
- plugin directory: `/config/.config/calibre/plugins`
- plugin installed successfully as `Calibre µMCP Bridge (0, 1, 0)` using `calibre-customize` as user `abc`.


A sidecar deployment should set:

```sh
CALIBRE_UMCP_BRIDGE_URL=http://calibre:9000/rpc
# or whichever bridge host/port is reachable from the sidecar network
```

Do not grant the sidecar write access to `/books` unless a future operation explicitly requires scratch/output space and remains safe under the plugin-first model.

LazyLibrarian on the media endpoint was aligned with Calibre ownership for incoming files: `PUID=1032`, `PGID=100`, `UMASK=002`, and `UMASK_SET=002`; its `/config` and `/downloads` data were chowned to `1032:100`. The shared `/books` bind mount still reports `1000:1000:777` from the host mount, so ownership there is a host-level mount concern rather than a container UID setting.

## Plugin feasibility

Calibre Interface Action plugins run in the Calibre GUI process and can access the active database object via Calibre APIs. That is the correct place to manipulate the active library safely, rather than editing `metadata.db` from a second process.
