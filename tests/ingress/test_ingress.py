"""HTTP/WS integration tests with real aiohttp transport and a fake HA user store."""
import asyncio
from contextlib import asynccontextmanager
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
import aiohttp
from aiohttp import web
from yarl import URL
import pytest

ROOT = Path(__file__).resolve().parents[2]
for name in (
    'homeassistant', 'homeassistant.components',
    'homeassistant.components.http', 'homeassistant.const',
):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules['homeassistant.components.http'].HomeAssistantView = object
sys.modules['homeassistant.const'].EVENT_HOMEASSISTANT_STOP = 'stop'
spec = importlib.util.spec_from_file_location(
    'woow_ingress', ROOT / 'ha/custom_components/woow_protected_apps/ingress.py',
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


async def serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    return runner, 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])


@pytest.mark.asyncio
async def test_auth_isolation_streams_and_websockets(monkeypatch):
    seen = []

    async def backend(request):
        seen.append(dict(request.headers))
        if request.path == '/ws':
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for message in ws:
                await ws.send_str(message.data)
            return ws
        if request.path == '/stream':
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            await response.write(b'data: first\n\n')
            await asyncio.sleep(0.05)
            await response.write(b'data: second\n\n')
            return response
        if request.path == '/redirect':
            raise web.HTTPFound('/login')
        return web.json_response({'method': request.method, 'body': await request.text()})

    async with proxy(backend, monkeypatch) as env:
        origin = env.base.removesuffix(env.prefix)
        # The second target uses the same backend, but needs its own session.
        env.ingress.targets.append('tailscale-web')
        monkeypatch.setitem(m.UPSTREAMS, 'tailscale-web', m.UPSTREAMS['pi-agent'])
        async with aiohttp.ClientSession() as anonymous:
            assert (await anonymous.get(env.base)).status == 401
            assert (await anonymous.get(origin + m.BASE + '/unknown/')).status == 404
            env.user.is_admin = False
            assert (await anonymous.get(origin + env.ingress.issue('pi-agent', 'regular'))).status == 401
            env.user.is_admin = True
        ticket = env.ingress.issue('pi-agent', 'admin')
        async with env.client.get(origin + ticket, allow_redirects=False) as response:
            assert response.status == 303
            assert 'HttpOnly' in response.headers['Set-Cookie']
            assert 'SameSite=Strict' in response.headers['Set-Cookie']
        assert (await env.client.get(origin + ticket, allow_redirects=False)).status == 401
        assert (await env.client.get(origin + m.BASE + '/tailscale-web/')).status == 401
        assert (await env.client.post(env.base, headers={'Origin': 'https://evil.test'}, data='x')).status == 403
        async with env.client.post(
            env.base, headers={'Origin': origin, 'Authorization': 'Bearer HA-SECRET'},
            data='payload',
        ) as response:
            assert (await response.json())['body'] == 'payload'
        assert 'Authorization' not in seen[-1]
        assert 'woow_ingress' not in seen[-1].get('Cookie', '')
        async with env.client.get(env.base + 'redirect', allow_redirects=False) as response:
            assert response.headers['Location'] == env.prefix + 'login'
        async with env.client.get(env.base + 'stream') as response:
            assert b'second' in await response.read()
        async with env.client.ws_connect(env.base + 'ws', headers={'Origin': origin}) as ws:
            await ws.send_str('roundtrip')
            assert (await ws.receive()).data == 'roundtrip'
        env.user.is_active = False
        assert (await env.client.get(env.base)).status == 401


def test_rewriting_preserves_javascript_regex_and_external_urls():
    prefix = '/api/woow_protected_apps/pi-agent/'
    source = r'let re=/foo\/bar/;var x="/api/home";e.p="/";'
    result = m.rewrite(source, prefix, 'application/javascript')
    assert r'/foo\/bar/' in result and '"/api/home"' in result
    assert 'e.p="' + prefix + '"' in result
    html = '<head></head><script src="/_next/a.js"></script><a href="https://example.com/a">x</a>'
    result = m.rewrite(html, prefix, 'text/html', 'window.bridge=true;')
    assert prefix + '_next/a.js' in result
    assert 'https://example.com/a' in result
    assert 'window.bridge' in result


