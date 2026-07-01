#!/usr/bin/env bash
# shellcheck disable=SC1090,SC2034  # launcher sourced via runtime path; vars used by sourced fns
# Unit + light integration tests for bin/dmf-init.
#
# Pure helpers are exercised by sourcing the launcher (its main() is guarded by a
# BASH_SOURCE check). The `up` paths are driven against a FAKE docker/curl on
# PATH, so no real Docker daemon or image is needed — this runs anywhere.
#
#   test/launcher/test-dmf-init.sh
set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="$(cd "$TEST_DIR/../.." && pwd)/bin/dmf-init"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1" >&2; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (want '$3', got '$2')"; fi; }

# 32+ char url-safe tokens (matches the container's secrets.token_urlsafe(24)).
OLD="OLDoldOLDoldOLDoldOLDoldOLDold01_-"
NEW="NEWnewNEWnewNEWnewNEWnewNEWnew99_-"
JSONTOKEN="JSONTOKENjsontokenjsontoken00123"
SCRAPETOKEN="SCRAPETOKENscrapetokenscrape0002"

# ── pure helpers (sourced) ──────────────────────────────────────────────────
echo "extract_token:"
# last match wins (a SIGHUP re-mint leaves the old line in the log)
got=$(printf 'launch token: %s\nopen http://localhost:8000/?token=%s\nre-minting...\nopen http://localhost:8000/?token=%s\n' \
        "$OLD" "$OLD" "$NEW" | ( . "$LAUNCHER"; extract_token ))
check "returns the LAST (freshest) token" "$got" "$NEW"

got=$(printf 'open https://localhost:8000/?token=%s\n' "$NEW" | ( . "$LAUNCHER"; extract_token ))
check "matches https launch lines too" "$got" "$NEW"

if printf 'nothing to see here\ninfo: started\n' | ( . "$LAUNCHER"; extract_token ) >/dev/null 2>&1; then
  bad "no-match must return nonzero"
else
  ok "no-match returns nonzero"
fi

# a decoy line that isn't the launch line must not match
if printf 'see open http://x/?token=short\n' | ( . "$LAUNCHER"; extract_token ) >/dev/null 2>&1; then
  bad "unanchored/short decoy must not match"
else
  ok "rejects unanchored/short decoy"
fi

echo "is_valid_port:"
for p in 1 8000 65535; do
  if ( . "$LAUNCHER"; is_valid_port "$p" ); then ok "accepts $p"; else bad "accepts $p"; fi
done
for p in 0 65536 99999 abc ""; do
  if ( . "$LAUNCHER"; is_valid_port "$p" ) 2>/dev/null; then bad "rejects '$p'"; else ok "rejects '$p'"; fi
done

echo "synth_url:"
got=$( . "$LAUNCHER"; HOST_PORT=9000; TLS=false; synth_url "$NEW" )
check "http + remapped port" "$got" "http://127.0.0.1:9000/?token=${NEW}"
got=$( . "$LAUNCHER"; HOST_PORT=8443; TLS=true; synth_url "$NEW" )
check "https scheme under --tls" "$got" "https://127.0.0.1:8443/?token=${NEW}"

# ── fake-docker integration ─────────────────────────────────────────────────
# An env-driven fake `docker`/`curl` on PATH. Behaviour is controlled per-case
# via FAKE_* env vars so we can exercise ownership/safety without a real daemon:
#   FAKE_EXISTS / FAKE_RUNNING  container presence (ps -aq / ps -q)
#   FAKE_LABEL                  value of the launcher label (empty = foreign)
#   FAKE_PORTLINES              "proto|ip|port" lines for verify_container_safe
#   FAKE_DERIVE_PORT            host port for derive_from_container
#   FAKE_TMPFS / FAKE_ENV       Tmpfs json / Config.Env lines
#   FAKE_LOGS_TOKEN             token embedded in `docker logs`
SHIM="$(mktemp -d)"
RUN_ARGS="$SHIM/run-args"
cat >"$SHIM/docker" <<SH
#!/usr/bin/env bash
case "\$1" in
  info) exit 0 ;;
  ps)
    case "\$*" in
      *-aq*) [ "\${FAKE_EXISTS:-0}" = 1 ] && echo fakeid || true ;;
      *)     [ "\${FAKE_RUNNING:-0}" = 1 ] && echo fakeid || true ;;
    esac ;;
  run)  shift; printf '%s\n' "\$*" >"$RUN_ARGS"; echo fakeid ;;
  logs)
    [ -n "\${FAKE_DMF_LAUNCH_TOKEN:-}" ] && printf 'DMF_LAUNCH {"token":"%s","port":8000,"scheme":"http","url":"http://localhost:8000/"}\n' "\$FAKE_DMF_LAUNCH_TOKEN"
    printf 'open http://localhost:8000/?token=%s\n' "\${FAKE_LOGS_TOKEN:-$NEW}" ;;
  kill) exit 0 ;;
  inspect)
    case "\$*" in
      *"index .HostConfig.PortBindings"*) printf '%s' "\${FAKE_DERIVE_PORT:-}" ;;
      *"HostConfig.PortBindings"*)        printf '%s\n' "\${FAKE_PORTLINES:-}" ;;
      *Config.Labels*)                    printf '%s' "\${FAKE_LABEL:-}" ;;
      *HostConfig.Tmpfs*)                 printf '%s' "\${FAKE_TMPFS:-null}" ;;
      *Config.Env*)                       printf '%s\n' "\${FAKE_ENV:-}" ;;
      *) : ;;
    esac ;;
  *) exit 0 ;;
