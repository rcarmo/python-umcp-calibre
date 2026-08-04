#!/bin/sh
set -eu
cd "$(dirname "$0")"
rm -f calibre-umcp-plugin.zip
find calibre_umcp_plugin -type d -name __pycache__ -prune -exec rm -rf {} +
cp ../src/calibre_umcp/umcp.py calibre_umcp_plugin/umcp.py
cp ../src/calibre_umcp/umcp_shared.py calibre_umcp_plugin/umcp_shared.py
trap 'rm -f calibre_umcp_plugin/umcp.py calibre_umcp_plugin/umcp_shared.py' EXIT
( cd calibre_umcp_plugin && zip -r ../calibre-umcp-plugin.zip . -x '*/__pycache__/*' '*.pyc' )
echo "plugins/calibre-umcp-plugin.zip"
