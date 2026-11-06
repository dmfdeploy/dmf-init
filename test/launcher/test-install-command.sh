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
SERVER_PID=""
cleanup() {
  if [ -n "$SERVER_PID" ]; then kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

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


# --- HTTP error statuses ---------------------------------------------------
# file:// URLs fail on a missing file even without `curl -f`, so they can't show
# that the command rejects an HTTP error page. A local server returns real
# status codes with a body that would pass the checksum if curl kept it.
# CI sets REQUIRE_HTTP_FIXTURE=1 so a missing python3 fails instead of skipping.
cat > "$WORK/server.py" <<'PY'
import http.server, os, sys

ROOT, PORTFILE = sys.argv[1], sys.argv[2]
# The first path segment picks the behaviour: it names the file that answers
# with an error status, or "ok" for none. The error response still carries the
# real file as its body.
FAIL = {"launcher404": ("dmf-init", 404), "sums500": ("SHA256SUMS", 500)}

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = os.path.normpath(os.path.join(ROOT, self.path.lstrip("/")))
        mode = self.path.lstrip("/").split("/", 1)[0]
        if not path.startswith(ROOT + os.sep) or not os.path.isfile(path):
            self.send_response(404); self.end_headers(); return
        status = 200
        name, code = FAIL.get(mode, (None, 200))
        if os.path.basename(path) == name:
            status = code
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
with open(PORTFILE, "w") as f:
    f.write(str(srv.server_address[1]))
srv.serve_forever()
PY

start_server() {
  mkdir -p "$WORK/http"
  python3 -I "$WORK/server.py" "$WORK/http" "$WORK/port" &
  SERVER_PID=$!
  local i
  for i in $(seq 1 50); do [ -s "$WORK/port" ] && return 0; sleep 0.1; done
  return 1
}

# $1 tool label, $2 PATH dir, $3 mode (ok|launcher404|sums500), $4 expected, $5 label
run_http_case() {
  local tool="$1" bindir="$2" mode="$3" want="$4" label="$5" rc=0
  rm -rf "$WORK/http"/* "$RAN_FILE" "$WORK/run"
  mkdir -p "$WORK/http/$mode/$VERSION" "$WORK/run/sub"
  fixture_launcher > "$WORK/http/$mode/$VERSION/dmf-init"
  ( cd "$WORK/http/$mode/$VERSION" && sha dmf-init > SHA256SUMS )
  sed -e "s|https://github.com/dmfdeploy/dmf-init/releases/download/|http://127.0.0.1:${PORT}/${mode}/|" \
    "$WORK/cmd.raw" > "$WORK/cmd-http.sh"
  grep -q "http://127.0.0.1:${PORT}/${mode}/" "$WORK/cmd-http.sh" || { echo "URL rewrite failed" >&2; exit 1; }
  ( cd "$WORK/run/sub" && PATH="$bindir" bash "$WORK/cmd-http.sh" ) >"$WORK/out" 2>&1 || rc=$?
  local stray; stray="$(ls -A "$WORK/run/sub")"
  if [ "$want" = 0 ]; then
    if [ "$rc" = 0 ] && [ -f "$RAN_FILE" ] && [ -z "$stray" ]; then ok "[$tool] $label"; else bad "[$tool] $label (rc=$rc)"; cat "$WORK/out" >&2; fi
  else
    if [ "$rc" != 0 ] && [ ! -f "$RAN_FILE" ] && [ -z "$stray" ]; then ok "[$tool] $label"; else bad "[$tool] $label (rc=$rc, launcher ran: $([ -f "$RAN_FILE" ] && echo yes || echo no), start dir: ${stray:-empty})"; cat "$WORK/out" >&2; fi
  fi
}

if ! command -v python3 >/dev/null; then
  if [ "${REQUIRE_HTTP_FIXTURE:-0}" = 1 ]; then bad "python3 is required for the HTTP status cases"; else echo "  skip HTTP status cases: python3 not installed"; fi
elif ! start_server; then
  bad "HTTP fixture server did not start"
else
  PORT="$(cat "$WORK/port")"
  for tool in sha256sum shasum; do
    if [ "$tool" = sha256sum ]; then bindir="$WORK/bin-sha256sum"; have="$HAVE_SHA256SUM"; else bindir="$WORK/bin-shasum"; have="$HAVE_SHASUM"; fi
    [ "$have" = 1 ] || continue
    echo "install command over HTTP, checksum tool: $tool"
    run_http_case "$tool" "$bindir" ok          0 "runs the launcher on 200 responses"
    run_http_case "$tool" "$bindir" launcher404 1 "refuses a 404 launcher with a body that matches the checksum"
    run_http_case "$tool" "$bindir" sums500     1 "refuses a 500 checksum file with a valid body"
  done
fi

# --- mktemp failure --------------------------------------------------------
# On macOS `cd ""` succeeds and stays put, so a failed mktemp must stop the
# command before any download. The start directory holds a launcher and a
# matching checksum that the release would also match, and SHA256SUMS is a
# symlink whose target a download could overwrite.
mktemp_fail_case() {
  local shell_name="$1" shell_path="$2" rc=0
  rm -rf "$REL" "$RAN_FILE" "$WORK/run"
  mkdir -p "$DIR" "$WORK/run/sub"
  fixture_launcher > "$DIR/dmf-init"
  good_sums
  mkdir -p "$WORK/failbin"
  mkbin "$WORK/failbin" sha256sum
  [ "$HAVE_SHA256SUM" = 1 ] || mkbin "$WORK/failbin" shasum
  rm -f "$WORK/failbin/mktemp"
  printf '#!/bin/sh\nexit 1\n' > "$WORK/failbin/mktemp"; chmod +x "$WORK/failbin/mktemp"
  fixture_launcher > "$WORK/run/sub/dmf-init"
  printf 'VICTIM\n' > "$WORK/run/victim"
  ln -s ../victim "$WORK/run/sub/SHA256SUMS"
  local before_launcher before_victim
  before_launcher="$(cat "$WORK/run/sub/dmf-init")"; before_victim="$(cat "$WORK/run/victim")"
  ( cd "$WORK/run/sub" && PATH="$WORK/failbin" "$shell_path" "$WORK/cmd.sh" ) >"$WORK/out" 2>&1 || rc=$?
  if [ "$rc" != 0 ] && [ ! -f "$RAN_FILE" ] \
     && [ "$(cat "$WORK/run/sub/dmf-init")" = "$before_launcher" ] \
     && [ "$(cat "$WORK/run/victim")" = "$before_victim" ] \
     && [ -L "$WORK/run/sub/SHA256SUMS" ] \
     && [ "$(ls -A "$WORK/run/sub" | wc -l | tr -d ' ')" = 2 ]; then
    ok "[$shell_name] a failed mktemp stops before any download or run"
  else
    bad "[$shell_name] a failed mktemp stops before any download or run (rc=$rc, launcher ran: $([ -f "$RAN_FILE" ] && echo yes || echo no))"; cat "$WORK/out" >&2
  fi
}

echo "install command, mktemp failure"
if [ "$HAVE_SHA256SUM" = 1 ] || [ "$HAVE_SHASUM" = 1 ]; then
  mktemp_fail_case bash "$(command -v bash)"
  if command -v zsh >/dev/null; then mktemp_fail_case zsh "$(command -v zsh)"; else echo "  skip [zsh] not installed on this machine"; fi
fi

echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
