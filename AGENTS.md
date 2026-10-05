<!-- RUI-PROFILE-LIFECYCLE-20261005 -->
## Current profiling and cleanup rule — supersedes older text below

Rui's explicit rule: **profile and tune during pre-release tests; remove profiling data immediately after analysis/use.** Ordinary development tests do not require profiling on every run. Targeted diagnostic profiling is optional when useful and follows the same disposal rule.

- During pre-release verification, capture CPU and heap/allocation behaviour, analyse hotspots and tune avoidable allocations/repeated work. Compare equivalent workloads without weakening correctness/security/numerical contracts. For Go, inspect CPU, alloc_space and alloc_objects; coverage alone is not profiling.
- Keep captures and matching artifacts only while the current analysis needs them. Once used, immediately delete raw profiles, traces, matching test binaries, temporary fixtures and disposable logs, including failed/probe artifacts after diagnosis. Retain only concise conclusions and important measurements/limitations. Do not keep indefinite raw archives or copy them into exports/reports/evidence to evade cleanup.
- Remove all completed disposable cache/build/test/run data promptly. Never delete files still in use: finish or safely pause the owning job and clean at a safe boundary. Preserve source, installed toolchains, durable datasets/checkpoints and intentional release assets. Minimise disk usage; no random exports or redundant snapshots.
- This rule overrides **every conflicting older paragraph in this file and linked local guidance**, including “profile every test”, “unprofiled tests prohibited”, “retain all raw evidence”, “never delete profiles” and cleanup exclusions based only on an evidence/profiles directory name. Update helper/CI cleanup behaviour accordingly; do not weaken pre-release analysis.
- Existing no-agent-contact and execution-pause rules remain unchanged. This policy grants no unsolicited coordination or automatic job restart.
<!-- /RUI-PROFILE-LIFECYCLE-20261005 -->

# Project instructions

## Cache and temporary paths

The canonical project name is `calibre-umcp`. All reproducible caches, generated build output, and disposable temporary files must use one resolved project root with this hierarchy:

- `cache/<tool>/` for rebuildable caches, including Python bytecode and pip downloads;
- `build/` for generated plugin ZIPs and other build output;
- `tests/<run-id>/` for isolated test scratch;
- `logs/` for disposable logs;
- `runs/<purpose>/<run-id>/` for other isolated task scratch.

Resolve the root once, before exporting child temporary variables, and snapshot the inherited `TMPDIR` first. `PROJECT_TMP_BASE` selects `<base>/calibre-umcp`; `PROJECT_TMP_ROOT` remains a compatible explicit project-root override. Both must be usable absolute, non-symlink paths, and if both are supplied they must agree; invalid, unusable, empty, or conflicting overrides fail. Without an override, CI tries `${RUNNER_TEMP}/calibre-umcp`, then the original `${TMPDIR}/calibre-umcp`, then the platform temporary directory. Local use tries writable `/workspace/tmp/calibre-umcp`, then platform temp. The vendored resolver keeps CI independent of `/workspace` and `/workspace/Makefile`.

Use the project `Makefile`, which exports the resolved `PROJECT_TMP_ROOT`, `TMPDIR`, `TMP`, `TEMP`, `PYTHONPYCACHEPREFIX`, and `PIP_CACHE_DIR`. Do not use a bare temporary directory, home-directory cache, ad-hoc root, or source directory for generated output. Tests must preserve their filesystem ownership, symlink, and isolation checks. `make test` runs ordinary development tests without profiling. During pre-release verification, run `make prerelease-test`, inspect its CPU cumulative and allocation reports, tune avoidable hotspots where justified, and record only concise conclusions. The helper deletes its raw captures immediately after reporting.

`make clean` may remove only `cache/`, `build/`, `tests/`, `logs/`, and `runs/` beneath the validated resolved root. It must not remove receipts, datasets, backups, source, installed toolchains, active-job paths, or another project's files. Completed disposable logs and profiling artifacts are not durable evidence and must be removed after use.

Container deployment follows the same resolver contract. If `/workspace/tmp` is not mounted, allow the fallback order or explicitly set a validated `PROJECT_TMP_ROOT` ending in `/calibre-umcp`; `WORK` and `OUT` remain confined to its `runs/` and `build/` subtrees.
