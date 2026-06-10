import { useCallback, useReducer, useState } from 'react'

// ─── Phase model ────────────────────────────────────────────────────────────
export type CreatePhase =
  | 'configure'
  | 'installing'
  | 'connect'
  | 'verifying'
  | 'finish'
  | 'validating'

// ─── Step / event types (from bootstrap stream) ─────────────────────────────
type StepState = 'pending' | 'running' | 'ok' | 'error'

type BootstrapCheckpoint = {
  n: number
  artifact_name: string
}

export type ActivePause =
  | { pause_id: 'ca-cert'; title: string; payload: Record<string, unknown> }
  | { pause_id: 'hosts-map'; title: string; payload: Record<string, unknown> }
  | { pause_id: 'passkey'; title: string; payload: Record<string, unknown> }

type TerminalState =
  | { kind: 'complete'; runId: string; checkpoints: number[] }
  | { kind: 'error'; step?: string; error: string }

// ─── Reducer state ──────────────────────────────────────────────────────────
type CreateState = {
  phase: CreatePhase
  runId: string | null
  steps: string[]
  stepStatuses: Record<string, StepState>
  currentStep: string | null
  checkpoints: BootstrapCheckpoint[]
  activePause: ActivePause | null
  terminal: TerminalState | null
}

type CreateAction =
  | { type: 'start' }
  | { type: 'run_started'; runId: string; steps: string[] }
  | { type: 'step_start'; step: string }
  | { type: 'step_complete'; step: string; status: 'ok' | 'error' }
  | { type: 'checkpoint'; n: number; artifact_name: string }
  | { type: 'pause'; pause: ActivePause }
  | { type: 'resume' }
  | { type: 'error'; step?: string; error: string }
  | { type: 'complete'; runId: string; checkpoints: number[] }

function determinePhase(state: CreateState): CreatePhase {
  // Complete → Finish. A terminal ERROR deliberately does NOT jump to Finish:
  // we stay in the install/verify view so InstallProgress can show the failing
  // step + last log lines (errors-as-content). FinishStep only ever shows the
  // happy "safe to delete" package, which requires verify + checkpoint #3.
  if (state.terminal?.kind === 'complete') return 'finish'

  if (!state.runId) return 'configure'

  // An active pause is unambiguously a Connect station.
  if (state.activePause) return 'connect'

  const st = state.stepStatuses
  // Pause steps emit `step_complete` after resume (orchestrate.py), so once
  // passkey is 'ok' every pause is done and we're in the verify tail.
  const passkeyDone = st['passkey'] === 'ok'
  if (passkeyDone) {
    return st['verify'] === 'ok' ? 'installing' : 'verifying'
  }

  // We're in the Connect region only once the FIRST pause (ca-cert) has begun.
  // Before that, the still-pending pause steps must not pull us out of Install
  // during pre-seed…configure.
  const caStarted = st['ca-cert'] === 'running' || st['ca-cert'] === 'ok'
  if (caStarted) return 'connect'

  return 'installing'
}

