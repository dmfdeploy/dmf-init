import { useCallback, useEffect, useRef, useState } from 'react'
import { Shell, type RailSubItem } from './app/Shell'
import { SessionExpiredOverlay } from './shared/SessionExpiredOverlay'
import { flagIfUnauthorized } from './shared/sessionExpiry'
import { ConfigureStep, type OperatorForm, type SandboxForm } from './create/ConfigureStep'
import { InstallProgress, stepDisplayName } from './create/InstallProgress'
import { ConnectStep } from './create/ConnectStep'
import { FinishStep } from './create/FinishStep'
import { ValidateStep } from './create/ValidateStep'
import { LandingResume, type EnvSummary } from './create/LandingResume'
import { usePackageBundle } from './shared/usePackageBundle'
import { useCreateFlow, type ActivePause } from './hooks/useCreateFlow'
import { useEventStream } from './hooks/useEventStream'
import ManageView from './ManageView'
import { readNdjson } from './ndjson'
import { describeFetchError } from './shared/errors'

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
  flagIfUnauthorized(response)

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
  flagIfUnauthorized(response)

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
  const [renderedEnvId, setRenderedEnvId] = useState<string | null>(null)
  const [renderPassphrase, setRenderPassphrase] = useState<string | null>(null)

  // Create flow hook
  const createFlow = useCreateFlow()
  const { state: createState, handleStreamEvent, startBootstrap, resumePause, verifyPasskey, pollPasskey, retryRun, retryBusy, resumeEnv, resumeEnvBusy } = createFlow

  // Single owner of recovery-bundle state, shared by the rail/inline download
  // affordances and the finish screen (#140).
  const bundle = usePackageBundle(renderedEnvId)

  // Landing affordance (#143): rendered envs with a failed bootstrap on disk,
  // offered for resume so a GC'd / reloaded / restarted session re-enters
  // instead of dead-ending. Fetched once on mount.
  const [resumableEnvs, setResumableEnvs] = useState<EnvSummary[]>([])
  const [landingDismissed, setLandingDismissed] = useState(false)
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const response = await fetch('/api/envs', {
          credentials: 'same-origin',
          headers: { accept: 'application/json' },
        })
        if (flagIfUnauthorized(response)) return
        if (!response.ok) return
        const data = (await response.json()) as { envs: EnvSummary[] }
        if (!cancelled) {
          setResumableEnvs(data.envs.filter((e) => e.resumable))
        }
      } catch {
        // Best-effort: a failed probe simply hides the affordance.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

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
    setRenderedEnvId(null)
    bootstrapKickoff.current = false

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

      setRenderStage('backing-up')
      await fetchJson<CreateNewBackupResponse>('/api/backup', {
        env_id: renderResult.envId,
        passphrase,
        passphrase_confirm: passphrase,
      })

      setRenderStage('done')
      setRenderPassphrase(passphrase)
    } catch (submitError) {
      setRenderError(describeFetchError(submitError))
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

  // Auto-start bootstrap exactly ONCE per deploy. The ref latch is essential:
  // guarding on !runId alone re-fired the effect after every reducer reset and
  // produced an unbounded stream of /api/bootstrap/start 409s (field-found).
  const bootstrapKickoff = useRef(false)
  useEffect(() => {
    if (
      renderStage === 'done' &&
      renderedEnvId &&
      renderPassphrase &&
      !bootstrapKickoff.current
    ) {
      bootstrapKickoff.current = true
      void handleStartBootstrap()
    }
  }, [renderStage, renderedEnvId, renderPassphrase, handleStartBootstrap])

  // Resume an unfinished env from disk (#143). On success the stream takes over
  // and drives the install view; we record env+passphrase so downstream
  // affordances (bundle download, retry) keep working, and latch the auto-start
  // ref so the deploy effect never double-fires on top of the resumed run.
  const handleResumeEnv = useCallback(
    async (envId: string, passphrase: string) => {
      const ok = await resumeEnv(envId, passphrase)
      if (ok) {
        setRenderedEnvId(envId)
        setRenderPassphrase(passphrase)
        bootstrapKickoff.current = true
      }
    },
    [resumeEnv],
  )

  // Determine if we should show validate phase
  const [showValidate, setShowValidate] = useState(false)

  // The operator-facing phase: the moment Deploy is pressed (renderStage
  // leaves 'idle') we are INSTALLING — render+backup+bootstrap-start are all
  // one continuous install from the operator's point of view. No interstitial
  // screens, no fallback buttons between Deploy and the splash.
  const effectivePhase =
    renderStage !== 'idle' && createState.phase === 'configure'
      ? 'installing'
      : createState.phase

  // Until the bootstrap stream takes over, the splash ticker/log shows the
  // wizard render output.
  const mergedLogs = bootstrapLogs.length
    ? bootstrapLogs
    : renderLogs.map((line) => ({ step: 'render', line }))

  // Render the appropriate create phase
  function renderCreatePhase() {
    if (showValidate && effectivePhase === 'finish') {
      return (
        <ValidateStep
          envId={renderedEnvId ?? ''}
          onBack={() => setShowValidate(false)}
          bundle={bundle}
        />
      )
    }

    switch (effectivePhase) {
      case 'installing':
      case 'verifying':
        return (
          <InstallProgress
            steps={createState.steps}
            stepStatuses={createState.stepStatuses}
            currentStep={createState.currentStep}
            checkpoints={createState.checkpoints}
            logs={mergedLogs}
            cursor={cursor}
            reconnectNote={reconnectNote}
            streamError={streamError}
            terminal={
              createState.terminal
                ? createState.terminal.kind === 'complete'
                  ? { kind: 'complete', runId: createState.terminal.runId, checkpoints: createState.terminal.checkpoints }
                  : { kind: 'error', step: createState.terminal.step, error: createState.terminal.error, hint: createState.terminal.hint }
                : null
            }
            onRetry={() => {
              if (renderPassphrase) retryRun(renderPassphrase, renderedEnvId)
            }}
            retryBusy={retryBusy}
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
            bundleSafe={Boolean(bundle.downloaded && bundle.current)}
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
            onRevalidate={() => setShowValidate(true)}
            bundle={bundle}
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
      createPhase={mode === 'create' ? effectivePhase : undefined}
      subItems={mode === 'create' ? subItems : undefined}
      envId={renderedEnvId}
      bundle={mode === 'create' ? bundle : undefined}
    >
      <SessionExpiredOverlay />
      {mode === 'create' ? (
        <>
          {/* Kept mounted (hidden) so form state survives a failed deploy. */}
          <div className={effectivePhase === 'configure' ? 'h-full' : 'hidden'}>
            {!landingDismissed && renderStage === 'idle' ? (
              <LandingResume
                envs={resumableEnvs}
                busy={resumeEnvBusy}
                error={createFlow.resumeError}
                onResume={handleResumeEnv}
                onDismiss={() => setLandingDismissed(true)}
              />
            ) : null}
            <ConfigureStep
              onSubmit={handleSubmit}
              onPageChange={setConfigPage}
              busy={renderBusy}
              error={renderError}
            />
          </div>
          {effectivePhase !== 'configure' && renderCreatePhase()}
        </>
      ) : (
        <ManageView />
      )}
    </Shell>
  )
}
