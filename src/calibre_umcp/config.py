from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Library:
    name: str
    path: Path


@dataclass(frozen=True)
class Config:
    libraries: dict[str, Library]
    default_library: str
    calibre_bin: Path | None
    dry_run: bool


def _parse_libraries(value: str) -> dict[str, Library]:
    libraries: dict[str, Library] = {}
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            name, path = chunk.split("=", 1)
        else:
            path = chunk
            name = Path(path).name or "default"
        name = name.strip()
        if not name:
            raise ValueError(f"Invalid empty library name in {value!r}")
        libraries[name] = Library(name=name, path=Path(path).expanduser())
    if not libraries:
        default_path = Path(os.environ.get("CALIBRE_LIBRARY", "/books"))
        libraries["default"] = Library(name="default", path=default_path)
    return libraries


def load_config() -> Config:
    libraries = _parse_libraries(os.environ.get("CALIBRE_LIBRARIES", ""))
    default_library = os.environ.get("CALIBRE_DEFAULT_LIBRARY", next(iter(libraries)))
    if default_library not in libraries:
        raise ValueError(f"CALIBRE_DEFAULT_LIBRARY {default_library!r} is not defined in CALIBRE_LIBRARIES")
    calibre_bin_env = os.environ.get("CALIBRE_BIN")
    return Config(
        libraries=libraries,
        default_library=default_library,
        calibre_bin=Path(calibre_bin_env).expanduser() if calibre_bin_env else None,
        dry_run=os.environ.get("CALIBRE_UMCP_DRY_RUN", "").lower() in {"1", "true", "yes"},
    )
