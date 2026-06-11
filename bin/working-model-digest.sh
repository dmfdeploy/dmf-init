#!/usr/bin/env bash
# working-model-digest.sh — print the DMF working-model digest for session context.
#
# Run by the Claude Code SessionStart hook (checked-in .claude/settings.json) in
# every DMF repo so each session starts with the working model in context
# (umbrella issue #32). Reads the digest AT RUNTIME from the canonical source —
# the WM-DIGEST section of the umbrella's docs/WORKING-MODEL.md — never from an
# embedded copy. codex/qwen don't run hooks; their forcing mechanism is the
# working-model block in AGENTS.md/QWEN.md.
#
# Umbrella checkout lookup order: $DMFDEPLOY_UMBRELLA → the repo this script
# lives in (umbrella case) → sibling ../dmfdeploy. Degrades gracefully: if no
# checkout is found, prints a one-line warning and exits 0 — never wedges the
# session.

set -uo pipefail

SCRIPT_REPO="$(cd "$(dirname "$0")/.." && pwd)"

candidates=()
[ -n "${DMFDEPLOY_UMBRELLA:-}" ] && candidates+=("$DMFDEPLOY_UMBRELLA")
candidates+=("$SCRIPT_REPO" "$(dirname "$SCRIPT_REPO")/dmfdeploy")

doc=""
for c in "${candidates[@]}"; do
    if [ -f "$c/docs/WORKING-MODEL.md" ]; then
        doc="$c/docs/WORKING-MODEL.md"
        break
    fi
done

if [ -z "$doc" ]; then
    echo "[working-model] WARN: umbrella checkout not found (set DMFDEPLOY_UMBRELLA or clone dmfdeploy/dmfdeploy as a sibling) — working-model digest unavailable; canonical: https://github.com/dmfdeploy/dmfdeploy/blob/main/docs/WORKING-MODEL.md"
    exit 0
fi

awk '/<!-- WM-DIGEST-START -->/{keep=1; next} /<!-- WM-DIGEST-END -->/{exit} keep{print}' "$doc"
