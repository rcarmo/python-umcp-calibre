from __future__ import annotations

import os

from calibre.gui2.actions import InterfaceAction
from calibre.gui2 import info_dialog, error_dialog
from .bridge import serve_bridge


class CalibreUmcpAction(InterfaceAction):
    name = "Calibre µMCP Bridge"
    action_spec = ("Start µMCP Bridge", None, "Start calibre-umcp for this library", None)

    def genesis(self):
        self.qaction.triggered.connect(self.start_bridge)
        self._server = None

    def start_bridge(self):
        db = self.gui.current_db
        library_path = getattr(db, "library_path", None) or getattr(db, "library_path", "")
        if callable(library_path):
            library_path = library_path()
        if not library_path:
            error_dialog(self.gui, "Calibre µMCP Bridge", "Could not determine current library path.", show=True)
            return

        port = os.environ.get("CALIBRE_UMCP_PORT", "9000")
        host = os.environ.get("CALIBRE_UMCP_BRIDGE_HOST", "127.0.0.1")
        token = os.environ.get("CALIBRE_UMCP_BRIDGE_TOKEN")
        try:
            self._server = serve_bridge(self.gui, host, int(port), token=token)
        except Exception as exc:
            error_dialog(self.gui, "Calibre µMCP Bridge", f"Failed to start bridge: {exc}", show=True)
            return
        info_dialog(self.gui, "Calibre µMCP Bridge", f"Bridge listening on http://{host}:{port}/rpc for {library_path}", show=True)
