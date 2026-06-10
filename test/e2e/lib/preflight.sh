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

check_playwright_browser() {
  local listing
  listing="$(npx playwright install --list 2>&1 || true)"
  printf '%s\n' "$listing" | grep -q 'chromium-' \
    || fail "Playwright Chromium browser not installed"
}

main() {
  log "preflight: checking host tooling"
  for cmd in bash curl jq limactl npx python3 ssh ssh-add ssh-agent ssh-keygen timeout uv; do
    require_cmd "$cmd"
  done
  require_cmd lsof

  preclean_runtime

  limactl list >/dev/null 2>&1 || fail "limactl is installed but not responding"
  [ -r "$LIMA_KEY" ] || fail "Lima SSH key not readable: $LIMA_KEY"

  for repo in $REPOS; do
    [ -d "$(repo_target_path "$repo")" ] || fail "repo target missing: $(repo_target_path "$repo")"
  done

  (
    cd "$DMF_INIT_REPO"
    uv run python -c 'import dmf_init.main'
  ) >/dev/null 2>&1 || fail "uv import check failed for dmf_init.main"

  if lsof -iTCP:"$DMF_BIND_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    fail "port $DMF_BIND_PORT is already in use on 127.0.0.1"
  fi

  if [ "${E2E_NO_PASSKEYS:-0}" != "1" ]; then
    check_playwright_browser
  fi

  log "preflight: ok"
}

main "$@"
