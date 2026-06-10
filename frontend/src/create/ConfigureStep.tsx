import { useState, type FormEvent } from 'react'
import { Field, Input, SectionCard, TextArea } from '../ui'

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

type ConfigureStepProps = {
  onSubmit: (operator: OperatorForm, sandbox: SandboxForm, passphrase: string) => void
  busy: boolean
  error: string | null
}

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

export function ConfigureStep({ onSubmit, busy, error }: ConfigureStepProps) {
  const [operator, setOperator] = useState(defaultOperator)
  const [sandbox, setSandbox] = useState(defaultSandbox)
  const [passphrase, setPassphrase] = useState('')
  const [passphraseConfirm, setPassphraseConfirm] = useState('')
  const [showReview, setShowReview] = useState(false)
  const [localError, setLocalError] = useState<string | null>(null)

  function validate(): string | null {
    if (!operator.username.trim()) return 'Username is required.'
    if (!operator.email.trim() || !operator.email.includes('@')) return 'A valid email is required.'
    if (!operator.display.trim()) return 'Display name is required.'
    if (!sandbox.label.trim()) return 'Sandbox label is required.'
    if (!passphrase) return 'Passphrase is required.'
    if (passphrase !== passphraseConfirm) return 'Passphrase entries do not match.'
    return null
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setLocalError(null)
    const validationError = validate()
    if (validationError) {
      setLocalError(validationError)
      return
    }
    setShowReview(true)
  }

  function handleConfirmStart(e: FormEvent) {
    e.preventDefault()
    setLocalError(null)
    const validationError = validate()
    if (validationError) {
      setLocalError(validationError)
      return
    }
    onSubmit(operator, sandbox, passphrase)
  }

  if (showReview) {
    return (
      <form onSubmit={handleConfirmStart} className="grid gap-6">
        <SectionCard
          title="Review your configuration"
          description="Confirm these values before starting the install."
          eyebrow="Configure"
        >
          <div className="grid gap-3 text-sm">
            <div className="rounded-2xl border border-border/70 bg-white/5 px-4 py-3">
              <div className="text-xs uppercase tracking-[0.28em] text-muted">Operator</div>
              <div className="mt-1 text-text">
                {operator.display} ({operator.username}) · {operator.email}
              </div>
            </div>
            <div className="rounded-2xl border border-border/70 bg-white/5 px-4 py-3">
              <div className="text-xs uppercase tracking-[0.28em] text-muted">Sandbox</div>
              <div className="mt-1 text-text">
                Label: {sandbox.label}
                {sandbox.nodeIp ? ` · Node IP: ${sandbox.nodeIp}` : ''}
                <br />
                Ansible user: {sandbox.ansibleUser} · Interface: {sandbox.iface}
              </div>
            </div>
            <div className="rounded-2xl border border-border/70 bg-white/5 px-4 py-3">
              <div className="text-xs uppercase tracking-[0.28em] text-muted">SSH key</div>
              <div className="mt-1 text-text">
                {sandbox.sshPrivateKey
                  ? `${sandbox.sshPrivateKey.slice(0, 40)}…`
                  : '(not provided)'}
              </div>
            </div>
          </div>
        </SectionCard>

        {(localError || error) ? (
          <div className="rounded-3xl border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-100">
            {localError || error}
          </div>
        ) : null}

        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={() => setShowReview(false)}
            className="rounded-2xl border border-border/70 bg-white/5 px-6 py-3 text-sm font-semibold text-text transition hover:bg-white/8"
          >
            ← Back to edit
          </button>
          <button
            type="submit"
            disabled={busy}
            className="rounded-2xl border border-accent/30 bg-accent px-6 py-3 text-sm font-semibold text-bg transition-transform hover:-translate-y-0.5 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {busy ? 'Starting…' : 'Start install'}
          </button>
        </div>
      </form>
    )
  }

  return (
    <form className="grid gap-6" onSubmit={handleSubmit}>
      <div className="grid gap-6">
        <SectionCard
          title="Operator identity"
          description="This identity is written into the sandbox wizard answers file."
        >
          <div className="grid gap-4 md:grid-cols-3">
            <Field label="Username">
              <Input
                value={operator.username}
                onChange={(e) => setOperator((p) => ({ ...p, username: e.target.value }))}
              />
            </Field>
            <Field label="Email">
              <Input
                type="email"
                value={operator.email}
                onChange={(e) => setOperator((p) => ({ ...p, email: e.target.value }))}
              />
            </Field>
            <Field label="Display name">
              <Input
                value={operator.display}
                onChange={(e) => setOperator((p) => ({ ...p, display: e.target.value }))}
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
                onChange={(e) => setSandbox((p) => ({ ...p, label: e.target.value }))}
              />
            </Field>
            <Field label="Node IP">
              <Input
                value={sandbox.nodeIp}
                onChange={(e) => setSandbox((p) => ({ ...p, nodeIp: e.target.value }))}
              />
            </Field>
            <Field label="Ansible user">
              <Input
                value={sandbox.ansibleUser}
                onChange={(e) => setSandbox((p) => ({ ...p, ansibleUser: e.target.value }))}
              />
            </Field>
            <Field label="Interface">
              <Input
                value={sandbox.iface}
                onChange={(e) => setSandbox((p) => ({ ...p, iface: e.target.value }))}
              />
            </Field>
          </div>
          <Field label="SSH private key" hint="Paste it or load a local file; the backend writes it into tmpfs.">
            <TextArea
              value={sandbox.sshPrivateKey}
              onChange={(e) => setSandbox((p) => ({ ...p, sshPrivateKey: e.target.value }))}
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
                onChange={async (e) => {
                  const file = e.target.files?.[0]
                  if (!file) return
                  const contents = await file.text()
                  setSandbox((p) => ({ ...p, sshPrivateKey: contents }))
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
                value={passphrase}
                onChange={(e) => setPassphrase(e.target.value)}
              />
            </Field>
            <Field label="Repeat passphrase">
              <Input
                type="password"
                value={passphraseConfirm}
                onChange={(e) => setPassphraseConfirm(e.target.value)}
              />
            </Field>
          </div>
        </SectionCard>
      </div>

      {(localError || error) ? (
        <div className="rounded-3xl border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-100">
          {localError || error}
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="submit"
          disabled={busy}
          className="rounded-2xl border border-accent/30 bg-accent px-6 py-3 text-sm font-semibold text-bg transition-transform hover:-translate-y-0.5 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {busy ? 'Working…' : 'Review & start install'}
        </button>
        <p className="text-sm text-muted">
          Session protected. The browser should already have the launch token exchanged.
        </p>
      </div>
    </form>
  )
}

export type { OperatorForm, SandboxForm }
