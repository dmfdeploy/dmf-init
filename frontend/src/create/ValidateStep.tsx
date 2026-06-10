import { useCallback, useEffect, useReducer, useState } from 'react'
import { useEventStream } from '../hooks/useEventStream'
import { StatusDot } from '../shared/StatusDot'
import { Disclosure } from '../shared/Disclosure'
import { LogConsole, type LogEntry } from '../shared/LogConsole'

type StepState = 'pending' | 'running' | 'ok' | 'error'

type ValidateStepProps = {
  envId: string
  onBack: () => void
}

type ValidateState = {
  runId: string | null
  steps: string[]
  stepStatuses: Record<string, StepState>
  currentStep: string | null
  checkpoints: { n: number; artifact_name: string }[]
  terminal: { kind: 'complete' | 'error'; runId?: string; checkpoints?: number[]; step?: string; error?: string } | null
}

type ValidateAction =
  | { type: 'run_started'; runId: string; steps: string[] }
  | { type: 'step_start'; step: string }
  | { type: 'step_complete'; step: string; status: 'ok' | 'error' }
  | { type: 'checkpoint'; n: number; artifact_name: string }
  | { type: 'error'; step?: string; error: string }
  | { type: 'complete'; runId: string; checkpoints: number[] }

function validateReducer(state: ValidateState, action: ValidateAction): ValidateState {
  switch (action.type) {
    case 'run_started':
      return {
        ...state,
        runId: action.runId,
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

export function ValidateStep({ envId, onBack }: ValidateStepProps) {
  const [state, dispatch] = useReducer(validateReducer, {
    runId: null,
    steps: [],
    stepStatuses: {},
    currentStep: null,
    checkpoints: [],
    terminal: null,
  })
  const [busy, setBusy] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  const [pkgDownloaded, setPkgDownloaded] = useState<boolean | null>(null)

  // Validation also reports whether the recovery package was downloaded —
  // the other half of "safe to delete".
  useEffect(() => {
    let cancelled = false
    fetch(`/api/package/${encodeURIComponent(envId)}/status`, {
      credentials: 'same-origin',
      headers: { accept: 'application/json' },
    })
      .then((response) => (response.ok ? response.json() : null))
      .then((payload: { downloaded_at: number | null } | null) => {
        if (!cancelled && payload) setPkgDownloaded(Boolean(payload.downloaded_at))
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [envId])

  const handleEvent = useCallback(
    (event: Record<string, unknown>) => {
      const eventType = event.event as string | undefined
      switch (eventType) {
        case 'run_start': {
          const e = event as { run_id: string; steps: string[] }
          dispatch({ type: 'run_started', runId: e.run_id, steps: e.steps })
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
          break
        }
        case 'complete': {
          const e = event as { run_id: string; checkpoints: number[] }
          dispatch({ type: 'complete', runId: e.run_id, checkpoints: e.checkpoints })
          break
        }
      }
    },
    [],
  )

  // Stream hook — transport only
  const { logs, cursor, reconnectNote, streamError } = useEventStream({
    runId: state.runId,
    onEvent: handleEvent,
  })

  async function runDoctor() {
    setBusy(true)
    setStartError(null)
    try {
      const response = await fetch('/api/bootstrap/doctor', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'content-type': 'application/json',
          accept: 'application/json',
        },
        body: JSON.stringify({ env_id: envId }),
      })
      if (!response.ok) {
        const text = await response.text()
        throw new Error(text)
      }
      const data = (await response.json()) as { run_id: string }
      // The stream will pick up from runId; run_start event will set steps
      // We need to set runId so the stream hook starts
      // But we don't know steps yet — set a minimal state
      dispatch({ type: 'run_started', runId: data.run_id, steps: ['doctor'] })
    } catch (error) {
      setStartError(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid gap-6">
      <section className="rounded-[1.75rem] border border-border/70 bg-panel/80 p-5 shadow-glow backdrop-blur">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Validate</p>
            <h2 className="mt-2 text-2xl font-semibold text-text">Doctor check</h2>
            <p className="mt-2 text-sm leading-6 text-muted">
              Re-run the doctor check to verify cluster health.
            </p>
            {pkgDownloaded !== null && (
              <p className="mt-2 text-sm leading-6" aria-live="polite">
                {pkgDownloaded ? (
                  <span className="text-emerald-200">✓ Recovery package downloaded.</span>
                ) : (
                  <span className="text-amber-300">
                    Recovery package not downloaded yet — do that before deleting the container.
                  </span>
                )}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={onBack}
            className="rounded-2xl border border-border/70 bg-white/5 px-4 py-2 text-sm text-text transition hover:bg-white/8"
          >
            ← Back to Finish
          </button>
        </div>

        {startError && (
          <div className="mt-4 rounded-3xl border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-100">
            {startError}
          </div>
        )}

        {!state.runId && (
          <div className="mt-5">
            <button
              type="button"
              onClick={runDoctor}
              disabled={busy}
              className="rounded-2xl border border-accent/30 bg-accent px-6 py-3 text-sm font-semibold text-bg transition-transform hover:-translate-y-0.5 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {busy ? 'Starting…' : 'Run doctor check'}
            </button>
          </div>
        )}
      </section>

      {state.runId && (
        <>
          {/* Step rail */}
          <section className="rounded-[1.75rem] border border-border/70 bg-panel/80 p-5 shadow-glow backdrop-blur">
            <div className="mb-4 flex items-center justify-between gap-3">
              <div>
                <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Doctor steps</p>
                <p className="mt-1 text-sm text-muted">
                  {state.currentStep ? `Current: ${state.currentStep}` : 'Waiting…'}
                </p>
              </div>
              {reconnectNote ? (
                <div className="rounded-full border border-border/70 bg-white/5 px-3 py-1 text-xs text-muted">
                  {reconnectNote}
                </div>
              ) : null}
            </div>

            <div className="grid gap-3">
              {state.steps.length ? (
                state.steps.map((step) => {
                  const status = state.stepStatuses[step] ?? 'pending'
                  const isCurrent = state.currentStep === step
                  return (
                    <div
                      key={step}
                      className={[
                        'rounded-2xl border p-4 transition',
                        isCurrent ? 'border-accent/40 bg-accent/10' : 'border-border/70 bg-black/20',
                      ].join(' ')}
                    >
                      <div className="flex flex-wrap items-center gap-3">
                        <StatusDot status={status} />
                        <span className="text-sm font-medium text-text">{step}</span>
                      </div>
                    </div>
                  )
                })
              ) : (
                <div className="rounded-2xl border border-dashed border-border/70 bg-black/20 p-4 text-sm text-muted">
                  Waiting for doctor steps…
                </div>
              )}
            </div>
          </section>

          {/* Terminal states */}
          {state.terminal?.kind === 'complete' && (
            <section className="rounded-3xl border border-emerald-400/30 bg-emerald-400/10 p-5">
              <p className="text-xs uppercase tracking-[0.34em] text-emerald-200">Complete</p>
              <h3 className="mt-2 text-2xl font-semibold text-text">Doctor check passed.</h3>
              <p className="mt-3 text-sm leading-6 text-muted">
                All doctor checks completed successfully.
              </p>
            </section>
          )}

          {state.terminal?.kind === 'error' && (
            <section className="rounded-3xl border border-red-500/30 bg-red-500/10 p-5">
              <p className="text-xs uppercase tracking-[0.34em] text-red-200">Error</p>
              <h3 className="mt-2 text-2xl font-semibold text-text">Doctor check failed.</h3>
              {state.terminal.error && (
                <p className="mt-3 text-sm leading-6 text-red-100">{state.terminal.error}</p>
              )}
            </section>
          )}

          {streamError && !state.terminal && (
            <section className="rounded-3xl border border-red-500/30 bg-red-500/10 p-5">
              <p className="text-xs uppercase tracking-[0.34em] text-red-200">Transport</p>
              <h3 className="mt-2 text-lg font-semibold text-text">Stream issue</h3>
              <p className="mt-3 text-sm leading-6 text-red-100">{streamError}</p>
            </section>
          )}

          {/* Collapsible log */}
          <Disclosure summary="Show details" defaultOpen={false}>
            <div className="rounded-[1.75rem] border border-border/70 bg-panel/80 p-5 shadow-glow backdrop-blur">
              <div className="mb-3 flex items-center justify-between">
                <div>
                  <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Doctor log</p>
                </div>
                <div className="rounded-full border border-border/70 bg-white/5 px-3 py-1 text-xs text-muted">
                  {cursor} events
                </div>
              </div>
              <LogConsole logs={logs as LogEntry[]} cursor={cursor} id="validate-log-console" />
            </div>
          </Disclosure>
        </>
      )}
    </div>
  )
}
