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

main() {
  log "reset: recreating sandbox VM"
  NEW_IP="$(
    cd "$DMF_ENV_REPO"
    VM_NAME="$VM_NAME" bin/recreate-sandbox-vm.sh -y
  )"
  [ -n "$NEW_IP" ] || fail "reset: recreate-sandbox-vm.sh did not return an IP"
  save_state NEW_IP "$NEW_IP"

  ssh-keygen -R "$NEW_IP" >/dev/null 2>&1 || true

  log "reset: wiping tmpfs data roots"
  rm -rf "$DMF_DATA_ROOT" "$BACKUP_A" "$BACKUP_B" "$REPOSRC_DIR"
  mkdir -p "$BACKUP_A" "$BACKUP_B" "$REPOSRC_DIR"

  log "reset: rebuilding reposrc symlinks"
  for repo in $REPOS; do
    ln -s "$(repo_target_path "$repo")" "$REPOSRC_DIR/${repo}.git"
  done

  log "reset: NEW_IP=$NEW_IP"
}

main "$@"
