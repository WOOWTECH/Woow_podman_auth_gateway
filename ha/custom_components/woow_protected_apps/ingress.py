"""Same-origin, administrator-authenticated ingress for trusted local applications.

Application scripts intentionally share HA's origin and frontend privileges. Cookie
namespacing and the bridge provide compatibility, not a browser security boundary.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, WSMsgType, web
from yarl import URL

from homeassistant.components.http import HomeAssistantView
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

BASE = '/api/woow_protected_apps'
UPSTREAMS = {
    'pi-agent': 'http://127.0.0.1:30141',
    'tailscale-web': 'http://127.0.0.1:8089',
}
HOP = {
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailer', 'transfer-encoding', 'upgrade', 'content-length',
}
UPSTREAM_ERRORS = (ClientError, OSError, asyncio.TimeoutError)


def rewrite(text, prefix, content_type='', bridge=''):
    if 'text/html' in content_type:
        text = re.sub(r'((?:src|href|action)=[\"\'])/(?!/)', lambda m: m[1] + prefix, text)
        text = text.replace('<head>', '<head><script>' + bridge + '</script>', 1)
    elif 'javascript' in content_type:
        # Only Next webpack's publicPath, not arbitrary JS strings or regex literals.
        text = re.sub(r'(\.p\s*=\s*[\"\'])/([\"\'])', lambda m: m[1] + prefix + m[2], text)
    elif 'text/css' in content_type:
        text = re.sub(r'(url\([\"\']?)/(?!/)', lambda m: m[1] + prefix, text)
    return text


class Ingress(HomeAssistantView):
    url = BASE + '/{target}/{path:.*}'
    name = 'api:woow_protected_apps:ingress'
    requires_auth = False  # Launch tickets come only from HA's admin WebSocket API.

    def __init__(self, hass, targets, bridge, *, session_check_interval=15):
        if session_check_interval <= 0:
            raise ValueError('session_check_interval must be positive')
        self.hass, self.targets = hass, targets
        self.bridge = bridge
        self.session_check_interval = session_check_interval
        self.tickets, self.sessions = {}, {}
        self.client = ClientSession(
            cookie_jar=DummyCookieJar(),
            timeout=ClientTimeout(total=None, connect=10),
            auto_decompress=True,
        )

        async def close(_event):
            await self.client.close()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, close)

    def issue(self, target, uid):
        now = time.monotonic()
        self.tickets = {k: v for k, v in self.tickets.items() if v[2] > now}
        self.sessions = {k: v for k, v in self.sessions.items() if v[2] > now}
        ticket = secrets.token_urlsafe(32)
        self.tickets[ticket] = (target, uid, now + 45)
        return f'{BASE}/{target}/_launch?ticket={ticket}'

    async def admin(self, uid):
        user = await self.hass.auth.async_get_user(uid)
        return bool(user and user.is_active and user.is_admin)

    async def watch_session(self, token, target):
        """Return on logout, revocation or expiry, even when a stream is idle."""
        while True:
            session = self.sessions.get(token)
            if not session or session[0] != target:
                return
            remaining = session[2] - time.monotonic()
            if remaining <= 0:
                return
            # Bound even a slow user lookup by the session's expiry deadline.
            try:
                allowed = await asyncio.wait_for(self.admin(session[1]), remaining)
            except asyncio.TimeoutError:
                return
            if not allowed:
                return
            remaining = session[2] - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(self.session_check_interval, remaining))

    async def relay_until_revoked(self, token, target, *relays):
        """Run relays until one finishes or authorization ends; always reap tasks."""
        monitor = asyncio.create_task(self.watch_session(token, target))
        tasks = [monitor, *(asyncio.create_task(relay) for relay in relays)]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            return monitor in done
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def handle(self, request, target, path):
        if target not in self.targets or target not in UPSTREAMS:
            raise web.HTTPNotFound()
        prefix = f'{BASE}/{target}/'
        cookie_name = 'woow_ingress_' + target.replace('-', '_')
        origin = request.headers.get('Origin')
        expected = f'{request.scheme}://{request.host}'
        if origin and origin != expected:
            raise web.HTTPForbidden()
        if request.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise web.HTTPForbidden()
        if path == '_launch':
            if request.method != 'GET':
                raise web.HTTPMethodNotAllowed(request.method, ['GET'])
            ticket = self.tickets.pop(request.query.get('ticket', ''), None)
            if (
                not ticket or ticket[0] != target or ticket[2] <= time.monotonic()
                or not await self.admin(ticket[1])
            ):
                raise web.HTTPUnauthorized()
            token = secrets.token_urlsafe(32)
            self.sessions[token] = (target, ticket[1], time.monotonic() + 3600)
            response = web.HTTPSeeOther(location=prefix)
            response.set_cookie(
                cookie_name, token, path=prefix, secure=request.secure,
                httponly=True, samesite='Strict', max_age=3600,
            )
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Referrer-Policy'] = 'no-referrer'
            raise response
        token = request.cookies.get(cookie_name, '')
        session = self.sessions.get(token)
        if (
            not session or session[0] != target or session[2] <= time.monotonic()
            or not await self.admin(session[1])
        ):
            raise web.HTTPUnauthorized()
        if path == '_logout':
            self.sessions.pop(token, None)
            response = web.Response(status=204)
            response.del_cookie(cookie_name, path=prefix)
            return response

        # Validate the decoded route, but forward the original encoded path/query.
        # Concatenation with the fixed origin (never URL joining) prevents SSRF.
        if path.startswith('/') or any(part in ('.', '..') for part in path.split('/')):
            raise web.HTTPBadRequest()
        raw_path = request.rel_url.raw_path
        if not raw_path.startswith(prefix):
            raise web.HTTPBadRequest()
        raw_url = UPSTREAMS[target] + '/' + raw_path[len(prefix):]
        if request.rel_url.raw_query_string:
            raw_url += '?' + request.rel_url.raw_query_string
        url = URL(raw_url, encoded=True)
        blocked = HOP | {
            'host', 'authorization', 'cookie', 'accept-encoding',
            'x-forwarded-for', 'x-forwarded-host', 'x-forwarded-proto',
        }
        blocked.update(x.strip().lower() for x in request.headers.get('Connection', '').split(','))
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in blocked and not k.lower().startswith('sec-websocket-')
        }
        headers['Host'] = urlsplit(UPSTREAMS[target]).netloc
        if origin:
            headers['Origin'] = UPSTREAMS[target]
        headers['X-Forwarded-Prefix'] = prefix.rstrip('/')
        # Namespace application cookies independently of HA.
        cookies = {
            k[len('app_' + target + '_'):]: v for k, v in request.cookies.items()
            if k.startswith('app_' + target + '_')
        }
        if cookies:
            jar = SimpleCookie()
            for name, value in cookies.items():
                jar[name] = value
            headers['Cookie'] = '; '.join(m.OutputString() for m in jar.values())
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            return await self.websocket(request, url, headers, token, target)
        try:
            upstream = await self.client.request(
                request.method, url, headers=headers,
                data=request.content.iter_chunked(65536), allow_redirects=False,
            )
        except UPSTREAM_ERRORS as err:
            raise web.HTTPBadGateway() from err
        response = None
        try:
            out = {
                k: v for k, v in upstream.headers.items()
                if k.lower() not in HOP | {
                    'set-cookie', 'content-encoding', 'content-security-policy',
                    'x-frame-options', 'etag', 'service-worker-allowed',
                }
            }
            out.update({
                'Cache-Control': 'no-store, private',
                'Content-Security-Policy': "frame-ancestors 'self'",
                'Referrer-Policy': 'same-origin',
                # A proxied worker must never claim HA's root scope.
                'Service-Worker-Allowed': prefix,
            })
            if loc := out.get('Location'):
                if loc.startswith('/') and not loc.startswith('//'):
                    out['Location'] = prefix + loc.lstrip('/')
                elif loc.startswith(UPSTREAMS[target] + '/'):
                    out['Location'] = prefix + loc[len(UPSTREAMS[target]) + 1:]
            content_type = upstream.headers.get('Content-Type', '').lower()
            transform = any(x in content_type for x in ('text/html', 'javascript', 'text/css'))
            if transform:
                bridge = self.bridge.replace('__PREFIX__', json.dumps(prefix)).replace('__TARGET__', json.dumps(target))
                body = rewrite(await upstream.text(), prefix, content_type, bridge)
                response = web.Response(body=body.encode(), status=upstream.status, headers=out)
            else:
                response = web.StreamResponse(status=upstream.status, headers=out)
            for raw in upstream.headers.getall('Set-Cookie', []):
                jar = SimpleCookie()
                jar.load(raw)
                for name, morsel in jar.items():
                    response.set_cookie(
                        'app_' + target + '_' + name, morsel.value,
                        path=prefix, secure=request.secure,
                        httponly=bool(morsel['httponly']), samesite='Strict',
                        max_age=morsel['max-age'] or None,
                        expires=morsel['expires'] or None,
                    )
            if not transform:
                await response.prepare(request)

                async def stream():
                    async for chunk in upstream.content.iter_chunked(65536):
                        await response.write(chunk)

                if 'text/event-stream' in content_type:
                    await self.relay_until_revoked(token, target, stream())
                else:
                    await stream()
                await response.write_eof()
            return response
        except UPSTREAM_ERRORS as err:
            if response is None or not response.prepared:
                raise web.HTTPBadGateway() from err
            # Headers are already sent: abort instead of appending a 502 or
            # marking a truncated upstream body as a successful response.
            response.force_close()
            if request.transport is not None:
                request.transport.close()
            return response
        finally:
            upstream.close()

    async def websocket(self, request, url, headers, token, target):
        protocols = [
            s.strip() for s in request.headers.get('Sec-WebSocket-Protocol', '').split(',')
            if s.strip()
        ]
        local = None
        try:
            async with self.client.ws_connect(url, headers=headers, protocols=protocols) as remote:
                local = web.WebSocketResponse(protocols=protocols)
                await local.prepare(request)

                async def pump(src, dst):
                    async for msg in src:
                        if msg.type == WSMsgType.TEXT:
                            await dst.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await dst.send_bytes(msg.data)
                        elif msg.type == WSMsgType.ERROR:
                            raise src.exception() or ClientError('WebSocket relay failed')
                    if src.exception() is not None:
                        raise src.exception()
                    if src.close_code == 1006:
                        raise ClientError('WebSocket closed without a close frame')

                try:
                    revoked = await self.relay_until_revoked(
                        token, target, pump(local, remote), pump(remote, local),
                    )
                    await local.close(code=1008 if revoked else 1000)
                except UPSTREAM_ERRORS:
                    await local.close(code=1011)
                    raise
                finally:
                    await local.close()
                return local
        except UPSTREAM_ERRORS as err:
            if local is None or not local.prepared:
                raise web.HTTPBadGateway() from err
            await local.close(code=1011)
            return local

    get = post = put = patch = delete = head = handle
