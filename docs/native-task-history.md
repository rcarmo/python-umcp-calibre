# Native task history — v0.3.5

The GUI plugin exposes native Calibre tasks through three read-only MCP tools. This includes scheduled news and automatic email jobs which were absent from the bridge's submission ledger. Toolset version is 10; schema version remains 2.

## Capture and retention

`list_native_tasks_readonly` supports status/query filters and offset pagination (maximum 200 records). `get_native_task_readonly` returns metadata for one opaque task identifier. `get_native_task_log_readonly` reads Unicode-character pages, up to 16,000 characters per call.

JobManager's `job_added` and `job_done` signals trigger capture on the GUI thread. A one-second timer captures progress and log changes. Observation never starts, cancels or changes native jobs. Capture considers at most 2,000 native jobs, using Calibre's active-first/newest-first ordering; old completed GUI jobs outside the retention window are excluded.

History persists in `calibre-umcp-task-history.sqlite` under Calibre's configuration directory. Retention is 30 days, with at most 2,000 records and 32 KiB of redacted log text per record. A record's retention age starts when first observed. Queued/running records become `interrupted` on restart. Disk errors preserve live history and appear as `persistence_error` in list responses. The database requires a regular file with mode 0600 and the expected schema; incompatible files remain untouched.

Bridge job identifiers and native task identifiers remain separate. A bridge link requires an explicit native-object identity match. Descriptions do not establish book associations. Native completion does not confirm SMTP acceptance or Kindle delivery. Removed native logs cannot be reconstructed, and GUI task metadata disappears when Calibre restarts unless already captured.

## Privacy and cost

Recipients, filesystem paths, protocol transcript lines, credential assignments, the bridge token and configured SMTP credentials are redacted before persistence. Relay passwords are also removed in decoded and base64 forms. Configuration-read failures suppress text. Loaded records and returned text are redacted again.

The observation cache contains content digests and already-redacted output, never raw log text. Credential changes invalidate it. Unchanged observations perform no database writes; changed observations update only changed rows and retention deletions in one transaction.

## Validation — 19 October 2026

- 160 unit/source-contract tests passed against the pinned Calibre 9.15.0 source tree, with ResourceWarning treated as an error.
- Isolated Calibre 9.15.0 runtime checks passed for live ThreadedJob/GUILog text, consolidated JSON logs, ParallelJob files, redaction, persistence/restart and real JobManager asynchronous add/done signals.
- Existing runtime checks passed for native copies, merge/replace policies, confined exports and a local SMTP sink. Content-server checks returned anonymous 401 and authenticated 200 without exposing filesystem paths. No production email was sent by validation.
- A 2,000-record native-task workload took 3.37 seconds for three unchanged profiled observations before tuning, and 0.56 seconds after caching redacted text (about 83% lower elapsed time). Incremental persistence is covered by a one-row-write assertion.
- Pre-release CPU/allocation profiling passed the full suite. Cumulative time includes worker shutdown/waits and test harness work; retained allocations were mainly imports and test infrastructure. No additional production hotspot was established by that suite. Raw profiling captures were removed after analysis.

Use `make test`, `make prerelease-test`, `make runtime-test` and `make build`. Runtime/source trees can be selected with `CALIBRE_RUNTIME_ROOT` and `CALIBRE_SOURCE_ROOT`. Build output is `<resolved PROJECT_TMP_ROOT>/build/calibre-umcp-plugin.zip`. Production deployment must preserve library, scheduled-news and SMTP configuration and refresh the MCP tool catalogue.
