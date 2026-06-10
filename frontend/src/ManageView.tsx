import { useEffect, useMemo, useRef, useState } from 'react'
import { readNdjson } from './ndjson'
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

type ManageRunStartEvent = {
  event: 'run_start'
  run_id: string
  steps: string[]
}

type ManageStepStartEvent = {
  event: 'step_start'
  step: string
  index: number
  kind: 'command' | 'checkpoint'
}

type ManageLogEvent = {
  event: 'log'
  step: string
  line: string
}

type ManageStepCompleteEvent = {
  event: 'step_complete'
  step: string
  status: 'ok' | 'error'
}

type ManageCheckpointEvent = {
  event: 'checkpoint'
  n: number
  artifact_name: string
}

type ManageErrorEvent = {
  event: 'error'
  step?: string
  error: string
}

type ManageCompleteEvent = {
  event: 'complete'
  run_id: string
  checkpoints: number[]
}

type ManageStreamEvent =
  | ManageRunStartEvent
  | ManageStepStartEvent
  | ManageLogEvent
  | ManageStepCompleteEvent
  | ManageCheckpointEvent
  | ManageErrorEvent
  | ManageCompleteEvent

type Banner = {
  tone: 'error' | 'info'
  text: string
}

function readText(response: Response): Promise<string> {
  return response.text()
}

