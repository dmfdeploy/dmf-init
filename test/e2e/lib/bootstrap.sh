#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/_common.sh"

ensure_run_dir

cleanup() {
  cleanup_stateful_resources
}

trap cleanup EXIT INT TERM

wait_for_launch_token() {
  local app_log="$1"
  local token=""
  local _i
  for _i in $(seq 1 60); do
    if grep -q "address ('127.0.0.1', ${DMF_BIND_PORT}): \\[errno 48\\] address already in use" "$app_log"; then
      fail "bootstrap: port ${DMF_BIND_PORT} is already in use"
    fi
    token="$(sed -n 's/^launch token: //p' "$app_log" | tail -n 1)"
    if [ -n "$token" ]; then
      printf '%s\n' "$token"
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_for_healthz() {
  local _i
  for _i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${DMF_BIND_PORT}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

build_fetch_payload() {
  python3 - <<'PY'
import json, os
refs = {name: "main" for name in os.environ["REPOS"].split()}
print(json.dumps({"base_url": os.environ["REPO_BASE_URL"], "refs": refs}))
PY
}

build_render_payload() {
  python3 - <<'PY'
import json, os, pathlib
ssh_key = pathlib.Path(os.environ["LIMA_KEY"]).read_text(encoding="utf-8")
payload = {
    "operator": {
        "username": os.environ["OPERATOR_USERNAME"],
        "email": os.environ["OPERATOR_EMAIL"],
        "display": os.environ["OPERATOR_DISPLAY"],
    },
    "sandbox": {
        "node_ip": os.environ["NEW_IP"],
        "ansible_user": os.environ["USER"],
        "iface": "lima0",
        "ssh_private_key": ssh_key,
    },
}
print(json.dumps(payload))
PY
}

build_backup_payload() {
  python3 - <<'PY'
import json, os
payload = {
    "env_id": os.environ["ENV_ID"],
    "passphrase": os.environ["PASSPHRASE"],
    "passphrase_confirm": os.environ["PASSPHRASE"],
    "remotes": [
        {
            "name": "backup-a",
            "type": "alias",
            "options": {"remote": os.environ["BACKUP_A"]},
            "destination_prefix": "backups",
        },
        {
            "name": "backup-b",
            "type": "alias",
            "options": {"remote": os.environ["BACKUP_B"]},
            "destination_prefix": "backups",
        },
    ],
}
print(json.dumps(payload))
PY
}

build_bootstrap_payload() {
  build_backup_payload
}

parse_render_complete() {
  python3 - "$1" <<'PY'
import json, sys
events = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
for event in reversed(events):
    if event.get("event") == "complete":
        print(event["env_id"])
        raise SystemExit(0)
errors = [event for event in events if event.get("event") == "error"]
if errors:
    raise SystemExit(errors[-1].get("error", "render failed"))
raise SystemExit("render stream ended without complete event")
PY
}

stream_bootstrap_and_resume() {
  python3 - "$@" <<'PY'
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

base_url, cookie_header, run_id, event_path = sys.argv[1:5]

def request_json(path, payload=None):
    data = None
    headers = {"Cookie": cookie_header}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base_url + path, data=data, headers=headers)
    with urllib.request.urlopen(req) as response:
        return response.read().decode()

cursor = 0
terminal = None
with open(event_path, "w", encoding="utf-8") as out:
    while terminal is None:
        req = urllib.request.Request(
            f"{base_url}/api/bootstrap/stream/{run_id}?from={cursor}",
            headers={"Cookie": cookie_header},
        )
        with urllib.request.urlopen(req) as response:
            for raw_line in response:
                line = raw_line.decode().strip()
                if not line:
                    continue
                out.write(line + "\n")
                out.flush()
                event = json.loads(line)
                cursor += 1
                etype = event.get("event")
                if etype == "pause":
                  payload = {"run_id": run_id, "pause_id": event["pause_id"], "payload": {}}
                  request_json("/api/bootstrap/resume", payload)
                elif etype in {"complete", "error"}:
                  terminal = event
                  break
if terminal.get("event") != "complete":
    raise SystemExit(terminal.get("error", "bootstrap run failed"))
PY
}

main() {
  load_state
  [ -n "${NEW_IP:-}" ] || fail "bootstrap: NEW_IP missing; run reset first"

  local cookie_jar="$E2E_RUN_DIR/cookies.txt"
  local app_log="$E2E_RUN_DIR/dmf-init.log"
  local fetch_payload="$E2E_RUN_DIR/repos-fetch.json"
  local fetch_response="$E2E_RUN_DIR/repos-fetch.response.json"
  local render_payload="$E2E_RUN_DIR/render.json"
  local render_events="$E2E_RUN_DIR/render.ndjson"
  local backup_payload="$E2E_RUN_DIR/backup.json"
  local backup_response="$E2E_RUN_DIR/backup.response.json"
  local start_payload="$E2E_RUN_DIR/bootstrap-start.json"
  local start_response="$E2E_RUN_DIR/bootstrap-start.response.json"
  local stream_events_file="$E2E_RUN_DIR/bootstrap-stream.ndjson"
  local token=""

  log "bootstrap: starting scoped ssh-agent"
  eval "$(ssh-agent -s)" >/dev/null
  save_state SSH_AGENT_PID "${SSH_AGENT_PID:-}"
  ssh-add "$LIMA_KEY" >/dev/null

  log "bootstrap: launching dmf-init on 127.0.0.1:${DMF_BIND_PORT}"
  (
    cd "$DMF_INIT_REPO"
    DMF_DATA_ROOT="$DMF_DATA_ROOT" \
    DMF_REPO_BASE_URL="$REPO_BASE_URL" \
    DMF_BIND_PORT="$DMF_BIND_PORT" \
    DMF_SESSION_TTL_SECONDS="$DMF_SESSION_TTL_SECONDS" \
    DMF_BOOTSTRAP_SKIP_TAGS="${E2E_SKIP_TAGS:-}" \
    uv run python -m dmf_init.main
  ) >"$app_log" 2>&1 &
  save_state DMF_INIT_PID "$!"

  token="$(wait_for_launch_token "$app_log")" || fail "bootstrap: launch token not observed in $app_log"
  wait_for_healthz || fail "bootstrap: dmf-init never became healthy on port ${DMF_BIND_PORT}"

  curl -fsS -L -c "$cookie_jar" "http://127.0.0.1:${DMF_BIND_PORT}/?token=${token}" >/dev/null
  local cookie_header
  cookie_header="$(awk 'NF >= 7 && ($1 !~ /^#/ || $1 ~ /^#HttpOnly_/) {print $6 "=" $7}' "$cookie_jar" | paste -sd';' -)"
  [ -n "$cookie_header" ] || fail "bootstrap: failed to capture session cookie"

  build_fetch_payload >"$fetch_payload"
  curl -fsS \
    -b "$cookie_jar" \
    -H 'content-type: application/json' \
    -d @"$fetch_payload" \
    "http://127.0.0.1:${DMF_BIND_PORT}/api/repos/fetch" \
    >"$fetch_response"
  python3 - "$fetch_response" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
repos = {item["name"] for item in payload["repos"]}
expected = {"dmf-env", "dmf-infra", "dmf-runbooks", "dmf-cms", "dmf-media", "dmf-promsd"}
missing = sorted(expected - repos)
if missing:
    raise SystemExit(f"missing fetched repos: {', '.join(missing)}")
PY

  build_render_payload >"$render_payload"
  curl -fsS -N \
    -b "$cookie_jar" \
    -H 'content-type: application/json' \
    -d @"$render_payload" \
    "http://127.0.0.1:${DMF_BIND_PORT}/api/render" \
    >"$render_events"
  ENV_ID="$(parse_render_complete "$render_events")" || fail "bootstrap: render failed"
  save_state ENV_ID "$ENV_ID"

  build_backup_payload >"$backup_payload"
  curl -fsS \
    -b "$cookie_jar" \
    -H 'content-type: application/json' \
    -d @"$backup_payload" \
    "http://127.0.0.1:${DMF_BIND_PORT}/api/backup" \
    >"$backup_response"

  build_bootstrap_payload >"$start_payload"
  curl -fsS \
    -b "$cookie_jar" \
    -H 'content-type: application/json' \
    -d @"$start_payload" \
    "http://127.0.0.1:${DMF_BIND_PORT}/api/bootstrap/start" \
    >"$start_response"

  local run_id
  run_id="$(python3 - "$start_response" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
print(payload["run_id"])
PY
)"
  save_state BOOTSTRAP_RUN_ID "$run_id"

  stream_bootstrap_and_resume \
    "http://127.0.0.1:${DMF_BIND_PORT}" \
    "$cookie_header" \
    "$run_id" \
    "$stream_events_file"

  log "bootstrap: complete for env $ENV_ID on $NEW_IP"

  if [ "${E2E_KEEP:-0}" != "1" ]; then
    cleanup_stateful_resources
  fi
}

main "$@"
