from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import queue
import re
import shutil
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from xml.etree import ElementTree
from functools import partial
from ipaddress import ip_address
from pathlib import Path

try:
    from . import PLUGIN_VERSION_STRING
except ImportError:  # Calibre loads plugin modules under calibre_plugins.*
    from calibre_plugins.calibre_umcp_plugin import PLUGIN_VERSION_STRING
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


BRIDGE_VERSION = PLUGIN_VERSION_STRING
SCHEMA_VERSION = 2
TOOLSET_VERSION = 4
SUPPORTED_CALIBRE_MUTATION_VERSION = (9, 12, 0)


def mutation_runtime_supported() -> bool:
    try:
        from calibre.constants import numeric_version
    except ImportError:  # Source-tree tests use fakes rather than Calibre itself.
        return True
    return tuple(numeric_version[:3]) == SUPPORTED_CALIBRE_MUTATION_VERSION


STABLE_READ_ERRORS = frozenset({
    "ACTIVE_LIBRARY_GENERATION_MISMATCH",
    "ACTIVE_LIBRARY_MISMATCH",
    "BOOK_NOT_FOUND",
    "CALIBRE_READ_FAILED",
    "CURSOR_INVALID",
    "CROSS_LIBRARY_SAME_LIBRARY",
    "CROSS_LIBRARY_TARGETS_REQUIRED",
    "FORMAT_NOT_FOUND",
    "FORMAT_READ_FAILED",
    "FORMAT_UNSUPPORTED",
    "INSPECTION_LIMIT_EXCEEDED",
    "INSPECTION_TIMEOUT",
    "JOB_NOT_FOUND",
    "LIBRARY_ALIAS_UNKNOWN",
    "LIBRARY_IDENTITY_MISMATCH",
    "LIBRARY_READ_DENIED",
    "LIBRARY_SWITCH_BLOCKED",
    "LIBRARY_SWITCH_DENIED",
    "LIBRARY_SWITCH_REQUIRED",
    "LIBRARY_UNAVAILABLE",
    "POLICY_DENIED",
})


STABLE_MUTATION_ERRORS = frozenset({
    "BOOK_NOT_FOUND",
    "FORMAT_NOT_FOUND",
    "DESTINATION_UNAVAILABLE",
    "DUPLICATE_REJECTED",
    "JOB_CANCELLED",
    "CALIBRE_JOB_FAILED",
    "POLICY_DENIED",
    "PATH_NOT_ALLOWED",
    "UNSUPPORTED_BY_CALIBRE_VERSION",
    "PARTIAL_COPY",
})


class BridgeMethodError(RuntimeError):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message or code
        super().__init__(f"{code}: {self.message}")


