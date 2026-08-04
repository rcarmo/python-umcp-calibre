import json
import unittest
import urllib.error
import urllib.request

from plugins.calibre_umcp_plugin.bridge import is_loopback_bind, serve_bridge
from test_plugin_bridge import FakeGui


class BridgeHttpTests(unittest.TestCase):
    def test_loopback_bind_detection(self):
        self.assertTrue(is_loopback_bind("127.0.0.1"))
        self.assertTrue(is_loopback_bind("::1"))
        self.assertTrue(is_loopback_bind("localhost"))
        self.assertFalse(is_loopback_bind("0.0.0.0"))
        self.assertFalse(is_loopback_bind("calibre"))

    def test_token_required_for_non_loopback_bind(self):
        with self.assertRaises(ValueError):
            serve_bridge(FakeGui(), "0.0.0.0", 0)
        self.server = serve_bridge(FakeGui(), "0.0.0.0", 0, token="secret")

    def tearDown(self):
        server = getattr(self, "server", None)
        if server is not None:
            server.shutdown()
            server.server_close()

    def start_server(self, token=None):
        self.server = serve_bridge(FakeGui(), "127.0.0.1", 0, token=token)
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def test_health_endpoint(self):
        base = self.start_server()
        with urllib.request.urlopen(f"{base}/health", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read().decode()), {"ok": True})

    def rpc(self, base, payload):
        body = json.dumps(payload).encode()
        request = urllib.request.Request(f"{base}/rpc", data=body, headers={"content-type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode())

    def test_rpc_preserves_request_id_on_error(self):
        base = self.start_server()
        payload = self.rpc(base, {"jsonrpc": "2.0", "id": "abc", "method": "missing", "params": {}})
        self.assertEqual(payload["id"], "abc")
        self.assertIn("error", payload)

    def test_rpc_validates_request_shape(self):
        base = self.start_server()
        missing_method = self.rpc(base, {"jsonrpc": "2.0", "id": "missing-method", "params": {}})
        self.assertEqual(missing_method["id"], "missing-method")
        self.assertIn("non-empty string", missing_method["error"]["message"])

        bad_params = self.rpc(base, {"jsonrpc": "2.0", "id": "bad-params", "method": "ping", "params": []})
        self.assertEqual(bad_params["id"], "bad-params")
        self.assertIn("params must be an object", bad_params["error"]["message"])

    def test_rpc_requires_bearer_token_when_configured(self):
        base = self.start_server(token="secret")
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}).encode()
        request = urllib.request.Request(f"{base}/rpc", data=body, headers={"content-type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        try:
            self.assertEqual(caught.exception.code, 401)
        finally:
            caught.exception.close()

        authed = urllib.request.Request(
            f"{base}/rpc",
            data=body,
            headers={"content-type": "application/json", "authorization": "Bearer secret"},
            method="POST",
        )
        with urllib.request.urlopen(authed, timeout=5) as response:
            payload = json.loads(response.read().decode())
        self.assertEqual(payload["result"]["ok"], True)


if __name__ == "__main__":
    unittest.main()
