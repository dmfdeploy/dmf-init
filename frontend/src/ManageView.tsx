import { useCallback, useReducer, useState } from 'react'
import { useEventStream } from './hooks/useEventStream'
import { StatusDot } from './shared/StatusDot'
import { LogConsole, type LogEntry } from './shared/LogConsole'
import { Field, Input, SectionCard } from './ui'

type ManageRestoreResult = {
  session_id: string
  env_id: string
  profile: string
  schema_version: string | number
  checkpoint: number | null
  repos: Array<{
    name: string
    ref: string
    resolved_sha?: string | null
  }>
  age_key_path: string
  answers_file_path: string
  render_dir: string
  verified: boolean
}

type ManageAction = 'rerun-playbook' | 'upgrade-in-place' | 'rotate' | 'teardown'

type ManageActionResponse = {
  run_id: string
}

type ManageStepState = 'pending' | 'running' | 'ok' | 'error'

type ManageCheckpoint = {
  n: number
  artifact_name: string
}

type Banner = {
  tone: 'error' | 'info'
  text: string
}

async function readError(response: Response): Promise<string> {
  const text = await response.text()
  try {
    const payload = JSON.parse(text) as { detail?: unknown; error?: unknown }
    if (payload.detail !== undefined) return String(payload.detail)
    if (payload.error !== undefined) return String(payload.error)
    return text || JSON.stringify(payload)
  } catch {
    return text
  }
}

async function fetchJson<T>(url: string, body: unknown): Promise<T> {
  const response = await fetch(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'content-type': 'application/json',
      accept: 'application/json',
    },
    body: JSON.stringify(body),
  })

  if (!response.ok) {
    throw new Error(await readError(response))
  }

  return (await response.json()) as T
}

function badgeClass(tone: 'error' | 'info'): string {
  return tone === 'error'
    ? 'border-red-500/30 bg-red-500/10 text-red-100'
    : 'border-accent/30 bg-accent/10 text-muted'
}

function checkpointBadge(checkpoint: ManageCheckpoint) {
  return (
    <span className="inline-flex items-center gap-2 rounded-md border border-accent/30 bg-accent/10 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-muted">
      <span>#{checkpoint.n} sealed</span>
      <span className="normal-case tracking-normal text-muted">{checkpoint.artifact_name}</span>
    </span>
  )
}

// ─── ManageConsole: uses shared useEventStream + primitives ──────────────────

type ManageTerminal =
  | { kind: 'complete'; runId: string; checkpoints: number[] }
  | { kind: 'error'; step?: string; error: string }

type ManageConsoleState = {
  steps: string[]
  stepStatuses: Record<string, ManageStepState>
  currentStep: string | null
  checkpoints: ManageCheckpoint[]
  terminal: ManageTerminal | null
}

type ManageConsoleAction =
  | { type: 'run_started'; steps: string[] }
  | { type: 'step_start'; step: string }
  | { type: 'step_complete'; step: string; status: 'ok' | 'error' }
  | { type: 'checkpoint'; n: number; artifact_name: string }
  | { type: 'error'; step?: string; error: string }
  | { type: 'complete'; runId: string; checkpoints: number[] }

function manageConsoleReducer(state: ManageConsoleState, action: ManageConsoleAction): ManageConsoleState {
  switch (action.type) {
    case 'run_started':
      return {
        ...state,
        steps: action.steps,
        stepStatuses: Object.fromEntries(action.steps.map((s) => [s, 'pending'] as const)),
        currentStep: null,
        checkpoints: [],
        terminal: null,
      }
    case 'step_start':
      return {
        ...state,
        currentStep: action.step,
        stepStatuses: { ...state.stepStatuses, [action.step]: 'running' },
      }
    case 'step_complete':
      return {
        ...state,
        stepStatuses: { ...state.stepStatuses, [action.step]: action.status },
      }
    case 'checkpoint':
      if (state.checkpoints.some((c) => c.n === action.n)) return state
      return {
        ...state,
        checkpoints: [...state.checkpoints, { n: action.n, artifact_name: action.artifact_name }],
      }
    case 'error':
      return {
        ...state,
        terminal: { kind: 'error', step: action.step, error: action.error },
      }
    case 'complete':
      return {
        ...state,
        terminal: { kind: 'complete', runId: action.runId, checkpoints: action.checkpoints },
      }
    default:
      return state
  }
}

