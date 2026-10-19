"""woow_protected_apps in HA 2026.7.2: panels for the enabled targets, and their tickets."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

REPO = Path(__file__).resolve().parents[2]
DOMAIN = "woow_protected_apps"


def _gateway():
    """The gateway program itself: its verify() is what a ticket has to pass."""
    spec = importlib.util.spec_from_file_location(
        "woow_auth_gateway", REPO / "gateway" / "woow_auth_gateway.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look themselves up here
    spec.loader.exec_module(module)
    return module


async def _create_ticket(hass_ws_client, target: str, **client_kwargs) -> dict:
    client = await hass_ws_client(**client_kwargs)
    await client.send_json_auto_id(
        {"type": "woow_protected_apps/create_ticket", "target": target}
    )
    return await client.receive_json()


async def _panels(hass_ws_client) -> dict:
    client = await hass_ws_client()
    await client.send_json_auto_id({"type": "get_panels"})
    msg = await client.receive_json()
    assert msg["success"], msg
    return msg["result"]


async def test_each_enabled_target_is_an_admin_only_sidebar_panel(
    hass: HomeAssistant, secret_file, hass_ws_client
) -> None:
    assert await async_setup_component(
        hass, DOMAIN, {DOMAIN: {"targets": ["pi-agent", "tailscale-web"]}}
    )

    panels = await _panels(hass_ws_client)

    pi = panels["pi-agent"]
    assert pi["component_name"] == "custom"
    assert pi["title"] == "Pi Agent"
    assert pi["icon"] == "mdi:robot"
    assert pi["require_admin"] is True
    assert pi["config"]["target"] == "pi-agent"
    assert pi["config"]["_panel_custom"]["name"] == "woow-protected-app-panel"

    ts = panels["tailscale-web"]
    assert ts["title"] == "Tailscale"
    assert ts["icon"] == "mdi:vpn"
    assert ts["require_admin"] is True
    assert ts["config"]["target"] == "tailscale-web"


async def test_bare_config_enables_tailscale_only(
    hass: HomeAssistant, secret_file, hass_ws_client
) -> None:
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: None})

    panels = await _panels(hass_ws_client)

    assert "tailscale-web" in panels
    assert "pi-agent" not in panels


async def test_a_target_the_component_does_not_know_fails_setup(
    hass: HomeAssistant, secret_file
) -> None:
    assert not await async_setup_component(
        hass, DOMAIN, {DOMAIN: {"targets": ["tailscale-web", "node-red"]}}
    )


async def test_non_admins_do_not_see_the_panels(
    hass: HomeAssistant, secret_file, hass_ws_client, hass_read_only_access_token
) -> None:
    assert await async_setup_component(
        hass, DOMAIN, {DOMAIN: {"targets": ["pi-agent", "tailscale-web"]}}
    )

    client = await hass_ws_client(access_token=hass_read_only_access_token)
    await client.send_json_auto_id({"type": "get_panels"})
    msg = await client.receive_json()

    assert msg["success"], msg
    assert "pi-agent" not in msg["result"]
    assert "tailscale-web" not in msg["result"]


async def test_an_admin_gets_a_ticket_the_gateway_accepts(
    hass: HomeAssistant, secret_file, hass_ws_client, hass_admin_user
) -> None:
    assert await async_setup_component(
        hass, DOMAIN, {DOMAIN: {"targets": ["pi-agent", "tailscale-web"]}}
    )

    msg = await _create_ticket(hass_ws_client, "pi-agent")

    assert msg["success"], msg
    assert msg["result"]["port"] == 30142
    assert msg["result"]["expires_in"] == 45
    gateway = _gateway()
    secret = bytes.fromhex(secret_file.read_text(encoding="utf-8"))
    payload = gateway.verify(secret, msg["result"]["ticket"], "ticket", "pi-agent")
    assert payload is not None
    assert payload["uid"] == hass_admin_user.id
    assert payload["exp"] - payload["iat"] == 45
    assert gateway.verify(secret, msg["result"]["ticket"], "ticket", "tailscale-web") is None


async def test_no_ticket_for_a_target_this_machine_does_not_enable(
    hass: HomeAssistant, secret_file, hass_ws_client
) -> None:
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {"targets": ["tailscale-web"]}})

    msg = await _create_ticket(hass_ws_client, "pi-agent")

    assert not msg["success"]
    assert "ticket" not in (msg.get("result") or {})


async def test_no_ticket_for_a_non_admin(
    hass: HomeAssistant, secret_file, hass_ws_client, hass_read_only_access_token
) -> None:
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: None})

    msg = await _create_ticket(
        hass_ws_client, "tailscale-web", access_token=hass_read_only_access_token
    )

    assert not msg["success"]
    assert msg["error"]["code"] == "unauthorized"


async def test_the_panel_module_is_served_where_the_panels_load_it(
    hass: HomeAssistant, secret_file, hass_ws_client, hass_client
) -> None:
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: None})
    module_url = (await _panels(hass_ws_client))["tailscale-web"]["config"]["_panel_custom"][
        "module_url"
    ]

    resp = await (await hass_client()).get(module_url)

    assert resp.status == 200
    assert 'customElements.define("woow-protected-app-panel"' in await resp.text()
