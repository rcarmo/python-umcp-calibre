# Project instructions

## Cache and temporary paths

The canonical project name is `calibre-umcp`. All reproducible caches, generated build output, and disposable temporary files must use one resolved project root with this hierarchy:

- `cache/<tool>/` for rebuildable caches, including Python bytecode and pip downloads;
- `build/` for generated plugin ZIPs and other build output;
- `tests/<run-id>/` for isolated test scratch;
- `logs/` for disposable logs;
- `runs/<purpose>/<run-id>/` for other isolated task scratch.

Resolve the root once, before exporting child temporary variables, and snapshot the inherited `TMPDIR` first. `PROJECT_TMP_BASE` selects `<base>/calibre-umcp`; `PROJECT_TMP_ROOT` remains a compatible explicit project-root override. Both must be usable absolute, non-symlink paths, and if both are supplied they must agree; invalid, unusable, empty, or conflicting overrides fail. Without an override, CI tries `${RUNNER_TEMP}/calibre-umcp`, then the original `${TMPDIR}/calibre-umcp`, then the platform temporary directory. Local use tries writable `/workspace/tmp/calibre-umcp`, then platform temp. The vendored resolver keeps CI independent of `/workspace` and `/workspace/Makefile`.

Use the project `Makefile`, which exports the resolved `PROJECT_TMP_ROOT`, `TMPDIR`, `TMP`, `TEMP`, `PYTHONPYCACHEPREFIX`, and `PIP_CACHE_DIR`. Do not use a bare temporary directory, home-directory cache, ad-hoc root, or source directory for generated output. Tests must preserve their filesystem ownership, symlink, and isolation checks. `make test` captures CPU and allocation profiles and retains them under `evidence/test-profiles/<run-id>/`; inspect both top reports after every run.

`make clean` may remove only `cache/`, `build/`, `tests/`, `logs/`, and `runs/` beneath the validated resolved root. It must not remove retained profiles in `evidence/test-profiles/`, logs, receipts, datasets, backups, source, installed toolchains, active-job paths, or another project's files.

Container deployment follows the same resolver contract. If `/workspace/tmp` is not mounted, allow the fallback order or explicitly set a validated `PROJECT_TMP_ROOT` ending in `/calibre-umcp`; `WORK` and `OUT` remain confined to its `runs/` and `build/` subtrees.
