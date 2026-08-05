# Design

## One Released MCP Path

The plugin subclasses `umcp.MCPServer` and exposes MCP directly from the Calibre process. That avoids a separate writer around `metadata.db`, and it also avoids the more subtle problem of maintaining two protocol stacks that look identical until a client negotiates another transport detail or sends a notification you forgot to mirror.

Four files contain the Calibre-specific plugin code:

* `plugins/calibre_umcp_plugin/ui.py` owns automatic startup, the Calibre menu actions and server lifecycle.
* `plugins/calibre_umcp_plugin/mcp.py` defines the published MCP surface, the `/health` hook and the mutation gate.
* `plugins/calibre_umcp_plugin/bridge.py` serialises work, jumps back to the GUI thread and wraps native Calibre jobs.
* `plugins/calibre_umcp_plugin/__init__.py` carries Calibre's plugin metadata, including the 9.11.0 minimum version declaration.

The build copies `src/calibre_umcp/umcp.py` and `src/calibre_umcp/umcp_shared.py` into the plugin ZIP. Those copied files are packaging inputs, not a second source tree.

## Read-Only First, Mutations Behind A Gate

The published MCP surface stays read-only until Calibre 9.11.0, a UI-saved token, explicit UI mutation enablement, and the active process token all line up. That extra friction is deliberate. An environment-only token can require bearer authentication, but it cannot enable mutations by itself, and a mismatched override token disables mutation discovery rather than trying to be clever.

Read-only discovery is small on purpose: status, active-library listing, bounded search, single-book metadata, duplicate detection, authenticated content-server status, and bridge job records. Mutation discovery adds metadata, format, cover, import, conversion, export, copy/move, configured-recipient e-mail, trash deletion, duplicate merge and job-cancellation tools.

The exact tool names and parameters live in the root `README.md`, because that is the page most people will copy from.

## Paths, Libraries And Recipients Come From Policy

The bridge never treats the client as a general-purpose filesystem or routing authority.

* Import paths, replacement formats and cover files must sit under UI-configured import roots.
* Save-to-disk exports and conversion exports must stay under UI-configured export roots.
* Cross-library copy and move destinations must match the UI allowlist exactly and must resolve to a real `metadata.db`.
* E-mail can only use recipients already configured in Calibre, and only the formats enabled for that recipient.

That keeps the request surface narrow enough to audit without pretending the bridge is a security boundary Calibre never claimed to be.

## Jobs That Tell The Truth

Short database changes run synchronously on the GUI thread, because that is the only thread where Calibre can safely mutate its live state. Long work is handed back to Calibre's own job machinery: conversion uses the JobManager worker path, while import, copy/move, save-to-disk and e-mail use `ThreadedJob`.

The bridge keeps its own stable job IDs alongside Calibre's native ones. That lets it expose one consistent audit trail and say what actually happened when cancellation arrives at an awkward moment: copy can stop between books, move can finish destination verification and still retain the source, save-to-disk can cancel before publication, and SMTP submission can succeed before a cancellation request takes effect.

## Content URLs Stay Narrow

`content_server_status_readonly()` reports only a concrete authenticated base URL from an already-running Calibre content server. Wildcard binds, disabled auth and temporary public links are all treated as out of scope. The bridge is reporting an existing service, not inventing a sharing layer.

## Compatibility Code Still In The Tree

The source tree still contains two older compatibility paths:

* `src/calibre_umcp/server.py` is a read-only compatibility server for older deployments and tests. It runs over stdio by default and can serve MCP at `/mcp` when started with `--http`.
* `plugins/calibre_umcp_plugin/bridge.py` still contains `serve_bridge()`, a lower-level JSON-RPC helper at `/rpc` used by tests and older wiring.

Neither of those is the released mutation surface, and the legacy mutator names they expose stay as explicit failures.

## Packaging Paths

`plugins/build-plugin.sh` builds from the local checkout and produces `plugins/calibre-umcp-plugin.zip`. `plugins/install-from-gitea.sh` fetches the same seven files from a remote source, assembles a ZIP inside a Calibre container, and then installs it. Despite the script name, its default `SOURCE_BASE` points at the repository's raw GitHub URL unless you override it.
