# python-umcp-calibre

Calibre is quite particular about who touches its active library--and rightly so, since the GUI keeps database and filesystem state in memory. I wrote `python-umcp-calibre` to expose that live library to MCP clients without putting a second process in charge of `metadata.db`.

The server runs inside Calibre as an Interface Action plugin and uses [`umcp`][umcp] for Streamable HTTP. There is no sidecar: an MCP client connects directly to `/mcp`, and every library call reaches `gui.current_db` through a serialised worker.

## What It Does

Clients can inspect the active library, search for books, fetch metadata, find probable duplicates, check an existing authenticated content server and inspect bridge audit records. `capabilities_readonly` gives agents a compact starting point, while `describe_tool_readonly` expands one tool at a time.

Mutations stay hidden until the Calibre UI has both saved a token and enabled them. Once that gate is open, the plugin can update metadata and formats, replace covers, import books, convert formats, export through Calibre's save-to-disk engine, copy or move books between allowlisted libraries, submit an existing format to a configured e-mail recipient, merge duplicates conservatively and move confirmed deletions to Calibre trash.

The slower paths appear in Calibre's own Jobs UI. Conversion uses a worker-process job; import, export, copy/move and e-mail use `ThreadedJob`. Move is deliberately fussy: preview -> copy -> metadata and format-hash verification -> source trash. Any uncertainty leaves the source in place.

A few boundaries are intentional. Permanent deletion, arbitrary e-mail recipients, implicit e-mail conversion, unauthenticated or temporary public links, and device actions are not exposed. Calibre 9.11 has no safe scoped temporary-link API, and device operations depend on live GUI/device state that the bridge cannot treat as a stable library mutation.

The plugin also exposes `GET /health`, plus Start, Status and Stop actions in the Calibre GUI. Non-loopback binds require a bearer token.

This release is source-contract and runtime tested against exactly Calibre 9.11.0 (`v9.11.0`, commit `b23dfb5d`). The plugin declares 9.11.0 as its minimum; later Calibre releases may still load the read-only surface, but mutation discovery fails closed until that exact runtime has been audited again.

## Building It

```sh
PYTHONPATH=.:src python3 -W error::ResourceWarning -m unittest discover -s tests -v
sh plugins/build-plugin.sh
```

The build produces `plugins/calibre-umcp-plugin.zip`. It copies `umcp.py` and `umcp_shared.py` from `src/calibre_umcp` into the archive, so the plugin uses the same µMCP runtime as the rest of the repository rather than carrying a second protocol implementation.

## Installing It

Install the ZIP with Calibre's plugin utility:

```sh
calibre-customize -a plugins/calibre-umcp-plugin.zip
```

linuxserver/calibre profiles are normally owned by `abc`, so install as that user inside the container:

```sh
s6-setuidgid abc calibre-customize -a plugins/calibre-umcp-plugin.zip
```

Restart or reload Calibre after replacing the plugin. It starts MCP automatically once the active library is available; the `µMCP Bridge` menu still provides Status, Stop and Start controls.

## Connecting

The default bind is local to the Calibre process:

```sh
CALIBRE_UMCP_BRIDGE_HOST=127.0.0.1
CALIBRE_UMCP_PORT=9000
```

To reach it across a container or LAN network, bind explicitly and set a long random token:

```sh
CALIBRE_UMCP_BRIDGE_HOST=0.0.0.0
CALIBRE_UMCP_PORT=9000
CALIBRE_UMCP_BRIDGE_TOKEN=<long-random-token>
```

The MCP URL is `http://<calibre-host>:9000/mcp`. Authenticated clients send `Authorization: Bearer <token>`.

The [architecture notes](docs/architecture.md) explain the process boundary, the [design notes](docs/design.md) cover the implementation choices, and the [plugin README](plugins/README.md) has the container-oriented installation details.

[umcp]: https://github.com/rcarmo/umcp
