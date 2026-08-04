# calibre-umcp

`calibre-umcp` is a Calibre automation MCP server built on Rui Carmo's [`umcp`](https://github.com/rcarmo/umcp).

It is designed around a **plugin-first safe architecture**:

- a Calibre plugin runs inside the existing Calibre process and owns all live library reads/writes;
- an optional container sidecar exposes MCP over a fixed HTTP port and delegates live operations to that plugin bridge.

Direct sidecar mutation of a mounted Calibre library is deliberately not the safe default.

## Calibre Jobs and auditing

Mutating operations are intentionally fail-closed until each one is mapped to Calibre's in-process APIs:

- `convert_book` should reuse Calibre's existing conversion flow, which queues `ParallelJob` work through `gui.job_manager.run_job()`.
- long-running in-process library mutations should be wrapped in `calibre.gui2.threaded_jobs.ThreadedJob` and queued with `gui.job_manager.run_threaded_job()`, using a shared `type_` and `max_concurrent_count=1` for serialized, operator-visible work.
- device/email flows should prefer Calibre's existing GUI/device actions where they already create `DeviceJob` or conversion/email jobs.

Calibre Jobs provide queue/progress/log visibility inside the running Calibre process. They are not a durable structured audit log, so the plugin bridge keeps a small JSON-serializable job/audit scaffold for MCP-visible status and future JSONL audit persistence.

## Initial goals

- Manage one or more Calibre libraries.
- Detect duplicates by title/author/identifier/file hash heuristics.
- Convert books through `ebook-convert`.
- Email books through Calibre's `calibre-smtp` or configured SMTP.
- Copy/move books between libraries through `calibredb`.
- Expose all operations as MCP tools using `umcp`.

## Status

Early scaffold. The current implementation is being pivoted toward a Calibre plugin JSON-RPC bridge for live library operations. The sidecar façade refuses mutating tools unless `CALIBRE_UMCP_BRIDGE_URL` points at that plugin bridge.

See [`docs/architecture.md`](docs/architecture.md) for the safe architecture and [`docs/design.md`](docs/design.md) for the initial feasibility assessment.
