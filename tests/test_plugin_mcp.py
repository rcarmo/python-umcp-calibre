import json
import os
import sys
import types
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("CALIBRE_UMCP_DRY_RUN", "1")

from plugins.calibre_umcp_plugin.mcp import CalibrePluginMCPServer, serve_mcp
from test_plugin_bridge import FakeGui


class PluginMCPTests(unittest.TestCase):
    def tearDown(self):
        server = getattr(self, "server", None)
        if server is not None:
            server.shutdown()
            server.server_close()
            server.bridge.close()
            server.thread.join(timeout=2)

    def start_server(self, token=None):
        self.server = serve_mcp(FakeGui(), "127.0.0.1", 0, token=token)
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def post(self, base, payload, token=None, initialized=True):
        headers = {"content-type": "application/json", "accept": "application/json"}
        if initialized:
            headers["mcp-protocol-version"] = "2025-03-26"
        if token:
            headers["authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"{base}/mcp", data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read()
            return response.status, json.loads(body.decode()) if body else None

    def test_initialize_and_progressive_tool_discovery(self):
        base = self.start_server()
        status, initialized = self.post(
            base,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "clientInfo": {"name": "test", "version": "1"}},
            },
            initialized=False,
        )
        self.assertEqual(status, 200)
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "calibre-umcp")
        self.assertEqual(initialized["result"]["capabilities"], {"tools": {"listChanged": False}})

        _, listed = self.post(base, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = [tool["name"] for tool in listed["result"]["tools"]]
        self.assertIn("capabilities_readonly", names)
        self.assertIn("search_books_readonly", names)
        self.assertNotIn("move_book_destructive", names)

    def test_calls_live_plugin_tool_through_umcp(self):
        base = self.start_server()
        _, called = self.post(
            base,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "search_books_readonly", "arguments": {"query": "Example", "limit": 1}},
            },
        )
        self.assertEqual(called["result"]["structuredContent"][0]["id"], 1)

    def test_mutation_discovery_requires_ui_token_and_explicit_policy(self):
        disabled = CalibrePluginMCPServer(
            FakeGui(), token="environment-only", ui_token_configured=False, mutations_enabled=True
        )
        self.addCleanup(disabled.bridge.close)
        disabled_names = {tool["name"] for tool in disabled.discover_tools()["tools"]}
        self.assertNotIn("capabilities_mutation", disabled_names)
        self.assertNotIn("update_book_metadata_mutation", disabled_names)
        self.assertFalse(
            disabled.authorize_request(
                SimpleNamespace(name="calibre-user"),
                rpc_method="tools/call",
                tool_name="update_book_metadata_mutation",
            )
        )

        enabled = CalibrePluginMCPServer(
            FakeGui(), token="ui-token", ui_token_configured=True, mutations_enabled=True
        )
        self.addCleanup(enabled.bridge.close)
        enabled_names = {tool["name"] for tool in enabled.discover_tools()["tools"]}
        self.assertIn("capabilities_mutation", enabled_names)
        self.assertIn("update_book_metadata_mutation", enabled_names)
        self.assertIn("convert_book_mutation", enabled_names)
        self.assertIn("copy_books_to_library_mutation", enabled_names)
        self.assertIn("move_books_to_library_mutation", enabled_names)
        described = enabled.tool_describe_tool_readonly("update_book_metadata_mutation")
        self.assertIn("changes", described["arguments"])
        self.assertEqual(described["args"], described["arguments"])

    def test_mutation_discovery_fails_closed_outside_exact_calibre_9_11_0(self):
        calibre = types.ModuleType("calibre")
        calibre.__path__ = []
        constants = types.ModuleType("calibre.constants")
        constants.numeric_version = (9, 11, 1)
        with patch.dict(sys.modules, {"calibre": calibre, "calibre.constants": constants}):
            server = CalibrePluginMCPServer(
                FakeGui(), token="ui-token", ui_token_configured=True, mutations_enabled=True
            )
        self.addCleanup(server.bridge.close)
        names = {tool["name"] for tool in server.discover_tools()["tools"]}
        self.assertFalse(server.mutation_runtime_supported)
        self.assertNotIn("capabilities_mutation", names)
        self.assertNotIn("update_book_metadata_mutation", names)

    def test_health_and_bearer_auth(self):
        base = self.start_server(token="secret")
        with urllib.request.urlopen(f"{base}/health", timeout=5) as response:
            self.assertTrue(json.loads(response.read().decode())["ok"])

        request = urllib.request.Request(
            f"{base}/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode(),
            headers={
                "content-type": "application/json",
                "accept": "application/json",
                "mcp-protocol-version": "2025-03-26",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        caught.exception.close()
        self.assertEqual(caught.exception.code, 401)

        status, payload = self.post(
            base, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, token="secret"
        )
        self.assertEqual(status, 200)
        self.assertIn("tools", payload["result"])


if __name__ == "__main__":
    unittest.main()
