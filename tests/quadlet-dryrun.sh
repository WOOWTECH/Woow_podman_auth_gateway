#!/usr/bin/env bash
# tests/quadlet-dryrun.sh: generate the systemd units the way a rootful host does, for the
# H618 base and for base + H4G drop-in, and check the gateway arguments each one ends up with.
# Starts no container. Needs podman's quadlet generator (apt podman 4.9.3 in CI).
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
QUADLET=${QUADLET:-/usr/libexec/podman/quadlet}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# generate <name> [drop-in dir]: prints the generated woow-auth-gateway.service
generate() {
  local dir=$WORK/$1/units out=$WORK/$1/out
  mkdir -p "$dir" "$out"
  cp "$REPO/quadlet/woow-auth-gateway.container" "$dir/"
  [[ -z ${2:-} ]] || cp -r "$2" "$dir/"
  QUADLET_UNIT_DIRS=$dir "$QUADLET" -no-kmsg-log "$out" >&2
  cat "$out/woow-auth-gateway.service"
}

# expect_exec <unit text> <expected trailing args>
expect_exec() {
  local exec
  exec=$(sed -n 's/^ExecStart=//p' <<<"$1")
  echo "ExecStart=$exec"
  case "$exec" in
    *" ghcr.io/woowtech/woow-auth-gateway:"*" $2") ;;
    *) echo "expected the image followed by '$2'" >&2; return 1 ;;
  esac
  for want in --network host --secret "woow-ingress-secret,type=mount,target=ingress-secret,uid=10001,gid=10001,mode=0400" --read-only; do
    grep -qF -- "$want" <<<"$exec" || { echo "missing $want" >&2; return 1; }
  done
}

echo "== base (Tailscale Web only)"
base=$(generate base)
expect_exec "$base" "--pi-port 0"
grep -qx 'After=network-online.target' <<<"$base" || { echo "base: After=network-online.target missing" >&2; exit 1; }
grep -qx 'Wants=network-online.target' <<<"$base" || { echo "base: Wants=network-online.target missing" >&2; exit 1; }

echo "== base + H4G drop-in (Tailscale Web and Pi Agent)"
h4g=$(generate h4g "$REPO/quadlet/overlays/h4g/woow-auth-gateway.container.d")
expect_exec "$h4g" "--pi-port 30142 --pi-upstream http://127.0.0.1:30141"

echo "ok"
