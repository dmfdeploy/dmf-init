import { useEffect, useState } from 'react'
import { ArtifactDownload, CaInstall, type CaCertPayload } from '../ui'

type BootstrapCheckpoint = {
  n: number
  artifact_name: string
}

type FinishStepProps = {
  checkpoints: BootstrapCheckpoint[]
  terminal: { kind: 'complete'; runId: string; checkpoints: number[] } | { kind: 'error'; error?: string } | null
  envId: string
  onRevalidate: () => void
}

export function FinishStep({ checkpoints, terminal, envId, onRevalidate }: FinishStepProps) {
  const [caPayload, setCaPayload] = useState<CaCertPayload | null>(null)
  const [caError, setCaError] = useState<string | null>(null)
  const [caRefreshing, setCaRefreshing] = useState(false)

  const fetchCa = () => {
    setCaRefreshing(true)
    setCaError(null)
    fetch(`/api/ca-cert/${encodeURIComponent(envId)}`, {
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

  useEffect(() => {
    fetchCa()
  }, [envId])

  const isComplete = terminal?.kind === 'complete'
  const isError = terminal?.kind === 'error'

  // Checkpoint #3 is the primary download; #1 is NOT on the artifact route
  const cp3 = checkpoints.find((c) => c.n === 3)

  return (
    <div className="grid gap-6">
      {/* Safe to delete — ONLY after complete (verify + checkpoint #3) */}
      {isComplete && (
        <section className="rounded-[1.75rem] border border-accent/30 bg-accent/10 p-5 shadow-glow">
          <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">
            Safe to delete this container
          </p>
          <h2 className="mt-2 text-2xl font-semibold text-text">Bootstrap complete.</h2>
          <p className="mt-3 text-sm leading-6 text-muted">
            Download the backup and keep the passphrase safe. The container itself can go away once
            you are satisfied with the backup.
          </p>

          {/* Primary download = checkpoint #3 */}
          {cp3 && (
            <div className="mt-4 grid gap-3 text-sm">
              <ArtifactDownload artifactName={cp3.artifact_name} label="Primary backup (checkpoint #3)" />
            </div>
          )}

          {/* Also list #2 if present */}
          {checkpoints.find((c) => c.n === 2) && (
            <div className="mt-2 grid gap-3 text-sm">
              {checkpoints
                .filter((c) => c.n === 2)
                .map((c) => (
                  <ArtifactDownload key={c.n} artifactName={c.artifact_name} label="Checkpoint #2" />
                ))}
            </div>
          )}
        </section>
      )}

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

      {/* CA Certificate — re-presented "for your records" (already done in Connect) */}
      <section className="rounded-[1.75rem] border border-border/70 bg-panel/80 p-5 shadow-glow backdrop-blur">
        <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">For your records</p>
        <h3 className="mt-2 text-xl font-semibold text-text">CA Certificate</h3>
        <p className="mt-2 text-sm leading-6 text-muted">
          Already installed during Connect. Kept here for reference.
        </p>

        {caPayload?.present ? (
          <div className="mt-4">
            <div className="flex flex-wrap items-center gap-3">
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
          </div>
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
      </section>

      {/* Enrollment summary */}
      <section className="rounded-[1.75rem] border border-border/70 bg-panel/80 p-5 shadow-glow backdrop-blur">
        <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">For your records</p>
        <h3 className="mt-2 text-xl font-semibold text-text">Passkey enrollment</h3>
        <p className="mt-2 text-sm leading-6 text-muted">
          Already completed during Connect. The operator identity has been enrolled with the
          passkey system.
        </p>
      </section>

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
