# python-umcp-calibre

Calibre is quite particular about who touches its active library--and rightly so, because the GUI keeps database, cache and filesystem state in memory. I wrote `python-umcp-calibre` to let MCP clients work against that live state without giving a second process permission to improvise around `metadata.db`.

For the released plugin path, the MCP client talks straight to the Calibre GUI process over Streamable HTTP at `/mcp`. The repository still carries a read-only compatibility server under `src/calibre_umcp/server.py`, plus an older JSON-RPC helper at `/rpc` for tests and older wiring, but neither is the released mutation surface.

## What It Exposes

Read-only discovery is small on purpose. Agents should start with `capabilities_readonly()`, then `list_libraries_readonly()`, then `search_books_readonly(query="", library="current", limit=20)`. Read results use bounded object envelopes with canonical aliases, stable ordering, truncation metadata and opaque cursors. Capability and status responses include `schema_version` and `toolset_version`; clients should reconnect and refresh `tools/list` whenever the server's toolset version changes. The rest of the read-only surface is:

* `describe_tool_readonly(tool_name)` expands one implemented tool at a time instead of dumping every schema into context.
* `bridge_status_readonly()` reports the bridge version, active alias and active generation without exposing library paths.
* `list_libraries_readonly()` returns only UI-configured aliases, labels, availability and policy flags. It also distinguishes configured from currently usable cross-library access through `cross_library_configured`, `cross_library_available`, `readable_target_count`, and `cross_library_reason_code`.
* `get_book_metadata_readonly(book_id, library="current")` returns a scoped `{library, book_id}` reference.
* `get_book_formats_readonly(book_id, library="current")` returns path-free size, modification-time and availability metadata for each format.
* `inspect_book_format_readonly(book_id, format="EPUB", library="current")` performs bounded EPUB container, metadata, cover, TOC and content-signal checks without returning book text.
* `assess_book_quality_readonly(...)` assigns a conservative score with stable, explainable reason and warning codes; EPUB is the only deeply inspected format in this first release.
* `compare_book_quality_readonly(left, right)` compares two library-scoped records entirely through MCP and recommends which candidate to keep, but never mutates either record.
* `find_duplicates_readonly(library="current", limit=1000)` groups probable duplicates through Calibre's 9.12 `new_api.all_book_ids()` enumeration.
* `find_cross_library_duplicates_readonly(source_library, target_libraries, limit=5, target_limit=100, cursor="")` compares one bounded source/target segment by normalized identifiers and title/authors without switching the visible GUI library. Partial responses include progress fields and an opaque `next_cursor`.
* `content_server_status_readonly()` reports only an existing authenticated content-server base URL, and only when the bind is concrete enough to be honest about.
* `list_bridge_jobs_readonly()` and `get_bridge_job_status_readonly(job_id)` expose the bridge's own job and audit records.

Quality inspection never returns absolute paths or ebook text. EPUB work is bounded to 64 MiB files, 4096 archive entries, 256 MiB expanded data, 8 MiB of content scanning and five seconds. Direct inspection calls return stable errors for unsupported, unavailable, unreadable, oversized or timed-out formats. Quality assessment and comparison instead degrade safe inspection failures into penalized normal results with `inspection_errors`, so one malformed candidate cannot abort duplicate triage.

Mutations appear only when all four conditions hold:

* Calibre is running exactly 9.12.0.
* The plugin UI has a saved bearer token.
* The plugin UI has mutation discovery enabled.
* If `CALIBRE_UMCP_BRIDGE_TOKEN` overrides the token at process start, it matches the UI-saved token. An environment-only token can enforce HTTP auth, but it does not unlock mutations by itself.

Once that gate opens, `capabilities_mutation()` advertises the current mutators:

* `update_book_metadata_mutation(book_id, changes)` supports `title`, `authors`, `series`, `series_index`, `tags`, `identifiers`, `publisher`, `language`, `languages`, `comments`, `rating`, `pubdate`, `timestamp`, and custom columns via `custom` or `#column_name`.
* `add_book_format_mutation(book_id, path, format="", replace=False)` imports one format from a configured import root.
* `delete_book_format_mutation(book_id, format, allow_last_format=False)` removes one explicit format; deleting the final remaining format needs `allow_last_format=true`.
* `set_book_cover_mutation(book_id, path="", remove=False)` replaces or removes the cover from a configured import root.
* `add_book_mutation(path, format="", duplicate_policy="reject")` imports one book through a native `ThreadedJob`; `duplicate_policy` is `reject`, `skip`, or `add`.
* `delete_books_mutation(book_ids, dry_run=True, confirmation="", permanent=False)` previews first, then moves confirmed books to Calibre trash. `permanent=true` is rejected.
* `merge_duplicates_mutation(survivor_id, source_ids, confirmation, replace_cover=False, save_alternate_cover=False)` keeps the source records, adds only missing formats to the survivor, and runs Calibre's conservative metadata merge.
* `convert_book_mutation(book_id, output_format, replace_existing=False, options={}, store_result=True, export_path="", overwrite_export=False)` queues one native conversion job. Supported `options` keys are `base_font_size`, `font_size_mapping`, `line_height`, `margin_top`, `margin_right`, `margin_bottom`, `margin_left`, `output_profile`, `input_encoding`, `remove_paragraph_spacing`, `insert_blank_line`, `chapter`, `chapter_mark`, `page_breaks_before`, and `pretty_print`.
* `copy_books_to_library_mutation(book_ids, destination_library, duplicate_policy="reject", destination_book_ids={})` copies into an exact UI-allowlisted library. `duplicate_policy` is `reject`, `skip`, `add`, `merge_missing`, or `replace`; the merge policies need an explicit `destination_book_ids` map.
* `move_books_to_library_mutation(book_ids, destination_library, dry_run=True, confirmation="", duplicate_policy="reject", destination_book_ids={})` does a preview first, then verified copy, then source trash. It never promises an all-or-nothing move if cancellation or destination verification fails.
* `save_book_to_disk_mutation(book_id, destination_directory, options={}, overwrite=False)` exports through Calibre's save-to-disk engine into a configured export root. Supported `options` keys are `template`, `formats`, `save_cover`, `write_opf`, `save_extra_files`, `update_metadata`, `asciiize`, `to_lowercase`, `replace_whitespace`, and `single_dir`.
* `email_book_mutation(book_id, recipient, format, auto_convert=False)` submits one existing format to one already-configured Calibre recipient. `auto_convert` is accepted as a parameter only so the tool can reject it cleanly; queue a conversion separately if the requested format is missing.
* `cancel_bridge_job_mutation(job_id)` asks Calibre to cancel a queued or running native job and reports the result at the next safe boundary.
* `switch_library_mutation(library, expected_active_library, expected_active_generation, confirmation)` is separately enabled and requires the exact confirmation `SWITCH_LIBRARY:<alias>`; it uses Calibre's GUI switch path with repair disabled.

Every mutation that depends on the active library also accepts optional `expected_active_library` and `expected_active_generation` guards. A stale request fails before changing Calibre.

## Boundaries That Matter

* Import, format-replacement and cover paths must live under UI-configured import roots. Exports must stay under UI-configured export roots. Cross-library copy and move destinations use registry aliases marked as copy destinations; raw allowlisted paths remain an internal one-release compatibility path. E-mail can only use Calibre-configured recipients plus the formats enabled for those recipients.
* Inactive reads use Calibre's `GuiLibraryBroker` secondary handles. The bridge never opens `metadata.db` directly and never switches the visible library as a side effect of a read.
* Library aliases match `^[a-z][a-z0-9_-]{0,63}$`. Registry paths and Calibre library identities remain private, and an identity mismatch fails closed until the operator reviews the configuration.
* `content_server_status_readonly()` reports only a concrete authenticated base URL. If the content server is stopped, auth is disabled, or it listens on a wildcard address such as `0.0.0.0`, the tool withholds the URL rather than inventing one. Temporary public links are not implemented.
* Short metadata, format, cover, merge and trash mutations are dispatched onto the GUI thread and are not interruptible once the database call starts. Longer operations use Calibre's own job machinery, and bridge job records tell the truth about partial work and delayed cancellation instead of pretending a killed job changed nothing.
* Permanent deletion, arbitrary recipients, automatic e-mail conversion, public temporary links, device operations, and the obsolete singular `copy_book` and `move_book` methods are not supported. The compatibility server under `src/calibre_umcp/server.py` also keeps legacy mutator names such as `convert_book`, `copy_book`, `move_book_destructive`, and `email_book` as explicit failures.

