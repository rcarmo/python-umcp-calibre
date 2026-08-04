import json
import os
import unittest
import urllib.error
import urllib.request

os.environ.setdefault("CALIBRE_UMCP_DRY_RUN", "1")

from plugins.calibre_umcp_plugin.mcp import serve_mcp
from test_plugin_bridge import FakeGui


class PluginMCPTests(unittest.TestCase):
    def tearDown(self):
        server = getattr(self, "server", None)
        if server is not None:
            server.shutdown()
            server.server_close()
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
