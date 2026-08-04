# calibre-umcp

`calibre-umcp` is a Calibre automation MCP server built on Rui Carmo's [`umcp`](https://github.com/rcarmo/umcp).

It uses a **plugin-first safe architecture** for live Calibre libraries:

- a Calibre Interface Action plugin runs inside the existing Calibre GUI process and owns live library access;
- an optional sidecar/facade exposes MCP over HTTP and delegates live-library operations to the plugin JSON-RPC bridge;
- direct sidecar mutation of a mounted Calibre library is deliberately not the safe default.

The bridge refuses non-loopback binds unless `CALIBRE_UMCP_BRIDGE_TOKEN` is set. Clients then authenticate to `/rpc` with `Authorization: Bearer <token>`.

## Current status

Implemented and tested locally:

- `calibre-umcp` MCP facade with read-only tools and fail-closed mutating tools.
- Calibre plugin package: `plugins/calibre_umcp_plugin`.
- Plugin JSON-RPC bridge with serialized request handling.
- Plugin UI action: `µMCP Bridge`, with Start / Status / Stop menu actions.
- Read-only bridge operations:
  - `ping`
  - `list_libraries`
  - `search_books`
  - `get_book_metadata`
  - `find_duplicates`
- Bridge job/audit visibility:
  - `list_jobs`
  - `get_job_status`
  - optional JSONL audit records via `CALIBRE_UMCP_AUDIT_PATH`
- Mutating operations are intentionally rejected until each one is mapped to Calibre's in-process job APIs.


- Container: `calibre`
- Image: `linuxserver/calibre:latest`
- Config/profile mount: `/config`
- Library mount: `/books`
- Plugin installed successfully with `calibre-customize` as `Calibre µMCP Bridge (0, 1, 0)`.
- A Calibre GUI/container restart is required before the installed Interface Action appears in the GUI.

## Safety model

The plugin is the authority for live library access. The sidecar may expose a stable MCP endpoint, but must not write directly to an open Calibre library.

Mutating MCP tools fail closed unless `CALIBRE_UMCP_BRIDGE_URL` points to the plugin bridge. Even then, current mutators still reject work until their safe Calibre API mapping is implemented.

## Calibre Jobs and auditing

Mutating operations should use Calibre's own queueing where appropriate:

- `convert_book` should reuse Calibre's existing conversion flow, which queues `ParallelJob` work through `gui.job_manager.run_job()`.
- long-running in-process library mutations should use `calibre.gui2.threaded_jobs.ThreadedJob` and `gui.job_manager.run_threaded_job()`, with a shared `type_` and `max_concurrent_count=1` for serialized, operator-visible work.
- device/email flows should prefer Calibre's existing GUI/device actions where they already create `DeviceJob` or conversion/email jobs.

Calibre Jobs provide queue/progress/log visibility inside the running Calibre process. They are not a durable structured audit log, so the plugin bridge also keeps MCP-visible job/audit records and can append JSONL audit records.

## Build and test

```sh
PYTHONPATH=.:src python3 -m unittest discover -s tests -v
sh plugins/build-plugin.sh
```

The plugin build produces `plugins/calibre-umcp-plugin.zip`.

## MCP tools

Read-only/live-safe tools:

- `bridge_status_readonly`
- `list_libraries_readonly`
- `list_libraries` compatibility alias
- `list_bridge_jobs_readonly`
- `get_bridge_job_status_readonly`
- `search_books_readonly`
- `get_book_metadata_readonly`
- `find_duplicates_readonly`

Fail-closed mutating tools:

- `convert_book`
- `copy_book`
- `move_book_destructive`
- `email_book`

See [`docs/architecture.md`](docs/architecture.md), [`docs/design.md`](docs/design.md), and [`plugins/README.md`](plugins/README.md) for details.
