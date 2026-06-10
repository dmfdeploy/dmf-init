import { useState, type FormEvent } from 'react'
import { Field, Input, TextArea } from '../ui'

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
  onPageChange?: (page: number) => void
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

type Page = 0 | 1 | 2 | 3

export function ConfigureStep({ onSubmit, onPageChange, busy, error }: ConfigureStepProps) {
  const [operator, setOperator] = useState(defaultOperator)
  const [sandbox, setSandbox] = useState(defaultSandbox)
  const [passphrase, setPassphrase] = useState('')
  const [passphraseConfirm, setPassphraseConfirm] = useState('')
  const [page, setPage] = useState<Page>(0)
  const [localError, setLocalError] = useState<string | null>(null)

  // Validation per page
  function validatePage(p: Page): string | null {
    switch (p) {
      case 0: // Identity
        if (!operator.username.trim()) return 'Username is required.'
        if (!operator.email.trim() || !operator.email.includes('@')) return 'A valid email is required.'
        if (!operator.display.trim()) return 'Display name is required.'
        return null
      case 1: // Target
        if (!sandbox.label.trim()) return 'Sandbox label is required.'
        if (!sandbox.nodeIp.trim()) return 'Node IP is required.'
        if (!sandbox.ansibleUser.trim()) return 'Ansible user is required.'
        if (!sandbox.iface.trim()) return 'Interface is required.'
        if (!sandbox.sshPrivateKey.trim())
          return 'SSH private key is required — paste it or load a key file.'
        return null
      case 2: // Security
        if (!passphrase) return 'Passphrase is required.'
        if (passphrase !== passphraseConfirm) return 'Passphrase entries do not match.'
        return null
      case 3: // Review
        return null
    }
  }

  function handleNext() {
    setLocalError(null)
    const err = validatePage(page)
    if (err) { setLocalError(err); return }
    if (page < 3) { const next = (page + 1) as Page; setPage(next); onPageChange?.(next) }
  }

  function handleBack() {
    setLocalError(null)
    if (page > 0) { const prev = (page - 1) as Page; setPage(prev); onPageChange?.(prev) }
  }

  function handleDeploy(e: FormEvent) {
    e.preventDefault()
    setLocalError(null)
    const err = validatePage(3) || validatePage(2) || validatePage(1) || validatePage(0)
    if (err) { setLocalError(err); return }
    onSubmit(operator, sandbox, passphrase)
  }

  const pageTitles = ['Operator identity', 'Target node', 'Security', 'Review & Deploy']
  const pageHints = [
    'Who is commissioning this environment.',
    'The node this installer will provision.',
    'One passphrase protects every backup — store it in your password manager.',
    'Confirm everything looks right, then deploy.',
  ]

  return (
    <div className="mx-auto flex min-h-full max-w-2xl flex-col justify-center pb-16">
      {/* Page indicator */}
      <div className="mb-4 flex items-center gap-1.5">
        {[0, 1, 2, 3].map((p) => (
          <span
            key={p}
            className={['h-1.5 flex-1 rounded-full transition', p === page ? 'bg-accent' : p < page ? 'bg-accent/40' : 'bg-border'].join(' ')}
          />
        ))}
      </div>

      <form onSubmit={page === 3 ? handleDeploy : (e) => { e.preventDefault(); handleNext() }} className="grid gap-4">
        <div>
          <h2 className="text-lg font-semibold text-text">{pageTitles[page]}</h2>
          <p className="mt-0.5 text-sm text-muted">{pageHints[page]}</p>
        </div>

        {/* Page 0: Identity */}
        {page === 0 && (
          <div className="grid gap-3 md:grid-cols-3">
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
        )}

        {/* Page 1: Target */}
        {page === 1 && (
          <div className="grid gap-3">
            <div className="grid gap-3 md:grid-cols-2">
              <Field label="Sandbox label" hint="DNS-safe subdomain label">
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
            <Field label="SSH private key" hint="Paste or load a local file.">
              <TextArea
                value={sandbox.sshPrivateKey}
                onChange={(e) => setSandbox((p) => ({ ...p, sshPrivateKey: e.target.value }))}
                placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                className="min-h-20"
              />
            </Field>
            <label className="inline-flex cursor-pointer items-center gap-2 rounded-lg border border-border bg-panel px-3 py-1.5 text-sm text-text transition hover:bg-white/5">
              Load key file
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
          </div>
        )}

        {/* Page 2: Security */}
        {page === 2 && (
          <div className="grid gap-3 md:grid-cols-2">
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
        )}

        {/* Page 3: Review & Deploy */}
        {page === 3 && (
          <div className="grid gap-3 text-sm">
            <div className="rounded-lg border border-border bg-panel/60 p-3">
              <div className="text-xs uppercase tracking-[0.18em] text-muted">Operator</div>
              <div className="mt-1 text-text">
                {operator.display} ({operator.username}) · {operator.email}
              </div>
            </div>
            <div className="rounded-lg border border-border bg-panel/60 p-3">
              <div className="text-xs uppercase tracking-[0.18em] text-muted">Sandbox</div>
              <div className="mt-1 text-text">
                Label: {sandbox.label}
                {sandbox.nodeIp ? ` · Node IP: ${sandbox.nodeIp}` : ''}
                <br />
                Ansible user: {sandbox.ansibleUser} · Interface: {sandbox.iface}
              </div>
            </div>
            <div className="rounded-lg border border-border bg-panel/60 p-3">
              <div className="text-xs uppercase tracking-[0.18em] text-muted">SSH key</div>
              <div className="mt-1 text-text">
                {/* Never echo private-key material, even truncated. */}
                {sandbox.sshPrivateKey
                  ? `✓ provided (${sandbox.sshPrivateKey.trim().split('\n').length} lines)`
                  : '(not provided)'}
              </div>
            </div>
          </div>
        )}

        {(localError || error) && (
          <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-100">
            {localError || error}
          </div>
        )}

        {/* Navigation */}
        <div className="flex items-center justify-between gap-3 pt-2">
          <button
            type="button"
            onClick={handleBack}
            disabled={page === 0}
            className="rounded-lg border border-border bg-panel px-4 py-2 text-sm font-medium text-text transition hover:bg-white/5 disabled:cursor-not-allowed disabled:opacity-40"
          >
            ← Back
          </button>
          {page < 3 ? (
            <button
              type="submit"
              className="rounded-lg border border-accent/30 bg-accent px-4 py-2 text-sm font-semibold text-bg transition hover:bg-accent/90"
            >
              Next →
            </button>
          ) : (
            <button
              type="submit"
              disabled={busy}
              className="rounded-lg border border-accent/30 bg-accent px-5 py-2.5 text-sm font-semibold text-bg transition hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {busy ? 'Deploying…' : 'Deploy'}
            </button>
          )}
        </div>
      </form>
    </div>
  )
}

export type { OperatorForm, SandboxForm }
