import { useCallback, useEffect, useState } from 'react'
import { Shell } from './app/Shell'
import { ConfigureStep, type OperatorForm, type SandboxForm } from './create/ConfigureStep'
import { InstallProgress } from './create/InstallProgress'
import { ConnectStep } from './create/ConnectStep'
import { FinishStep } from './create/FinishStep'
import { ValidateStep } from './create/ValidateStep'
import { useCreateFlow, type ActivePause } from './hooks/useCreateFlow'
import { useEventStream } from './hooks/useEventStream'
import ManageView from './ManageView'
import { readNdjson } from './ndjson'

type CreateNewBackupResponse = {
  env_id: string
  backup: {
    artifact_name: string
  }
}

type StreamEvent =
  | { event: 'log'; line: string }
  | { event: 'complete'; env_id: string; render_dir: string }
  | { event: 'error'; error: string }

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

async function streamRender(
  payload: unknown,
  onLine: (line: string) => void,
): Promise<{ envId: string; renderDir: string }> {
  const response = await fetch('/api/render', {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'content-type': 'application/json',
      accept: 'application/x-ndjson',
    },
    body: JSON.stringify(payload),
  })

  if (!response.ok) {
    throw new Error(await readError(response))
  }

  let envId = ''
  let renderDir = ''

  await readNdjson(response, (event) => {
    const streamEvent = event as Partial<StreamEvent> & { event?: string }
    if (streamEvent.event === 'log') {
      onLine((streamEvent as { line: string }).line)
    } else if (streamEvent.event === 'error') {
      throw new Error((streamEvent as { error: string }).error)
    } else if (streamEvent.event === 'complete') {
      envId = (streamEvent as { env_id: string }).env_id
      renderDir = (streamEvent as { render_dir: string }).render_dir
    }
  })

  if (!envId) {
    throw new Error('render completed without an env_id')
  }

  return { envId, renderDir }
}

