# Calibre 9.11 Mutation API Map

This map is pinned to Calibre 9.11.0 (`v9.11.0`, upstream commit `b23dfb5d`). The plugin must fail closed when a later release no longer matches the source-contract tests described below. Those tests pin both callable locations and signature prefixes for the audited entry points.

## Thread Boundary

MCP runs on µMCP's HTTP threads. Calibre database, Qt and Interface Action calls belong on the GUI thread.

Calibre provides `calibre.gui2.Dispatcher`, a `QObject` whose queued signal invokes a callable in the thread where the dispatcher was created. The bridge must create its dispatcher during plugin `genesis()` and use it for every short database operation and every call that enqueues a Calibre job. The existing bridge worker may serialise incoming calls, but it must not touch `gui.current_db` itself.

`calibre.gui2.jobs.JobManager` exposes:

```python
run_job(done, name, args=[], kwargs={}, description='', core_usage=1)
run_threaded_job(job)
```

`run_job()` creates a worker-process `ParallelJob`. Completion handlers are normally wrapped in `Dispatcher`. `ThreadedJob` is defined in `calibre.gui2.threaded_jobs`:

```python
ThreadedJob(type_, description, func, args, kwargs, callback,
            max_concurrent_count=1, killable=True, log=None)
```

Its function receives `abort`, `log` and `notifications`; progress is `(fraction, message)`. A running cancellation sets `abort`, while queued cancellation marks the job killed. Device jobs cannot be cancelled through `JobManager`.

Device work has a second queue in `calibre.gui2.device.DeviceManager`. `create_job_step(func, done, description, to_job, args=[], kwargs={})` appends follow-on work to `job_steps` only when `to_job == self.current_job` and the callback is `None` or a `FunctionDispatcher`; otherwise it queues a fresh `DeviceJob`. `prepare_addable_books()`, `sync_booklists()`, `upload_books()` and `save_books()` all expose `add_as_step_to_job` for this deferral. The bridge should not interleave unrelated library mutations with an active device pipeline; it should either defer onto the same device job explicitly or fail closed while the device queue is busy.

The bridge needs its own IDs and state because small GUI-thread mutations are not Calibre jobs and Calibre's job list is not a durable audit store.

## Metadata, Formats, Covers And Removal

The supported mutation surface is `gui.current_db.new_api`, implemented by `calibre.db.cache.Cache`:

```python
set_field(name, book_id_to_val_map, allow_case_change=True,
          do_path_update=True)
set_metadata(book_id, mi, ignore_errors=False, force_changes=False,
             set_title=True, set_authors=True, allow_case_change=False)
add_format(book_id, fmt, stream_or_path, replace=True,
           run_hooks=True, dbapi=None)
remove_formats(formats_map, db_only=False)
create_book_entry(mi, cover=None, add_duplicates=True, force_id=None,
                  apply_import_tags=True, preserve_uuid=False)
add_books(books, add_duplicates=True, apply_import_tags=True,
          preserve_uuid=False, run_hooks=True, dbapi=None)
remove_books(book_ids, permanent=False)
set_cover(book_id_data_map)
merge_book_metadata(dest_id, src_ids, replace_cover=False,
                    save_alternate_cover=False)
```

`create_book_entry()` is the first add/import primitive. With `apply_import_tags=True` it appends the configured new-book tags and default values for custom columns before insertion. Duplicate rejection is only the cache-level `_has_book()` heuristic; richer automerge behaviour is not part of `Cache.add_books()` itself.

`add_books()` loops book by book: it calls `create_book_entry()`, then `add_format()` for each incoming format, then `run_plugins_on_postadd()` for that one book. It returns `(ids, duplicates)`, where `duplicates` contains the untouched `(mi, format_map)` pairs that were rejected by the duplicate heuristic. There is no batch transaction across the whole request. Partial success is therefore normal and must be audited as such.

`add_format()` runs import plugins before taking the write lock, may change the effective format after `check_ebook_format()`, updates size metadata, queues a pages scan and emits `EventType.format_added`. The backing file semantics are important:

- when the bridge supplies a filesystem path, `backend.add_format()` finishes with `os.replace(stream, dest)`, so the final replacement is an atomic rename on the same filesystem;
- when the bridge supplies an already-open stream, Calibre writes directly to `dest` with `open(dest, 'wb')`, so there is no preserved old-format rollback copy;
- if the calculated filename changes, Calibre first renames the old format path into the new target name so the previous file is not orphaned, but this is still not a multi-step rollback mechanism.

The bridge should therefore stage uploads outside the library, prefer path-based replacement when it needs the stronger rename behaviour, and treat each record as independently recoverable. If a failure happens after `create_book_entry()` but before all formats are attached, the bridge should use compensating cleanup rather than assume Calibre rolled the whole add back.

