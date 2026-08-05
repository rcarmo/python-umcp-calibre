"""Run with Calibre 9.12.0's embedded Python, not the system Python.

Example:
    CALIBRE_RUNTIME_ROOT=/opt/calibre-9.12.0 \
      /opt/calibre-9.12.0/calibre-debug -e tests/calibre_9_12_runtime_integration.py
"""

import json
import os
import queue
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

PROJECT = Path(__file__).resolve().parents[1]
CALIBRE_ROOT = Path(os.environ["CALIBRE_RUNTIME_ROOT"]).resolve()
sys.path.insert(0, str(PROJECT / "plugins"))

from calibre.constants import numeric_version
from calibre.db.legacy import LibraryDatabase
from calibre.ebooks.metadata.book.base import Metadata
from calibre_umcp_plugin.bridge import CalibreRpcBridge


def require(condition, detail):
    if not condition:
        raise RuntimeError(detail)


require(tuple(numeric_version[:3]) == (9, 12, 0), f"Unexpected Calibre version: {numeric_version!r}")
root = Path(tempfile.mkdtemp(prefix="calibre-umcp-runtime-"))
source_path = root / "source"
destination_path = root / "destination"
export_path = root / "exports"
fixture_path = root / "fixtures"
for path in (source_path, destination_path, export_path, fixture_path):
    path.mkdir(parents=True, exist_ok=True)

