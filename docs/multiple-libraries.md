## Multiple-library design

Calibre already knows about multiple libraries and keeps secondary database handles through its GUI library broker. The µMCP bridge should use that machinery for read-only access rather than switching the GUI behind the user's back or opening `metadata.db` directly.

This design keeps one simple rule: reads may name any configured library, but writes apply only to the active GUI library unless a tool already has an explicit, allowlisted destination. Changing the active library is a separate, visible mutation.

## Outcomes

An MCP client can:

* discover libraries without receiving filesystem paths;
* search and inspect a selected library without changing the GUI;
* compare books across libraries and obtain bounded duplicate candidates;
* request an explicit GUI library switch, subject to the same constraints as Calibre's own Choose Library action;
* carry a library reference from a read into a later call without confusing identical book IDs from different libraries; and
* distinguish bad arguments, unsupported libraries, stale active-library state, Calibre read failures and policy denials.

The first implementation does not create, rename, move or remove libraries. It does not mutate an inactive library. Copy and move retain their existing active-source, allowlisted-destination behaviour.

## Library registry

The plugin owns a registry in its UI configuration. Calibre's `library_usage_stats` is useful for discovery, but its IDs are derived from path basenames and can change after a rename or collision. They are not suitable as a public contract.

Each registry entry contains:

```json
{
  "alias": "main",
  "label": "Main Library",
  "path": "/configured/in/the/plugin",
  "read": true,
  "switch": true,
  "copy_destination": true
}
```

`alias` is a stable, case-sensitive MCP identifier matching `^[a-z][a-z0-9_-]{0,63}$`. The UI creates a suggested alias when importing Calibre's known libraries, but the user confirms it. Aliases are unique and are never inferred from a request path.

Paths remain local configuration. MCP responses return `alias`, `label`, availability and policy flags, but never the path. The special selector `current` resolves at call time and every result includes the canonical alias it resolved to.

The registry records Calibre's `library_id` after opening a library. This detects a path being rebound to a different database; it does not replace the configured alias. A mismatch makes the entry unavailable until the user confirms the rebind in the plugin UI.

## Library and book references

Bare book IDs become ambiguous as soon as two libraries are visible. New and extended tools therefore use:

```json
{
  "library": "main",
  "book_id": 42
}
```

Responses include the same pair. Existing active-library calls remain compatible: omitted `library` means `current`, and an existing bare `book_id` is resolved against `current`.

Every library resolution returns an `active_generation`. The bridge increments this integer after each successful GUI switch. Mutation requests that depend on a previous read include `expected_active_library` and `expected_active_generation`; a mismatch fails before any work is queued.

## Read access

All library access remains inside the Calibre process and enters through the existing serialized bridge dispatcher.

For the active library, the bridge uses `gui.current_db`. For a configured inactive library, it asks `gui.library_broker` for the secondary handle that Calibre itself creates with `LibraryDatabase(path, is_second_db=True)`. It does not construct SQLite connections or read `metadata.db` directly.

Version 1 serializes active and inactive reads through the bridge lock. This is deliberately conservative. Per-library read concurrency can be considered after runtime tests establish that Calibre's broker, caches and custom metadata functions behave correctly under parallel access.

Each resolved database is checked before use:

* the alias exists and has read permission;
* the configured path still identifies a Calibre library;
* the broker returns a database handle;
* the observed `library_id` matches the registry; and
* the requested book exists in that database.

Secondary handles are owned and closed by Calibre's broker. The plugin does not cache another layer of database objects.

## MCP surface

### Discovery

`list_libraries_readonly()` returns:

```json
{
  "active_library": "incoming",
  "active_generation": 7,
  "libraries": [
    {
      "alias": "incoming",
      "label": "Incoming",
      "active": true,
      "available": true,
      "readable": true,
      "switchable": true,
      "copy_destination": false
    },
    {
      "alias": "main",
      "label": "Main Library",
      "active": false,
      "available": true,
      "readable": true,
      "switchable": true,
      "copy_destination": true
    }
  ]
}
```

