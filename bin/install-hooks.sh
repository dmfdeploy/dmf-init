#!/usr/bin/env bash
# install-hooks.sh — point this clone's git at .githooks/.
#
# Run once after cloning. It's a per-clone setting (lives in .git/config,
# not tracked), so each operator/machine needs to do this once per fresh
# clone.
#
# Idempotent — re-running is safe.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

current="$(git config --get core.hooksPath || echo '')"

if [ "$current" = ".githooks" ]; then
    echo "core.hooksPath already set to .githooks"
    exit 0
fi

if [ -n "$current" ]; then
    echo "WARNING: core.hooksPath is currently '$current'. Overwriting to '.githooks'." >&2
fi

git config core.hooksPath .githooks
echo "Set core.hooksPath = .githooks"
echo ""
echo "Hooks active:"
ls -la .githooks/ | grep -v '^total' | grep -v '^d' | sed 's/^/  /'
