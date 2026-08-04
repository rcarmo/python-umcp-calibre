# Design notes

## Architecture decision

The project started with a sidecar-first idea, but the safe implementation is now **plugin-first**:

1. **Primary live-library path:** Calibre Interface Action plugin running inside the existing Calibre GUI process.
2. **MCP facade path:** `calibre-umcp` exposes MCP tools and delegates live-library work to the plugin JSON-RPC bridge when `CALIBRE_UMCP_BRIDGE_URL` is configured.
3. **Fallback CLI path:** direct `calibredb` use is limited to read-only operations and dry-run/testing contexts. Mutations fail closed without the plugin bridge.

This avoids direct external mutation of an open Calibre library while still allowing a stable MCP endpoint.

## Implemented plugin

The plugin lives under `plugins/calibre_umcp_plugin` and is packaged with:

```sh
sh plugins/build-plugin.sh
```

Important implementation points:

- Calibre metadata class: `CalibreUmcpPlugin` in `__init__.py`.
- Calibre plugin namespace: `calibre_plugins.calibre_umcp_plugin.ui:CalibreUmcpAction`.
- GUI action: `µMCP Bridge`.
- Menu actions: Start bridge, Bridge status, Stop bridge.
- HTTP endpoints:
  - `GET /health`
  - `POST /rpc`
- JSON-RPC is serialized through a bridge worker queue before touching Calibre DB state.
- Non-loopback binds require `CALIBRE_UMCP_BRIDGE_TOKEN`; unauthorized `/rpc` requests return JSON-RPC-style error bodies.
- UI status reports bridge version, endpoint, auth state, library path, and tracked audit job count.

## Implemented MCP/facade tools

Progressive discovery tools, intended to minimize MCP context use:

- `capabilities_readonly` — compact start-here list, read-only by default.
- `describe_tool_readonly` — detailed guidance for one selected tool.

Read-only tools:

- `bridge_status_readonly`
- `list_libraries_readonly`
- `list_libraries` compatibility alias
- `list_bridge_jobs_readonly`
- `get_bridge_job_status_readonly`
- `search_books_readonly`
- `get_book_metadata_readonly`
- `find_duplicates_readonly`

Mutating tools currently fail closed:

- `convert_book`
- `copy_book`
- `move_book_destructive`
- `email_book`

`move_book_destructive` has explicit destructive MCP annotations.

## Bridge methods

Implemented plugin bridge methods:

- `ping`
- `list_libraries`
- `search_books`
- `get_book_metadata`
- `find_duplicates`
- `list_jobs`
- `get_job_status`

Rejected-but-audited bridge methods:

- `convert_book`
- `copy_book`
- `move_book`
- `email_book`

Rejected mutation attempts create in-memory job records and can append JSONL records when `CALIBRE_UMCP_AUDIT_PATH` is set.

## Calibre Jobs plan

Calibre's job infrastructure should be used for mutators instead of creating an unrelated queue:

- Use existing conversion action flow for `convert_book`; Calibre already queues conversion `ParallelJob`s via `gui.job_manager.run_job()`.
- Use `calibre.gui2.threaded_jobs.ThreadedJob` plus `gui.job_manager.run_threaded_job()` for long-running in-process library operations, with `type_="umcp-bridge"` and `max_concurrent_count=1`.
- Use existing GUI/device APIs for device and email operations where those APIs already create `DeviceJob` or conversion/email jobs.

The bridge's own queue remains useful for serializing JSON-RPC dispatch and for quick read operations, but Calibre Jobs should provide operator-visible progress/logging for real work.

## Required tools

For local testing/development:

- Python 3
- `unittest`
- `zip` for local plugin packaging, or Python `zipfile` in container environments

Inside the Calibre container/profile:

- `calibre-customize` for plugin installation
- `s6-setuidgid abc` on linuxserver/calibre to install with the profile owner


Observed live environment:

- Calibre container: `calibre`
- Config/profile: `/config`
- Plugin dir: `/config/.config/calibre/plugins`
- Library: `/books`

Plugin install was verified with:

```text
Plugin added: Calibre µMCP Bridge (0, 1, 0)
User interface action Calibre µMCP Bridge (0, 1, 0) False
```

The Calibre GUI/container has been restarted after the latest plugin installation and the plugin still reports enabled. The bridge is not auto-started; use the `µMCP Bridge` GUI action to start it.

## Example future sidecar configuration

```yaml
calibre-umcp:
  ports:
    - "9000:9000"
  environment:
    - CALIBRE_LIBRARIES=main=/books,articles=/books/Articles
    - CALIBRE_DEFAULT_LIBRARY=main
    - CALIBRE_UMCP_BRIDGE_URL=http://calibre:9000/rpc
    - CALIBRE_UMCP_BRIDGE_TOKEN=<same-token-as-plugin>
```

The exact bridge URL depends on how the Calibre plugin bridge is bound and exposed inside the Docker network. Keep the bridge loopback-only unless the sidecar/container network requires otherwise, and use `CALIBRE_UMCP_BRIDGE_TOKEN` when exposed beyond loopback.
