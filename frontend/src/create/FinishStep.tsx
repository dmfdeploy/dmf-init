import { useCallback, useEffect, useState } from 'react'

type BootstrapCheckpoint = {
  n: number
  artifact_name: string
}

type PackageStatus = {
  downloaded_at: number | null
  sha256?: string
  filename?: string
}

type FinishStepProps = {
  checkpoints: BootstrapCheckpoint[]
  terminal: { kind: 'complete'; runId: string; checkpoints: number[] } | { kind: 'error'; error?: string } | null
  envId: string
  onRevalidate: () => void
}

async function fetchPackageStatus(envId: string): Promise<PackageStatus | null> {
  try {
    const response = await fetch(`/api/package/${encodeURIComponent(envId)}/status`, {
      credentials: 'same-origin',
      headers: { accept: 'application/json' },
    })
    if (!response.ok) return null
    return (await response.json()) as PackageStatus
  } catch {
    return null
  }
}

export function FinishStep({ checkpoints, terminal, envId, onRevalidate }: FinishStepProps) {
  const [pkgStatus, setPkgStatus] = useState<PackageStatus | null>(null)
  const [pkgBusy, setPkgBusy] = useState(false)
  const [pkgError, setPkgError] = useState<string | null>(null)

  const isComplete = terminal?.kind === 'complete'
  const isError = terminal?.kind === 'error'
  const downloaded = Boolean(pkgStatus?.downloaded_at)

  const refreshStatus = useCallback(async () => {
    const status = await fetchPackageStatus(envId)
    if (status) setPkgStatus(status)
  }, [envId])

  useEffect(() => {
    if (isComplete) void refreshStatus()
  }, [isComplete, refreshStatus])

  async function downloadPackage() {
    setPkgBusy(true)
    setPkgError(null)
    try {
      const response = await fetch(`/api/package/${encodeURIComponent(envId)}`, {
        credentials: 'same-origin',
      })
      if (!response.ok) {
        throw new Error(await response.text())
      }
      const disposition = response.headers.get('content-disposition') ?? ''
      const match = /filename="([^"]+)"/.exec(disposition)
      const filename = match?.[1] ?? `dmf-package-${envId}.zip`
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = filename
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 0)
      // The server records completion when the stream finished; reflect it.
      await refreshStatus()
    } catch (error) {
      setPkgError(error instanceof Error ? error.message : String(error))
    } finally {
      setPkgBusy(false)
    }
  }

  const cp3 = checkpoints.find((c) => c.n === 3)

  return (
    <div className="grid gap-6">
      {/* Error state */}
      {isError && (
        <section className="rounded-3xl border border-red-500/30 bg-red-500/10 p-5">
          <p className="text-xs uppercase tracking-[0.34em] text-red-200">Error</p>
          <h2 className="mt-2 text-2xl font-semibold text-text">Bootstrap stopped.</h2>
          {terminal.error && (
            <p className="mt-3 text-sm leading-6 text-red-100">{terminal.error}</p>
          )}
        </section>
      )}

      {/* Package + safe-to-delete: amber until the package download completed,
          green after — the server records stream completion (Art. 1: we state
          what we know; verify the file on your side via MANIFEST.json). */}
      {isComplete && (
        <section
          aria-live="polite"
          className={[
            'rounded-[1.75rem] border p-5 shadow-glow',
            downloaded
              ? 'border-emerald-400/30 bg-emerald-400/10'
              : 'border-amber-500/40 bg-amber-500/10',
          ].join(' ')}
        >
          <p
            className={[
              'text-xs uppercase tracking-[0.34em]',
              downloaded ? 'text-emerald-200' : 'text-amber-300',
            ].join(' ')}
          >
            {downloaded ? 'Safe to delete this container' : 'One step left'}
          </p>
          <h2 className="mt-2 text-2xl font-semibold text-text">
            {downloaded ? 'Your recovery package is saved.' : 'Download your recovery package.'}
          </h2>
          <p className="mt-3 text-sm leading-6 text-muted">
            {downloaded
              ? 'Keep the package and your passphrase in separate places; together they are the only way to manage or recover this environment.'
              : 'One zip with everything to keep: the encrypted backup (checkpoint #3), the cluster CA certificate, and a README with the workstation reference. Your passphrase is NOT inside — store it separately.'}
          </p>

          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={() => void downloadPackage()}
              disabled={pkgBusy}
              className="rounded-2xl border border-accent/30 bg-accent px-6 py-3 text-sm font-semibold text-bg transition-transform hover:-translate-y-0.5 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {pkgBusy ? 'Preparing…' : downloaded ? 'Download again' : 'Download package'}
            </button>
            {downloaded && pkgStatus?.downloaded_at ? (
              <span className="text-sm text-muted">
                ✓ downloaded {new Date(pkgStatus.downloaded_at * 1000).toLocaleTimeString()}
                {pkgStatus.sha256 ? ` · sha256 ${pkgStatus.sha256.slice(0, 12)}…` : ''}
              </span>
            ) : null}
          </div>

          {cp3 ? (
            <p className="mt-3 text-xs leading-5 text-muted">
              Backup inside: {cp3.artifact_name}
            </p>
          ) : null}

          {pkgError ? (
            <div className="mt-4 rounded-2xl border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-100">
              {pkgError}
            </div>
          ) : null}
        </section>
      )}

      {/* Re-validate */}
      {isComplete && (
        <section className="rounded-[1.75rem] border border-border/70 bg-panel/80 p-5 shadow-glow backdrop-blur">
          <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Optional</p>
          <h3 className="mt-2 text-xl font-semibold text-text">Re-validate the cluster</h3>
          <p className="mt-2 text-sm leading-6 text-muted">
            Run a doctor check to verify cluster health after bootstrap.
          </p>
          <div className="mt-4">
            <button
              type="button"
              onClick={onRevalidate}
              className="rounded-2xl border border-border/70 bg-white/5 px-5 py-3 text-sm font-semibold text-text transition hover:bg-white/8"
            >
              Re-validate
            </button>
          </div>
        </section>
      )}
    </div>
  )
}
