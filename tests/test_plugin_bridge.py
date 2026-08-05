import copy
import io
import json
import os
import queue
import smtplib
import socketserver
import sys
import tempfile
import threading
import types
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from plugins.calibre_umcp_plugin.bridge import BRIDGE_VERSION, BridgeMethodError, CalibreRpcBridge


class FakeMetadata:
    def __init__(self, title, authors, identifiers=None, tags=None):
        self.title = title
        self.authors = authors
        self.identifiers = identifiers or {}
        self.tags = tags or []
        self.series = None
        self.series_index = None
        self.publisher = None
        self.languages = []
        self.comments = None
        self.rating = None
        self.pubdate = None
        self.timestamp = None
        self.custom = {}

    def set(self, field, value):
        if field.startswith("#"):
            self.custom[field] = value
        else:
            setattr(self, field, value)

    def set_identifiers(self, value):
        self.identifiers = dict(value)

    def set_user_metadata(self, key, metadata):
        return None


class FakeNewApi:
    def __init__(self, db):
        self.db = db
        self.format_data = {
            1: {"EPUB": b"old epub", "PDF": b"old pdf"},
            2: {"EPUB": b"second epub"},
            3: {"EPUB": b"third epub"},
        }
        self.field_metadata = {
            "#genre": {"is_custom": True, "datatype": "text"},
            "#computed": {"is_custom": True, "datatype": "composite"},
        }
        self.fail_set_once = False
        self.fail_add_once = False
        self.fail_remove_once = False
        self.fail_remove_books_once = False
        self.cover_data = {1: b"old cover", 2: b"source cover"}

    def all_book_ids(self):
        return frozenset(self.db.rows)

    def has_id(self, book_id):
        return book_id in self.db.rows

    def get_metadata(self, book_id, **kwargs):
        return copy.deepcopy(self.db.rows[book_id])

    def set_metadata(self, book_id, metadata, **kwargs):
        self.db.rows[book_id] = copy.deepcopy(metadata)
        if self.fail_set_once:
            self.fail_set_once = False
            raise OSError("simulated metadata write failure")
        return {book_id}

    def format(self, book_id, fmt):
        return self.format_data.get(book_id, {}).get(fmt.upper())

    def has_format(self, book_id, fmt):
        return fmt.upper() in self.format_data.get(book_id, {})

    def add_format(self, book_id, fmt, source, replace=True, **kwargs):
        fmt = fmt.upper()
        formats = self.format_data.setdefault(book_id, {})
        if fmt in formats and not replace:
            return False
        if hasattr(source, "read"):
            data = source.read()
        else:
            data = Path(source).read_bytes()
        formats[fmt] = data
        if self.fail_add_once:
            self.fail_add_once = False
            raise OSError("simulated format write failure")
        return True

    def remove_formats(self, mapping, db_only=False):
        for book_id, formats in mapping.items():
            for fmt in formats:
                self.format_data.setdefault(book_id, {}).pop(fmt.upper(), None)
        if self.fail_remove_once:
            self.fail_remove_once = False
            raise OSError("simulated remove failure")
        return {book_id: set(formats) for book_id, formats in mapping.items()}

    def cover(self, book_id):
        return self.cover_data.get(book_id)

    def set_cover(self, mapping):
        for book_id, data in mapping.items():
            if data is None:
                self.cover_data.pop(book_id, None)
            else:
                self.cover_data[book_id] = data

    def remove_books(self, book_ids, permanent=False):
        if self.fail_remove_books_once:
            self.fail_remove_books_once = False
            raise OSError("simulated trash failure")
        for book_id in book_ids:
            self.db.rows.pop(book_id, None)
            self.format_data.pop(book_id, None)
            self.cover_data.pop(book_id, None)

    def merge_book_metadata(self, destination_id, source_ids, replace_cover=False, save_alternate_cover=False):
        destination = self.db.rows[destination_id]
        for source_id in source_ids:
            source = self.db.rows[source_id]
            destination.tags = sorted(set(destination.tags).union(source.tags))
            destination.identifiers.update(source.identifiers)
            if replace_cover and source_id in self.cover_data:
                self.cover_data[destination_id] = self.cover_data[source_id]

    def add_books(self, books, add_duplicates=True, **kwargs):
        ids, duplicates = [], []
        for metadata, format_map in books:
            duplicate = any(
                row.title == metadata.title and row.authors == metadata.authors
                for row in self.db.rows.values()
            )
            if duplicate and not add_duplicates:
                duplicates.append((metadata, format_map))
                continue
            book_id = max(self.db.rows, default=0) + 1
            self.db.rows[book_id] = copy.deepcopy(metadata)
            self.format_data[book_id] = {
                fmt.upper(): Path(path).read_bytes() for fmt, path in format_map.items()
            }
            ids.append(book_id)
        return ids, duplicates


class FakeDb:
    library_path = "/books"
    library_id = "fake-library"

    def __init__(self, library_path="/books", library_id="fake-library"):
        self.library_path = library_path
        self.library_id = library_id
        self.rows = {
            1: FakeMetadata("Example", ["Author"], {"isbn": "1"}),
            2: FakeMetadata("Example", ["Author"], {"isbn": "1"}),
            3: FakeMetadata("Other", ["Someone"]),
        }
        self.new_api = FakeNewApi(self)
        self.format_paths = {}

    def all_book_ids(self):
        return list(self.rows)

    def search_getting_ids(self, query, _):
        return [book_id for book_id, meta in self.rows.items() if query.casefold() in meta.title.casefold()]

    def get_metadata(self, book_id, index_is_id=True):
        self.assert_index_is_id(index_is_id)
        return self.rows[book_id]

    def formats(self, book_id, index_is_id=True):
        self.assert_index_is_id(index_is_id)
        return ",".join(self.new_api.format_data.get(book_id, {}))

    def format_abspath(self, book_id, fmt, index_is_id=True):
        self.assert_index_is_id(index_is_id)
        return self.format_paths.get((book_id, fmt.upper()))

    @staticmethod
    def assert_index_is_id(index_is_id):
        if index_is_id is not True:
            raise AssertionError("index_is_id must be True")


class FakeIndex:
    def row(self):
        return 0


class FakeModel:
    def __init__(self):
        self.refreshed = []
        self.deleted = []
        self.delete_refreshes = 0
        self.added = []

    def refresh_ids(self, ids, current_row=-1):
        self.refreshed.append((tuple(ids), current_row))

    def ids_deleted(self, ids):
        self.deleted.append(tuple(ids))
        self.books_deleted()

    def books_deleted(self):
        self.delete_refreshes += 1

    def books_added(self, count):
        self.added.append(count)


class FakeView:
    def __init__(self):
        self._model = FakeModel()

    def model(self):
        return self._model

    def currentIndex(self):
        return FakeIndex()


class FakeNativeJob:
    def __init__(self, job_id, callback, description):
        self.id = job_id
        self.callback = callback
        self.description = description
        self.failed = False
        self.killed = False
        self.killable = True
        self.percent = 0
        self.status_text = "Waiting"
        self.is_running = False
        self.duration = None
        self.details = ""


class FakeThreadedJob(FakeNativeJob):
    def __init__(self, type_name, description, func, args, callback):
        super().__init__(100, callback, description)
        self.type = type_name
        self.func = func
        self.args = args
        self.result = None
        self.exception = None
        self.abort = threading.Event()

    def execute(self):
        self.is_running = True
        try:
            self.result = self.func(
                *self.args,
                abort=self.abort,
                log=lambda *args: None,
                notifications=queue.Queue(),
            )
        except Exception as exc:
            self.failed = True
            self.exception = exc
            self.details = str(exc)
        self.is_running = False
        self.duration = 1
        self.callback(self)


class FakeJobManager:
    def __init__(self):
        self.jobs = []

    def run_job(self, callback, name, args=None, description="", core_usage=1, **kwargs):
        job = FakeNativeJob(len(self.jobs) + 1, callback, description)
        job.name = name
        job.args = args or []
        job.core_usage = core_usage
        self.jobs.append(job)
        return job

    def run_threaded_job(self, job):
        self.jobs.append(job)

    def _kill_job(self, job):
        job.killed = True
        job.duration = 0
        job.callback(job)


class FakeGui:
    def __init__(self, job_manager=None):
        self.current_db = FakeDb()
        self.library_view = FakeView()
        self.job_manager = job_manager or FakeJobManager()


