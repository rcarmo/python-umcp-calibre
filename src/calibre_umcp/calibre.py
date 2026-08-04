from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

from .config import Config, Library


class CalibreError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


class CalibreCLI:
    def __init__(self, config: Config):
        self.config = config

    def library(self, name: str | None = None) -> Library:
        selected = name or self.config.default_library
        try:
            return self.config.libraries[selected]
        except KeyError as exc:
            raise CalibreError(f"Unknown library {selected!r}") from exc

    def tool_path(self, executable: str) -> str:
        if self.config.calibre_bin:
            return str(self.config.calibre_bin / executable)
        found = shutil.which(executable)
        if not found:
            raise CalibreError(f"Required Calibre executable not found on PATH: {executable}")
        return found

    def run(self, args: list[str], *, timeout: int = 300) -> CommandResult:
        if self.config.dry_run:
            return CommandResult(command=args, returncode=0, stdout=json.dumps({"dry_run": args}), stderr="")
        proc = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
        result = CommandResult(args, proc.returncode, proc.stdout, proc.stderr)
        if proc.returncode != 0:
            raise CalibreError(f"Command failed ({proc.returncode}): {' '.join(args)}\n{proc.stderr.strip()}")
        return result

    def calibredb(self, library: Library, *args: str, timeout: int = 300) -> CommandResult:
        return self.run([self.tool_path("calibredb"), "--library-path", str(library.path), *args], timeout=timeout)

    def list_books(self, library_name: str | None = None, search: str = "", limit: int = 50) -> list[dict[str, Any]]:
        library = self.library(library_name)
        args = ["list", "--for-machine", "--fields", "id,title,authors,identifiers,formats"]
        if search:
            args.extend(["--search", search])
        result = self.calibredb(library, *args)
        rows = json.loads(result.stdout or "[]")
        return rows[: max(0, min(limit, 500))]

    def show_metadata(self, book_id: int, library_name: str | None = None) -> dict[str, Any]:
        library = self.library(library_name)
        result = self.calibredb(library, "show_metadata", str(book_id), "--as-json")
        return json.loads(result.stdout or "{}")

    def convert_book(self, input_path: str, output_path: str, extra_args: list[str] | None = None) -> dict[str, Any]:
        args = [self.tool_path("ebook-convert"), input_path, output_path, *(extra_args or [])]
        result = self.run(args, timeout=1800)
        return {"output_path": output_path, "stdout": result.stdout, "stderr": result.stderr}

    def copy_or_move(self, book_id: int, target_library: str, source_library: str | None = None, move: bool = False) -> dict[str, Any]:
        source = self.library(source_library)
        target = self.library(target_library)
        action = "move" if move else "copy"
        result = self.calibredb(source, action, str(book_id), "--dest-library", str(target.path), timeout=1800)
        return {"action": action, "book_id": book_id, "source": source.name, "target": target.name, "output": result.stdout}

    def email_book(self, book_id: int, to: str, library_name: str | None = None) -> dict[str, Any]:
        library = self.library(library_name)
        result = self.calibredb(library, "email", str(book_id), to, timeout=600)
        return {"book_id": book_id, "to": to, "output": result.stdout}

    def find_duplicates(self, library_name: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        books = self.list_books(library_name, limit=limit)
        buckets: dict[str, list[dict[str, Any]]] = {}
        for book in books:
            title = str(book.get("title") or "").casefold().strip()
            authors = ",".join(book.get("authors") or []).casefold().strip()
            ids = json.dumps(book.get("identifiers") or {}, sort_keys=True)
            key = hashlib.sha256(f"{title}\0{authors}\0{ids}".encode()).hexdigest()
            buckets.setdefault(key, []).append(book)
        return [{"count": len(rows), "books": rows} for rows in buckets.values() if len(rows) > 1]