function ManageConsole(props: { runId: string; title?: string; onTerminal?: () => void }) {
  const [state, dispatch] = useReducer(manageConsoleReducer, {
    steps: [],
    stepStatuses: {},
    currentStep: null,
    checkpoints: [],
    terminal: null,
  })
  const [runStatus, setRunStatus] = useState<'starting' | 'running' | 'complete' | 'error'>('running')

  const handleEvent = useCallback(
    (event: Record<string, unknown>) => {
      const eventType = event.event as string | undefined
      switch (eventType) {
        case 'run_start': {
          const e = event as { steps: string[] }
          dispatch({ type: 'run_started', steps: e.steps })
          break
        }
        case 'step_start': {
          const e = event as { step: string }
          dispatch({ type: 'step_start', step: e.step })
          break
        }
        case 'step_complete': {
          const e = event as { step: string; status: 'ok' | 'error' }
          dispatch({ type: 'step_complete', step: e.step, status: e.status })
          break
        }
        case 'checkpoint': {
          const e = event as { n: number; artifact_name: string }
          dispatch({ type: 'checkpoint', n: e.n, artifact_name: e.artifact_name })
          break
        }
        case 'error': {
          const e = event as { step?: string; error: string }
          dispatch({ type: 'error', step: e.step, error: e.error })
          setRunStatus('error')
          props.onTerminal?.()
          break
        }
        case 'complete': {
          const e = event as { run_id: string; checkpoints: number[] }
          dispatch({ type: 'complete', runId: e.run_id, checkpoints: e.checkpoints })
          setRunStatus('complete')
          props.onTerminal?.()
          break
        }
      }
    },
    [props.onTerminal],
  )

  const { logs, cursor, reconnectNote, streamError } = useEventStream({
    runId: props.runId,
    onEvent: handleEvent,
  })

  const stepsByStatus = state.steps.map((step) => ({
    id: step,
    status: state.stepStatuses[step] ?? 'pending',
  }))

  const completionLine =
    state.terminal?.kind === 'complete' && state.terminal.checkpoints.length > 0
      ? `${state.terminal.checkpoints.map((c) => `#${c}`).join(' and ')} sealed.`
      : state.terminal?.kind === 'complete'
        ? 'All checkpoints sealed.'
        : ''

  return (
    <section className="rounded-lg border border-border bg-panel p-4  ">
      <div className="flex flex-col gap-4 border-b border-border pb-5 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-muted">Manage console</p>
          <h3 className="mt-2 text-2xl font-semibold text-text">
            {props.title ?? 'Streamed manage run'}
          </h3>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-muted">
            This console mirrors the bootstrap stream, reconnects with a cursor, and shows the
            sealed checkpoint after the manage action completes.
          </p>
        </div>
        <div className="rounded-md border border-border bg-bg/60 px-4 py-2 text-sm text-muted">
          <span className="text-text">{runStatus}</span>
          <span className="ml-2">· {props.runId}</span>
        </div>
      </div>

      {reconnectNote && (
        <div className="mt-4 rounded-lg border border-border bg-bg/60 px-3 py-2 text-sm text-muted">
          {reconnectNote}
        </div>
      )}

      {streamError && (
        <div className="mt-4 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-100">
          {streamError}
        </div>
      )}

      <div className="mt-5 grid gap-4 lg:grid-cols-[0.95fr_1.05fr]">
        {/* Step rail */}
        <div className="rounded-lg border border-border bg-bg/60 p-4">
          <div className="mb-4 flex items-center justify-between gap-3">
            <div>
              <p className="text-xs uppercase tracking-[0.2em] text-muted">Steps</p>
              <p className="mt-1 text-sm text-muted">Current step: {state.currentStep ?? 'waiting'}</p>
            </div>
            {state.terminal?.kind === 'complete' ? (
              <span className="rounded-md border border-accent/30 bg-accent/10 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-muted">
                Complete
              </span>
            ) : state.terminal?.kind === 'error' ? (
              <span className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-red-100">
                Error
              </span>
            ) : null}
          </div>
          <div className="grid gap-3">
            {stepsByStatus.length ? (
              stepsByStatus.map((step) => (
                <div
                  key={step.id}
                  className="flex items-center gap-3 rounded-lg border border-border bg-bg/60 px-3 py-3"
                >
                  <StatusDot status={step.status as 'pending' | 'running' | 'ok' | 'error'} />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium text-text">{step.id}</div>
                    <div className="text-xs uppercase tracking-[0.18em] text-muted">
                      {step.status === 'running'
                        ? 'Running'
                        : step.status === 'ok'
                          ? 'Done'
                          : step.status === 'error'
                            ? 'Error'
                            : 'Pending'}
                    </div>
                  </div>
                </div>
              ))
            ) : (
              <div className="rounded-lg border border-dashed border-border px-3 py-5 text-sm text-muted">
                Waiting for streamed events...
              </div>
            )}
          </div>
          {state.checkpoints.length > 0 && (
            <div className="mt-4 flex flex-wrap gap-2">
              {state.checkpoints.map((checkpoint) => (
                <div key={checkpoint.n}>{checkpointBadge(checkpoint)}</div>
              ))}
            </div>
          )}
          {completionLine && (
            <p className="mt-4 text-sm text-muted">{completionLine}</p>
          )}
        </div>

        {/* Log pane — shared LogConsole with auto-scroll */}
        <div className="rounded-lg border border-border bg-bg/60 p-4">
          <div className="mb-4 flex items-center justify-between gap-3">
            <div>
              <p className="text-xs uppercase tracking-[0.2em] text-muted">Log</p>
              <p className="mt-1 text-sm text-muted">Redacted streamed output</p>
            </div>
            <div className="rounded-md border border-border bg-bg/60 px-3 py-1 text-xs text-muted">
              {cursor} events
            </div>
          </div>
          <LogConsole
            logs={logs as LogEntry[]}
            cursor={cursor}
            id="manage-log-console"
            maxHeight="30rem"
          />
        </div>
      </div>
    </section>
  )
}

