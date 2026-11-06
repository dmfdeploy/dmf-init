#!/usr/bin/env bash
# Tests the install command documented in README.md, so the docs and the test
# can't drift apart. The command is extracted from the README, its release URL
# is pointed at a local directory, and it runs with no network.
#
#   test/launcher/test-install-command.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1" >&2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Pull the fenced block between the README markers.
awk '/install-command:start/{on=1;next} /install-command:end/{on=0} on && !/^```/' \
  "$ROOT/README.md" > "$WORK/cmd.raw"
[ -s "$WORK/cmd.raw" ] || { echo "no install command found in README.md" >&2; exit 1; }

# Documented command must pin a tag, not a branch.
if grep -qE '^version=v[0-9]+\.[0-9]+\.[0-9]+' "$WORK/cmd.raw" && ! grep -q '/main' "$WORK/cmd.raw"; then
  ok "command pins a version tag and never names main"
else
  bad "command must pin a version tag and never name main"
fi

# Redirect the release URL to a local release dir; replace the launcher run with
# a marker so a pass is visible and nothing real starts.
REL="$WORK/release"
sed -e "s|https://github.com/dmfdeploy/dmf-init/releases/download/|file://${REL}/|" \
    -e 's|bash dmf-init up|echo LAUNCHED > ../ran|' "$WORK/cmd.raw" > "$WORK/cmd.sh"
grep -q "file://${REL}/" "$WORK/cmd.sh" || { echo "URL rewrite failed" >&2; exit 1; }

sha() { if command -v sha256sum >/dev/null; then sha256sum "$@"; else shasum -a 256 "$@"; fi; }

# $1 label, $2 expected exit (0 = runs, 1 = refuses), $3 setup function name
run_case() {
  local label="$1" want="$2" setup="$3" rc=0
  rm -rf "$REL" "$WORK/ran" "$WORK/run"
  mkdir -p "$REL/v0.3.5" "$WORK/run/sub"
  printf '#!/usr/bin/env bash\necho hello\n' > "$REL/v0.3.5/dmf-init"
  "$setup"
  ( cd "$WORK/run/sub" && bash "$WORK/cmd.sh" ) >"$WORK/out" 2>&1 || rc=$?
  if [ "$want" = 0 ]; then
    if [ "$rc" = 0 ] && [ -f "$WORK/run/ran" ]; then ok "$label"; else bad "$label (rc=$rc)"; cat "$WORK/out" >&2; fi
  else
    if [ "$rc" != 0 ] && [ ! -f "$WORK/run/ran" ]; then ok "$label"; else bad "$label (rc=$rc, launcher ran: $([ -f "$WORK/run/ran" ] && echo yes || echo no))"; cat "$WORK/out" >&2; fi
  fi
}

good_sums()  { ( cd "$REL/v0.3.5" && sha dmf-init > SHA256SUMS ); }
wrong_sums() { ( cd "$REL/v0.3.5" && printf '%064d  dmf-init\n' 0 > SHA256SUMS ); }
empty_sums() { : > "$REL/v0.3.5/SHA256SUMS"; }
no_sums()    { :; }
tampered()   { good_sums; printf 'echo pwned\n' >> "$REL/v0.3.5/dmf-init"; }

echo "install command:"
run_case "runs the launcher when the checksum matches"      0 good_sums
run_case "refuses on a wrong checksum"                      1 wrong_sums
run_case "refuses when the launcher changed after checksum" 1 tampered
run_case "refuses on an empty checksum file"                1 empty_sums
run_case "refuses when the checksum file is missing"        1 no_sums

echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
