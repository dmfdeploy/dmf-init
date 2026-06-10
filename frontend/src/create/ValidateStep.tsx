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
    return () => { cancelled = true }
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
        headers: { 'content-type': 'application/json', accept: 'application/json' },
        body: JSON.stringify({ env_id: envId }),
      })
      if (!response.ok) {
        const text = await response.text()
        throw new Error(text)
      }
      const data = (await response.json()) as { run_id: string }
      dispatch({ type: 'run_started', runId: data.run_id, steps: ['doctor'] })
    } catch (error) {
      setStartError(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto max-w-2xl grid gap-4">
      <div className="rounded-lg border border-border bg-panel p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="text-xs uppercase tracking-[0.2em] text-muted">Validate</p>
            <h2 className="mt-0.5 text-lg font-semibold text-text">Doctor check</h2>
            <p className="mt-1 text-sm text-muted">
              Re-run the doctor check to verify cluster health.
            </p>
            {pkgDownloaded !== null && (
              <p className="mt-1.5 text-sm" aria-live="polite">
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
            className="rounded-lg border border-border bg-white/5 px-3 py-1.5 text-sm text-text transition hover:bg-white/8"
          >
            ← Back
          </button>
        </div>

        {startError && (
          <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 p-2.5 text-sm text-red-100">
            {startError}
          </div>
        )}

        {!state.runId && (
          <div className="mt-3">
            <button
              type="button"
              onClick={runDoctor}
              disabled={busy}
              className="rounded-lg border border-accent/30 bg-accent px-4 py-2 text-sm font-semibold text-bg transition hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {busy ? 'Starting…' : 'Run doctor check'}
            </button>
          </div>
        )}
      </div>

      {state.runId && (
        <>
          {/* Step rail */}
          <div className="rounded-lg border border-border bg-panel p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <div>
                <p className="text-xs uppercase tracking-[0.2em] text-muted">Doctor steps</p>
                <p className="mt-0.5 text-sm text-muted">
                  {state.currentStep ? `Current: ${state.currentStep}` : 'Waiting…'}
                </p>
              </div>
              {reconnectNote && (
                <span className="rounded-md border border-border bg-bg px-2 py-0.5 text-[11px] text-muted">
                  {reconnectNote}
                </span>
              )}
            </div>

            <div className="grid gap-1.5">
              {state.steps.length ? (
                state.steps.map((step) => {
                  const status = state.stepStatuses[step] ?? 'pending'
                  const isCurrent = state.currentStep === step
                  return (
                    <div
                      key={step}
                      className={[
                        'flex items-center gap-2 rounded-md px-2.5 py-1.5 text-sm transition',
                        isCurrent ? 'bg-accent/10 text-text' : 'text-muted',
                      ].join(' ')}
                    >
                      <StatusDot status={status} />
                      <span className="font-medium">{step}</span>
                    </div>
                  )
                })
              ) : (
                <p className="text-sm text-muted">Waiting for doctor steps…</p>
              )}
            </div>
          </div>

          {/* Terminal states */}
          {state.terminal?.kind === 'complete' && (
            <div className="rounded-lg border border-emerald-400/30 bg-emerald-400/10 p-4">
              <p className="text-xs uppercase tracking-[0.2em] text-emerald-200">Complete</p>
              <h3 className="mt-0.5 text-lg font-semibold text-text">Doctor check passed.</h3>
              <p className="mt-1 text-sm text-muted">All doctor checks completed successfully.</p>
            </div>
          )}

          {state.terminal?.kind === 'error' && (
            <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-4">
              <p className="text-xs uppercase tracking-[0.2em] text-red-200">Error</p>
              <h3 className="mt-0.5 text-lg font-semibold text-text">Doctor check failed.</h3>
              {state.terminal.error && (
                <p className="mt-1 text-sm text-red-100">{state.terminal.error}</p>
              )}
            </div>
          )}

          {streamError && !state.terminal && (
            <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-4">
              <p className="text-xs uppercase tracking-[0.2em] text-red-200">Transport</p>
              <h3 className="mt-0.5 text-base font-semibold text-text">Stream issue</h3>
              <p className="mt-1 text-sm text-red-100">{streamError}</p>
            </div>
          )}

          {/* Collapsible log */}
          <Disclosure summary="Show details" defaultOpen={false}>
            <LogConsole logs={logs as LogEntry[]} cursor={cursor} id="validate-log-console" maxHeight="16rem" />
          </Disclosure>
        </>
      )}
    </div>
  )
}
