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

# Redirect the release URL to a local release dir. The fixture launchers write
# a marker file, so a pass is visible and nothing real starts.
REL="$WORK/release"
sed -e "s|https://github.com/dmfdeploy/dmf-init/releases/download/|file://${REL}/|" \
  "$WORK/cmd.raw" > "$WORK/cmd.sh"
grep -q "file://${REL}/" "$WORK/cmd.sh" || { echo "URL rewrite failed" >&2; exit 1; }

# The fixture release directory follows the version the README pins.
VERSION="$(sed -n 's/^version=\(v[0-9][0-9.]*\).*/\1/p' "$WORK/cmd.raw" | head -n 1)"
[ -n "$VERSION" ] || { echo "no version= line in the install command" >&2; exit 1; }
DIR="$REL/$VERSION"

# Two PATH directories of symlinks, so the command sees only one checksum tool.
# Everything else the command needs is linked into both.
mkbin() {
  local dir="$1" tool; shift
  mkdir -p "$dir"
  for tool in bash curl sed grep cut mktemp rm cat wc env perl dirname uname "$@"; do
    ln -sf "$(command -v "$tool")" "$dir/$tool"
  done
}
HAVE_SHA256SUM=0; command -v sha256sum >/dev/null && HAVE_SHA256SUM=1
HAVE_SHASUM=0; command -v shasum >/dev/null && HAVE_SHASUM=1
[ "$HAVE_SHA256SUM" = 1 ] && mkbin "$WORK/bin-sha256sum" sha256sum
[ "$HAVE_SHASUM" = 1 ] && mkbin "$WORK/bin-shasum" shasum

sha() { if command -v sha256sum >/dev/null; then sha256sum "$@"; else shasum -a 256 "$@"; fi; }

export RAN_FILE="$WORK/ran"
export TMPDIR="$WORK/tmp"; mkdir -p "$TMPDIR"

# shellcheck disable=SC2016  # $RAN_FILE expands when the fixture runs
fixture_launcher() { printf '#!/usr/bin/env bash\necho LAUNCHED > "$RAN_FILE"\n'; }

# $1 tool label, $2 PATH dir, $3 case label, $4 expected (0 = runs, 1 = refuses), $5 setup function
run_case() {
  local tool="$1" bindir="$2" label="$3" want="$4" setup="$5" rc=0
  rm -rf "$REL" "$RAN_FILE" "$WORK/run"
  mkdir -p "$DIR" "$WORK/run/sub"
  fixture_launcher > "$DIR/dmf-init"
  "$setup"
  ( cd "$WORK/run/sub" && PATH="$bindir" bash "$WORK/cmd.sh" ) >"$WORK/out" 2>&1 || rc=$?
  if [ "$want" = 0 ]; then
    if [ "$rc" = 0 ] && [ -f "$RAN_FILE" ]; then ok "[$tool] $label"; else bad "[$tool] $label (rc=$rc)"; cat "$WORK/out" >&2; fi
  else
    if [ "$rc" != 0 ] && [ ! -f "$RAN_FILE" ]; then ok "[$tool] $label"; else bad "[$tool] $label (rc=$rc, launcher ran: $([ -f "$RAN_FILE" ] && echo yes || echo no))"; cat "$WORK/out" >&2; fi
  fi
}

good_sums()  { ( cd "$DIR" && sha dmf-init > SHA256SUMS ); }
wrong_sums() { printf '%064d  dmf-init\n' 0 > "$DIR/SHA256SUMS"; }
empty_sums() { : > "$DIR/SHA256SUMS"; }
no_sums()    { :; }
tampered()   { good_sums; printf 'echo pwned\n' >> "$DIR/dmf-init"; }

