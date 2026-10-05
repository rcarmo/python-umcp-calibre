#!/usr/bin/env bash
# Build and install the in-Calibre plugin ZIP from raw source files. Despite the
# script name, SOURCE_BASE defaults to the repository's raw GitHub URL unless overridden.
set -euo pipefail

SOURCE_BASE=${SOURCE_BASE:-https://raw.githubusercontent.com/rcarmo/python-umcp-calibre/main}
explicit_root=${PROJECT_TMP_ROOT+x}
original_tmpdir=${TMPDIR:-}
path_usable() {
  local path="$1" parent
  [[ "$path" == /* && "${path##*/}" == calibre-umcp && ! -L "$path" ]] || return 1
  case "/${path#/}/" in */../*|*/./*) return 1;; esac
  if [[ -e "$path" ]]; then [[ -d "$path" && -O "$path" && -w "$path" && -x "$path" ]] || return 1; fi
  parent="$path"; while [[ ! -e "$parent" && ! -L "$parent" ]]; do parent="${parent%/*}"; [[ -n "$parent" ]] || parent=/; done
  [[ -d "$parent" && -w "$parent" && -x "$parent" ]]
}
if [[ -n "$explicit_root" ]]; then
  PROJECT_TMP_ROOT=${PROJECT_TMP_ROOT%/}
  path_usable "$PROJECT_TMP_ROOT" || { echo 'Invalid explicit PROJECT_TMP_ROOT' >&2; exit 1; }
else
  PROJECT_TMP_ROOT=
  for base in /workspace/tmp "${RUNNER_TEMP:-}" "$original_tmpdir" /tmp; do
    [[ -n "$base" ]] || continue
    candidate="${base%/}/calibre-umcp"
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

for path in "$PROJECT_TMP_ROOT" "$PROJECT_TMP_ROOT/cache" "$PROJECT_TMP_ROOT/build" "$PROJECT_TMP_ROOT/runs"; do
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
