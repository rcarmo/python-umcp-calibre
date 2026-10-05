# Project instructions

## Cache and temporary paths

The canonical project name is `calibre-umcp`. All reproducible caches, generated build output, and disposable temporary files must remain under `/workspace/tmp/calibre-umcp/`:

- `cache/<tool>/` for rebuildable caches, including Python bytecode and pip downloads;
- `build/` for generated plugin ZIPs and other build output;
- `runs/<purpose>/<run-id>/` for isolated test and task scratch.

Use the project `Makefile`, which exports `TMPDIR`, `TMP`, `TEMP`, `PYTHONPYCACHEPREFIX`, and `PIP_CACHE_DIR`. Do not use bare `/tmp`, home-directory caches, ad-hoc workspace roots, or source directories for generated output. Tests must keep their existing filesystem and symlink isolation checks. `make test` captures CPU and allocation profiles and retains them under `evidence/test-profiles/<run-id>/`; inspect both top reports after every run.

`make clean` may remove only the three disposable directories beneath `/workspace/tmp/calibre-umcp`; it must not remove retained profiles in `evidence/test-profiles/`, logs, receipts, datasets, backups, source, installed toolchains, or another project's files. Do not clean paths used by active jobs.

Container deployment is an external runtime mapping: set `PROJECT_TMP_ROOT` to a container-owned persistent/scratch location with the same `cache/`, `build/`, and `runs/` structure if `/workspace/tmp/calibre-umcp` is not mounted. Never silently fall back to `/tmp`.
