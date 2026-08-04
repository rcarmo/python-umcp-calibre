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

    def test_progressive_discovery_is_compact_and_readonly_by_default(self):
        server = CalibreMCPServer()
        capabilities = server.tool_capabilities_readonly()
        self.assertEqual(capabilities["strategy"], "progressive-discovery")
        names = [tool["name"] for tool in capabilities["tools"]]
        self.assertIn("bridge_status_readonly", names)
        self.assertIn("search_books_readonly", names)
        self.assertNotIn("move_book_destructive", names)
        self.assertLess(len(str(capabilities)), 3000)

    def test_progressive_discovery_can_include_mutating_placeholders(self):
        server = CalibreMCPServer()
        names = [tool["name"] for tool in server.tool_capabilities_readonly(include_mutating=True)["tools"]]
        self.assertIn("move_book_destructive", names)

    def test_describes_one_tool_at_a_time(self):
        server = CalibreMCPServer()
        detail = server.tool_describe_tool_readonly("search_books_readonly")
        self.assertEqual(detail["name"], "search_books_readonly")
        self.assertIn("limit", detail["args"])
        with self.assertRaises(BridgeError):
            server.tool_describe_tool_readonly("missing")

    def test_mutating_tools_fail_closed_without_plugin_bridge(self):
        server = CalibreMCPServer()
        with self.assertRaises(BridgeError):
            server.tool_convert_book("/tmp/in.epub", "/tmp/out.pdf")

    def test_bridge_status_reports_disabled(self):
        server = CalibreMCPServer()
        self.assertEqual(server.tool_bridge_status_readonly()["enabled"], False)

    def test_bridge_job_tools_fail_closed_without_plugin_bridge(self):
        server = CalibreMCPServer()
        with self.assertRaises(BridgeError):
            server.tool_list_bridge_jobs_readonly()
        with self.assertRaises(BridgeError):
            server.tool_get_bridge_job_status_readonly("missing")


if __name__ == "__main__":
    unittest.main()