class CalibreRpcBridgeTests(unittest.TestCase):
    @staticmethod
    def write_epub(path: Path, title="Example", author="Author", cover=True, toc=True, body=None, identifier="urn:isbn:9780000000002", unsafe_xml=False):
        body = body or ("Chapter text. " * 600)
        doctype = '<!DOCTYPE package [<!ENTITY unsafe "blocked">]>' if unsafe_xml else ""
        manifest_extra = '<item id="cover" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>' if cover else ""
        nav_item = '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>' if toc else ""
        package = f'''<?xml version="1.0" encoding="UTF-8"?>{doctype}
        <package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0">
          <metadata><dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>{f'<dc:identifier>{identifier}</dc:identifier>' if identifier else ''}</metadata>
          <manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>{manifest_extra}{nav_item}</manifest>
          <spine><itemref idref="chapter"/></spine>
        </package>'''
        container = '''<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'''
        chapter = f'<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Chapter</h1><p>{body}</p></body></html>'
        nav = '<html xmlns="http://www.w3.org/1999/xhtml"><body><nav><ol><li><a href="chapter.xhtml">Chapter</a></li></ol></nav></body></html>'
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            archive.writestr("META-INF/container.xml", container)
            archive.writestr("EPUB/package.opf", package)
            archive.writestr("EPUB/chapter.xhtml", chapter)
            if cover:
                archive.writestr("EPUB/cover.jpg", b"image")
            if toc:
                archive.writestr("EPUB/nav.xhtml", nav)

    @staticmethod
    def threaded_job_factory(type_name, description, func, args, callback):
        return FakeThreadedJob(type_name, description, func, args, callback)

    @staticmethod
    def copy_adapter(source_path, destination_path, book_ids, duplicate_policy, destination_book_ids, **kwargs):
        return {
            "ok": True,
            "copied": [
                {
                    "source_book_id": book_id,
                    "destination_book_id": 1000 + book_id,
                    "action": "add",
                    "formats": ["EPUB"],
                }
                for book_id in book_ids
            ],
        }

    @staticmethod
    def save_adapter(source_path, book_id, destination_directory, options, overwrite, **kwargs):
        artifact = str(Path(destination_directory) / f"book-{book_id}.epub")
        return {
            "ok": True,
            "book_id": book_id,
            "destination_directory": destination_directory,
            "artifacts": [artifact],
            "artifact_count": 1,
        }

    @staticmethod
    def email_send_adapter(source_path, book_id, fmt, recipient, subject, text, attachment_name, **kwargs):
        return {
            "ok": True,
            "book_id": book_id,
            "format": fmt,
            "recipient": recipient,
            "smtp_accepted": True,
            "delivery_confirmed": False,
        }

    def conversion_adapter(self, _gui, _db, book_id, output_format):
        input_file = tempfile.NamedTemporaryFile(suffix=".epub", delete=False)
        input_file.write(b"source")
        input_file.close()
        output_file = tempfile.NamedTemporaryFile(suffix="." + output_format.lower(), delete=False)
        output_file.close()
        files = (SimpleNamespace(name=input_file.name), SimpleNamespace(name=output_file.name))
        job = (
            "gui_convert_override",
            [input_file.name, output_file.name, []],
            f"Convert book {book_id}",
            output_format,
            book_id,
            files,
        )
        return [job], True, []

    def test_bridge_ping_reports_version_and_current_library(self):
        bridge = CalibreRpcBridge(FakeGui())
        self.assertEqual(bridge.dispatch("ping", {}), {"ok": True, "version": BRIDGE_VERSION, "library_path": "/books"})

    def test_bridge_searches_current_library(self):
        bridge = CalibreRpcBridge(FakeGui())
        result = bridge.dispatch("search_books", {"query": "Example", "limit": 10})
        self.assertEqual([row["id"] for row in result["items"]], [1, 2])
        self.assertEqual(result["library"], "current")
        self.assertNotIn("library_path", result["items"][0])
        self.assertEqual(result["items"][0]["formats"], ["EPUB", "PDF"])

    def test_format_inventory_and_bounded_epub_inspection_are_path_and_content_safe(self):
        gui = FakeGui()
        bridge = CalibreRpcBridge(gui)
        with tempfile.TemporaryDirectory() as root:
            epub = Path(root) / "private-title.epub"
            self.write_epub(epub)
            gui.current_db.format_paths[(1, "EPUB")] = str(epub)
            formats = bridge.dispatch("get_book_formats", {"book_id": 1})
            self.assertEqual(formats["formats"][0]["format"], "EPUB")
            self.assertGreater(formats["formats"][0]["size_bytes"], 0)
            self.assertNotIn(str(epub), json.dumps(formats))
            inspected = bridge.dispatch("inspect_book_format", {"book_id": 1, "format": "epub"})
            self.assertTrue(inspected["container"]["valid"])
            self.assertTrue(inspected["structure"]["has_cover"])
            self.assertTrue(inspected["structure"]["has_toc"])
            self.assertEqual(inspected["structure"]["toc_entries"], 1)
            self.assertEqual(inspected["metadata"]["record_metadata_match"], "full")
            self.assertGreater(inspected["content_signals"]["estimated_text_chars"], 5000)
            self.assertFalse(inspected["limits"]["text_sample_included"])
            payload = json.dumps(inspected)
            self.assertNotIn(str(epub), payload)
            self.assertNotIn("Chapter text", payload)

    def test_quality_assessment_and_comparison_are_explainable(self):
        gui = FakeGui()
        bridge = CalibreRpcBridge(gui)
        with tempfile.TemporaryDirectory() as root:
            good = Path(root) / "good.epub"
            poor = Path(root) / "poor.epub"
            self.write_epub(good)
            self.write_epub(poor, title="Other", author="Someone", cover=False, toc=False, body="Short")
            gui.current_db.format_paths[(1, "EPUB")] = str(good)
            gui.current_db.format_paths[(3, "EPUB")] = str(poor)
            assessed = bridge.dispatch("assess_book_quality", {"book_id": 1, "formats": ["EPUB"]})
            self.assertGreaterEqual(assessed["score"], 75)
            self.assertEqual(assessed["best_format"], "EPUB")
            self.assertIn("container_valid", assessed["format_scores"][0]["reasons"])
            self.assertIn("identifier:isbn", assessed["format_scores"][0]["reasons"])
            self.write_epub(good, identifier="")
            without_embedded_id = bridge.dispatch("assess_book_quality", {"book_id": 1, "formats": ["EPUB"]})
            self.assertNotIn("identifier:isbn", without_embedded_id["format_scores"][0]["reasons"])
            self.write_epub(good)
            compared = bridge.dispatch("compare_book_quality", {
                "left": {"book_id": 3, "formats": ["EPUB"]},
                "right": {"book_id": 1, "formats": ["EPUB"]},
            })
            self.assertEqual(compared["recommendation"]["keep"], "right")
            self.assertIn("right_has_higher_quality_score", compared["recommendation"]["reasons"])

    def test_quality_assessment_and_comparison_degrade_on_safe_inspection_failure(self):
        gui = FakeGui()
        bridge = CalibreRpcBridge(gui)
        with tempfile.TemporaryDirectory() as root:
            malformed = Path(root) / "malformed.epub"
            valid = Path(root) / "valid.epub"
            self.write_epub(malformed, unsafe_xml=True)
            self.write_epub(valid, title="Other", author="Someone")
            gui.current_db.format_paths[(1, "EPUB")] = str(malformed)
            gui.current_db.format_paths[(3, "EPUB")] = str(valid)
            assessed = bridge.dispatch("assess_book_quality", {"book_id": 1, "formats": ["EPUB"]})
            self.assertEqual(assessed["grade"], "unknown")
            self.assertEqual(assessed["inspection_errors"], [{
                "format": "EPUB",
                "code": "FORMAT_READ_FAILED",
                "reason": "UNSAFE_XML_DECLARATION",
            }])
            self.assertIn("format_read_failed", assessed["format_scores"][0]["warnings"])
            self.assertIn("inspection_failed", assessed["format_scores"][0]["warnings"])
            compared = bridge.dispatch("compare_book_quality", {
                "left": {"book_id": 1, "formats": ["EPUB"]},
                "right": {"book_id": 3, "formats": ["EPUB"]},
            })
            self.assertEqual(compared["recommendation"]["keep"], "right")
            self.assertEqual(compared["recommendation"]["confidence"], "low")
            self.assertIn("left_candidate_inspection_failed", compared["recommendation"]["reasons"])
            self.assertEqual(compared["left"]["inspection_errors"][0]["reason"], "UNSAFE_XML_DECLARATION")

    def test_format_inspection_has_stable_failures_and_limits(self):
        gui = FakeGui()
        bridge = CalibreRpcBridge(gui)
        with self.assertRaises(BridgeMethodError) as unsupported:
            bridge.dispatch("inspect_book_format", {"book_id": 1, "format": "PDF"})
        self.assertEqual(unsupported.exception.code, "FORMAT_UNSUPPORTED")
        with self.assertRaises(BridgeMethodError) as missing:
            bridge.dispatch("inspect_book_format", {"book_id": 1, "format": "EPUB"})
        self.assertEqual(missing.exception.code, "FORMAT_NOT_FOUND")
        with self.assertRaises(BridgeMethodError) as sample:
            bridge.dispatch("inspect_book_format", {"book_id": 1, "format": "EPUB", "include_text_sample": True})
        self.assertEqual(sample.exception.code, "POLICY_DENIED")
        with tempfile.TemporaryDirectory() as root:
            oversized = Path(root) / "oversized.epub"
            with oversized.open("wb") as stream:
                stream.truncate(64 * 1024 * 1024 + 1)
            gui.current_db.format_paths[(1, "EPUB")] = str(oversized)
            with self.assertRaises(BridgeMethodError) as limited:
                bridge.dispatch("inspect_book_format", {"book_id": 1, "format": "EPUB"})
            self.assertEqual(limited.exception.code, "INSPECTION_LIMIT_EXCEEDED")
            valid = Path(root) / "valid.epub"
            self.write_epub(valid)
            gui.current_db.format_paths[(1, "EPUB")] = str(valid)
            with patch.object(queue.Queue, "get", side_effect=queue.Empty):
                with self.assertRaises(BridgeMethodError) as timed_out:
                    bridge.dispatch("inspect_book_format", {"book_id": 1, "format": "EPUB"})
            self.assertEqual(timed_out.exception.code, "INSPECTION_TIMEOUT")

    def test_content_server_url_is_exposed_only_when_running_and_authenticated(self):
        gui = FakeGui()
        bridge = CalibreRpcBridge(gui)
        stopped = bridge.dispatch("content_server_status", {})
        self.assertIsNone(stopped["base_url"])
        self.assertEqual(stopped["reason_code"], "CONTENT_SERVER_NOT_RUNNING")
        gui.content_server = type(
            "Server",
            (),
            {
                "current_thread": object(),
                "is_running": True,
                "loop": type("Loop", (), {"bound_address": ("127.0.0.1", 8080)})(),
                "opts": type("Options", (), {"auth": True, "ssl_certfile": None, "url_prefix": "books"})(),
            },
        )()
        status = bridge.dispatch("content_server_status", {})
        self.assertEqual(status["base_url"], "http://127.0.0.1:8080/books")
        self.assertIsNone(status["reason_code"])
        self.assertFalse(status["temporary_links_supported"])
        gui.content_server.opts.auth = False
        unauthenticated = bridge.dispatch("content_server_status", {})
        self.assertIsNone(unauthenticated["base_url"])
        self.assertEqual(unauthenticated["reason_code"], "CONTENT_SERVER_AUTH_DISABLED")
        gui.content_server.opts.auth = True
        gui.content_server.loop.bound_address = ("0.0.0.0", 8080)
        wildcard = bridge.dispatch("content_server_status", {})
        self.assertIsNone(wildcard["base_url"])
        self.assertEqual(wildcard["reason_code"], "ADVERTISED_CONTENT_SERVER_HOST_NOT_CONFIGURED")
        advertised = CalibreRpcBridge(gui, content_server_advertised_host="books.example.test").dispatch("content_server_status", {})
        self.assertEqual(advertised["base_url"], "http://books.example.test:8080/books")
        self.assertIsNone(advertised["reason_code"])
        gui.content_server.is_running = False
        self.assertFalse(bridge.dispatch("content_server_status", {})["running"])

    def test_bridge_finds_duplicates(self):
        bridge = CalibreRpcBridge(FakeGui())
        result = bridge.dispatch("find_duplicates", {"limit": 10})
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["count"], 2)
        self.assertIn("identifier:isbn", result["items"][0]["reasons"])

    def test_duplicate_enumeration_uses_calibre_new_api(self):
        db = FakeDb()
        db.all_book_ids = None
        result = CalibreRpcBridge(SimpleNamespace(current_db=db)).dispatch("find_duplicates", {"limit": 10})
        self.assertEqual(result["scanned"], 3)

    def test_library_registry_supports_redacted_inactive_reads_and_cross_library_matches(self):
        with tempfile.TemporaryDirectory() as root:
            incoming_path = Path(root) / "Incoming"
            main_path = Path(root) / "Main"
            for path in (incoming_path, main_path):
                path.mkdir()
                (path / "metadata.db").touch()
            incoming = FakeDb(str(incoming_path), "incoming-id")
            main = FakeDb(str(main_path), "main-id")
            main.rows[1] = FakeMetadata("Different", ["Writer"], {"isbn": "1"})
            main.rows[2] = FakeMetadata("Example", ["Author"], {})
            broker = SimpleNamespace(get_library=lambda path: main if Path(path) == main_path else None)
            gui = SimpleNamespace(current_db=incoming, library_broker=broker, job_manager=FakeJobManager())
            registry = (
                {"alias": "incoming", "label": "Incoming", "path": str(incoming_path), "read": True, "switch": True, "copy_destination": False, "library_id": "incoming-id"},
                {"alias": "main", "label": "Main", "path": str(main_path), "read": True, "switch": True, "copy_destination": True, "library_id": "main-id"},
            )
            bridge = CalibreRpcBridge(gui, library_registry=registry)
            listed = bridge.dispatch("list_libraries", {})
            self.assertEqual(listed["active_library"], "incoming")
            self.assertTrue(listed["cross_library_configured"])
            self.assertTrue(listed["cross_library_available"])
            self.assertEqual(listed["readable_target_count"], 1)
            self.assertIsNone(listed["cross_library_reason_code"])
            self.assertNotIn(str(main_path), json.dumps(listed))
            searched = bridge.dispatch("search_books", {"library": "main", "query": "Example", "limit": 10})
            self.assertEqual(searched["library"], "main")
            self.assertEqual(searched["items"][0]["library"], "main")
            self.assertIs(gui.current_db, incoming)
            compared = bridge.dispatch("find_cross_library_duplicates", {"source_library": "incoming", "target_libraries": ["main"], "limit": 10})
            self.assertTrue(compared["matches"])
            reasons = {reason for match in compared["matches"] for candidate in match["candidates"] for reason in candidate["reasons"]}
            self.assertIn("identifier:isbn", reasons)
            self.assertIn("title_authors", reasons)
            with self.assertRaises(BridgeMethodError) as empty_targets:
                bridge.dispatch("find_cross_library_duplicates", {"source_library": "incoming", "target_libraries": []})
            self.assertEqual(empty_targets.exception.code, "CROSS_LIBRARY_TARGETS_REQUIRED")
            with self.assertRaises(BridgeMethodError) as same_library:
                bridge.dispatch("find_cross_library_duplicates", {"source_library": "incoming", "target_libraries": ["incoming"]})
            self.assertEqual(same_library.exception.code, "CROSS_LIBRARY_SAME_LIBRARY")
            with self.assertRaises(BridgeMethodError) as unknown:
                bridge.dispatch("find_cross_library_duplicates", {"source_library": "incoming", "target_libraries": ["missing"]})
            self.assertEqual(unknown.exception.code, "LIBRARY_ALIAS_UNKNOWN")

    def test_cross_library_availability_distinguishes_unconfigured_unavailable_and_unreadable(self):
        bridge = CalibreRpcBridge(FakeGui())
        status = bridge.dispatch("list_libraries", {})
        self.assertFalse(status["cross_library_configured"])
        self.assertFalse(status["cross_library_available"])
        self.assertEqual(status["cross_library_reason_code"], "NO_TARGET_LIBRARIES_CONFIGURED")
        with tempfile.TemporaryDirectory() as root:
            active_path = Path(root) / "active"
            missing_path = Path(root) / "missing"
            denied_path = Path(root) / "denied"
            active_path.mkdir()
            denied_path.mkdir()
            (active_path / "metadata.db").touch()
            (denied_path / "metadata.db").touch()
            gui = SimpleNamespace(current_db=FakeDb(str(active_path), "active-id"))
            registry = (
                {"alias": "active", "path": str(active_path), "read": True},
                {"alias": "missing", "path": str(missing_path), "read": True},
                {"alias": "denied", "path": str(denied_path), "read": False},
            )
            configured = CalibreRpcBridge(gui, library_registry=registry).dispatch("list_libraries", {})
            self.assertTrue(configured["cross_library_configured"])
            self.assertFalse(configured["cross_library_available"])
            self.assertEqual(configured["readable_target_count"], 0)
            self.assertEqual(configured["cross_library_reason_code"], "NO_TARGET_LIBRARIES_AVAILABLE")

    def test_switch_library_is_explicit_guarded_and_increments_generation(self):
        with tempfile.TemporaryDirectory() as root:
            incoming_path = Path(root) / "Incoming"
            main_path = Path(root) / "Main"
            for path in (incoming_path, main_path):
                path.mkdir()
                (path / "metadata.db").touch()
            incoming = FakeDb(str(incoming_path), "incoming-id")
            main = FakeDb(str(main_path), "main-id")
            gui = SimpleNamespace(current_db=incoming, job_manager=FakeJobManager(), proceed_question=SimpleNamespace(questions=[]))
            def switch(path, allow_rebuild=False):
                self.assertFalse(allow_rebuild)
                gui.current_db = main if Path(path) == main_path else incoming
            gui.library_moved = switch
            registry = (
                {"alias": "incoming", "label": "Incoming", "path": str(incoming_path), "read": True, "switch": True},
                {"alias": "main", "label": "Main", "path": str(main_path), "read": True, "switch": True},
            )
            bridge = CalibreRpcBridge(gui, library_registry=registry, library_switching_enabled=True)
            switched = bridge.dispatch("switch_library", {"library": "main", "expected_active_library": "incoming", "expected_active_generation": 0, "confirmation": "SWITCH_LIBRARY:main"})
            self.assertEqual(switched, {"active_library": "main", "active_generation": 1, "switched": True})
            with self.assertRaises(BridgeMethodError) as caught:
                bridge.dispatch("update_book_metadata", {"book_id": 1, "changes": {}, "expected_active_library": "incoming"})
            self.assertEqual(caught.exception.code, "ACTIVE_LIBRARY_MISMATCH")

    def test_three_library_runtime_integration_keeps_aliases_and_active_state_isolated(self):
        with tempfile.TemporaryDirectory() as root:
            paths = {
                alias: Path(root) / parent / "Library"
                for alias, parent in (("incoming", "one"), ("main", "two"), ("archive", "three"))
            }
            for path in paths.values():
                path.mkdir(parents=True)
                (path / "metadata.db").touch()
            databases = {
                alias: FakeDb(str(path), f"{alias}-id")
                for alias, path in paths.items()
            }
            databases["incoming"].rows = {
                1: FakeMetadata("Shared ISBN", ["Author A"], {"isbn": "shared"}),
                2: FakeMetadata("Shared Title", ["Author B"]),
            }
            databases["main"].rows = {
                1: FakeMetadata("Other title", ["Author A"], {"isbn": "shared"}),
                2: FakeMetadata("Shared Title", ["Author B"]),
            }
            databases["archive"].rows = {
                1: FakeMetadata("Shared ISBN", ["Author A"], {"isbn": "different"}),
                2: FakeMetadata("Unique", ["Author C"]),
            }
            active_jobs = {"value": True}
            manager = SimpleNamespace(has_jobs=lambda: active_jobs["value"])
            gui = SimpleNamespace(
                current_db=databases["incoming"],
                library_broker=SimpleNamespace(
                    get_library=lambda path: next(
                        (db for alias, db in databases.items() if paths[alias] == Path(path)),
                        None,
                    )
                ),
                job_manager=manager,
                proceed_question=SimpleNamespace(questions=[]),
            )

            def switch(path, allow_rebuild=False):
                self.assertFalse(allow_rebuild)
                gui.current_db = next(db for alias, db in databases.items() if paths[alias] == Path(path))

            gui.library_moved = switch
            registry = tuple({
                "alias": alias,
                "label": alias.title(),
                "path": str(paths[alias]),
                "read": True,
                "switch": True,
                "copy_destination": alias != "incoming",
                "library_id": f"{alias}-id",
            } for alias in ("incoming", "main", "archive"))
            bridge = CalibreRpcBridge(gui, library_registry=registry, library_switching_enabled=True)

            # Book ids overlap in all libraries, but aliases make references unambiguous.
            main_result = bridge.dispatch("search_books", {"library": "main", "query": "", "limit": 10})
            archive_result = bridge.dispatch("search_books", {"library": "archive", "query": "", "limit": 10})
            self.assertEqual(main_result["items"][0]["id"], archive_result["items"][0]["id"])
            self.assertEqual(main_result["items"][0]["library"], "main")
            self.assertEqual(archive_result["items"][0]["library"], "archive")
            self.assertIs(gui.current_db, databases["incoming"])
            self.assertNotIn(str(paths["main"]), json.dumps(bridge.dispatch("list_libraries", {})))

            compared = bridge.dispatch("find_cross_library_duplicates", {
                "source_library": "incoming",
                "target_libraries": ["main", "archive"],
                "limit": 10,
            })
            reasons = {
                reason
                for match in compared["matches"]
                for candidate in match["candidates"]
                for reason in candidate["reasons"]
            }
            self.assertIn("identifier:isbn", reasons)
            self.assertIn("title_authors", reasons)
            selective = bridge.dispatch("find_cross_library_duplicates", {
                "source_library": "incoming",
                "target_libraries": ["main"],
                "source_query": "Shared ISBN",
                "limit": 10,
            })
            self.assertEqual(selective["scanned_source_count"], 1)
            self.assertFalse(selective["truncated"])
            chunk = bridge.dispatch("find_cross_library_duplicates", {
                "source_library": "incoming",
                "target_libraries": ["main"],
                "source_query": "Shared ISBN",
                "limit": 1,
                "target_limit": 1,
            })
            self.assertTrue(chunk["truncated"])
            self.assertTrue(chunk["next_cursor"])
            self.assertEqual(chunk["source_scanned"], 1)
            self.assertEqual(chunk["source_total_known"], 1)
            self.assertEqual(chunk["target_libraries_scanned"], 1)
            self.assertEqual(chunk["target_books_scanned"], 1)
            self.assertEqual(chunk["candidate_queries"], 1)
            continued = bridge.dispatch("find_cross_library_duplicates", {
                "source_library": "incoming",
                "target_libraries": ["main"],
                "source_query": "Shared ISBN",
                "limit": 1,
                "target_limit": 1,
                "cursor": chunk["next_cursor"],
            })
            self.assertFalse(continued["truncated"])
            self.assertIsNone(continued["next_cursor"])
            with self.assertRaises(BridgeMethodError) as wrong_cursor:
                bridge.dispatch("find_cross_library_duplicates", {
                    "source_library": "incoming",
                    "target_libraries": ["archive"],
                    "source_query": "Shared ISBN",
                    "limit": 1,
                    "target_limit": 1,
                    "cursor": chunk["next_cursor"],
                })
            self.assertEqual(wrong_cursor.exception.code, "CURSOR_INVALID")

            with self.assertRaises(BridgeMethodError) as rejected:
                bridge.dispatch("update_book_metadata", {"library": "main", "book_id": 1, "changes": {}})
            self.assertEqual(rejected.exception.code, "LIBRARY_SWITCH_REQUIRED")

            switch_params = {
                "library": "main",
                "expected_active_library": "incoming",
                "expected_active_generation": 0,
                "confirmation": "SWITCH_LIBRARY:main",
            }
            with self.assertRaises(BridgeMethodError) as blocked:
                bridge.dispatch("switch_library", switch_params)
            self.assertEqual(blocked.exception.code, "LIBRARY_SWITCH_BLOCKED")
            active_jobs["value"] = False
            switched = bridge.dispatch("switch_library", switch_params)
            self.assertEqual(switched["active_library"], "main")
            self.assertEqual(switched["active_generation"], 1)
            self.assertIs(gui.current_db, databases["main"])

    def test_serialized_calls_are_handed_to_gui_dispatcher(self):
        gui_calls = queue.Queue()
        bridge = CalibreRpcBridge(FakeGui(), gui_dispatch=lambda *args: gui_calls.put(args))
        result = {}

        def invoke():
            result["value"] = bridge.call_serialized("ping", {})

        caller = threading.Thread(target=invoke)
        caller.start()
        method, params, reply = gui_calls.get(timeout=2)
        self.assertTrue(caller.is_alive())
        bridge._execute_on_gui(method, params, reply)
        caller.join(timeout=2)
        self.assertEqual(result["value"]["library_path"], "/books")
        bridge.close()

    def test_call_serialized_after_close_reports_shutdown_code(self):
        bridge = CalibreRpcBridge(FakeGui())
        bridge.close()
        with self.assertRaises(BridgeMethodError) as caught:
            bridge.call_serialized("ping", {})
        self.assertEqual(caught.exception.code, "BRIDGE_SHUTTING_DOWN")

    def test_mutations_fail_closed_outside_exact_calibre_9_12_0(self):
        calibre = types.ModuleType("calibre")
        calibre.__path__ = []
        constants = types.ModuleType("calibre.constants")
        constants.numeric_version = (9, 12, 1)
        with patch.dict(sys.modules, {"calibre": calibre, "calibre.constants": constants}):
            with self.assertRaises(BridgeMethodError) as caught:
                CalibreRpcBridge(FakeGui())._new_api()
        self.assertEqual(caught.exception.code, "UNSUPPORTED_BY_CALIBRE_VERSION")

    def test_metadata_mutation_updates_supported_fields_and_refreshes(self):
        gui = FakeGui()
        bridge = CalibreRpcBridge(gui)
        job = bridge.dispatch(
            "update_book_metadata",
            {
                "book_id": 1,
                "changes": {
                    "title": "Updated",
                    "authors": ["New Author"],
                    "tags": ["one", "two"],
                    "language": "en",
                    "identifiers": {"isbn": "2"},
                    "rating": 8,
                    "custom": {"#genre": "Fiction"},
                },
            },
        )
        self.assertEqual(job["status"], "completed")
        self.assertEqual(gui.current_db.rows[1].title, "Updated")
        self.assertEqual(gui.current_db.rows[1].languages, ["en"])
        self.assertEqual(gui.current_db.rows[1].custom["#genre"], "Fiction")
        self.assertEqual(gui.library_view.model().refreshed[-1], ((1,), 0))
        self.assertEqual(job["params"]["changes"], {"fields": ["authors", "custom", "identifiers", "language", "rating", "tags", "title"]})

    def test_metadata_mutation_rolls_back_on_failure(self):
        gui = FakeGui()
        gui.current_db.new_api.fail_set_once = True
        bridge = CalibreRpcBridge(gui)
        with self.assertRaises(BridgeMethodError) as caught:
            bridge.dispatch("update_book_metadata", {"book_id": 1, "changes": {"title": "Broken"}})
        self.assertEqual(caught.exception.code, "CALIBRE_JOB_FAILED")
        self.assertEqual(gui.current_db.rows[1].title, "Example")
        self.assertEqual(bridge.dispatch("list_jobs", {})[-1]["status"], "failed")
        self.assertTrue(gui.library_view.model().refreshed)

    def test_metadata_mutation_rejects_missing_book_and_composite_column(self):
        gui = FakeGui()
        bridge = CalibreRpcBridge(gui)
        with self.assertRaises(BridgeMethodError) as missing:
            bridge.dispatch("update_book_metadata", {"book_id": 99, "changes": {"title": "No"}})
        self.assertEqual(missing.exception.code, "BOOK_NOT_FOUND")
        with self.assertRaises(BridgeMethodError) as composite:
            bridge.dispatch("update_book_metadata", {"book_id": 1, "changes": {"custom": {"#computed": "No"}}})
        self.assertEqual(composite.exception.code, "POLICY_DENIED")

    def test_add_format_confines_paths_and_preserves_replacement_on_failure(self):
        gui = FakeGui()
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            imported = root / "replacement.epub"
            imported.write_bytes(b"new epub")
            forbidden = Path(outside) / "outside.epub"
            forbidden.write_bytes(b"outside")
            bridge = CalibreRpcBridge(gui, import_roots=(str(root),))
            with self.assertRaises(BridgeMethodError) as denied:
                bridge.dispatch("add_book_format", {"book_id": 1, "path": str(forbidden), "replace": True})
            self.assertEqual(denied.exception.code, "PATH_NOT_ALLOWED")

            gui.current_db.new_api.fail_add_once = True
            with self.assertRaises(BridgeMethodError) as failed:
                bridge.dispatch("add_book_format", {"book_id": 1, "path": str(imported), "replace": True})
            self.assertEqual(failed.exception.code, "CALIBRE_JOB_FAILED")
            self.assertEqual(gui.current_db.new_api.format(1, "EPUB"), b"old epub")

            job = bridge.dispatch("add_book_format", {"book_id": 1, "path": str(imported), "replace": True})
            self.assertEqual(job["status"], "completed")
            self.assertEqual(gui.current_db.new_api.format(1, "EPUB"), b"new epub")
            self.assertEqual(job["params"]["path"], "replacement.epub")

    def test_delete_format_requires_final_confirmation_and_rolls_back(self):
        gui = FakeGui()
        bridge = CalibreRpcBridge(gui)
        with self.assertRaises(BridgeMethodError) as denied:
            bridge.dispatch("delete_book_format", {"book_id": 2, "format": "EPUB"})
        self.assertEqual(denied.exception.code, "POLICY_DENIED")
        self.assertEqual(gui.current_db.new_api.format(2, "EPUB"), b"second epub")

        gui.current_db.new_api.fail_remove_once = True
        with self.assertRaises(BridgeMethodError) as failed:
            bridge.dispatch("delete_book_format", {"book_id": 1, "format": "PDF"})
        self.assertEqual(failed.exception.code, "CALIBRE_JOB_FAILED")
        self.assertEqual(gui.current_db.new_api.format(1, "PDF"), b"old pdf")

        job = bridge.dispatch("delete_book_format", {"book_id": 1, "format": "PDF"})
        self.assertEqual(job["status"], "completed")
        self.assertIsNone(gui.current_db.new_api.format(1, "PDF"))

    def test_conversion_handles_immediate_native_callback_without_losing_context(self):
        class ImmediateJobManager(FakeJobManager):
            def run_job(self, callback, name, args=None, description="", core_usage=1, **kwargs):
                job = super().run_job(callback, name, args=args, description=description, core_usage=core_usage, **kwargs)
                Path(job.args[1]).write_bytes(b"converted mobi")
                job.duration = 0
                callback(job)
                return job

        gui = FakeGui(job_manager=ImmediateJobManager())
        bridge = CalibreRpcBridge(gui, conversion_adapter=self.conversion_adapter)
        completed = bridge.dispatch("convert_book", {"book_id": 1, "output_format": "MOBI"})
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["calibre_job_id"], 1)
        self.assertEqual(gui.current_db.new_api.format(1, "MOBI"), b"converted mobi")
        self.assertFalse(bridge.calibre_jobs)
        self.assertFalse(bridge._conversion_context)
        native = gui.job_manager.jobs[0]
        self.assertFalse(Path(native.args[0]).exists())
        self.assertFalse(Path(native.args[1]).exists())

    def test_conversion_queue_failure_is_wrapped_and_cleans_temp_files(self):
        class FailingJobManager(FakeJobManager):
            def __init__(self):
                super().__init__()
                self.failed_args = None

            def run_job(self, callback, name, args=None, description="", core_usage=1, **kwargs):
                self.failed_args = list(args or [])
                raise OSError("queue exploded")

        manager = FailingJobManager()
        gui = FakeGui(job_manager=manager)
        bridge = CalibreRpcBridge(gui, conversion_adapter=self.conversion_adapter)
        with self.assertRaises(BridgeMethodError) as caught:
            bridge.dispatch("convert_book", {"book_id": 1, "output_format": "MOBI"})
        self.assertEqual(caught.exception.code, "CALIBRE_JOB_FAILED")
        self.assertIn("queue exploded", caught.exception.message)
        self.assertFalse(Path(manager.failed_args[0]).exists())
        self.assertFalse(Path(manager.failed_args[1]).exists())
        self.assertFalse(bridge.calibre_jobs)
        self.assertFalse(bridge._conversion_context)
        self.assertEqual(bridge.dispatch("list_jobs", {})[-1]["status"], "failed")

    def test_add_book_uses_native_threaded_job_and_duplicate_policy(self):
        gui = FakeGui()
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "new.epub"
            source.write_bytes(b"new epub")
            bridge = CalibreRpcBridge(
                gui,
                import_roots=(root,),
                import_adapter=lambda path, fmt: FakeMetadata("Imported", ["Writer"]),
                threaded_job_factory=self.threaded_job_factory,
            )
            queued = bridge.dispatch("add_book", {"path": str(source), "duplicate_policy": "reject"})
            self.assertEqual(queued["status"], "queued")
            native = gui.job_manager.jobs[0]
            native.execute()
            completed = bridge.dispatch("get_job_status", {"job_id": queued["id"]})
            self.assertEqual(completed["status"], "completed")
            book_id = completed["result"]["book_id"]
            self.assertEqual(gui.current_db.new_api.format(book_id, "EPUB"), b"new epub")
            self.assertEqual(gui.library_view.model().added, [1])

            duplicate = bridge.dispatch("add_book", {"path": str(source), "duplicate_policy": "reject"})
            gui.job_manager.jobs[-1].execute()
            rejected = bridge.dispatch("get_job_status", {"job_id": duplicate["id"]})
            self.assertEqual(rejected["status"], "failed")
            self.assertIn("DUPLICATE_REJECTED", rejected["error"])

    def test_add_book_skip_reports_duplicate_without_new_record(self):
        gui = FakeGui()
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "example.epub"
            source.write_bytes(b"duplicate")
            bridge = CalibreRpcBridge(
                gui,
                import_roots=(root,),
                import_adapter=lambda path, fmt: FakeMetadata("Example", ["Author"]),
                threaded_job_factory=self.threaded_job_factory,
            )
            queued = bridge.dispatch("add_book", {"path": str(source), "duplicate_policy": "skip"})
            gui.job_manager.jobs[0].execute()
            skipped = bridge.dispatch("get_job_status", {"job_id": queued["id"]})
            self.assertEqual(skipped["status"], "completed")
            self.assertEqual(skipped["result"], {"added": False, "duplicate": True, "policy": "skip"})

    def test_corrupt_import_preparation_fails_without_adding_a_book(self):
        gui = FakeGui()
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "corrupt.epub"
            source.write_bytes(b"not an epub")

            def reject_corrupt(path, fmt):
                raise ValueError("corrupt metadata container")

            bridge = CalibreRpcBridge(
                gui,
                import_roots=(root,),
                import_adapter=reject_corrupt,
                threaded_job_factory=self.threaded_job_factory,
            )
            queued = bridge.dispatch("add_book", {"path": str(source), "duplicate_policy": "reject"})
            gui.job_manager.jobs[-1].execute()
            failed = bridge.dispatch("get_job_status", {"job_id": queued["id"]})
            self.assertEqual(failed["status"], "failed")
            self.assertIn("corrupt metadata container", failed["error"])
            self.assertEqual(set(gui.current_db.rows), {1, 2, 3})

    def test_running_import_cancellation_waits_for_worker_and_removes_temporary_file(self):
        class DelayedKillJobManager(FakeJobManager):
            def _kill_job(self, job):
                job.killed = True
                job.abort.set()

        manager = DelayedKillJobManager()
        gui = FakeGui(job_manager=manager)
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "cancel.epub"
            source.write_bytes(b"cancel fixture")
            bridge = CalibreRpcBridge(
                gui,
                import_roots=(root,),
                import_adapter=lambda path, fmt: FakeMetadata("Cancelled", ["Writer"]),
                threaded_job_factory=self.threaded_job_factory,
            )
            queued = bridge.dispatch("add_book", {"path": str(source)})
            native = manager.jobs[0]
            native.is_running = True
            requested = bridge.dispatch("cancel_job", {"job_id": queued["id"]})
            self.assertEqual(requested["status"], "running")
            self.assertIn("waiting for metadata extraction", requested["message"])

            temporary = Path(root) / "prepared.epub"
            temporary.write_bytes(b"prepared")
            native.result = (FakeMetadata("Cancelled", ["Writer"]), str(temporary))
            native.is_running = False
            native.duration = 1
            cancelled = bridge.dispatch("get_job_status", {"job_id": queued["id"]})
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertFalse(temporary.exists())
            self.assertEqual(set(gui.current_db.rows), {1, 2, 3})

    def test_non_conversion_completion_callbacks_report_cancellation_truthfully(self):
        bridge = CalibreRpcBridge(FakeGui())
        cases = (
            (
                "copy_books_to_library",
                bridge._copy_context,
                {"move": False, "source_path": Path("/source"), "destination_path": Path("/destination"), "book_ids": (1,)},
                bridge._copy_library_done,
                {"ok": False, "code": "JOB_CANCELLED", "message": "Copy cancelled", "copied": []},
            ),
            (
                "save_book_to_disk",
                bridge._save_context,
                {"book_id": 1, "destination": Path("/export")},
                bridge._save_disk_done,
                {"ok": False, "code": "JOB_CANCELLED", "message": "Export cancelled"},
            ),
            (
                "email_book",
                bridge._email_context,
                {"book_id": 1, "format": "EPUB", "recipient": "reader@example.invalid"},
                bridge._email_done,
                {"ok": False, "code": "JOB_CANCELLED", "message": "E-mail cancelled"},
            ),
        )
        for method, contexts, context, callback, result in cases:
            with self.subTest(method=method):
                job_id = bridge._record_job(method, {}, "running", "Running")
                contexts[job_id] = context
                native = SimpleNamespace(killed=True, failed=False, result=result)
                callback(job_id, native)
                record = bridge.dispatch("get_job_status", {"job_id": job_id})
                self.assertEqual(record["status"], "cancelled")
                self.assertIn("JOB_CANCELLED", record["error"])

    def test_cancelled_partial_copy_is_failed_and_never_deletes_source(self):
        gui = FakeGui()
        bridge = CalibreRpcBridge(gui)
        job_id = bridge._record_job("move_books_to_library", {}, "running", "Running")
        bridge._copy_context[job_id] = {
            "move": True,
            "source_path": Path("/source"),
            "destination_path": Path("/destination"),
            "book_ids": (1,),
        }
        bridge._copy_library_done(
            job_id,
            SimpleNamespace(
                killed=True,
                failed=False,
                result={
                    "ok": False,
                    "code": "JOB_CANCELLED",
                    "message": "Copy cancelled",
                    "copied": [{"source_book_id": 1, "destination_book_id": 10, "action": "copied"}],
                },
            ),
        )
        record = bridge.dispatch("get_job_status", {"job_id": job_id})
        self.assertEqual(record["status"], "failed")
        self.assertIn("PARTIAL_COPY", record["error"])
        self.assertIn(1, gui.current_db.rows)

    def test_cover_replace_and_remove_are_confined_and_rollback_capable(self):
        gui = FakeGui()
        with tempfile.TemporaryDirectory() as root:
            cover = Path(root) / "cover.jpg"
            cover.write_bytes(b"new cover")
            bridge = CalibreRpcBridge(gui, import_roots=(root,))
            replaced = bridge.dispatch("set_book_cover", {"book_id": 1, "path": str(cover)})
            self.assertEqual(replaced["status"], "completed")
            self.assertEqual(gui.current_db.new_api.cover(1), b"new cover")
            removed = bridge.dispatch("set_book_cover", {"book_id": 1, "remove": True})
            self.assertEqual(removed["status"], "completed")
            self.assertIsNone(gui.current_db.new_api.cover(1))

    def test_delete_books_requires_dry_run_confirmation_and_uses_trash(self):
        gui = FakeGui()
        bridge = CalibreRpcBridge(gui)
        preview = bridge.dispatch("delete_books", {"book_ids": [3], "dry_run": True})
        self.assertTrue(preview["result"]["dry_run"])
        confirmation = preview["result"]["confirmation"]
        with self.assertRaises(BridgeMethodError) as denied:
            bridge.dispatch("delete_books", {"book_ids": [3], "dry_run": False, "confirmation": "wrong"})
        self.assertEqual(denied.exception.code, "POLICY_DENIED")
        deleted = bridge.dispatch(
            "delete_books",
            {"book_ids": [3], "dry_run": False, "confirmation": confirmation},
        )
        self.assertEqual(deleted["result"]["trashed"], [3])
        self.assertNotIn(3, gui.current_db.rows)
        self.assertEqual(gui.library_view.model().deleted, [(3,)])
        self.assertEqual(gui.library_view.model().delete_refreshes, 1)
        self.assertEqual(deleted["result"]["warnings"], [])

    def test_delete_reports_success_after_verified_removal_when_model_notification_fails(self):
        gui = FakeGui()
        bridge = CalibreRpcBridge(gui)
        preview = bridge.dispatch("delete_books", {"book_ids": [3], "dry_run": True})

        def fail_notification():
            raise RuntimeError("model refresh failed")

        gui.library_view.model().books_deleted = fail_notification
        deleted = bridge.dispatch(
            "delete_books",
            {
                "book_ids": [3],
                "dry_run": False,
                "confirmation": preview["result"]["confirmation"],
            },
        )
        self.assertEqual(deleted["status"], "completed")
        self.assertEqual(deleted["result"]["trashed"], [3])
        self.assertEqual(deleted["result"]["warnings"], ["gui_model_notification_failed"])
        self.assertNotIn(3, gui.current_db.rows)

    def test_email_uses_only_configured_recipient_and_native_threaded_job(self):
        gui = FakeGui()
        bridge = CalibreRpcBridge(
            gui,
            email_config_adapter=lambda: ({"reader@example.test": ["EPUB", False, False]}, {"reader@example.test": "Configured subject"}),
            email_send_adapter=self.email_send_adapter,
            threaded_job_factory=self.threaded_job_factory,
        )
        queued = bridge.dispatch(
            "email_book",
            {"book_id": 1, "recipient": "reader@example.test", "format": "EPUB"},
        )
        self.assertEqual(queued["status"], "queued")
        gui.job_manager.jobs[-1].execute()
        completed = bridge.dispatch("get_job_status", {"job_id": queued["id"]})
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(completed["result"]["smtp_accepted"])
        self.assertFalse(completed["result"]["delivery_confirmed"])
        with self.assertRaises(BridgeMethodError) as denied:
            bridge.dispatch(
                "email_book",
                {"book_id": 1, "recipient": "other@example.test", "format": "EPUB"},
            )
        self.assertEqual(denied.exception.code, "POLICY_DENIED")

    def test_email_rejects_unconfigured_recipient_format_and_conversion_shortcut(self):
        gui = FakeGui()
        bridge = CalibreRpcBridge(
            gui,
            email_config_adapter=lambda: ({"reader@example.test": ["AZW3", False, False]}, {}),
            email_send_adapter=self.email_send_adapter,
            threaded_job_factory=self.threaded_job_factory,
        )
        with self.assertRaises(BridgeMethodError) as denied:
            bridge.dispatch(
                "email_book",
                {"book_id": 1, "recipient": "reader@example.test", "format": "EPUB"},
            )
        self.assertEqual(denied.exception.code, "POLICY_DENIED")
        with self.assertRaises(BridgeMethodError) as unavailable:
            bridge.dispatch(
                "email_book",
                {"book_id": 1, "recipient": "reader@example.test", "format": "AZW3", "auto_convert": True},
            )
        self.assertEqual(unavailable.exception.code, "UNSUPPORTED_BY_CALIBRE_VERSION")

    def test_smtp_rejection_is_reported_as_failed_without_changing_the_book(self):
        gui = FakeGui()

        def reject_smtp(*args, **kwargs):
            raise OSError("SMTP 550 rejected")

        bridge = CalibreRpcBridge(
            gui,
            email_config_adapter=lambda: ({"reader@example.com": ("EPUB", False, False)}, {}),
            email_send_adapter=reject_smtp,
            threaded_job_factory=self.threaded_job_factory,
        )
        queued = bridge.dispatch(
            "email_book", {"book_id": 1, "recipient": "reader@example.com", "format": "EPUB"}
        )
        gui.job_manager.jobs[-1].execute()
        failed = bridge.dispatch("get_job_status", {"job_id": queued["id"]})
        self.assertEqual(failed["status"], "failed")
        self.assertIn("SMTP 550 rejected", failed["error"])
        self.assertEqual(gui.current_db.new_api.format(1, "EPUB"), b"old epub")

    def test_email_worker_submits_to_local_smtp_sink_without_receiving_credentials(self):
        received = []

        class SmtpHandler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b"220 local-sink ESMTP\r\n")
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    command = line.decode("ascii", "replace").strip().upper()
                    if command.startswith(("EHLO", "HELO")):
                        self.wfile.write(b"250-local-sink\r\n250 SIZE 104857600\r\n")
                    elif command.startswith(("MAIL FROM", "RCPT TO")):
                        self.wfile.write(b"250 OK\r\n")
                    elif command == "DATA":
                        self.wfile.write(b"354 End with <CRLF>.<CRLF>\r\n")
                        payload = bytearray()
                        while True:
                            chunk = self.rfile.readline()
                            if chunk == b".\r\n":
                                break
                            payload.extend(chunk)
                        received.append(bytes(payload))
                        self.wfile.write(b"250 accepted\r\n")
                    elif command == "QUIT":
                        self.wfile.write(b"221 bye\r\n")
                        return
                    else:
                        self.wfile.write(b"250 OK\r\n")

        class EmailLibrary:
            def __init__(self, path, is_second_db=False):
                self.new_api = SimpleNamespace(
                    has_id=lambda book_id: book_id == 1,
                    copy_format_to=lambda book_id, fmt, target: Path(target).write_bytes(b"smtp fixture"),
                )

            def close(self):
                return None

        with socketserver.TCPServer(("127.0.0.1", 0), SmtpHandler) as sink:
            thread = threading.Thread(target=sink.serve_forever, daemon=True)
            thread.start()

            def fake_gui_sendmail(attachment, attachment_name, recipient, subject, text, *, log, abort, notifications):
                self.assertEqual(Path(attachment).read_bytes(), b"smtp fixture")
                with smtplib.SMTP("127.0.0.1", sink.server_address[1], timeout=5) as client:
                    client.sendmail("calibre@example.invalid", [recipient], f"Subject: {subject}\r\n\r\n{text}")
                notifications.put((1.0, "SMTP accepted"))

            calibre = types.ModuleType("calibre")
            calibre.__path__ = []
            calibre_db = types.ModuleType("calibre.db")
            calibre_db.__path__ = []
            legacy = types.ModuleType("calibre.db.legacy")
            legacy.LibraryDatabase = EmailLibrary
            calibre_gui2 = types.ModuleType("calibre.gui2")
            calibre_gui2.__path__ = []
            email_module = types.ModuleType("calibre.gui2.email")
            email_module.gui_sendmail = fake_gui_sendmail
            modules = {
                "calibre": calibre,
                "calibre.db": calibre_db,
                "calibre.db.legacy": legacy,
                "calibre.gui2": calibre_gui2,
                "calibre.gui2.email": email_module,
            }
            with patch.dict(sys.modules, modules):
                result = CalibreRpcBridge(FakeGui())._email_worker(
                    "/books", 1, "EPUB", "reader@example.invalid", "Fixture", "Body", "fixture.epub",
                    abort=threading.Event(), log=lambda *args: None, notifications=queue.Queue(),
                )
            sink.shutdown()
            thread.join(timeout=2)
        self.assertTrue(result["smtp_accepted"])
        self.assertFalse(result["delivery_confirmed"])
        self.assertEqual(len(received), 1)

    def test_save_book_to_disk_is_confined_and_runs_as_threaded_job(self):
        gui = FakeGui()
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "exports"
            bridge = CalibreRpcBridge(
                gui,
                export_roots=(root,),
                save_to_disk_adapter=self.save_adapter,
                threaded_job_factory=self.threaded_job_factory,
            )
            queued = bridge.dispatch(
                "save_book_to_disk",
                {
                    "book_id": 1,
                    "destination_directory": str(destination),
                    "options": {"formats": "epub", "write_opf": True},
                },
            )
            self.assertEqual(queued["status"], "queued")
            gui.job_manager.jobs[-1].execute()
            completed = bridge.dispatch("get_job_status", {"job_id": queued["id"]})
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["result"]["artifact_count"], 1)
            with self.assertRaises(BridgeMethodError) as denied:
                bridge.dispatch(
                    "save_book_to_disk",
                    {"book_id": 1, "destination_directory": str(Path(root).parent / "outside")},
                )
            self.assertEqual(denied.exception.code, "PATH_NOT_ALLOWED")

    def test_save_book_to_disk_preserves_stable_failure_code(self):
        gui = FakeGui()
        with tempfile.TemporaryDirectory() as root:
            bridge = CalibreRpcBridge(
                gui,
                export_roots=(root,),
                save_to_disk_adapter=lambda *args, **kwargs: {
                    "ok": False,
                    "code": "DUPLICATE_REJECTED",
                    "message": "collision",
                },
                threaded_job_factory=self.threaded_job_factory,
            )
            queued = bridge.dispatch(
                "save_book_to_disk",
                {"book_id": 1, "destination_directory": str(Path(root) / "exports")},
            )
            gui.job_manager.jobs[-1].execute()
            failed = bridge.dispatch("get_job_status", {"job_id": queued["id"]})
            self.assertEqual(failed["status"], "failed")
            self.assertIn("DUPLICATE_REJECTED", failed["error"])

    def test_save_worker_restores_existing_files_when_publication_fails(self):
        class SaveLibrary:
            def __init__(self, path, is_second_db=False):
                self.new_api = SimpleNamespace(
                    has_id=lambda book_id: book_id == 1,
                    pref=lambda name, default=None: default,
                )

            def get_metadata(self, book_id, index_is_id=False):
                if not index_is_id:
                    raise AssertionError("save-to-disk must use a book id")
                return SimpleNamespace(title="Fixture")

            def close(self):
                return None

        class SaveConfig:
            def parse(self):
                return SimpleNamespace()

        def fake_save_to_disk(database, ids, root, opts=None, callback=None):
            self.assertIs(database.new_api, database)
            self.assertEqual(database.get_metadata(1, index_is_id=True).title, "Fixture")
            self.assertIsNone(database.pref("missing"))
            output = Path(root)
            (output / "book-a.epub").write_bytes(b"new a")
            (output / "book-b.epub").write_bytes(b"new b")
            return []

        calibre = types.ModuleType("calibre")
        calibre.__path__ = []
        calibre_db = types.ModuleType("calibre.db")
        calibre_db.__path__ = []
        legacy = types.ModuleType("calibre.db.legacy")
        legacy.LibraryDatabase = SaveLibrary
        calibre_library = types.ModuleType("calibre.library")
        calibre_library.__path__ = []
        save_module = types.ModuleType("calibre.library.save_to_disk")
        save_module.config = SaveConfig
        save_module.save_to_disk = fake_save_to_disk
        modules = {
            "calibre": calibre,
            "calibre.db": calibre_db,
            "calibre.db.legacy": legacy,
            "calibre.library": calibre_library,
            "calibre.library.save_to_disk": save_module,
        }
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "exports"
            destination.mkdir()
            first = destination / "book-a.epub"
            second = destination / "book-b.epub"
            first.write_bytes(b"old a")
            second.write_bytes(b"old b")
            real_replace = os.replace

            def fail_second_publish(source, target):
                source_path = Path(source)
                if source_path.name == "book-b.epub" and ".calibre-umcp-save-" in str(source_path):
                    raise OSError("simulated full disk during publication")
                return real_replace(source, target)

            bridge = CalibreRpcBridge(FakeGui(), export_roots=(root,))
            with patch.dict(sys.modules, modules), patch(
                "plugins.calibre_umcp_plugin.bridge.os.replace", side_effect=fail_second_publish
            ):
                result = bridge._save_disk_worker(
                    "/books", 1, str(destination), {}, True,
                    abort=threading.Event(), log=lambda *args: None, notifications=queue.Queue(),
                )
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "CALIBRE_JOB_FAILED")
            self.assertEqual(first.read_bytes(), b"old a")
            self.assertEqual(second.read_bytes(), b"old b")

    def test_copy_rejects_populated_missing_or_incompatible_custom_columns(self):
        metadata = SimpleNamespace(
            custom_field_keys=lambda: ("#owned",),
            get=lambda key: "value",
        )
        source = SimpleNamespace(
            field_metadata={"#owned": {"datatype": "text", "is_multiple": {"cache_to_list": ","}}}
        )
        with self.assertRaises(BridgeMethodError) as missing:
            CalibreRpcBridge._check_custom_column_compatibility(
                source, SimpleNamespace(field_metadata={}), metadata
            )
        self.assertEqual(missing.exception.code, "DESTINATION_UNAVAILABLE")

        destination = SimpleNamespace(
            field_metadata={"#owned": {"datatype": "text", "is_multiple": {}}}
        )
        with self.assertRaises(BridgeMethodError) as incompatible:
            CalibreRpcBridge._check_custom_column_compatibility(source, destination, metadata)
        self.assertEqual(incompatible.exception.code, "DESTINATION_UNAVAILABLE")

    def test_copy_books_uses_threaded_job_and_returns_verified_result(self):
        gui = FakeGui()
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "destination"
            destination.mkdir()
            (destination / "metadata.db").write_bytes(b"db")
            bridge = CalibreRpcBridge(
                gui,
                destination_libraries=(str(destination),),
                copy_to_library_adapter=self.copy_adapter,
                threaded_job_factory=self.threaded_job_factory,
            )
            queued = bridge.dispatch(
                "copy_books_to_library",
                {"book_ids": [1, 2], "destination_library": str(destination), "duplicate_policy": "reject"},
            )
            self.assertEqual(queued["status"], "queued")
            gui.job_manager.jobs[-1].execute()
            completed = bridge.dispatch("get_job_status", {"job_id": queued["id"]})
            self.assertEqual(completed["status"], "completed")
            self.assertEqual([item["source_book_id"] for item in completed["result"]["copied"]], [1, 2])
            self.assertIn(1, gui.current_db.rows)
            self.assertIn(2, gui.current_db.rows)

    def test_move_requires_preview_and_deletes_only_after_verified_copy(self):
        gui = FakeGui()
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "destination"
            destination.mkdir()
            (destination / "metadata.db").write_bytes(b"db")
            bridge = CalibreRpcBridge(
                gui,
                destination_libraries=(str(destination),),
                copy_to_library_adapter=self.copy_adapter,
                threaded_job_factory=self.threaded_job_factory,
            )
            preview = bridge.dispatch(
                "move_books_to_library",
                {"book_ids": [3], "destination_library": str(destination)},
            )
            confirmation = preview["result"]["confirmation"]
            self.assertIn(3, gui.current_db.rows)
            queued = bridge.dispatch(
                "move_books_to_library",
                {
                    "book_ids": [3],
                    "destination_library": str(destination),
                    "dry_run": False,
                    "confirmation": confirmation,
                },
            )
            gui.job_manager.jobs[-1].execute()
            completed = bridge.dispatch("get_job_status", {"job_id": queued["id"]})
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["result"]["moved_to_trash"], [3])
            self.assertEqual(completed["result"]["warnings"], [])
            self.assertEqual(gui.library_view.model().deleted, [(3,)])
            self.assertEqual(gui.library_view.model().delete_refreshes, 1)
            self.assertNotIn(3, gui.current_db.rows)

    def test_partial_copy_failure_never_deletes_move_sources(self):
        gui = FakeGui()
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "destination"
            destination.mkdir()
            (destination / "metadata.db").write_bytes(b"db")

            def partial_adapter(*args, **kwargs):
                return {
                    "ok": False,
                    "code": "PARTIAL_COPY",
                    "message": "verification failed",
                    "copied": [{"source_book_id": 1, "destination_book_id": 1001, "action": "add"}],
                    "failed_book_id": 2,
                }

            bridge = CalibreRpcBridge(
                gui,
                destination_libraries=(str(destination),),
                copy_to_library_adapter=partial_adapter,
                threaded_job_factory=self.threaded_job_factory,
            )
            preview = bridge.dispatch(
                "move_books_to_library",
                {"book_ids": [1, 2], "destination_library": str(destination)},
            )
            queued = bridge.dispatch(
                "move_books_to_library",
                {
                    "book_ids": [1, 2],
                    "destination_library": str(destination),
                    "dry_run": False,
                    "confirmation": preview["result"]["confirmation"],
                },
            )
            gui.job_manager.jobs[-1].execute()
            failed = bridge.dispatch("get_job_status", {"job_id": queued["id"]})
            self.assertEqual(failed["status"], "failed")
            self.assertIn("PARTIAL_COPY", failed["error"])
            self.assertIn(1, gui.current_db.rows)
            self.assertIn(2, gui.current_db.rows)

    def test_move_retains_sources_when_library_switches_before_deletion(self):
        gui = FakeGui()
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "destination"
            destination.mkdir()
            (destination / "metadata.db").write_bytes(b"db")

            def switching_adapter(*args, **kwargs):
                gui.current_db.library_path = "/other-library"
                return self.copy_adapter(*args, **kwargs)

            bridge = CalibreRpcBridge(
                gui,
                destination_libraries=(str(destination),),
                copy_to_library_adapter=switching_adapter,
                threaded_job_factory=self.threaded_job_factory,
            )
            preview = bridge.dispatch(
                "move_books_to_library", {"book_ids": [3], "destination_library": str(destination)}
            )
            queued = bridge.dispatch(
                "move_books_to_library",
                {
                    "book_ids": [3], "destination_library": str(destination), "dry_run": False,
                    "confirmation": preview["result"]["confirmation"],
                },
            )
            gui.job_manager.jobs[-1].execute()
            failed = bridge.dispatch("get_job_status", {"job_id": queued["id"]})
            self.assertEqual(failed["status"], "failed")
            self.assertIn("PARTIAL_COPY", failed["error"])
            self.assertIn(3, gui.current_db.rows)

    def test_move_reports_trash_failure_without_deleting_sources(self):
        gui = FakeGui()
        gui.current_db.new_api.fail_remove_books_once = True
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "destination"
            destination.mkdir()
            (destination / "metadata.db").write_bytes(b"db")
            bridge = CalibreRpcBridge(
                gui,
                destination_libraries=(str(destination),),
                copy_to_library_adapter=self.copy_adapter,
                threaded_job_factory=self.threaded_job_factory,
            )
            preview = bridge.dispatch(
                "move_books_to_library", {"book_ids": [3], "destination_library": str(destination)}
            )
            queued = bridge.dispatch(
                "move_books_to_library",
                {
                    "book_ids": [3], "destination_library": str(destination), "dry_run": False,
                    "confirmation": preview["result"]["confirmation"],
                },
            )
            gui.job_manager.jobs[-1].execute()
            failed = bridge.dispatch("get_job_status", {"job_id": queued["id"]})
            self.assertEqual(failed["status"], "failed")
            self.assertIn("simulated trash failure", failed["error"])
            self.assertIn(3, gui.current_db.rows)

    def test_duplicate_merge_refuses_books_with_any_active_calibre_job(self):
        bridge = CalibreRpcBridge(FakeGui())
        bridge._email_context["active-email"] = {"book_id": 1}
        with self.assertRaises(BridgeMethodError) as caught:
            bridge.dispatch(
                "merge_duplicates",
                {
                    "survivor_id": 1,
                    "source_ids": [2],
                    "confirmation": "MERGE_KEEP_SOURCES:1:2",
                },
            )
        self.assertEqual(caught.exception.code, "POLICY_DENIED")
        self.assertIn("active Calibre jobs", caught.exception.message)

    def test_duplicate_merge_keeps_sources_and_adds_only_missing_formats(self):
        gui = FakeGui()
        gui.current_db.rows[2].tags = ["source"]
        gui.current_db.new_api.format_data[2]["AZW3"] = b"source azw3"
        bridge = CalibreRpcBridge(gui)
        merged = bridge.dispatch(
            "merge_duplicates",
            {
                "survivor_id": 1,
                "source_ids": [2],
                "confirmation": "MERGE_KEEP_SOURCES:1:2",
            },
        )
        self.assertEqual(merged["status"], "completed")
        self.assertEqual(merged["result"]["added_formats"], ["AZW3"])
        self.assertEqual(gui.current_db.new_api.format(1, "EPUB"), b"old epub")
        self.assertEqual(gui.current_db.new_api.format(1, "AZW3"), b"source azw3")
        self.assertIn(2, gui.current_db.rows)
        self.assertIn("source", gui.current_db.rows[1].tags)

    def test_conversion_queues_native_job_and_attaches_output_only_on_completion(self):
        gui = FakeGui()
        bridge = CalibreRpcBridge(gui, conversion_adapter=self.conversion_adapter)
        queued = bridge.dispatch(
            "convert_book",
            {"book_id": 1, "output_format": "MOBI", "replace_existing": False, "options": {"line_height": 1.2}},
        )
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["calibre_job_id"], 1)
        self.assertIsNone(gui.current_db.new_api.format(1, "MOBI"))

        native = gui.job_manager.jobs[0]
        native.is_running = True
        native.percent = 50
        native.status_text = "Converting"
        running = bridge.dispatch("get_job_status", {"job_id": queued["id"]})
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["progress"], 0.5)
        self.assertIn(("line_height", 1.2, 3), native.args[2])

        Path(native.args[1]).write_bytes(b"converted mobi")
        native.is_running = False
        native.duration = 1
        native.callback(native)
        completed = bridge.dispatch("get_job_status", {"job_id": queued["id"]})
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(gui.current_db.new_api.format(1, "MOBI"), b"converted mobi")
        self.assertFalse(Path(native.args[0]).exists())
        self.assertFalse(Path(native.args[1]).exists())

    def test_conversion_can_export_atomically_without_mutating_book(self):
        gui = FakeGui()
        with tempfile.TemporaryDirectory() as export_root:
            destination = Path(export_root) / "result.mobi"
            bridge = CalibreRpcBridge(
                gui,
                export_roots=(export_root,),
                conversion_adapter=self.conversion_adapter,
            )
            queued = bridge.dispatch(
                "convert_book",
                {
                    "book_id": 1,
                    "output_format": "MOBI",
                    "store_result": False,
                    "export_path": str(destination),
                },
            )
            native = gui.job_manager.jobs[0]
            Path(native.args[1]).write_bytes(b"exported mobi")
            native.duration = 1
            native.callback(native)
            completed = bridge.dispatch("get_job_status", {"job_id": queued["id"]})
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["result"]["artifact"], str(destination))
            self.assertEqual(destination.read_bytes(), b"exported mobi")
            self.assertIsNone(gui.current_db.new_api.format(1, "MOBI"))

    def test_conversion_export_requires_configured_root_and_explicit_overwrite(self):
        gui = FakeGui()
        with tempfile.TemporaryDirectory() as export_root, tempfile.TemporaryDirectory() as outside:
            bridge = CalibreRpcBridge(
                gui,
                export_roots=(export_root,),
                conversion_adapter=self.conversion_adapter,
            )
            with self.assertRaises(BridgeMethodError) as denied:
                bridge.dispatch(
                    "convert_book",
                    {"book_id": 1, "output_format": "MOBI", "store_result": False, "export_path": str(Path(outside) / "x.mobi")},
                )
            self.assertEqual(denied.exception.code, "PATH_NOT_ALLOWED")
            destination = Path(export_root) / "x.mobi"
            destination.write_bytes(b"old")
            with self.assertRaises(BridgeMethodError) as duplicate:
                bridge.dispatch(
                    "convert_book",
                    {"book_id": 1, "output_format": "MOBI", "store_result": False, "export_path": str(destination)},
                )
            self.assertEqual(duplicate.exception.code, "DUPLICATE_REJECTED")

    def test_conversion_rejects_replacement_and_unknown_options_before_queueing(self):
        gui = FakeGui()
        bridge = CalibreRpcBridge(gui, conversion_adapter=self.conversion_adapter)
        with self.assertRaises(BridgeMethodError) as duplicate:
            bridge.dispatch("convert_book", {"book_id": 1, "output_format": "PDF"})
        self.assertEqual(duplicate.exception.code, "DUPLICATE_REJECTED")
        self.assertFalse(gui.job_manager.jobs)
        with self.assertRaises(BridgeMethodError) as policy:
            bridge.dispatch("convert_book", {"book_id": 1, "output_format": "MOBI", "options": {"unsafe": True}})
        self.assertEqual(policy.exception.code, "POLICY_DENIED")

    def test_conversion_cancellation_tracks_native_calibre_job(self):
        gui = FakeGui()
        bridge = CalibreRpcBridge(gui, conversion_adapter=self.conversion_adapter)
        queued = bridge.dispatch("convert_book", {"book_id": 1, "output_format": "AZW3"})
        cancelled = bridge.dispatch("cancel_job", {"job_id": queued["id"]})
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIn("JOB_CANCELLED", cancelled["error"])
        self.assertIsNone(gui.current_db.new_api.format(1, "AZW3"))

    def test_bridge_shutdown_requests_native_conversion_cancellation(self):
        gui = FakeGui()
        bridge = CalibreRpcBridge(gui, conversion_adapter=self.conversion_adapter)
        queued = bridge.dispatch("convert_book", {"book_id": 1, "output_format": "AZW3"})
        bridge.close()
        cancelled = bridge.dispatch("get_job_status", {"job_id": queued["id"]})
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(gui.job_manager.jobs[0].killed)

    def test_conversion_failure_does_not_attach_empty_output(self):
        gui = FakeGui()
        bridge = CalibreRpcBridge(gui, conversion_adapter=self.conversion_adapter)
        queued = bridge.dispatch("convert_book", {"book_id": 1, "output_format": "AZW3"})
        native = gui.job_manager.jobs[0]
        native.duration = 1
        native.callback(native)
        failed = bridge.dispatch("get_job_status", {"job_id": queued["id"]})
        self.assertEqual(failed["status"], "failed")
        self.assertIn("CALIBRE_JOB_FAILED", failed["error"])
        self.assertIsNone(gui.current_db.new_api.format(1, "AZW3"))

    def test_mutations_are_rejected_but_audited_until_safe_api_mapping_exists(self):
        bridge = CalibreRpcBridge(FakeGui())
        with self.assertRaises(BridgeMethodError) as caught:
            bridge.dispatch("move_book", {"book_id": 1})
        self.assertEqual(caught.exception.code, "UNSUPPORTED_BY_CALIBRE_VERSION")
        jobs = bridge.dispatch("list_jobs", {})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["method"], "move_book")
        self.assertEqual(jobs[0]["status"], "rejected")
        self.assertEqual(bridge.dispatch("get_job_status", {"job_id": jobs[0]["id"]}), jobs[0])

    def test_audit_redacts_export_and_destination_root_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            export_root = root / "private-exports"
            destination = root / "private-library"
            export_root.mkdir()
            destination.mkdir()
            audit_path = root / "audit.jsonl"
            bridge = CalibreRpcBridge(
                FakeGui(),
                audit_path=str(audit_path),
                export_roots=(str(export_root),),
                destination_libraries=(str(destination),),
            )
            bridge._append_audit(
                {
                    "status": "completed",
                    "result": {
                        "artifacts": [str(export_root / "Author" / "Book.epub")],
                        "detail": f"Copied to {destination / 'Author' / 'Book'}",
                    },
                }
            )
            written = audit_path.read_text(encoding="utf-8")
            self.assertNotIn(str(export_root), written)
            self.assertNotIn(str(destination), written)

    def test_rejected_mutation_can_write_jsonl_audit_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "audit.jsonl"
            bridge = CalibreRpcBridge(FakeGui(), audit_path=str(audit_path))
            with self.assertRaises(BridgeMethodError):
                bridge.dispatch("move_book", {"book_id": 1, "to": "other-library", "token": "secret"})
            record = json.loads(audit_path.read_text(encoding="utf-8").strip())
            self.assertEqual(record["method"], "move_book")
            self.assertEqual(record["status"], "rejected")
            self.assertEqual(record["params"]["token"], "<redacted>")


if __name__ == "__main__":
    unittest.main()