# These regressions use real HTTP framing and cookie jars, not mocked transports.
@asynccontextmanager
async def proxy(backend, monkeypatch, *, interval=0.02):
    backend_app = web.Application()
    backend_app.router.add_route('*', '/{path:.*}', backend)
    backend_runner, backend_url = await serve(backend_app)
    monkeypatch.setattr(m, 'UPSTREAMS', {'pi-agent': backend_url})
    user = SimpleNamespace(is_active=True, is_admin=True)

    async def get_user(_uid):
        return user

    hass = SimpleNamespace(
        auth=SimpleNamespace(async_get_user=get_user),
        bus=SimpleNamespace(async_listen_once=lambda *args: None),
    )
    ingress = m.Ingress(hass, ['pi-agent'], '', session_check_interval=interval)

    async def route(request):
        return await ingress.handle(
            request, request.match_info['target'], request.match_info['path'],
        )

    app = web.Application()
    app.router.add_route('*', m.BASE + '/{target}/{path:.*}', route)
    runner, origin = await serve(app)
    client = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    prefix = m.BASE + '/pi-agent/'
    try:
        async with client.get(
            origin + ingress.issue('pi-agent', 'admin'), allow_redirects=False,
        ) as response:
            assert response.status == 303
            token = response.cookies['woow_ingress_pi_agent'].value
        yield SimpleNamespace(
            ingress=ingress, client=client, base=origin + prefix,
            prefix=prefix, token=token, user=user,
        )
    finally:
        await client.close()
        await ingress.client.close()
        await runner.cleanup()
        await backend_runner.cleanup()


@pytest.mark.asyncio
async def test_encoded_paths_and_queries_are_forwarded_verbatim(monkeypatch):
    seen = []

    async def backend(request):
        seen.append(request.raw_path)
        return web.Response(text='ok')

    async with proxy(backend, monkeypatch) as env:
        paths = [
            'files/a%23b?name=a%23b',
            'files/CF%20%E6%B8%AC%E8%A9%A6%20%23%20%2B%20%2525.txt',
            'files/a%252Fb?name=a%252Fb',
            'files/a%3Fb?name=a%2Bb&name=a+b&empty=&q=a%26b',
            'files/a%2Fb?x=%25&x=%2f',
            'https://evil.example/file?next=//evil.example/',
        ]
        for path in paths:
            async with env.client.get(URL(env.base + path, encoded=True)) as response:
                assert response.status == 200
                assert await response.text() == 'ok'
        assert seen == ['/' + path for path in paths]
        for path in ['%2e%2e/secret', 'a/%2E/secret', 'a/%2e%2e/secret', '%2f%2fevil.example/']:
            async with env.client.get(URL(env.base + path, encoded=True)) as response:
                assert response.status == 400
        assert len(seen) == len(paths)


@pytest.mark.asyncio
async def test_cookie_lifetime_and_deletion_survive_namespacing(monkeypatch):
    expires = 'Wed, 09 Jun 2038 10:18:14 GMT'

    async def backend(request):
        response = web.Response(text='ok')
        if request.path == '/delete':
            response.del_cookie('auth')
        else:
            response.set_cookie('auth', 'secret', max_age=600, expires=expires, httponly=True)
        return response

    async with proxy(backend, monkeypatch) as env:
        async with env.client.get(env.base) as response:
            cookie = response.cookies['app_pi-agent_auth']
            assert cookie['max-age'] == '600'
            assert cookie['expires'] == expires
            assert cookie['path'] == env.prefix
            assert cookie['httponly']
            assert cookie['samesite'] == 'Strict'
        assert env.client.cookie_jar.filter_cookies(URL(env.base))['app_pi-agent_auth'].value == 'secret'
        async with env.client.get(env.base + 'delete') as response:
            cookie = response.cookies['app_pi-agent_auth']
            assert cookie['max-age'] == '0'
            assert cookie['expires'] == 'Thu, 01 Jan 1970 00:00:00 GMT'
        assert 'app_pi-agent_auth' not in env.client.cookie_jar.filter_cookies(URL(env.base))


