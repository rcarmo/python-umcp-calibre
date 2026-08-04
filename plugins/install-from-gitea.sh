#!/bin/sh
set -eu

SOURCE_BASE=${SOURCE_BASE:-https://raw.githubusercontent.com/rcarmo/python-umcp-calibre/main}
WORK=${WORK:-/tmp/calibre-umcp-plugin-src}
OUT=${OUT:-/tmp/calibre-umcp-plugin.zip}
CALIBRE_USER=${CALIBRE_USER:-abc}
CALIBRE_GROUP=${CALIBRE_GROUP:-users}

rm -rf "$WORK"
mkdir -p "$WORK"

for file in __init__.py bridge.py ui.py; do
  curl -fsSL \
    "$SOURCE_BASE/plugins/calibre_umcp_plugin/$file" \
    -o "$WORK/$file"
done

python3 - <<'PY'
import os
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
work = Path(os.environ.get('WORK', '/tmp/calibre-umcp-plugin-src'))
out = Path(os.environ.get('OUT', '/tmp/calibre-umcp-plugin.zip'))
with ZipFile(out, 'w', ZIP_DEFLATED) as zf:
    for name in ('__init__.py', 'bridge.py', 'ui.py'):
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