`remove_books(..., permanent=False)` uses Calibre's trash and emits `EventType.books_removed`. Trash listing, restore, permanent deletion and expiry also exist in `Cache`, but permanent cleanup should remain a separate operation.

`add_books()` and `remove_books()` do not themselves confine import or export paths. Import paths must therefore be confined by bridge policy to configured roots or bridge-owned uploads before they are handed to Calibre.

Extra files can be enumerated and copied through `list_extra_files()` and `copy_extra_file_to()`. Their handling should follow Calibre's copy-to-library backend rather than bridge code.

## Conversion

The GUI action is `calibre.gui2.actions.convert.ConvertAction`. Its `queue_convert_jobs()` loops over jobs produced by the conversion dialogs and calls:

```python
gui.job_manager.run_job(
    Dispatcher(converted_func),
    func,
    args=args,
    description=description,
    core_usage=core_usage,
)
```

The worker names and arguments come from `calibre.gui2.convert.single.convert_single_ebook` and the corresponding bulk helper. `ConvertAction.book_converted()` receives the `ParallelJob`, obtains the output path from its temporary files, attaches the format to the current database and refreshes the GUI.

The plugin uses `calibre.gui2.tools.convert_single_ebook()` as the small non-dialog preparation adapter, then submits the returned worker function and arguments through `gui.job_manager.run_job()`. It accepts only a bounded scalar option set. Output remains temporary until the successful GUI-thread callback; it is then attached once or atomically exported. An existing destination format is replaced only when the request explicitly opts in.

## Copy And Move Between Libraries

The reusable backend is `calibre.db.copy_to_library.copy_one_book()`:

```python
copy_one_book(book_id, src_db, dest_db,
              duplicate_action='add', automerge_action='overwrite',
              preserve_date=True, identical_books_data=None,
              preserve_uuid=False)
```

It runs under `src_db.new_api.safe_read_lock` and `dest_db.new_api.write_lock`, copies metadata, formats, covers, extra files, conversion options and annotations, and returns a small action summary. `postprocess_copy()` also replays author sort values for newly created destination authors and updates the destination identical-books cache when duplicate handling is active.

Duplicate handling is more specific than the GUI wording suggests. `duplicate_action='add_formats_to_existing'` calls `automerge_book()`, which tries to add every incoming format to every identical destination record with `replace=(automerge_action == 'overwrite')`. Extra files are copied into an existing destination record only if at least one format was added there. With `automerge_action='new record'`, Calibre creates a fresh destination record only if at least one incoming format collides with a format already seen in the identical destination set; if all incoming formats were merely missing, Calibre merges them into the existing records and creates no new record.

`calibre.gui2.actions.copy_to_library.Worker` is a plain `threading.Thread` with a progress dialog, not a Calibre job. It gets an independent destination database from `gui.library_broker.get_library(path)`, calls `copy_one_book()` for each ID and prunes broker databases afterwards. Cancellation is only checked between books, so copy/move is per-book atomic at best and routinely leaves a partially completed batch. The bridge should repackage that loop as `ThreadedJob(type_='umcp-copy-library', max_concurrent_count=1)` so it appears in Calibre's Jobs UI and honours `abort` between books.

Move semantics in the stock GUI are stronger than the bridge should adopt. After a successful batch, `do_copy()` computes `done_ids = processed - duplicate_ids` and then permanently deletes those source IDs with `v.model().delete_books_by_id(done_ids, permanent=True)`. This includes books that were auto-merged into existing destination records, not only newly created destination records. There is no upstream destination hash verification, no mandatory re-open of the copied record, and no source-trash default. The bridge should default to Calibre trash, verify destination records plus expected format hashes before any source deletion, and require an explicit stronger policy for permanent removal.

Custom-column compatibility is checked by the GUI action before launching its worker. `_column_is_compatible()` requires matching datatypes and, for text columns, matching `is_multiple`. `ask_about_cc_mismatch()` can offer to create missing destination columns, and the result is cached once per source-library/destination-library ID pair for the session. The bridge must perform the same comparison and reject incompatible libraries unless an explicit, separately tested policy creates missing columns.

## Duplicate Merging

`calibre.gui2.actions.edit_metadata.EditMetadataAction` uses database primitives rather than a background job. In the default merge flows it first copies formats to the explicit destination record with `replace=False`, then merges metadata with:

```python
gui.current_db.new_api.merge_book_metadata(
    destination_id, source_ids, replace_cover,
    save_alternate_cover=save_alternate_cover,
)
```

