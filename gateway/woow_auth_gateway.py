#!/usr/bin/env python3
"""HA-ticket authenticated reverse proxy for Pi Web and Tailscale Web."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, TCPConnector, WSMsgType, web
from multidict import CIMultiDict

_LOGGER = logging.getLogger("woow_auth_gateway")
_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


@dataclass(frozen=True)
class Target:
    name: str
    listen_port: int
    upstream: str
    cookie_name: str


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sign(secret: bytes, payload: dict[str, Any]) -> str:
    body = _b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    signature = _b64encode(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def verify(secret: bytes, token: str, kind: str, target: str) -> dict[str, Any] | None:
    try:
        body, supplied_signature = token.split(".", 1)
        expected_signature = _b64encode(
            hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        payload = json.loads(_b64decode(body))
        now = int(time.time())
        if payload.get("v") != 1 or payload.get("kind") != kind:
            return None
        if payload.get("target") != target or not payload.get("uid"):
            return None
        if int(payload.get("iat", 0)) > now + 30 or int(payload.get("exp", 0)) < now:
            return None
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def filtered_request_headers(request: web.Request) -> CIMultiDict[str]:
    connection_tokens = {
        item.strip().lower()
        for item in request.headers.get("Connection", "").split(",")
        if item.strip()
    }
    blocked = _HOP_HEADERS | connection_tokens
    headers: CIMultiDict[str] = CIMultiDict()
    for name, value in request.headers.items():
        if name.lower() not in blocked and name.lower() != "content-length":
            headers.add(name, value)
    headers["Host"] = request.host
    headers["X-Forwarded-Host"] = request.host
    headers["X-Forwarded-Proto"] = request.scheme
    if request.remote:
        headers["X-Forwarded-For"] = request.remote
    return headers


def filtered_response_headers(response_headers: Any) -> CIMultiDict[str]:
    connection_tokens = {
        item.strip().lower()
        for item in response_headers.get("Connection", "").split(",")
        if item.strip()
    }
    blocked = _HOP_HEADERS | connection_tokens
    headers: CIMultiDict[str] = CIMultiDict()
    for name, value in response_headers.items():
        if name.lower() not in blocked:
            headers.add(name, value)
    return headers


class Gateway:
    def __init__(self, secret: bytes, target: Target, session_hours: int) -> None:
        self.secret = secret
        self.target = target
        self.session_seconds = session_hours * 3600
        self.used_tickets: dict[str, int] = {}
        self.client: ClientSession | None = None

    async def startup(self, _app: web.Application) -> None:
        self.client = ClientSession(
            timeout=ClientTimeout(total=None, connect=10, sock_connect=10),
            connector=TCPConnector(force_close=True),
            auto_decompress=False,
        )

    async def cleanup(self, _app: web.Application) -> None:
        if self.client is not None:
            await self.client.close()

    def authenticated(self, request: web.Request) -> dict[str, Any] | None:
        token = request.cookies.get(self.target.cookie_name)
        if not token:
            return None
        return verify(self.secret, token, "session", self.target.name)

    def same_origin(self, request: web.Request) -> bool:
        origin = request.headers.get("Origin")
        if not origin:
            return True
        parsed = urlsplit(origin)
        return parsed.scheme == request.scheme and parsed.netloc == request.host

    def launched_from_ha_iframe(self, request: web.Request) -> bool:
        referer = urlsplit(request.headers.get("Referer", ""))
        gateway = urlsplit(f"{request.scheme}://{request.host}")
        return (
            referer.scheme == request.scheme
            and referer.hostname == gateway.hostname
            and referer.port == 8123
            and request.headers.get("Sec-Fetch-Dest") != "document"
        )

    def is_embedded_browser_request(self, request: web.Request) -> bool:
        if request.headers.get("Sec-Fetch-Dest") == "document":
            return False
        if request.headers.get("Sec-Fetch-Site") in {"cross-site", "none"}:
            return False
        gateway = urlsplit(f"{request.scheme}://{request.host}")
        referer = urlsplit(request.headers.get("Referer", ""))
        if (
            referer.scheme == request.scheme
            and referer.hostname == gateway.hostname
            and referer.port in {8123, gateway.port}
        ):
            return True
        origin = urlsplit(request.headers.get("Origin", ""))
        return origin.scheme == request.scheme and origin.netloc == request.host

    async def launch(self, request: web.Request) -> web.Response:
        if not self.launched_from_ha_iframe(request):
            raise web.HTTPNotFound()
        token = request.query.get("ticket", "")
        payload = verify(self.secret, token, "ticket", self.target.name)
        if payload is None:
            raise web.HTTPNotFound()
        now = int(time.time())
        self.used_tickets = {
            jti: expiry for jti, expiry in self.used_tickets.items() if expiry >= now
        }
        jti = str(payload.get("jti", ""))
        if not jti or jti in self.used_tickets:
            raise web.HTTPNotFound()
        self.used_tickets[jti] = int(payload["exp"])
        session_payload = {
            "v": 1,
            "kind": "session",
            "target": self.target.name,
            "uid": payload["uid"],
            "iat": now,
            "exp": now + self.session_seconds,
            "nonce": secrets.token_urlsafe(18),
        }
        response = web.HTTPSeeOther(location="/")
        response.set_cookie(
            self.target.cookie_name,
            sign(self.secret, session_payload),
            httponly=True,
            secure=request.secure,
            samesite="Lax",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    async def logout(self, _request: web.Request) -> web.Response:
        response = web.HTTPSeeOther(location="/")
        response.del_cookie(self.target.cookie_name, path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    async def dispatch(self, request: web.Request) -> web.StreamResponse:
        if self.authenticated(request) is None or not self.is_embedded_browser_request(request):
            raise web.HTTPNotFound()
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not self.same_origin(request):
            raise web.HTTPNotFound()
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self.proxy_websocket(request)
        return await self.proxy_http(request)

    async def proxy_http(self, request: web.Request) -> web.StreamResponse:
        assert self.client is not None
        upstream_url = f"{self.target.upstream}{request.rel_url}"
        body = request.content.iter_chunked(64 * 1024) if request.can_read_body else None
        try:
            upstream = await self.client.request(
                request.method,
                upstream_url,
                headers=filtered_request_headers(request),
                data=body,
                allow_redirects=False,
            )
        except Exception as err:
            _LOGGER.warning("%s upstream request failed: %s", self.target.name, err)
            raise web.HTTPBadGateway(text="Protected service is unavailable") from err
        headers = filtered_response_headers(upstream.headers)
        # Protected content must never be reusable by a top-level navigation from
        # the browser cache after it was viewed inside HA.
        headers["Cache-Control"] = "no-store, private"
        headers["Pragma"] = "no-cache"
        headers.popall("Expires", None)
        if headers.get("Content-Type", "").lower().startswith("text/html"):
            gateway = urlsplit(f"{request.scheme}://{request.host}")
            hostname = gateway.hostname or ""
            if ":" in hostname:
                hostname = f"[{hostname}]"
            headers["Content-Security-Policy"] = (
                f"frame-ancestors {request.scheme}://{hostname}:8123"
            )
        response = web.StreamResponse(status=upstream.status, reason=upstream.reason, headers=headers)
        try:
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await response.write(chunk)
            await response.write_eof()
        except ConnectionResetError:
            raise asyncio.CancelledError from None
        finally:
            upstream.release()
        return response

    async def proxy_websocket(self, request: web.Request) -> web.WebSocketResponse:
        assert self.client is not None
        protocols = [
            item.strip()
            for item in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
            if item.strip()
        ]
        upstream_url = f"{self.target.upstream.replace('http://', 'ws://', 1)}{request.rel_url}"
        client_ws = web.WebSocketResponse(protocols=protocols, autoping=True)
        await client_ws.prepare(request)
        headers = filtered_request_headers(request)
        for name in ("Sec-WebSocket-Key", "Sec-WebSocket-Version", "Sec-WebSocket-Extensions"):
            headers.popall(name, None)
        try:
            upstream_ws = await self.client.ws_connect(
                upstream_url, headers=headers, protocols=protocols, autoping=True
            )
        except Exception as err:
            _LOGGER.warning("%s websocket connection failed: %s", self.target.name, err)
            await client_ws.close(code=1011, message=b"Upstream unavailable")
            return client_ws

        async def client_to_upstream() -> None:
            async for message in client_ws:
                if message.type == WSMsgType.TEXT:
                    await upstream_ws.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await upstream_ws.send_bytes(message.data)
                elif message.type == WSMsgType.ERROR:
                    break

        async def upstream_to_client() -> None:
            async for message in upstream_ws:
                if message.type == WSMsgType.TEXT:
                    await client_ws.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await client_ws.send_bytes(message.data)
                elif message.type == WSMsgType.ERROR:
                    break

        tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)
        await upstream_ws.close()
        await client_ws.close()
        return client_ws

    def app(self) -> web.Application:
        app = web.Application(client_max_size=1024**3)
        app.router.add_get("/auth/launch", self.launch)
        app.router.add_get("/auth/logout", self.logout)
        app.router.add_route("*", "/{path:.*}", self.dispatch)
        app.on_startup.append(self.startup)
        app.on_cleanup.append(self.cleanup)
        return app


async def run(args: argparse.Namespace) -> None:
    secret = bytes.fromhex(Path(args.secret_file).read_text(encoding="utf-8").strip())
    if len(secret) < 32:
        raise RuntimeError("Gateway secret must contain at least 32 bytes")
    targets = (
        Target("pi-agent", args.pi_port, args.pi_upstream, "woow_pi_session"),
        Target("tailscale-web", args.tailscale_port, args.tailscale_upstream, "woow_ts_session"),
    )
    runners: list[web.AppRunner] = []
    for target in targets:
        runner = web.AppRunner(Gateway(secret, target, args.session_hours).app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, args.listen, target.listen_port).start()
        runners.append(runner)
        _LOGGER.info("Protecting %s on %s:%d", target.name, args.listen, target.listen_port)
    try:
        await asyncio.Event().wait()
    finally:
        for runner in runners:
            await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--secret-file", required=True)
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--pi-port", type=int, default=30142)
    parser.add_argument("--pi-upstream", default="http://127.0.0.1:30143")
    parser.add_argument("--tailscale-port", type=int, default=8088)
    parser.add_argument("--tailscale-upstream", default="http://127.0.0.1:8089")
    parser.add_argument("--session-hours", type=int, default=12)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
