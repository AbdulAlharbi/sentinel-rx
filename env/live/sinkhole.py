"""Helpers for the live environment: free ports, tiny HTTP client, and the sinkhole server.

The sinkhole plays both the C2 server (``/gate``) and the update mirror (``/updates``) and
counts the hits, which the demo reports as ground truth.
"""

from __future__ import annotations

import json
import socket
import urllib.request
from http.server import BaseHTTPRequestHandler
from typing import Any


def free_port() -> int:
    """An unused localhost TCP port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(url: str, data: dict | None = None, timeout: float = 2.0) -> Any:
    """GET (or POST ``data`` as JSON) and parse the JSON reply."""
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


class Sinkhole(BaseHTTPRequestHandler):
    """Accepts beacon check-ins and update polls; counts them."""

    hits: dict[str, int] = {"gate": 0, "updates": 0}

    def log_message(self, *args: Any) -> None:  # noqa: D102
        pass

    def do_GET(self) -> None:  # noqa: N802, D102
        key = "gate" if self.path.startswith("/gate") else "updates"
        Sinkhole.hits[key] += 1
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