Format collision rules are therefore strict and easy to miss: destination formats always win, because `add_formats()` calls legacy `db.add_format(..., replace=False)`. If the source records are then deleted, any colliding source formats are discarded permanently. Extra files are handled separately by `merge_extra_files(..., replace=False)`, which auto-renames conflicting extra files instead of replacing them.

`merge_book_metadata()` is conservative. It appends differing comments, fills title/authors/publisher/rating/series/pubdate only when the destination lacks a value, extends tags and multi-text custom columns, preserves destination-or-earlier identifier values on key collision, and only replaces the destination cover when the destination had none or `replace_cover=True` was requested. When `save_alternate_cover=True`, alternate covers are stored as numbered files under the book's `data/` directory.

There is no source-level coordination with Calibre's active jobs list here. `merge_books()` runs synchronously on the GUI thread, does not create a `JobManager` entry and does not check for in-flight conversion, e-mail, save-to-disk, copy or device jobs touching the same records. The current bridge implementation therefore refuses merge requests when one of its own tracked bridge jobs still references the survivor or source IDs, rather than claiming wider visibility it does not yet have.

The first bridge policy should name the survivor explicitly, add only missing formats, merge metadata conservatively and leave source records in place unless deletion is separately confirmed.

## Save To Disk

`calibre.gui2.actions.save_to_disk.SaveToDiskAction` delegates library exports to `calibre.gui2.save.Saver` by constructing `Saver(book_ids, gui.current_db, opts, path, parent=gui, pool=gui.spare_pool())` on the GUI thread.

`Saver` has a staged lifecycle driven by its queued `do_one_signal` rather than a single callback-bearing Calibre job:

1. `__init__()` captures database preferences, creates `ProgressDialog`, creates `PersistentTemporaryDirectory('_save_to_disk')`, sanitises the destination path and starts collection.
2. `collect_data()` gathers metadata, final path components and available formats on the GUI side.
3. `collection_finished()` de-duplicates path components with `ensure_unique_components()`, optionally starts `Pool(name='SaveToDisk')`, and seeds worker common data with plugboards, template functions and the library ID.
4. `write_book()` writes covers, OPF sidecars, requested formats and extra files, and for metadata updates stages temporary OPF/JPG artefacts under `tdir` before queueing `update_serialized_metadata` work to the pool.
5. `consume_results()`, `do_one_update()` and `updating_metadata_finished()` drain worker results, collate per-book errors, show the final report and tear everything down via `break_cycles()`.

`Saver` is tied to a progress dialog and a queued GUI lifecycle, so the bridge uses the lower-level `calibre.library.save_to_disk.save_to_disk()` entry point inside a native `ThreadedJob`. That function retains Calibre's template evaluation, sanitisation, plugboards, cover/OPF handling and format metadata updates without constructing a second GUI dialog. In 9.11.0 it mixes legacy `get_metadata(index_is_id=True)` calls with Cache-only `pref()`, `copy_format_to()` and extra-file methods after dereferencing `db.new_api`; the bridge therefore uses a narrow local adapter around an independent `calibre.db.legacy.LibraryDatabase` handle. The worker writes into a bridge staging directory under an allowlisted export root, checks every collision, then publishes with `os.replace()`. Existing files are moved into a temporary backup first and restored if publication fails.

## E-mail

`calibre.gui2.email.send_mails()` creates one `ThreadedJob(type_='email', ...)` per attachment and submits it with `gui.job_manager.run_threaded_job(job)`. The job function is `gui_sendmail`, an instance of `Sendmail`, whose call signature is:

```python
__call__(attachment, aname, to, subject, text,
         log=None, abort=None, notifications=None)
```

`Sendmail` keeps Calibre's operational policy in code: `TIMEOUT = 25 * 60`, `MAX_RETRIES = 1` after the first failure, and an SMTP rate limit derived from public-relay tweaks. The actual SMTP work happens in a helper thread so the `ThreadedJob` can poll for `abort` and timeout.

Configured recipients live in Calibre's e-mail preferences as `opts.accounts`, with related `subjects`, `aliases` and `tags`. `EmailMixin.send_by_mail()` first sends any already-available preferred format, then optionally asks the Convert Books action to auto-convert the remaining books through `auto_convert_mail()` or `auto_convert_multiple_mail()`. `email_news()` further filters configured accounts by the auto-send flag and optional tag rules.

SMTP credentials are only read inside `Sendmail.sendmail()` from `email_config().parse()`. The relay password is decoded with `from_hex_unicode(opts.relay_password)` immediately before calling `calibre.utils.smtp.sendmail()`. The job callback and log surface success or failure, but not the configured credentials. For Kindle and PocketBook destinations, Calibre may also randomise the subject, body and sometimes the attachment name to work around remote service quirks.

