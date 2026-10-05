#!/usr/bin/env bash
# Build and install the in-Calibre plugin ZIP from raw source files. Despite the
# script name, SOURCE_BASE defaults to the repository's raw GitHub URL unless overridden.
set -euo pipefail

SOURCE_BASE=${SOURCE_BASE:-https://raw.githubusercontent.com/rcarmo/python-umcp-calibre/main}
original_tmpdir=${TMPDIR:-}
path_usable() {
  local path="$1" parent ancestor
  [[ "$path" == /* && ! -L "$path" ]] || return 1
  case "/${path#/}/" in */../*|*/./*) return 1;; esac
  ancestor="${path%/*}"; while [[ -n "$ancestor" && "$ancestor" != / ]]; do
    [[ ! -L "$ancestor" || "$ancestor" == /workspace ]] || return 1; ancestor="${ancestor%/*}"
  done
  if [[ -e "$path" ]]; then [[ -d "$path" && -O "$path" && -w "$path" && -x "$path" ]] || return 1; fi
  parent="$path"; while [[ ! -e "$parent" && ! -L "$parent" ]]; do parent="${parent%/*}"; [[ -n "$parent" ]] || parent=/; done
  [[ -d "$parent" && -w "$parent" && -x "$parent" ]]
}
is_ci() {
  case "${CI:-}" in ''|0|false|FALSE) ;; *) return 0;; esac
  case "${GITHUB_ACTIONS:-}:${GITLAB_CI:-}:${TF_BUILD:-}:${CIRCLECI:-}" in *true*|*True*|*TRUE*) return 0;; esac
  return 1
}
explicit_base_root=
if [[ -n "${PROJECT_TMP_BASE+x}" ]]; then
  [[ -n "$PROJECT_TMP_BASE" ]] || { echo 'PROJECT_TMP_BASE must not be empty' >&2; exit 1; }
  explicit_base_root="${PROJECT_TMP_BASE%/}/calibre-umcp"
  path_usable "$explicit_base_root" || { echo 'Invalid explicit PROJECT_TMP_BASE' >&2; exit 1; }
fi
if [[ -n "${PROJECT_TMP_ROOT+x}" ]]; then
  PROJECT_TMP_ROOT=${PROJECT_TMP_ROOT%/}
  [[ "${PROJECT_TMP_ROOT##*/}" == calibre-umcp ]] && path_usable "$PROJECT_TMP_ROOT" || { echo 'Invalid explicit PROJECT_TMP_ROOT' >&2; exit 1; }
  [[ -z "$explicit_base_root" || "$PROJECT_TMP_ROOT" == "$explicit_base_root" ]] || { echo 'Conflicting PROJECT_TMP_BASE and PROJECT_TMP_ROOT' >&2; exit 1; }
elif [[ -n "$explicit_base_root" ]]; then
  PROJECT_TMP_ROOT=$explicit_base_root
else
  PROJECT_TMP_ROOT=
  if is_ci; then bases=("${RUNNER_TEMP:-}" "$original_tmpdir" /tmp); else bases=(/workspace/tmp /tmp); fi
  for base in "${bases[@]}"; do
    [[ -n "$base" ]] || continue; candidate="${base%/}/calibre-umcp"
    if path_usable "$candidate"; then PROJECT_TMP_ROOT="$candidate"; break; fi
  done
  [[ -n "$PROJECT_TMP_ROOT" ]] || { echo 'No writable project-owned temporary root available' >&2; exit 1; }
fi
export PROJECT_TMP_ROOT
WORK=${WORK:-$PROJECT_TMP_ROOT/runs/plugin-install/current/source}
OUT=${OUT:-$PROJECT_TMP_ROOT/build/calibre-umcp-plugin.zip}
CALIBRE_USER=${CALIBRE_USER:-abc}
CALIBRE_GROUP=${CALIBRE_GROUP:-users}
export WORK OUT
case "$WORK" in "$PROJECT_TMP_ROOT"/runs/*) ;; *) echo 'WORK must be under PROJECT_TMP_ROOT/runs' >&2; exit 1;; esac
case "$OUT" in "$PROJECT_TMP_ROOT"/build/*) ;; *) echo 'OUT must be under PROJECT_TMP_ROOT/build' >&2; exit 1;; esac

for path in "$PROJECT_TMP_ROOT" "$PROJECT_TMP_ROOT/cache" "$PROJECT_TMP_ROOT/build" "$PROJECT_TMP_ROOT/tests" "$PROJECT_TMP_ROOT/logs" "$PROJECT_TMP_ROOT/runs"; do
  test ! -L "$path" || { echo "Refusing symlink scratch path: $path" >&2; exit 1; }
done
rm -rf "$WORK"
mkdir -p "$WORK" "$(dirname "$OUT")"

for file in __init__.py bridge.py config.py mcp.py ui.py; do
  curl -fsSL \
    "$SOURCE_BASE/plugins/calibre_umcp_plugin/$file" \
    -o "$WORK/$file"
done
for file in umcp.py umcp_shared.py; do
  curl -fsSL \
    "$SOURCE_BASE/src/calibre_umcp/$file" \
    -o "$WORK/$file"
done
touch "$WORK/plugin-import-name-calibre_umcp_plugin.txt"

python3 - <<'PY'
import os
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
work = Path(os.environ['WORK'])
out = Path(os.environ['OUT'])
with ZipFile(out, 'w', ZIP_DEFLATED) as zf:
    for name in (
        '__init__.py',
        'bridge.py',
        'config.py',
        'mcp.py',
        'ui.py',
        'umcp.py',
        'umcp_shared.py',
        'plugin-import-name-calibre_umcp_plugin.txt',
    ):
        zf.write(work / name, name)
print(f'{out} {out.stat().st_size}')
PY

chown "$CALIBRE_USER:$CALIBRE_GROUP" "$OUT"
if command -v s6-setuidgid >/dev/null 2>&1; then
  s6-setuidgid "$CALIBRE_USER" calibre-customize -a "$OUT"
else
  calibre-customize -a "$OUT"
fi
calibre-customize -l | grep -i -A4 -B2 'umcp\|µMCP' || true
