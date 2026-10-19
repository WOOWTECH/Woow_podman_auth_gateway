#!/usr/bin/env bash
# tests/container-smoke.sh IMAGE: check a built image the way the hosts will run it.
#   - runs as 10001:10001, is arm64 (EXPECT_ARCH), and carries no secret
#   - the unit tests pass against the image's own python3-aiohttp
#   - with a throwaway test secret, started with the quadlet's Secret= and Exec= (base, then
#     the H4G drop-in): every open port answers 404 without a ticket, a closed port is not
#     listening, and switching both targets off is an error
# Uses host ports 8088 and 30142, so run it on a CI runner, not on a machine that serves them.
set -euo pipefail
IMAGE=${1:?usage: $0 IMAGE}
EXPECT_ARCH=${EXPECT_ARCH:-arm64}
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
QUADLET=$REPO/quadlet/woow-auth-gateway.container
DROPIN=$REPO/quadlet/overlays/h4g/woow-auth-gateway.container.d/pi-agent.conf
SECRET_NAME=woow-ingress-secret
NAME=woow-auth-gateway-smoke

fail() { echo "FAIL: $*" >&2; exit 1; }
last_value() { sed -n "s/^$1=//p" "$2" | tail -n1; }

cleanup() {
  podman rm -f "$NAME" >/dev/null 2>&1 || true
  podman secret rm "$SECRET_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== user and architecture"
read -r user arch < <(podman image inspect --format '{{.Config.User}} {{.Architecture}}' "$IMAGE")
echo "User=$user Architecture=$arch"
[[ $user == 10001:10001 ]] || fail "image user is '$user', expected 10001:10001"
[[ $arch == "$EXPECT_ARCH" ]] || fail "image architecture is '$arch', expected $EXPECT_ARCH"
uid=$(podman run --rm --network none --entrypoint id "$IMAGE" -u)
[[ $uid == 10001 ]] || fail "the container runs as uid $uid"

echo "== base image is pinned by digest"
grep -Eq '^ARG BASE_IMAGE=[^ ]+@sha256:[0-9a-f]{64}$' "$REPO/Containerfile" || fail "Containerfile base is not pinned by digest"

echo "== no secret in the image"
podman run --rm --network none --entrypoint sh "$IMAGE" -c '
  if [ -n "$(ls -A /run/secrets 2>/dev/null)" ]; then echo "/run/secrets is not empty" >&2; exit 1; fi
  found=$(find / -xdev \( -iname "*secret*" -o -name "*.key" -o -name "*.pem" \) \
            -not -path "/proc/*" -not -path "/sys/*" -not -path "/usr/lib/python3*" \
            -not -path "/usr/share/*" -not -path "/etc/ssl/*" 2>/dev/null)
  if [ -n "$found" ]; then echo "secret-looking files in the image:" >&2; echo "$found" >&2; exit 1; fi
' || fail "the image carries a secret"

echo "== unit tests against the image's aiohttp"
podman run --rm --network none --entrypoint python3 "$IMAGE" -c 'import aiohttp; print("aiohttp", aiohttp.__version__)'
podman run --rm --network none --user 10001:10001 -v "$REPO:/src:ro" --entrypoint python3 "$IMAGE" \
  -m unittest discover -s /src/tests -v

echo "== with a test secret"
podman secret rm "$SECRET_NAME" >/dev/null 2>&1 || true
python3 -c 'import secrets; print(secrets.token_hex(32))' | podman secret create "$SECRET_NAME" - >/dev/null
secret_opt=$(last_value Secret "$QUADLET")

listening() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
wait_listening() {
  for _ in $(seq 50); do listening "$1" && return 0; sleep 0.2; done
  podman logs "$NAME" >&2 || true
  fail "nothing listens on :$1"
}
expect_404() {
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$1/")
  [[ $code == 404 ]] || fail ":$1 answered $code without a ticket, expected 404"
  echo ":$1 -> 404 without a ticket"
}

# run_variant <label> <exec args from a quadlet file> <open ports> -- <closed ports>
run_variant() {
  local label=$1 exec_args=$2
  shift 2
  echo "-- $label: Exec=$exec_args"
  podman rm -f "$NAME" >/dev/null 2>&1 || true
  # shellcheck disable=SC2086  # Exec= is a word list, as quadlet splits it
  podman run -d --name "$NAME" --network host --read-only --secret "$secret_opt" "$IMAGE" $exec_args >/dev/null
  local closed=0
  for port in "$@"; do
    if [[ $port == -- ]]; then closed=1; continue; fi
    if ((closed)); then
      listening "$port" && fail ":$port is listening, expected closed"
      echo ":$port is not listening"
    else
      wait_listening "$port"
      expect_404 "$port"
    fi
  done
  podman rm -f "$NAME" >/dev/null
}

run_variant base "$(last_value Exec "$QUADLET")" 8088 -- 30142
run_variant h4g "$(last_value Exec "$DROPIN")" 8088 30142 --

echo "-- both targets off"
if podman run --rm --network host --secret "$secret_opt" "$IMAGE" --pi-port 0 --tailscale-port 0; then
  fail "both targets off did not exit with an error"
fi
echo "both off -> error exit"

echo "ok"
