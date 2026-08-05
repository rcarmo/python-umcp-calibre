from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass

from calibre.utils.config import JSONConfig


_CONFIG_NAME = "plugins/calibre_umcp_bridge"


@dataclass(frozen=True)
class BridgeSettings:
    host: str
    port: int
    token: str | None
    ui_token_configured: bool
    mutations_enabled: bool
    import_roots: tuple[str, ...]
    export_roots: tuple[str, ...]
    destination_libraries: tuple[str, ...]
    library_registry: tuple[dict[str, object], ...]
    library_switching_enabled: bool
    content_server_advertised_host: str
    audit_path: str | None
    audit_retention: int


def config() -> JSONConfig:
    prefs = JSONConfig(_CONFIG_NAME)
    prefs.defaults.update({
        "host": "127.0.0.1",
        "port": 9000,
        "token": "",
        "mutations_enabled": False,
        "import_roots": "",
        "export_roots": "",
        "destination_libraries": "",
        "library_registry": "[]",
        "library_switching_enabled": False,
        "content_server_advertised_host": "",
        "audit_path": "",
        "audit_retention": 500,
    })
    return prefs


def _paths(value: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in (value or "").splitlines() if line.strip())


_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _library_registry(value: str) -> tuple[dict[str, object], ...]:
    try:
        raw = json.loads(value or "[]")
    except (TypeError, ValueError) as exc:
        raise ValueError("Library registry must be a JSON array") from exc
    if not isinstance(raw, list):
        raise ValueError("Library registry must be a JSON array")
    entries: list[dict[str, object]] = []
    aliases: set[str] = set()
    paths: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Each library registry entry must be an object")
        alias = str(item.get("alias") or "")
        path = str(item.get("path") or "").strip()
        if not _ALIAS_RE.fullmatch(alias):
            raise ValueError(f"Invalid library alias: {alias!r}")
        normalized_path = os.path.realpath(os.path.expanduser(path)) if path else ""
        if not normalized_path:
            raise ValueError(f"Library {alias!r} requires a path")
        if alias in aliases or normalized_path in paths:
            raise ValueError("Library aliases and paths must be unique")
        aliases.add(alias)
        paths.add(normalized_path)
        entries.append({
            "alias": alias,
            "label": str(item.get("label") or alias),
            "path": normalized_path,
            "read": bool(item.get("read", True)),
            "switch": bool(item.get("switch", False)),
            "copy_destination": bool(item.get("copy_destination", False)),
            "library_id": str(item.get("library_id") or "") or None,
        })
    return tuple(entries)


def _advertised_host(value: str) -> str:
    host = str(value or "").strip().strip("[]")
    if not host:
        return ""
    valid = False
    try:
        ipaddress.ip_address(host)
        valid = True
    except ValueError:
        valid = bool(re.fullmatch(r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", host))
    if not valid:
        raise ValueError("Content-server advertised host must be a hostname or IP address without a scheme, port, or path")
    return host


def load_settings(environ=None) -> BridgeSettings:
    environ = os.environ if environ is None else environ
    prefs = config()
    ui_token = str(prefs["token"] or "").strip()
    environment_token = str(environ.get("CALIBRE_UMCP_BRIDGE_TOKEN") or "").strip()
    token = environment_token or ui_token or None
    host = str(environ.get("CALIBRE_UMCP_BRIDGE_HOST") or prefs["host"] or "127.0.0.1")
    port = int(environ.get("CALIBRE_UMCP_PORT") or prefs["port"] or 9000)
    audit_path = str(environ.get("CALIBRE_UMCP_AUDIT_PATH") or prefs["audit_path"] or "").strip() or None
    return BridgeSettings(
        host=host,
        port=port,
        token=token,
        ui_token_configured=bool(ui_token),
        mutations_enabled=bool(
            ui_token
            and prefs["mutations_enabled"]
            and (not environment_token or environment_token == ui_token)
        ),
        import_roots=_paths(str(prefs["import_roots"] or "")),
        export_roots=_paths(str(prefs["export_roots"] or "")),
        destination_libraries=_paths(str(prefs["destination_libraries"] or "")),
        library_registry=_library_registry(str(prefs["library_registry"] or "[]")),
        library_switching_enabled=bool(prefs["library_switching_enabled"]),
        content_server_advertised_host=_advertised_host(
            str(environ.get("CALIBRE_UMCP_CONTENT_SERVER_ADVERTISED_HOST") or prefs["content_server_advertised_host"] or "")
        ),
        audit_path=audit_path,
        audit_retention=max(10, min(int(prefs["audit_retention"] or 500), 10000)),
    )
