import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { QRCodeSVG } from 'qrcode.react'
import { readNdjson } from './ndjson'
import { CaInstall, type CaCertPayload } from './ui'

type BootstrapStartResponse = {
  run_id: string
}

type BootstrapStepState = 'pending' | 'running' | 'ok' | 'error'

type BootstrapCheckpoint = {
  n: number
  artifact_name: string
}

type HostsMapPayload = {
  entries: string[]
  node_ip: string
  base_domain: string
  dns_note: string
  note: string
}

type PasskeyPayload = {
  enrollment_url: string
  confirmed: number
  required: number
  hint: string
}

type ActivePause =
  | { pause_id: 'ca-cert'; title: string; payload: CaCertPayload }
  | { pause_id: 'hosts-map'; title: string; payload: HostsMapPayload }
  | { pause_id: 'passkey'; title: string; payload: PasskeyPayload }

type BootstrapRunStartEvent = {
  event: 'run_start'
  run_id: string
  steps: string[]
}

type BootstrapStepStartEvent = {
  event: 'step_start'
  step: string
  index: number
  kind: 'command' | 'checkpoint' | 'pause'
}

type BootstrapLogEvent = {
  event: 'log'
  step: string
  line: string
}

type BootstrapStepCompleteEvent = {
  event: 'step_complete'
  step: string
  status: 'ok' | 'error'
}

type BootstrapCheckpointEvent = {
  event: 'checkpoint'
  n: number
  artifact_name: string
}

type BootstrapPauseEvent = {
  event: 'pause'
  pause_id: ActivePause['pause_id']
  title: string
  payload: CaCertPayload | HostsMapPayload | PasskeyPayload
}

type BootstrapResumeEvent = {
  event: 'resume'
  pause_id: ActivePause['pause_id']
}

type BootstrapErrorEvent = {
  event: 'error'
  step?: string
  error: string
}

type BootstrapCompleteEvent = {
  event: 'complete'
  run_id: string
  checkpoints: number[]
}

type BootstrapEvent =
  | BootstrapRunStartEvent
  | BootstrapStepStartEvent
  | BootstrapLogEvent
  | BootstrapStepCompleteEvent
  | BootstrapCheckpointEvent
  | BootstrapPauseEvent
  | BootstrapResumeEvent
  | BootstrapErrorEvent
  | BootstrapCompleteEvent

type LogEntry = {
  step: string
  line: string
}

type TerminalState =
  | { kind: 'complete'; runId: string; checkpoints: number[] }
  | { kind: 'error'; step?: string; error: string }

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

