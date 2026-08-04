from __future__ import annotations

import os
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
        "audit_path": "",
        "audit_retention": 500,
    })
    return prefs


def _paths(value: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in (value or "").splitlines() if line.strip())


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
        audit_path=audit_path,
        audit_retention=max(10, min(int(prefs["audit_retention"] or 500), 10000)),
    )
