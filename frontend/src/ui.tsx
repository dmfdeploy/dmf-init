import { type InputHTMLAttributes, type ReactNode, type TextareaHTMLAttributes, useState } from 'react'

export function Field(props: {
  label: string
  hint?: string
  children: ReactNode
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-xs uppercase tracking-[0.2em] text-muted">{props.label}</span>
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
        'rounded-lg border border-border bg-panel px-3 py-2 text-sm text-text outline-none',
        'transition focus:border-accent/50 focus:ring-1 focus:ring-accent/20',
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
        'min-h-24 rounded-lg border border-border bg-panel px-3 py-2 text-sm text-text outline-none',
        'transition focus:border-accent/50 focus:ring-1 focus:ring-accent/20',
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
    <section className="rounded-lg border border-border bg-panel p-4">
      <div className="mb-4">
        <p className="text-xs uppercase tracking-[0.2em] text-muted">
          {props.eyebrow ?? 'Create new'}
        </p>
        <h2 className="mt-1 text-lg font-semibold text-text">{props.title}</h2>
        <p className="mt-1 max-w-2xl text-sm leading-5 text-muted">{props.description}</p>
      </div>
      <div className="grid gap-3">{props.children}</div>
    </section>
  )
}

export function ArtifactDownload(props: {
  artifactName: string
  label?: string
}) {
  return (
    <div className="min-w-0 rounded-lg border border-border bg-panel p-3">
      <span className="text-muted">{props.label ?? 'Artifact'}</span>
      <div className="mt-1 flex items-center gap-3">
        <span className="font-medium text-text">{props.artifactName}</span>
        <a
          href={`/api/backup/artifact/${encodeURIComponent(props.artifactName)}`}
          download={props.artifactName}
          className="rounded-md border border-accent/30 bg-accent/10 px-2.5 py-1 text-xs font-medium text-accentSoft transition hover:bg-accent/20"
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

// Detect the current OS for auto-expand
function detectOS(): 'windows' | 'macos' | 'linux' | 'firefox' | 'unknown' {
  const ua = navigator.userAgent
  const platform = (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData?.platform ?? navigator.platform ?? ''
  if (/Win(dows)?/i.test(ua) || /Win/i.test(platform)) return 'windows'
  if (/Mac/i.test(ua) || /Mac/i.test(platform)) return 'macos'
  if (/Linux/i.test(ua) || /Linux/i.test(platform)) return 'linux'
  return 'unknown'
}

type OsKey = 'windows' | 'macos' | 'debian' | 'fedora' | 'firefox'

export function CaInstall(props: {
  payload: CaCertPayload
  onCopyError?: (msg: string) => void
}) {
  const { payload } = props
  const onCopyError = props.onCopyError ?? (() => {})
  const detected = detectOS()

  // Map detected OS to which sections should be open by default
  const osMap: Record<string, OsKey[]> = {
    windows: ['windows', 'firefox'],
    macos: ['macos', 'firefox'],
    linux: ['debian', 'fedora', 'firefox'],
  }
  const autoOpen = new Set(osMap[detected] ?? [])

  return (
    <div className="grid gap-2">
      <CaOsSection
        os="Windows"
        osKey="windows"
        defaultOpen={autoOpen.has('windows')}
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
      </CaOsSection>

      <CaOsSection
        os="macOS"
        osKey="macos"
        defaultOpen={autoOpen.has('macos')}
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
      </CaOsSection>

      <CaOsSection
        os="Debian/Ubuntu"
        osKey="debian"
        defaultOpen={autoOpen.has('debian')}
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
      </CaOsSection>

      <CaOsSection
        os="Fedora/RHEL"
        osKey="fedora"
        defaultOpen={autoOpen.has('fedora')}
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
      </CaOsSection>

      <CaOsSection
        os="Firefox (own store — recommended)"
        osKey="firefox"
        defaultOpen={autoOpen.has('firefox')}
      >
        <p>
          Firefox maintains its own certificate store, separate from the OS.
          Import here to trust the CA only in Firefox, bounding blast radius.
        </p>
        <p className="mt-2 text-sm text-text">
          Settings → Privacy &amp; Security → Certificates → View Certificates →
          Authorities → Import → select{' '}
          <code className="rounded bg-white/10 px-1 text-xs">{payload.filename}</code> → check
          &quot;Trust this CA to identify websites&quot;.
        </p>
        <p className="mt-1.5 text-sm text-muted">
          Note: Firefox on Windows uses its own store — follow this same import path.
        </p>
      </CaOsSection>

      <p className="text-sm leading-5 text-muted">
        Restart your browser after importing the certificate. To remove later, delete the
        &quot;DMF Local CA&quot; entry from your certificate store.
      </p>
    </div>
  )
}

function CaOsSection(props: {
  os: string
  osKey: OsKey
  defaultOpen?: boolean
  children: ReactNode
  command?: string
  onCopy?: () => void
}) {
  const [open, setOpen] = useState(props.defaultOpen ?? false)
  return (
    <details open={open} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)} className="group rounded-lg border border-border bg-panel/40">
      <summary
        className="flex cursor-pointer items-center justify-between rounded-lg px-3 py-2 text-sm font-medium text-muted transition hover:text-text"
        onClick={(e) => e.preventDefault()}
      >
        <span>{props.os}</span>
        <span className="text-xs transition-transform group-open:rotate-90" aria-hidden="true">▸</span>
      </summary>
      <div className="px-3 pb-3">
        {props.children}
        {props.command && props.onCopy && (
          <div className="mt-2">
            <div className="flex items-center justify-between gap-2">
              <pre className="min-w-0 overflow-auto whitespace-pre-wrap break-all rounded-md border border-border bg-bg p-2 text-xs leading-5 text-text">
                {props.command}
              </pre>
              <button
                type="button"
                onClick={props.onCopy}
                className="shrink-0 rounded-md border border-border bg-white/5 px-2.5 py-1.5 text-xs font-semibold text-text transition hover:bg-white/10"
              >
                Copy
              </button>
            </div>
          </div>
        )}
      </div>
    </details>
  )
}
