import type { PackageBundle } from '../shared/usePackageBundle'

type BootstrapCheckpoint = {
  n: number
  artifact_name: string
}

type FinishStepProps = {
  checkpoints: BootstrapCheckpoint[]
  terminal: { kind: 'complete'; runId: string; checkpoints: number[] } | { kind: 'error'; error?: string } | null
  onRevalidate: () => void
  bundle: PackageBundle
}

export function FinishStep({ checkpoints, terminal, onRevalidate, bundle }: FinishStepProps) {
  const isComplete = terminal?.kind === 'complete'
  const isError = terminal?.kind === 'error'
  // "Safe to delete" requires the saved bundle to be the LATEST backup — a
  // bundle downloaded earlier (e.g. pre-deploy) is stale once checkpoint #3 is
  // sealed and must not be presented as a complete recovery point (#140).
  const safe = bundle.downloaded && bundle.current
  const stale = bundle.downloaded && !bundle.current

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
            safe
              ? 'border-emerald-400/30 bg-emerald-400/10'
              : 'border-amber-500/40 bg-amber-500/10',
          ].join(' ')}
        >
          <p
            className={[
              'text-xs uppercase tracking-[0.2em]',
              safe ? 'text-emerald-200' : 'text-amber-300',
            ].join(' ')}
          >
            {safe ? 'Safe to delete this container' : 'One step left'}
          </p>
          <h2 className="mt-1 text-lg font-semibold text-text">
            {safe ? 'Your recovery package is saved.' : 'Download your recovery package.'}
          </h2>
          <p className="mt-1.5 text-sm text-muted">
            {safe
              ? 'Keep the package and your passphrase in separate places; together they are the only way to manage or recover this environment.'
              : stale
                ? 'You downloaded an earlier bundle — download again to capture the final verified backup (checkpoint #3) before deleting the container.'
                : 'One zip with the encrypted backup (checkpoint #3), the cluster CA certificate, and a README with the workstation reference. Your passphrase is NOT inside — store it separately.'}
          </p>

          <div className="mt-3 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={() => bundle.download()}
              disabled={bundle.busy}
              className="rounded-lg border border-accent/30 bg-accent px-4 py-2 text-sm font-semibold text-bg transition hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {bundle.busy ? 'Preparing…' : bundle.downloaded ? 'Download again' : 'Download package'}
            </button>
            {safe && bundle.downloadedAt ? (
              <span className="text-sm text-muted">
                ✓ downloaded {new Date(bundle.downloadedAt * 1000).toLocaleTimeString()}
              </span>
            ) : null}
          </div>

          {cp3 && (
            <p className="mt-2 text-xs text-muted">
              Backup inside: {cp3.artifact_name}
            </p>
          )}

          {bundle.error && (
            <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 p-2.5 text-sm text-red-100">
              {bundle.error}
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
