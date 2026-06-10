import { useCallback, useEffect, useState } from 'react'
import { Shell, type RailSubItem } from './app/Shell'
import { ConfigureStep, type OperatorForm, type SandboxForm } from './create/ConfigureStep'
import { InstallProgress, stepDisplayName } from './create/InstallProgress'
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
  const [configPage, setConfigPage] = useState(0)
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
    // During render/backup (before bootstrap starts)
    if (renderStage === 'idle' || renderStage === 'rendering' || renderStage === 'backing-up') {
      return (
        <ConfigureStep
          onSubmit={handleSubmit}
          onPageChange={setConfigPage}
          busy={renderBusy}
          error={renderError}
        />
      )
    }

    // Render/backup done, bootstrap about to start (or starting)
    if (renderStage === 'done' && !createState.runId && !createState.terminal) {
      return (
        <div className="flex flex-col items-center justify-center gap-3 py-16">
          <p className="text-sm text-muted">Starting bootstrap…</p>
          {renderedDir && <details className="w-full max-w-lg"><summary className="cursor-pointer text-xs text-muted">Show render logs</summary><pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-bg/80 p-2 text-[11px] leading-5 text-text">{renderLogs.join('\n')}</pre></details>}
        </div>
      )
    }

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
        // Should not normally reach here after render done (auto-start kicks in)
        // But if render produced a result without auto-starting, show it
        return (
          <div className="grid gap-4">
            {result && (
              <div className="rounded-lg border border-accent/30 bg-accent/10 p-4">
                <p className="text-xs uppercase tracking-[0.2em] text-accentSoft">Checkpoint #1 sealed</p>
                <h2 className="mt-1 text-lg font-semibold text-text">Ready to bootstrap.</h2>
                <p className="mt-1 text-sm text-muted">Download the checkpoint #1 backup and keep the passphrase safe.</p>
                <div className="mt-3">
                  <button type="button" onClick={handleStartBootstrap} className="rounded-lg border border-accent/30 bg-accent px-4 py-2 text-sm font-semibold text-bg transition hover:bg-accent/90">
                    Run bootstrap
                  </button>
                </div>
              </div>
            )}
            <details className="rounded-lg border border-border bg-panel p-3">
              <summary className="cursor-pointer text-sm font-medium text-muted transition hover:text-text">Show render logs</summary>
              <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-bg/80 p-2 text-[11px] leading-5 text-text">{renderLogs.length ? renderLogs.join('\n') : 'Waiting for render logs...'}</pre>
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

  // Sidebar sub-items for the active rail phase.
  const PAUSE_IDS = ['workstation', 'passkey']
  const wizardPages = ['Identity', 'Target node', 'Security', 'Review & Deploy']
  const subItems: Record<string, RailSubItem[]> = {
    configure: wizardPages.map((label, i) => ({
      key: `cfg-${i}`,
      label,
      status: i < configPage ? 'ok' : i === configPage ? 'running' : 'pending',
    })),
    installing: createState.steps
      .filter((s) => !PAUSE_IDS.includes(s))
      .map((s) => ({
        key: s,
        label: stepDisplayName(s),
        status: createState.stepStatuses[s] ?? 'pending',
      })),
    connect: createState.steps
      .filter((s) => PAUSE_IDS.includes(s))
      .map((s) => ({
        key: s,
        label: stepDisplayName(s),
        status: createState.stepStatuses[s] ?? 'pending',
      })),
  }

  return (
    <Shell
      mode={mode}
      onModeChange={setMode}
      createPhase={mode === 'create' ? createState.phase : undefined}
      subItems={mode === 'create' ? subItems : undefined}
      envId={renderedEnvId}
    >
      {mode === 'create' ? renderCreatePhase() : <ManageView />}
    </Shell>
  )
}
