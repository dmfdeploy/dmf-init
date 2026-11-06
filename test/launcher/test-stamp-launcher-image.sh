#!/usr/bin/env bash
# Tests scripts/stamp-launcher-image.sh with no network: the stamp changes
# exactly one line of the real launcher, and refuses a missing or duplicated
# marker or a malformed image ref.
#
#   test/launcher/test-stamp-launcher-image.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STAMP="$ROOT/scripts/stamp-launcher-image.sh"
LAUNCHER="$ROOT/bin/dmf-init"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1" >&2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

MARKER='DEFAULT_IMAGE="ghcr.io/dmfdeploy/dmf-init:latest"'

# The repo launcher carries the marker exactly once.
if [ "$(grep -cFx "$MARKER" "$LAUNCHER")" = 1 ]; then ok "launcher has the marker once"; else bad "launcher marker count"; fi

D64="$(printf 'b%.0s' $(seq 64))"
REPO=ghcr.io/dmfdeploy/dmf-init
# Happy path: one changed line, exactly the ref we pass.
cp "$LAUNCHER" "$WORK/l"
if "$STAMP" "$WORK/l" "$REPO:0.4.0@sha256:$D64" >"$WORK/out" 2>&1; then
  changed="$(diff "$LAUNCHER" "$WORK/l" | grep -c '^>' || true)"
  removed="$(diff "$LAUNCHER" "$WORK/l" | grep -c '^<' || true)"
  if [ "$changed" = 1 ] && [ "$removed" = 1 ]; then ok "stamp changes exactly one line"; else bad "changed=$changed removed=$removed"; fi
  if grep -qFx "DEFAULT_IMAGE=\"$REPO:0.4.0@sha256:$D64\"" "$WORK/l"; then ok "stamped image ref is tag plus digest"; else bad "stamped ref wrong"; fi
  if ! grep -q '^DEFAULT_IMAGE=.*:latest' "$WORK/l"; then ok "default image is no longer :latest"; else bad "default image still :latest"; fi
  if [ -x "$LAUNCHER" ] && cmp -s <(tail -n +2 "$LAUNCHER" | grep -vF DEFAULT_IMAGE=) <(tail -n +2 "$WORK/l" | grep -vF DEFAULT_IMAGE=); then ok "rest of the file is byte-identical"; else bad "rest of file differs"; fi
  # --image must still override: IMAGE starts from DEFAULT_IMAGE and the parser sets it.
  if grep -q -- '--image' "$WORK/l" && grep -q "IMAGE=\"\$DEFAULT_IMAGE\"" "$WORK/l"; then ok "--image override path intact"; else bad "--image override path missing"; fi
else
  bad "stamp failed on the real launcher"; cat "$WORK/out" >&2
fi

# Missing marker.
printf '#!/usr/bin/env bash\necho hi\n' > "$WORK/none"
if ! "$STAMP" "$WORK/none" "$REPO:0.4.0" >/dev/null 2>&1; then ok "fails when marker is missing"; else bad "accepted a missing marker"; fi

# Duplicated marker.
{ cat "$LAUNCHER"; printf '%s\n' "$MARKER"; } > "$WORK/dup"
cp "$WORK/dup" "$WORK/dup.orig"
if ! "$STAMP" "$WORK/dup" "$REPO:0.4.0" >/dev/null 2>&1; then ok "fails when marker is duplicated"; else bad "accepted a duplicated marker"; fi
if cmp -s "$WORK/dup" "$WORK/dup.orig"; then ok "failed stamp leaves the file untouched"; else bad "file modified on failure"; fi

# Tag-only and digest-only refs are accepted.
for r in "$REPO:0.4.0" "$REPO@sha256:$D64"; do
  cp "$LAUNCHER" "$WORK/t"
  if "$STAMP" "$WORK/t" "$r" >/dev/null 2>&1 && grep -qFx "DEFAULT_IMAGE=\"$r\"" "$WORK/t"; then ok "accepts '$r'"; else bad "rejected '$r'"; fi
done

# Malformed refs leave the file untouched.
for r in '' "$REPO" "$REPO:" "$REPO@sha256:abc" "$REPO@sha256:${D64}0" "$REPO@sha256:$(printf %s "$D64" | tr a-f A-F)" "$REPO:0.4.0@md5:$D64" \
         "other.io/dmfdeploy/dmf-init:0.4.0" "$REPO:0.4.0 extra" "$REPO:a\"b" 'v0.4.0'; do
  cp "$LAUNCHER" "$WORK/t"
  if ! "$STAMP" "$WORK/t" "$r" >/dev/null 2>&1 && cmp -s "$LAUNCHER" "$WORK/t"; then ok "rejects ref '$r'"; else bad "accepted ref '$r'"; fi
done

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ]
