# python-umcp-calibre

Calibre keeps its database, caches and filesystem state in the GUI process. `python-umcp-calibre` runs an MCP server in that process, so clients use Calibre's APIs and job machinery without opening `metadata.db` from a sidecar.

Version 0.3.2 provides the released plugin and an older read-only compatibility server. Mutations are tested against exactly Calibre 9.12.0; the plugin hides them on every other Calibre version.

## Process boundaries

| Component | Location | Interface | Library access |
|---|---|---|---|
| Calibre Interface Action plugin | `plugins/calibre_umcp_plugin/` | Streamable HTTP | Runs in the Calibre GUI process and uses the live database object |
| Read-only compatibility server | `src/calibre_umcp/server.py` | stdio or Streamable HTTP | Uses Calibre CLI commands, or the older JSON-RPC bridge when configured |
| JSON-RPC bridge helper | `serve_bridge()` in `plugins/calibre_umcp_plugin/bridge.py` | HTTP JSON-RPC | Kept for tests and older wiring; the plugin UI does not publish it |

Inactive-library reads use Calibre's `GuiLibraryBroker` handles. They do not switch the visible GUI library. Explicit library switching goes through Calibre's GUI action and requires a generation guard plus `SWITCH_LIBRARY:<alias>` confirmation.

## HTTP endpoints

| Server | Method and path | Authentication | Use |
|---|---|---|---|
| Released plugin | `POST /mcp` | Bearer token when configured | Streamable HTTP MCP endpoint |
| Released plugin | `GET /health` | None | Returns plugin, schema and toolset versions |
| Compatibility server | `POST /mcp` | Depends on the compatibility server deployment | Streamable HTTP MCP endpoint when started with `--http` |
| JSON-RPC helper | `POST /rpc` | Bearer token when configured | Older bridge protocol used by compatibility code and tests |
| JSON-RPC helper | `GET /health` | None | Basic helper-process liveness check |

The released plugin endpoint is `/mcp`. `CALIBRE_UMCP_BRIDGE_URL` belongs to the compatibility client and expects the older `/rpc` endpoint.

## Tool discovery

Start with `capabilities_readonly()`. Read `list_libraries_readonly()` before choosing a library, then search with a small result limit and fetch details for one selected book. Responses include `schema_version` and `toolset_version`; reconnect and refresh `tools/list` after either value changes.

### Read-only tools

| Tool | Inputs | Result |
|---|---|---|
| `capabilities_readonly` | None | Versions, limits, stable errors and a compact tool index |
| `describe_tool_readonly` | `tool_name` | Arguments and result notes for one exposed tool |
| `bridge_status_readonly` | None | Plugin version, active library alias and generation |
| `list_libraries_readonly` | None | Configured aliases, labels, availability and policy flags |
| `search_books_readonly` | `query`, `limit`, `library`, `cursor` | Bounded metadata rows with an opaque continuation cursor |
| `get_book_metadata_readonly` | `book_id`, `library` | Metadata for one library-scoped book ID |
| `get_book_formats_readonly` | `book_id`, `library` | Path-free format sizes, modification times and availability |
| `inspect_book_format_readonly` | `book_id`, `format`, `library`, `include_text_sample=false` | Bounded EPUB structure and content signals; no ebook text |
| `assess_book_quality_readonly` | `book_id`, `library`, optional `formats` | Score, reasons, warnings and structured inspection errors |
| `compare_book_quality_readonly` | `left`, `right`, `policy` | Two assessments and a non-mutating retention proposal |
| `find_duplicates_readonly` | `library`, `limit`, `target_limit`, `cursor` | One bounded pair-comparison segment |
| `find_cross_library_duplicates_readonly` | source and target aliases, limits, optional query and cursor | One bounded cross-library comparison segment |
| `content_server_status_readonly` | None | Authenticated content-server URL when it can be stated unambiguously |
| `list_scheduled_news_readonly` | None | Recipe URN, schedule, last download and customisation metadata |
| `list_bridge_jobs_readonly` | None | Bridge audit and job records |
| `get_bridge_job_status_readonly` | `job_id` | One bridge audit or job record |

EPUB inspection accepts files up to 64 MiB and archives with at most 4,096 entries or 256 MiB of expanded data. It scans no more than 8 MiB of content and runs for at most five seconds. Assessment and comparison convert supported inspection failures into `grade: unknown` results with `inspection_errors`; a direct inspection call returns the corresponding stable error.

Duplicate searches are segmented. Pass `next_cursor` back unchanged with the same arguments until it becomes `null`.

## Mutation gate

`capabilities_mutation()` and all mutation tools appear only when every condition in this table holds:

| Condition | Required state |
|---|---|
| Calibre runtime | Exactly 9.12.0 |
| UI token | Saved in the plugin configuration |
| Mutation setting | **Enable implemented mutation tools** checked in the plugin UI |
| Environment override | `CALIBRE_UMCP_BRIDGE_TOKEN`, when set, matches the saved UI token |

An environment-only token can authenticate HTTP requests. It cannot enable mutations. A mismatched environment override hides the mutation surface.

Metadata, format, cover, recipe-schedule, import, deletion, merge, conversion, copy, move, export and e-mail mutations accept `expected_active_library` and `expected_active_generation`. The bridge checks both before changing Calibre. Library switching requires both guards.

### Mutation tools

| Tool | Main inputs | Operation |
|---|---|---|
| `capabilities_mutation` | None | Mutation policy, stable errors and a compact tool index |
| `update_book_metadata_mutation` | `book_id`, `changes` | Updates validated standard or custom metadata with rollback |
| `begin_import_attachment_mutation` | filename, byte count, SHA-256, optional format | Opens a bounded staged upload |
| `append_import_attachment_mutation` | `upload_handle`, base64 chunk | Appends one bounded chunk |
| `finalize_import_attachment_mutation` | `upload_handle` | Verifies size and SHA-256, then returns a one-time staged handle |
| `stage_import_attachment_mutation` | filename, base64 content, optional format | One-call staging for small attachments |
| `add_book_format_mutation` | `book_id`, configured path or staged handle, format, `replace` | Adds or replaces one format |
| `delete_book_format_mutation` | `book_id`, format, `allow_last_format` | Removes one format; final-format deletion needs explicit permission |
| `set_book_cover_mutation` | `book_id`, configured path or `remove=true` | Replaces or removes a cover with rollback |
| `add_book_mutation` | configured path or staged handle, format, duplicate policy | Queues a native Calibre book import |
| `download_scheduled_news_mutation` | recipe `urn` | Queues an existing recipe through `FetchNewsAction` and Calibre's scheduler |
| `update_scheduled_news_schedule_mutation` | `urn`, `days_of_week`, `hour`, `minute` | Changes an existing weekly schedule through `RecipeModel.schedule_recipe()` |
| `delete_books_mutation` | IDs, dry-run flag, confirmation | Previews, then moves confirmed books to Calibre trash |
| `merge_duplicates_mutation` | survivor ID, source IDs, confirmation, cover options | Adds missing formats and merges metadata while retaining source records |
| `convert_book_mutation` | book ID, output format, options, result/export policy | Queues a native Calibre conversion job |
| `copy_books_to_library_mutation` | IDs, destination alias, duplicate policy, optional destination map | Copies books and verifies destination hashes |
| `move_books_to_library_mutation` | copy inputs plus dry-run and confirmation | Verifies the copy before moving source books to Calibre trash |
| `save_book_to_disk_mutation` | book ID, configured destination, options, overwrite flag | Queues Calibre's save-to-disk engine |
| `email_book_mutation` | book ID, configured recipient, existing format | Submits the format through Calibre's configured mail path |
| `cancel_bridge_job_mutation` | `job_id` | Requests cancellation at the next boundary supported by the native job |
| `switch_library_mutation` | alias, active-library guards, exact confirmation | Switches the visible GUI library with repair disabled |

Metadata changes support `title`, `authors`, `series`, `series_index`, `tags`, `identifiers`, `publisher`, `language`, `languages`, `comments`, `rating`, `pubdate`, `timestamp` and custom columns through `custom` or `#column_name`.

Conversion options are limited to `base_font_size`, `font_size_mapping`, `line_height`, the four margin fields, `output_profile`, `input_encoding`, `remove_paragraph_spacing`, `insert_blank_line`, `chapter`, `chapter_mark`, `page_breaks_before` and `pretty_print`.

Save-to-disk options are limited to `template`, `formats`, `save_cover`, `write_opf`, `save_extra_files`, `update_metadata`, `asciiize`, `to_lowercase`, `replace_whitespace` and `single_dir`.

## Controls and limits