async function readError(response: Response): Promise<string> {
  // Read the body exactly once; a non-JSON error body (HTML/plaintext) must not
  // trigger a second read ("body stream already read").
  const text = await readText(response)
  try {
    const payload = JSON.parse(text) as { detail?: unknown; error?: unknown }
    if (payload.detail !== undefined) {
      return String(payload.detail)
    }
    if (payload.error !== undefined) {
      return String(payload.error)
    }
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
    : 'border-accent/30 bg-accent/10 text-accentSoft'
}

function stepDotClass(status: ManageStepState): string {
  switch (status) {
    case 'running':
      return 'bg-accent shadow-[0_0_0_4px_rgba(125,211,252,0.15)] animate-pulse'
    case 'ok':
      return 'bg-emerald-400 shadow-[0_0_0_4px_rgba(52,211,153,0.15)]'
    case 'error':
      return 'bg-red-400 shadow-[0_0_0_4px_rgba(248,113,113,0.15)]'
    default:
      return 'bg-slate-500/80'
  }
}

function stepLabel(status: ManageStepState): string {
  switch (status) {
    case 'running':
      return 'Running'
    case 'ok':
      return 'Done'
    case 'error':
      return 'Error'
    default:
      return 'Pending'
  }
}

function checkpointBadge(checkpoint: ManageCheckpoint) {
  return (
    <span className="inline-flex items-center gap-2 rounded-full border border-accent/30 bg-accent/10 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.24em] text-accentSoft">
      <span>#{checkpoint.n} sealed</span>
      <span className="normal-case tracking-normal text-muted">{checkpoint.artifact_name}</span>
    </span>
  )
}

function ManageConsole(props: { runId: string; title?: string; onTerminal?: () => void }) {
  const [runStatus, setRunStatus] = useState<'idle' | 'starting' | 'running' | 'complete' | 'error'>(
    'idle',
  )
  const [steps, setSteps] = useState<string[]>([])
  const [stepStatuses, setStepStatuses] = useState<Record<string, ManageStepState>>({})
  const [currentStep, setCurrentStep] = useState<string | null>(null)
  const [logs, setLogs] = useState<Array<{ step: string; line: string }>>([])
  const [checkpoints, setCheckpoints] = useState<ManageCheckpoint[]>([])
  const [terminal, setTerminal] = useState<
    { kind: 'complete'; runId: string; checkpoints: number[] } | { kind: 'error'; step?: string; error: string } | null
  >(null)
  const [reconnectNote, setReconnectNote] = useState<string | null>(null)
  const [streamError, setStreamError] = useState<string | null>(null)
  const [cursor, setCursor] = useState(0)
  const cursorRef = useRef(0)
  const terminalRef = useRef<typeof terminal>(null)

  const stepsByStatus = useMemo(
    () => steps.map((step) => ({ id: step, status: stepStatuses[step] ?? 'pending' })),
    [steps, stepStatuses],
  )

  useEffect(() => {
    setRunStatus('starting')
    setSteps([])
    setStepStatuses({})
    setCurrentStep(null)
    setLogs([])
    setCheckpoints([])
    setTerminal(null)
    setReconnectNote(null)
    setStreamError(null)
    setCursor(0)
    cursorRef.current = 0
    terminalRef.current = null
    setRunStatus('running')

    let cancelled = false
    const controller = new AbortController()

    async function waitForRetry(delayMs: number) {
      await new Promise<void>((resolve) => {
        const timer = window.setTimeout(resolve, delayMs)
        controller.signal.addEventListener(
          'abort',
          () => {
            window.clearTimeout(timer)
            resolve(undefined)
          },
          { once: true },
        )
      })
    }

    async function streamOnce(from: number): Promise<boolean> {
      const response = await fetch(`/api/bootstrap/stream/${props.runId}?from=${from}`, {
        method: 'GET',
        credentials: 'same-origin',
        headers: {
          accept: 'application/x-ndjson',
        },
        signal: controller.signal,
      })

      if (!response.ok) {
        throw new Error(await readError(response))
      }

      setReconnectNote(null)
      let sawTerminal = false
      await readNdjson(response, (event) => {
        if (cancelled) return

        cursorRef.current += 1
        setCursor(cursorRef.current)
        const typedEvent = event as Partial<ManageStreamEvent> & { event?: string }

        switch (typedEvent.event) {
          case 'run_start': {
            const startEvent = typedEvent as ManageRunStartEvent
            setSteps(startEvent.steps)
            setStepStatuses(
              Object.fromEntries(startEvent.steps.map((step) => [step, 'pending'] as const)),
            )
            setCurrentStep(null)
            break
          }
          case 'step_start': {
            const stepEvent = typedEvent as ManageStepStartEvent
            setCurrentStep(stepEvent.step)
            setStepStatuses((previous) => ({
              ...previous,
              [stepEvent.step]: 'running',
            }))
            break
          }
          case 'log': {
            const logEvent = typedEvent as ManageLogEvent
            setLogs((previous) => [...previous, { step: logEvent.step, line: logEvent.line }])
            break
          }
          case 'step_complete': {
            const completeEvent = typedEvent as ManageStepCompleteEvent
            setStepStatuses((previous) => ({
              ...previous,
              [completeEvent.step]: completeEvent.status,
            }))
            break
          }
          case 'checkpoint': {
            const checkpointEvent = typedEvent as ManageCheckpointEvent
            setCheckpoints((previous) => {
              if (previous.some((checkpoint) => checkpoint.n === checkpointEvent.n)) {
                return previous
              }
              return [
                ...previous,
                {
                  n: checkpointEvent.n,
                  artifact_name: checkpointEvent.artifact_name,
                },
              ]
            })
            break
          }
          case 'error': {
            const errorEvent = typedEvent as ManageErrorEvent
            terminalRef.current = {
              kind: 'error',
              step: errorEvent.step,
              error: errorEvent.error,
            }
            setTerminal(terminalRef.current)
            setRunStatus('error')
            setStreamError(errorEvent.error)
            props.onTerminal?.()
            sawTerminal = true
            break
          }
          case 'complete': {
            const completeEvent = typedEvent as ManageCompleteEvent
            terminalRef.current = {
              kind: 'complete',
              runId: completeEvent.run_id,
              checkpoints: completeEvent.checkpoints,
            }
            setTerminal(terminalRef.current)
            setRunStatus('complete')
            props.onTerminal?.()
            sawTerminal = true
            break
          }
          default:
            break
        }
      })

      return sawTerminal
    }

    async function runStream() {
      let retries = 0
      while (!cancelled) {
        try {
          const sawTerminal = await streamOnce(cursorRef.current)
          if (cancelled || sawTerminal || terminalRef.current) {
            return
          }
          if (retries >= 5) {
            setRunStatus('error')
            setStreamError('Stream disconnected before completion after 5 retries.')
            return
          }
          retries += 1
          setReconnectNote(`Reconnecting after drop (${retries}/5)…`)
          await waitForRetry(250 * retries)
        } catch (error) {
          if (cancelled || controller.signal.aborted) {
            return
          }
          if (retries >= 5) {
            setRunStatus('error')
            setStreamError(error instanceof Error ? error.message : String(error))
            return
          }
          retries += 1
          setReconnectNote(`Reconnecting after error (${retries}/5)…`)
          await waitForRetry(250 * retries)
        }
      }
    }

    void runStream()

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [props.runId])

  useEffect(() => {
    if (!logs.length) return
    const logBox = document.getElementById('manage-log-console')
    if (logBox) {
      logBox.scrollTop = logBox.scrollHeight
    }
  }, [logs])

  const completionLine = useMemo(() => {
    if (terminal?.kind !== 'complete') {
      return ''
    }
    if (!terminal.checkpoints.length) {
      return 'All checkpoints sealed.'
    }
    return `${terminal.checkpoints.map((checkpoint) => `#${checkpoint}`).join(' and ')} sealed.`
  }, [terminal])

  return (
    <section className="rounded-[1.75rem] border border-border/70 bg-panel/90 p-5 shadow-glow backdrop-blur">
      <div className="flex flex-col gap-4 border-b border-border/60 pb-5 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Manage console</p>
          <h3 className="mt-2 text-2xl font-semibold text-text">
            {props.title ?? 'Streamed manage run'}
          </h3>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-muted">
            This console mirrors the bootstrap stream, reconnects with a cursor, and shows the
            sealed checkpoint after the manage action completes.
          </p>
        </div>
        <div className="rounded-full border border-border/70 bg-white/5 px-4 py-2 text-sm text-muted">
          <span className="text-text">{runStatus}</span>
          <span className="ml-2">· {props.runId}</span>
        </div>
      </div>

      {reconnectNote ? (
        <div className="mt-4 rounded-2xl border border-border/70 bg-white/5 px-4 py-3 text-sm text-muted">
          {reconnectNote}
        </div>
      ) : null}

      {streamError ? (
        <div className="mt-4 rounded-2xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-100">
          {streamError}
        </div>
      ) : null}

      <div className="mt-5 grid gap-5 lg:grid-cols-[0.95fr_1.05fr]">
        <div className="rounded-3xl border border-border/70 bg-black/20 p-4">
          <div className="mb-4 flex items-center justify-between gap-3">
            <div>
              <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Steps</p>
              <p className="mt-1 text-sm text-muted">Current step: {currentStep ?? 'waiting'}</p>
            </div>
            {terminal?.kind === 'complete' ? (
              <span className="rounded-full border border-accent/30 bg-accent/10 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.24em] text-accentSoft">
                Complete
              </span>
            ) : terminal?.kind === 'error' ? (
              <span className="rounded-full border border-red-500/30 bg-red-500/10 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.24em] text-red-100">
                Error
              </span>
            ) : null}
          </div>
          <div className="grid gap-3">
            {stepsByStatus.length ? (
              stepsByStatus.map((step) => (
                <div key={step.id} className="flex items-center gap-3 rounded-2xl border border-border/60 bg-white/5 px-3 py-3">
                  <span className={`h-3 w-3 rounded-full ${stepDotClass(step.status)}`} />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium text-text">{step.id}</div>
                    <div className="text-xs uppercase tracking-[0.28em] text-muted">
                      {stepLabel(step.status)}
                    </div>
                  </div>
                </div>
              ))
            ) : (
              <div className="rounded-2xl border border-dashed border-border/60 px-3 py-5 text-sm text-muted">
                Waiting for streamed events...
              </div>
            )}
          </div>
          {checkpoints.length ? (
            <div className="mt-4 flex flex-wrap gap-2">
              {checkpoints.map((checkpoint) => (
                <div key={checkpoint.n}>{checkpointBadge(checkpoint)}</div>
              ))}
            </div>
          ) : null}
          {completionLine ? (
            <p className="mt-4 text-sm text-muted">{completionLine}</p>
          ) : null}
        </div>

        <div className="rounded-3xl border border-border/70 bg-black/20 p-4">
          <div className="mb-4 flex items-center justify-between gap-3">
            <div>
              <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Log</p>
              <p className="mt-1 text-sm text-muted">Redacted streamed output</p>
            </div>
            <div className="rounded-full border border-border/70 bg-white/5 px-3 py-1 text-xs text-muted">
              {cursor} events
            </div>
          </div>
          <pre
            id="manage-log-console"
            className="max-h-[30rem] overflow-auto whitespace-pre-wrap rounded-3xl border border-border/70 bg-black/40 p-4 text-xs leading-6 text-text"
          >
            {logs.length
              ? logs.map((entry) => `${entry.step}: ${entry.line}`).join('\n')
              : 'Waiting for streamed logs...'}
          </pre>
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
      setRestoreError(message.includes('restore decryption failed') ? 'wrong passphrase or corrupt artifact' : message)
    } finally {
      setRestoreBusy(false)
    }
  }

  async function runDoctor() {
    if (!sessionId) return
    setManageBusy(true)
    setBanner(null)
    try {
      const response = await fetchJson<ManageActionResponse>('/api/manage/doctor', { session_id: sessionId })
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
          ? {
              session_id: sessionId,
              action,
              params: { playbook },
            }
          : {
              session_id: sessionId,
              action,
              params: undefined,
            }
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
    <section className="rounded-[1.75rem] border border-border/70 bg-panel/80 p-5 shadow-glow backdrop-blur">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Verified restore</p>
          <h2 className="mt-2 text-2xl font-semibold text-text">Env {restoreResult.env_id}</h2>
        </div>
        <span className="rounded-full border border-accent/30 bg-accent/10 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.24em] text-accentSoft">
          {restoreResult.verified ? 'verified' : 'unverified'}
        </span>
      </div>
      <div className="mt-4 grid gap-3 text-sm text-muted">
        <div className="rounded-2xl border border-border/70 bg-white/5 px-4 py-3">
          <div className="text-xs uppercase tracking-[0.28em] text-muted">Profile</div>
          <div className="mt-1 text-text">{restoreResult.profile}</div>
        </div>
        <div className="rounded-2xl border border-border/70 bg-white/5 px-4 py-3">
          <div className="text-xs uppercase tracking-[0.28em] text-muted">Schema / checkpoint</div>
          <div className="mt-1 text-text">
            {restoreResult.schema_version}
            {restoreResult.checkpoint !== null ? ` · #${restoreResult.checkpoint}` : ''}
          </div>
        </div>
        <div className="rounded-2xl border border-border/70 bg-white/5 px-4 py-3">
          <div className="text-xs uppercase tracking-[0.28em] text-muted">Repos</div>
          <div className="mt-2 grid gap-2">
            {restoreResult.repos.map((repo) => (
              <div key={repo.name} className="rounded-2xl border border-border/70 bg-black/20 px-3 py-2">
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
                  className="rounded-2xl border border-border/80 bg-white/5 px-4 py-3 text-sm text-text file:mr-4 file:rounded-lg file:border-0 file:bg-accent/10 file:px-3 file:py-1.5 file:text-accentSoft file:cursor-pointer"
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
                className="rounded-2xl border border-accent/30 bg-accent px-6 py-3 text-sm font-semibold text-bg transition-transform hover:-translate-y-0.5 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {restoreBusy ? 'Restoring…' : 'Restore & verify'}
              </button>
              <p className="text-sm text-muted">
                Wrong passphrase maps to the same backend message used by the API contract.
              </p>
            </div>
            {restoreError ? (
              <div className="rounded-3xl border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-100">
                {restoreError}
              </div>
            ) : null}
          </SectionCard>
        </div>

        <div className="grid gap-6">
          <section className="rounded-[1.75rem] border border-border/70 bg-panel/80 p-5 shadow-glow backdrop-blur">
            <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Manage flow</p>
            <h2 className="mt-2 text-xl font-semibold text-text">Restore, verify, then manage</h2>
            <p className="mt-3 text-sm leading-6 text-muted">
              Upload the backup, enter the passphrase, and once verified you can run manage actions.
            </p>
            <div className="mt-4 grid gap-3 text-sm text-muted">
              <div className="rounded-2xl border border-border/70 bg-white/5 px-4 py-3">1. Upload and verify the backup artifact.</div>
              <div className="rounded-2xl border border-border/70 bg-white/5 px-4 py-3">2. Review the verified env summary and repo provenance.</div>
              <div className="rounded-2xl border border-border/70 bg-white/5 px-4 py-3">3. Run a manage action (playbook, upgrade, rotate, teardown).</div>
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

            <section className="rounded-[1.75rem] border border-border/70 bg-panel/80 p-5 shadow-glow backdrop-blur">
              <div className="flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  onClick={runDoctor}
                  disabled={manageBusy || activeRunLive}
                  className="rounded-2xl border border-border/70 bg-white/5 px-5 py-3 text-sm font-semibold text-text transition hover:bg-white/8 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  Run doctor
                </button>
              </div>
              {sessionId ? (
                <p className="mt-4 text-sm text-muted">Session: {sessionId}</p>
              ) : null}
              {banner ? (
                <div className={`mt-4 rounded-3xl border px-4 py-3 text-sm ${badgeClass(banner.tone)}`}>
                  {banner.text}
                </div>
              ) : null}
            </section>
          </div>

          <div className="grid gap-6">
            <section className="rounded-[1.75rem] border border-border/70 bg-panel/80 p-5 shadow-glow backdrop-blur">
              <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Verified restore</p>
              <h2 className="mt-2 text-xl font-semibold text-text">What you can do next</h2>
              <p className="mt-3 text-sm leading-6 text-muted">
                Doctor is read-only. Mutating actions stream under the same session with a new re-backup checkpoint.
              </p>
              <div className="mt-4 rounded-3xl border border-border/70 bg-white/5 p-4 text-sm text-muted">
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
          <section className="rounded-[1.75rem] border border-border/70 bg-panel/80 p-5 shadow-glow backdrop-blur">
            <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Actions</p>
            <h2 className="mt-2 text-2xl font-semibold text-text">Mutating manage actions</h2>
            <p className="mt-3 text-sm leading-6 text-muted">
              Every action ends with a re-backup checkpoint saved as a downloadable artifact.
            </p>
            <div className="mt-5 grid gap-4">
            <div className="rounded-3xl border border-border/70 bg-white/5 p-4">
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
                  className="rounded-2xl border border-accent/30 bg-accent px-5 py-3 text-sm font-semibold text-bg transition-transform hover:-translate-y-0.5 disabled:cursor-not-allowed disabled:opacity-60"
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
                <div key={action} className="rounded-3xl border border-border/70 bg-white/5 p-4">
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
                        className="rounded-2xl border border-red-500/30 bg-red-500/10 px-5 py-3 text-sm font-semibold text-red-100 transition hover:bg-red-500/15 disabled:cursor-not-allowed disabled:opacity-60"
                      >
                        Yes, tear down
                      </button>
                    </div>
                  ) : (
                    <button
                      type="button"
                      onClick={() => startAction(action)}
                      disabled={manageBusy || activeRunLive}
                      className="mt-4 rounded-2xl border border-border/70 bg-white/5 px-5 py-3 text-sm font-semibold text-text transition hover:bg-white/8 disabled:cursor-not-allowed disabled:opacity-60"
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
            {banner ? (
              <div className={`mt-4 rounded-3xl border px-4 py-3 text-sm ${badgeClass(banner.tone)}`}>
                {banner.text}
              </div>
            ) : null}
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
          <section className="rounded-[1.75rem] border border-border/70 bg-panel/80 p-5 shadow-glow backdrop-blur">
            <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Stream</p>
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

  // managing phase removed - actions shown in restored phase
  return null
}
