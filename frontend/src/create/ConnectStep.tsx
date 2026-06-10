import { useEffect, useRef, useState } from 'react'
import { QRCodeSVG } from 'qrcode.react'
import { CaInstall, type CaCertPayload } from '../ui'
import type { ActivePause } from '../hooks/useCreateFlow'

type HostsMapPayload = {
  entries: string[]
  node_ip: string
  base_domain: string
  dns_note: string
  note: string
}

type PasskeyPayload = {
  enrollment_url: string
  confirmed: number
  required: number
  hint: string
}

type WorkstationPayload = {
  ca: CaCertPayload
  hosts: HostsMapPayload
}

type ConnectStepProps = {
  activePause: ActivePause | null
  resumeBusy: boolean
  resumeError: string | null
  passkeyChecking: boolean
  passkeyStatus: string | null
  onResume: () => void
  onVerifyPasskey: () => void
  envId: string
  runId: string
  pollPasskey: (runId: string) => Promise<{ confirmed: number; required: number } | null>
}

export function ConnectStep({
  activePause,
  resumeBusy,
  resumeError,
  passkeyChecking,
  passkeyStatus,
  onResume,
  onVerifyPasskey,
  runId,
  pollPasskey,
}: ConnectStepProps) {
  const [passkeyCount, setPasskeyCount] = useState<{ confirmed: number; required: number } | null>(null)
  const stationRef = useRef<HTMLDivElement>(null)

  // Move focus to active station on change
  useEffect(() => {
    if (activePause && stationRef.current) {
      stationRef.current.focus()
    }
  }, [activePause?.pause_id])

  // Poll passkey count when passkey pause is active
  useEffect(() => {
    if (activePause?.pause_id !== 'passkey' || !runId) return
    let cancelled = false
    const interval = setInterval(async () => {
      const result = await pollPasskey(runId)
      if (!cancelled && result) {
        setPasskeyCount(result)
      }
    }, 3000)
    // Initial poll
    pollPasskey(runId).then((r) => {
      if (!cancelled && r) setPasskeyCount(r)
    })
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [activePause?.pause_id, runId, pollPasskey])

  // Download CA certificate
  function downloadCaCertificate(payload: CaCertPayload) {
    const blob = new Blob([payload.pem], { type: 'application/x-pem-file' })
    const objectUrl = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = objectUrl
    anchor.download = payload.filename
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0)
  }

  // Hosts map helpers
  function shellQuote(value: string): string {
    return `'${value.replaceAll("'", "'\\''")}'`
  }

  function hostsMapCommand(entries: string[]): string {
    return `printf '%s\\n' ${entries.map(shellQuote).join(' ')} | sudo tee -a /etc/hosts >/dev/null`
  }

  async function copyText(text: string) {
    await navigator.clipboard.writeText(text)
  }

  async function copyHostsMap(payload: HostsMapPayload) {
    await copyText(payload.entries.join('\n'))
  }

  if (!activePause) {
    return (
      <div className="rounded-[1.75rem] border border-border/70 bg-panel/80 p-5 shadow-glow backdrop-blur">
        <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Connect</p>
        <h2 className="mt-2 text-2xl font-semibold text-text">Waiting for operator pauses…</h2>
        <p className="mt-3 text-sm leading-6 text-muted">
          The bootstrap will pause here when it needs your attention.
        </p>
      </div>
    )
  }

  return (
    <div className="grid gap-6">
      <section
        ref={stationRef}
        tabIndex={-1}
        role="region"
        aria-label={`Active station: ${activePause.title}`}
        aria-live="polite"
        className="rounded-[1.75rem] border border-accent/30 bg-panel/80 p-5 shadow-glow backdrop-blur"
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Active station</p>
            <h2 className="mt-2 text-2xl font-semibold text-text">{activePause.title}</h2>
          </div>
          <span className="rounded-full border border-border/70 bg-white/5 px-3 py-1 text-xs text-muted">
            {activePause.pause_id}
          </span>
        </div>

        {/* Station content — fixed-height slots to prevent reflow */}
        <div className="mt-5 grid gap-4" style={{ minHeight: '16rem' }}>
          {/* Workstation station: CA trust + hosts mapping, one Continue */}
          {activePause.pause_id === 'workstation' && (
            <>
              <p className="text-xs uppercase tracking-[0.28em] text-accentSoft">
                1 · Trust the DMF Local CA
              </p>
              <CaCertStation
                payload={(activePause.payload as unknown as WorkstationPayload).ca}
                onDownload={downloadCaCertificate}
              />
              <p className="mt-2 text-xs uppercase tracking-[0.28em] text-accentSoft">
                2 · Map cluster hostnames
              </p>
              <HostsMapStation
                payload={(activePause.payload as unknown as WorkstationPayload).hosts}
                onCopy={copyHostsMap}
                onCopyCommand={async () => {
                  await copyText(
                    hostsMapCommand(
                      (activePause.payload as unknown as WorkstationPayload).hosts.entries,
                    ),
                  )
                }}
              />
            </>
          )}

          {/* Passkey station */}
          {activePause.pause_id === 'passkey' && (
            <PasskeyStation
              payload={activePause.payload as PasskeyPayload}
              passkeyCount={passkeyCount}
              passkeyChecking={passkeyChecking}
              passkeyStatus={passkeyStatus}
              onVerify={onVerifyPasskey}
            />
          )}
        </div>

        {/* Error */}
        {resumeError && (
          <div className="mt-4 rounded-2xl border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-100">
            {resumeError}
          </div>
        )}

        {/* Continue button */}
        <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-border/60 pt-4">
          {activePause.pause_id === 'passkey' ? (
            <button
              type="button"
              onClick={onVerifyPasskey}
              disabled={passkeyChecking || resumeBusy}
              className="rounded-2xl border border-accent/30 bg-accent px-6 py-3 text-sm font-semibold text-bg transition-transform hover:-translate-y-0.5 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {passkeyChecking ? 'Checking…' : resumeBusy ? 'Continuing…' : 'Verify & Continue'}
            </button>
          ) : (
            <button
              type="button"
              disabled={resumeBusy}
              onClick={onResume}
              className="rounded-2xl border border-accent/30 bg-accent px-6 py-3 text-sm font-semibold text-bg transition-transform hover:-translate-y-0.5 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {resumeBusy ? 'Continuing…' : "I've completed these steps → Continue"}
            </button>
          )}
          <span className="text-sm text-muted">
            {activePause.pause_id === 'passkey'
              ? 'The station stays active until the passkey check confirms the required count.'
              : 'The station stays active until the pause is resumed.'}
          </span>
        </div>
      </section>
    </div>
  )
}

