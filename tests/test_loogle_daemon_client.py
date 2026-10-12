import asyncio
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests.support import install_model_library_stub, reload_module

install_model_library_stub()
mcp_client = reload_module("proof_bench.mcp_client")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(port: int, status: int, body: dict) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_query_waits_for_a_daemon_that_is_still_loading():
    port = _free_port()
    servers: list[HTTPServer] = []
    timer = threading.Timer(1.0, lambda: servers.append(_serve(port, 200, {"hits": ["Nat.succ_le_iff"]})))
    timer.start()
    start = time.monotonic()
    try:
        result = asyncio.run(mcp_client._query_loogle_daemon(f"http://127.0.0.1:{port}", "Nat.succ_le_iff", 5))
    finally:
        timer.cancel()
        timer.join()
        for server in servers:
            server.shutdown()
            server.server_close()
    assert json.loads(result) == {"hits": ["Nat.succ_le_iff"]}
    assert time.monotonic() - start >= 1.0


def test_query_gives_up_on_a_daemon_that_never_starts(monkeypatch):
    monkeypatch.setattr(mcp_client, "LOOGLE_DAEMON_STARTUP_TIMEOUT_SECONDS", 0.6)
    url = f"http://127.0.0.1:{_free_port()}"
    with pytest.raises(ConnectionError, match="after waiting"):
        asyncio.run(mcp_client._query_loogle_daemon(url, "Nat.succ_le_iff", 5))


def test_server_errors_are_not_retried(monkeypatch):
    calls = []

    def fail_with_server_error(request, timeout):
        calls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 500, "boom", Message(), None)

    monkeypatch.setattr(urllib.request, "urlopen", fail_with_server_error)
    with pytest.raises(urllib.error.HTTPError):
        asyncio.run(mcp_client._query_loogle_daemon("http://127.0.0.1:8765", "Nat.succ_le_iff", 5))
    assert calls == ["http://127.0.0.1:8765/query"]