esac
SH
cat >"$SHIM/curl" <<'SH'
#!/usr/bin/env bash
exit 0   # healthz always healthy
SH
chmod +x "$SHIM/docker" "$SHIM/curl"
export PATH="$SHIM:$PATH"

echo "up:"
# happy path with a remapped host port (fresh: not existing, running post-run)
out=$( FAKE_EXISTS=0 FAKE_RUNNING=1 "$LAUNCHER" up --port 9000 2>&1 )
case "$out" in
  *"http://127.0.0.1:9000/?token=${NEW}"*) ok "opens synthesised URL on the mapped host port" ;;
  *) bad "mapped-port URL (got: $(printf '%s' "$out" | tr '\n' '|'))" ;;
esac
case "$(cat "$RUN_ARGS")" in
  *"-p 127.0.0.1:9000:8000"*) ok "publishes loopback-only on the mapped port" ;;
  *) bad "loopback publish mapping" ;;
esac
case "$(cat "$RUN_ARGS")" in
  *"--tmpfs /tmp/dmf-init-data"*) ok "mounts tmpfs data root by default" ;;
  *) bad "default tmpfs mount" ;;
esac

# --dev-no-tmpfs refused without the extra env gate
if out=$( FAKE_EXISTS=0 FAKE_RUNNING=1 "$LAUNCHER" up --dev-no-tmpfs 2>&1 ); then
  bad "--dev-no-tmpfs must be refused without DMF_INIT_DEV_NO_TMPFS=1"
else
  case "$out" in
    *"DMF_INIT_DEV_NO_TMPFS=1"*) ok "--dev-no-tmpfs refused without the env gate" ;;
    *) bad "--dev-no-tmpfs refusal message (got: $out)" ;;
  esac
fi

# --dev-no-tmpfs honoured WITH the env gate → allow-flag, no tmpfs mount
DMF_INIT_DEV_NO_TMPFS=1 FAKE_EXISTS=0 FAKE_RUNNING=1 "$LAUNCHER" up --dev-no-tmpfs >/dev/null 2>&1 || true
case "$(cat "$RUN_ARGS")" in
  *"DMF_ALLOW_NON_TMPFS_DATA_ROOT=true"*) ok "dev-no-tmpfs sets the allow-flag" ;;
  *) bad "dev-no-tmpfs allow-flag" ;;
esac
case "$(cat "$RUN_ARGS")" in
  *"--tmpfs"*) bad "dev-no-tmpfs must NOT mount tmpfs" ;;
  *) ok "dev-no-tmpfs omits the tmpfs mount" ;;
esac

echo "DMF_LAUNCH machine-readable sentinel:"
# DISCRIMINATOR: both DMF_LAUNCH and open lines present with DIFFERENT tokens —
# the launcher must prefer the JSON token (old code would use the scrape token).
out=$( FAKE_EXISTS=0 FAKE_RUNNING=1 FAKE_DMF_LAUNCH_TOKEN="$JSONTOKEN" FAKE_LOGS_TOKEN="$SCRAPETOKEN" "$LAUNCHER" up 2>&1 )
case "$out" in
  *"$JSONTOKEN"*) ok "prefers DMF_LAUNCH token over scrape token" ;;
  *) bad "DMF_LAUNCH preference (got: $(printf '%s' "$out" | tr '\n' '|'))" ;;
esac
case "$out" in
  *"$SCRAPETOKEN"*) bad "must NOT use scrape token when DMF_LAUNCH present" ;;
  *) ok "scrape token absent when DMF_LAUNCH present" ;;
esac

# FALLBACK: only the open line (no DMF_LAUNCH) — must still resolve the token.
out=$( FAKE_EXISTS=0 FAKE_RUNNING=1 FAKE_LOGS_TOKEN="$NEW" "$LAUNCHER" up 2>&1 )
case "$out" in
  *"$NEW"*) ok "falls back to scrape token when no DMF_LAUNCH line" ;;
  *) bad "scrape fallback (got: $(printf '%s' "$out" | tr '\n' '|'))" ;;
esac

# docker run args must include DMF_LAUNCH_JSON=true
case "$(cat "$RUN_ARGS")" in
  *"-e DMF_LAUNCH_JSON=true"*) ok "passes DMF_LAUNCH_JSON=true to container" ;;
  *) bad "DMF_LAUNCH_JSON env var missing from run args" ;;
esac

