import { type InputHTMLAttributes, type ReactNode, type TextareaHTMLAttributes } from 'react'

export function Field(props: {
  label: string
  hint?: string
  children: ReactNode
}) {
  return (
    <label className="flex flex-col gap-2">
      <span className="text-xs uppercase tracking-[0.28em] text-muted">{props.label}</span>
      {props.children}
      {props.hint ? <span className="text-xs leading-5 text-muted">{props.hint}</span> : null}
    </label>
  )
}

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={[
        'rounded-2xl border border-border/80 bg-white/5 px-4 py-3 text-sm text-text outline-none',
        'transition focus:border-accent/50 focus:bg-white/8 focus:ring-2 focus:ring-accent/20',
        props.className ?? '',
      ].join(' ')}
    />
  )
}

export function TextArea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      {...props}
      className={[
        'min-h-28 rounded-2xl border border-border/80 bg-white/5 px-4 py-3 text-sm text-text outline-none',
        'transition focus:border-accent/50 focus:bg-white/8 focus:ring-2 focus:ring-accent/20',
        props.className ?? '',
      ].join(' ')}
    />
  )
}

export function SectionCard(props: {
  title: string
  description: string
  eyebrow?: string
  children: ReactNode
}) {
  return (
    <section className="rounded-[1.75rem] border border-border/70 bg-panel/80 p-5 shadow-glow backdrop-blur">
      <div className="mb-5">
        <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">
          {props.eyebrow ?? 'Create new'}
        </p>
        <h2 className="mt-2 text-xl font-semibold text-text">{props.title}</h2>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-muted">{props.description}</p>
      </div>
      <div className="grid gap-4">{props.children}</div>
    </section>
  )
}

export function ArtifactDownload(props: {
  artifactName: string
  label?: string
}) {
  return (
    <div className="rounded-2xl border border-border/70 bg-white/5 p-3">
      <span className="text-muted">{props.label ?? 'Artifact'}</span>
      <div className="mt-1 flex items-center gap-3">
        <span className="font-medium text-text">{props.artifactName}</span>
        <a
          href={`/api/backup/artifact/${encodeURIComponent(props.artifactName)}`}
          download={props.artifactName}
          className="rounded-lg border border-accent/30 bg-accent/10 px-3 py-1.5 text-xs font-medium text-accentSoft transition hover:bg-accent/20"
        >
          Download
        </a>
      </div>
    </div>
  )
}

// ---- CA Certificate Install Instructions ----

export type CaCertPayload = {
  present: boolean
  filename: string
  pem: string
  note: string
  requirement_note?: string
}

async function copyText(text: string): Promise<void> {
  await navigator.clipboard.writeText(text)
}

function OsInstruction(props: {
  os: string
  children: ReactNode
  command?: string
  onCopy?: () => void
}) {
  return (
    <div className="rounded-3xl border border-border/70 bg-black/30 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-[0.24em] text-accentSoft">{props.os}</p>
          <div className="mt-2 text-sm text-text">{props.children}</div>
        </div>
        {props.command && props.onCopy && (
          <button
            type="button"
            onClick={props.onCopy}
            className="rounded-2xl border border-border/70 bg-white/5 px-4 py-2 text-xs font-semibold text-text transition hover:bg-white/10"
          >
            Copy
          </button>
        )}
      </div>
      {props.command && (
        <pre className="mt-3 overflow-auto rounded-2xl border border-border/70 bg-slate-950/80 p-3 text-xs leading-6 text-text">
          {props.command}
        </pre>
      )}
    </div>
  )
}

export function CaInstall(props: {
  payload: CaCertPayload
  onCopyError?: (msg: string) => void
}) {
  const { payload } = props
  const onCopyError = props.onCopyError ?? (() => {})

  return (
    <div className="grid gap-3">
      {/* Windows */}
      <OsInstruction
        os="Windows"
        command={`certutil -addstore -f "Root" ${payload.filename}`}
        onCopy={() =>
          copyText(`certutil -addstore -f "Root" ${payload.filename}`).catch(() =>
            onCopyError('Unable to copy Windows command.'),
          )
        }
      >
        <p>
          Run as Administrator: <code className="rounded bg-white/10 px-1 text-xs">certutil -addstore -f &quot;Root&quot; {payload.filename}</code>
        </p>
        <p className="mt-1">
          Or GUI: double-click {payload.filename} → Install Certificate → Local Machine →
          &quot;Place all certificates in the following store&quot; → Trusted Root Certification
          Authorities → Finish. Restart the browser.
        </p>
      </OsInstruction>

      {/* macOS */}
      <OsInstruction
        os="macOS"
        command={`sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ~/Downloads/${payload.filename}`}
        onCopy={() =>
          copyText(
            `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ~/Downloads/${payload.filename}`,
          ).catch(() => onCopyError('Unable to copy macOS command.'))
        }
      >
        <p>Import the cert into the System keychain, then restart the browser.</p>
        <p className="mt-1">
          Or GUI: open Keychain Access → System → drag {payload.filename} in → double-click → Always Trust. Restart the browser.
        </p>
      </OsInstruction>

      {/* Debian/Ubuntu */}
      <OsInstruction
        os="Debian/Ubuntu"
        command={`sudo cp ${payload.filename} /usr/local/share/ca-certificates/ && sudo update-ca-certificates`}
        onCopy={() =>
          copyText(
            `sudo cp ${payload.filename} /usr/local/share/ca-certificates/ && sudo update-ca-certificates`,
          ).catch(() => onCopyError('Unable to copy Debian command.'))
        }
      >
        <p>
          Copy the CA into the system trust store, update certificates, then restart the browser.
        </p>
      </OsInstruction>

      {/* Fedora/RHEL */}
      <OsInstruction
        os="Fedora/RHEL"
        command={`sudo cp ${payload.filename} /etc/pki/ca-trust/source/anchors/ && sudo update-ca-trust`}
        onCopy={() =>
          copyText(
            `sudo cp ${payload.filename} /etc/pki/ca-trust/source/anchors/ && sudo update-ca-trust`,
          ).catch(() => onCopyError('Unable to copy Fedora command.'))
        }
      >
        <p>
          Copy the CA into the CA trust source, update the trust database, then restart the browser.
        </p>
      </OsInstruction>

      {/* Firefox */}
      <div className="rounded-3xl border border-border/70 bg-black/30 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-xs uppercase tracking-[0.24em] text-accentSoft">
              Firefox (own store — recommended)
            </p>
            <p className="mt-2 text-sm text-text">
              Firefox maintains its own certificate store, separate from the OS.
              Import here to trust the CA only in Firefox, bounding blast radius.
            </p>
          </div>
        </div>
        <p className="mt-3 text-sm text-text">
          Settings → Privacy &amp; Security → Certificates → View Certificates →
          Authorities → Import → select{' '}
          <code className="rounded bg-white/10 px-1 text-xs">{payload.filename}</code> → check
          &quot;Trust this CA to identify websites&quot;.
        </p>
        <p className="mt-2 text-sm text-muted">
          Note: Firefox on Windows uses its own store — follow this same import path.
        </p>
      </div>

      <p className="text-sm leading-6 text-muted">
        Restart your browser after importing the certificate. To remove later, delete the
        &quot;DMF Local CA&quot; entry from your certificate store.
      </p>
    </div>
  )
}
