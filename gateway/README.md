# HA-authenticated management gateway

Home Assistant administrators obtain a 45-second, one-use launch ticket through `woow_ingress/create_ticket`. The gateway exchanges it for an HttpOnly signed session and proxies Pi Web and Tailscale Web. Raw backends listen only on loopback.

| Application | Protected gateway | Private backend |
|---|---:|---:|
| Pi Web | 30142 | 127.0.0.1:30143 |
| Tailscale Web | 8088 | 127.0.0.1:8089 |

The signing key is `/opt/homeassistant/.woow_ingress_secret`; preserve its mode and never publish it. The HA dashboards use `/local/woow-auth-iframe-card.js`. Validate HA configuration before changing the component or card. The pre-deployment rollback is `/opt/woow-deploy/backups/ha-auth-gateway-20260901T051635Z`.

## Iframe-only enforcement

Public gateway ports return HTTP 404 for direct, top-level, headerless, cross-site, or unsigned requests. A launch is accepted only as an iframe navigation referred by the same-host HA frontend on port 8123 with a valid one-use admin ticket. Signed sessions reject top-level document navigation, and proxied HTML sets `frame-ancestors` to that HA origin.