// ─── Station sub-components ──────────────────────────────────────────────────

function CaCertStation({
  payload,
  onDownload,
}: {
  payload: CaCertPayload
  onDownload: (p: CaCertPayload) => void
}) {
  return (
    <>
      <div className="rounded-2xl border border-amber-500/40 bg-amber-500/10 p-4">
        <p className="text-sm font-semibold text-amber-300">
          Required step — passkeys will not work without this.
        </p>
        <p className="mt-1 text-sm leading-6 text-muted">
          If the DMF Local CA is not trusted, WebAuthn passkey enrollment fails silently:
          &quot;Registration cancelled or timed out&quot; means the browser treated
          the origin as non-secure, NOT that you cancelled anything.
        </p>
      </div>
      {payload.requirement_note && (
        <p className="text-sm leading-6 text-muted">{payload.requirement_note}</p>
      )}
      <p className="text-sm leading-6 text-muted">
        Download the CA certificate and install it in your browser&apos;s trust store.
        The PEM is public.
      </p>
      {payload.present ? (
        <>
          <pre className="max-h-48 overflow-auto rounded-3xl border border-border/70 bg-black/30 p-4 text-xs leading-6 text-text">
            {payload.pem}
          </pre>
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={() => onDownload(payload)}
              className="rounded-2xl border border-accent/30 bg-accent px-5 py-3 text-sm font-semibold text-bg transition-transform hover:-translate-y-0.5"
            >
              Download
            </button>
            <span className="text-sm text-muted">{payload.filename}</span>
          </div>
          <CaInstall payload={payload} />
        </>
      ) : (
        <div className="rounded-2xl border border-border/70 bg-white/5 p-4 text-sm text-muted">
          <p>{payload.note || 'The CA certificate is not available yet.'}</p>
        </div>
      )}
    </>
  )
}

