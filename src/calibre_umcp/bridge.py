from __future__ import annotations

import json
import os
import urllib.request
from typing import Any


class BridgeError(RuntimeError):
    pass


class CalibreBridgeClient:
    def __init__(self, url: str | None = None, token: str | None = None) -> None:
        self.url = url or os.environ.get("CALIBRE_UMCP_BRIDGE_URL")
        self.token = token or os.environ.get("CALIBRE_UMCP_BRIDGE_TOKEN")

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def call(self, method: str, **params: Any) -> Any:
        if not self.url:
            raise BridgeError("CALIBRE_UMCP_BRIDGE_URL is required for live library operations")
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        headers = {"content-type": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=120) as response:
            payload = json.loads(response.read().decode())
        if "error" in payload:
            raise BridgeError(str(payload["error"]))
        return payload.get("result")
