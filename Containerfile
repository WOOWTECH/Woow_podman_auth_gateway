# woow-auth-gateway: the HA-ticket check in front of Tailscale Web (and Pi Agent on H4G).
#
# Base: debian:bookworm-slim, pinned to the digest Woow_podman_pi_agent_package uses, with
# Debian's python3-aiohttp (3.8.4 on bookworm), the same runtime the host service had on .25.
# The image is public and carries no secret: the signing key arrives at run time as the
# podman secret woow-ticket-secret, mounted at /run/secrets/ticket-secret (see quadlet/).
#
#   podman build --format docker -t localhost/woow-auth-gateway:dev .

ARG BASE_IMAGE=docker.io/library/debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.source=https://github.com/WOOWTECH/Woow_podman_auth_gateway \
      org.opencontainers.image.description="HA-ticket authenticated reverse proxy for Pi Agent and Tailscale Web on H618 container machines"

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-aiohttp \
 && rm -rf /var/lib/apt/lists/*

COPY gateway/woow_auth_gateway.py /usr/local/lib/woow-auth-gateway/woow_auth_gateway.py

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Not root, and no account needed: the secret is mounted with uid/gid 10001.
USER 10001:10001

# Arguments given to the container (quadlet Exec=) are appended, so they can switch a target
# off (--pi-port 0) or point it elsewhere; a later --secret-file overrides this one.
ENTRYPOINT ["python3", "/usr/local/lib/woow-auth-gateway/woow_auth_gateway.py", "--secret-file", "/run/secrets/ticket-secret"]

# Documentation only: the quadlet runs with Network=host. 8088 Tailscale Web, 30142 Pi Agent.
EXPOSE 8088/tcp 30142/tcp