This release is source-contract and runtime tested against exactly Calibre 9.12.0. The plugin declares 9.12.0 as its minimum so later releases may still load the read-only surface, but mutation discovery and execution fail closed until that exact runtime has been audited again.

## Building It

```sh
PYTHONPATH=.:src python3 -W error::ResourceWarning -m unittest discover -s tests -v
sh plugins/build-plugin.sh
```

The build produces `plugins/calibre-umcp-plugin.zip`. It copies `umcp.py` and `umcp_shared.py` from `src/calibre_umcp` into the archive, so the plugin uses the same runtime as the rest of the repository rather than carrying a second protocol implementation.

## Installing It

Install the ZIP with Calibre's plugin utility:

```sh
calibre-customize -a plugins/calibre-umcp-plugin.zip
```

linuxserver/calibre profiles are normally owned by `abc`, so install as that user inside the container:

```sh
s6-setuidgid abc calibre-customize -a plugins/calibre-umcp-plugin.zip
```

Restart or reload Calibre after replacing the plugin. The plugin tries to start MCP about a second after initialisation, once it can resolve the active library. If that does not happen -- usually because the bind or token settings still need fixing -- the `µMCP Bridge` menu exposes Status, Configure, Stop and Start actions for a manual retry.

## Connecting

The safe default is loopback:

```sh
CALIBRE_UMCP_BRIDGE_HOST=127.0.0.1
CALIBRE_UMCP_PORT=9000
```

To reach the plugin across a container or LAN network, bind explicitly and set a long random token:

```sh
CALIBRE_UMCP_BRIDGE_HOST=0.0.0.0
CALIBRE_UMCP_PORT=9000
CALIBRE_UMCP_BRIDGE_TOKEN=<long-random-token>
```

A non-loopback bind is refused without `CALIBRE_UMCP_BRIDGE_TOKEN`. If a token is configured at all, MCP clients send `Authorization: Bearer <token>` to `POST /mcp`; `GET /health` stays unauthenticated. In container deployments that environment token only authenticates `/mcp`. When Calibre's content server listens on a wildcard address, set `CALIBRE_UMCP_CONTENT_SERVER_ADVERTISED_HOST` to a hostname or IP address (without a scheme, port, or path) if `content_server_status_readonly()` should return an actionable URL. Stable `reason_code` values explain every withheld URL. Mutations stay hidden until you open the plugin's Configure bridge dialog inside Calibre, save the same token there, and check Enable implemented mutation tools. `CALIBRE_UMCP_AUDIT_PATH` may point to a redacted JSONL audit file for bridge job records, and long-running native work still appears in Calibre's own Jobs UI while `list_bridge_jobs_readonly()` remains the bridge ledger.

## Compatibility Paths

* The plugin publishes Streamable HTTP at `POST /mcp` and a small unauthenticated `GET /health` endpoint.
* `calibre-umcp`, or `python -m calibre_umcp.server`, is the older read-only compatibility server. With no flags it uses stdio. With `--http --port N` it serves MCP at `/mcp`, because that transport comes from `umcp.MCPServer` itself.
* If that compatibility server is pointed at `CALIBRE_UMCP_BRIDGE_URL`, it expects the older JSON-RPC helper endpoint such as `http://127.0.0.1:9000/rpc`, not the released plugin `/mcp` endpoint.

The [architecture notes][arch] cover the process boundary, the [design notes][design] explain the implementation choices, the [multiple-library design][libraries] defines discovery, inactive reads, switching and cross-library duplicate checks, the [Calibre 9.12 API map][api] keeps the exact mutation audit in one place, and the [plugin README][plugin] focuses on the ZIP and container path.

[api]: docs/calibre-9.12-api-map.md
[arch]: docs/architecture.md
[design]: docs/design.md
[libraries]: docs/multiple-libraries.md
[plugin]: plugins/README.md
