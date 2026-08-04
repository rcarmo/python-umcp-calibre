# calibre-umcp

`calibre-umcp` is a Calibre automation MCP server built on Rui Carmo's [`umcp`](https://github.com/rcarmo/umcp).

It is designed to run either:

- through a small Calibre plugin shim that launches the same server from inside Calibre when plugin deployment is viable.

## Initial goals

- Manage one or more Calibre libraries.
- Detect duplicates by title/author/identifier/file hash heuristics.
- Convert books through `ebook-convert`.
- Email books through Calibre's `calibre-smtp` or configured SMTP.
- Copy/move books between libraries through `calibredb`.
- Expose all operations as MCP tools using `umcp`.

## Status

Early scaffold. The first implementation wraps Calibre command-line tools (`calibredb`, `ebook-convert`, `calibre-smtp`) so it works in containers and does not require embedding into Calibre's GUI process.

See [`docs/design.md`](docs/design.md) for the feasibility assessment and implementation plan.
