import json
import unittest
from unittest.mock import patch

from calibre_umcp.bridge import BridgeError, CalibreBridgeClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class BridgeClientTests(unittest.TestCase):
    def test_sends_bearer_token_when_configured(self):
        seen = {}

        def fake_urlopen(request, timeout):
            seen["timeout"] = timeout
            seen["authorization"] = request.get_header("Authorization")
            seen["content_type"] = request.get_header("Content-type")
            seen["body"] = json.loads(request.data.decode())
            return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

        client = CalibreBridgeClient("http://127.0.0.1:9000/rpc", token="secret")
        with patch("urllib.request.urlopen", fake_urlopen):
            result = client.call("ping")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen["authorization"], "Bearer secret")
        self.assertEqual(seen["content_type"], "application/json")
        self.assertEqual(seen["body"], {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
        self.assertEqual(seen["timeout"], 120)

    def test_raises_bridge_error_for_json_rpc_error(self):
        def fake_urlopen(request, timeout):
            return FakeResponse({"jsonrpc": "2.0", "id": 1, "error": {"message": "nope"}})

        client = CalibreBridgeClient("http://127.0.0.1:9000/rpc")
        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(BridgeError):
                client.call("missing")


if __name__ == "__main__":
    unittest.main()