function createReducer(state: CreateState, action: CreateAction): CreateState {
  switch (action.type) {
    case 'start':
      return {
        ...state,
        phase: 'installing',
        runId: null,
        steps: [],
        stepStatuses: {},
        currentStep: null,
        checkpoints: [],
        activePause: null,
        terminal: null,
      }

    case 'run_started':
      return {
        ...state,
        runId: action.runId,
        steps: action.steps,
        stepStatuses: Object.fromEntries(
          action.steps.map((s) => [s, 'pending'] as const),
        ),
        currentStep: null,
        activePause: null,
        terminal: null,
      }

    case 'step_start':
      return {
        ...state,
        currentStep: action.step,
        stepStatuses: {
          ...state.stepStatuses,
          [action.step]: 'running',
        },
      }

    case 'step_complete':
      return {
        ...state,
        stepStatuses: {
          ...state.stepStatuses,
          [action.step]: action.status,
        },
      }

    case 'checkpoint':
      if (state.checkpoints.some((c) => c.n === action.n)) return state
      return {
        ...state,
        checkpoints: [
          ...state.checkpoints,
          { n: action.n, artifact_name: action.artifact_name },
        ],
      }

    case 'pause':
      return {
        ...state,
        activePause: action.pause,
      }

    case 'resume':
      return {
        ...state,
        activePause: null,
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

// ─── Hook ────────────────────────────────────────────────────────────────────
export function useCreateFlow() {
  const [state, dispatch] = useReducer(createReducer, {
    phase: 'configure',
    runId: null,
    steps: [],
    stepStatuses: {},
    currentStep: null,
    checkpoints: [],
    activePause: null,
    terminal: null,
  })

  // Passkey polling state
  const [passkeyChecking, setPasskeyChecking] = useState(false)
  const [passkeyStatus, setPasskeyStatus] = useState<string | null>(null)
  const [resumeError, setResumeError] = useState<string | null>(null)
  const [resumeBusy, setResumeBusy] = useState(false)

  const derivePhase = useCallback(
    (s: CreateState) => determinePhase(s),
    [],
  )

  const phase = derivePhase(state)

  // Stream event handler — feeds the reducer
  const handleStreamEvent = useCallback(
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
        case 'pause': {
          const e = event as { pause_id: string; title: string; payload: Record<string, unknown> }
          dispatch({
            type: 'pause',
            pause: {
              pause_id: e.pause_id as ActivePause['pause_id'],
              title: e.title,
              payload: e.payload,
            },
          })
          break
        }
        case 'resume':
          dispatch({ type: 'resume' })
          break
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

  // startBootstrap
  const startBootstrap = useCallback(
    async (envId: string, passphrase: string) => {
      setResumeError(null)
      dispatch({ type: 'start' })
      try {
        const response = await fetch('/api/bootstrap/start', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'content-type': 'application/json',
            accept: 'application/json',
          },
          body: JSON.stringify({
            env_id: envId,
            passphrase,
            passphrase_confirm: passphrase,
          }),
        })

        if (!response.ok) {
          const text = await response.text()
          throw new Error(text)
        }

        const data = (await response.json()) as { run_id: string }
        // Update runId in state; the stream will handle the rest
        dispatch({
          type: 'run_started',
          runId: data.run_id,
          steps: state.steps, // will be overwritten by run_start event
        })
      } catch (error) {
        dispatch({
          type: 'error',
          error: error instanceof Error ? error.message : String(error),
        })
      }
    },
    [state.steps],
  )

  // resumePause
  const resumePause = useCallback(
    async (runId: string, pauseId: string) => {
      setResumeBusy(true)
      setResumeError(null)
      try {
        await fetch('/api/bootstrap/resume', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ run_id: runId, pause_id: pauseId, payload: null }),
        })
        dispatch({ type: 'resume' })
      } catch (error) {
        setResumeError(error instanceof Error ? error.message : String(error))
      } finally {
        setResumeBusy(false)
      }
    },
    [],
  )

  // verifyPasskey
  const verifyPasskey = useCallback(
    async (runId: string) => {
      setPasskeyChecking(true)
      setResumeError(null)
      try {
        const response = await fetch(`/api/bootstrap/passkey/${runId}`, {
          credentials: 'same-origin',
          headers: { accept: 'application/json' },
        })
        if (!response.ok) throw new Error(await response.text())
        const payload = (await response.json()) as { confirmed: number; required: number }
        const statusText = `${payload.confirmed}/${payload.required}`
        setPasskeyStatus(statusText)
        if (payload.confirmed >= payload.required) {
          await resumePause(runId, 'passkey')
          return
        }
        setResumeError(`Passkey enrollment is not complete yet (${statusText}).`)
      } catch (error) {
        setResumeError(error instanceof Error ? error.message : String(error))
      } finally {
        setPasskeyChecking(false)
      }
    },
    [resumePause],
  )

  // pollPasskey (for Connect station live count)
  const pollPasskey = useCallback(
    async (runId: string): Promise<{ confirmed: number; required: number } | null> => {
      try {
        const response = await fetch(`/api/bootstrap/passkey/${runId}`, {
          credentials: 'same-origin',
          headers: { accept: 'application/json' },
        })
        if (!response.ok) return null
        return (await response.json()) as { confirmed: number; required: number }
      } catch {
        return null
      }
    },
    [],
  )

  return {
    state: { ...state, phase },
    dispatch,
    handleStreamEvent,
    startBootstrap,
    resumePause,
    verifyPasskey,
    pollPasskey,
    passkeyChecking,
    passkeyStatus,
    resumeError,
    resumeBusy,
    setResumeError,
  }
}