`capabilities_readonly()` reports `cross_library_reads: true`, `inactive_library_mutations: false`, the hard result limits and the supported duplicate match methods.

### Search and metadata

The existing tools gain an optional `library` property in their MCP schemas:

```text
search_books_readonly(query="", limit=20, library="current", cursor=null)
get_book_metadata_readonly(book_id, library="current")
find_duplicates_readonly(limit=100, target_limit=100, library="current", cursor=null)
```

List-shaped results become object-shaped and carry scope and pagination:

```json
{
  "library": "main",
  "items": [],
  "limit": 20,
  "truncated": false,
  "next_cursor": null
}
```

Sorting is stable by Calibre book ID unless a tool documents another order. Cursors are opaque, scoped to the tool arguments and library identity, and rejected if reused with different arguments.

### Cross-library duplicate candidates

A dedicated tool avoids overloading active-library duplicate grouping:

```text
find_cross_library_duplicates_readonly(
  source_library,
  target_libraries,
  source_query="",
  limit=100,
  candidate_limit_per_book=20,
  match=["identifiers", "title_authors"]
)
```

`target_libraries` is required and bounded to 16 aliases. The caller can obtain all readable aliases from discovery, but there is no magic filesystem-wide scan. `limit` bounds source books, not matches, and has a hard maximum of 500. The total response has a hard maximum of 2,000 candidates.

The bridge loads only the fields needed for comparison. Identifier matches are exact after normalising identifier type and value. Title and author matching uses Calibre's normalised forms where available, with a documented local fallback. It does not use fuzzy title distance in version 1.

A candidate result contains safe metadata and reasons:

```json
{
  "source_library": "incoming",
  "target_libraries": ["main"],
  "scanned_source_count": 27,
  "truncated": false,
  "matches": [
    {
      "source": {
        "library": "incoming",
        "book_id": 1,
        "title": "Example",
        "authors": ["A. Writer"],
        "formats": ["EPUB"],
        "identifiers": {"isbn": "9780000000000"}
      },
      "candidates": [
        {
          "library": "main",
          "book_id": 42,
          "title": "Example",
          "authors": ["A. Writer"],
          "formats": ["EPUB", "PDF"],
          "identifiers": {"isbn": "9780000000000"},
          "reasons": ["identifier:isbn", "title_authors"],
          "confidence": "high"
        }
      ]
    }
  ]
}
```

Confidence is deterministic: an exact strong identifier match is `high`; title plus all normalised authors is `medium`; conflicting strong identifiers prevent a title/author-only candidate from being labelled `high`. Results are advisory and never feed directly into merge without a fresh guarded validation.

## Switching the GUI library

Switching is a mutation because it changes visible GUI state, search restrictions, views, device context and the default target of existing tools. It is never an implicit side effect of a read.

The API is one explicit call:

```text
switch_library_mutation(
  library,
  expected_active_library,
  expected_active_generation,
  confirmation
)
```

The confirmation string includes the target label and is checked exactly, following the existing destructive-operation confirmation style. The plugin UI must enable switching separately from general mutations.

The bridge executes the switch on the GUI thread. It uses Calibre's Choose Library action guard where available and then `gui.library_moved(path, allow_rebuild=False)`. `allow_rebuild=False` prevents an MCP request from opening a repair prompt. The operation fails closed when:

* `CALIBRE_OVERRIDE_DATABASE_PATH` disables switching;
* Calibre's job manager has running jobs;
* bridge jobs are queued or running;
* Calibre has pending proceed questions or another modal operation;
* the expected active alias or generation is stale;
* the target is missing, unregistered or not switchable; or
* Calibre cannot open the target without repair.

Success is reported only after `gui.current_db.library_id` matches the registered target. The bridge then increments `active_generation`, records a redacted audit entry and emits the normal tool-list/capability state notifications if required.

There is no automatic switch-back. A switch is a user-visible state transition, and pretending otherwise would make failures and concurrent UI use harder to reason about.

## Mutation targeting

Existing mutation tools continue to operate on the active database. They gain optional guards:

```json
{
  "expected_active_library": "incoming",
  "expected_active_generation": 7
}
```

