# python-umcp-calibre

Calibre is quite particular about who touches its active library--and rightly so, because the GUI keeps database, cache and filesystem state in memory. I wrote `python-umcp-calibre` to let MCP clients work against that live state without giving a second process permission to improvise around `metadata.db`.

For the released plugin path, the MCP client talks straight to the Calibre GUI process over Streamable HTTP at `/mcp`. The repository still carries a read-only compatibility server under `src/calibre_umcp/server.py`, plus an older JSON-RPC helper at `/rpc` for tests and older wiring, but neither is the released mutation surface.

## What It Exposes

Read-only discovery is small on purpose. Agents should start with `capabilities_readonly()`, then `search_books_readonly(query="", limit=20)`, then `get_book_metadata_readonly(book_id)`. The rest of the read-only surface is:

* `describe_tool_readonly(tool_name)` expands one implemented tool at a time instead of dumping every schema into context.
* `bridge_status_readonly()` reports the bridge version and active library path.
* `list_libraries_readonly()` returns the active Calibre library for the in-process plugin surface.
* `find_duplicates_readonly(limit=1000)` groups probable duplicates by title, authors and identifiers.
* `content_server_status_readonly()` reports only an existing authenticated content-server base URL, and only when the bind is concrete enough to be honest about.
* `list_bridge_jobs_readonly()` and `get_bridge_job_status_readonly(job_id)` expose the bridge's own job and audit records.

Mutations appear only when all four conditions hold:

* Calibre is running exactly 9.11.0.
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

## Boundaries That Matter

* Import, format-replacement and cover paths must live under UI-configured import roots. Exports must stay under UI-configured export roots. Cross-library copy and move destinations must match the UI allowlist exactly, and e-mail can only use Calibre-configured recipients plus the formats enabled for those recipients.
* `content_server_status_readonly()` reports only a concrete authenticated base URL. If the content server is stopped, auth is disabled, or it listens on a wildcard address such as `0.0.0.0`, the tool withholds the URL rather than inventing one. Temporary public links are not implemented.
* Short metadata, format, cover, merge and trash mutations are dispatched onto the GUI thread and are not interruptible once the database call starts. Longer operations use Calibre's own job machinery, and bridge job records tell the truth about partial work and delayed cancellation instead of pretending a killed job changed nothing.
* Permanent deletion, arbitrary recipients, automatic e-mail conversion, public temporary links, device operations, and the obsolete singular `copy_book` and `move_book` methods are not supported. The compatibility server under `src/calibre_umcp/server.py` also keeps legacy mutator names such as `convert_book`, `copy_book`, `move_book_destructive`, and `email_book` as explicit failures.

This release is source-contract and runtime tested against exactly Calibre 9.11.0. The plugin declares 9.11.0 as its minimum so later releases may still load the read-only surface, but mutation discovery and execution fail closed until that exact runtime has been audited again.

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

A non-loopback bind is refused without `CALIBRE_UMCP_BRIDGE_TOKEN`. If a token is configured at all, MCP clients send `Authorization: Bearer <token>` to `POST /mcp`; `GET /health` stays unauthenticated. In container deployments that environment token only authenticates `/mcp`. Mutations stay hidden until you open the plugin's Configure bridge dialog inside Calibre, save the same token there, and check Enable implemented mutation tools. `CALIBRE_UMCP_AUDIT_PATH` may point to a redacted JSONL audit file for bridge job records, and long-running native work still appears in Calibre's own Jobs UI while `list_bridge_jobs_readonly()` remains the bridge ledger.

## Compatibility Paths

* The plugin publishes Streamable HTTP at `POST /mcp` and a small unauthenticated `GET /health` endpoint.
* `calibre-umcp`, or `python -m calibre_umcp.server`, is the older read-only compatibility server. With no flags it uses stdio. With `--http --port N` it serves MCP at `/mcp`, because that transport comes from `umcp.MCPServer` itself.
* If that compatibility server is pointed at `CALIBRE_UMCP_BRIDGE_URL`, it expects the older JSON-RPC helper endpoint such as `http://127.0.0.1:9000/rpc`, not the released plugin `/mcp` endpoint.

The [architecture notes][arch] cover the process boundary, the [design notes][design] explain the implementation choices, the [Calibre 9.11 API map][api] keeps the exact mutation audit in one place, and the [plugin README][plugin] focuses on the ZIP and container path.

[api]: docs/calibre-9.11-api-map.md
[arch]: docs/architecture.md
[design]: docs/design.md
[plugin]: plugins/README.md
