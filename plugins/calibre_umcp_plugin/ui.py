from __future__ import annotations

import os

from calibre.gui2 import error_dialog, info_dialog
from calibre.gui2.actions import InterfaceAction
from qt.core import QAction, QMenu

from .bridge import serve_bridge


class CalibreUmcpAction(InterfaceAction):
    name = "Calibre µMCP Bridge"
    action_spec = ("µMCP Bridge", None, "Manage calibre-umcp bridge for this library", None)

    def genesis(self):
        self._server = None
        self._endpoint = None

        self.menu = QMenu(self.gui)
        self.start_action = QAction("Start bridge", self.gui)
        self.status_action = QAction("Bridge status", self.gui)
        self.stop_action = QAction("Stop bridge", self.gui)
        self.menu.addAction(self.start_action)
        self.menu.addAction(self.status_action)
        self.menu.addAction(self.stop_action)
        self.qaction.setMenu(self.menu)

        self.qaction.triggered.connect(self.status_bridge)
        self.start_action.triggered.connect(self.start_bridge)
        self.status_action.triggered.connect(self.status_bridge)
        self.stop_action.triggered.connect(self.stop_bridge)
        self._refresh_actions()

    def _library_path(self) -> str:
        db = self.gui.current_db
        library_path = getattr(db, "library_path", None)
        if callable(library_path):
            library_path = library_path()
        return str(library_path or "")

    def _refresh_actions(self) -> None:
        running = self._server is not None
        self.start_action.setEnabled(not running)
        self.status_action.setEnabled(True)
        self.stop_action.setEnabled(running)

    def start_bridge(self):
        if self._server is not None:
            self.status_bridge()
            return

        library_path = self._library_path()
        if not library_path:
            error_dialog(self.gui, "Calibre µMCP Bridge", "Could not determine current library path.", show=True)
            return

        port = int(os.environ.get("CALIBRE_UMCP_PORT", "9000"))
        host = os.environ.get("CALIBRE_UMCP_BRIDGE_HOST", "127.0.0.1")
        token = os.environ.get("CALIBRE_UMCP_BRIDGE_TOKEN")
        audit_path = os.environ.get("CALIBRE_UMCP_AUDIT_PATH")
        try:
            self._server = serve_bridge(self.gui, host, port, token=token, audit_path=audit_path)
            self._endpoint = f"http://{host}:{port}/rpc"
        except Exception as exc:
            self._server = None
            self._endpoint = None
            error_dialog(self.gui, "Calibre µMCP Bridge", f"Failed to start bridge: {exc}", show=True)
            return

        self._refresh_actions()
        info_dialog(
            self.gui,
            "Calibre µMCP Bridge",
            f"Bridge listening on {self._endpoint}\nLibrary: {library_path}",
            show=True,
        )

    def status_bridge(self):
        library_path = self._library_path() or "unknown"
        if self._server is None:
            info_dialog(self.gui, "Calibre µMCP Bridge", f"Bridge is stopped.\nLibrary: {library_path}", show=True)
            return
        bridge = getattr(self._server, "bridge", None)
        job_count = len(getattr(bridge, "job_records", {}) or {})
        info_dialog(
            self.gui,
            "Calibre µMCP Bridge",
            f"Bridge is running.\nEndpoint: {self._endpoint}\nLibrary: {library_path}\nTracked audit jobs: {job_count}",
            show=True,
        )

    def stop_bridge(self):
        if self._server is None:
            self._refresh_actions()
            return
        try:
            self._server.shutdown()
            self._server.server_close()
        finally:
            self._server = None
            self._endpoint = None
            self._refresh_actions()
        info_dialog(self.gui, "Calibre µMCP Bridge", "Bridge stopped.", show=True)

    def shutting_down(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self._endpoint = None
