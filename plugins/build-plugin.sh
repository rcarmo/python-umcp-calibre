#!/bin/sh
# Build the in-Calibre plugin ZIP without writing generated files into source.
set -eu
cd "$(dirname "$0")"
PROJECT_TMP_ROOT=${PROJECT_TMP_ROOT:-/workspace/tmp/calibre-umcp}
OUT=${OUT:-$PROJECT_TMP_ROOT/build/calibre-umcp-plugin.zip}
case "$PROJECT_TMP_ROOT" in /workspace/tmp/calibre-umcp) ;; *) echo "PROJECT_TMP_ROOT must be /workspace/tmp/calibre-umcp" >&2; exit 1;; esac
mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"
export OUT
python3 - <<'PY'
import os
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

plugin = Path('calibre_umcp_plugin')
shared = Path('../src/calibre_umcp')
out = Path(os.environ['OUT'])
with ZipFile(out, 'w', ZIP_DEFLATED) as archive:
    for path in sorted(plugin.iterdir()):
        if path.is_file() and path.suffix not in {'.pyc'}:
            archive.write(path, path.name)
    archive.write(shared / 'umcp.py', 'umcp.py')
    archive.write(shared / 'umcp_shared.py', 'umcp_shared.py')
print(out)
PY
