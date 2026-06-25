import { useEffect, useRef, useState } from 'react'
import { QRCodeSVG } from 'qrcode.react'
import { CaInstall, type CaCertPayload } from '../ui'
import { Disclosure } from '../shared/Disclosure'
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

  useEffect(() => {
    if (activePause && stationRef.current) {
      stationRef.current.focus()
    }
  }, [activePause?.pause_id])

  useEffect(() => {
    if (activePause?.pause_id !== 'passkey' || !runId) return
    let cancelled = false
    const interval = setInterval(async () => {
      const result = await pollPasskey(runId)
      if (!cancelled && result) setPasskeyCount(result)
    }, 3000)
    pollPasskey(runId).then((r) => {
      if (!cancelled && r) setPasskeyCount(r)
    })
    return () => { cancelled = true; clearInterval(interval) }
  }, [activePause?.pause_id, runId, pollPasskey])

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
      <div className="flex flex-col items-center justify-center gap-2 py-16">
        <p className="text-sm text-muted">Waiting for operator pauses…</p>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-3xl">
      <section
        ref={stationRef}
        tabIndex={-1}
        role="region"
        aria-label={`Active station: ${activePause.title}`}
        aria-live="polite"
        className="rounded-lg border border-border bg-panel p-4"
      >
        <div className="mb-3 flex items-center justify-between gap-3 border-b border-border pb-3">
          <div>
            <p className="text-xs uppercase tracking-[0.18em] text-muted">Active station</p>
            <h2 className="mt-0.5 text-base font-semibold text-text">{activePause.title}</h2>
          </div>
          <span className="rounded-md border border-border bg-bg px-2 py-0.5 text-[11px] text-muted">
            {activePause.pause_id}
          </span>
        </div>

        {/* Station content — single column, internal scroll keeps the page
            itself viewport-fit. (A two-column grid here overlapped in the
            field; pre blocks with unbreakable lines make column layouts
            fragile.) */}
        <div className="grid max-h-[calc(100vh-16rem)] min-w-0 gap-4 overflow-y-auto pr-1">
          {/* Workstation station: CA trust + hosts mapping */}
          {activePause.pause_id === 'workstation' && (
            <div className="grid min-w-0 gap-4">
              <div className="grid min-w-0 gap-3">
                <p className="text-xs uppercase tracking-[0.18em] text-muted">1 · Trust the DMF Local CA</p>
                <CaCertStation
                  payload={(activePause.payload as unknown as WorkstationPayload).ca}
                  onDownload={downloadCaCertificate}
                />
              </div>
              <div className="grid min-w-0 gap-3 border-t border-border pt-4">
                <p className="text-xs uppercase tracking-[0.18em] text-muted">2 · Map cluster hostnames</p>
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
              </div>
            </div>
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
          <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 p-2.5 text-sm text-red-100">
            {resumeError}
          </div>
        )}

        {/* Continue button */}
        <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-border pt-3">
          {activePause.pause_id === 'passkey' ? (
            <button
              type="button"
              onClick={onVerifyPasskey}
              disabled={passkeyChecking || resumeBusy}
              className="rounded-lg border border-accent/30 bg-accent px-4 py-2 text-sm font-semibold text-bg transition hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {passkeyChecking ? 'Checking…' : resumeBusy ? 'Continuing…' : 'Verify & Continue'}
            </button>
          ) : (
            <button
              type="button"
              disabled={resumeBusy}
              onClick={onResume}
              className="rounded-lg border border-accent/30 bg-accent px-4 py-2 text-sm font-semibold text-bg transition hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {resumeBusy ? 'Continuing…' : 'Continue'}
            </button>
          )}
          <span className="text-xs text-muted">
            {activePause.pause_id === 'passkey'
              ? 'The station stays active until the passkey check confirms the required count.'
              : 'The bootstrap will continue once resumed.'}
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
    <div className="grid gap-3">
      <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3">
        <p className="text-sm font-medium text-amber-300">
          Required on desktop Chrome/Chromium — enrollment fails silently there.
        </p>
        <p className="mt-1 text-sm leading-5 text-muted">
          Other browsers may proceed after a warning. Install the CA to avoid trust warnings on every service.
        </p>
      </div>
      {payload.requirement_note && (
        <Disclosure summary="Learn more">
          <p className="text-sm leading-5 text-muted">{payload.requirement_note}</p>
        </Disclosure>
      )}
      {payload.present ? (
        <>
          <pre className="max-h-32 min-w-0 overflow-auto whitespace-pre-wrap break-all rounded-lg border border-border bg-bg p-2.5 text-xs leading-5 text-text">
            {payload.pem}
          </pre>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => onDownload(payload)}
              className="rounded-lg border border-accent/30 bg-accent px-3 py-1.5 text-xs font-semibold text-bg transition hover:bg-accent/90"
            >
              Download
            </button>
            <span className="text-xs text-muted">{payload.filename}</span>
          </div>
          <CaInstall payload={payload} />
        </>
      ) : (
        <div className="rounded-lg border border-border bg-panel/60 p-3 text-sm text-muted">
          {payload.note || 'The CA certificate is not available yet.'}
        </div>
      )}
    </div>
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
    <div className="grid gap-3">
      <p className="text-sm leading-5 text-muted">{payload.note}</p>
      <div className="min-w-0 rounded-lg border border-border bg-bg p-3">
        <div className="flex items-center justify-between gap-2">
          <p className="text-xs uppercase tracking-[0.18em] text-muted">/etc/hosts entries</p>
          <button
            type="button"
            onClick={async () => {
              await onCopy(payload)
              setCopied(true)
              setTimeout(() => setCopied(false), 2000)
            }}
            className="shrink-0 rounded-md border border-border bg-white/5 px-2.5 py-1 text-[11px] font-semibold text-text transition hover:bg-white/10"
          >
            {copied ? '✓ Copied' : 'Copy'}
          </button>
        </div>
        <pre className="mt-2 max-h-24 min-w-0 overflow-auto whitespace-pre-wrap break-all rounded-md border border-border bg-panel p-2 text-[11px] leading-5 text-text">
          {payload.entries.join('\n') || 'No entries yet.'}
        </pre>
      </div>
      <div className="min-w-0 rounded-lg border border-border bg-bg p-3">
        <div className="flex items-center justify-between gap-2">
          <p className="text-xs uppercase tracking-[0.18em] text-muted">One-liner</p>
          <button
            type="button"
            onClick={async () => {
              await onCopyCommand()
              setCopiedCmd(true)
              setTimeout(() => setCopiedCmd(false), 2000)
            }}
            className="shrink-0 rounded-md border border-border bg-white/5 px-2.5 py-1 text-[11px] font-semibold text-text transition hover:bg-white/10"
          >
            {copiedCmd ? '✓ Copied' : 'Copy'}
          </button>
        </div>
        <pre className="mt-2 max-h-24 min-w-0 overflow-auto whitespace-pre-wrap break-all rounded-md border border-border bg-panel p-2 text-[11px] leading-5 text-text">
          {`printf '%s\\n' ${payload.entries.map((e) => `'${e.replaceAll("'", "'\\''")}'`).join(' ')} | sudo tee -a /etc/hosts >/dev/null`}
        </pre>
      </div>
    </div>
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
    <div className="grid gap-3">
      <p className="text-sm leading-5 text-muted">{payload.hint}</p>
      {/* The count can sit at 0/1 because enrollment failed silently — call it
          out where the operator is re-checking, not only on the CA station. */}
      <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-2.5 text-xs leading-5 text-amber-300">
        On desktop Chrome/Chromium, passkey enrollment can fail silently if the
        DMF Local CA isn&apos;t trusted. If the count stays below the required
        number, complete the CA-trust step (previous station) or try another
        browser, then re-check.
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        <div className="grid gap-2 rounded-lg border border-border bg-panel/60 p-3 text-sm">
          <div className="text-muted">
            A. Open the enrollment link:{' '}
            {payload.enrollment_url ? (
              <a
                className="inline-block rounded-md border border-border bg-white/5 px-2 py-1 text-xs text-text transition hover:bg-white/8"
                href={payload.enrollment_url}
                target="_blank"
                rel="noopener noreferrer"
              >
                {payload.enrollment_url}
              </a>
            ) : (
              <span className="text-text">
                {displayCount.confirmed >= displayCount.required
                  ? `already enrolled (${displayCount.confirmed}/${displayCount.required})`
                  : 'No enrollment URL is available yet.'}
              </span>
            )}
          </div>
          <div className="text-muted">
            B. In Console, open Create new device invitation and complete enrollment.
          </div>
        </div>
        {payload.enrollment_url && (
          <div className="flex flex-col items-center gap-2 rounded-lg border border-border bg-panel/60 p-3">
            <p className="text-xs uppercase tracking-[0.18em] text-muted">Scan to enroll on phone</p>
            <div className="rounded-md border border-border bg-white/5 p-2">
              <QRCodeSVG value={payload.enrollment_url} size={120} level="M" />
            </div>
            <p className="text-[11px] leading-4 text-muted">
              A <span className="text-text">.test</span> domain won&apos;t resolve off-network.
            </p>
          </div>
        )}
      </div>
      <div className="flex items-center gap-3">
        {passkeyStatus && (
          <span className="rounded-md border border-border bg-panel px-2 py-0.5 text-[11px] text-muted">
            {passkeyStatus}
          </span>
        )}
        <span className="text-xs text-muted" aria-live="polite">
          Passkey: {displayCount.confirmed}/{displayCount.required} confirmed
        </span>
      </div>
    </div>
  )
}
