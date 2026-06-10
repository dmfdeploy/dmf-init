import { useEffect, useMemo, useState, type FormEvent } from 'react'
import BootstrapView from './BootstrapView'
import ManageView from './ManageView'
import { readNdjson } from './ndjson'
import { ArtifactDownload, CaInstall, Field, Input, SectionCard, TextArea, type CaCertPayload } from './ui'

type OperatorForm = {
  username: string
  email: string
  display: string
}

type SandboxForm = {
  label: string
  nodeIp: string
  ansibleUser: string
  iface: string
  sshPrivateKey: string
}

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

const defaultSandbox: SandboxForm = {
  label: 'demo',
  nodeIp: '',
  ansibleUser: 'lima',
  iface: 'lima0',
  sshPrivateKey: '',
}

const defaultOperator: OperatorForm = {
  username: 'marty-mcfly',
  email: 'marty@dmf.test',
  display: 'Marty McFly',
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
  const [operator, setOperator] = useState(defaultOperator)
  const [sandbox, setSandbox] = useState(defaultSandbox)
  const [operatorPassphrase, setOperatorPassphrase] = useState('')
  const [operatorPassphraseConfirm, setOperatorPassphraseConfirm] = useState('')
  const [logs, setLogs] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [stage, setStage] = useState<'idle' | 'rendering' | 'backing-up' | 'done'>('idle')
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<CreateNewBackupResponse | null>(null)
  const [renderedEnvId, setRenderedEnvId] = useState<string | null>(null)
  const [renderedDir, setRenderedDir] = useState<string | null>(null)
  const [bootstrapOpen, setBootstrapOpen] = useState(false)
  const [mode, setMode] = useState<'create' | 'manage'>('create')
  const modeDescription =
    mode === 'create'
      ? 'Fetches runtime repos, streams the wizard render, then seals checkpoint #1 as a downloadable backup.'
      : 'Restore an existing env from a backup file and stream mutating actions under the same session.'

  const statusLabel = useMemo(() => {
    switch (stage) {
      case 'rendering':
        return 'Rendering env'
      case 'backing-up':
        return 'Checkpoint #1 backup'
      case 'done':
        return 'Complete'
      default:
        return 'Idle'
    }
  }, [stage])

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setLogs([])
    setResult(null)
    setRenderedEnvId(null)
    setRenderedDir(null)
    setBootstrapOpen(false)

    if (operatorPassphrase !== operatorPassphraseConfirm) {
      setBusy(false)
      setError('Passphrase entries do not match.')
      return
    }

    try {
      setStage('rendering')
      // P1: ensure repos before render (uses server-side fallback if absent)
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
        (line) => setLogs((previous) => [...previous, line]),
      )
      setRenderedEnvId(renderResult.envId)
      setRenderedDir(renderResult.renderDir)

      setStage('backing-up')
      const backupResponse = await fetchJson<CreateNewBackupResponse>('/api/backup', {
        env_id: renderResult.envId,
        passphrase: operatorPassphrase,
        passphrase_confirm: operatorPassphraseConfirm,
      })

      setResult(backupResponse)
      setStage('done')
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : String(submitError))
      setStage('idle')
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="min-h-screen bg-[radial-gradient(circle_at_top_left,rgba(14,165,233,0.18),transparent_24%),radial-gradient(circle_at_top_right,rgba(45,212,191,0.12),transparent_22%),linear-gradient(180deg,#050816_0%,#081122_45%,#050816_100%)] px-4 py-6 text-text sm:px-6 lg:px-8">
      <div className="mx-auto flex min-h-[calc(100vh-3rem)] w-full max-w-7xl flex-col overflow-hidden rounded-[2rem] border border-border/60 bg-bg/70 shadow-glow backdrop-blur-xl">
        <header className="border-b border-border/60 px-6 py-5 sm:px-8">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
            <div>
              <p className="text-xs uppercase tracking-[0.35em] text-accentSoft">DMF Init</p>
              <h1 className="mt-2 text-3xl font-semibold tracking-tight sm:text-4xl">
                Create new or manage an existing env.
              </h1>
            </div>
            <div className="flex flex-col items-stretch gap-3 sm:items-end">
              <div className="inline-flex rounded-full border border-border/70 bg-white/5 p-1 text-sm text-muted">
                <button
                  type="button"
                  onClick={() => setMode('create')}
                  className={[
                    'rounded-full px-4 py-2 transition',
                    mode === 'create' ? 'bg-accent text-bg shadow-glow' : 'text-muted hover:text-text',
                  ].join(' ')}
                >
                  Create new
                </button>
                <button
                  type="button"
                  onClick={() => setMode('manage')}
                  className={[
                    'rounded-full px-4 py-2 transition',
                    mode === 'manage' ? 'bg-accent text-bg shadow-glow' : 'text-muted hover:text-text',
                  ].join(' ')}
                >
                  Manage
                </button>
              </div>
              <div className="rounded-full border border-border/70 bg-white/5 px-4 py-2 text-sm text-muted">
                <span className="text-text">{statusLabel}</span>
                {renderedEnvId ? <span className="ml-2">· {renderedEnvId}</span> : null}
              </div>
            </div>
          </div>
          <p className="mt-4 max-w-3xl text-sm leading-6 text-muted">{modeDescription}</p>
        </header>
        {mode === 'create' ? (
          <>
            <form className="grid gap-6 px-6 py-6 lg:grid-cols-[1.1fr_0.9fr] lg:px-8" onSubmit={handleSubmit}>
              <div className="grid gap-6">
                <SectionCard
                  title="Operator identity"
                  description="This identity is written into the sandbox wizard answers file."
                >
                  <div className="grid gap-4 md:grid-cols-3">
                    <Field label="Username">
                      <Input
                        value={operator.username}
                        onChange={(event) => setOperator((previous) => ({ ...previous, username: event.target.value }))}
                      />
                    </Field>
                    <Field label="Email">
                      <Input
                        type="email"
                        value={operator.email}
                        onChange={(event) => setOperator((previous) => ({ ...previous, email: event.target.value }))}
                      />
                    </Field>
                    <Field label="Display name">
                      <Input
                        value={operator.display}
                        onChange={(event) => setOperator((previous) => ({ ...previous, display: event.target.value }))}
                      />
                    </Field>
                  </div>
                </SectionCard>

                <SectionCard
                  title="Sandbox inputs"
                  description="These mirror the wizard's sandbox answers-file fields."
                >
                  <div className="grid gap-4 md:grid-cols-2">
                    <Field label="Sandbox label" hint="The DNS-safe subdomain label">
                      <Input
                        value={sandbox.label}
                        onChange={(event) => setSandbox((previous) => ({ ...previous, label: event.target.value }))}
                      />
                    </Field>
                    <Field label="Node IP">
                      <Input
                        value={sandbox.nodeIp}
                        onChange={(event) => setSandbox((previous) => ({ ...previous, nodeIp: event.target.value }))}
                      />
                    </Field>
                    <Field label="Ansible user">
                      <Input
                        value={sandbox.ansibleUser}
                        onChange={(event) => setSandbox((previous) => ({ ...previous, ansibleUser: event.target.value }))}
                      />
                    </Field>
                    <Field label="Interface">
                      <Input
                        value={sandbox.iface}
                        onChange={(event) => setSandbox((previous) => ({ ...previous, iface: event.target.value }))}
                      />
                    </Field>
                  </div>
                  <Field label="SSH private key" hint="Paste it or load a local file; the backend writes it into tmpfs.">
                    <TextArea
                      value={sandbox.sshPrivateKey}
                      onChange={(event) => setSandbox((previous) => ({ ...previous, sshPrivateKey: event.target.value }))}
                      placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                    />
                  </Field>
                  <div className="flex flex-wrap items-center gap-3">
                    <label className="inline-flex cursor-pointer items-center gap-2 rounded-full border border-border/70 bg-white/5 px-4 py-2 text-sm text-text transition hover:bg-white/8">
                      <span>Load key file</span>
                      <input
                        className="hidden"
                        type="file"
                        accept=".pem,.key,.txt"
                        onChange={async (event) => {
                          const file = event.target.files?.[0]
                          if (!file) return
                          const contents = await file.text()
                          setSandbox((previous) => ({ ...previous, sshPrivateKey: contents }))
                        }}
                      />
                    </label>
                    <span className="text-sm text-muted">The wizard generates everything else.</span>
                  </div>
                </SectionCard>

                <SectionCard
                  title="Passphrase"
                  description="The same operator passphrase wraps the checkpoint #1 backup."
                >
                  <div className="grid gap-4 md:grid-cols-2">
                    <Field label="Passphrase">
                      <Input
                        type="password"
                        value={operatorPassphrase}
                        onChange={(event) => setOperatorPassphrase(event.target.value)}
                      />
                    </Field>
                    <Field label="Repeat passphrase">
                      <Input
                        type="password"
                        value={operatorPassphraseConfirm}
                        onChange={(event) => setOperatorPassphraseConfirm(event.target.value)}
                      />
                    </Field>
                  </div>
                </SectionCard>

                <div className="flex flex-wrap items-center gap-3">
                  <button
                    type="submit"
                    disabled={busy}
                    className="rounded-2xl border border-accent/30 bg-accent px-6 py-3 text-sm font-semibold text-bg transition-transform hover:-translate-y-0.5 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {busy ? 'Working…' : 'Create new'}
                  </button>
                  <p className="text-sm text-muted">
                    Session protected. The browser should already have the launch token exchanged.
                  </p>
                </div>

                {error ? (
                  <div className="rounded-3xl border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-100">
                    {error}
                  </div>
                ) : null}
              </div>

              <div className="grid gap-6">
                <section className="rounded-[1.75rem] border border-border/70 bg-[linear-gradient(180deg,rgba(15,23,42,0.92),rgba(2,6,23,0.8))] p-5 shadow-glow">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Render output</p>
                      <h2 className="mt-2 text-xl font-semibold text-text">Streamed wizard logs</h2>
                    </div>
                    {renderedEnvId ? (
                      <div className="rounded-full border border-border/70 bg-white/5 px-3 py-1 text-xs text-muted">
                        {renderedEnvId}
                      </div>
                    ) : null}
                  </div>
                  <p className="mt-3 text-sm leading-6 text-muted">
                    The backend redacts request secrets and streams the wizard output line by line.
                  </p>
                  <pre className="mt-4 max-h-[28rem] overflow-auto whitespace-pre-wrap rounded-3xl border border-border/70 bg-black/30 p-4 text-xs leading-6 text-text">
                    {logs.length ? logs.join('\n') : 'Waiting for a render run...'}
                  </pre>
                </section>

                {stage === 'done' && result ? (
                  <CompletionCard
                    artifactName={result.backup.artifact_name}
                    envId={renderedEnvId}
                    renderedDir={renderedDir}
                    onBootstrapOpen={() => setBootstrapOpen(true)}
                    bootstrapOpen={bootstrapOpen}
                  />
                ) : (
                  <section className="rounded-[1.75rem] border border-border/70 bg-white/5 p-5 text-sm leading-6 text-muted">
                    <p className="text-text">Flow</p>
                    <ol className="mt-3 grid gap-2">
                      <li>1. Render the sandbox env with a tmpfs age key and answers file.</li>
                      <li>2. Seal checkpoint #1 as a downloadable backup.</li>
                    </ol>
                  </section>
                )}
              </div>
            </form>

            {bootstrapOpen && renderedEnvId ? (
              <div className="px-6 pb-6 lg:px-8">
                <BootstrapView
                  envId={renderedEnvId}
                  passphrase={operatorPassphrase}
                  operatorUsername={operator.username}
                />
              </div>
            ) : null}
          </>
        ) : (
          <div className="px-6 py-6 lg:px-8">
            <ManageView />
          </div>
        )}
      </div>
    </main>
  )

