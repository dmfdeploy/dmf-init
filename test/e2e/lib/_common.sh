#!/usr/bin/env bash

set -euo pipefail

COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_ROOT="$(cd "$COMMON_DIR/.." && pwd)"
DMF_INIT_REPO="$(cd "$E2E_ROOT/../.." && pwd)"
UMBRELLA_ROOT="$(cd "$DMF_INIT_REPO/.." && pwd)"
DMF_ENV_REPO="$UMBRELLA_ROOT/dmf-env"
DMF_INFRA_REPO="$UMBRELLA_ROOT/dmf-infra"
PROFILE_FILE="$E2E_ROOT/profile.montest.env"
RUNS_ROOT="${E2E_RUNS_ROOT:-/tmp/sandbox-e2e/runs}"
UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/sandbox-e2e/uv-cache}"
export UV_CACHE_DIR

# shellcheck source=/dev/null
. "$PROFILE_FILE"
export OPERATOR_USERNAME OPERATOR_EMAIL OPERATOR_DISPLAY PASSPHRASE DMF_DATA_ROOT
export REPO_BASE_URL REPOSRC_DIR BACKUP_A BACKUP_B DMF_BIND_PORT VM_NAME
export DMF_SESSION_TTL_SECONDS LIMA_KEY REPOS

timestamp_utc() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log() {
  printf '[%s] %s\n' "$(timestamp_utc)" "$*"
}

fail() {
  log "FAIL: $*"
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

ensure_run_dir() {
  if [ -n "${E2E_RUN_DIR:-}" ]; then
    mkdir -p "$E2E_RUN_DIR"
  else
    mkdir -p "$RUNS_ROOT"
    E2E_RUN_DIR="$RUNS_ROOT/$(date -u +%Y%m%dT%H%M%SZ)"
    export E2E_RUN_DIR
  fi
  mkdir -p "$E2E_RUN_DIR"
  E2E_STATE_FILE="${E2E_STATE_FILE:-$E2E_RUN_DIR/state.env}"
  export E2E_STATE_FILE
  touch "$E2E_STATE_FILE"
  load_state
}

save_state() {
  local key="$1"
  local value="${2-}"
  printf 'export %s=%q\n' "$key" "$value" >>"$E2E_STATE_FILE"
  export "$key=$value"
}

load_state() {
  if [ -f "$E2E_STATE_FILE" ]; then
    # shellcheck source=/dev/null
    . "$E2E_STATE_FILE"
  fi
}

latest_env_id() {
  local env_root="${DMF_DATA_ROOT}/envs"
  [ -d "$env_root" ] || return 1
  find "$env_root" -mindepth 1 -maxdepth 1 -type d -print0 \
    | xargs -0 ls -td 2>/dev/null \
    | head -n 1 \
    | xargs -I{} basename "{}"
}

ensure_env_context() {
  if [ -z "${ENV_ID:-}" ]; then
    ENV_ID="$(latest_env_id || true)"
    [ -n "${ENV_ID:-}" ] || fail "ENV_ID unavailable and no env found under $DMF_DATA_ROOT/envs"
    save_state ENV_ID "$ENV_ID"
  fi
  if [ -z "${NEW_IP:-}" ]; then
    local render_json="${DMF_DATA_ROOT}/runs/${ENV_ID}/render.json"
    if [ -f "$render_json" ]; then
      NEW_IP="$(python3 - "$render_json" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
print(payload.get("node_ip", ""))
PY
)"
    fi
    [ -n "${NEW_IP:-}" ] || fail "NEW_IP unavailable and render metadata missing node_ip"
    save_state NEW_IP "$NEW_IP"
  fi
}

repo_target_path() {
  local repo="$1"
  printf '%s/%s/.git' "$UMBRELLA_ROOT" "$repo"
}

json_from_file() {
  python3 - "$1" <<'PY'
import json, sys
print(json.dumps(json.load(open(sys.argv[1], encoding="utf-8")), separators=(",", ":")))
PY
}

kill_pid_if_running() {
  local pid="${1:-}"
  [ -n "$pid" ] || return 0
  if kill -0 "$pid" >/dev/null 2>&1; then
    kill "$pid" >/dev/null 2>&1 || true
    local _i
    for _i in 1 2 3 4 5; do
      if kill -0 "$pid" >/dev/null 2>&1; then
        sleep 1
      else
        break
      fi
    done
    if kill -0 "$pid" >/dev/null 2>&1; then
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
    wait "$pid" 2>/dev/null || true
  fi
}

cleanup_stateful_resources() {
  if [ "${_E2E_CLEANUP_DONE:-0}" = "1" ]; then
    return 0
  fi
  _E2E_CLEANUP_DONE=1
  load_state
  kill_pid_if_running "${PORT_FORWARD_PID:-}"
  kill_pid_if_running "${SSH_TUNNEL_PID:-}"
  kill_pid_if_running "${PLAYWRIGHT_PID:-}"
  kill_pid_if_running "${DMF_INIT_PID:-}"
  if [ -n "${SSH_AGENT_PID:-}" ]; then
    ssh-agent -k >/dev/null 2>&1 || kill_pid_if_running "${SSH_AGENT_PID:-}"
  fi
  local job_pid
  for job_pid in $(jobs -p 2>/dev/null || true); do
    kill_pid_if_running "$job_pid"
  done
}

reap_state_file_pids() {
  local state_file="$1"
  [ -f "$state_file" ] || return 0
  local pid
  for pid in "$(
    bash -lc '
      set -euo pipefail
      # shellcheck source=/dev/null
      . "$1"
      for name in DMF_INIT_PID SSH_AGENT_PID PORT_FORWARD_PID SSH_TUNNEL_PID PLAYWRIGHT_PID; do
        eval "value=\${$name:-}"
        [ -n "$value" ] && printf "%s\n" "$value"
      done
    ' -- "$state_file" 2>/dev/null
  )"; do
    kill_pid_if_running "$pid"
  done
}

preclean_runtime() {
  mkdir -p "$RUNS_ROOT"
  local state_file
  while IFS= read -r state_file; do
    reap_state_file_pids "$state_file"
  done < <(find "$RUNS_ROOT" -name state.env -type f 2>/dev/null | sort)

  local stale_pid=""
  while read -r stale_pid; do
    [ -n "$stale_pid" ] || continue
    kill_pid_if_running "$stale_pid"
  done < <(ps -ax -o pid=,command= | awk '/(uv run python -m dmf_init.main|python3 .* -m dmf_init.main|python .* -m dmf_init.main)/ {print $1}')

  local ssh_agent_pid=""
  while read -r ssh_agent_pid; do
    [ -n "$ssh_agent_pid" ] || continue
    kill_pid_if_running "$ssh_agent_pid"
  done < <(ps -ax -o pid=,command= | awk '/ssh-agent -s$/ {print $1}')

  local listener_pid=""
  listener_pid="$(lsof -t -iTCP:"$DMF_BIND_PORT" -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
  if [ -n "$listener_pid" ]; then
    local listener_cmd=""
    listener_cmd="$(ps -p "$listener_pid" -o command= 2>/dev/null || true)"
    if printf '%s' "$listener_cmd" | grep -q 'dmf_init.main'; then
      kill_pid_if_running "$listener_pid"
    else
      fail "port $DMF_BIND_PORT is busy with a non-harness process: $listener_cmd"
    fi
  fi
}
