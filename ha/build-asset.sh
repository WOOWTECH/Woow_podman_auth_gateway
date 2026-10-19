#!/usr/bin/env bash
# ha/build-asset.sh TAG OUTDIR: pack the HA component as the release asset
# OUTDIR/woow_protected_apps-TAG.tar.gz, which unpacks to custom_components/woow_protected_apps/.
# restore.sh installs it into /opt/homeassistant after checking the sha256 the release records.
#
# Reproducible: entries sorted, owner root, mtime of the last commit, gzip without a timestamp,
# so the same commit gives the same sha256.
set -euo pipefail
TAG=${1:?usage: $0 TAG OUTDIR}
OUTDIR=${2:?usage: $0 TAG OUTDIR}
HA=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
mtime=$(git -C "$HA" log -1 --format=%ct 2>/dev/null || echo 0)
out=$OUTDIR/woow_protected_apps-$TAG.tar.gz

mkdir -p "$OUTDIR"
tar -C "$HA" --sort=name --mtime="@$mtime" --owner=0 --group=0 --numeric-owner \
  --mode='u=rwX,go=rX' --exclude=__pycache__ --exclude='*.py[cod]' \
  -cf - custom_components/woow_protected_apps | gzip -n -9 > "$out"
echo "$out"
