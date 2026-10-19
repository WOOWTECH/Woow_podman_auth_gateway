#!/usr/bin/env bash
# tests/quadlet-dryrun.sh: generate the systemd units the way a rootful host does, for the
# H618 base and for base + H4G drop-in, and check the gateway arguments each one ends up with.
# Starts no container.
#
# The generator is the hosts' own: podman-static 6.1.1 (arm64), checked by sha256; its quadlet
# is byte-identical to /usr/local/libexec/podman/quadlet on .25. apt podman 4.9.3 is no
# substitute: its quadlet ignores .container.d drop-ins. Set QUADLET=<path> to use another.
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
PODMAN_STATIC_URL=https://github.com/mgoltzsche/podman-static/releases/download/v6.1.1/podman-linux-arm64.tar.gz
PODMAN_STATIC_SHA256=d17b17e034ee04ac416e2a8f3a46ad481ebd3ff7a62f0b90367ecdd51729f616
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

if [[ -z ${QUADLET:-} ]]; then
  curl -sSfL --retry 3 -o "$WORK/podman-static.tgz" "$PODMAN_STATIC_URL"
  echo "$PODMAN_STATIC_SHA256  $WORK/podman-static.tgz" | sha256sum -c --quiet
  tar xzf "$WORK/podman-static.tgz" -C "$WORK" podman-linux-arm64/usr/local/libexec/podman/quadlet
  QUADLET=$WORK/podman-linux-arm64/usr/local/libexec/podman/quadlet
fi
echo "quadlet $("$QUADLET" -version)"

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
  local secret want
  secret=$(sed -n 's/^Secret=//p' "$REPO/quadlet/woow-auth-gateway.container")
  for want in "--network host" "--secret $secret" "--read-only"; do
    grep -qF -- " $want " <<<"$exec " || { echo "missing '$want'" >&2; return 1; }
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
