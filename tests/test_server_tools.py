import os
import unittest

os.environ.setdefault("CALIBRE_UMCP_DRY_RUN", "1")
os.environ.setdefault("CALIBRE_LIBRARIES", "main=/books,articles=/books/Articles")
os.environ.setdefault("CALIBRE_DEFAULT_LIBRARY", "main")

from calibre_umcp.bridge import BridgeError
from calibre_umcp.server import CalibreMCPServer


class ServerToolTests(unittest.TestCase):
    def test_lists_libraries(self):
        server = CalibreMCPServer()
        self.assertEqual(server.tool_list_libraries_readonly()["main"], "/books")
        self.assertEqual(server.tool_list_libraries()["main"], "/books")

    def test_mutating_tools_fail_closed_without_plugin_bridge(self):
        server = CalibreMCPServer()
        with self.assertRaises(BridgeError):
            server.tool_convert_book("/tmp/in.epub", "/tmp/out.pdf")

    def test_bridge_status_reports_disabled(self):
        server = CalibreMCPServer()
        self.assertEqual(server.tool_bridge_status_readonly()["enabled"], False)


if __name__ == "__main__":
    unittest.main()