function HostsMapStation({
  payload,
  onCopy,
  onCopyCommand,
}: {
  payload: HostsMapPayload
  onCopy: (p: HostsMapPayload) => Promise<void>
  onCopyCommand: () => Promise<void>
}) {
  const [copied, setCopied] = useState(false)
  const [copiedCmd, setCopiedCmd] = useState(false)

  return (
    <>
      <p className="text-sm leading-6 text-muted">{payload.note}</p>
      <p className="text-sm leading-6 text-muted">{payload.dns_note}</p>
      <div className="grid gap-4">
        <div className="rounded-3xl border border-border/70 bg-black/30 p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs uppercase tracking-[0.24em] text-accentSoft">/etc/hosts entries</p>
            <button
              type="button"
              onClick={async () => {
                await onCopy(payload)
                setCopied(true)
                setTimeout(() => setCopied(false), 2000)
              }}
              className="rounded-2xl border border-border/70 bg-white/5 px-4 py-2 text-xs font-semibold text-text transition hover:bg-white/10"
            >
              {copied ? '✓ Copied' : 'Copy'}
            </button>
          </div>
          <pre className="mt-3 max-h-48 overflow-auto rounded-2xl border border-border/70 bg-slate-950/80 p-3 text-xs leading-6 text-text">
            {payload.entries.join('\n') || 'No entries yet.'}
          </pre>
        </div>
        <div className="rounded-3xl border border-border/70 bg-black/30 p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs uppercase tracking-[0.24em] text-accentSoft">One-liner</p>
            <button
              type="button"
              onClick={async () => {
                await onCopyCommand()
                setCopiedCmd(true)
                setTimeout(() => setCopiedCmd(false), 2000)
              }}
              className="rounded-2xl border border-border/70 bg-white/5 px-4 py-2 text-xs font-semibold text-text transition hover:bg-white/10"
            >
              {copiedCmd ? '✓ Copied' : 'Copy'}
            </button>
          </div>
          <pre className="mt-3 max-h-48 overflow-auto rounded-2xl border border-border/70 bg-slate-950/80 p-3 text-xs leading-6 text-text">
            {`printf '%s\\n' ${payload.entries.map((e) => `'${e.replaceAll("'", "'\\''")}'`).join(' ')} | sudo tee -a /etc/hosts >/dev/null`}
          </pre>
        </div>
      </div>
    </>
  )
}

function PasskeyStation({
  payload,
  passkeyCount,
  passkeyChecking: _passkeyChecking,
  passkeyStatus,
  onVerify: _onVerify,
}: {
  payload: PasskeyPayload
  passkeyCount: { confirmed: number; required: number } | null
  passkeyChecking: boolean
  passkeyStatus: string | null
  onVerify: () => void
}) {
  const displayCount = passkeyCount ?? { confirmed: payload.confirmed, required: payload.required }

  return (
    <>
      <p className="text-sm leading-6 text-muted">{payload.hint}</p>
      <div className="grid gap-3 rounded-3xl border border-border/70 bg-black/30 p-4">
        <div className="grid gap-2 text-sm leading-6 text-muted">
          <div>
            A. Open the enrollment link:
            {payload.enrollment_url ? (
              <a
                className="ml-2 inline-flex w-fit rounded-2xl border border-border/70 bg-white/5 px-3 py-1.5 text-text transition hover:bg-white/8"
                href={payload.enrollment_url}
                target="_blank"
                rel="noopener noreferrer"
              >
                {payload.enrollment_url}
              </a>
            ) : (
              <span className="ml-2 text-text">
                {displayCount.confirmed >= displayCount.required
                  ? `already enrolled (${displayCount.confirmed}/${displayCount.required})`
                  : 'No enrollment URL is available yet.'}
              </span>
            )}
          </div>
          <div>
            B. In Console, open Create new device invitation and complete enrollment.
          </div>
          <div className="text-text">Operator username: {payload.confirmed >= 0 ? '' : ''}</div>
        </div>
      </div>

      {payload.enrollment_url && (
        <div className="flex flex-col items-start gap-2 rounded-3xl border border-border/70 bg-black/30 p-4">
          <p className="text-xs uppercase tracking-[0.24em] text-accentSoft">Scan to enroll on phone</p>
          <div className="rounded-2xl border border-border/70 bg-white/5 p-3">
            <QRCodeSVG value={payload.enrollment_url} size={160} level="M" />
          </div>
          <p className="text-[11px] leading-5 text-muted">
            Note: a QR for a <span className="text-text">.test</span> domain won&apos;t resolve on a phone off-network — mainly useful for real-domain cloud envs.
          </p>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3">
        {passkeyStatus && (
          <span className="rounded-full border border-border/70 bg-white/5 px-3 py-1 text-xs text-muted">
            {passkeyStatus}
          </span>
        )}
        <span className="text-sm text-muted" aria-live="polite">
          Passkey: {displayCount.confirmed}/{displayCount.required} confirmed
        </span>
      </div>
    </>
  )
}