@pytest.mark.asyncio
@pytest.mark.parametrize('allowed', [None, '/', 'https://other.example/'])
async def test_worker_scope_is_capped_to_ingress(monkeypatch, allowed):
    async def backend(request):
        headers = {} if allowed is None else {'service-worker-allowed': allowed}
        return web.Response(text='/* worker */', content_type='application/javascript', headers=headers)

    async with proxy(backend, monkeypatch) as env:
        async with env.client.get(env.base + 'sw.js') as response:
            assert response.status == 200
            assert response.headers.getall('Service-Worker-Allowed') == [env.prefix]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['disconnect', 'html-payload', 'ws-handshake'])
async def test_upstream_failures_before_response_become_502(monkeypatch, kind):
    async def backend(request):
        if kind == 'disconnect':
            request.transport.close()
            return web.Response()
        if kind == 'ws-handshake':
            return web.Response(status=403)
        response = web.StreamResponse(headers={'Content-Type': 'text/html', 'Content-Length': '100'})
        await response.prepare(request)
        await response.write(b'<head>')
        request.transport.close()
        return response

    async with proxy(backend, monkeypatch) as env:
        if kind == 'ws-handshake':
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                await env.client.ws_connect(env.base + 'ws')
            assert exc.value.status == 502
        else:
            async with env.client.get(env.base) as response:
                assert response.status == 502


@pytest.mark.asyncio
async def test_truncated_stream_aborts_without_appending_an_error_page(monkeypatch):
    async def backend(request):
        response = web.StreamResponse(headers={'Content-Type': 'application/octet-stream', 'Content-Length': '100'})
        await response.prepare(request)
        await response.write(b'partial')
        request.transport.close()
        return response

    async with proxy(backend, monkeypatch) as env:
        async with env.client.get(env.base) as response:
            assert response.status == 200
            with pytest.raises(aiohttp.ClientPayloadError):
                await response.read()


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['ws', 'sse'])
@pytest.mark.parametrize('reason', ['expiry', 'logout', 'inactive', 'demoted'])
async def test_idle_streams_close_on_expiry_or_revocation(monkeypatch, transport, reason):
    finish_backend = asyncio.Event()

    async def backend(request):
        if transport == 'ws':
            response = web.WebSocketResponse()
            await response.prepare(request)
            async for message in response:
                await response.send_str(message.data)
            return response
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await response.prepare(request)
        await response.write(b'data: ready\n\n')
        await finish_backend.wait()
        return response

    # Expiry must close well before the periodic interval, including idle streams.
    interval = 10 if reason == 'expiry' else 0.02
    async with proxy(backend, monkeypatch, interval=interval) as env:
        if reason == 'expiry':
            env.ingress.sessions[env.token] = ('pi-agent', 'admin', m.time.monotonic() + 0.2)
        try:
            if transport == 'ws':
                response = await env.client.ws_connect(env.base + 'ws')
                await response.send_str('ready')
                assert (await response.receive()).data == 'ready'
            else:
                response = await env.client.get(env.base + 'stream')
                assert await response.content.readexactly(13) == b'data: ready\n\n'
            if reason == 'logout':
                async with env.client.post(env.base + '_logout') as logout:
                    assert logout.status == 204
            elif reason == 'inactive':
                env.user.is_active = False
            elif reason == 'demoted':
                env.user.is_admin = False
            if transport == 'ws':
                message = await asyncio.wait_for(response.receive(), 1)
                assert message.type == aiohttp.WSMsgType.CLOSE
                assert message.data == 1008
                await response.close()
            else:
                assert await asyncio.wait_for(response.content.read(), 1) == b''
                response.close()
        finally:
            finish_backend.set()


@pytest.mark.asyncio
async def test_websocket_preserves_encoded_url_and_reports_relay_failure(monkeypatch):
    seen = []

    async def backend(request):
        seen.append(request.raw_path)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        message = await ws.receive()
        assert message.data == 'disconnect'
        request.transport.close()
        return ws

    async with proxy(backend, monkeypatch) as env:
        path = 'ws/a%23b?name=a%252Fb&name=a%2Bb'
        async with env.client.ws_connect(URL(env.base + path, encoded=True)) as ws:
            await ws.send_str('disconnect')
            message = await asyncio.wait_for(ws.receive(), 1)
            assert message.type == aiohttp.WSMsgType.CLOSE
            assert message.data == 1011
        assert seen == ['/' + path]