The plugin allows only recipients already configured in Calibre and requires an existing format enabled for that account. Its native `ThreadedJob(type_='email')` prepares the attachment through an independent library reader, then invokes Calibre's shared `gui_sendmail` callable, retaining Calibre's timeout, retry, rate-limit and credential handling. The bridge never returns SMTP settings. Successful completion means SMTP submission succeeded; the result explicitly leaves `delivery_confirmed` false.

## Content Server

`calibre.gui2.ui.Main.start_content_server(check_started=True)` constructs and starts `calibre.srv.embedded.Server` from the saved content-server configuration. The Device action toggles it through this method and `gui.content_server.stop()`.

Authentication is configured elsewhere, not by these GUI entry points. The server preferences expose an `auth` flag plus per-user accounts and library restrictions. In lower-level server code, `calibre.srv.auth.AuthController` implements Basic/Digest auth and one scoped-expiry mechanism for authenticated downloads: an Android-workaround cookie whose path is pinned to the requested endpoint and whose default lifetime is `MAX_AGE_SECONDS = 3600`. This is the only scoped-expiry facility found in the 9.11 GUI/server path audit, and it is an authenticated session cookie rather than a shareable temporary public link.

There is still no scoped temporary-link facility in the GUI entry points for arbitrary files or books. The bridge may report an existing authenticated content-server URL only when the server is already running on a concrete non-wildcard host, and it must not invent an unauthenticated filesystem URL. Temporary public links remain unsupported unless a lower-level Calibre API with explicit link expiry and scope is identified.

## Configuration

Calibre plugins commonly use `calibre.utils.config.JSONConfig`. Calibre 9.11 has no general OS credential-store abstraction in its source tree, and its own e-mail settings use Calibre configuration. The bridge token can therefore be stored in a plugin JSON file with Calibre-profile permissions and edited through a masked UI field; documentation must state that it is not hardware-backed secret storage.

Environment variables may override host, port and token for container deployment. They can require authentication, but mutation discovery still requires the token to have been explicitly saved in the plugin UI plus the UI mutation switch. An environment-only token cannot enable mutations on its own, and a different override token disables mutation discovery rather than widening it.

## Cancellation Boundaries

Queued and running native jobs are cancelled through Calibre's `JobManager._kill_job()` path without changing Calibre job objects. A queued job can become `cancelled` immediately. A running `ThreadedJob` is not reported as cancelled merely because its `killed` flag was set: the bridge waits for a worker result or exception at the next safe boundary.

Import cancellation prevents the GUI-thread `add_books()` callback and removes any prepared temporary file. Copy checks `abort` between books and reports destination changes as `PARTIAL_COPY`; a move never reaches source trash after cancellation. Save-to-disk checks before collection and before staged publication. E-mail checks before preparation, before SMTP submission and after Calibre's abort-aware `gui_sendmail` returns. Conversion cancellation discards temporary output. Short GUI-thread metadata, format, cover, merge and trash mutations are deliberately not interruptible once their database call starts.

## Release Surface And Compatibility Decision

The mutation catalogue shipped by this release comprises metadata and format updates, cover replace/remove, confined add-book import, native conversion, confined save-to-disk, configured-recipient e-mail, verified copy/move, conservative duplicate merge, Calibre-trash deletion and native-job cancellation. Singular `copy_book` and `move_book` remain rejected legacy bridge methods; clients use the plural verified operations.

Permanent deletion, arbitrary recipients, implicit e-mail conversion, temporary public links and device/connected-folder actions are intentionally unsupported. The authenticated content-server status call exposes only a concrete running authenticated base URL and never a raw library path.

The source-tree compatibility server under `src/calibre_umcp/server.py` stays read-only by default and keeps its legacy mutator names as explicit failures. When it is pointed at `CALIBRE_UMCP_BRIDGE_URL`, that optional proxy path still speaks the older `/rpc` JSON-RPC helper rather than the released plugin `/mcp` transport.

`CalibreUmcpPlugin.minimum_calibre_version` is `(9, 11, 0)` because no older release has passed this source/runtime contract. Since Calibre interprets that field as a lower bound, later releases may load the read-only plugin surface, but mutation discovery and execution both require exactly `(9, 11, 0)` and fail closed otherwise.

## Monkey-Patch Decision

No monkey patch is used. Database, copy, conversion preparation, save-to-disk and e-mail operations all expose bounded callable entry points in 9.11.0. Any future patch would have to be local to one call, version-checked, restored in `finally`, covered by source-contract tests and no less safe than the corresponding GUI action. Global database or job patches remain prohibited.