# present-but-unparseable: short token in DMF_LAUNCH must fall back to scrape
out=$( FAKE_EXISTS=0 FAKE_RUNNING=1 FAKE_DMF_LAUNCH_TOKEN="SHORT" FAKE_LOGS_TOKEN="$NEW" "$LAUNCHER" up 2>&1 )
case "$out" in
  *"$NEW"*) ok "short sentinel token falls back to scrape" ;;
  *) bad "short sentinel fallback (got: $(printf '%s' "$out" | tr '\n' '|'))" ;;
esac

echo "ownership / safety guards:"
# down must refuse a same-named container it did not create
if out=$( FAKE_EXISTS=1 FAKE_LABEL="" "$LAUNCHER" down 2>&1 ); then
  bad "down must refuse an unlabelled (foreign) container"
else
  case "$out" in *"not created by this launcher"*) ok "down refuses a foreign container" ;; *) bad "down refusal message (got: $out)" ;; esac
fi
# down removes a container it owns
out=$( FAKE_EXISTS=1 FAKE_LABEL="dmf-init" "$LAUNCHER" down 2>&1 )
case "$out" in *"stopped and removed"*) ok "down removes an owned container" ;; *) bad "down owned (got: $out)" ;; esac

# link must refuse a foreign running container
if FAKE_RUNNING=1 FAKE_LABEL="" "$LAUNCHER" link >/dev/null 2>&1; then
  bad "link must refuse a foreign container"
else ok "link refuses a foreign container"; fi
# link must refuse an owned-but-unsafe container (published on 0.0.0.0)
if out=$( FAKE_RUNNING=1 FAKE_LABEL="dmf-init" FAKE_PORTLINES="8000/tcp|0.0.0.0|8000" "$LAUNCHER" link 2>&1 ); then
  bad "link must refuse a non-loopback container"
else
  case "$out" in *invariants*) ok "link refuses a non-loopback container" ;; *) bad "link safety message (got: $out)" ;; esac
fi
# status reports a foreign container instead of synthesising a link
out=$( FAKE_RUNNING=1 FAKE_LABEL="" "$LAUNCHER" status 2>&1 )
case "$out" in *"not created by this launcher"*) ok "status reports a foreign container" ;; *) bad "status foreign (got: $out)" ;; esac

echo "up-only option guards:"
# discriminator: down --port must be REJECTED (old code silently accepted it)
if out=$( FAKE_EXISTS=1 FAKE_LABEL="dmf-init" "$LAUNCHER" down --port 9000 2>&1 ); then
  bad "down --port must be rejected (exited 0)"
else
  case "$out" in *"--port"*) ok "down --port rejected with --port in message" ;; *) bad "down --port message missing --port (got: $out)" ;; esac
fi
# all five up-only flags rejected on a non-up subcommand
for flag in "--image foo" "--port 9000" "--tls" "--repo-base-url http://x" "--dev-no-tmpfs"; do
  # shellcheck disable=SC2086  # word-split intentional: "--image foo" → two args
  if FAKE_EXISTS=1 FAKE_LABEL="dmf-init" "$LAUNCHER" down $flag >/dev/null 2>&1; then
    bad "down $flag must be rejected"
  else
    ok "down $flag rejected"
  fi
done
# -h/--help still works on non-up subcommands
if FAKE_EXISTS=0 FAKE_RUNNING=0 "$LAUNCHER" down --help >/dev/null 2>&1; then
  ok "down --help still works"
else
  bad "down --help must still work"
fi
# status with no up-only flags still succeeds
out=$( FAKE_RUNNING=1 FAKE_LABEL="dmf-init" FAKE_PORTLINES="8000/tcp|127.0.0.1|8000" FAKE_TMPFS='{"/tmp/dmf-init-data":""}' FAKE_LOGS_TOKEN="$NEW" "$LAUNCHER" status 2>&1 )
case "$out" in *"link:"*) ok "status with no up-only flags succeeds" ;; *) bad "status clean invocation (got: $out)" ;; esac

echo "verify_container_safe:"
run_verify() { # exports FAKE_* into the sourced verify call
  ( export PATH FAKE_PORTLINES="$1" FAKE_TMPFS="${2:-{\"/tmp/dmf-init-data\":\"\"}}"
    . "$LAUNCHER"; verify_container_safe )
}
if run_verify "8000/tcp|127.0.0.1|8017" >/dev/null 2>&1; then ok "accepts single loopback binding + tmpfs"; else bad "accepts single loopback binding"; fi
if run_verify "8000/tcp|0.0.0.0|8000" >/dev/null 2>&1; then bad "rejects a 0.0.0.0 binding"; else ok "rejects a 0.0.0.0 binding"; fi
if run_verify "$(printf '8000/tcp|127.0.0.1|8017\n9000/tcp|127.0.0.1|9000')" >/dev/null 2>&1; then bad "rejects an extra published port"; else ok "rejects an extra published port"; fi

rm -rf "$SHIM"

# ── summary ─────────────────────────────────────────────────────────────────
echo
echo "passed=$PASS failed=$FAIL"
[ "$FAIL" -eq 0 ]