function statusDotClass(status: BootstrapStepState): string {
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

function statusLabel(status: BootstrapStepState): string {
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

function checkpointBadge(checkpoint: BootstrapCheckpoint): ReactNode {
  return (
    <span className="inline-flex items-center gap-2 rounded-full border border-accent/30 bg-accent/10 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.24em] text-accentSoft">
      <span>#{checkpoint.n} sealed</span>
      <span className="normal-case tracking-normal text-muted">{checkpoint.artifact_name}</span>
    </span>
  )
}

// P2-3: CA card on post-verify completion
function BootstrapCaCard(props: { envId: string }) {
  const [caPayload, setCaPayload] = useState<CaCertPayload | null>(null)
  const [caError, setCaError] = useState<string | null>(null)
  const [caRefreshing, setCaRefreshing] = useState(false)

  const fetchCa = () => {
    setCaRefreshing(true)
    setCaError(null)
    fetch(`/api/ca-cert/${encodeURIComponent(props.envId)}`, {
      credentials: 'same-origin',
      headers: { accept: 'application/json' },
    })
      .then((resp) => {
        if (!resp.ok) throw new Error(`CA cert unavailable (${resp.status})`)
        return resp.json() as Promise<CaCertPayload>
      })
      .then((payload) => {
        setCaPayload(payload)
        setCaRefreshing(false)
      })
      .catch((err) => {
        setCaError(err instanceof Error ? err.message : String(err))
        setCaRefreshing(false)
      })
  }

  useEffect(() => { fetchCa() }, [props.envId])

  if (!caPayload && !caError && !caRefreshing) return null

  return (
    <section className="rounded-3xl border border-accent/30 bg-accent/10 p-5">
      <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">CA Certificate</p>
      <h3 className="mt-2 text-xl font-semibold text-text">Install the DMF Local CA</h3>
      <p className="mt-2 text-sm leading-6 text-muted">
        The cluster serves all its HTTPS services via its own local CA; until your OS/browser
        trusts it you get warnings AND passkey/WebAuthn enrollment fails SILENTLY.
      </p>

      {caPayload?.present ? (
        <div className="mt-4">
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={() => {
                const blob = new Blob([caPayload.pem], { type: 'application/x-x509-ca-cert' })
                const url = URL.createObjectURL(blob)
                const a = document.createElement('a')
                a.href = url
                a.download = caPayload.filename
                a.click()
                URL.revokeObjectURL(url)
              }}
              className="rounded-lg border border-accent/30 bg-accent/10 px-3 py-1.5 text-xs font-medium text-accentSoft transition hover:bg-accent/20"
            >
              Download {caPayload.filename}
            </button>
          </div>
          <div className="mt-4">
            <CaInstall payload={caPayload} onCopyError={(msg) => setCaError(msg)} />
          </div>
        </div>
      ) : (
        <div className="mt-3">
          <p className="text-sm text-muted">{caError || 'CA not yet available.'}</p>
          <button
            type="button"
            onClick={fetchCa}
            disabled={caRefreshing}
            className="mt-2 rounded-lg border border-border/70 bg-white/5 px-3 py-1.5 text-xs text-muted transition hover:bg-white/8 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {caRefreshing ? 'Refreshing…' : 'Refresh / retry'}
          </button>
        </div>
      )}
    </section>
  )
}

export default function BootstrapView(props: {
  envId: string
  passphrase: string
  operatorUsername: string
}) {
  const [runId, setRunId] = useState<string | null>(null)
  const [runStatus, setRunStatus] = useState<'idle' | 'starting' | 'running' | 'complete' | 'error'>(
    'idle',
  )
  const [startError, setStartError] = useState<string | null>(null)
  const [streamError, setStreamError] = useState<string | null>(null)
  const [steps, setSteps] = useState<string[]>([])
  const [stepStatuses, setStepStatuses] = useState<Record<string, BootstrapStepState>>({})
  const [currentStep, setCurrentStep] = useState<string | null>(null)
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [checkpoints, setCheckpoints] = useState<BootstrapCheckpoint[]>([])
  const [activePause, setActivePause] = useState<ActivePause | null>(null)
  const [terminal, setTerminal] = useState<TerminalState | null>(null)
  const [reconnectNote, setReconnectNote] = useState<string | null>(null)
  const [resumeBusy, setResumeBusy] = useState(false)
  const [resumeError, setResumeError] = useState<string | null>(null)
  const [passkeyChecking, setPasskeyChecking] = useState(false)
  const [passkeyStatus, setPasskeyStatus] = useState<string | null>(null)
  const [cursor, setCursor] = useState(0)
  const cursorRef = useRef(0)
  const terminalRef = useRef<TerminalState | null>(null)

  const stepsByStatus = useMemo(() => {
    return steps.map((step) => ({
      id: step,
      status: stepStatuses[step] ?? 'pending',
    }))
  }, [steps, stepStatuses])

  useEffect(() => {
    if (!runId) return

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
      const response = await fetch(`/api/bootstrap/stream/${runId}?from=${from}`, {
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
        const typedEvent = event as Partial<BootstrapEvent> & { event?: string }
        switch (typedEvent.event) {
          case 'run_start': {
            const startEvent = typedEvent as BootstrapRunStartEvent
            setSteps(startEvent.steps)
            setStepStatuses(
              Object.fromEntries(startEvent.steps.map((step) => [step, 'pending'] as const)),
            )
            setCurrentStep(null)
            setReconnectNote(null)
            break
          }
          case 'step_start': {
            const stepEvent = typedEvent as BootstrapStepStartEvent
            setCurrentStep(stepEvent.step)
            setStepStatuses((previous) => ({
              ...previous,
              [stepEvent.step]: 'running',
            }))
            break
          }
          case 'log': {
            const logEvent = typedEvent as BootstrapLogEvent
            setLogs((previous) => [...previous, { step: logEvent.step, line: logEvent.line }])
            break
          }
          case 'step_complete': {
            const stepCompleteEvent = typedEvent as BootstrapStepCompleteEvent
            setStepStatuses((previous) => ({
              ...previous,
              [stepCompleteEvent.step]: stepCompleteEvent.status,
            }))
            break
          }
          case 'checkpoint': {
            const checkpointEvent = typedEvent as BootstrapCheckpointEvent
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
          case 'pause': {
            const pauseEvent = typedEvent as BootstrapPauseEvent
              if (pauseEvent.pause_id === 'ca-cert') {
                setActivePause({
                  pause_id: 'ca-cert',
                  title: pauseEvent.title,
                  payload: pauseEvent.payload as CaCertPayload,
              })
            } else if (pauseEvent.pause_id === 'hosts-map') {
              setActivePause({
                pause_id: 'hosts-map',
                title: pauseEvent.title,
                payload: pauseEvent.payload as HostsMapPayload,
              })
            } else {
                setActivePause({
                  pause_id: 'passkey',
                  title: pauseEvent.title,
                  payload: pauseEvent.payload as PasskeyPayload,
                })
              }
              setResumeError(null)
              setPasskeyChecking(false)
              setPasskeyStatus(null)
              break
            }
          case 'resume': {
            setActivePause(null)
            setResumeError(null)
            setResumeBusy(false)
            break
          }
          case 'error': {
            const errorEvent = typedEvent as BootstrapErrorEvent
            terminalRef.current = {
              kind: 'error',
              step: errorEvent.step,
              error: errorEvent.error,
            }
            setTerminal(terminalRef.current)
            setRunStatus('error')
            setStreamError(errorEvent.error)
            sawTerminal = true
            break
          }
          case 'complete': {
            const completeEvent = typedEvent as BootstrapCompleteEvent
            terminalRef.current = {
              kind: 'complete',
              runId: completeEvent.run_id,
              checkpoints: completeEvent.checkpoints,
            }
            setTerminal(terminalRef.current)
            setRunStatus('complete')
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
  }, [runId])

  useEffect(() => {
    if (!logs.length) return
    const logBox = document.getElementById('bootstrap-log-console')
    if (logBox) {
      logBox.scrollTop = logBox.scrollHeight
    }
  }, [logs])

  async function startBootstrap() {
    setStartError(null)
    setStreamError(null)
    setTerminal(null)
    terminalRef.current = null
    setRunStatus('starting')
    setLogs([])
    setSteps([])
    setStepStatuses({})
    setCurrentStep(null)
    setCheckpoints([])
    setActivePause(null)
    setReconnectNote(null)
    cursorRef.current = 0

    try {
      const response = await fetchJson<BootstrapStartResponse>('/api/bootstrap/start', {
        env_id: props.envId,
        passphrase: props.passphrase,
        passphrase_confirm: props.passphrase,
      })
      setRunId(response.run_id)
      setRunStatus('running')
    } catch (error) {
      setRunStatus('error')
      setStartError(error instanceof Error ? error.message : String(error))
    }
  }

  async function resumePause() {
    if (!runId || !activePause) return

    setResumeBusy(true)
    setResumeError(null)

    try {
      await fetchJson('/api/bootstrap/resume', {
        run_id: runId,
        pause_id: activePause.pause_id,
        payload: null,
      })
      setActivePause(null)
    } catch (error) {
      setResumeError(error instanceof Error ? error.message : String(error))
    } finally {
      setResumeBusy(false)
    }
  }

  function shellQuote(value: string): string {
    return `'${value.replaceAll("'", "'\\''")}'`
  }

  async function copyText(text: string) {
    await navigator.clipboard.writeText(text)
  }

  function downloadCaCertificate(payload: CaCertPayload) {
    const blob = new Blob([payload.pem], { type: 'application/x-pem-file' })
    const objectUrl = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = objectUrl
    anchor.download = payload.filename
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0)
  }

  function hostsMapCommand(entries: string[]): string {
    return `printf '%s\\n' ${entries.map(shellQuote).join(' ')} | sudo tee -a /etc/hosts >/dev/null`
  }

  async function copyHostsMap(payload: HostsMapPayload) {
    await copyText(payload.entries.join('\n'))
  }

  async function verifyPasskeyAndContinue() {
    if (!runId || !activePause || activePause.pause_id !== 'passkey') return

    setPasskeyChecking(true)
    setResumeError(null)
    try {
      const response = await fetch(`/api/bootstrap/passkey/${runId}`, {
        method: 'GET',
        credentials: 'same-origin',
        headers: {
          accept: 'application/json',
        },
      })
      if (!response.ok) {
        throw new Error(await readError(response))
      }
      const payload = (await response.json()) as { confirmed: number; required: number }
      const statusText = `${payload.confirmed}/${payload.required}`
      setPasskeyStatus(statusText)
      if (payload.confirmed >= payload.required) {
        await resumePause()
        return
      }
      setResumeError(`Passkey enrollment is not complete yet (${statusText}).`)
    } catch (error) {
      setResumeError(error instanceof Error ? error.message : String(error))
    } finally {
      setPasskeyChecking(false)
    }
  }

  function stepCompletionLine(): string {
    if (terminal?.kind !== 'complete') {
      return ''
    }
    const done = terminal.checkpoints
    if (!done.length) {
      return 'All checkpoints sealed.'
    }
    const formatted = done.map((checkpoint) => `#${checkpoint}`).join(' and ')
    return `${formatted} sealed.`
  }

  return (
    <section className="rounded-[1.75rem] border border-border/70 bg-panel/90 p-5 shadow-glow backdrop-blur">
      <div className="flex flex-col gap-4 border-b border-border/60 pb-5 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Bootstrap</p>
          <h2 className="mt-2 text-2xl font-semibold text-text">Run the sandbox bootstrap</h2>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-muted">
            This console consumes the streamed bootstrap events, keeps a reconnect cursor, and
            gates the three operator pauses until the run resumes.
          </p>
        </div>
        <div className="rounded-full border border-border/70 bg-white/5 px-4 py-2 text-sm text-muted">
          <span className="text-text">{runStatus}</span>
          <span className="ml-2">· {props.envId}</span>
        </div>
      </div>

      {!runId ? (
        <div className="grid gap-4 py-6 lg:grid-cols-[1.2fr_0.8fr]">
          <div className="rounded-3xl border border-border/70 bg-white/5 p-5">
            <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Ready</p>
            <h3 className="mt-2 text-xl font-semibold text-text">Checkpoint #1 is sealed.</h3>
            <p className="mt-3 text-sm leading-6 text-muted">
              Start the live bootstrap when you are ready to work through the three pauses and the
              final verification pass.
            </p>
            {startError ? (
              <div className="mt-4 rounded-2xl border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-100">
                {startError}
              </div>
            ) : null}
            <div className="mt-5 flex flex-wrap items-center gap-3">
              <button
                type="button"
                onClick={startBootstrap}
                className="rounded-2xl border border-accent/30 bg-accent px-6 py-3 text-sm font-semibold text-bg transition-transform hover:-translate-y-0.5 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {runStatus === 'starting' ? 'Starting…' : 'Run bootstrap'}
              </button>
              <p className="text-sm text-muted">Passphrase and remotes are reused from the create-new flow.</p>
            </div>
          </div>
          <div className="rounded-3xl border border-border/70 bg-[linear-gradient(180deg,rgba(15,23,42,0.92),rgba(2,6,23,0.8))] p-5 text-sm leading-6 text-muted">
            <p className="text-text">What this run will do</p>
            <ul className="mt-3 grid gap-2">
              <li>1. Stream the pre-seed, seed, configure, and verify steps live.</li>
              <li>2. Pause for CA cert, hosts mapping, and passkey enrollment.</li>
              <li>3. Re-back up checkpoint #2 and checkpoint #3 as the run progresses.</li>
            </ul>
          </div>
        </div>
      ) : (
        <div className="grid gap-6 py-6 lg:grid-cols-[0.95fr_1.05fr]">
          <div className="grid gap-5">
            <section className="rounded-3xl border border-border/70 bg-white/5 p-5">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Step rail</p>
                  <h3 className="mt-2 text-lg font-semibold text-text">Ordered bootstrap steps</h3>
                </div>
                {reconnectNote ? (
                  <div className="rounded-full border border-border/70 bg-white/5 px-3 py-1 text-xs text-muted">
                    {reconnectNote}
                  </div>
                ) : null}
              </div>
              <div className="mt-4 grid gap-3">
                {stepsByStatus.length ? (
                  stepsByStatus.map((step, index) => {
                    const checkpointNumber = step.id === 'checkpoint-2' ? 2 : step.id === 'checkpoint-3' ? 3 : null
                    const checkpoint = checkpointNumber
                      ? checkpoints.find((entry) => entry.n === checkpointNumber)
                      : null
                    const isCurrent = currentStep === step.id
                    return (
                      <div
                        key={step.id}
                        className={[
                          'rounded-2xl border p-4 transition',
                          isCurrent
                            ? 'border-accent/40 bg-accent/10'
                            : 'border-border/70 bg-black/20',
                        ].join(' ')}
                      >
                        <div className="flex flex-wrap items-center gap-3">
                          <span className={['h-3 w-3 rounded-full', statusDotClass(step.status)].join(' ')} />
                          <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="text-sm font-medium text-text">{step.id}</span>
                              <span className="rounded-full border border-border/70 bg-white/5 px-2.5 py-0.5 text-[11px] uppercase tracking-[0.22em] text-muted">
                                {statusLabel(step.status)}
                              </span>
                            </div>
                            <p className="mt-1 text-xs uppercase tracking-[0.22em] text-muted">
                              Step {index + 1}
                            </p>
                          </div>
                          {checkpoint ? checkpointBadge(checkpoint) : null}
                        </div>
                      </div>
                    )
                  })
                ) : (
                  <div className="rounded-2xl border border-dashed border-border/70 bg-black/20 p-4 text-sm text-muted">
                    Waiting for the run to stream its step list.
                  </div>
                )}
              </div>
            </section>

            <section className="rounded-3xl border border-border/70 bg-white/5 p-5">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Checkpoints</p>
                  <h3 className="mt-2 text-lg font-semibold text-text">Sealed artifacts</h3>
                </div>
                <div className="rounded-full border border-border/70 bg-white/5 px-3 py-1 text-xs text-muted">
                  {checkpoints.length ? `${checkpoints.length} captured` : 'Waiting'}
                </div>
              </div>
              <div className="mt-4 grid gap-3">
                {checkpoints.length ? (
                  checkpoints.map((checkpoint) => (
                    <div key={checkpoint.n} className="rounded-2xl border border-border/70 bg-black/20 p-4">
                      {checkpointBadge(checkpoint)}
                    </div>
                  ))
                ) : (
                  <div className="rounded-2xl border border-dashed border-border/70 bg-black/20 p-4 text-sm text-muted">
                    Checkpoint #2 and checkpoint #3 will appear here after they seal.
                  </div>
                )}
              </div>
            </section>
          </div>

          <div className="grid gap-5">
            <section className="rounded-3xl border border-border/70 bg-[linear-gradient(180deg,rgba(15,23,42,0.92),rgba(2,6,23,0.8))] p-5 shadow-glow">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Live log console</p>
                  <h3 className="mt-2 text-lg font-semibold text-text">Server-redacted stream</h3>
                </div>
                <div className="rounded-full border border-border/70 bg-white/5 px-3 py-1 text-xs text-muted">
                  cursor {cursor}
                </div>
              </div>
              <pre
                id="bootstrap-log-console"
                className="mt-4 max-h-[34rem] overflow-auto whitespace-pre-wrap rounded-3xl border border-border/70 bg-black/30 p-4 text-xs leading-6 text-text"
              >
                {logs.length
                  ? logs.map((entry) => `${entry.step}: ${entry.line}`).join('\n')
                  : 'Waiting for bootstrap logs...'}
              </pre>
            </section>

            {terminal?.kind === 'complete' ? (
              <>
                <section className="rounded-3xl border border-emerald-400/30 bg-emerald-400/10 p-5">
                  <p className="text-xs uppercase tracking-[0.34em] text-emerald-200">Complete</p>
                  <h3 className="mt-2 text-2xl font-semibold text-text">Bootstrap verified.</h3>
                  <p className="mt-3 text-sm leading-6 text-muted">
                    {stepCompletionLine()} Safe to delete the container once you have kept the sealed
                    artifacts and passphrase record.
                  </p>
                  <div className="mt-4 rounded-2xl border border-border/70 bg-white/5 p-4 text-sm text-muted">
                    Run ID <span className="text-text">{terminal.runId}</span>
                  </div>
                </section>

                {/* CA Certificate Card — post-verify when CA reliably exists */}
                <BootstrapCaCard envId={props.envId} />
              </>
            ) : null}

            {terminal?.kind === 'error' ? (
              <section className="rounded-3xl border border-red-500/30 bg-red-500/10 p-5">
                <p className="text-xs uppercase tracking-[0.34em] text-red-200">Error</p>
                <h3 className="mt-2 text-2xl font-semibold text-text">Bootstrap stopped.</h3>
                <p className="mt-3 text-sm leading-6 text-red-100">
                  {terminal.step ? `Step ${terminal.step}: ` : ''}
                  {terminal.error}
                </p>
              </section>
            ) : null}

            {streamError && terminal === null ? (
              <section className="rounded-3xl border border-red-500/30 bg-red-500/10 p-5">
                <p className="text-xs uppercase tracking-[0.34em] text-red-200">Transport</p>
                <h3 className="mt-2 text-lg font-semibold text-text">Stream issue</h3>
                <p className="mt-3 text-sm leading-6 text-red-100">{streamError}</p>
              </section>
            ) : null}
          </div>
        </div>
      )}

      {activePause ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 px-4 py-6 backdrop-blur-sm">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="bootstrap-pause-title"
            className="w-full max-w-3xl rounded-[2rem] border border-border/70 bg-[linear-gradient(180deg,rgba(15,23,42,0.98),rgba(2,6,23,0.96))] p-5 shadow-[0_40px_120px_rgba(0,0,0,0.55)]"
          >
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Pause</p>
                <h3 id="bootstrap-pause-title" className="mt-2 text-2xl font-semibold text-text">
                  {activePause.title}
                </h3>
              </div>
              <span className="rounded-full border border-border/70 bg-white/5 px-3 py-1 text-xs text-muted">
                {activePause.pause_id}
              </span>
            </div>

            <div className="mt-5 grid gap-4">
              {activePause.pause_id === 'ca-cert' ? (
                <>
                  <div className="rounded-2xl border border-amber-500/40 bg-amber-500/10 p-4">
                    <p className="text-sm font-semibold text-amber-300">
                      Required step — passkeys will not work without this.
                    </p>
                    <p className="mt-1 text-sm leading-6 text-muted">
                      If the DMF Local CA is not trusted, WebAuthn passkey enrollment fails silently:
                      &quot;Registration cancelled or timed out&quot; means the browser treated
                      the origin as non-secure, NOT that you cancelled anything.
                    </p>
                  </div>
                  {activePause.payload.requirement_note && (
                    <p className="text-sm leading-6 text-muted">
                      {activePause.payload.requirement_note}
                    </p>
                  )}
                  <p className="text-sm leading-6 text-muted">
                    Download the CA certificate and install it in your browser&apos;s trust store.
                    The PEM is public.
                  </p>
                  {activePause.payload.present ? (
                    <div className="grid gap-4">
                      <pre className="max-h-72 overflow-auto rounded-3xl border border-border/70 bg-black/30 p-4 text-xs leading-6 text-text">
                        {activePause.payload.pem}
                      </pre>
                      <div className="flex flex-wrap items-center gap-3">
                        <button
                          type="button"
                          onClick={() => downloadCaCertificate(activePause.payload)}
                          className="rounded-2xl border border-accent/30 bg-accent px-5 py-3 text-sm font-semibold text-bg transition-transform hover:-translate-y-0.5"
                        >
                          Download
                        </button>
                        <span className="text-sm text-muted">{activePause.payload.filename}</span>
                      </div>
                      <CaInstall payload={activePause.payload} onCopyError={setResumeError} />
                    </div>
                  ) : (
                    <div className="rounded-2xl border border-border/70 bg-white/5 p-4 text-sm text-muted">
                      <p>{activePause.payload.note || 'The CA certificate is not available yet.'}</p>
                      <p className="mt-2">
                        Retry after the certificate pause becomes available or continue once the live
                        fetch succeeds.
                      </p>
                    </div>
                  )}
                </>
              ) : null}

              {activePause.pause_id === 'hosts-map' ? (
                <>
                  <p className="text-sm leading-6 text-muted">{activePause.payload.note}</p>
                  <p className="text-sm leading-6 text-muted">{activePause.payload.dns_note}</p>
                  <div className="grid gap-4">
                    <div className="rounded-3xl border border-border/70 bg-black/30 p-4">
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <p className="text-xs uppercase tracking-[0.24em] text-accentSoft">/etc/hosts entries</p>
                        <button
                          type="button"
                          onClick={() => {
                            void copyHostsMap(activePause.payload).catch(() => {
                              setResumeError('Unable to copy hosts mapping.')
                            })
                          }}
                          className="rounded-2xl border border-border/70 bg-white/5 px-4 py-2 text-xs font-semibold text-text transition hover:bg-white/10"
                        >
                          Copy
                        </button>
                      </div>
                      <pre className="mt-3 max-h-72 overflow-auto rounded-2xl border border-border/70 bg-slate-950/80 p-3 text-xs leading-6 text-text">
                        {activePause.payload.entries.join('\n') || 'No entries yet.'}
                      </pre>
                    </div>
                    <div className="rounded-3xl border border-border/70 bg-black/30 p-4">
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <p className="text-xs uppercase tracking-[0.24em] text-accentSoft">One-liner</p>
                        <button
                          type="button"
                          onClick={() => {
                            void copyText(hostsMapCommand(activePause.payload.entries)).catch(() => {
                              setResumeError('Unable to copy hosts command.')
                            })
                          }}
                          className="rounded-2xl border border-border/70 bg-white/5 px-4 py-2 text-xs font-semibold text-text transition hover:bg-white/10"
                        >
                          Copy
                        </button>
                      </div>
                      <pre className="mt-3 max-h-72 overflow-auto rounded-2xl border border-border/70 bg-slate-950/80 p-3 text-xs leading-6 text-text">
                        {hostsMapCommand(activePause.payload.entries)}
                      </pre>
                    </div>
                  </div>
                </>
              ) : null}

              {activePause.pause_id === 'passkey' ? (
                <>
                  <p className="text-sm leading-6 text-muted">{activePause.payload.hint}</p>
                  <div className="grid gap-3 rounded-3xl border border-border/70 bg-black/30 p-4">
                    <div className="grid gap-2 text-sm leading-6 text-muted">
                      <div>
                        A. Open the enrollment link:
                        {activePause.payload.enrollment_url ? (
                          <a
                            className="ml-2 inline-flex w-fit rounded-2xl border border-border/70 bg-white/5 px-3 py-1.5 text-text transition hover:bg-white/8"
                            href={activePause.payload.enrollment_url}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            {activePause.payload.enrollment_url}
                          </a>
                        ) : (
                          <span className="ml-2 text-text">
                            {activePause.payload.confirmed >= activePause.payload.required
                              ? `already enrolled (${activePause.payload.confirmed}/${activePause.payload.required})`
                              : 'No enrollment URL is available yet.'}
                          </span>
                        )}
                      </div>
                      <div>
                        B. In Console, open Create new device invitation and complete enrollment.
                      </div>
                      <div className="text-text">Operator username: {props.operatorUsername}</div>
                    </div>
                  </div>

                  {activePause.payload.enrollment_url && (
                    <div className="flex flex-col items-start gap-2 rounded-3xl border border-border/70 bg-black/30 p-4">
                      <p className="text-xs uppercase tracking-[0.24em] text-accentSoft">Scan to enroll on phone</p>
                      <div className="rounded-2xl border border-border/70 bg-white/5 p-3">
                        <QRCodeSVG value={activePause.payload.enrollment_url} size={160} level="M" />
                      </div>
                      <p className="text-[11px] leading-5 text-muted">
                        Note: a QR for a <span className="text-text">.test</span> domain won't resolve on a phone off-network — mainly useful for real-domain cloud envs.
                      </p>
                    </div>
                  )}

                  <div className="flex flex-wrap items-center gap-3">
                    <button
                      type="button"
                      onClick={() => {
                        void verifyPasskeyAndContinue()
                      }}
                      disabled={passkeyChecking || resumeBusy}
                      className="rounded-2xl border border-accent/30 bg-accent px-6 py-3 text-sm font-semibold text-bg transition-transform hover:-translate-y-0.5 disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      {passkeyChecking ? 'Checking…' : resumeBusy ? 'Continuing…' : 'Verify & Continue'}
                    </button>
                    {passkeyStatus ? (
                      <span className="rounded-full border border-border/70 bg-white/5 px-3 py-1 text-xs text-muted">
                        {passkeyStatus}
                      </span>
                    ) : null}
                  </div>
                </>
              ) : null}
            </div>

            {resumeError ? (
              <div className="mt-4 rounded-2xl border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-100">
                {resumeError}
              </div>
            ) : null}

            {activePause.pause_id === 'passkey' ? (
              <div className="mt-5 border-t border-border/60 pt-4">
                <span className="text-sm text-muted">
                  The modal stays locked until the passkey check confirms the required count.
                </span>
              </div>
            ) : (
              <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-border/60 pt-4">
                <button
                  type="button"
                  disabled={resumeBusy}
                  onClick={() => {
                    void resumePause()
                  }}
                  className="rounded-2xl border border-accent/30 bg-accent px-6 py-3 text-sm font-semibold text-bg transition-transform hover:-translate-y-0.5 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {resumeBusy ? 'Continuing…' : "I've completed these steps → Continue"}
                </button>
                <span className="text-sm text-muted">The modal stays locked until the pause is resumed.</span>
              </div>
            )}
          </div>
        </div>
      ) : null}
    </section>
  )
}
