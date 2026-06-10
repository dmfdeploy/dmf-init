#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/lib/_common.sh"

ONLY_STAGE=""
KEEP=0
E2E_NO_PASSKEYS=0
E2E_SKIP_TAGS=""
TEARDOWN=0

usage() {
  cat <<'EOF'
usage: ./e2e.sh [--no-passkeys] [--keep] [--teardown] [--only reset|bootstrap|verify]
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --no-passkeys)
      E2E_NO_PASSKEYS=1
      E2E_SKIP_TAGS="verify-d8-passkeys"
      ;;
    --keep)
      KEEP=1
      ;;
    --teardown)
      TEARDOWN=1
      ;;
    --only)
      shift
      [ $# -gt 0 ] || fail "--only requires a stage"
      ONLY_STAGE="$1"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown flag: $1"
      ;;
  esac
  shift
done

export E2E_NO_PASSKEYS
export E2E_SKIP_TAGS
export E2E_KEEP="$KEEP"

ensure_run_dir

cleanup() {
  cleanup_stateful_resources
}

trap cleanup EXIT INT TERM

run_stage() {
  local stage="$1"
  local script="$2"
  local log_file="$E2E_RUN_DIR/${stage}.log"
  log "stage=${stage} start"
  if "$script" 2>&1 | tee "$log_file"; then
    log "stage=${stage} ok"
  else
    log "stage=${stage} failed"
    return 1
  fi
}

teardown_vm() {
  log "teardown: stopping and deleting $VM_NAME"
  limactl stop -f "$VM_NAME" >/dev/null 2>&1 || true
  limactl delete -f "$VM_NAME" >/dev/null 2>&1 || true
  rm -rf "$DMF_DATA_ROOT"
}

main() {
  local failed=0
  local summary="$E2E_RUN_DIR/summary.txt"
  : >"$summary"

  run_stage preflight "$SCRIPT_DIR/lib/preflight.sh"

  if [ -n "$ONLY_STAGE" ]; then
    case "$ONLY_STAGE" in
      reset) run_stage reset "$SCRIPT_DIR/lib/reset.sh" ;;
      bootstrap) run_stage bootstrap "$SCRIPT_DIR/lib/bootstrap.sh" ;;
      verify) run_stage verify "$SCRIPT_DIR/lib/verify.sh" ;;
      *) fail "unsupported --only stage: $ONLY_STAGE" ;;
    esac
  else
    run_stage reset "$SCRIPT_DIR/lib/reset.sh" || failed=1
    [ "$failed" -eq 0 ] && run_stage bootstrap "$SCRIPT_DIR/lib/bootstrap.sh" || failed=1
    [ "$failed" -eq 0 ] && run_stage verify "$SCRIPT_DIR/lib/verify.sh" || failed=1
  fi

  load_state
  {
    printf 'run_dir=%s\n' "$E2E_RUN_DIR"
    printf 'env_id=%s\n' "${ENV_ID:-}"
    printf 'node_ip=%s\n' "${NEW_IP:-}"
    printf 'passkeys=%s\n' "$([ "$E2E_NO_PASSKEYS" = "1" ] && echo skipped || echo pending)"
    printf 'result=%s\n' "$([ "$failed" -eq 0 ] && echo PASS || echo FAIL)"
  } | tee -a "$summary"

  if [ "$TEARDOWN" -eq 1 ]; then
    teardown_vm
  fi

  if [ "$failed" -ne 0 ]; then
    exit 1
  fi
}

main "$@"
