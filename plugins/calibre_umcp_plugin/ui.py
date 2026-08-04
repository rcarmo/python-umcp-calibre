from __future__ import annotations

import os
import subprocess
import sys

from calibre.gui2.actions import InterfaceAction
from calibre.gui2 import info_dialog, error_dialog


class CalibreUmcpAction(InterfaceAction):
    name = "Calibre µMCP Bridge"
    action_spec = ("Start µMCP Bridge", None, "Start calibre-umcp for this library", None)

    def genesis(self):
        self.qaction.triggered.connect(self.start_bridge)
        self._process = None

    def start_bridge(self):
        db = self.gui.current_db
        library_path = getattr(db, "library_path", None) or getattr(db, "library_path", "")
        if callable(library_path):
            library_path = library_path()
        if not library_path:
            error_dialog(self.gui, "Calibre µMCP Bridge", "Could not determine current library path.", show=True)
            return

        port = os.environ.get("CALIBRE_UMCP_PORT", "9000")
        env = os.environ.copy()
        env.setdefault("CALIBRE_LIBRARIES", f"current={library_path}")
        env.setdefault("CALIBRE_DEFAULT_LIBRARY", "current")

        cmd = [sys.executable, "-m", "calibre_umcp.server", "--host", "127.0.0.1", "--port", port, "--http"]
        try:
            self._process = subprocess.Popen(cmd, env=env)
        except Exception as exc:
            error_dialog(self.gui, "Calibre µMCP Bridge", f"Failed to start bridge: {exc}", show=True)
            return
        info_dialog(self.gui, "Calibre µMCP Bridge", f"Bridge listening on http://127.0.0.1:{port}/mcp", show=True)
