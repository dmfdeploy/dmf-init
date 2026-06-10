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
      await refreshStatus()
    } catch (error) {
      setPkgError(error instanceof Error ? error.message : String(error))
    } finally {
      setPkgBusy(false)
    }
  }

  const cp3 = checkpoints.find((c) => c.n === 3)

  return (
    <div className="mx-auto max-w-2xl grid gap-4">
      {/* Error state */}
      {isError && (
        <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-4">
          <p className="text-xs uppercase tracking-[0.2em] text-red-200">Error</p>
          <h2 className="mt-1 text-lg font-semibold text-text">Bootstrap stopped.</h2>
          {terminal.error && (
            <p className="mt-1.5 text-sm text-red-100">{terminal.error}</p>
          )}
        </div>
      )}

      {/* Package + safe-to-delete */}
      {isComplete && (
        <section
          aria-live="polite"
          className={[
            'rounded-lg border p-4',
            downloaded
              ? 'border-emerald-400/30 bg-emerald-400/10'
              : 'border-amber-500/40 bg-amber-500/10',
          ].join(' ')}
        >
          <p
            className={[
              'text-xs uppercase tracking-[0.2em]',
              downloaded ? 'text-emerald-200' : 'text-amber-300',
            ].join(' ')}
          >
            {downloaded ? 'Safe to delete this container' : 'One step left'}
          </p>
          <h2 className="mt-1 text-lg font-semibold text-text">
            {downloaded ? 'Your recovery package is saved.' : 'Download your recovery package.'}
          </h2>
          <p className="mt-1.5 text-sm text-muted">
            {downloaded
              ? 'Keep the package and your passphrase in separate places; together they are the only way to manage or recover this environment.'
              : 'One zip with the encrypted backup (checkpoint #3), the cluster CA certificate, and a README with the workstation reference. Your passphrase is NOT inside — store it separately.'}
          </p>

          <div className="mt-3 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={() => void downloadPackage()}
              disabled={pkgBusy}
              className="rounded-lg border border-accent/30 bg-accent px-4 py-2 text-sm font-semibold text-bg transition hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-60"
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

          {cp3 && (
            <p className="mt-2 text-xs text-muted">
              Backup inside: {cp3.artifact_name}
            </p>
          )}

          {pkgError && (
            <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 p-2.5 text-sm text-red-100">
              {pkgError}
            </div>
          )}
        </section>
      )}

      {/* Re-validate */}
      {isComplete && (
        <section className="rounded-lg border border-border bg-panel p-4">
          <p className="text-xs uppercase tracking-[0.2em] text-muted">Optional</p>
          <h3 className="mt-1 text-base font-semibold text-text">Re-validate the cluster</h3>
          <p className="mt-1 text-sm text-muted">
            Run a doctor check to verify cluster health after bootstrap.
          </p>
          <div className="mt-3">
            <button
              type="button"
              onClick={onRevalidate}
              className="rounded-lg border border-border bg-white/5 px-4 py-2 text-sm font-semibold text-text transition hover:bg-white/8"
            >
              Re-validate
            </button>
          </div>
        </section>
      )}
    </div>
  )
}
