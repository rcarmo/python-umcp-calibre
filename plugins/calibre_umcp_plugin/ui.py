from __future__ import annotations

from calibre.gui2 import error_dialog, info_dialog
from calibre.gui2.actions import InterfaceAction
from qt.core import (
    QAction,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QSpinBox,
    QTimer,
    QVBoxLayout,
)

from .bridge import BRIDGE_VERSION
from .config import config, load_settings
from .mcp import serve_mcp


class CalibreUmcpAction(InterfaceAction):
    name = "Calibre µMCP Bridge"
    action_spec = ("µMCP Bridge", None, "Manage calibre-umcp bridge for this library", None)

    def genesis(self):
        self._server = None
        self._endpoint = None
        self._auth_enabled = False

        self.menu = QMenu(self.gui)
        self.start_action = QAction("Start bridge", self.gui)
        self.status_action = QAction("Bridge status", self.gui)
        self.configure_action = QAction("Configure bridge", self.gui)
        self.stop_action = QAction("Stop bridge", self.gui)
        for action in (self.start_action, self.status_action, self.configure_action, self.stop_action):
            self.menu.addAction(action)
        self.qaction.setMenu(self.menu)

        self.qaction.triggered.connect(self.status_bridge)
        self.start_action.triggered.connect(self.start_bridge)
        self.status_action.triggered.connect(self.status_bridge)
        self.configure_action.triggered.connect(self.configure_bridge)
        self.stop_action.triggered.connect(self.stop_bridge)
        self._refresh_actions()

        # Let Calibre finish loading the active library before binding MCP.
        QTimer.singleShot(1000, lambda: self.start_bridge(notify=False))

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

    def start_bridge(self, checked=False, notify=True):
        if self._server is not None:
            if notify:
                self.status_bridge()
            return

        library_path = self._library_path()
        if not library_path:
            error_dialog(self.gui, "Calibre µMCP Bridge", "Could not determine current library path.", show=True)
            return

        settings = load_settings()
        self._auth_enabled = bool(settings.token)
        try:
            self._server = serve_mcp(self.gui, settings=settings)
            self._endpoint = f"http://{settings.host}:{settings.port}/mcp"
        except Exception as exc:
            self._server = None
            self._endpoint = None
            self._auth_enabled = False
            error_dialog(self.gui, "Calibre µMCP Bridge", f"Failed to start bridge: {exc}", show=True)
            return

        self._refresh_actions()
        if notify:
            info_dialog(
                self.gui,
                "Calibre µMCP Bridge",
                f"Bridge {BRIDGE_VERSION} listening on {self._endpoint}\nAuth: {'enabled' if self._auth_enabled else 'disabled'}\nLibrary: {library_path}",
                show=True,
            )

    def configure_bridge(self):
        prefs = config()
        dialog = QDialog(self.gui)
        dialog.setWindowTitle("Configure Calibre µMCP Bridge")
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        layout.addLayout(form)

        host = QLineEdit(str(prefs["host"] or "127.0.0.1"), dialog)
        port = QSpinBox(dialog)
        port.setRange(1, 65535)
        port.setValue(int(prefs["port"] or 9000))
        token = QLineEdit(str(prefs["token"] or ""), dialog)
        token.setEchoMode(QLineEdit.EchoMode.Password)
        mutations = QCheckBox("Enable implemented mutation tools", dialog)
        mutations.setChecked(bool(prefs["mutations_enabled"]))
        imports = QPlainTextEdit(str(prefs["import_roots"] or ""), dialog)
        exports = QPlainTextEdit(str(prefs["export_roots"] or ""), dialog)
        destinations = QPlainTextEdit(str(prefs["destination_libraries"] or ""), dialog)
        library_registry = QPlainTextEdit(str(prefs["library_registry"] or "[]"), dialog)
        switching = QCheckBox("Enable explicit library switching", dialog)
        switching.setChecked(bool(prefs["library_switching_enabled"]))
        audit_path = QLineEdit(str(prefs["audit_path"] or ""), dialog)
        retention = QSpinBox(dialog)
        retention.setRange(10, 10000)
        retention.setValue(int(prefs["audit_retention"] or 500))

        form.addRow("Bind host", host)
        form.addRow("Port", port)
        form.addRow("Bearer token", token)
        form.addRow("Mutations", mutations)
        form.addRow("Import roots (one per line)", imports)
        form.addRow("Export roots (one per line)", exports)
        form.addRow("Destination libraries (one per line, deprecated)", destinations)
        form.addRow("Library registry (JSON array)", library_registry)
        form.addRow("Library switching", switching)
        form.addRow("Audit JSONL path", audit_path)
        form.addRow("Audit records retained", retention)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog,
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        prefs["host"] = host.text().strip() or "127.0.0.1"
        prefs["port"] = port.value()
        prefs["token"] = token.text().strip()
        prefs["mutations_enabled"] = bool(mutations.isChecked())
        prefs["import_roots"] = imports.toPlainText().strip()
        prefs["export_roots"] = exports.toPlainText().strip()
        prefs["destination_libraries"] = destinations.toPlainText().strip()
        prefs["library_registry"] = library_registry.toPlainText().strip() or "[]"
        prefs["library_switching_enabled"] = bool(switching.isChecked())
        prefs["audit_path"] = audit_path.text().strip()
        prefs["audit_retention"] = retention.value()
        prefs.commit()

        was_running = self._server is not None
        if was_running:
            self.stop_bridge(notify=False)
            self.start_bridge(notify=False)
        info_dialog(
            self.gui,
            "Calibre µMCP Bridge",
            "Bridge settings saved" + (" and applied." if was_running else "."),
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
            f"Bridge {BRIDGE_VERSION} is running.\nEndpoint: {self._endpoint}\nAuth: {'enabled' if self._auth_enabled else 'disabled'}\nLibrary: {library_path}\nTracked jobs: {job_count}",
            show=True,
        )

    def stop_bridge(self, checked=False, notify=True):
        if self._server is None:
            self._refresh_actions()
            return
        server = self._server
        try:
            server.shutdown()
            server.server_close()
            bridge = getattr(server, "bridge", None)
            if bridge is not None:
                bridge.close()
            thread = getattr(server, "thread", None)
            if thread is not None:
                thread.join(timeout=2)
        finally:
            self._server = None
            self._endpoint = None
            self._auth_enabled = False
            self._refresh_actions()
        if notify:
            info_dialog(self.gui, "Calibre µMCP Bridge", "Bridge stopped.", show=True)

    def shutting_down(self):
        self.stop_bridge(notify=False)