| Area | Behaviour |
|---|---|
| Import paths | Book, format and cover files must be below a UI-configured import root |
| Attachment staging | Opt-in root; 1 KiB to 1 GiB files; 60-second to 24-hour expiry; 8 MiB decoded chunks; SHA-256 verification; one-time handles |
| Export paths | Save and conversion exports must stay below a UI-configured export root |
| Library aliases | Must match `^[a-z][a-z0-9_-]{0,63}$`; paths are omitted from MCP results |
| Copy and move | Destinations use configured aliases marked as copy targets; a move trashes sources after verified copying |
| Duplicate policies | `reject`, `skip`, `add`, `merge_missing` or `replace`; merge policies require explicit destination IDs |
| Scheduled news | Only registered `builtin:` or `custom:` URNs; one active job per URN; schedule days use Monday `0` through Sunday `6` |
| E-mail | Recipient and format must already exist in Calibre's mail configuration; automatic conversion is unsupported |
| Content server | Returns a URL only for a running authenticated server with a concrete or explicitly advertised host |
| Short mutations | GUI-thread database calls cannot be interrupted after they start |
| Long operations | Use Calibre jobs; audit records report partial work and delayed cancellation |
| Deletion | Permanent deletion is unsupported; book removal uses Calibre trash |
| Audit | Optional redacted JSONL file plus 10 to 10,000 in-memory records; default 500 |

`update_scheduled_news_schedule_mutation()` reads the existing schedule, calls Calibre's live recipe model, checks the stored result and verifies that recipe identity, title, last-download marker and customisation fields did not change. It restores the previous schedule if the update or verification fails.

`content_server_status_readonly()` withholds the URL when the server is stopped, authentication is disabled or a wildcard bind has no advertised host. Its `reason_code` states the failed condition.

Arbitrary recipients, automatic e-mail conversion, public temporary links and device actions are unsupported. The singular `copy_book` and `move_book` methods return explicit failures.

## Build and test

The test suite uses Python's `unittest` runner:

```sh
PYTHONPATH=.:src python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

Build the plugin ZIP with:

```sh
sh plugins/build-plugin.sh
```

The output is `plugins/calibre-umcp-plugin.zip`. The build copies `umcp.py` and `umcp_shared.py` from `src/calibre_umcp`, keeping one protocol implementation in the repository.

## Install the plugin

Install from a checkout:

```sh
calibre-customize -a plugins/calibre-umcp-plugin.zip
```

linuxserver/calibre runs its profile as `abc`, so container installs normally use:

```sh
s6-setuidgid abc calibre-customize -a plugins/calibre-umcp-plugin.zip
```

Restart or reload Calibre after replacing the ZIP. The plugin starts MCP about one second after initialisation, once the active library is available. The **µMCP Bridge** menu has Status, Configure, Stop and Start commands if automatic startup fails.

## Configure network access

Loopback is the default:

```sh
CALIBRE_UMCP_BRIDGE_HOST=127.0.0.1
CALIBRE_UMCP_PORT=9000
```

A container or LAN bind needs a token:

```sh
CALIBRE_UMCP_BRIDGE_HOST=0.0.0.0
CALIBRE_UMCP_PORT=9000
CALIBRE_UMCP_BRIDGE_TOKEN=<long-random-token>
```

| Variable | Default | Effect |
|---|---|---|
| `CALIBRE_UMCP_BRIDGE_HOST` | `127.0.0.1` | MCP bind address; non-loopback binds require a token |
| `CALIBRE_UMCP_PORT` | `9000` | MCP HTTP port |
| `CALIBRE_UMCP_BRIDGE_TOKEN` | Unset | Authenticates `/mcp`; must match the UI token to expose mutations |
| `CALIBRE_UMCP_CONTENT_SERVER_ADVERTISED_HOST` | Unset | Concrete host returned for an authenticated content server bound to a wildcard address |
| `CALIBRE_UMCP_AUDIT_PATH` | Unset | Redacted JSONL audit file |

Clients send `Authorization: Bearer <token>` to `/mcp` whenever a token is configured, including loopback connections. `/health` stays unauthenticated. Configure import roots, staging, export roots, library aliases, copy destinations, switching and mutation discovery in the plugin UI.

## Compatibility server

The `calibre-umcp` command, equivalent to `python -m calibre_umcp.server`, runs the read-only compatibility server. It uses stdio without transport flags. `--http --port N` publishes Streamable HTTP at `/mcp`.

The supplied `Dockerfile` and `docker-compose.yml` build this compatibility server. They do not install the Interface Action plugin or expose its mutation tools.

Legacy mutator names such as `convert_book`, `copy_book`, `move_book_destructive` and `email_book` fail with directions to the corresponding plugin tool.

## Design notes

| Document | Subject |
|---|---|
| [Architecture](docs/architecture.md) | Process and trust boundaries |
| [Design](docs/design.md) | Implementation choices |
| [Multiple libraries](docs/multiple-libraries.md) | Alias discovery, brokered reads, switching and duplicate checks |
| [Calibre 9.12 API map](docs/calibre-9.12-api-map.md) | Audited read and mutation APIs |
| [Plugin README](plugins/README.md) | ZIP contents and container installation |

The project is released under the [MIT licence](LICENSE).