Tools reject a `library` or source reference that resolves to an inactive library with `LIBRARY_SWITCH_REQUIRED`. This includes metadata, format, cover, delete, conversion, save and e-mail mutations.

Copy and move remain the exception already present in the bridge: the source must be active, while the destination must be a registry entry with `copy_destination: true`. Their path arguments are replaced at the MCP boundary by `destination_library`; raw paths remain an internal compatibility detail until removed.

Duplicate merge is active-library-only. Cross-library candidates can lead to copy, move or an explicit switch followed by a new active-library duplicate check, but never to a cross-database merge operation.

## Errors

Bridge errors retain a machine-readable code through the MCP layer:

```json
{
  "code": "LIBRARY_ALIAS_UNKNOWN",
  "message": "The requested library alias is not configured",
  "details": {"library": "archive"}
}
```

The public error set adds:

* `LIBRARY_ALIAS_UNKNOWN`
* `LIBRARY_UNAVAILABLE`
* `LIBRARY_IDENTITY_MISMATCH`
* `LIBRARY_READ_DENIED`
* `LIBRARY_SWITCH_DENIED`
* `LIBRARY_SWITCH_BLOCKED`
* `LIBRARY_SWITCH_REQUIRED`
* `ACTIVE_LIBRARY_MISMATCH`
* `ACTIVE_LIBRARY_GENERATION_MISMATCH`
* `BOOK_NOT_FOUND`
* `CURSOR_INVALID`
* `RESULT_LIMIT_EXCEEDED`
* `CALIBRE_READ_FAILED`

Messages never contain configured paths, database filenames or exception text that might expose local details. Full tracebacks remain in local logs with the existing redaction rules.

## Fixing the current duplicate failure

The active-library bug is independent of this design. `_find_duplicates()` currently calls `all_book_ids()` on the legacy database wrapper, while the Calibre 9.12 read contract should consistently use the resolved database's `new_api`. The fix belongs in the first implementation slice, with a runtime regression test and a structured bridge error if ID enumeration fails.

The current key also requires identifiers, title and authors to match simultaneously. That finds exact metadata clones rather than probable duplicates. The replacement groups candidates by shared identifiers or normalised title/authors and reports reasons; it does not silently merge the two meanings.

## Implementation slices

1. Add structured bridge errors, object-shaped bounded results and the active-library duplicate regression fix. Existing calls remain source compatible.
2. Add the UI-managed library registry, redacted discovery and alias/book reference resolution.
3. Add broker-backed inactive-library search and metadata reads, initially serialized.
4. Add deterministic cross-library duplicate candidate comparison.
5. Add guarded GUI switching and active-generation checks.
6. Migrate copy/move destination arguments from paths to aliases, retaining a deprecated internal compatibility path for one release.

Mutation discovery remains gated to exact Calibre 9.12.0 until every switching and targeting contract test passes. Read-only multi-library tools also report their tested Calibre version rather than implying compatibility with an untested runtime.

## Verification

Pure tests cover alias validation, path redaction, identity mismatches, book-reference scope, cursor binding, limits, duplicate reasons, deterministic confidence and active-generation guards.

Calibre 9.12 source-contract tests pin the broker methods, secondary `LibraryDatabase(..., is_second_db=True)` behaviour, `gui.library_moved()` entry point and Choose Library guard conditions.

Runtime integration uses three temporary libraries with overlapping book IDs and controlled metadata. It verifies inactive search without a GUI switch, cross-library identifier and title/author matches, same-basename alias handling, stale generation rejection, blocked switching while jobs run, successful switching, broker cleanup and absence of paths in every default response and error.

Production verification is read-only until the new plugin has passed those tests. The only safe live mutation check is an operator-observed switch between two disposable or explicitly approved libraries, followed by a switch back as a separate call.

## Definition of done

Issue #1 can close when an MCP client can discover configured aliases, search inactive libraries without changing the GUI, compare bounded source books against selected libraries, and understand every failure from a stable error code. Explicit switching must honour Calibre's own blockers and stale-state guards, while all other mutations continue to fail unless their target is the active library.
