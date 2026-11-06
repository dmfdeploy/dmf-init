#!/usr/bin/env bash
# Tests scripts/build-launcher-release.sh with no network. The checks run on
# the files the script leaves in the output directory, which are the files the
# release workflow attaches. If a change stamps the launcher after it writes the
# checksum, SHA256SUMS no longer matches dmf-init and this test fails.
#
#   test/launcher/test-build-launcher-release.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="$ROOT/scripts/build-launcher-release.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1" >&2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

DIGEST="sha256:$(printf 'a%.0s' $(seq 64))"
REF="ghcr.io/dmfdeploy/dmf-init:0.4.0@${DIGEST}"
OUT="$WORK/out"

digest_of() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d ' ' -f 1; }

if "$BUILD" "$REF" "$OUT" >"$WORK/log" 2>&1; then
  ok "build succeeds"
else
  bad "build failed"; cat "$WORK/log" >&2
fi

if [ "$(find "$OUT" -type f | wc -l | tr -d ' ')" = 2 ] && [ -f "$OUT/dmf-init" ] && [ -f "$OUT/SHA256SUMS" ]; then ok "output holds exactly dmf-init and SHA256SUMS"; else bad "unexpected output files"; fi

if grep -qFx "DEFAULT_IMAGE=\"${REF}\"" "$OUT/dmf-init"; then ok "published launcher carries the stamped ref"; else bad "stamped ref missing from the published launcher"; fi
if ! grep -q ':latest"$' <(grep '^DEFAULT_IMAGE=' "$OUT/dmf-init"); then ok "published launcher is no longer :latest"; else bad "still :latest"; fi

if [ "$(grep -c '' "$OUT/SHA256SUMS")" = 1 ] && grep -qE '^[0-9a-f]{64}  dmf-init$' "$OUT/SHA256SUMS"; then ok "SHA256SUMS is one line in the documented form"; else bad "SHA256SUMS form"; fi

want="$(cut -d ' ' -f 1 "$OUT/SHA256SUMS")"
if [ "$(digest_of "$OUT/dmf-init")" = "$want" ]; then ok "SHA256SUMS matches the final published bytes"; else bad "SHA256SUMS does not match the published launcher"; fi

# The checksum of the unstamped launcher must differ, so the check above can
# only pass when the stamp came first.
if [ "$(digest_of "$ROOT/bin/dmf-init")" != "$want" ]; then ok "checksum is not that of the unstamped launcher"; else bad "checksum covers the unstamped launcher"; fi

# A bad ref publishes nothing usable: the script fails and writes no checksum.
rm -rf "$WORK/bad"
if ! "$BUILD" "ghcr.io/dmfdeploy/dmf-init:latest@nope" "$WORK/bad" >/dev/null 2>&1 && [ ! -e "$WORK/bad/SHA256SUMS" ]; then ok "bad image ref fails before any checksum is written"; else bad "bad ref accepted or checksum written"; fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ]