class CalibreRpcBridge:
    """JSON-RPC bridge intended to run inside the Calibre GUI process.

    HTTP handlers may run concurrently, but every library operation is serialized
    through one worker queue before touching Calibre's live database object.
    """

    def __init__(
        self,
        gui,
        token: str | None = None,
        audit_path: str | None = None,
        audit_retention: int = 500,
        gui_dispatch=None,
        import_roots: tuple[str, ...] = (),
        export_roots: tuple[str, ...] = (),
        destination_libraries: tuple[str, ...] = (),
        library_registry: tuple[dict[str, object], ...] = (),
        library_switching_enabled: bool = False,
        content_server_advertised_host: str = "",
        conversion_adapter=None,
        import_adapter=None,
        threaded_job_factory=None,
        copy_to_library_adapter=None,
        save_to_disk_adapter=None,
        email_config_adapter=None,
        email_send_adapter=None,
    ):
        self.gui = gui
        self.token = token
        self.jobs: queue.Queue = queue.Queue()
        self.job_records: dict[str, dict[str, Any]] = {}
        self.calibre_jobs: dict[str, Any] = {}
        self.audit_path = Path(audit_path) if audit_path else None
        self.audit_retention = max(10, min(int(audit_retention), 10000))
        self.import_roots = tuple(Path(root).expanduser().resolve() for root in import_roots)
        self.export_roots = tuple(Path(root).expanduser().resolve() for root in export_roots)
        self.destination_libraries = tuple(Path(root).expanduser().resolve() for root in destination_libraries)
        self.library_registry = tuple(dict(entry) for entry in library_registry)
        self.library_switching_enabled = bool(library_switching_enabled)
        self.content_server_advertised_host = str(content_server_advertised_host or "").strip()
        self.active_generation = 0
        self._conversion_adapter = conversion_adapter
        self._import_adapter = import_adapter
        self._threaded_job_factory = threaded_job_factory
        self._copy_to_library_adapter = copy_to_library_adapter
        self._save_to_disk_adapter = save_to_disk_adapter
        self._email_config_adapter = email_config_adapter
        self._email_send_adapter = email_send_adapter
        self._conversion_context: dict[str, dict[str, Any]] = {}
        self._import_context: dict[str, dict[str, Any]] = {}
        self._copy_context: dict[str, dict[str, Any]] = {}
        self._save_context: dict[str, dict[str, Any]] = {}
        self._email_context: dict[str, dict[str, Any]] = {}
        self._cancellation_requested: set[str] = set()
        self._cancelled_before_start: set[str] = set()
        self._records_lock = threading.RLock()
        self._closed = False
        self._gui_dispatch = gui_dispatch or self._create_gui_dispatcher()
        self.worker = threading.Thread(target=self._worker, name="calibre-umcp-worker", daemon=True)
        self.worker.start()

    def _create_gui_dispatcher(self):
        try:
            from calibre.gui2 import Dispatcher
        except ImportError:  # Source-tree tests do not run inside Calibre/Qt.
            return self._execute_on_gui
        return Dispatcher(self._execute_on_gui)

    def _worker(self):
        while True:
            item = self.jobs.get()
            if item is None:
                return
            method, params, reply = item
            # Dispatcher emits a queued Qt signal in Calibre. The worker waits for
            # the GUI thread to execute one operation before submitting another.
            self._gui_dispatch(method, params, reply)

    def _execute_on_gui(self, method: str, params: dict[str, Any], reply: queue.Queue) -> None:
        try:
            reply.put((True, self.dispatch(method, params)))
        except BridgeMethodError as exc:
            reply.put((False, {"code": exc.code, "message": exc.message}))
        except Exception as exc:  # Calibre exceptions are not JSON serializable
            reply.put((False, {"code": "CALIBRE_OPERATION_FAILED", "message": f"{type(exc).__name__}: {exc}"}))

    def call_serialized(self, method: str, params: dict[str, Any]) -> Any:
        if self._closed:
            raise BridgeMethodError("BRIDGE_SHUTTING_DOWN", "Calibre µMCP bridge is shutting down")
        reply: queue.Queue = queue.Queue(maxsize=1)
        self.jobs.put((method, params, reply))
        ok, payload = reply.get(timeout=600)
        if not ok:
            if isinstance(payload, dict):
                raise BridgeMethodError(str(payload.get("code") or "CALIBRE_OPERATION_FAILED"), str(payload.get("message") or "Operation failed"))
            raise BridgeMethodError("CALIBRE_OPERATION_FAILED", str(payload))
        return payload

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        manager = getattr(self.gui, "job_manager", None)
        killer = getattr(manager, "_kill_job", None)
        for job_id, native_job in tuple(self.calibre_jobs.items()):
            if callable(killer) and getattr(native_job, "killable", True) and getattr(native_job, "duration", None) is None:
                try:
                    self._mark_native_job_cancel_requested(job_id, native_job)
                    killer(native_job)
                    self._sync_calibre_job(job_id)
                    with self._records_lock:
                        terminal = self.job_records.get(job_id, {}).get("status") in {"completed", "failed", "cancelled", "rejected"}
                    if not terminal:
                        self._update_job(job_id, message="Cancellation requested during bridge shutdown")
                except Exception as exc:
                    self._update_job(job_id, message="Bridge shutdown could not cancel native job", error=str(exc))
        self.jobs.put(None)
        if threading.current_thread() is not self.worker:
            self.worker.join(timeout=2)

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method in {
            "update_book_metadata", "add_book_format", "delete_book_format", "set_book_cover",
            "add_book", "delete_books", "merge_duplicates", "convert_book", "copy_books_to_library",
            "move_books_to_library", "save_book_to_disk", "email_book",
        }:
            self._require_active_guards(params)
        if method == "ping":
            return {"ok": True, "version": BRIDGE_VERSION, "library_path": self._library_path()}
        if method == "list_libraries":
            return self._list_libraries()
        if method == "search_books":
            return self._search_books(params)
        if method == "get_book_metadata":
            db, alias = self._resolve_library(params.get("library"))
            return self._metadata(db, int(params["book_id"]), alias)
        if method == "get_book_formats":
            return self._get_book_formats(params)
        if method == "inspect_book_format":
            return self._inspect_book_format(params)
        if method == "assess_book_quality":
            return self._assess_book_quality(params)
        if method == "compare_book_quality":
            return self._compare_book_quality(params)
        if method == "find_duplicates":
            return self._find_duplicates(params)
        if method == "find_cross_library_duplicates":
            return self._find_cross_library_duplicates(params)
        if method == "switch_library":
            return self._switch_library(params)
        if method == "content_server_status":
            return self._content_server_status()
        if method == "list_jobs":
            return self._list_jobs()
        if method == "get_job_status":
            return self._get_job_status(str(params["job_id"]))
        if method == "update_book_metadata":
            return self._update_book_metadata(params)
        if method == "add_book_format":
            return self._add_book_format(params)
        if method == "delete_book_format":
            return self._delete_book_format(params)
        if method == "set_book_cover":
            return self._set_book_cover(params)
        if method == "add_book":
            return self._add_book(params)
        if method == "delete_books":
            return self._delete_books(params)
        if method == "merge_duplicates":
            return self._merge_duplicates(params)
        if method == "convert_book":
            return self._convert_book(params)
        if method == "copy_books_to_library":
            return self._copy_books_to_library(params, move=False)
        if method == "move_books_to_library":
            return self._copy_books_to_library(params, move=True)
        if method == "save_book_to_disk":
            return self._save_book_to_disk(params)
        if method == "email_book":
            return self._email_book(params)
        if method == "cancel_job":
            return self._cancel_job(str(params["job_id"]))
        if method in {"copy_book", "move_book"}:
            return self._reject_legacy_singular_mutation(method, params)
        raise NotImplementedError(f"Bridge method not implemented: {method}")

    def _db(self):
        return self.gui.current_db

    def _library_path(self) -> str:
        db = self._db()
        library_path = getattr(db, "library_path", None)
        if callable(library_path):
            library_path = library_path()
        return str(library_path or "")

    def _entry_for_path(self, path: str) -> dict[str, object] | None:
        try:
            resolved = Path(path).expanduser().resolve()
        except Exception:
            return None
        return next((entry for entry in self.library_registry if Path(str(entry["path"])).resolve() == resolved), None)

    def _active_alias(self) -> str:
        entry = self._entry_for_path(self._library_path())
        return str(entry["alias"]) if entry else "current"

    def _resolve_library(self, selector: Any = None):
        alias = str(selector or "current")
        if alias == "current" or alias == self._active_alias():
            db = self._db()
            canonical = self._active_alias()
        else:
            entry = next((item for item in self.library_registry if item.get("alias") == alias), None)
            if entry is None:
                raise BridgeMethodError("LIBRARY_ALIAS_UNKNOWN", "The requested library alias is not configured")
            if not entry.get("read", True):
                raise BridgeMethodError("LIBRARY_READ_DENIED", "Read access is disabled for the requested library")
            path = Path(str(entry["path"]))
            if not (path / "metadata.db").is_file():
                raise BridgeMethodError("LIBRARY_UNAVAILABLE", "The requested library is unavailable")
            broker = getattr(self.gui, "library_broker", None)
            getter = getattr(broker, "get_library", None)
            if not callable(getter):
                raise BridgeMethodError("LIBRARY_UNAVAILABLE", "Calibre's library broker is unavailable")
            try:
                db = getter(str(path))
            except Exception as exc:
                raise BridgeMethodError("CALIBRE_READ_FAILED", "Calibre could not open the requested library") from exc
            if db is None:
                raise BridgeMethodError("LIBRARY_UNAVAILABLE", "The requested library is unavailable")
            canonical = alias
        entry = next((item for item in self.library_registry if item.get("alias") == canonical), None)
        expected_id = str(entry.get("library_id") or "") if entry else ""
        observed_id = str(getattr(db, "library_id", "") or getattr(getattr(db, "new_api", None), "library_id", "") or "")
        if expected_id and observed_id and expected_id != observed_id:
            raise BridgeMethodError("LIBRARY_IDENTITY_MISMATCH", "The configured library identity has changed")
        return db, canonical

    def _list_libraries(self) -> dict[str, Any]:
        active = self._active_alias()
        libraries = []
        seen = set()
        for entry in self.library_registry:
            alias = str(entry["alias"])
            seen.add(alias)
            path = Path(str(entry["path"]))
            libraries.append({
                "alias": alias,
                "label": str(entry.get("label") or alias),
                "active": alias == active,
                "available": (path / "metadata.db").is_file() or alias == active,
                "readable": bool(entry.get("read", True)),
                "switchable": bool(entry.get("switch", False) and self.library_switching_enabled),
                "copy_destination": bool(entry.get("copy_destination", False)),
            })
        if active == "current" and active not in seen:
            libraries.insert(0, {"alias": "current", "label": "Current library", "active": True, "available": True, "readable": True, "switchable": False, "copy_destination": False})
        configured_targets = [item for item in libraries if not item["active"] and item["readable"]]
        available_targets = [item for item in configured_targets if item["available"]]
        reason_code = None
        if not configured_targets:
            reason_code = "NO_TARGET_LIBRARIES_CONFIGURED"
        elif not available_targets:
            reason_code = "NO_TARGET_LIBRARIES_AVAILABLE"
        return {
            "schema_version": SCHEMA_VERSION,
            "active_library": active,
            "active_generation": self.active_generation,
            "libraries": libraries,
            "cross_library_configured": bool(configured_targets),
            "cross_library_available": bool(available_targets),
            "readable_target_count": len(available_targets),
            "cross_library_reason_code": reason_code,
        }

    @staticmethod
    def _cursor_fingerprint(label: str, arguments: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps([label, arguments], sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]

    def _cursor_offset(self, cursor: Any, label: str, arguments: dict[str, Any]) -> int:
        if not cursor:
            return 0
        try:
            payload = json.loads(base64.urlsafe_b64decode(str(cursor) + "=" * (-len(str(cursor)) % 4)))
            if payload != {"v": 1, "o": int(payload["o"]), "f": self._cursor_fingerprint(label, arguments)} or payload["o"] < 0:
                raise ValueError
            return int(payload["o"])
        except Exception as exc:
            raise BridgeMethodError("CURSOR_INVALID", "The cursor does not match this request") from exc

    def _next_cursor(self, offset: int, label: str, arguments: dict[str, Any]) -> str:
        payload = {"v": 1, "o": offset, "f": self._cursor_fingerprint(label, arguments)}
        return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")

    def _search_books(self, params: dict[str, Any]) -> dict[str, Any]:
        db, alias = self._resolve_library(params.get("library"))
        query = str(params.get("query") or "")
        limit = max(1, min(int(params.get("limit") or 20), 500))
        arguments = {"library": alias, "query": query, "limit": limit}
        offset = self._cursor_offset(params.get("cursor"), "search", arguments)
        try:
            if query:
                ids = list(db.search_getting_ids(query, None))
            else:
                api = getattr(db, "new_api", None)
                ids = list(api.all_book_ids()) if callable(getattr(api, "all_book_ids", None)) else list(db.all_book_ids())
        except BridgeMethodError:
            raise
        except Exception as exc:
            raise BridgeMethodError("CALIBRE_READ_FAILED", "Calibre search failed") from exc
        ids = sorted(int(book_id) for book_id in ids)
        page = ids[offset: offset + limit]
        truncated = offset + limit < len(ids)
        return {
            "library": alias,
            "items": [self._metadata(db, book_id, alias) for book_id in page],
            "limit": limit,
            "truncated": truncated,
            "next_cursor": self._next_cursor(offset + limit, "search", arguments) if truncated else None,
        }

    def _metadata(self, db, book_id: int, library: str | None = None) -> dict[str, Any]:
        try:
            mi = db.get_metadata(book_id, index_is_id=True)
        except Exception as exc:
            raise BridgeMethodError("BOOK_NOT_FOUND", "The requested book does not exist in the selected library") from exc
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
            "library": library or self._active_alias(),
        }

    @staticmethod
    def _format_path(db, book_id: int, fmt: str) -> Path:
        try:
            db.get_metadata(book_id, index_is_id=True)
        except Exception as exc:
            raise BridgeMethodError("BOOK_NOT_FOUND", "The requested book does not exist in the selected library") from exc
        getter = getattr(db, "format_abspath", None)
        if not callable(getter):
            raise BridgeMethodError("FORMAT_READ_FAILED", "Calibre cannot provide the requested format")
        try:
            path_value = getter(book_id, fmt, index_is_id=True)
            path = Path(str(path_value or ""))
            if not path.is_file():
                raise FileNotFoundError
            return path
        except BridgeMethodError:
            raise
        except FileNotFoundError as exc:
            raise BridgeMethodError("FORMAT_NOT_FOUND", "The requested format is unavailable") from exc
        except Exception as exc:
            raise BridgeMethodError("FORMAT_READ_FAILED", "Calibre could not read the requested format") from exc

    def _get_book_formats(self, params: dict[str, Any]) -> dict[str, Any]:
        db, alias = self._resolve_library(params.get("library"))
        book_id = int(params["book_id"])
        metadata = self._metadata(db, book_id, alias)
        formats = []
        for fmt in sorted(metadata["formats"]):
            try:
                path = self._format_path(db, book_id, fmt)
                stat = path.stat()
                formats.append({
                    "format": fmt.upper(),
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
                    "available": True,
                })
            except BridgeMethodError as exc:
                formats.append({"format": fmt.upper(), "size_bytes": None, "modified_at": None, "available": False, "reason_code": exc.code})
        return {"schema_version": SCHEMA_VERSION, "library": alias, "book_id": book_id, "formats": formats}

    @staticmethod
    def _zip_read_bounded(archive: zipfile.ZipFile, name: str, maximum: int) -> bytes:
        try:
            info = archive.getinfo(name)
            if info.file_size > maximum:
                raise BridgeMethodError("INSPECTION_LIMIT_EXCEEDED", "An EPUB component exceeds the inspection limit")
            with archive.open(info) as stream:
                data = stream.read(maximum + 1)
        except BridgeMethodError:
            raise
        except Exception as exc:
            raise BridgeMethodError("FORMAT_READ_FAILED", "An EPUB component could not be read") from exc
        if len(data) > maximum:
            raise BridgeMethodError("INSPECTION_LIMIT_EXCEEDED", "An EPUB component exceeds the inspection limit")
        return data

    @staticmethod
    def _xml_root(data: bytes, failure_code: str = "FORMAT_READ_FAILED"):
        if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            raise BridgeMethodError("FORMAT_READ_FAILED", "UNSAFE_XML_DECLARATION: Unsafe XML declarations are not inspected")
        try:
            return ElementTree.fromstring(data)
        except ElementTree.ParseError as exc:
            raise BridgeMethodError(failure_code, "EPUB XML is invalid") from exc

    @staticmethod
    def _local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    @staticmethod
    def _embedded_identifier_kind(value: str) -> str:
        lowered = value.casefold()
        compact = re.sub(r"[^0-9x]", "", lowered)
        if "isbn" in lowered or len(compact) in {10, 13}:
            return "isbn"
        if lowered.startswith("urn:uuid:"):
            return "uuid"
        return ""

    def _inspect_epub(self, path: Path, record: dict[str, Any], started: float) -> dict[str, Any]:
        if path.stat().st_size > 64 * 1024 * 1024:
            raise BridgeMethodError("INSPECTION_LIMIT_EXCEEDED", "The format exceeds the 64 MiB inspection limit")
        warnings = []
        try:
            archive = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise BridgeMethodError("FORMAT_READ_FAILED", "The EPUB container could not be opened") from exc
        with archive:
            infos = archive.infolist()
            if len(infos) > 4096 or sum(info.file_size for info in infos) > 256 * 1024 * 1024:
                raise BridgeMethodError("INSPECTION_LIMIT_EXCEEDED", "The EPUB archive exceeds inspection limits")
            names = set(archive.namelist())
            valid_mimetype = "mimetype" in names and self._zip_read_bounded(archive, "mimetype", 128).strip() == b"application/epub+zip"
            if not valid_mimetype:
                warnings.append("epub_mimetype_invalid")
            container = self._xml_root(self._zip_read_bounded(archive, "META-INF/container.xml", 256 * 1024))
            rootfile = next((element.attrib.get("full-path") for element in container.iter() if self._local_name(element.tag) == "rootfile"), None)
            if not rootfile or rootfile not in names:
                raise BridgeMethodError("FORMAT_READ_FAILED", "The EPUB package document is unavailable")
            package = self._xml_root(self._zip_read_bounded(archive, rootfile, 2 * 1024 * 1024))
            manifest = {}
            spine_ids = []
            package_title = ""
            package_authors = []
            identifiers = []
            cover_id = None
            for element in package.iter():
                local = self._local_name(element.tag)
                if local == "item":
                    manifest[element.attrib.get("id", "")] = element.attrib
                elif local == "itemref":
                    spine_ids.append(element.attrib.get("idref", ""))
                elif local == "title" and not package_title:
                    package_title = "".join(element.itertext()).strip()
                elif local == "creator":
                    value = "".join(element.itertext()).strip()
                    if value:
                        package_authors.append(value)
                elif local == "identifier":
                    value = "".join(element.itertext()).strip()
                    if value:
                        identifiers.append(value)
                elif local == "meta" and element.attrib.get("name", "").lower() == "cover":
                    cover_id = element.attrib.get("content")
            cover_item = manifest.get(cover_id or "", {})
            has_cover = bool(cover_item) or any("cover-image" in item.get("properties", "").split() for item in manifest.values())
            nav_items = [item for item in manifest.values() if "nav" in item.get("properties", "").split()]
            ncx_items = [item for item in manifest.values() if item.get("media-type") == "application/x-dtbncx+xml"]
            toc_entries = 0
            package_dir = str(Path(rootfile).parent)
            for item in (nav_items or ncx_items)[:1]:
                member = str(Path(package_dir, item.get("href", ""))) if package_dir != "." else item.get("href", "")
                if member in names:
                    toc_root = self._xml_root(self._zip_read_bounded(archive, member, 2 * 1024 * 1024))
                    target_name = "a" if nav_items else "navPoint"
                    toc_entries = sum(1 for element in toc_root.iter() if self._local_name(element.tag) == target_name)
            estimated_chars = 0
            text_budget = 8 * 1024 * 1024
            text_parts = []
            for item_id in spine_ids[:256]:
                if time.monotonic() - started > 5:
                    raise BridgeMethodError("INSPECTION_TIMEOUT", "EPUB inspection exceeded five seconds")
                item = manifest.get(item_id, {})
                member = str(Path(package_dir, item.get("href", ""))) if package_dir != "." else item.get("href", "")
                if member not in names:
                    continue
                remaining = text_budget - sum(len(part) for part in text_parts)
                if remaining <= 0:
                    warnings.append("content_scan_truncated")
                    break
                data = self._zip_read_bounded(archive, member, min(2 * 1024 * 1024, remaining))
                text_parts.append(data)
                decoded = data.decode("utf-8", "replace")
                estimated_chars += len(re.sub(r"<[^>]+>", " ", decoded))
            combined = b"".join(text_parts).decode("utf-8", "replace")
            replacement_ratio = combined.count("\ufffd") / max(1, len(combined))
            control_ratio = sum(ord(char) < 32 and char not in "\n\r\t" for char in combined) / max(1, len(combined))
            garbage_score = round(min(1.0, replacement_ratio * 10 + control_ratio * 20), 4)
            normalise = lambda value: re.sub(r"\W+", "", str(value or "").casefold())
            title_match = bool(package_title and normalise(package_title) == normalise(record.get("title")))
            record_authors = {normalise(author) for author in record.get("authors", [])}
            author_match = bool(record_authors and record_authors.intersection(normalise(author) for author in package_authors))
            metadata_match = "full" if title_match and author_match else "partial" if title_match or author_match else "mismatch"
            return {
                "container": {"valid": valid_mimetype, "warnings": sorted(warnings)},
                "metadata": {
                    "title_present": bool(package_title),
                    "authors_present": bool(package_authors),
                    "identifiers_present": sorted({
                    kind for value in identifiers if (kind := self._embedded_identifier_kind(value))
                }),
                    "record_metadata_match": metadata_match,
                },
                "structure": {
                    "has_cover": has_cover,
                    "has_toc": toc_entries > 0,
                    "toc_entries": toc_entries,
                    "spine_items": len(spine_ids),
                    "image_count": sum(item.get("media-type", "").startswith("image/") for item in manifest.values()),
                    "stylesheet_count": sum(item.get("media-type") == "text/css" for item in manifest.values()),
                },
                "content_signals": {
                    "estimated_text_chars": estimated_chars,
                    "suspiciously_short": estimated_chars < 5000,
                    "encoding_warnings": ["replacement_characters"] if replacement_ratio > 0.001 else [],
                    "ocr_garbage_score": garbage_score,
                },
            }

    def _inspect_book_format(self, params: dict[str, Any]) -> dict[str, Any]:
        if bool(params.get("include_text_sample", False)):
            raise BridgeMethodError("POLICY_DENIED", "Text samples are disabled by the current read-only inspection policy")
        db, alias = self._resolve_library(params.get("library"))
        book_id = int(params["book_id"])
        fmt = str(params.get("format") or "").strip().upper()
        if fmt != "EPUB":
            raise BridgeMethodError("FORMAT_UNSUPPORTED", "Only bounded EPUB inspection is supported")
        record = self._metadata(db, book_id, alias)
        path = self._format_path(db, book_id, fmt)
        started = time.monotonic()
        result_queue: queue.Queue = queue.Queue(maxsize=1)

        def inspect_file() -> None:
            try:
                result_queue.put((True, self._inspect_epub(path, record, started)))
            except Exception as exc:
                result_queue.put((False, exc))

        inspector = threading.Thread(target=inspect_file, name="calibre-umcp-epub-inspector", daemon=True)
        inspector.start()
        try:
            ok, result = result_queue.get(timeout=5.5)
        except queue.Empty as exc:
            raise BridgeMethodError("INSPECTION_TIMEOUT", "EPUB inspection exceeded five seconds") from exc
        if not ok:
            if isinstance(result, BridgeMethodError):
                raise result
            raise BridgeMethodError("FORMAT_READ_FAILED", "The EPUB format could not be inspected") from result
        inspection = result
        return {
            "schema_version": SCHEMA_VERSION,
            "library": alias,
            "book_id": book_id,
            "format": fmt,
            **inspection,
            "limits": {
                "max_file_bytes": 64 * 1024 * 1024,
                "max_archive_entries": 4096,
                "max_uncompressed_bytes": 256 * 1024 * 1024,
                "timeout_seconds": 5,
                "truncated": "content_scan_truncated" in inspection["container"]["warnings"],
                "text_sample_included": False,
            },
        }

    @staticmethod
    def _generic_title(title: str) -> bool:
        compact = re.sub(r"[^a-z0-9]", "", str(title or "").casefold())
        return not compact or compact in {"unknown", "untitled", "ebook", "book"} or bool(re.fullmatch(r"(?:b0)?[a-z0-9]{8,16}", compact))

    def _assess_book_quality(self, params: dict[str, Any]) -> dict[str, Any]:
        db, alias = self._resolve_library(params.get("library"))
        book_id = int(params["book_id"])
        record = self._metadata(db, book_id, alias)
        requested = params.get("formats")
        available = sorted(str(fmt).upper() for fmt in record["formats"])
        selected = available if requested is None else [str(fmt).strip().upper() for fmt in requested]
        if not selected or len(selected) > 8:
            raise BridgeMethodError("FORMAT_NOT_FOUND", "One to eight available formats are required")
        metadata_warnings = []
        if not record.get("title"):
            metadata_warnings.append("metadata_title_missing")
        elif self._generic_title(record["title"]):
            metadata_warnings.append("generic_import_title")
        if not record.get("authors"):
            metadata_warnings.append("metadata_author_missing")
        if not record.get("publisher"):
            metadata_warnings.append("missing_publisher")
        format_scores = []
        inspection_errors = []
        degradable_codes = {"FORMAT_READ_FAILED", "INSPECTION_LIMIT_EXCEEDED", "INSPECTION_TIMEOUT"}
        for fmt in selected:
            if fmt not in available:
                raise BridgeMethodError("FORMAT_NOT_FOUND", "A requested format is unavailable")
            if fmt != "EPUB":
                format_scores.append({"format": fmt, "score": 0, "reasons": [], "warnings": ["unsupported_format"]})
                continue
            score = 45
            reasons = ["format_preferred:epub"]
            warnings = []
            try:
                inspection = self._inspect_book_format({"library": alias, "book_id": book_id, "format": fmt})
            except BridgeMethodError as exc:
                if exc.code not in degradable_codes:
                    raise
                reason = exc.message.split(":", 1)[0] if re.fullmatch(r"[A-Z0-9_]+:.*", exc.message) else exc.code
                warning = exc.code.casefold()
                warnings.extend([warning, "inspection_failed"])
                inspection_errors.append({"format": fmt, "code": exc.code, "reason": reason})
                score -= 15
            else:
                warnings.extend(inspection["container"]["warnings"])
                if inspection["container"]["valid"]:
                    score += 20
                    reasons.append("container_valid")
                else:
                    score -= 20
                    warnings.append("container_invalid")
                if inspection["structure"]["has_cover"]:
                    score += 8
                    reasons.append("has_cover")
                else:
                    warnings.append("missing_cover")
                if inspection["structure"]["has_toc"]:
                    score += 10
                    reasons.append("has_toc")
                else:
                    warnings.append("missing_toc")
                identifiers = inspection["metadata"]["identifiers_present"]
                if identifiers:
                    score += 7
                    reasons.extend(f"identifier:{identifier}" for identifier in identifiers)
                else:
                    warnings.append("missing_identifiers")
                if inspection["content_signals"]["suspiciously_short"]:
                    score -= 20
                    warnings.append("suspiciously_small_file")
            if "generic_import_title" in metadata_warnings:
                score -= 15
            if "metadata_title_missing" in metadata_warnings:
                score -= 20
            if "metadata_author_missing" in metadata_warnings:
                score -= 10
            format_scores.append({"format": fmt, "score": max(0, min(100, score)), "reasons": sorted(set(reasons)), "warnings": sorted(set(warnings))})
        best = max(format_scores, key=lambda item: (item["score"], item["format"] == "EPUB"))
        best_failed = "inspection_failed" in best["warnings"]
        grade = "unknown" if best_failed else "good" if best["score"] >= 75 else "fair" if best["score"] >= 50 else "poor"
        return {
            "schema_version": SCHEMA_VERSION,
            "library": alias,
            "book_id": book_id,
            "score": best["score"],
            "grade": grade,
            "best_format": best["format"] if best["score"] > 0 else None,
            "format_scores": format_scores,
            "metadata_warnings": sorted(metadata_warnings),
            "inspection_errors": inspection_errors,
        }

    def _compare_book_quality(self, params: dict[str, Any]) -> dict[str, Any]:
        policy = str(params.get("policy") or "prefer_epub_then_metadata")
        if policy != "prefer_epub_then_metadata":
            raise BridgeMethodError("POLICY_DENIED", "The requested quality comparison policy is unsupported")
        left_input = dict(params.get("left") or {})
        right_input = dict(params.get("right") or {})
        left = self._assess_book_quality(left_input)
        right = self._assess_book_quality(right_input)
        delta = right["score"] - left["score"]
        left_failed = bool(left["inspection_errors"])
        right_failed = bool(right["inspection_errors"])
        if left_failed and right_failed:
            keep = "undetermined"
        elif left_failed:
            keep = "right"
        elif right_failed:
            keep = "left"
        else:
            keep = "right" if delta > 0 else "left" if delta < 0 else "undetermined"
        reasons = []
        if left_failed:
            reasons.append("left_candidate_inspection_failed")
        if right_failed:
            reasons.append("right_candidate_inspection_failed")
        if left["best_format"] != right["best_format"]:
            preferred = "left" if left["best_format"] == "EPUB" else "right" if right["best_format"] == "EPUB" else None
            if preferred:
                reasons.append(f"{preferred}_has_preferred_format")
        if delta and keep != "undetermined" and not (left_failed or right_failed):
            reasons.append(f"{keep}_has_higher_quality_score")
        confidence = "low" if left_failed or right_failed else "high" if abs(delta) >= 20 else "medium" if abs(delta) >= 8 else "low"
        compact = lambda item: {
            "library": item["library"],
            "book_id": item["book_id"],
            "score": item["score"],
            "best_format": item["best_format"],
            "inspection_errors": item["inspection_errors"],
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "left": compact(left),
            "right": compact(right),
            "recommendation": {"keep": keep, "confidence": confidence, "reasons": reasons},
        }

    def _content_server_status(self) -> dict[str, Any]:
        server = getattr(self.gui, "content_server", None)
        loop = getattr(server, "loop", None)
        reported_running = getattr(server, "is_running", None)
        running = bool(
            server is not None
            and loop is not None
            and (
                reported_running
                if isinstance(reported_running, bool)
                else getattr(server, "current_thread", None) is not None
            )
        )
        if not running:
            return {
                "schema_version": SCHEMA_VERSION,
                "running": False,
                "authenticated": False,
                "base_url": None,
                "temporary_links_supported": False,
                "reason_code": "CONTENT_SERVER_NOT_RUNNING",
            }
        options = getattr(server, "opts", None)
        authenticated = bool(getattr(options, "auth", False))
        address = getattr(loop, "bound_address", None) or getattr(loop, "bind_address", None)
        host = str(address[0]) if isinstance(address, (tuple, list)) and address else ""
        port = int(address[1]) if isinstance(address, (tuple, list)) and len(address) > 1 else int(getattr(options, "port", 0) or 0)
        concrete_host = host not in {"", "0.0.0.0", "::", "::0"}
        advertised_host = host if concrete_host else self.content_server_advertised_host
        base_url = None
        if authenticated and advertised_host and port:
            if ":" in advertised_host and not advertised_host.startswith("["):
                advertised_host = f"[{advertised_host}]"
            scheme = "https" if getattr(options, "ssl_certfile", None) else "http"
            prefix = str(getattr(options, "url_prefix", "") or "").strip("/")
            base_url = f"{scheme}://{advertised_host}:{port}" + (f"/{prefix}" if prefix else "")
        reason_code = None
        if not authenticated:
            reason_code = "CONTENT_SERVER_AUTH_DISABLED"
        elif not advertised_host:
            reason_code = "ADVERTISED_CONTENT_SERVER_HOST_NOT_CONFIGURED"
        elif not port:
            reason_code = "CONTENT_SERVER_PORT_UNAVAILABLE"
        return {
            "schema_version": SCHEMA_VERSION,
            "running": True,
            "authenticated": authenticated,
            "base_url": base_url,
            "temporary_links_supported": False,
            "reason_code": reason_code,
        }

    @staticmethod
    def _normalised_match_fields(item: dict[str, Any]) -> tuple[dict[str, str], tuple[str, tuple[str, ...]]]:
        identifiers = {
            str(kind).casefold().strip(): re.sub(r"[\s-]+", "", str(value)).casefold()
            for kind, value in (item.get("identifiers") or {}).items()
            if str(kind).strip() and str(value).strip()
        }
        title_authors = (
            re.sub(r"\s+", " ", str(item.get("title") or "").casefold()).strip(),
            tuple(sorted(re.sub(r"\s+", " ", str(author).casefold()).strip() for author in item.get("authors") or [])),
        )
        return identifiers, title_authors

    def _all_book_ids(self, db) -> list[int]:
        api = getattr(db, "new_api", None)
        method = getattr(api, "all_book_ids", None)
        if not callable(method):
            raise BridgeMethodError("CALIBRE_READ_FAILED", "Calibre does not expose the tested book enumeration API")
        try:
            return sorted(int(book_id) for book_id in method())
        except Exception as exc:
            raise BridgeMethodError("CALIBRE_READ_FAILED", "Calibre could not enumerate books") from exc

    def _find_duplicates(self, params: dict[str, Any]) -> dict[str, Any]:
        db, alias = self._resolve_library(params.get("library"))
        limit = max(1, min(int(params.get("limit") or 1000), 5000))
        arguments = {"library": alias, "limit": limit}
        offset = self._cursor_offset(params.get("cursor"), "duplicates", arguments)
        ids = self._all_book_ids(db)
        selected = ids[offset: offset + limit]
        identifier_buckets: dict[tuple[str, str], set[int]] = {}
        title_buckets: dict[tuple[str, tuple[str, ...]], set[int]] = {}
        items: dict[int, dict[str, Any]] = {}
        for book_id in selected:
            item = self._metadata(db, book_id, alias)
            items[book_id] = item
            identifiers, title_authors = self._normalised_match_fields(item)
            for pair in identifiers.items():
                identifier_buckets.setdefault(pair, set()).add(book_id)
            if title_authors[0] and title_authors[1]:
                title_buckets.setdefault(title_authors, set()).add(book_id)
        groups: dict[tuple[int, ...], set[str]] = {}
        for (kind, _value), book_ids in identifier_buckets.items():
            if len(book_ids) > 1:
                groups.setdefault(tuple(sorted(book_ids)), set()).add(f"identifier:{kind}")
        for book_ids in title_buckets.values():
            if len(book_ids) > 1:
                groups.setdefault(tuple(sorted(book_ids)), set()).add("title_authors")
        result = [
            {"count": len(book_ids), "books": [items[book_id] for book_id in book_ids], "reasons": sorted(reasons)}
            for book_ids, reasons in sorted(groups.items())
        ]
        truncated = offset + limit < len(ids)
        return {
            "library": alias,
            "items": result,
            "limit": limit,
            "scanned": len(selected),
            "truncated": truncated,
            "next_cursor": self._next_cursor(offset + limit, "duplicates", arguments) if truncated else None,
        }

    def _cross_library_cursor(
        self, source_offset: int, target_index: int, target_offset: int, arguments: dict[str, Any]
    ) -> str:
        payload = {
            "v": 1,
            "s": source_offset,
            "ti": target_index,
            "to": target_offset,
            "f": self._cursor_fingerprint("cross-library", arguments),
        }
        return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")

    def _cross_library_cursor_offsets(self, cursor: Any, arguments: dict[str, Any]) -> tuple[int, int, int]:
        if not cursor:
            return 0, 0, 0
        try:
            payload = json.loads(base64.urlsafe_b64decode(str(cursor) + "=" * (-len(str(cursor)) % 4)))
            offsets = (int(payload["s"]), int(payload["ti"]), int(payload["to"]))
            if (
                payload.get("v") != 1
                or payload.get("f") != self._cursor_fingerprint("cross-library", arguments)
                or any(value < 0 for value in offsets)
            ):
                raise ValueError
            return offsets
        except Exception as exc:
            raise BridgeMethodError("CURSOR_INVALID", "The cross-library cursor does not match this request") from exc

    def _find_cross_library_duplicates(self, params: dict[str, Any]) -> dict[str, Any]:
        source_db, source_alias = self._resolve_library(params.get("source_library"))
        targets = params.get("target_libraries")
        if not isinstance(targets, list) or not targets or len(targets) > 16:
            raise BridgeMethodError("CROSS_LIBRARY_TARGETS_REQUIRED", "One to sixteen target library aliases are required")
        canonical_targets = []
        active_alias = self._active_alias()
        for selector in targets:
            alias = active_alias if str(selector) in {"current", active_alias} else str(selector)
            if alias == source_alias:
                raise BridgeMethodError("CROSS_LIBRARY_SAME_LIBRARY", "Source and target libraries must differ")
            if alias in canonical_targets:
                raise BridgeMethodError("CROSS_LIBRARY_TARGETS_REQUIRED", "Target library aliases must be unique")
            entry = next((item for item in self.library_registry if item.get("alias") == alias), None)
            if entry is None:
                raise BridgeMethodError("LIBRARY_ALIAS_UNKNOWN", "The requested library alias is not configured")
            if not entry.get("read", True):
                raise BridgeMethodError("LIBRARY_READ_DENIED", "Read access is disabled for the requested library")
            if not (Path(str(entry["path"])) / "metadata.db").is_file():
                raise BridgeMethodError("LIBRARY_UNAVAILABLE", "The requested library is unavailable")
            canonical_targets.append(alias)
        source_limit = max(1, min(int(params.get("limit") or 5), 25))
        target_limit = max(1, min(int(params.get("target_limit") or 100), 250))
        per_book = max(1, min(int(params.get("candidate_limit_per_book") or 20), 100))
        query = str(params.get("source_query") or "")
        arguments = {
            "source_library": source_alias,
            "target_libraries": canonical_targets,
            "source_query": query,
            "limit": source_limit,
            "target_limit": target_limit,
            "candidate_limit_per_book": per_book,
        }
        source_offset, target_index, target_offset = self._cross_library_cursor_offsets(params.get("cursor"), arguments)
        if target_index >= len(canonical_targets):
            raise BridgeMethodError("CURSOR_INVALID", "The cross-library cursor is outside the requested targets")
        all_source_ids = sorted(source_db.search_getting_ids(query, None)) if query else self._all_book_ids(source_db)
        if source_offset >= len(all_source_ids) and all_source_ids:
            raise BridgeMethodError("CURSOR_INVALID", "The cross-library cursor is past the source result set")
        source_ids = [int(book_id) for book_id in all_source_ids[source_offset: source_offset + source_limit]]
        if not source_ids:
            return {
                "source_library": source_alias,
                "target_libraries": canonical_targets,
                "source_scanned": 0,
                "scanned_source_count": 0,
                "source_total_known": len(all_source_ids),
                "target_libraries_scanned": 0,
                "target_library_scanned": None,
                "target_books_scanned": 0,
                "candidate_queries": 0,
                "matches": [],
                "truncated": False,
                "next_cursor": None,
            }
        target_alias = canonical_targets[target_index]
        target_db, target_alias = self._resolve_library(target_alias)
        all_target_ids = self._all_book_ids(target_db)
        if target_offset >= len(all_target_ids) and all_target_ids:
            raise BridgeMethodError("CURSOR_INVALID", "The cross-library cursor is past the target result set")
        target_ids = all_target_ids[target_offset: target_offset + target_limit]
        target_items = [self._metadata(target_db, book_id, target_alias) for book_id in target_ids]
        matches = []
        candidate_queries = 0
        for source_id in source_ids:
            source = self._metadata(source_db, source_id, source_alias)
            source_identifiers, source_title = self._normalised_match_fields(source)
            candidates = []
            for candidate in target_items:
                candidate_queries += 1
                candidate_identifiers, candidate_title = self._normalised_match_fields(candidate)
                shared = sorted(
                    kind for kind, value in source_identifiers.items()
                    if candidate_identifiers.get(kind) == value
                )
                reasons = [f"identifier:{kind}" for kind in shared]
                if source_title[0] and source_title == candidate_title:
                    reasons.append("title_authors")
                if not reasons:
                    continue
                conflicts = any(
                    kind in candidate_identifiers and candidate_identifiers[kind] != value
                    for kind, value in source_identifiers.items()
                )
                candidates.append({
                    **candidate,
                    "reasons": reasons,
                    "confidence": "high" if shared and not conflicts else "medium",
                })
                if len(candidates) >= per_book:
                    break
            if candidates:
                matches.append({"source": source, "candidates": candidates})
        next_offsets = None
        if target_offset + len(target_ids) < len(all_target_ids):
            next_offsets = (source_offset, target_index, target_offset + len(target_ids))
        elif target_index + 1 < len(canonical_targets):
            next_offsets = (source_offset, target_index + 1, 0)
        elif source_offset + len(source_ids) < len(all_source_ids):
            next_offsets = (source_offset + len(source_ids), 0, 0)
        next_cursor = self._cross_library_cursor(*next_offsets, arguments) if next_offsets else None
        return {
            "source_library": source_alias,
            "target_libraries": canonical_targets,
            "source_scanned": len(source_ids),
            "scanned_source_count": len(source_ids),
            "source_total_known": len(all_source_ids),
            "target_libraries_scanned": 1,
            "target_library_scanned": target_alias,
            "target_books_scanned": len(target_items),
            "candidate_queries": candidate_queries,
            "matches": matches,
            "truncated": next_cursor is not None,
            "next_cursor": next_cursor,
        }

    def _switch_library(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.library_switching_enabled:
            raise BridgeMethodError("LIBRARY_SWITCH_DENIED", "Library switching is disabled by plugin policy")
        target = str(params.get("library") or "")
        entry = next((item for item in self.library_registry if item.get("alias") == target), None)
        if entry is None:
            raise BridgeMethodError("LIBRARY_ALIAS_UNKNOWN", "The requested library alias is not configured")
        if not entry.get("switch", False):
            raise BridgeMethodError("LIBRARY_SWITCH_DENIED", "Switching to the requested library is disabled")
        self._require_active_guards({key: value for key, value in params.items() if key != "library"})
        expected_confirmation = f"SWITCH_LIBRARY:{target}"
        if params.get("confirmation") != expected_confirmation:
            raise BridgeMethodError("POLICY_DENIED", f"confirmation must be exactly {expected_confirmation}")
        if os.environ.get("CALIBRE_OVERRIDE_DATABASE_PATH"):
            raise BridgeMethodError("LIBRARY_SWITCH_BLOCKED", "Calibre's database override prevents switching")
        manager = getattr(self.gui, "job_manager", None)
        has_jobs = getattr(manager, "has_jobs", None)
        if (callable(has_jobs) and has_jobs()) or any(record.get("status") in {"queued", "running"} for record in self.job_records.values()):
            raise BridgeMethodError("LIBRARY_SWITCH_BLOCKED", "Library switching is blocked while jobs are active")
        questions = getattr(getattr(self.gui, "proceed_question", None), "questions", None)
        if questions:
            raise BridgeMethodError("LIBRARY_SWITCH_BLOCKED", "Library switching is blocked by pending Calibre questions")
        switch = getattr(self.gui, "library_moved", None)
        if not callable(switch):
            raise BridgeMethodError("LIBRARY_SWITCH_DENIED", "Calibre's tested GUI switch API is unavailable")
        try:
            switch(str(entry["path"]), allow_rebuild=False)
        except Exception as exc:
            raise BridgeMethodError("LIBRARY_SWITCH_BLOCKED", "Calibre could not switch to the requested library") from exc
        observed = self._entry_for_path(self._library_path())
        if observed is None or observed.get("alias") != target:
            raise BridgeMethodError("LIBRARY_IDENTITY_MISMATCH", "Calibre did not activate the requested library")
        self.active_generation += 1
        return {"active_library": target, "active_generation": self.active_generation, "switched": True}

    def _require_active_guards(self, params: dict[str, Any]) -> None:
        expected_alias = params.get("expected_active_library")
        expected_generation = params.get("expected_active_generation")
        if expected_alias is not None and str(expected_alias) not in {"current", self._active_alias()}:
            raise BridgeMethodError("ACTIVE_LIBRARY_MISMATCH", "The active library changed after the request was prepared")
        if expected_generation is not None and int(expected_generation) != self.active_generation:
            raise BridgeMethodError("ACTIVE_LIBRARY_GENERATION_MISMATCH", "The active library generation is stale")
        requested = params.get("library")
        if requested is not None and str(requested) not in {"current", self._active_alias()}:
            raise BridgeMethodError("LIBRARY_SWITCH_REQUIRED", "The requested mutation target is not the active library")

    def _new_api(self):
        try:
            from calibre.constants import numeric_version
        except ImportError:  # Source-tree tests use fakes rather than Calibre itself.
            numeric_version = SUPPORTED_CALIBRE_MUTATION_VERSION
        if not mutation_runtime_supported():
            raise BridgeMethodError(
                "UNSUPPORTED_BY_CALIBRE_VERSION",
                f"Calibre {'.'.join(map(str, numeric_version))} has not passed the exact 9.12.0 mutation contract tests",
            )
        api = getattr(self._db(), "new_api", None)
        required = ("has_id", "set_metadata", "format", "add_format", "remove_formats")
        if api is None or any(not callable(getattr(api, name, None)) for name in required):
            raise BridgeMethodError(
                "UNSUPPORTED_BY_CALIBRE_VERSION",
                "The active database does not expose the Calibre 9.12 mutation API contract",
            )
        return api

    def _require_book(self, api, book_id: int) -> None:
        if not api.has_id(book_id):
            raise BridgeMethodError("BOOK_NOT_FOUND", f"No book with id {book_id}")

    def _refresh_book(self, book_id: int) -> None:
        view = getattr(self.gui, "library_view", None)
        if view is None:
            return
        model = view.model()
        current = getattr(view, "currentIndex", lambda: None)()
        row = current.row() if current is not None and callable(getattr(current, "row", None)) else -1
        model.refresh_ids((book_id,), current_row=row)

    def _run_short_mutation(self, method: str, params: dict[str, Any], operation) -> dict[str, Any]:
        job_id = self._record_job(method, params, "queued", "Accepted for serialised GUI-thread execution")
        self._update_job(job_id, status="waiting_for_gui", message="Waiting for Calibre database access")
        self._update_job(job_id, status="running", message="Applying database mutation")
        try:
            result = operation()
        except Exception as exc:
            error = str(exc)
            self._update_job(job_id, status="failed", message="Mutation failed", error=error)
            raise
        return self._update_job(
            job_id,
            status="completed",
            progress=1.0,
            message="Mutation completed",
            result=result,
            error=None,
        )

    @staticmethod
    def _set_metadata_value(mi, field: str, value: Any) -> None:
        setter = getattr(mi, "set", None)
        if callable(setter):
            setter(field, value)
        else:
            setattr(mi, field, value)

    @staticmethod
    def _date_value(value: Any, field: str) -> Any:
        if value is None or isinstance(value, datetime):
            return value
        if not isinstance(value, str) or not value.strip():
            raise BridgeMethodError("POLICY_DENIED", f"{field} must be an ISO date/time string or null")
        try:
            from calibre.utils.date import parse_date
            return parse_date(value, assume_utc=False)
        except ImportError:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise BridgeMethodError("POLICY_DENIED", f"Invalid {field} value") from exc
        except Exception as exc:
            raise BridgeMethodError("POLICY_DENIED", f"Invalid {field} value") from exc

    def _prepare_metadata(self, api, original, changes: dict[str, Any]):
        if not isinstance(changes, dict) or not changes:
            raise BridgeMethodError("POLICY_DENIED", "changes must be a non-empty object")
        updated = copy.deepcopy(original)
        supported = {
            "title", "authors", "series", "series_index", "tags", "identifiers",
            "publisher", "language", "languages", "comments", "rating", "pubdate",
            "timestamp", "custom",
        }
        unknown = sorted(set(changes) - supported - {key for key in changes if key.startswith("#")})
        if unknown:
            raise BridgeMethodError("POLICY_DENIED", f"Unsupported metadata fields: {', '.join(unknown)}")

        for field, value in changes.items():
            if field == "custom" or field.startswith("#"):
                continue
            if field == "title" and (not isinstance(value, str) or not value.strip()):
                raise BridgeMethodError("POLICY_DENIED", "title must be a non-empty string")
            if field in {"authors", "tags", "languages"}:
                if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
                    raise BridgeMethodError("POLICY_DENIED", f"{field} must be a list of non-empty strings")
            if field == "authors" and not value:
                raise BridgeMethodError("POLICY_DENIED", "authors cannot be empty")
            if field in {"series", "publisher", "comments"} and value is not None and not isinstance(value, str):
                raise BridgeMethodError("POLICY_DENIED", f"{field} must be a string or null")
            if field == "series_index" and value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise BridgeMethodError("POLICY_DENIED", "series_index must be numeric or null")
            if field == "identifiers":
                if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
                    raise BridgeMethodError("POLICY_DENIED", "identifiers must be a string-to-string object")
                setter = getattr(updated, "set_identifiers", None)
                if callable(setter):
                    setter(value)
                else:
                    updated.identifiers = dict(value)
                continue
            if field == "language":
                if value is not None and (not isinstance(value, str) or not value.strip()):
                    raise BridgeMethodError("POLICY_DENIED", "language must be a non-empty string or null")
                self._set_metadata_value(updated, "languages", [value] if value else [])
                continue
            if field in {"pubdate", "timestamp"}:
                value = self._date_value(value, field)
            if field == "rating" and value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 10:
                    raise BridgeMethodError("POLICY_DENIED", "rating must be between 0 and 10")
            self._set_metadata_value(updated, field, value)

        raw_custom = changes.get("custom") or {}
        if not isinstance(raw_custom, dict):
            raise BridgeMethodError("POLICY_DENIED", "custom must be an object keyed by Calibre column name")
        custom = dict(raw_custom)
        custom.update({key: value for key, value in changes.items() if key.startswith("#")})
        field_metadata = getattr(api, "field_metadata", {})
        for key, value in custom.items():
            metadata = field_metadata.get(key) if hasattr(field_metadata, "get") else None
            if not metadata or not metadata.get("is_custom") or metadata.get("datatype") == "composite":
                raise BridgeMethodError("POLICY_DENIED", f"Unknown, non-custom or composite column: {key}")
            user_metadata = getattr(updated, "set_user_metadata", None)
            if callable(user_metadata):
                user_metadata(key, metadata)
            self._set_metadata_value(updated, key, value)
        return updated

    def _update_book_metadata(self, params: dict[str, Any]) -> dict[str, Any]:
        book_id = int(params["book_id"])
        changes = params.get("changes")

        def operation():
            api = self._new_api()
            self._require_book(api, book_id)
            db = self._db()
            original = copy.deepcopy(db.get_metadata(book_id, index_is_id=True))
            updated = self._prepare_metadata(api, original, changes)
            try:
                api.set_metadata(book_id, updated, force_changes=True, allow_case_change=True)
            except Exception as exc:
                rollback_error = None
                try:
                    api.set_metadata(book_id, original, force_changes=True, allow_case_change=True)
                except Exception as rollback_exc:
                    rollback_error = rollback_exc
                try:
                    self._refresh_book(book_id)
                except Exception:
                    pass
                detail = f"Metadata update failed: {exc}"
                if rollback_error is not None:
                    detail += f"; rollback failed: {rollback_error}"
                raise BridgeMethodError("CALIBRE_JOB_FAILED", detail) from exc
            self._refresh_book(book_id)
            return {"book_id": book_id, "updated_fields": sorted(changes)}

        return self._run_short_mutation("update_book_metadata", params, operation)

    def _allowed_import_path(self, value: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise BridgeMethodError("PATH_NOT_ALLOWED", "A source file path is required")
        candidate = Path(os.path.realpath(os.path.expanduser(value)))
        if not candidate.is_file():
            raise BridgeMethodError("PATH_NOT_ALLOWED", "The source file does not exist or is not a regular file")
        if not any(candidate == root or root in candidate.parents for root in self.import_roots):
            raise BridgeMethodError("PATH_NOT_ALLOWED", "The source file is outside configured import roots")
        return candidate

    def _allowed_export_path(self, value: Any, output_format: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise BridgeMethodError("PATH_NOT_ALLOWED", "An export_path is required when store_result=false")
        candidate = Path(os.path.realpath(os.path.expanduser(value)))
        if not any(candidate == root or root in candidate.parents for root in self.export_roots):
            raise BridgeMethodError("PATH_NOT_ALLOWED", "The export path is outside configured export roots")
        if candidate.suffix.lstrip(".").upper() != output_format:
            raise BridgeMethodError("PATH_NOT_ALLOWED", f"export_path must end in .{output_format.lower()}")
        if not candidate.parent.is_dir():
            raise BridgeMethodError("PATH_NOT_ALLOWED", "The export parent directory does not exist")
        return candidate

    @staticmethod
    def _format_name(value: Any, path: Path | None = None) -> str:
        fmt = str(value or (path.suffix[1:] if path else "")).strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{1,16}", fmt):
            raise BridgeMethodError("POLICY_DENIED", "format must be a valid extension name")
        return fmt

    def _add_book_format(self, params: dict[str, Any]) -> dict[str, Any]:
        book_id = int(params["book_id"])
        source = self._allowed_import_path(str(params.get("path") or ""))
        fmt = self._format_name(params.get("format"), source)
        replace = bool(params.get("replace", False))

        def operation():
            api = self._new_api()
            self._require_book(api, book_id)
            previous = api.format(book_id, fmt)
            if previous is not None and not replace:
                raise BridgeMethodError("DUPLICATE_REJECTED", f"Book {book_id} already has format {fmt}")
            try:
                accepted = api.add_format(book_id, fmt, str(source), replace=replace, run_hooks=True, dbapi=self._db())
                if not accepted:
                    raise BridgeMethodError("DUPLICATE_REJECTED", f"Calibre declined format {fmt}")
            except Exception as exc:
                rollback_error = None
                if previous is not None:
                    try:
                        api.add_format(
                            book_id, fmt, io.BytesIO(previous), replace=True,
                            run_hooks=False, dbapi=self._db(),
                        )
                    except Exception as rollback_exc:
                        rollback_error = rollback_exc
                try:
                    self._refresh_book(book_id)
                except Exception:
                    pass
                if isinstance(exc, BridgeMethodError) and rollback_error is None:
                    raise
                detail = f"Format import failed: {exc}"
                if rollback_error is not None:
                    detail += f"; rollback failed: {rollback_error}"
                raise BridgeMethodError("CALIBRE_JOB_FAILED", detail) from exc
            self._refresh_book(book_id)
            return {"book_id": book_id, "format": fmt, "replaced": previous is not None}

        return self._run_short_mutation("add_book_format", params, operation)

    def _delete_book_format(self, params: dict[str, Any]) -> dict[str, Any]:
        book_id = int(params["book_id"])
        fmt = self._format_name(params.get("format"))
        allow_last = bool(params.get("allow_last_format", False))

        def operation():
            api = self._new_api()
            self._require_book(api, book_id)
            previous = api.format(book_id, fmt)
            if previous is None:
                raise BridgeMethodError("FORMAT_NOT_FOUND", f"Book {book_id} has no {fmt} format")
            formats_value = self._db().formats(book_id, index_is_id=True) or ""
            formats = [item.strip().upper() for item in formats_value.split(",") if item.strip()] if isinstance(formats_value, str) else [str(item).upper() for item in formats_value]
            if len(formats) <= 1 and not allow_last:
                raise BridgeMethodError("POLICY_DENIED", "allow_last_format=true is required to remove the final format")
            try:
                api.remove_formats({book_id: {fmt}})
                if api.format(book_id, fmt) is not None:
                    raise RuntimeError("Calibre still reports the removed format")
            except Exception as exc:
                rollback_error = None
                try:
                    api.add_format(
                        book_id, fmt, io.BytesIO(previous), replace=True,
                        run_hooks=False, dbapi=self._db(),
                    )
                except Exception as rollback_exc:
                    rollback_error = rollback_exc
                try:
                    self._refresh_book(book_id)
                except Exception:
                    pass
                detail = f"Format deletion failed: {exc}"
                if rollback_error is not None:
                    detail += f"; rollback failed: {rollback_error}"
                raise BridgeMethodError("CALIBRE_JOB_FAILED", detail) from exc
            self._refresh_book(book_id)
            return {"book_id": book_id, "format": fmt, "removed": True}

        return self._run_short_mutation("delete_book_format", params, operation)

    def _make_threaded_job(self, type_name: str, description: str, func, args, callback):
        if self._threaded_job_factory is not None:
            return self._threaded_job_factory(type_name, description, func, args, callback)
        try:
            from calibre.gui2.threaded_jobs import ThreadedJob
        except ImportError as exc:
            raise BridgeMethodError(
                "UNSUPPORTED_BY_CALIBRE_VERSION",
                "Calibre's ThreadedJob API is unavailable",
            ) from exc
        return ThreadedJob(
            type_name,
            description,
            func,
            args,
            {},
            callback,
            max_concurrent_count=1,
            killable=True,
        )

    def _prepare_book_import(self, source: str, fmt: str, *, abort, log, notifications):
        if abort.is_set():
            raise BridgeMethodError("JOB_CANCELLED", "Import cancelled before metadata extraction")
        notifications.put((0.1, "Copying import source to bridge-managed temporary storage"))
        suffix = "." + fmt.lower()
        import tempfile
        handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        temp_path = handle.name
        try:
            with handle, open(source, "rb") as input_file:
                shutil.copyfileobj(input_file, handle)
            if abort.is_set():
                raise BridgeMethodError("JOB_CANCELLED", "Import cancelled before metadata extraction")
            notifications.put((0.5, "Extracting book metadata"))
            if self._import_adapter is not None:
                metadata = self._import_adapter(temp_path, fmt)
            else:
                from calibre.ebooks.metadata.meta import get_metadata
                with open(temp_path, "rb") as stream:
                    metadata = get_metadata(stream, stream_type=fmt.lower())
            notifications.put((1.0, "Import preparation complete"))
            return metadata, temp_path
        except Exception:
            Path(temp_path).unlink(missing_ok=True)
            raise

    def _add_book(self, params: dict[str, Any]) -> dict[str, Any]:
        source = self._allowed_import_path(str(params.get("path") or ""))
        fmt = self._format_name(params.get("format"), source)
        duplicate_policy = str(params.get("duplicate_policy") or "reject").strip().lower()
        if duplicate_policy not in {"reject", "skip", "add"}:
            raise BridgeMethodError(
                "POLICY_DENIED",
                "duplicate_policy must be reject, skip or add; merge policies require merge_duplicates_mutation",
            )
        job_id = self._record_job("add_book", params, "waiting_for_gui", "Preparing native Calibre import job")
        callback = partial(self._book_import_done, job_id)
        try:
            from calibre.gui2 import Dispatcher
            callback = Dispatcher(callback)
        except ImportError:
            pass
        try:
            native_job = self._make_threaded_job(
                "umcp-add-book",
                f"Import {source.name} into the active library",
                self._prepare_book_import,
                (str(source), fmt),
                callback,
            )
            self._import_context[job_id] = {
                "format": fmt,
                "duplicate_policy": duplicate_policy,
                "source_name": source.name,
            }
            self.calibre_jobs[job_id] = native_job
            self.gui.job_manager.run_threaded_job(native_job)
            return self._update_job(
                job_id,
                status="queued",
                calibre_job_id=getattr(native_job, "id", None),
                message="Queued metadata extraction in Calibre's JobManager",
            )
        except Exception as exc:
            self.calibre_jobs.pop(job_id, None)
            self._import_context.pop(job_id, None)
            error = exc if isinstance(exc, BridgeMethodError) else BridgeMethodError(
                "CALIBRE_JOB_FAILED", f"Book import was not queued: {exc}",
            )
            self._update_job(job_id, status="failed", message="Book import was not queued", error=str(error))
            if error is exc:
                raise
            raise error from exc

    def _book_import_done(self, job_id: str, native_job) -> None:
        self.calibre_jobs.pop(job_id, None)
        context = self._import_context.pop(job_id, None)
        if context is None:
            return
        temp_path = None
        try:
            if getattr(native_job, "killed", False):
                result = getattr(native_job, "result", None)
                if isinstance(result, (tuple, list)) and len(result) > 1:
                    Path(result[1]).unlink(missing_ok=True)
                self._update_job(
                    job_id,
                    status="cancelled",
                    message="Book import was cancelled before the library was changed",
                    error="JOB_CANCELLED: import cancelled",
                )
                return
            if getattr(native_job, "failed", False):
                exception = getattr(native_job, "exception", None)
                if isinstance(exception, BridgeMethodError) and exception.code == "JOB_CANCELLED":
                    self._update_job(
                        job_id,
                        status="cancelled",
                        message="Book import was cancelled before the library was changed",
                        error=str(exception),
                    )
                    return
                details = str(getattr(native_job, "details", "") or exception or "Import worker failed")[-4000:]
                self._update_job(
                    job_id, status="failed", message="Book import preparation failed",
                    error=f"CALIBRE_JOB_FAILED: {details}",
                )
                return
            metadata, temp_path = native_job.result
            api = self._new_api()
            add_books = getattr(api, "add_books", None)
            if not callable(add_books):
                raise BridgeMethodError("UNSUPPORTED_BY_CALIBRE_VERSION", "Calibre add_books is unavailable")
            add_duplicates = context["duplicate_policy"] == "add"
            ids, duplicates = add_books(
                [(metadata, {context["format"]: temp_path})],
                add_duplicates=add_duplicates,
                run_hooks=True,
                dbapi=self._db(),
            )
            if duplicates:
                if context["duplicate_policy"] == "skip":
                    self._update_job(
                        job_id,
                        status="completed",
                        progress=1.0,
                        message="Duplicate book skipped",
                        result={"added": False, "duplicate": True, "policy": "skip"},
                    )
                    return
                raise BridgeMethodError("DUPLICATE_REJECTED", "Calibre detected an existing duplicate book")
            if len(ids) != 1:
                raise BridgeMethodError("CALIBRE_JOB_FAILED", "Calibre did not return exactly one imported book id")
            book_id = int(ids[0])
            self._refresh_book(book_id)
            model = getattr(getattr(self.gui, "library_view", None), "model", lambda: None)()
            notify = getattr(model, "books_added", None)
            if callable(notify):
                notify(1)
            self._update_job(
                job_id,
                status="completed",
                progress=1.0,
                message="Book imported into active library",
                result={"added": True, "book_id": book_id, "format": context["format"]},
                error=None,
            )
        except Exception as exc:
            self._update_job(job_id, status="failed", message="Book import completion failed", error=str(exc))
        finally:
            if temp_path:
                Path(temp_path).unlink(missing_ok=True)

    def _set_book_cover(self, params: dict[str, Any]) -> dict[str, Any]:
        book_id = int(params["book_id"])
        remove = bool(params.get("remove", False))
        source = None if remove else self._allowed_import_path(str(params.get("path") or ""))

        def operation():
            api = self._new_api()
            self._require_book(api, book_id)
            cover_getter = getattr(api, "cover", None)
            setter = getattr(api, "set_cover", None)
            if not callable(cover_getter) or not callable(setter):
                raise BridgeMethodError("UNSUPPORTED_BY_CALIBRE_VERSION", "Cover APIs are unavailable")
            previous = cover_getter(book_id)
            data = None if remove else source.read_bytes()
            if data is not None and not data:
                raise BridgeMethodError("FORMAT_NOT_FOUND", "The cover file is empty")
            try:
                setter({book_id: data})
            except Exception as exc:
                rollback_error = None
                try:
                    setter({book_id: previous})
                except Exception as rollback_exc:
                    rollback_error = rollback_exc
                detail = f"Cover update failed: {exc}"
                if rollback_error is not None:
                    detail += f"; rollback failed: {rollback_error}"
                raise BridgeMethodError("CALIBRE_JOB_FAILED", detail) from exc
            self._refresh_book(book_id)
            refresh_covers = getattr(self.gui, "refresh_cover_browser", None)
            if callable(refresh_covers):
                refresh_covers()
            return {"book_id": book_id, "removed": remove, "replaced": previous is not None and not remove}

        return self._run_short_mutation("set_book_cover", params, operation)

    def _delete_books(self, params: dict[str, Any]) -> dict[str, Any]:
        raw_ids = params.get("book_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise BridgeMethodError("POLICY_DENIED", "book_ids must be a non-empty list")
        book_ids = tuple(dict.fromkeys(int(value) for value in raw_ids))
        permanent = bool(params.get("permanent", False))
        dry_run = bool(params.get("dry_run", True))

        def operation():
            api = self._new_api()
            for book_id in book_ids:
                self._require_book(api, book_id)
            preview = [
                {"book_id": book_id, "title": self._db().get_metadata(book_id, index_is_id=True).title}
                for book_id in book_ids
            ]
            confirmation = "DELETE_TO_TRASH:" + ",".join(map(str, book_ids))
            if dry_run:
                return {"dry_run": True, "permanent": False, "confirmation": confirmation, "books": preview}
            if permanent:
                raise BridgeMethodError("POLICY_DENIED", "Permanent deletion is not authorised")
            if params.get("confirmation") != confirmation:
                raise BridgeMethodError("POLICY_DENIED", "The exact dry-run confirmation value is required")
            remover = getattr(api, "remove_books", None)
            if not callable(remover):
                raise BridgeMethodError("UNSUPPORTED_BY_CALIBRE_VERSION", "Calibre trash removal is unavailable")
            remover(set(book_ids), permanent=False)
            remaining = [book_id for book_id in book_ids if api.has_id(book_id)]
            if remaining:
                raise BridgeMethodError("CALIBRE_JOB_FAILED", f"Calibre retained book ids: {remaining}")
            warnings = []
            model = getattr(getattr(self.gui, "library_view", None), "model", lambda: None)()
            notify = getattr(model, "ids_deleted", None)
            if callable(notify):
                try:
                    notify(book_ids)
                except Exception:
                    warnings.append("gui_model_notification_failed")
            return {"dry_run": False, "trashed": list(book_ids), "permanent": False, "warnings": warnings}

        return self._run_short_mutation("delete_books", params, operation)

    def _merge_duplicates(self, params: dict[str, Any]) -> dict[str, Any]:
        survivor_id = int(params["survivor_id"])
        raw_sources = params.get("source_ids")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise BridgeMethodError("POLICY_DENIED", "source_ids must be a non-empty list")
        source_ids = tuple(dict.fromkeys(int(value) for value in raw_sources if int(value) != survivor_id))
        if not source_ids:
            raise BridgeMethodError("POLICY_DENIED", "At least one source record distinct from the survivor is required")
        confirmation = "MERGE_KEEP_SOURCES:" + str(survivor_id) + ":" + ",".join(map(str, source_ids))
        if params.get("confirmation") != confirmation:
            raise BridgeMethodError("POLICY_DENIED", f"Exact confirmation required: {confirmation}")

        def operation():
            api = self._new_api()
            for book_id in (survivor_id, *source_ids):
                self._require_book(api, book_id)
            active_ids = set()
            for contexts in (
                self._conversion_context,
                self._import_context,
                self._copy_context,
                self._save_context,
                self._email_context,
            ):
                for context in contexts.values():
                    if context.get("book_id") is not None:
                        active_ids.add(int(context["book_id"]))
                    active_ids.update(int(value) for value in context.get("book_ids", ()))
            conflicts = sorted(active_ids.intersection((survivor_id, *source_ids)))
            if conflicts:
                raise BridgeMethodError("POLICY_DENIED", f"Books have active Calibre jobs: {conflicts}")
            merger = getattr(api, "merge_book_metadata", None)
            cover_getter = getattr(api, "cover", None)
            cover_setter = getattr(api, "set_cover", None)
            if not all(callable(value) for value in (merger, cover_getter, cover_setter)):
                raise BridgeMethodError("UNSUPPORTED_BY_CALIBRE_VERSION", "Calibre merge APIs are unavailable")
            original_metadata = copy.deepcopy(self._db().get_metadata(survivor_id, index_is_id=True))
            original_cover = cover_getter(survivor_id)
            original_formats = {
                fmt: api.format(survivor_id, fmt)
                for fmt in (self._db().formats(survivor_id, index_is_id=True) or "").split(",")
                if fmt
            }
            added_formats: list[str] = []
            try:
                for source_id in source_ids:
                    source_formats = self._db().formats(source_id, index_is_id=True) or ""
                    for fmt in (item.strip().upper() for item in source_formats.split(",") if item.strip()):
                        if api.format(survivor_id, fmt) is None:
                            data = api.format(source_id, fmt)
                            if data is not None and api.add_format(
                                survivor_id, fmt, io.BytesIO(data), replace=False,
                                run_hooks=False, dbapi=self._db(),
                            ):
                                added_formats.append(fmt)
                merger(
                    survivor_id,
                    list(source_ids),
                    bool(params.get("replace_cover", False)),
                    save_alternate_cover=bool(params.get("save_alternate_cover", False)),
                )
            except Exception as exc:
                rollback_errors = []
                try:
                    api.set_metadata(survivor_id, original_metadata, force_changes=True, allow_case_change=True)
                except Exception as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
                try:
                    cover_setter({survivor_id: original_cover})
                except Exception as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
                try:
                    current_formats = {
                        item.strip().upper()
                        for item in (self._db().formats(survivor_id, index_is_id=True) or "").split(",")
                        if item.strip()
                    }
                    extras = current_formats - set(original_formats)
                    if extras:
                        api.remove_formats({survivor_id: extras})
                    for fmt, data in original_formats.items():
                        api.add_format(survivor_id, fmt, io.BytesIO(data), replace=True, run_hooks=False, dbapi=self._db())
                except Exception as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
                detail = f"Duplicate merge failed: {exc}"
                if rollback_errors:
                    detail += "; rollback failed: " + "; ".join(rollback_errors)
                raise BridgeMethodError("CALIBRE_JOB_FAILED", detail) from exc
            self._refresh_book(survivor_id)
            return {
                "survivor_id": survivor_id,
                "source_ids": list(source_ids),
                "sources_deleted": False,
                "added_formats": sorted(set(added_formats)),
            }

        return self._run_short_mutation("merge_duplicates", params, operation)

    def _allowed_destination_library(self, value: Any) -> Path:
        if not value:
            raise BridgeMethodError("DESTINATION_UNAVAILABLE", "A configured destination library is required")
        selector = str(value)
        entry = next((item for item in self.library_registry if item.get("alias") == selector), None)
        if entry is not None:
            if not entry.get("copy_destination", False):
                raise BridgeMethodError("POLICY_DENIED", "Destination alias is not enabled for copy or move")
            candidate = Path(str(entry["path"])).expanduser().resolve()
        else:
            # One-release compatibility for deployments that still store destination paths.
            candidate = Path(selector).expanduser().resolve()
            if candidate not in self.destination_libraries:
                raise BridgeMethodError("POLICY_DENIED", "Destination is not in the UI-configured library registry")
        if candidate == Path(self._library_path()).expanduser().resolve():
            raise BridgeMethodError("POLICY_DENIED", "Source and destination libraries must differ")
        if not (candidate / "metadata.db").is_file():
            raise BridgeMethodError("DESTINATION_UNAVAILABLE", "Destination library is unavailable")
        return candidate

    @staticmethod
    def _copy_snapshot(api, book_id: int) -> dict[str, Any]:
        metadata = api.get_metadata(book_id, get_cover=True, cover_as_data=True)
        formats = tuple(str(fmt).upper() for fmt in (api.formats(book_id, verify_formats=True) or ()))
        hashes = {}
        for fmt in formats:
            value = api.format_hash(book_id, fmt)
            hashes[fmt] = value.hex() if isinstance(value, bytes) else str(value)
        cover_data = getattr(metadata, "cover_data", None)
        cover_bytes = cover_data[1] if isinstance(cover_data, (tuple, list)) and len(cover_data) > 1 else None
        cover_hash = hashlib.sha256(cover_bytes).hexdigest() if cover_bytes else None
        extra_hashes = {}
        list_extra_files = getattr(api, "list_extra_files", None)
        if callable(list_extra_files):
            for extra in list_extra_files(book_id):
                path = Path(extra.file_path)
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                extra_hashes[str(extra.relpath)] = digest.hexdigest()
        return {
            "title": str(metadata.title or ""),
            "authors": tuple(str(author) for author in (metadata.authors or ())),
            "identifiers": dict(getattr(metadata, "identifiers", None) or {}),
            "formats": hashes,
            "cover": cover_hash,
            "extra_files": extra_hashes,
            "metadata": metadata,
        }

    @staticmethod
    def _filtered_identical_data(api, destination_book_id: int):
        author_map, author_book_map, title_map, language_map = api.data_for_find_identical_books()
        filtered_author_books = {
            author_id: ({destination_book_id} if destination_book_id in book_ids else set())
            for author_id, book_ids in author_book_map.items()
        }
        return (
            author_map,
            filtered_author_books,
            {destination_book_id: title_map.get(destination_book_id, "")},
            {destination_book_id: language_map.get(destination_book_id)},
        )

    @staticmethod
    def _check_custom_column_compatibility(source_api, destination_api, metadata) -> None:
        keys = tuple(getattr(metadata, "custom_field_keys", lambda: ())())
        for key in keys:
            if metadata.get(key) in (None, "", [], (), {}):
                continue
            source = source_api.field_metadata.get(key)
            destination = destination_api.field_metadata.get(key)
            if not source or not destination:
                raise BridgeMethodError("DESTINATION_UNAVAILABLE", f"Destination lacks populated custom column {key}")
            if source.get("datatype") != destination.get("datatype"):
                raise BridgeMethodError("DESTINATION_UNAVAILABLE", f"Custom column {key} has an incompatible datatype")
            if source.get("datatype") == "text" and source.get("is_multiple") != destination.get("is_multiple"):
                raise BridgeMethodError("DESTINATION_UNAVAILABLE", f"Custom column {key} has incompatible multiplicity")

    def _copy_library_worker(
        self,
        source_path: str,
        destination_path: str,
        book_ids: tuple[int, ...],
        duplicate_policy: str,
        destination_book_ids: dict[int, int],
        *,
        abort,
        log,
        notifications,
    ) -> dict[str, Any]:
        if self._copy_to_library_adapter is not None:
            return self._copy_to_library_adapter(
                source_path,
                destination_path,
                book_ids,
                duplicate_policy,
                destination_book_ids,
                abort=abort,
                log=log,
                notifications=notifications,
            )
        try:
            from calibre.db.copy_to_library import copy_one_book
            from calibre.db.legacy import LibraryDatabase
        except ImportError as exc:
            raise BridgeMethodError(
                "UNSUPPORTED_BY_CALIBRE_VERSION",
                "Calibre's independent library copy APIs are unavailable",
            ) from exc
        source_db = destination_db = None
        copied: list[dict[str, Any]] = []
        try:
            source_db = LibraryDatabase(source_path, is_second_db=True)
            destination_db = LibraryDatabase(destination_path, is_second_db=True)
            source_api, destination_api = source_db.new_api, destination_db.new_api
            total = len(book_ids)
            for position, book_id in enumerate(book_ids, 1):
                if abort.is_set():
                    return {"ok": False, "code": "JOB_CANCELLED", "message": "Copy cancelled", "copied": copied}
                notifications.put(((position - 1) / total, f"Copying book {position} of {total}"))
                try:
                    if not source_api.has_id(book_id):
                        raise BridgeMethodError("BOOK_NOT_FOUND", f"Source book {book_id} no longer exists")
                    expected = self._copy_snapshot(source_api, book_id)
                    self._check_custom_column_compatibility(source_api, destination_api, expected["metadata"])
                    identical = set(destination_api.find_identical_books(expected["metadata"]))
                    if duplicate_policy in {"reject", "skip"} and identical:
                        if duplicate_policy == "skip":
                            copied.append({"source_book_id": book_id, "action": "skipped", "duplicate_ids": sorted(identical)})
                            continue
                        raise BridgeMethodError("DUPLICATE_REJECTED", f"Destination already contains a duplicate of book {book_id}")
                    expected_metadata = expected
                    if duplicate_policy in {"merge_missing", "replace"}:
                        destination_book_id = destination_book_ids.get(book_id)
                        if destination_book_id is None or not destination_api.has_id(destination_book_id):
                            raise BridgeMethodError(
                                "DUPLICATE_REJECTED",
                                f"An existing destination_book_id is required for source book {book_id}",
                            )
                        if destination_book_id not in identical:
                            raise BridgeMethodError(
                                "DUPLICATE_REJECTED",
                                f"Destination book {destination_book_id} is not an identified duplicate of source book {book_id}",
                            )
                        previous = self._copy_snapshot(destination_api, destination_book_id)
                        result = copy_one_book(
                            book_id,
                            source_db,
                            destination_db,
                            duplicate_action="add_formats_to_existing",
                            automerge_action="overwrite" if duplicate_policy == "replace" else "ignore",
                            identical_books_data=self._filtered_identical_data(destination_api, destination_book_id),
                        )
                        target_id = destination_book_id
                        expected_metadata = previous
                        expected_hashes = dict(previous["formats"])
                        for fmt, digest in expected["formats"].items():
                            if duplicate_policy == "replace" or fmt not in expected_hashes:
                                expected_hashes[fmt] = digest
                    else:
                        result = copy_one_book(book_id, source_db, destination_db, duplicate_action="add")
                        target_id = int(result.get("new_book_id") or 0)
                        if not target_id:
                            raise BridgeMethodError("PARTIAL_COPY", f"Calibre did not return a destination id for book {book_id}")
                        expected_hashes = expected["formats"]
                    actual = self._copy_snapshot(destination_api, target_id)
                    if any(
                        actual[field] != expected_metadata[field]
                        for field in ("title", "authors", "identifiers")
                    ):
                        raise BridgeMethodError("PARTIAL_COPY", f"Destination metadata verification failed for source book {book_id}")
                    if actual["formats"] != expected_hashes:
                        raise BridgeMethodError("PARTIAL_COPY", f"Destination format hash verification failed for source book {book_id}")
                    if duplicate_policy == "add" and (
                        actual["cover"] != expected["cover"]
                        or actual["extra_files"] != expected["extra_files"]
                    ):
                        raise BridgeMethodError("PARTIAL_COPY", f"Destination cover or extra-file verification failed for source book {book_id}")
                    copied.append({
                        "source_book_id": book_id,
                        "destination_book_id": target_id,
                        "action": str(result.get("action") or "add"),
                        "formats": sorted(actual["formats"]),
                    })
                except Exception as exc:
                    code = exc.code if isinstance(exc, BridgeMethodError) else "PARTIAL_COPY"
                    return {"ok": False, "code": code, "message": str(exc), "copied": copied, "failed_book_id": book_id}
            notifications.put((1.0, "Destination verification complete"))
            return {"ok": True, "copied": copied}
        finally:
            for database in (destination_db, source_db):
                if database is not None:
                    try:
                        database.close()
                    except Exception:
                        pass

    def _copy_books_to_library(self, params: dict[str, Any], *, move: bool) -> dict[str, Any]:
        try:
            book_ids = tuple(dict.fromkeys(int(value) for value in params.get("book_ids", ())) )
        except (TypeError, ValueError) as exc:
            raise BridgeMethodError("BOOK_NOT_FOUND", "book_ids must be an integer list") from exc
        if not book_ids:
            raise BridgeMethodError("BOOK_NOT_FOUND", "At least one source book is required")
        source_path = Path(self._library_path()).expanduser().resolve()
        destination_path = self._allowed_destination_library(params.get("destination_library"))
        api = self._new_api()
        for book_id in book_ids:
            self._require_book(api, book_id)
        duplicate_policy = str(params.get("duplicate_policy") or "reject").strip().lower()
        if duplicate_policy not in {"reject", "skip", "add", "merge_missing", "replace"}:
            raise BridgeMethodError("POLICY_DENIED", "Unsupported duplicate policy")
        raw_destinations = params.get("destination_book_ids") or {}
        if not isinstance(raw_destinations, dict):
            raise BridgeMethodError("POLICY_DENIED", "destination_book_ids must be an object")
        try:
            destination_book_ids = {int(key): int(value) for key, value in raw_destinations.items()}
        except (TypeError, ValueError) as exc:
            raise BridgeMethodError("POLICY_DENIED", "destination_book_ids must map integer source ids to destination ids") from exc
        confirmation = "MOVE_TO_TRASH:" + ",".join(map(str, book_ids)) + ":" + hashlib.sha256(str(destination_path).encode()).hexdigest()[:16]
        operation = "move_books_to_library" if move else "copy_books_to_library"
        if move and bool(params.get("dry_run", True)):
            job_id = self._record_job(operation, params, "waiting_for_gui", "Preparing move preview")
            return self._update_job(
                job_id,
                status="completed",
                progress=1.0,
                message="Move preview completed; no library was changed",
                result={
                    "dry_run": True,
                    "source_book_ids": list(book_ids),
                    "destination_library": str(destination_path),
                    "duplicate_policy": duplicate_policy,
                    "confirmation": confirmation,
                },
            )
        if move and params.get("confirmation") != confirmation:
            raise BridgeMethodError("POLICY_DENIED", "The exact dry-run confirmation value is required")
        job_id = self._record_job(operation, params, "waiting_for_gui", "Preparing native Calibre library-copy job")
        callback = partial(self._copy_library_done, job_id)
        try:
            from calibre.gui2 import Dispatcher
            callback = Dispatcher(callback)
        except ImportError:
            pass
        try:
            native_job = self._make_threaded_job(
                "umcp-copy-library",
                f"{'Move' if move else 'Copy'} {len(book_ids)} book(s) to {destination_path.name}",
                self._copy_library_worker,
                (str(source_path), str(destination_path), book_ids, duplicate_policy, destination_book_ids),
                callback,
            )
            self._copy_context[job_id] = {
                "move": move,
                "source_path": source_path,
                "destination_path": destination_path,
                "book_ids": book_ids,
            }
            self.calibre_jobs[job_id] = native_job
            self.gui.job_manager.run_threaded_job(native_job)
            return self._update_job(
                job_id,
                status="queued",
                calibre_job_id=getattr(native_job, "id", None),
                message="Queued in Calibre's JobManager",
            )
        except Exception as exc:
            self.calibre_jobs.pop(job_id, None)
            self._copy_context.pop(job_id, None)
            error = exc if isinstance(exc, BridgeMethodError) else BridgeMethodError("CALIBRE_JOB_FAILED", str(exc))
            self._update_job(job_id, status="failed", message="Library copy was not queued", error=str(error))
            if error is exc:
                raise
            raise error from exc

    def _copy_library_done(self, job_id: str, native_job) -> None:
        self.calibre_jobs.pop(job_id, None)
        context = self._copy_context.pop(job_id, None)
        if context is None:
            return
        try:
            killed = bool(getattr(native_job, "killed", False))
            result = getattr(native_job, "result", None)
            if killed:
                if isinstance(result, dict) and result.get("ok"):
                    final = dict(result)
                    final.update({"destination_library": str(context["destination_path"]), "moved_to_trash": []})
                    if context["move"]:
                        self._update_job(
                            job_id,
                            status="failed",
                            progress=1.0,
                            message="Move was cancelled after destination verification; source was retained",
                            result=final,
                            error="PARTIAL_COPY: destination copy completed before source trash",
                        )
                    else:
                        self._update_job(
                            job_id,
                            status="completed",
                            progress=1.0,
                            message="Library copy completed before cancellation took effect",
                            result=final,
                            error=None,
                        )
                    return
                if isinstance(result, dict):
                    code = str(result.get("code") or "JOB_CANCELLED")
                    if result.get("copied"):
                        self._update_job(
                            job_id,
                            status="failed",
                            message="Move was interrupted after destination changes; source was retained" if context["move"] else "Library copy was interrupted after destination changes",
                            result=result,
                            error=f"PARTIAL_COPY: {result.get('message')}",
                        )
                        return
                    self._update_job(
                        job_id,
                        status="cancelled",
                        message="Library move was cancelled before source deletion" if context["move"] else "Library copy was cancelled",
                        result=result,
                        error=f"{code}: {result.get('message')}",
                    )
                    return
                self._update_job(
                    job_id,
                    status="cancelled",
                    message="Library move was cancelled before source deletion" if context["move"] else "Library copy was cancelled",
                    error="JOB_CANCELLED: operation cancelled",
                )
                return
            if getattr(native_job, "failed", False):
                detail = str(getattr(native_job, "details", "") or getattr(native_job, "exception", "") or "Copy worker failed")[-4000:]
                self._update_job(job_id, status="failed", message="Library copy failed", error=f"CALIBRE_JOB_FAILED: {detail}")
                return
            if not isinstance(result, dict):
                raise BridgeMethodError("CALIBRE_JOB_FAILED", "Copy worker returned no result")
            if not result.get("ok"):
                code = str(result.get("code") or "PARTIAL_COPY")
                if code == "JOB_CANCELLED" and not result.get("copied"):
                    self._update_job(
                        job_id,
                        status="cancelled",
                        message="Library move was cancelled before source deletion" if context["move"] else "Library copy was cancelled",
                        result=result,
                        error=f"{code}: {result.get('message')}",
                    )
                    return
                if result.get("copied"):
                    code = "PARTIAL_COPY"
                self._update_job(
                    job_id,
                    status="failed",
                    message="Move was incomplete; source was retained where possible" if context["move"] else "Library copy was incomplete",
                    result=result,
                    error=f"{code}: {result.get('message')}",
                )
                return
            moved_ids: list[int] = []
            warnings: list[str] = []
            if context["move"]:
                if Path(self._library_path()).expanduser().resolve() != context["source_path"]:
                    raise BridgeMethodError("PARTIAL_COPY", "Active source library changed before move deletion")
                copied_source_ids = {
                    int(item["source_book_id"]) for item in result["copied"]
                    if item.get("action") != "skipped"
                }
                if copied_source_ids != set(context["book_ids"]):
                    raise BridgeMethodError("PARTIAL_COPY", "Not every source book was copied and verified; source retained")
                api = self._new_api()
                missing = sorted(book_id for book_id in context["book_ids"] if not api.has_id(book_id))
                if missing:
                    raise BridgeMethodError("PARTIAL_COPY", f"Source changed before move deletion: {missing}")
                api.remove_books(set(context["book_ids"]), permanent=False)
                remaining = sorted(book_id for book_id in context["book_ids"] if api.has_id(book_id))
                if remaining:
                    raise BridgeMethodError("PARTIAL_COPY", f"Destination verified but source trash operation was incomplete: {remaining}")
                moved_ids = list(context["book_ids"])
                model = getattr(getattr(self.gui, "library_view", None), "model", lambda: None)()
                notify = getattr(model, "ids_deleted", None)
                if callable(notify):
                    try:
                        notify(moved_ids)
                    except Exception:
                        warnings.append("gui_model_notification_failed")
            final = dict(result)
            final.update({"destination_library": str(context["destination_path"]), "moved_to_trash": moved_ids, "warnings": warnings})
            self._update_job(
                job_id,
                status="completed",
                progress=1.0,
                message="Library move completed" if context["move"] else "Library copy completed",
                result=final,
                error=None,
            )
        except Exception as exc:
            self._update_job(job_id, status="failed", message="Post-copy completion failed; source was retained where possible", error=str(exc))

    def _allowed_export_directory(self, value: Any) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise BridgeMethodError("PATH_NOT_ALLOWED", "A destination_directory is required")
        candidate = Path(os.path.realpath(os.path.expanduser(value)))
        if not any(candidate == root or root in candidate.parents for root in self.export_roots):
            raise BridgeMethodError("PATH_NOT_ALLOWED", "The export directory is outside configured export roots")
        existing_parent = candidate
        while not existing_parent.exists() and existing_parent != existing_parent.parent:
            existing_parent = existing_parent.parent
        if not existing_parent.is_dir():
            raise BridgeMethodError("PATH_NOT_ALLOWED", "The export directory has no existing parent")
        return candidate

    @staticmethod
    def _save_options(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise BridgeMethodError("POLICY_DENIED", "options must be an object")
        allowed = {
            "template", "formats", "save_cover", "write_opf", "save_extra_files",
            "update_metadata", "asciiize", "to_lowercase", "replace_whitespace", "single_dir",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise BridgeMethodError("POLICY_DENIED", f"Unsupported save-to-disk options: {', '.join(unknown)}")
        options = dict(value)
        if "template" in options:
            template = str(options["template"])
            if not template or len(template) > 500:
                raise BridgeMethodError("POLICY_DENIED", "template must contain 1--500 characters")
            options["template"] = template
        if "formats" in options:
            formats = str(options["formats"]).strip().lower()
            if formats != "all" and not re.fullmatch(r"[a-z0-9]+(?:\s*,\s*[a-z0-9]+)*", formats):
                raise BridgeMethodError("POLICY_DENIED", "formats must be 'all' or a comma-separated extension list")
            options["formats"] = formats
        for key in allowed - {"template", "formats"}:
            if key in options and not isinstance(options[key], bool):
                raise BridgeMethodError("POLICY_DENIED", f"{key} must be boolean")
        return options

    def _save_disk_worker(
        self,
        source_path: str,
        book_id: int,
        destination_directory: str,
        options: dict[str, Any],
        overwrite: bool,
        *,
        abort,
        log,
        notifications,
    ) -> dict[str, Any]:
        if self._save_to_disk_adapter is not None:
            return self._save_to_disk_adapter(
                source_path,
                book_id,
                destination_directory,
                options,
                overwrite,
                abort=abort,
                log=log,
                notifications=notifications,
            )
        try:
            from calibre.db.legacy import LibraryDatabase
            from calibre.library.save_to_disk import config, save_to_disk
        except ImportError as exc:
            raise BridgeMethodError("UNSUPPORTED_BY_CALIBRE_VERSION", "Calibre save-to-disk APIs are unavailable") from exc
        import tempfile
        source_db = None
        destination = Path(destination_directory)
        configured_root = next(root for root in self.export_roots if destination == root or root in destination.parents)
        staging = Path(tempfile.mkdtemp(prefix=".calibre-umcp-save-", dir=str(configured_root)))
        backup = Path(tempfile.mkdtemp(prefix=".calibre-umcp-backup-", dir=str(configured_root)))
        published: list[Path] = []
        backed_up: list[tuple[Path, Path]] = []
        try:
            if abort.is_set():
                return {"ok": False, "code": "JOB_CANCELLED", "message": "Export cancelled before collection"}
            source_db = LibraryDatabase(source_path, is_second_db=True)
            if not source_db.new_api.has_id(book_id):
                return {"ok": False, "code": "BOOK_NOT_FOUND", "message": f"Book {book_id} no longer exists"}

            class SaveToDiskDatabaseAdapter:
                # Calibre 9.12's legacy save_to_disk() dereferences db.new_api,
                # calls get_metadata(index_is_id=True), then uses Cache-only
                # helpers such as pref() and copy_format_to(). Keep that mixed
                # compatibility boundary local to this adapter.
                def __init__(self, database):
                    self.new_api = self
                    self._database = database

                def get_metadata(self, book_id, index_is_id=True):
                    return self._database.get_metadata(book_id, index_is_id=index_is_id)

                def __getattr__(self, name):
                    cache = self._database.new_api
                    if hasattr(cache, name):
                        return getattr(cache, name)
                    return getattr(self._database, name)

            save_database = SaveToDiskDatabaseAdapter(source_db)
            save_options = config().parse()
            for key, value in options.items():
                setattr(save_options, key, value)
            notifications.put((0.1, "Saving through Calibre's template and sanitisation engine"))
            failures = save_to_disk(save_database, (book_id,), str(staging), opts=save_options)
            if failures:
                return {"ok": False, "code": "CALIBRE_JOB_FAILED", "message": str(failures[0][2])[-4000:]}
            files = sorted(path for path in staging.rglob("*") if path.is_file())
            if not files:
                return {"ok": False, "code": "FORMAT_NOT_FOUND", "message": "Calibre produced no export artefacts"}
            targets = [(path, destination / path.relative_to(staging)) for path in files]
            collisions = [str(target) for _, target in targets if target.exists()]
            if collisions and not overwrite:
                return {
                    "ok": False,
                    "code": "DUPLICATE_REJECTED",
                    "message": "Export artefacts already exist",
                    "collisions": collisions[:100],
                }
            if abort.is_set():
                return {"ok": False, "code": "JOB_CANCELLED", "message": "Export cancelled before publication"}
            notifications.put((0.8, "Publishing staged export artefacts"))
            for staged, target in targets:
                resolved_destination = Path(os.path.realpath(destination))
                resolved_target = Path(os.path.realpath(target))
                if not (
                    (resolved_destination == configured_root or configured_root in resolved_destination.parents)
                    and (resolved_target == configured_root or configured_root in resolved_target.parents)
                ):
                    raise BridgeMethodError("PATH_NOT_ALLOWED", "Export path escaped its configured root before publication")
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    relative = target.relative_to(destination)
                    saved = backup / relative
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, saved)
                    backed_up.append((target, saved))
                os.replace(staged, target)
                published.append(target)
            notifications.put((1.0, "Export publication complete"))
            return {
                "ok": True,
                "book_id": book_id,
                "destination_directory": str(destination),
                "artifacts": [str(path) for path in published[:200]],
                "artifact_count": len(published),
            }
        except Exception as exc:
            for target in reversed(published):
                target.unlink(missing_ok=True)
            rollback_failures = []
            for target, saved in reversed(backed_up):
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(saved, target)
                except Exception as rollback_exc:
                    rollback_failures.append(str(rollback_exc))
            message = str(exc)
            if rollback_failures:
                message += "; rollback failed: " + "; ".join(rollback_failures[:5])
            return {"ok": False, "code": "CALIBRE_JOB_FAILED", "message": message}
        finally:
            if source_db is not None:
                try:
                    source_db.close()
                except Exception:
                    pass
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(backup, ignore_errors=True)

    def _save_book_to_disk(self, params: dict[str, Any]) -> dict[str, Any]:
        book_id = int(params["book_id"])
        api = self._new_api()
        self._require_book(api, book_id)
        source_path = Path(self._library_path()).expanduser().resolve()
        destination = self._allowed_export_directory(params.get("destination_directory"))
        options = self._save_options(params.get("options"))
        overwrite = bool(params.get("overwrite", False))
        job_id = self._record_job("save_book_to_disk", params, "waiting_for_gui", "Preparing native Calibre save-to-disk job")
        callback = partial(self._save_disk_done, job_id)
        try:
            from calibre.gui2 import Dispatcher
            callback = Dispatcher(callback)
        except ImportError:
            pass
        try:
            native_job = self._make_threaded_job(
                "umcp-save-to-disk",
                f"Save book {book_id} to disk",
                self._save_disk_worker,
                (str(source_path), book_id, str(destination), options, overwrite),
                callback,
            )
            self._save_context[job_id] = {"book_id": book_id, "destination": destination}
            self.calibre_jobs[job_id] = native_job
            self.gui.job_manager.run_threaded_job(native_job)
            return self._update_job(
                job_id,
                status="queued",
                calibre_job_id=getattr(native_job, "id", None),
                message="Queued in Calibre's JobManager",
            )
        except Exception as exc:
            self.calibre_jobs.pop(job_id, None)
            self._save_context.pop(job_id, None)
            error = exc if isinstance(exc, BridgeMethodError) else BridgeMethodError("CALIBRE_JOB_FAILED", str(exc))
            self._update_job(job_id, status="failed", message="Save-to-disk was not queued", error=str(error))
            if error is exc:
                raise
            raise error from exc

    def _save_disk_done(self, job_id: str, native_job) -> None:
        self.calibre_jobs.pop(job_id, None)
        context = self._save_context.pop(job_id, None)
        if context is None:
            return
        result = getattr(native_job, "result", None)
        if getattr(native_job, "killed", False):
            if isinstance(result, dict) and result.get("ok"):
                self._update_job(
                    job_id,
                    status="completed",
                    progress=1.0,
                    message="Book saved to disk before cancellation took effect",
                    result=result,
                    error=None,
                )
                return
            if isinstance(result, dict):
                code = str(result.get("code") or "CALIBRE_JOB_FAILED")
                status = "cancelled" if code == "JOB_CANCELLED" else "failed"
                message = "Save-to-disk was cancelled" if status == "cancelled" else "Save-to-disk did not complete"
                self._update_job(job_id, status=status, message=message, result=result, error=f"{code}: {result.get('message')}")
                return
            self._update_job(job_id, status="cancelled", message="Save-to-disk was cancelled", error="JOB_CANCELLED: operation cancelled")
            return
        if getattr(native_job, "failed", False):
            detail = str(getattr(native_job, "details", "") or getattr(native_job, "exception", "") or "Save worker failed")[-4000:]
            self._update_job(job_id, status="failed", message="Save-to-disk failed", error=f"CALIBRE_JOB_FAILED: {detail}")
            return
        if not isinstance(result, dict):
            self._update_job(job_id, status="failed", message="Save-to-disk failed", error="CALIBRE_JOB_FAILED: Save worker returned no result")
            return
        if not result.get("ok"):
            code = str(result.get("code") or "CALIBRE_JOB_FAILED")
            status = "cancelled" if code == "JOB_CANCELLED" else "failed"
            message = "Save-to-disk was cancelled" if status == "cancelled" else "Save-to-disk did not complete"
            self._update_job(job_id, status=status, message=message, result=result, error=f"{code}: {result.get('message')}")
            return
        self._update_job(
            job_id,
            status="completed",
            progress=1.0,
            message="Book saved to disk",
            result=result,
            error=None,
        )

    def _configured_email_policy(self) -> tuple[dict[str, Any], dict[str, str]]:
        if self._email_config_adapter is not None:
            accounts, subjects = self._email_config_adapter()
            return dict(accounts or {}), dict(subjects or {})
        try:
            from calibre.utils.smtp import config as email_config
        except ImportError as exc:
            raise BridgeMethodError("UNSUPPORTED_BY_CALIBRE_VERSION", "Calibre e-mail configuration is unavailable") from exc
        settings = email_config().parse()
        return dict(settings.accounts or {}), dict(settings.subjects or {})

    def _email_worker(
        self,
        source_path: str,
        book_id: int,
        fmt: str,
        recipient: str,
        subject: str,
        text: str,
        attachment_name: str,
        *,
        abort,
        log,
        notifications,
    ) -> dict[str, Any]:
        if self._email_send_adapter is not None:
            return self._email_send_adapter(
                source_path,
                book_id,
                fmt,
                recipient,
                subject,
                text,
                attachment_name,
                abort=abort,
                log=log,
                notifications=notifications,
            )
        try:
            from calibre.db.legacy import LibraryDatabase
            from calibre.gui2.email import gui_sendmail
        except ImportError as exc:
            raise BridgeMethodError("UNSUPPORTED_BY_CALIBRE_VERSION", "Calibre e-mail job APIs are unavailable") from exc
        import tempfile
        database = None
        descriptor, attachment = tempfile.mkstemp(prefix="calibre-umcp-mail-", suffix="." + fmt.lower())
        os.close(descriptor)
        try:
            if abort.is_set():
                return {"ok": False, "code": "JOB_CANCELLED", "message": "E-mail cancelled before attachment preparation"}
            database = LibraryDatabase(source_path, is_second_db=True)
            api = database.new_api
            if not api.has_id(book_id):
                return {"ok": False, "code": "BOOK_NOT_FOUND", "message": f"Book {book_id} no longer exists"}
            notifications.put((0.1, "Preparing e-mail attachment from an independent library reader"))
            api.copy_format_to(book_id, fmt, attachment)
            size = Path(attachment).stat().st_size
            if size < 1:
                return {"ok": False, "code": "FORMAT_NOT_FOUND", "message": "The selected format produced an empty attachment"}
            if size > 50 * 1024 * 1024:
                return {"ok": False, "code": "POLICY_DENIED", "message": "Attachment exceeds the bridge's 50 MiB limit"}
            if abort.is_set():
                return {"ok": False, "code": "JOB_CANCELLED", "message": "E-mail cancelled before SMTP submission"}
            notifications.put((0.25, "Submitting through Calibre's configured mail transport"))
            gui_sendmail(
                attachment,
                attachment_name,
                recipient,
                subject,
                text,
                log=log,
                abort=abort,
                notifications=notifications,
            )
            if abort.is_set():
                return {"ok": False, "code": "JOB_CANCELLED", "message": "E-mail job was cancelled"}
            return {
                "ok": True,
                "book_id": book_id,
                "format": fmt,
                "recipient": recipient,
                "smtp_accepted": True,
                "delivery_confirmed": False,
            }
        finally:
            if database is not None:
                try:
                    database.close()
                except Exception:
                    pass
            Path(attachment).unlink(missing_ok=True)

    def _email_book(self, params: dict[str, Any]) -> dict[str, Any]:
        book_id = int(params["book_id"])
        api = self._new_api()
        self._require_book(api, book_id)
        recipient = str(params.get("recipient") or "").strip()
        accounts, subjects = self._configured_email_policy()
        if recipient not in accounts:
            raise BridgeMethodError("POLICY_DENIED", "Recipient is not configured in Calibre's e-mail preferences")
        account = accounts[recipient]
        configured_formats = account[0] if isinstance(account, (list, tuple)) and account else ""
        allowed_formats = {item.strip().upper() for item in str(configured_formats).split(",") if item.strip()}
        fmt = self._format_name(params.get("format"))
        if fmt not in allowed_formats:
            raise BridgeMethodError("POLICY_DENIED", f"Format {fmt} is not enabled for this configured recipient")
        has_format = getattr(api, "has_format", None)
        if not callable(has_format):
            raise BridgeMethodError("UNSUPPORTED_BY_CALIBRE_VERSION", "Calibre format-existence API is unavailable")
        if not has_format(book_id, fmt):
            if params.get("auto_convert"):
                raise BridgeMethodError(
                    "UNSUPPORTED_BY_CALIBRE_VERSION",
                    "Recipient-compatible conversion must be queued separately with convert_book_mutation",
                )
            raise BridgeMethodError("FORMAT_NOT_FOUND", f"Book {book_id} has no {fmt} format")
        metadata = api.get_metadata(book_id)
        title = str(metadata.title or f"Book {book_id}")
        configured_subject = str(subjects.get(recipient) or "").strip()
        subject = configured_subject or f"E-book: {title}"
        text = f"Attached is {title}, sent by Calibre."
        safe_title = re.sub(r"[^A-Za-z0-9._ -]+", "_", title).strip(" .")[:120] or f"book-{book_id}"
        attachment_name = f"{safe_title}.{fmt.lower()}"
        source_path = Path(self._library_path()).expanduser().resolve()
        job_id = self._record_job("email_book", params, "waiting_for_gui", "Preparing native Calibre e-mail job")
        callback = partial(self._email_done, job_id)
        try:
            from calibre.gui2 import Dispatcher
            callback = Dispatcher(callback)
        except ImportError:
            pass
        try:
            native_job = self._make_threaded_job(
                "email",
                f"Email {title} to configured recipient",
                self._email_worker,
                (str(source_path), book_id, fmt, recipient, subject, text, attachment_name),
                callback,
            )
            self._email_context[job_id] = {"book_id": book_id, "format": fmt, "recipient": recipient}
            self.calibre_jobs[job_id] = native_job
            self.gui.job_manager.run_threaded_job(native_job)
            return self._update_job(
                job_id,
                status="queued",
                calibre_job_id=getattr(native_job, "id", None),
                message="Queued in Calibre's JobManager",
            )
        except Exception as exc:
            self.calibre_jobs.pop(job_id, None)
            self._email_context.pop(job_id, None)
            error = exc if isinstance(exc, BridgeMethodError) else BridgeMethodError("CALIBRE_JOB_FAILED", str(exc))
            self._update_job(job_id, status="failed", message="E-mail was not queued", error=str(error))
            if error is exc:
                raise
            raise error from exc

    def _email_done(self, job_id: str, native_job) -> None:
        self.calibre_jobs.pop(job_id, None)
        context = self._email_context.pop(job_id, None)
        if context is None:
            return
        result = getattr(native_job, "result", None)
        if getattr(native_job, "killed", False):
            if isinstance(result, dict) and result.get("ok"):
                self._update_job(
                    job_id,
                    status="completed",
                    progress=1.0,
                    message="SMTP submission completed before cancellation took effect; recipient delivery is not confirmed",
                    result=result,
                    error=None,
                )
                return
            if isinstance(result, dict):
                code = str(result.get("code") or "CALIBRE_JOB_FAILED")
                status = "cancelled" if code == "JOB_CANCELLED" else "failed"
                message = "E-mail was cancelled" if status == "cancelled" else "SMTP submission did not complete"
                self._update_job(job_id, status=status, message=message, result=result, error=f"{code}: {result.get('message')}")
                return
            self._update_job(job_id, status="cancelled", message="E-mail was cancelled", error="JOB_CANCELLED: operation cancelled")
            return
        if getattr(native_job, "failed", False):
            detail = str(getattr(native_job, "details", "") or getattr(native_job, "exception", "") or "Calibre e-mail worker failed")[-4000:]
            self._update_job(job_id, status="failed", message="SMTP submission failed", error=f"CALIBRE_JOB_FAILED: {detail}")
            return
        if not isinstance(result, dict):
            self._update_job(job_id, status="failed", message="SMTP submission failed", error="CALIBRE_JOB_FAILED: E-mail worker returned no result")
            return
        if not result.get("ok"):
            code = str(result.get("code") or "CALIBRE_JOB_FAILED")
            status = "cancelled" if code == "JOB_CANCELLED" else "failed"
            message = "E-mail was cancelled" if status == "cancelled" else "SMTP submission did not complete"
            self._update_job(job_id, status=status, message=message, result=result, error=f"{code}: {result.get('message')}")
            return
        self._update_job(
            job_id,
            status="completed",
            progress=1.0,
            message="SMTP submission accepted; recipient delivery is not confirmed",
            result=result,
            error=None,
        )

    def _prepare_conversion(self, book_id: int, output_format: str):
        if self._conversion_adapter is not None:
            return self._conversion_adapter(self.gui, self._db(), book_id, output_format)
        try:
            from calibre.gui2.tools import convert_single_ebook
        except ImportError as exc:
            raise BridgeMethodError(
                "UNSUPPORTED_BY_CALIBRE_VERSION",
                "Calibre's single-book conversion preparation API is unavailable",
            ) from exc
        return convert_single_ebook(
            self.gui,
            self._db(),
            [book_id],
            auto_conversion=True,
            out_format=output_format,
            show_no_format_warning=False,
        )

    @staticmethod
    def _conversion_options(options: Any) -> dict[str, Any]:
        if options is None:
            return {}
        if not isinstance(options, dict):
            raise BridgeMethodError("POLICY_DENIED", "options must be an object")
        allowed = {
            "base_font_size", "font_size_mapping", "line_height", "margin_top",
            "margin_right", "margin_bottom", "margin_left", "output_profile",
            "input_encoding", "remove_paragraph_spacing", "insert_blank_line",
            "chapter", "chapter_mark", "page_breaks_before", "pretty_print",
        }
        unknown = sorted(set(options) - allowed)
        if unknown:
            raise BridgeMethodError("POLICY_DENIED", f"Unsupported conversion options: {', '.join(unknown)}")
        if any(isinstance(value, (dict, list)) for value in options.values()):
            raise BridgeMethodError("POLICY_DENIED", "Conversion option values must be scalar")
        return dict(options)

    def _convert_book(self, params: dict[str, Any]) -> dict[str, Any]:
        book_id = int(params["book_id"])
        output_format = self._format_name(params.get("output_format"))
        replace_existing = bool(params.get("replace_existing", False))
        store_result = bool(params.get("store_result", True))
        export_path = None if store_result else self._allowed_export_path(params.get("export_path"), output_format)
        overwrite_export = bool(params.get("overwrite_export", False))
        options = self._conversion_options(params.get("options"))
        job_id = self._record_job("convert_book", params, "waiting_for_gui", "Preparing Calibre conversion job")
        temp_files = ()
        try:
            api = self._new_api()
            self._require_book(api, book_id)
            if export_path is not None and export_path.exists() and not overwrite_export:
                raise BridgeMethodError(
                    "DUPLICATE_REJECTED",
                    "The export destination exists; set overwrite_export=true explicitly",
                )
            existing = api.format(book_id, output_format) if store_result else None
            if store_result and existing is not None and not replace_existing:
                raise BridgeMethodError(
                    "DUPLICATE_REJECTED",
                    f"Book {book_id} already has format {output_format}; set replace_existing=true explicitly",
                )
            jobs, _changed, bad = self._prepare_conversion(book_id, output_format)
            if bad or not jobs:
                raise BridgeMethodError("FORMAT_NOT_FOUND", "Calibre found no supported source format for conversion")
            if len(jobs) != 1:
                raise BridgeMethodError("CALIBRE_JOB_FAILED", "Expected one Calibre conversion job")
            func, args, description, fmt, prepared_book_id, temp_files = jobs[0]
            temp_files = tuple(temp_files)
            if int(prepared_book_id) != book_id or str(fmt).upper() != output_format:
                raise BridgeMethodError("CALIBRE_JOB_FAILED", "Calibre prepared an unexpected conversion target")
            func, _, parts_value = func.partition(":")
            parts = set(parts_value.split(";")) if parts_value else set()
            args = list(args)
            if options:
                try:
                    from calibre.customize.conversion import OptionRecommendation
                    level = OptionRecommendation.HIGH
                except ImportError:
                    level = 3
                args[2] = list(args[2]) + [(name, value, level) for name, value in options.items()]
            input_format = Path(str(args[0])).suffix.lstrip(".")
            core_usage = 1
            try:
                from calibre.customize.ui import plugin_for_input_format
                plugin = plugin_for_input_format(input_format)
                if plugin is not None:
                    core_usage = plugin.core_usage
            except ImportError:
                pass

            callback = partial(self._conversion_done, job_id)
            try:
                from calibre.gui2 import Dispatcher
                callback = Dispatcher(callback)
            except ImportError:
                pass
            self._conversion_context[job_id] = {
                "book_id": book_id,
                "format": output_format,
                "replace_existing": replace_existing,
                "previous": existing,
                "temp_files": temp_files,
                "store_result": store_result,
                "export_path": export_path,
                "overwrite_export": overwrite_export,
            }
            native_job = self.gui.job_manager.run_job(
                callback,
                func,
                args=args,
                description=description,
                core_usage=core_usage,
            )
            native_job.conversion_of_same_fmt = "same_fmt" in parts
            snapshot = self._update_job(job_id, calibre_job_id=getattr(native_job, "id", None))
            if snapshot["status"] in {"completed", "failed", "cancelled", "rejected"}:
                return snapshot
            self.calibre_jobs[job_id] = native_job
            return self._update_job(
                job_id,
                status="queued",
                message="Queued in Calibre's JobManager",
            )
        except Exception as exc:
            self.calibre_jobs.pop(job_id, None)
            context = self._conversion_context.pop(job_id, None)
            self._cleanup_temp_files((context or {}).get("temp_files", temp_files))
            error = exc if isinstance(exc, BridgeMethodError) else BridgeMethodError(
                "CALIBRE_JOB_FAILED",
                f"Conversion was not queued: {exc}",
            )
            self._update_job(job_id, status="failed", message="Conversion was not queued", error=str(error))
            if error is exc:
                raise
            raise error from exc

    def _conversion_done(self, job_id: str, native_job) -> None:
        self.calibre_jobs.pop(job_id, None)
        context = self._conversion_context.pop(job_id, None)
        if context is None:
            return
        temp_files = context["temp_files"]
        try:
            if getattr(native_job, "killed", False):
                self._update_job(
                    job_id,
                    status="cancelled",
                    message="Calibre conversion job was cancelled",
                    error="JOB_CANCELLED: conversion cancelled",
                )
                return
            if getattr(native_job, "failed", False):
                details = str(getattr(native_job, "details", "") or "Calibre conversion worker failed")[-4000:]
                self._update_job(
                    job_id,
                    status="failed",
                    message="Calibre conversion job failed",
                    error=f"CALIBRE_JOB_FAILED: {details}",
                )
                return
            book_id = context["book_id"]
            output_format = context["format"]
            api = self._new_api()
            self._require_book(api, book_id)
            output_path = Path(temp_files[-1].name)
            if not output_path.is_file() or output_path.stat().st_size < 1:
                raise BridgeMethodError("CALIBRE_JOB_FAILED", "Conversion produced an empty output file")
            if context["store_result"]:
                try:
                    accepted = api.add_format(
                        book_id,
                        output_format,
                        str(output_path),
                        replace=context["replace_existing"],
                        run_hooks=False,
                        dbapi=self._db(),
                    )
                    if not accepted:
                        raise BridgeMethodError("DUPLICATE_REJECTED", "Calibre declined the converted format")
                except Exception as exc:
                    rollback_error = None
                    if context["previous"] is not None:
                        try:
                            api.add_format(
                                book_id,
                                output_format,
                                io.BytesIO(context["previous"]),
                                replace=True,
                                run_hooks=False,
                                dbapi=self._db(),
                            )
                        except Exception as rollback_exc:
                            rollback_error = rollback_exc
                    detail = f"Could not attach converted format: {exc}"
                    if rollback_error is not None:
                        detail += f"; rollback failed: {rollback_error}"
                    raise BridgeMethodError("CALIBRE_JOB_FAILED", detail) from exc
                try:
                    from calibre.customize.ui import run_plugins_on_postconvert
                    run_plugins_on_postconvert(self._db(), book_id, output_format)
                except ImportError:
                    pass
                signal = getattr(self.gui, "book_converted", None)
                if signal is not None and callable(getattr(signal, "emit", None)):
                    signal.emit(book_id, output_format)
                self._refresh_book(book_id)
                message = "Conversion completed and format attached"
                result = {"book_id": book_id, "format": output_format, "stored": True}
            else:
                destination = self._allowed_export_path(str(context["export_path"]), output_format)
                if destination.exists() and not context["overwrite_export"]:
                    raise BridgeMethodError("DUPLICATE_REJECTED", "The export destination appeared during conversion")
                temporary = destination.with_name(f".{destination.name}.umcp-{uuid.uuid4().hex}.tmp")
                try:
                    with output_path.open("rb") as source, temporary.open("xb") as target:
                        shutil.copyfileobj(source, target)
                        target.flush()
                        os.fsync(target.fileno())
                    if destination.exists() and not context["overwrite_export"]:
                        raise BridgeMethodError("DUPLICATE_REJECTED", "The export destination appeared during conversion")
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
                message = "Conversion completed and output exported"
                result = {
                    "book_id": book_id,
                    "format": output_format,
                    "stored": False,
                    "artifact": str(destination),
                }
            self._update_job(
                job_id,
                status="completed",
                progress=1.0,
                message=message,
                result=result,
                error=None,
            )
        except Exception as exc:
            self._update_job(
                job_id,
                status="failed",
                message="Conversion completion failed",
                error=str(exc),
            )
        finally:
            self._cleanup_temp_files(temp_files)

    @staticmethod
    def _cleanup_temp_files(temp_files) -> None:
        for item in temp_files or ():
            try:
                Path(item.name).unlink(missing_ok=True)
            except Exception:
                pass

    def _mark_native_job_cancel_requested(self, job_id: str, native_job) -> None:
        if getattr(native_job, "is_running", False):
            self._cancellation_requested.add(job_id)
            self._cancelled_before_start.discard(job_id)
        else:
            self._cancelled_before_start.add(job_id)
            self._cancellation_requested.discard(job_id)

    def _clear_cancellation_tracking(self, job_id: str) -> None:
        self._cancellation_requested.discard(job_id)
        self._cancelled_before_start.discard(job_id)

    def _sync_calibre_job(self, job_id: str) -> None:
        native_job = self.calibre_jobs.get(job_id)
        if native_job is None:
            return
        with self._records_lock:
            record = self.job_records.get(job_id)
            if record is None or record["status"] in {"completed", "failed", "cancelled", "rejected"}:
                return
        if getattr(native_job, "killed", False):
            method = record["method"]
            result = getattr(native_job, "result", None)
            exception = getattr(native_job, "exception", None)
            if method == "convert_book":
                conversion = self._conversion_context.pop(job_id, None)
                if conversion is not None:
                    self._cleanup_temp_files(conversion.get("temp_files", ()))
                self._update_job(
                    job_id,
                    status="cancelled",
                    message="Native Calibre job was cancelled",
                    error="JOB_CANCELLED: operation cancelled",
                )
                return
            if method == "add_book":
                if job_id in self._cancelled_before_start or result is not None or exception is not None:
                    self._book_import_done(job_id, native_job)
                else:
                    self._update_job(job_id, message="Cancellation requested; waiting for metadata extraction to stop before any library change")
                return
            if method in {"copy_books_to_library", "move_books_to_library"}:
                if job_id in self._cancelled_before_start or result is not None or exception is not None:
                    self._copy_library_done(job_id, native_job)
                else:
                    self._update_job(job_id, message="Cancellation requested; waiting for the copy worker to stop between books")
                return
            if method == "save_book_to_disk":
                if job_id in self._cancelled_before_start or result is not None or exception is not None:
                    self._save_disk_done(job_id, native_job)
                else:
                    self._update_job(job_id, message="Cancellation requested; waiting for save-to-disk to reach a safe publication boundary")
                return
            if method == "email_book":
                if job_id in self._cancelled_before_start or result is not None or exception is not None:
                    self._email_done(job_id, native_job)
                else:
                    self._update_job(job_id, message="Cancellation requested; waiting for SMTP submission to stop cleanly")
                return
            self._update_job(
                job_id,
                status="cancelled",
                message="Native Calibre job was cancelled",
                error="JOB_CANCELLED: operation cancelled",
            )
            return
        progress = max(0.0, min(float(getattr(native_job, "percent", 0) or 0) / 100.0, 1.0))
        status = "running" if getattr(native_job, "is_running", False) else "queued"
        message = str(getattr(native_job, "status_text", "") or record["message"])
        self._update_job(job_id, status=status, progress=progress, message=message)

    def _cancel_job(self, job_id: str) -> dict[str, Any]:
        self._sync_calibre_job(job_id)
        record = self._get_job_status(job_id)
        if record["status"] in {"completed", "failed", "cancelled", "rejected"}:
            return record
        native_job = self.calibre_jobs.get(job_id)
        if native_job is None:
            raise BridgeMethodError("JOB_CANCELLED", "The operation cannot be interrupted once its GUI mutation starts")
        manager = getattr(self.gui, "job_manager", None)
        killer = getattr(manager, "_kill_job", None)
        if not callable(killer) or not getattr(native_job, "killable", True):
            raise BridgeMethodError("JOB_CANCELLED", "The native Calibre job cannot be cancelled safely")
        self._mark_native_job_cancel_requested(job_id, native_job)
        killer(native_job)
        self._sync_calibre_job(job_id)
        updated = self._get_job_status(job_id)
        if updated["status"] in {"completed", "failed", "cancelled", "rejected"}:
            return updated
        if str(updated.get("message") or "").startswith("Cancellation requested;"):
            return updated
        return self._update_job(job_id, message="Cancellation requested; final status will reflect the next safe Calibre boundary")

    def _list_jobs(self) -> list[dict[str, Any]]:
        with self._records_lock:
            job_ids = tuple(self.job_records)
        for job_id in job_ids:
            self._sync_calibre_job(job_id)
        with self._records_lock:
            return sorted((dict(item) for item in self.job_records.values()), key=lambda item: item["created_at"])

    def _get_job_status(self, job_id: str) -> dict[str, Any]:
        self._sync_calibre_job(job_id)
        with self._records_lock:
            try:
                return dict(self.job_records[job_id])
            except KeyError as exc:
                raise BridgeMethodError("JOB_NOT_FOUND", f"Unknown calibre-umcp job id: {job_id}") from exc

    def _reject_legacy_singular_mutation(self, method: str, params: dict[str, Any]) -> Any:
        """Fail closed for obsolete singular copy/move bridge methods.

        The released MCP surface uses the plural operations because their preview,
        destination-id mapping, batch cancellation and partial-copy results are part
        of the safety contract. Silently translating an old singular request would
        bypass that contract.
        """
        job_id = self._record_job(method, params, "rejected", "Obsolete singular copy/move method rejected")
        raise BridgeMethodError(
            "UNSUPPORTED_BY_CALIBRE_VERSION",
            f"{method} is obsolete; use the verified plural Calibre 9.12 mutation (job_id={job_id})",
        )

    def _record_job(self, method: str, params: dict[str, Any], status: str, message: str) -> str:
        job_id = f"calibre-umcp:{uuid.uuid4().hex}"
        now = time.time()
        record = {
            "id": job_id,
            "method": method,
            "status": status,
            "message": message,
            "created_at": now,
            "started_at": now if status == "running" else None,
            "completed_at": now if status in {"completed", "failed", "cancelled", "rejected"} else None,
            "progress": 1.0 if status == "completed" else 0.0,
            "calibre_job_id": None,
            "library_path": Path(self._library_path()).name,
            "params": self._safe_params(params),
            "result": None,
            "error": None,
        }
        stale_contexts: list[dict[str, Any]] = []
        with self._records_lock:
            self.job_records[job_id] = record
            while len(self.job_records) > self.audit_retention:
                terminal = [
                    key for key, item in self.job_records.items()
                    if item["status"] in {"completed", "failed", "cancelled", "rejected"}
                ]
                if not terminal:
                    break
                oldest = min(terminal, key=lambda key: self.job_records[key]["created_at"])
                self.job_records.pop(oldest, None)
                self.calibre_jobs.pop(oldest, None)
                context = self._conversion_context.pop(oldest, None)
                self._import_context.pop(oldest, None)
                self._copy_context.pop(oldest, None)
                self._save_context.pop(oldest, None)
                self._email_context.pop(oldest, None)
                self._clear_cancellation_tracking(oldest)
                if context is not None:
                    stale_contexts.append(context)
        for context in stale_contexts:
            self._cleanup_temp_files(context.get("temp_files", ()))
        self._append_audit(record)
        return job_id

    def _update_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
        terminal = False
        with self._records_lock:
            try:
                record = self.job_records[job_id]
            except KeyError as exc:
                raise BridgeMethodError("JOB_NOT_FOUND", f"Unknown calibre-umcp job id: {job_id}") from exc
            record.update(changes)
            if changes.get("status") == "running" and record.get("started_at") is None:
                record["started_at"] = time.time()
            if changes.get("status") in {"completed", "failed", "cancelled", "rejected"}:
                record["completed_at"] = time.time()
                terminal = True
            snapshot = dict(record)
        if terminal:
            self.calibre_jobs.pop(job_id, None)
            self._clear_cancellation_tracking(job_id)
        self._append_audit(snapshot)
        return snapshot

    def _append_audit(self, record: dict[str, Any]) -> None:
        if self.audit_path:
            source_paths = tuple(sorted({
                value
                for value in (
                    self._library_path(),
                    *(str(path) for path in self.import_roots),
                    *(str(path) for path in self.export_roots),
                    *(str(path) for path in self.destination_libraries),
                )
                if value
            }, key=len, reverse=True))

            def redact(value):
                if isinstance(value, dict):
                    return {key: redact(item) for key, item in value.items()}
                if isinstance(value, (list, tuple, set)):
                    return [redact(item) for item in value]
                if isinstance(value, os.PathLike):
                    value = os.fspath(value)
                if isinstance(value, str):
                    for source_path in source_paths:
                        value = value.replace(source_path, f"<source:{Path(source_path).name}>")
                    return value
                return value

            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(redact(record), sort_keys=True) + "\n")

    def _safe_params(self, params: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(params)
        for key in ("token", "password", "secret"):
            if key in redacted:
                redacted[key] = "<redacted>"
        if isinstance(redacted.get("changes"), dict):
            redacted["changes"] = {"fields": sorted(redacted["changes"])}
        for key in ("path", "export_path", "destination_library", "destination_directory"):
            if redacted.get(key):
                redacted[key] = Path(str(redacted[key])).name
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
                self._write_json(401, {"jsonrpc": "2.0", "id": None, "error": {"message": "missing or invalid bearer token"}})
                return
            request_id = None
            try:
                length = int(self.headers.get("content-length") or "0")
                payload = json.loads(self.rfile.read(length).decode())
                if not isinstance(payload, dict):
                    raise BridgeMethodError("INVALID_REQUEST", "JSON-RPC request must be an object")
                request_id = payload.get("id")
                method = payload.get("method")
                params = payload.get("params", {})
                if params is None:
                    params = {}
                if not isinstance(method, str) or not method:
                    raise BridgeMethodError("INVALID_REQUEST", "JSON-RPC method must be a non-empty string")
                if not isinstance(params, dict):
                    raise BridgeMethodError("INVALID_REQUEST", "JSON-RPC params must be an object")
                result = bridge.call_serialized(method, params)
                body = {"jsonrpc": "2.0", "id": request_id, "result": result}
                self._write_json(200, body)
            except BridgeMethodError as exc:
                self._write_json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32000, "message": exc.message, "data": {"code": exc.code}},
                    },
                )
            except Exception as exc:
                self._write_json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32603,
                            "message": "Calibre bridge operation failed",
                            "data": {"code": "CALIBRE_OPERATION_FAILED", "detail": str(exc)[-4000:]},
                        },
                    },
                )

        def _write_json(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            return

    class BridgeHTTPServer(ThreadingHTTPServer):
        daemon_threads = True

        def server_close(self):
            try:
                super().server_close()
            finally:
                bridge.close()

    server = BridgeHTTPServer((host, port), Handler)
    server.bridge = bridge  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, name="calibre-umcp-http", daemon=True)
    thread.start()
    server.thread = thread  # type: ignore[attr-defined]
    return server