export default function App() {
  const [mode, setMode] = useState<'create' | 'manage'>('create')
  const [renderLogs, setRenderLogs] = useState<string[]>([])
  const [renderBusy, setRenderBusy] = useState(false)
  const [renderStage, setRenderStage] = useState<'idle' | 'rendering' | 'backing-up' | 'done'>('idle')
  const [renderError, setRenderError] = useState<string | null>(null)
  const [result, setResult] = useState<CreateNewBackupResponse | null>(null)
  const [renderedEnvId, setRenderedEnvId] = useState<string | null>(null)
  const [renderedDir, setRenderedDir] = useState<string | null>(null)
  const [renderPassphrase, setRenderPassphrase] = useState<string | null>(null)

  // Create flow hook
  const createFlow = useCreateFlow()
  const { state: createState, handleStreamEvent, startBootstrap, resumePause, verifyPasskey, pollPasskey } = createFlow

  // Stream hook for bootstrap
  const { logs: bootstrapLogs, cursor, reconnectNote, streamError } = useEventStream({
    runId: createState.runId,
    onEvent: handleStreamEvent,
  })

  // Map createFlow's activePause to the typed ActivePause for ConnectStep
  const activePause = createState.activePause as ActivePause | null

  async function handleSubmit(operator: OperatorForm, sandbox: SandboxForm, passphrase: string) {
    setRenderBusy(true)
    setRenderError(null)
    setRenderLogs([])
    setResult(null)
    setRenderedEnvId(null)
    setRenderedDir(null)

    try {
      setRenderStage('rendering')
      await fetchJson('/api/repos/fetch', {})
      const renderResult = await streamRender(
        {
          operator: {
            username: operator.username,
            email: operator.email,
            display: operator.display,
          },
          sandbox: {
            label: sandbox.label,
            node_ip: sandbox.nodeIp,
            ansible_user: sandbox.ansibleUser,
            iface: sandbox.iface,
            ssh_private_key: sandbox.sshPrivateKey,
          },
        },
        (line) => setRenderLogs((prev) => [...prev, line]),
      )
      setRenderedEnvId(renderResult.envId)
      setRenderedDir(renderResult.renderDir)

      setRenderStage('backing-up')
      const backupResponse = await fetchJson<CreateNewBackupResponse>('/api/backup', {
        env_id: renderResult.envId,
        passphrase,
        passphrase_confirm: passphrase,
      })

      setResult(backupResponse)
      setRenderStage('done')
      setRenderPassphrase(passphrase)
    } catch (submitError) {
      setRenderError(submitError instanceof Error ? submitError.message : String(submitError))
      setRenderStage('idle')
    } finally {
      setRenderBusy(false)
    }
  }

  const handleStartBootstrap = useCallback(async () => {
    if (!renderedEnvId || !renderPassphrase) return
    await startBootstrap(renderedEnvId, renderPassphrase)
  }, [renderedEnvId, renderPassphrase, startBootstrap])

  const handleResumePause = useCallback(async () => {
    if (!activePause || !createState.runId) return
    await resumePause(createState.runId, activePause.pause_id)
  }, [activePause, createState.runId, resumePause])

  const handleVerifyPasskey = useCallback(async () => {
    if (!createState.runId) return
    await verifyPasskey(createState.runId)
  }, [createState.runId, verifyPasskey])

  // Auto-start bootstrap when render is done (stage = 'done')
  useEffect(() => {
    if (renderStage === 'done' && renderedEnvId && renderPassphrase && !createState.runId) {
      void handleStartBootstrap()
    }
  }, [renderStage, renderedEnvId, renderPassphrase, createState.runId, handleStartBootstrap])

  // Determine if we should show validate phase
  const [showValidate, setShowValidate] = useState(false)

  // Render the appropriate create phase
  function renderCreatePhase() {
    // If still in render phase (not yet bootstrapping)
    if (renderStage === 'idle' || renderStage === 'rendering' || renderStage === 'backing-up') {
      return (
        <ConfigureStep
          onSubmit={handleSubmit}
          busy={renderBusy}
          error={renderError}
        />
      )
    }

    // After render is done, show the bootstrap flow phases
    const phase = createState.phase

    if (showValidate && phase === 'finish') {
      return (
        <ValidateStep
          envId={renderedEnvId ?? ''}
          onBack={() => setShowValidate(false)}
        />
      )
    }

    switch (phase) {
      case 'configure':
        return (
          <div className="grid gap-6">
            {result && (
              <section className="rounded-[1.75rem] border border-accent/30 bg-accent/10 p-5">
                <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">
                  Checkpoint #1 sealed
                </p>
                <h2 className="mt-2 text-2xl font-semibold text-text">Ready to bootstrap.</h2>
                <p className="mt-3 text-sm leading-6 text-muted">
                  Download the checkpoint #1 backup and keep the passphrase safe.
                </p>
                {renderedDir && (
                  <p className="mt-3 text-sm text-muted">Rendered env dir: {renderedDir}</p>
                )}
                <div className="mt-4">
                  <button
                    type="button"
                    onClick={handleStartBootstrap}
                    className="rounded-2xl border border-accent/30 bg-accent px-6 py-3 text-sm font-semibold text-bg transition-transform hover:-translate-y-0.5"
                  >
                    Run bootstrap
                  </button>
                </div>
              </section>
            )}
            <details className="rounded-[1.75rem] border border-border/70 bg-panel/80 p-5 shadow-glow backdrop-blur">
              <summary className="cursor-pointer text-sm font-medium text-muted transition hover:text-text">
                Show render logs
              </summary>
              <pre className="mt-3 max-h-[28rem] overflow-auto whitespace-pre-wrap rounded-3xl border border-border/70 bg-black/30 p-4 text-xs leading-6 text-text">
                {renderLogs.length ? renderLogs.join('\n') : 'Waiting for render logs...'}
              </pre>
            </details>
          </div>
        )

      case 'installing':
      case 'verifying':
        return (
          <InstallProgress
            steps={createState.steps}
            stepStatuses={createState.stepStatuses}
            currentStep={createState.currentStep}
            checkpoints={createState.checkpoints}
            logs={bootstrapLogs}
            cursor={cursor}
            reconnectNote={reconnectNote}
            streamError={streamError}
            terminal={
              createState.terminal
                ? createState.terminal.kind === 'complete'
                  ? { kind: 'complete', runId: createState.terminal.runId, checkpoints: createState.terminal.checkpoints }
                  : { kind: 'error', step: createState.terminal.step, error: createState.terminal.error }
                : null
            }
          />
        )

      case 'connect':
        return (
          <ConnectStep
            activePause={activePause}
            resumeBusy={createFlow.resumeBusy}
            resumeError={createFlow.resumeError}
            passkeyChecking={createFlow.passkeyChecking}
            passkeyStatus={createFlow.passkeyStatus}
            onResume={handleResumePause}
            onVerifyPasskey={handleVerifyPasskey}
            envId={renderedEnvId ?? ''}
            runId={createState.runId ?? ''}
            pollPasskey={pollPasskey}
          />
        )

      case 'finish':
        return (
          <FinishStep
            checkpoints={createState.checkpoints}
            terminal={
              createState.terminal
                ? createState.terminal.kind === 'complete'
                  ? { kind: 'complete', runId: createState.terminal.runId, checkpoints: createState.terminal.checkpoints }
                  : { kind: 'error', error: createState.terminal.error }
                : null
            }
            envId={renderedEnvId ?? ''}
            onRevalidate={() => setShowValidate(true)}
          />
        )

      default:
        return null
    }
  }

  return (
    <Shell
      mode={mode}
      onModeChange={setMode}
      createPhase={mode === 'create' ? createState.phase : undefined}
      envId={renderedEnvId}
    >
      {mode === 'create' ? renderCreatePhase() : <ManageView />}
    </Shell>
  )
}
