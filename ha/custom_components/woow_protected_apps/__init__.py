"""Sidebar panels for the protected WOOW web apps, opened with short-lived admin tickets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import time
from typing import Any, NamedTuple

import voluptuous as vol

from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.config_validation as cv

DOMAIN = "woow_protected_apps"
SECRET_FILE = ".woow_ticket_secret"
PANEL_ELEMENT = "woow-protected-app-panel"
PANEL_URL = f"/{DOMAIN}/{PANEL_ELEMENT}.js"


class Setup(NamedTuple):
    secret: bytes
    targets: list[str]


class Target(NamedTuple):
    port: int
    title: str
    icon: str


# The only name-to-port list on the HA side; the gateway serves the same ports.
TARGETS = {
    "pi-agent": Target(30142, "Pi Agent", "mdi:robot"),
    "tailscale-web": Target(8088, "Tailscale", "mdi:vpn"),
}

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            lambda value: value or {},
            vol.Schema(
                {
                    vol.Optional("targets", default=["tailscale-web"]): vol.All(
                        cv.ensure_list, [vol.In(TARGETS)]
                    )
                }
            ),
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _sign(secret: bytes, payload: dict[str, Any]) -> str:
    body = _b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    signature = _b64encode(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


@callback
@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/create_ticket",
        vol.Required("target"): vol.In(TARGETS),
    }
)
def ws_create_ticket(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Create a one-use launch ticket for an authenticated HA administrator."""
    if msg["target"] not in hass.data[DOMAIN].targets:
        connection.send_error(
            msg["id"], websocket_api.ERR_NOT_FOUND, f"{msg['target']} is not enabled here"
        )
        return
    now = int(time.time())
    payload = {
        "v": 1,
        "kind": "ticket",
        "target": msg["target"],
        "uid": connection.user.id,
        "iat": now,
        "exp": now + 45,
        "jti": secrets.token_urlsafe(24),
    }
    connection.send_result(
        msg["id"],
        {
            "ticket": _sign(hass.data[DOMAIN].secret, payload),
            "port": TARGETS[msg["target"]].port,
            "expires_in": 45,
        },
    )


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Load the shared signing secret, then register the command and one panel per target."""
    secret_path = Path(hass.config.path(SECRET_FILE))
    secret_hex = await hass.async_add_executor_job(secret_path.read_text, "utf-8")
    secret = bytes.fromhex(secret_hex.strip())
    if len(secret) < 32:
        raise ValueError("WOOW ticket secret must contain at least 32 bytes")
    hass.data[DOMAIN] = Setup(secret, config[DOMAIN]["targets"])
    websocket_api.async_register_command(hass, ws_create_ticket)

    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                PANEL_URL, str(Path(__file__).parent / "frontend" / f"{PANEL_ELEMENT}.js"), False
            )
        ]
    )
    for name in hass.data[DOMAIN].targets:
        target = TARGETS[name]
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=name,
            webcomponent_name=PANEL_ELEMENT,
            sidebar_title=target.title,
            sidebar_icon=target.icon,
            module_url=PANEL_URL,
            config={"target": name},
            require_admin=True,
        )
    return True
