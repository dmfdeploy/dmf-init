---
name: dmf-init-frontend-conventions
description: dmf-init frontend architecture, build conventions, and development rules for the React/Vite/Tailwind frontend
source: auto-skill
extracted_at: '2026-06-10T13:00:00.000Z'
---

# dmf-init Frontend Conventions

## Project context

`dmf-init` is the Day-0 stateless bootstrap container — a localhost-only React + FastAPI UI that wraps `dmf-env`'s `init-wizard.sh` and `bin/` scripts. **Wrap, don't reimplement** the bash. The answers-file is the shared CLI/web contract; generated secrets stay wizard-internal. **Stateless**: tmpfs scratch only, never persist env state or secrets to the host or repo.

## Frontend location

All frontend code lives under `frontend/src/`. Build output goes to `src/dmf_init/static/app/` (served by FastAPI).

## Build commands

```bash
cd frontend && npm install          # install deps
cd frontend && npm run build        # tsc && vite build (production)
cd frontend && npx tsc --noEmit     # type-check only (included in build)
```

**TypeScript is strict**: `noUnusedLocals: true`, `noUnusedParameters: true`, `noFallthroughCasesInSwitch: true`. Every import, variable, and parameter must be used. Prefix unused params with `_` (e.g., `_cursor`) if they must exist for type compatibility.

## Architecture

### State model

- **Create and manage reducers are SEPARATE.** Do not merge into one mega machine.
- Share only dumb primitives: `Shell`, `LogConsole`, `StatusDot`, and the transport-only `useEventStream` hook.
- `useEventStream` is **transport only**; the reducer interprets events into UI state.

### Cursor rules (in useEventStream)

- Isolate state **by `run_id`**; reset on run change
- **Replay from 0** when local state is lost
- Handle terminal **404 / TTL-expired** runs
- **Increment the cursor ONLY after the event is applied** (so a reducer error can't silently skip state)

### NDJSON contract

Reuse `frontend/src/ndjson.ts` `readNdjson`. Backend events: `run_start, step_start, log, step_complete, checkpoint, pause, resume, complete, error`. Pauses arrive **sequentially** — each `pause` is followed by a `/api/bootstrap/resume`, then the next fires.

### Passkey gating

**Client-side, not server-side.** `/api/bootstrap/resume` accepts the `passkey` pause like any other; `/api/bootstrap/passkey/{run_id}` only *reports* `confirmed/required`. The UI enforces "Verify & Continue" by checking that count before it calls `resume`. **Advance the UI on the authoritative `resume`/`step_complete` stream events, not on the resume HTTP 200.**

### Checkpoint/artifact rules

- **`complete` returns checkpoint NUMBERS only.** Artifact names come on the `checkpoint` stream events — capture and keep them (survive replay) to build the Finish download list.
- **Checkpoint #3 = primary download**; #1 is NOT on the artifact route (`/api/backup/artifact/{name}`).
- "Safe to delete" shows **ONLY after `complete` (verify + checkpoint #3)**, never #1.

## UI identity

- **Cyan accent**: `--accent: #7dd3fc` in `index.css`. Tailwind tokens: `bg`, `panel`, `accent`, `accentSoft`, `text`, `muted`, `border`.
- **Dark theme** with radial gradient backgrounds.
- **Self-contained / air-gap**: no CDN fonts or scripts.
- **Status = dot + LABEL** (never colour alone). Use `StatusDot` component.
- **No disruptive reflow**: state patches in place; fixed-height slots for dynamic content (QR, passkey count).
- **Errors are content**: lift current step + last useful log lines into visible error content even when log is collapsed.
- `prefers-reduced-motion` respected for pulse/animation.

## Directory structure (post-rearchitecture)

```
frontend/src/
  app/Shell.tsx              # header + Create/Manage toggle + StepProgress rail
  create/                    # Create flow phases
    ConfigureStep.tsx        # form + Review summary
    InstallProgress.tsx      # step rail + collapsible log
    ConnectStep.tsx          # 3-station inline checklist (replaces modal)
    FinishStep.tsx           # package + safe-to-delete
    ValidateStep.tsx         # optional doctor
  hooks/
    useEventStream.ts        # NDJSON transport (reconnect, cursor)
    useCreateFlow.ts         # create reducer + passkey logic
  shared/
    StatusDot.tsx            # dot + label (a11y)
    Disclosure.tsx           # collapsible detail panel
    LogConsole.tsx           # shared log display
    StepProgress.tsx         # horizontal step rail
  ui.tsx                     # Field, Input, TextArea, SectionCard, ArtifactDownload, CaInstall
  ndjson.ts                  # readNdjson utility
  ManageView.tsx             # restore/doctor/actions (separate reducer)
  App.tsx                    # root: Shell + mode toggle + create flow routing
```

## ManageView

Keep restore/doctor/action logic intact. Render inside Shell from App.tsx. Reducer stays separate from create.

## Agent-bridge communication

Multi-agent collaboration uses `agent-bridge`:
- Binary: `~/.claude/skills/agent-bridge/bin/agent-bridge`
- Send: `agent-bridge send <role> -- "<text>"` or heredoc: `agent-bridge send <role> - <<'MSG' ... MSG`
- List roles: `agent-bridge list`
- If identity mismatch: add `--force` flag

## Bulk token replacement with sed (reskinning)

When reskinning a file with many class changes, batch `sed` is faster than individual edits. On macOS:

```bash
cd frontend/src && sed -i '' -e 's/old/new/g' -e 's/old2/new2/g' file.tsx
```

**Critical rules:**
- Always use `sed -i ''` on macOS (empty string after `-i`, not `-i''`).
- Use `|` as the delimiter when patterns contain `/`: `sed -i '' 's|old|new|g'`.
- **Never mix `/` and `|` delimiters in the same command** — the shell will parse them differently and fail.
- **Chain order matters**: earlier substitutions change text that later patterns may match. For example, replacing `bg-white/5` → `bg-bg/60` before replacing `bg-white/8` can cause the second pattern to match the first result. Order from most-specific to least-specific, or run as separate commands.
- Remove empty class tokens after substitution (e.g., `shadow-glow` → empty string leaves double-spaces in className).

## SVG icons in the frontend

**Do NOT import SVG files as React components** (e.g., `import { DmfLogo } from '../assets/icon.svg'`). Vite requires `@vitejs/plugin-react-svgr` or equivalent for this. Instead, inline the SVG directly in the component as a `<svg>` element. Copy the source SVG to `frontend/src/assets/` for reference, but render it inline with Tailwind classes for sizing/fill.

## TypeScript strictness — common rewrite errors

When bulk-restyling components, watch for these TypeScript errors:
- **`await` inside setState updater**: `setSandbox(p => ({ ...p, key: await file.text() }))` — the updater function is not async. Extract the `await` to a local variable first, then pass it to the updater.
- **Unused imports**: `useRef`, `ReactNode`, etc. must be used or removed. Strict `noUnusedLocals` catches these.
- **Unused functions**: Helper components like `OsInstruction` that get replaced by new implementations must be deleted entirely.

## write_file requires prior read_file

The `write_file` tool will refuse to overwrite a file that has not been read in the current session. Always `read_file` before `write_file`, or use the `edit` tool for targeted changes.
