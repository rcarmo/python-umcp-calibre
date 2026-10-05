#!/bin/sh
# Build the in-Calibre plugin ZIP without writing generated files into source.
set -eu
cd "$(dirname "$0")"
PROJECT_TMP_ROOT=$(../scripts/project-tmp.sh root)
export PROJECT_TMP_ROOT
OUT=${OUT:-$PROJECT_TMP_ROOT/build/calibre-umcp-plugin.zip}
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
