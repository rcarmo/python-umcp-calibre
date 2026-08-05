import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from calibre_umcp.umcp import MCPServer


class MCPLoggingTests(unittest.TestCase):
    def test_unwritable_zip_import_path_does_not_block_startup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "plugin.zip"
            archive.touch()
            server = object.__new__(MCPServer)
            server.log_file = archive / "calibre_plugins" / "plugin" / "mcpserver.log"
            with patch.dict(os.environ, {"CALIBRE_UMCP_DRY_RUN": ""}):
                server._setup_logging()
            self.assertFalse(server.log_file.exists())

    def test_unrelated_log_directory_errors_remain_visible(self):
        server = object.__new__(MCPServer)
        server.log_file = Path("/tmp/calibre-umcp-test/mcpserver.log")
        with (
            patch.dict(os.environ, {"CALIBRE_UMCP_DRY_RUN": ""}),
            patch.object(Path, "mkdir", side_effect=PermissionError("denied")),
            self.assertRaises(PermissionError),
        ):
            server._setup_logging()


if __name__ == "__main__":
    unittest.main()
