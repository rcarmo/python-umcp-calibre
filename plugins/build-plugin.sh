#!/bin/sh
set -eu
cd "$(dirname "$0")"
rm -f calibre-umcp-plugin.zip
( cd calibre_umcp_plugin && zip -r ../calibre-umcp-plugin.zip . )
echo "plugins/calibre-umcp-plugin.zip"
