"""The gateway, driven as a process through its CLI: ports, refusals and the proxy path.

Run with `python -m unittest discover -s tests`. Needs aiohttp (the image's
python3-aiohttp, or any 3.8+ from pip).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

GATEWAY = Path(__file__).resolve().parent.parent / "gateway" / "woow_auth_gateway.py"
TEST_SECRET = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"


def sign(payload: dict) -> str:
    """The ticket and session format woow_ingress signs (protocol v1), written out independently."""
    def b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    body = b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    digest = hmac.new(bytes.fromhex(TEST_SECRET), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{b64(digest)}"


def ticket(target: str) -> str:
    now = int(time.time())
    return sign({"v": 1, "kind": "ticket", "target": target, "uid": "admin",
                 "iat": now, "exp": now + 45, "jti": f"jti-{time.monotonic_ns()}"})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def get(port: int, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict, bytes]:
    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


# What the HA iframe card's browser sends: same host, HA frontend on 8123, embedded.
IFRAME_HEADERS = {
    "Referer": "http://127.0.0.1:8123/",
    "Sec-Fetch-Dest": "iframe",
    "Sec-Fetch-Site": "same-site",
}


def fake_upstream(port: int, body: bytes) -> http.server.HTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def listening(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_listening(port: int, proc: subprocess.Popen, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"gateway exited with {proc.returncode}: {proc.log()}")
        if listening(port):
            return
        time.sleep(0.1)
    raise AssertionError(f"gateway is not listening on {port}")


class GatewayCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.secret_file = Path(tmp.name) / "ingress-secret"
        self.secret_file.write_text(TEST_SECRET + "\n", encoding="utf-8")
        self.log_file = Path(tmp.name) / "gateway.log"

    def start(self, *args: str) -> subprocess.Popen:
        log = self.log_file.open("w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(GATEWAY), "--secret-file", str(self.secret_file),
             "--listen", "127.0.0.1", *args],
            stdout=subprocess.DEVNULL,
            stderr=log,
            text=True,
        )
        proc.log = lambda: self.log_file.read_text(encoding="utf-8")

        def stop() -> None:
            proc.kill()
            proc.wait()
            log.close()

        self.addCleanup(stop)
        return proc


class PortsTest(GatewayCase):
    def test_pi_port_0_opens_only_the_tailscale_port(self) -> None:
        tailscale = free_port()
        pi_default = 30142
        self.assertFalse(listening(pi_default), "something else holds 30142; cannot test")
        proc = self.start("--pi-port", "0", "--tailscale-port", str(tailscale))
        wait_listening(tailscale, proc)
        self.assertFalse(listening(pi_default))
        self.assertIsNone(proc.poll())
        log = proc.log()
        self.assertIn(f"Protecting tailscale-web on 127.0.0.1:{tailscale}", log)
        self.assertNotIn("Protecting pi-agent", log)

    def test_tailscale_port_0_opens_only_the_pi_port(self) -> None:
        pi = free_port()
        tailscale_default = 8088
        self.assertFalse(listening(tailscale_default), "something else holds 8088; cannot test")
        proc = self.start("--pi-port", str(pi), "--tailscale-port", "0")
        wait_listening(pi, proc)
        self.assertFalse(listening(tailscale_default))
        log = proc.log()
        self.assertIn(f"Protecting pi-agent on 127.0.0.1:{pi}", log)
        self.assertNotIn("Protecting tailscale-web", log)

    def test_both_ports_0_is_an_error(self) -> None:
        proc = self.start("--pi-port", "0", "--tailscale-port", "0")
        self.assertNotEqual(proc.wait(timeout=15), 0)
        self.assertIn("every target is switched off", proc.log())


class RefusalTest(GatewayCase):
    def test_open_port_without_ticket_is_404(self) -> None:
        port = free_port()
        proc = self.start("--pi-port", "0", "--tailscale-port", str(port))
        wait_listening(port, proc)
        for path in ("/", "/auth/launch", "/auth/launch?ticket=forged.ticket"):
            with self.subTest(path=path):
                self.assertEqual(get(port, path, IFRAME_HEADERS)[0], 404)
        self.assertEqual(get(port, "/")[0], 404)


class ProxyTest(GatewayCase):
    def test_pi_agent_proxies_to_127_0_0_1_30141_by_default(self) -> None:
        self.assertFalse(listening(30141), "something else holds 30141; cannot test")
        upstream = fake_upstream(30141, b"pi-web says hi")
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        port = free_port()
        proc = self.start("--pi-port", str(port), "--tailscale-port", "0")
        wait_listening(port, proc)

        status, headers, _ = get(port, f"/auth/launch?ticket={ticket('pi-agent')}", IFRAME_HEADERS)
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertTrue(cookie.startswith("woow_pi_session="))

        status, _, body = get(port, "/", {**IFRAME_HEADERS, "Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(body, b"pi-web says hi")


if __name__ == "__main__":
    unittest.main()