function CompletionCard(props: {
  artifactName: string
  envId: string | null
  renderedDir: string | null
  bootstrapOpen: boolean
  onBootstrapOpen: () => void
}) {
  const [caPayload, setCaPayload] = useState<CaCertPayload | null>(null)
  const [caError, setCaError] = useState<string | null>(null)
  const [caRefreshing, setCaRefreshing] = useState(false)

  const fetchCa = () => {
    if (!props.envId) return
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

  // Fetch on mount
  useEffect(() => { fetchCa() }, [props.envId])

  const whyText =
    'The cluster serves all its HTTPS services via its own local CA; until your OS/browser trusts it you get warnings AND passkey/WebAuthn enrollment fails SILENTLY (non-secure-context, not a real cancel). Trusting it makes the sites work.'

  return (
    <section className="rounded-[1.75rem] border border-accent/30 bg-accent/10 p-5">
      <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">
        Safe to delete this container
      </p>
      <h2 className="mt-2 text-2xl font-semibold text-text">Checkpoint #1 is sealed.</h2>
      <p className="mt-3 text-sm leading-6 text-muted">
        Download the backup and keep the passphrase safe. The container itself can go away once
        you are satisfied with the backup.
      </p>
      <div className="mt-4 grid gap-3 text-sm">
        <ArtifactDownload artifactName={props.artifactName} />
      </div>

      {/* CA Certificate Card */}
      <div className="mt-6 rounded-2xl border border-border/70 bg-white/5 p-4">
        <p className="text-xs uppercase tracking-[0.28em] text-accentSoft">CA Certificate</p>
        <h3 className="mt-2 text-lg font-semibold text-text">Install the DMF Local CA</h3>
        <p className="mt-2 text-sm leading-6 text-muted">{whyText}</p>

        {caPayload?.present ? (
          <>
            <div className="mt-4 flex flex-wrap items-center gap-3">
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
            {caPayload.requirement_note && (
              <p className="mt-3 text-xs leading-5 text-muted">{caPayload.requirement_note}</p>
            )}
            <div className="mt-4">
              <CaInstall payload={caPayload} onCopyError={(msg) => setCaError(msg)} />
            </div>
          </>
        ) : (
          <div className="mt-3">
            <p className="text-sm text-muted">
              {caError ? caError : 'CA not available until the cluster is up.'}
            </p>
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
      </div>

      {props.renderedDir ? (
        <p className="mt-4 text-sm text-muted">Rendered env dir: {props.renderedDir}</p>
      ) : null}
      {!props.bootstrapOpen ? (
        <button
          type="button"
          onClick={props.onBootstrapOpen}
          className="mt-5 rounded-2xl border border-accent/30 bg-accent px-6 py-3 text-sm font-semibold text-bg transition-transform hover:-translate-y-0.5"
        >
          Continue -&gt; Run bootstrap
        </button>
      ) : null}
    </section>
  )
}

}
