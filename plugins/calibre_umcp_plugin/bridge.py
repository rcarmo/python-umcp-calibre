from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from ipaddress import ip_address
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


BRIDGE_VERSION = "0.1.0"


class BridgeMethodError(RuntimeError):
    pass


class CalibreRpcBridge:
    """JSON-RPC bridge intended to run inside the Calibre GUI process.

    HTTP handlers may run concurrently, but every library operation is serialized
    through one worker queue before touching Calibre's live database object.
    """

    def __init__(self, gui, token: str | None = None, audit_path: str | None = None):
        self.gui = gui
        self.token = token
        self.jobs: queue.Queue[tuple[str, dict[str, Any], queue.Queue]] = queue.Queue()
        self.job_records: dict[str, dict[str, Any]] = {}
        self.audit_path = Path(audit_path) if audit_path else None
        self.worker = threading.Thread(target=self._worker, name="calibre-umcp-worker", daemon=True)
        self.worker.start()

    def _worker(self):
        while True:
            method, params, reply = self.jobs.get()
            try:
                reply.put((True, self.dispatch(method, params)))
            except Exception as exc:  # Calibre exceptions are not JSON serializable
                reply.put((False, f"{type(exc).__name__}: {exc}"))

    def call_serialized(self, method: str, params: dict[str, Any]) -> Any:
        reply: queue.Queue = queue.Queue(maxsize=1)
        self.jobs.put((method, params, reply))
        ok, payload = reply.get(timeout=600)
        if not ok:
            raise RuntimeError(payload)
        return payload

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "ping":
            return {"ok": True, "version": BRIDGE_VERSION, "library_path": self._library_path()}
        if method == "list_libraries":
            return self._list_libraries()
        if method == "search_books":
            return self._search_books(params)
        if method == "get_book_metadata":
            return self._metadata(self._db(), int(params["book_id"]))
        if method == "find_duplicates":
            return self._find_duplicates(int(params.get("limit") or 1000))
        if method == "list_jobs":
            return self._list_jobs()
        if method == "get_job_status":
            return self._get_job_status(str(params["job_id"]))
        if method in {"convert_book", "copy_book", "move_book", "email_book"}:
            return self._reject_mutation_until_job_mapping_exists(method, params)
        raise NotImplementedError(f"Bridge method not implemented: {method}")

    def _db(self):
        return self.gui.current_db

    def _library_path(self) -> str:
        db = self._db()
        library_path = getattr(db, "library_path", None)
        if callable(library_path):
            library_path = library_path()
        return str(library_path or "")

    def _list_libraries(self) -> dict[str, str]:
        return {"current": self._library_path()}

    def _search_books(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        db = self._db()
        query = params.get("query") or ""
        limit = max(0, min(int(params.get("limit") or 50), 500))
        if query:
            # Calibre DB API returns ids for standard Calibre search syntax.
            ids = list(db.search_getting_ids(query, None))
        else:
            ids = list(db.all_book_ids())
        return [self._metadata(db, book_id) for book_id in ids[:limit]]

    def _metadata(self, db, book_id: int) -> dict[str, Any]:
        mi = db.get_metadata(book_id, index_is_id=True)
        formats_value = db.formats(book_id, index_is_id=True) or []
        formats = [f.strip() for f in formats_value.split(",") if f.strip()] if isinstance(formats_value, str) else list(formats_value)
        return {
            "id": book_id,
            "title": mi.title,
            "authors": list(mi.authors or []),
            "series": mi.series,
            "series_index": mi.series_index,
            "publisher": mi.publisher,
            "tags": list(mi.tags or []),
            "identifiers": dict(mi.identifiers or {}),
            "formats": formats,
            "library_path": self._library_path(),
        }

    def _find_duplicates(self, limit: int) -> list[dict[str, Any]]:
        db = self._db()
        buckets: dict[str, list[dict[str, Any]]] = {}
        for book_id in list(db.all_book_ids())[: max(0, min(limit, 5000))]:
            item = self._metadata(db, book_id)
            key = (
                (item.get("title") or "").casefold().strip(),
                tuple(a.casefold().strip() for a in item.get("authors") or []),
                json.dumps(item.get("identifiers") or {}, sort_keys=True),
            )
            buckets.setdefault(repr(key), []).append(item)
        return [{"count": len(v), "books": v} for v in buckets.values() if len(v) > 1]

    def _list_jobs(self) -> list[dict[str, Any]]:
        return sorted(self.job_records.values(), key=lambda item: item["created_at"])

    def _get_job_status(self, job_id: str) -> dict[str, Any]:
        try:
            return self.job_records[job_id]
        except KeyError as exc:
            raise BridgeMethodError(f"Unknown calibre-umcp job id: {job_id}") from exc

    def _reject_mutation_until_job_mapping_exists(self, method: str, params: dict[str, Any]) -> Any:
        """Fail closed for mutators until each operation is mapped to Calibre jobs.

        Investigation outcome:
        - convert_book should reuse Calibre's existing conversion action path, which
          ultimately queues ParallelJob instances via gui.job_manager.run_job().
        - generic long-running in-process mutations should use
          calibre.gui2.threaded_jobs.ThreadedJob and
          gui.job_manager.run_threaded_job(job), with type_="umcp-bridge" and
          max_concurrent_count=1 for serialized operator-visible queueing.
        - device/email flows should prefer existing GUI/device APIs where Calibre
          already creates DeviceJob or conversion/email jobs.
        - Calibre Jobs provide queue/progress/log visibility, not durable audit;
          JSONL audit records here complement, but do not replace, JobManager.
        """
        job_id = self._record_job(method, params, "rejected", "Safe Calibre JobManager/ThreadedJob mapping not implemented yet")
        raise BridgeMethodError(
            f"{method} rejected as unsafe until implemented through Calibre's in-process job APIs "
            f"(calibre-umcp audit job_id={job_id})"
        )

    def _record_job(self, method: str, params: dict[str, Any], status: str, message: str) -> str:
        job_id = uuid.uuid4().hex
        record = {
            "id": job_id,
            "method": method,
            "status": status,
            "message": message,
            "created_at": time.time(),
            "library_path": self._library_path(),
            "params": self._safe_params(params),
        }
        self.job_records[job_id] = record
        if self.audit_path:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        return job_id

    def _safe_params(self, params: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(params)
        for key in ("token", "password", "secret"):
            if key in redacted:
                redacted[key] = "<redacted>"
        return redacted


def is_loopback_bind(host: str) -> bool:
    if host in {"localhost", ""}:
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def serve_bridge(gui, host: str, port: int, token: str | None = None, audit_path: str | None = None):
    if not token and not is_loopback_bind(host):
        raise ValueError("CALIBRE_UMCP_BRIDGE_TOKEN is required when binding the bridge outside loopback")
    bridge = CalibreRpcBridge(gui, token=token, audit_path=audit_path)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if urlparse(self.path).path != "/health":
                self.send_error(404)
                return
            self._write_json(200, {"ok": True})

        def do_POST(self):
            if urlparse(self.path).path != "/rpc":
                self.send_error(404)
                return
            if token and self.headers.get("authorization") != f"Bearer {token}":
                self.send_error(401)
                return
            request_id = None
            try:
                length = int(self.headers.get("content-length") or "0")
                payload = json.loads(self.rfile.read(length).decode())
                request_id = payload.get("id")
                result = bridge.call_serialized(str(payload.get("method") or ""), payload.get("params") or {})
                body = {"jsonrpc": "2.0", "id": request_id, "result": result}
                self._write_json(200, body)
            except Exception as exc:
                self._write_json(200, {"jsonrpc": "2.0", "id": request_id, "error": {"message": str(exc)}})

        def _write_json(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.bridge = bridge  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, name="calibre-umcp-http", daemon=True)
    thread.start()
    return server
