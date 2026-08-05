#!/bin/sh
# Run against an unpacked Calibre 9.12.0 bundle.
set -eu
: "${CALIBRE_RUNTIME_ROOT:?Set CALIBRE_RUNTIME_ROOT to Calibre 9.12.0}"
ROOT="$(mktemp -d -t calibre-umcp-content-XXXXXX)"
PID=
cleanup() {
    if [ -n "$PID" ]; then kill "$PID" 2>/dev/null || true; wait "$PID" 2>/dev/null || true; fi
    rm -rf "$ROOT"
}
trap cleanup EXIT INT TERM
mkdir -p "$ROOT/library" "$ROOT/config"
export CALIBRE_CONFIG_DIRECTORY="$ROOT/config"
"$CALIBRE_RUNTIME_ROOT/calibredb" add --with-library="$ROOT/library" \
    "$CALIBRE_RUNTIME_ROOT/resources/quick_start/eng.epub" >/dev/null
"$CALIBRE_RUNTIME_ROOT/calibre-server" --userdb="$ROOT/users.sqlite" \
    --manage-users -- add reader test-password >/dev/null
PORT="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(('127.0.0.1', 0))
print(s.getsockname()[1])
s.close()
PY
)"
"$CALIBRE_RUNTIME_ROOT/calibre-server" --listen-on=127.0.0.1 --port="$PORT" \
    --enable-auth --auth-mode=basic --userdb="$ROOT/users.sqlite" "$ROOT/library" \
    >"$ROOT/server.log" 2>&1 &
PID=$!
i=0
while [ "$i" -lt 50 ]; do
    sleep .2
    if curl -sf -o /dev/null "http://127.0.0.1:$PORT/"; then break; fi
    i=$((i + 1))
done
ANON="$(curl -sS -o "$ROOT/anon.body" -w '%{http_code}' "http://127.0.0.1:$PORT/mobile")"
AUTH="$(curl -sS -u reader:test-password -o "$ROOT/auth.body" -w '%{http_code}' "http://127.0.0.1:$PORT/mobile")"
[ "$ANON" = 401 ]
[ "$AUTH" = 200 ]
! grep -q "$ROOT/library" "$ROOT/auth.body"
printf '{"calibre_content_server":"9.12.0","anonymous_status":%s,"authenticated_status":%s,"raw_filesystem_path_exposed":false}\n' "$ANON" "$AUTH"