# An unrelated file exists in the download directory and the checksum names it.
unrelated_sums() {
  printf 'unrelated\n' > "$DIR/unrelated"
  ( cd "$DIR" && sha unrelated > SHA256SUMS )
}
# The checksum file lists dmf-init and an unrelated file.
extra_entry_sums() {
  printf 'unrelated\n' > "$DIR/unrelated"
  ( cd "$DIR" && { sha dmf-init; sha unrelated; } > SHA256SUMS )
}
good_then_junk() { ( cd "$DIR" && { sha dmf-init; printf 'junk'; } > SHA256SUMS ); }
duplicate_sums() { ( cd "$DIR" && { sha dmf-init; sha dmf-init; } > SHA256SUMS ); }
blank_line_sums() { ( cd "$DIR" && { sha dmf-init; printf '\n'; } > SHA256SUMS ); }
one_space_sums() { ( cd "$DIR" && printf '%s dmf-init\n' "$(sha dmf-init | cut -d' ' -f1)" > SHA256SUMS ); }
upper_sums()     { ( cd "$DIR" && printf '%s  dmf-init\n' "$(sha dmf-init | cut -d' ' -f1 | tr a-f A-F)" > SHA256SUMS ); }
short_sums()     { ( cd "$DIR" && printf '%s  dmf-init\n' "$(sha dmf-init | cut -c1-63)" > SHA256SUMS ); }
other_name_sums() { ( cd "$DIR" && sha dmf-init | sed 's/dmf-init/other/' > SHA256SUMS ); }
path_name_sums()  { ( cd "$DIR" && sha dmf-init | sed 's| dmf-init| ./dmf-init|' > SHA256SUMS ); }
crlf_sums()       { ( cd "$DIR" && sha dmf-init | sed 's/$/\r/' > SHA256SUMS ); }
garbage_sums()    { printf 'not a checksum file\n' > "$DIR/SHA256SUMS"; }

# The starting directory already holds a launcher that would write the marker.
plant_launcher() { fixture_launcher > "$WORK/run/sub/dmf-init"; }
plant_pair() { plant_launcher; ( cd "$WORK/run/sub" && sha dmf-init > SHA256SUMS ); }

missing_launcher()      { good_sums; rm -f "$DIR/dmf-init"; }
missing_launcher_planted() { plant_pair; good_sums; rm -f "$DIR/dmf-init"; }
missing_sums_planted()  { plant_pair; }

for tool in sha256sum shasum; do
  if [ "$tool" = sha256sum ]; then bindir="$WORK/bin-sha256sum"; have="$HAVE_SHA256SUM"; else bindir="$WORK/bin-shasum"; have="$HAVE_SHASUM"; fi
  if [ "$have" != 1 ]; then echo "  skip [$tool] not installed on this machine"; continue; fi
  echo "install command, checksum tool: $tool"
  run_case "$tool" "$bindir" "runs the launcher when the checksum matches"      0 good_sums
  run_case "$tool" "$bindir" "refuses on a wrong checksum"                      1 wrong_sums
  run_case "$tool" "$bindir" "refuses when the launcher changed after checksum" 1 tampered
  run_case "$tool" "$bindir" "refuses on an empty checksum file"                1 empty_sums
  run_case "$tool" "$bindir" "refuses when the checksum file is missing"        1 no_sums
  run_case "$tool" "$bindir" "refuses when the sums name an unrelated file"     1 unrelated_sums
  run_case "$tool" "$bindir" "refuses extra entries beside dmf-init"            1 extra_entry_sums
  run_case "$tool" "$bindir" "refuses a trailing line without a newline"        1 good_then_junk
  run_case "$tool" "$bindir" "refuses a duplicated dmf-init line"               1 duplicate_sums
  run_case "$tool" "$bindir" "refuses a trailing blank line"                    1 blank_line_sums
  run_case "$tool" "$bindir" "refuses one space between digest and name"        1 one_space_sums
  run_case "$tool" "$bindir" "refuses an uppercase digest"                      1 upper_sums
  run_case "$tool" "$bindir" "refuses a short digest"                           1 short_sums
  run_case "$tool" "$bindir" "refuses a different file name"                    1 other_name_sums
  run_case "$tool" "$bindir" "refuses a ./ path in the name"                    1 path_name_sums
  run_case "$tool" "$bindir" "refuses CRLF line endings"                        1 crlf_sums
  run_case "$tool" "$bindir" "refuses a non-checksum file"                      1 garbage_sums
  run_case "$tool" "$bindir" "refuses a missing launcher (second transfer ok)"  1 missing_launcher
  run_case "$tool" "$bindir" "missing launcher never runs a dmf-init already in the start directory" 1 missing_launcher_planted
  run_case "$tool" "$bindir" "missing checksum never runs a dmf-init already in the start directory" 1 missing_sums_planted
done

echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
