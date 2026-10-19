"""Issue short-lived, admin-only launch tickets for protected WOOW web apps."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

DOMAIN = "woow_ingress"
SECRET_FILE = ".woow_ingress_secret"
TARGETS = {"pi-agent": 30142, "tailscale-web": 8088}


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
        vol.Required("type"): "woow_ingress/create_ticket",
        vol.Required("target"): vol.In(TARGETS),
    }
)
def ws_create_ticket(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Create a one-use launch ticket for an authenticated HA administrator."""
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
            "ticket": _sign(hass.data[DOMAIN], payload),
            "port": TARGETS[msg["target"]],
            "expires_in": 45,
        },
    )


async def async_setup(hass: HomeAssistant, _config: dict[str, Any]) -> bool:
    """Load the shared signing secret and register the WebSocket command."""
    secret_path = Path(hass.config.path(SECRET_FILE))
    secret_hex = await hass.async_add_executor_job(secret_path.read_text, "utf-8")
    secret = bytes.fromhex(secret_hex.strip())
    if len(secret) < 32:
        raise ValueError("WOOW ingress secret must contain at least 32 bytes")
    hass.data[DOMAIN] = secret
    websocket_api.async_register_command(hass, ws_create_ticket)
    return True
