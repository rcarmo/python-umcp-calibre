from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


class CalibreRpcBridge:
    """Tiny JSON-RPC bridge intended to run inside the Calibre process.

    The bridge serializes work onto a single worker so Calibre library operations
    do not run concurrently from HTTP handler threads.
    """

    def __init__(self, gui, token: str | None = None):
        self.gui = gui
        self.token = token
        self.jobs: queue.Queue[tuple[str, dict[str, Any], queue.Queue]] = queue.Queue()
        self.worker = threading.Thread(target=self._worker, name="calibre-umcp-worker", daemon=True)
        self.worker.start()

    def _worker(self):
        while True:
            method, params, reply = self.jobs.get()
            try:
                reply.put((True, self.dispatch(method, params)))
            except Exception as exc:
                reply.put((False, str(exc)))

    def call_serialized(self, method: str, params: dict[str, Any]) -> Any:
        reply: queue.Queue = queue.Queue(maxsize=1)
        self.jobs.put((method, params, reply))
        ok, payload = reply.get(timeout=600)
        if not ok:
            raise RuntimeError(payload)
        return payload

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        db = self.gui.current_db
        if method == "search_books":
            query = params.get("query") or ""
            limit = int(params.get("limit") or 50)
            ids = list(db.search_getting_ids(query, None) if query else db.all_book_ids())[:limit]
            return [self._metadata(db, book_id) for book_id in ids]
        if method == "get_book_metadata":
            return self._metadata(db, int(params["book_id"]))
        if method == "find_duplicates":
            return self._find_duplicates(db, int(params.get("limit") or 1000))
        raise NotImplementedError(f"Bridge method not implemented yet: {method}")

    def _metadata(self, db, book_id: int) -> dict[str, Any]:
        mi = db.get_metadata(book_id, index_is_id=True)
        return {
            "id": book_id,
            "title": mi.title,
            "authors": list(mi.authors or []),
            "identifiers": dict(mi.identifiers or {}),
            "formats": list(db.formats(book_id, index_is_id=True) or []),
        }

    def _find_duplicates(self, db, limit: int) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for book_id in list(db.all_book_ids())[:limit]:
            item = self._metadata(db, book_id)
            key = (item.get("title") or "").casefold().strip(), tuple(a.casefold().strip() for a in item.get("authors") or [])
            buckets.setdefault(repr(key), []).append(item)
        return [{"count": len(v), "books": v} for v in buckets.values() if len(v) > 1]


def serve_bridge(gui, host: str, port: int, token: str | None = None):
    bridge = CalibreRpcBridge(gui, token=token)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/rpc":
                self.send_error(404)
                return
            if token and self.headers.get("authorization") != f"Bearer {token}":
                self.send_error(401)
                return
            length = int(self.headers.get("content-length") or "0")
            payload = json.loads(self.rfile.read(length).decode())
            try:
                result = bridge.call_serialized(payload.get("method"), payload.get("params") or {})
                body = {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}
            except Exception as exc:
                body = {"jsonrpc": "2.0", "id": payload.get("id"), "error": {"message": str(exc)}}
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="calibre-umcp-http", daemon=True)
    thread.start()
    return server
