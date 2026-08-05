# Calibre µMCP Plugin

This Interface Action plugin runs `umcp.MCPServer` inside the Calibre GUI process, which lets MCP clients work against the active library without a sidecar writing around `metadata.db`.

## Building The ZIP

```sh
sh plugins/build-plugin.sh
```

The resulting `plugins/calibre-umcp-plugin.zip` contains the plugin package files (`__init__.py`, `bridge.py`, `config.py`, `mcp.py` and `ui.py`), Calibre's `plugin-import-name-calibre_umcp_plugin.txt` namespace marker, and `umcp.py` plus `umcp_shared.py`. The last two are copied from the canonical runtime under `src/calibre_umcp` during packaging and removed from the plugin source directory afterwards.

## Installing In A Container

linuxserver/calibre runs the profile as `abc`, so install the ZIP with matching ownership:

```sh
s6-setuidgid abc calibre-customize -a plugins/calibre-umcp-plugin.zip
```

Restart or reload Calibre. The plugin tries to start MCP about a second after initialisation, once it can resolve the active library. If that does not happen -- usually because the bind or token settings still need fixing -- the `µMCP Bridge` menu exposes Status, Configure, Stop and Start actions for a manual retry.

For installations without a checkout, `plugins/install-from-gitea.sh` fetches the seven source files, adds the Calibre namespace marker, builds `/tmp/calibre-umcp-plugin.zip` and installs it with `calibre-customize`. Despite the script name, its default `SOURCE_BASE` points at the repository's raw GitHub URL unless you override it.

Override `SOURCE_BASE`, `WORK`, `OUT`, `CALIBRE_USER` or `CALIBRE_GROUP` when you need a different source or container layout.

## Binding And Authentication

The safe default is loopback:

```sh
CALIBRE_UMCP_BRIDGE_HOST=127.0.0.1
CALIBRE_UMCP_PORT=9000
```

Docker or LAN clients need an explicit bind and token:

```sh
CALIBRE_UMCP_BRIDGE_HOST=0.0.0.0
CALIBRE_UMCP_PORT=9000
CALIBRE_UMCP_BRIDGE_TOKEN=<long-random-token>
```

The plugin publishes Streamable HTTP at `POST /mcp` and a small unauthenticated `GET /health` endpoint. A non-loopback bind is refused without `CALIBRE_UMCP_BRIDGE_TOKEN`. If a token is configured at all, clients send `Authorization: Bearer <token>` on `/mcp`, even on loopback.

`CALIBRE_UMCP_CONTENT_SERVER_ADVERTISED_HOST` may provide a hostname or IP address (without scheme, port, or path) when Calibre's authenticated content server binds to a wildcard address. Without it, `content_server_status_readonly()` returns no URL and the stable reason code `ADVERTISED_CONTENT_SERVER_HOST_NOT_CONFIGURED`.

`CALIBRE_UMCP_AUDIT_PATH` may point to a redacted JSONL audit file for bridge job records. The UI also stores an in-memory audit retention value between 10 and 10000 records, defaulting to 500. Long-running native work still appears in Calibre's own Jobs UI; `list_bridge_jobs_readonly()` is the bridge ledger.

MCP clients should reconnect and refresh `tools/list` after plugin upgrades whenever `toolset_version` changes; gateways may otherwise retain an older tool catalogue.

## Read-Only Quality Assessment

The MCP-only quality workflow adds `get_book_formats_readonly`, `inspect_book_format_readonly`, `assess_book_quality_readonly` and `compare_book_quality_readonly`. The first release deeply inspects EPUB only. It reports path-free size and modification metadata, container validity, embedded metadata agreement, cover and TOC structure, bounded text metrics and explainable scoring reasons.

Inspection never returns ebook text or internal paths. Files, archive entries, expanded size, scanned content and elapsed time are bounded; unsupported formats, missing files, read failures and exceeded limits use stable structured error codes. Assessment and comparison degrade safe inspection failures into penalized `grade: unknown` results with machine-readable `inspection_errors`, rather than aborting duplicate triage. All database access remains library-broker-backed and the visible GUI library is never switched implicitly.

Cross-library duplicate matching scans one source chunk against one target-library segment per call. Use `next_cursor` unchanged with the same arguments until it becomes null. `source_scanned`, `source_total_known`, `target_libraries_scanned`, `target_books_scanned` and `candidate_queries` expose bounded progress without revealing paths.

## Mutation Gate And Current Limits

Mutation tools only appear when the runtime is exactly Calibre 9.12.0, a token has been saved in the plugin UI, and mutation discovery is explicitly enabled there. An environment-only token can enforce HTTP auth, but it does not enable mutations by itself, and a different override token disables mutation discovery. In container deployments that means `CALIBRE_UMCP_BRIDGE_TOKEN` is not enough on its own: save the same token in the plugin UI and check Enable implemented mutation tools if you want `capabilities_mutation()` to appear.

Import and replacement paths are confined to configured roots. Exports stay under configured export roots. Destination libraries use an exact UI allowlist. E-mail can only use a recipient already configured in Calibre, and only a format already enabled for that recipient.

`content_server_status_readonly()` reports only an existing authenticated content-server base URL, and only when the server is running on a concrete advertised host. Wildcard binds, disabled auth and temporary public links stay out of scope.

Permanent deletion, arbitrary recipients, implicit e-mail conversion, public temporary links and device actions are unavailable. Moves use Calibre trash only after a verified copy. The obsolete singular copy and move methods are rejected, and the older `/rpc` helper in `bridge.py` is not the published plugin endpoint.