source = None
try:
    epub = fixture_path / "fixture.epub"
    shutil.copy2(CALIBRE_ROOT / "resources" / "quick_start" / "eng.epub", epub)
    pdf = fixture_path / "fixture.pdf"
    subprocess.run(
        [str(CALIBRE_ROOT / "ebook-convert"), str(epub), str(pdf)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cover = (CALIBRE_ROOT / "resources" / "images" / "default_cover.png").read_bytes()

    source = LibraryDatabase(str(source_path), is_second_db=True)
    destination = LibraryDatabase(str(destination_path), is_second_db=True)
    destination.close()

    metadata = Metadata("Fixture Book", ["Fixture Author"])
    metadata.identifiers = {"uuid": "fixture"}
    book_id = source.new_api.create_book_entry(metadata, cover=cover, add_duplicates=True)
    require(
        source.new_api.add_format(book_id, "EPUB", str(epub), replace=True, run_hooks=False),
        "EPUB insertion failed",
    )
    require(
        source.new_api.add_format(book_id, "PDF", str(pdf), replace=True, run_hooks=False),
        "PDF insertion failed",
    )
    source_hashes = {fmt: source.new_api.format_hash(book_id, fmt) for fmt in ("EPUB", "PDF")}

    bridge = CalibreRpcBridge(
        SimpleNamespace(current_db=source),
        export_roots=(str(export_path),),
        destination_libraries=(str(destination_path),),
    )
    worker_args = {
        "abort": threading.Event(),
        "log": lambda *args: None,
        "notifications": queue.Queue(),
    }
    copied = bridge._copy_library_worker(
        str(source_path), str(destination_path), (book_id,), "add", {}, **worker_args
    )
    require(copied["ok"] and len(copied["copied"]) == 1, copied)

    copied_id = copied["copied"][0]["destination_book_id"]
    copied_db = LibraryDatabase(str(destination_path), is_second_db=True)
    try:
        actual = copied_db.new_api.get_metadata(copied_id, get_cover=True, cover_as_data=True)
        require(actual.title == "Fixture Book", f"Unexpected title: {actual.title!r}")
        require(tuple(actual.authors) == ("Fixture Author",), f"Unexpected authors: {actual.authors!r}")
        require(
            set(copied_db.new_api.formats(copied_id, verify_formats=True)) == {"EPUB", "PDF"},
            "Format set mismatch",
        )
        for fmt in ("EPUB", "PDF"):
            actual_hash = copied_db.new_api.format_hash(copied_id, fmt)
            require(actual_hash == source_hashes[fmt], (fmt, actual_hash, source_hashes[fmt]))
    finally:
        copied_db.close()

    alternate_epub = fixture_path / "alternate.epub"
    shutil.copy2(CALIBRE_ROOT / "resources" / "quick_start" / "ita.epub", alternate_epub)
    copied_db = LibraryDatabase(str(destination_path), is_second_db=True)
    try:
        copied_db.new_api.remove_formats({copied_id: {"PDF"}})
        require(
            copied_db.new_api.add_format(
                copied_id, "EPUB", str(alternate_epub), replace=True, run_hooks=False
            ),
            "Could not prepare duplicate-policy fixture",
        )
        alternate_hash = copied_db.new_api.format_hash(copied_id, "EPUB")
        require(alternate_hash != source_hashes["EPUB"], "Alternate EPUB hash unexpectedly matches")
    finally:
        copied_db.close()

    merged = bridge._copy_library_worker(
        str(source_path),
        str(destination_path),
        (book_id,),
        "merge_missing",
        {book_id: copied_id},
        **worker_args,
    )
    require(merged["ok"], merged)
    copied_db = LibraryDatabase(str(destination_path), is_second_db=True)
    try:
        require(copied_db.new_api.format_hash(copied_id, "EPUB") == alternate_hash, "merge_missing replaced EPUB")
        require(copied_db.new_api.format_hash(copied_id, "PDF") == source_hashes["PDF"], "merge_missing omitted PDF")
    finally:
        copied_db.close()

    replaced = bridge._copy_library_worker(
        str(source_path),
        str(destination_path),
        (book_id,),
        "replace",
        {book_id: copied_id},
        **worker_args,
    )
    require(replaced["ok"], replaced)
    copied_db = LibraryDatabase(str(destination_path), is_second_db=True)
    try:
        require(copied_db.new_api.format_hash(copied_id, "EPUB") == source_hashes["EPUB"], "replace kept stale EPUB")
    finally:
        copied_db.close()

    exported = bridge._save_disk_worker(
        str(source_path),
        book_id,
        str(bridge.export_roots[0]),
        {"formats": "EPUB,PDF"},
        False,
        **worker_args,
    )
    require(exported["ok"], exported)
    require(exported["artifacts"], exported)
    for artifact in exported["artifacts"]:
        require(
            Path(artifact).resolve().is_relative_to(export_path.resolve()),
            f"Export escaped root: {artifact}",
        )

    received_mail = []

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
                    received_mail.append(bytes(payload))
                    self.wfile.write(b"250 accepted\r\n")
                elif command == "QUIT":
                    self.wfile.write(b"221 bye\r\n")
                    return
                else:
                    self.wfile.write(b"250 OK\r\n")

    with socketserver.TCPServer(("127.0.0.1", 0), SmtpHandler) as sink:
        from calibre.utils.smtp import config as email_config

        smtp = email_config()
        smtp.set("from_", "calibre@example.invalid")
        smtp.set("accounts", {"reader@example.invalid": ["EPUB", False, False]})
        smtp.set("subjects", {"reader@example.invalid": "Fixture subject"})
        smtp.set("relay_host", "127.0.0.1")
        smtp.set("relay_port", sink.server_address[1])
        smtp.set("relay_username", None)
        smtp.set("relay_password", "")
        smtp.set("encryption", "NONE")
        accounts, subjects = bridge._configured_email_policy()
        require("reader@example.invalid" in accounts, "Configured recipient was not resolved")
        require(subjects["reader@example.invalid"] == "Fixture subject", "Configured subject was not resolved")
        sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
        sink_thread.start()
        mailed = bridge._email_worker(
            str(source_path),
            book_id,
            "EPUB",
            "reader@example.invalid",
            "Fixture subject",
            "Fixture body",
            "fixture.epub",
            **worker_args,
        )
        sink.shutdown()
        sink_thread.join(timeout=2)
    require(mailed["ok"] and mailed["smtp_accepted"] and not mailed["delivery_confirmed"], mailed)
    require(len(received_mail) == 1, f"Expected one SMTP message, got {len(received_mail)}")

    print(
        json.dumps(
            {
                "calibre_version": ".".join(map(str, numeric_version[:3])),
                "copied_books": len(copied["copied"]),
                "export_artifact_count": len(exported["artifacts"]),
                "duplicate_policies_verified": ["merge_missing", "replace"],
                "export_confined": True,
                "plugin_version_gate": "exact",
                "smtp_sink_accepted": True,
                "verified_formats": ["EPUB", "PDF"],
            },
            sort_keys=True,
        )
    )
finally:
    if source is not None:
        source.close()
    shutil.rmtree(root, ignore_errors=True)