function repoLine(repo: ManageRestoreResult['repos'][number]): string {
  const bits = [repo.ref]
  if (repo.resolved_sha) {
    bits.push(repo.resolved_sha)
  }
  return bits.join(' · ')
}

export default function ManageView() {
  const [phase, setPhase] = useState<'restore' | 'restored' | 'managing'>('restore')
  const [uploadedFile, setUploadedFile] = useState<File | null>(null)
  const [passphrase, setPassphrase] = useState('')
  const [restoreBusy, setRestoreBusy] = useState(false)
  const [restoreError, setRestoreError] = useState<string | null>(null)
  const [restoreResult, setRestoreResult] = useState<ManageRestoreResult | null>(null)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [banner, setBanner] = useState<Banner | null>(null)
  const [manageBusy, setManageBusy] = useState(false)
  const [playbook, setPlaybook] = useState('dmf-infra/k3s-lab-bootstrap/example.yml')
  const [teardownConfirm, setTeardownConfirm] = useState('')
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [activeRunTitle, setActiveRunTitle] = useState<string | null>(null)
  const [activeRunLive, setActiveRunLive] = useState(false)

  async function restoreSession() {
    if (!uploadedFile) {
      setRestoreError('Select a backup file first.')
      return
    }
    setRestoreBusy(true)
    setRestoreError(null)
    setBanner(null)
    try {
      const formData = new FormData()
      formData.append('file', uploadedFile)
      formData.append('passphrase', passphrase)
      const response = await fetch('/api/manage/restore', {
        method: 'POST',
        credentials: 'same-origin',
        body: formData,
      })
      if (!response.ok) {
        throw new Error(await readError(response))
      }
      const result = await response.json() as ManageRestoreResult
      setRestoreResult(result)
      setSessionId(result.session_id)
      setActiveRunId(null)
      setActiveRunTitle(null)
      setActiveRunLive(false)
      setPhase('restored')
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      setRestoreError(
        message.includes('restore decryption failed')
          ? 'wrong passphrase or corrupt artifact'
          : message,
      )
    } finally {
      setRestoreBusy(false)
    }
  }

  async function runDoctor() {
    if (!sessionId) return
    setManageBusy(true)
    setBanner(null)
    try {
      const response = await fetchJson<ManageActionResponse>('/api/manage/doctor', {
        session_id: sessionId,
      })
      setActiveRunId(response.run_id)
      setActiveRunTitle('Doctor run')
      setActiveRunLive(true)
    } catch (error) {
      setBanner({
        tone: 'error',
        text: error instanceof Error ? error.message : String(error),
      })
    } finally {
      setManageBusy(false)
    }
  }

  async function startAction(action: ManageAction) {
    if (!sessionId) return
    if (action === 'teardown' && teardownConfirm.trim().toLowerCase() !== 'tear down') {
      setBanner({ tone: 'error', text: 'type "tear down" to confirm teardown' })
      return
    }

    setManageBusy(true)
    setBanner(null)
    try {
      const payload =
        action === 'rerun-playbook'
          ? { session_id: sessionId, action, params: { playbook } }
          : { session_id: sessionId, action, params: undefined }
      const response = await fetchJson<ManageActionResponse>('/api/manage/action/start', payload)
      setActiveRunId(response.run_id)
      setActiveRunTitle(
        action === 'rerun-playbook'
          ? 'Re-run playbook'
          : action === 'upgrade-in-place'
            ? 'Upgrade in place'
            : action === 'rotate'
              ? 'Rotate secret id'
              : 'Teardown',
      )
      setActiveRunLive(true)
    } catch (error) {
      setBanner({
        tone: 'error',
        text: error instanceof Error ? error.message : String(error),
      })
    } finally {
      setManageBusy(false)
    }
  }

  const restoreSummary = restoreResult ? (
    <section className="rounded-lg border border-border bg-panel p-4  ">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-muted">Verified restore</p>
          <h2 className="mt-2 text-2xl font-semibold text-text">Env {restoreResult.env_id}</h2>
        </div>
        <span className="rounded-md border border-accent/30 bg-accent/10 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-muted">
          {restoreResult.verified ? 'verified' : 'unverified'}
        </span>
      </div>
      <div className="mt-4 grid gap-3 text-sm text-muted">
        <div className="rounded-lg border border-border bg-bg/60 px-3 py-2">
          <div className="text-xs uppercase tracking-[0.18em] text-muted">Profile</div>
          <div className="mt-1 text-text">{restoreResult.profile}</div>
        </div>
        <div className="rounded-lg border border-border bg-bg/60 px-3 py-2">
          <div className="text-xs uppercase tracking-[0.18em] text-muted">Schema / checkpoint</div>
          <div className="mt-1 text-text">
            {restoreResult.schema_version}
            {restoreResult.checkpoint !== null ? ` · #${restoreResult.checkpoint}` : ''}
          </div>
        </div>
        <div className="rounded-lg border border-border bg-bg/60 px-3 py-2">
          <div className="text-xs uppercase tracking-[0.18em] text-muted">Repos</div>
          <div className="mt-2 grid gap-2">
            {restoreResult.repos.map((repo) => (
              <div
                key={repo.name}
                className="rounded-lg border border-border bg-bg/60 px-3 py-2"
              >
                <div className="font-medium text-text">{repo.name}</div>
                <div className="text-xs text-muted">{repoLine(repo)}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  ) : null

  if (phase === 'restore') {
    return (
      <div className="grid gap-6 lg:grid-cols-[1.05fr_0.95fr]">
        <div className="grid gap-6">
          <SectionCard
            eyebrow="Manage"
            title="Restore an existing backup"
            description="Upload a backup file, enter the passphrase, and verify it."
          >
            <div className="grid gap-4">
              <Field label="Backup file" hint="Upload the .tar.age file you downloaded">
                <input
                  type="file"
                  accept=".tar.age"
                  onChange={(event) => setUploadedFile(event.target.files?.[0] ?? null)}
                  className="rounded-lg border border-border bg-bg/60 px-3 py-2 text-sm text-text file:mr-4 file:rounded-lg file:border-0 file:bg-accent/10 file:px-3 file:py-1.5 file:text-muted file:cursor-pointer"
                />
                {uploadedFile && (
                  <p className="mt-2 text-xs text-muted">Selected: {uploadedFile.name}</p>
                )}
              </Field>
              <Field label="Passphrase">
                <Input
                  type="password"
                  value={passphrase}
                  onChange={(event) => setPassphrase(event.target.value)}
                />
              </Field>
            </div>

            <div className="flex flex-wrap items-center gap-3">
              <button
                type="button"
                onClick={restoreSession}
                disabled={restoreBusy || !uploadedFile}
                className="rounded-lg border border-accent/30 bg-accent px-4 py-2 text-sm font-semibold text-bg  disabled:cursor-not-allowed disabled:opacity-60"
              >
                {restoreBusy ? 'Restoring…' : 'Restore & verify'}
              </button>
              <p className="text-sm text-muted">
                The backup is decrypted inside the container; nothing changes until it verifies.
              </p>
            </div>
            {restoreError && (
              <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-100">
                {restoreError}
              </div>
            )}
          </SectionCard>
        </div>

        <div className="grid gap-6">
          <section className="rounded-lg border border-border bg-panel p-4  ">
            <p className="text-xs uppercase tracking-[0.2em] text-muted">Manage flow</p>
            <h2 className="mt-2 text-xl font-semibold text-text">Restore, verify, then manage</h2>
            <p className="mt-3 text-sm leading-6 text-muted">
              Upload the backup, enter the passphrase, and once verified you can run manage actions.
            </p>
            <div className="mt-4 grid gap-3 text-sm text-muted">
              <div className="rounded-lg border border-border bg-bg/60 px-3 py-2">
                1. Upload and verify the backup artifact.
              </div>
              <div className="rounded-lg border border-border bg-bg/60 px-3 py-2">
                2. Review the verified env summary and repo provenance.
              </div>
              <div className="rounded-lg border border-border bg-bg/60 px-3 py-2">
                3. Run a manage action (playbook, upgrade, rotate, teardown).
              </div>
            </div>
          </section>
        </div>
      </div>
    )
  }

  if (phase === 'restored' && restoreResult) {
    return (
      <>
        <div className="grid gap-6 lg:grid-cols-[1.05fr_0.95fr]">
          <div className="grid gap-6">
            {restoreSummary}

            <section className="rounded-lg border border-border bg-panel p-4  ">
              <div className="flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  onClick={runDoctor}
                  disabled={manageBusy || activeRunLive}
                  className="rounded-lg border border-border bg-bg/60 px-4 py-2 text-sm font-semibold text-text transition hover:bg-bg/80 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  Run doctor
                </button>
              </div>
              {sessionId && (
                <p className="mt-4 text-sm text-muted">Session: {sessionId}</p>
              )}
              {banner && (
                <div
                  className={`mt-4 rounded-lg border px-3 py-2 text-sm ${badgeClass(banner.tone)}`}
                >
                  {banner.text}
                </div>
              )}
            </section>
          </div>

          <div className="grid gap-6">
            <section className="rounded-lg border border-border bg-panel p-4  ">
              <p className="text-xs uppercase tracking-[0.2em] text-muted">Verified restore</p>
              <h2 className="mt-2 text-xl font-semibold text-text">What you can do next</h2>
              <p className="mt-3 text-sm leading-6 text-muted">
                Doctor is read-only. Mutating actions stream under the same session with a new
                re-backup checkpoint.
              </p>
              <div className="mt-4 rounded-lg border border-border bg-bg/60 p-4 text-sm text-muted">
                <p className="text-text">Restored paths</p>
                <div className="mt-2 grid gap-2">
                  <div>Age key: {restoreResult.age_key_path}</div>
                  <div>Answers file: {restoreResult.answers_file_path}</div>
                  <div>Render dir: {restoreResult.render_dir}</div>
                </div>
              </div>
            </section>
          </div>
        </div>

        <div className="mt-6">
          <section className="rounded-lg border border-border bg-panel p-4  ">
            <p className="text-xs uppercase tracking-[0.2em] text-muted">Actions</p>
            <h2 className="mt-2 text-2xl font-semibold text-text">Mutating manage actions</h2>
            <p className="mt-3 text-sm leading-6 text-muted">
              Every action ends with a re-backup checkpoint saved as a downloadable artifact.
            </p>
            <div className="mt-5 grid gap-4">
              <div className="rounded-lg border border-border bg-bg/60 p-4">
                <Field label="Rerun playbook" hint="Path under repos/">
                  <Input
                    value={playbook}
                    onChange={(event) => setPlaybook(event.target.value)}
                    placeholder="dmf-infra/k3s-lab-bootstrap/example.yml"
                  />
                </Field>
                <div className="mt-4 flex flex-wrap gap-3">
                  <button
                    type="button"
                    onClick={() => startAction('rerun-playbook')}
                    disabled={manageBusy || activeRunLive}
                    className="rounded-lg border border-accent/30 bg-accent px-4 py-2 text-sm font-semibold text-bg  disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    Rerun playbook
                  </button>
                </div>
              </div>

              <div className="grid gap-3 md:grid-cols-3">
                {(
                  [
                    ['upgrade-in-place', 'Upgrade in place'],
                    ['rotate', 'Rotate secret id'],
                    ['teardown', 'Teardown'],
                  ] as const
                ).map(([action, label]) => (
                  <div
                    key={action}
                    className="rounded-lg border border-border bg-bg/60 p-4"
                  >
                    <p className="text-sm font-semibold text-text">{label}</p>
                    <p className="mt-2 text-sm text-muted">
                      {action === 'teardown'
                        ? 'Remove the env after the backup checkpoint is sealed.'
                        : 'Runs immediately and then re-seals the env backup.'}
                    </p>
                    {action === 'teardown' ? (
                      <div className="mt-4 grid gap-3">
                        <Field label='Type "tear down" to confirm'>
                          <Input
                            value={teardownConfirm}
                            onChange={(event) => setTeardownConfirm(event.target.value)}
                            placeholder="tear down"
                          />
                        </Field>
                        <button
                          type="button"
                          onClick={() => startAction(action)}
                          disabled={
                            manageBusy ||
                            activeRunLive ||
                            teardownConfirm.trim().toLowerCase() !== 'tear down'
                          }
                          className="rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2 text-sm font-semibold text-red-100 transition hover:bg-red-500/15 disabled:cursor-not-allowed disabled:opacity-60"
                        >
                          Yes, tear down
                        </button>
                      </div>
                    ) : (
                      <button
                        type="button"
                        onClick={() => startAction(action)}
                        disabled={manageBusy || activeRunLive}
                        className="mt-4 rounded-lg border border-border bg-bg/60 px-4 py-2 text-sm font-semibold text-text transition hover:bg-bg/80 disabled:cursor-not-allowed disabled:opacity-60"
                      >
                        {label}
                      </button>
                    )}
                  </div>
                ))}
              </div>

              <p className="mt-4 text-sm text-muted">
                Remote backups are kept by default; purge is a manual step and is out of scope for
                this build.
              </p>
              {banner && (
                <div
                  className={`mt-4 rounded-lg border px-3 py-2 text-sm ${badgeClass(banner.tone)}`}
                >
                  {banner.text}
                </div>
              )}
            </div>
          </section>
        </div>

        <div className="grid gap-6">
          {activeRunId ? (
            <ManageConsole
              runId={activeRunId}
              title={activeRunTitle ?? undefined}
              onTerminal={() => setActiveRunLive(false)}
            />
          ) : (
            <section className="rounded-lg border border-border bg-panel p-4  ">
              <p className="text-xs uppercase tracking-[0.2em] text-muted">Stream</p>
              <h2 className="mt-2 text-xl font-semibold text-text">Console appears here</h2>
              <p className="mt-3 text-sm leading-6 text-muted">
                Start doctor or an action to stream the manage run over the bootstrap stream
                endpoint.
              </p>
            </section>
          )}
        </div>
      </>
    )
  }

  return null
}
