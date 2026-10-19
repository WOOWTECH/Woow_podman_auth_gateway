#!/usr/bin/env bash
# tests/ha-asset-check.sh TARBALL: check a woow_protected_apps release asset.
#   - every entry is under custom_components/woow_protected_apps/, nothing else
#   - no __pycache__ or compiled files
#   - unpacked, it is byte-identical to ha/custom_components/woow_protected_apps/ (minus caches)
set -euo pipefail
TARBALL=${1:?usage: $0 TARBALL}
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
COMPONENT=custom_components/woow_protected_apps
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

[[ -f $TARBALL ]] || fail "$TARBALL does not exist"

echo "== entries"
tar tzf "$TARBALL" | tee "$WORK/entries"
outside=$(grep -vE "^$COMPONENT(/|$)" "$WORK/entries" || true)
[[ -z $outside ]] || fail "entries outside $COMPONENT/: $outside"
caches=$(grep -E '__pycache__|\.py[cod]$' "$WORK/entries" || true)
[[ -z $caches ]] || fail "compiled files in the asset: $caches"

echo "== same as the repo"
mkdir "$WORK/unpacked"
tar xzf "$TARBALL" -C "$WORK/unpacked"
diff -r --exclude=__pycache__ "$REPO/ha/$COMPONENT" "$WORK/unpacked/$COMPONENT" \
  || fail "the asset differs from ha/$COMPONENT"
for f in __init__.py manifest.json frontend/woow-protected-app-panel.js; do
  [[ -f $WORK/unpacked/$COMPONENT/$f ]] || fail "$f missing"
done

echo "ok"
