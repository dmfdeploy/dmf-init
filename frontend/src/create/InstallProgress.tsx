import { useMemo } from 'react'
import { StatusDot } from '../shared/StatusDot'
import { Disclosure } from '../shared/Disclosure'
import { LogConsole, type LogEntry } from '../shared/LogConsole'

type StepState = 'pending' | 'running' | 'ok' | 'error'

type BootstrapCheckpoint = {
  n: number
  artifact_name: string
}

type InstallProgressProps = {
  steps: string[]
  stepStatuses: Record<string, StepState>
  currentStep: string | null
  checkpoints: BootstrapCheckpoint[]
  logs: LogEntry[]
  cursor: number
  reconnectNote: string | null
  streamError: string | null
  terminal: { kind: 'complete' | 'error'; runId?: string; checkpoints?: number[]; step?: string; error?: string } | null
}

const stepOrder = [
  'pre-seed', 'checkpoint-2', 'unseal', 'seed-bao',
  'post-seed', 'configure',
  'ca-cert', 'hosts-map', 'passkey',
  'verify', 'checkpoint-3',
]

function stepDisplayName(step: string): string {
  const names: Record<string, string> = {
    'pre-seed': 'Pre-seed',
    'checkpoint-2': 'Checkpoint #2',
    'unseal': 'Unseal',
    'seed-bao': 'Seed Bao',
    'post-seed': 'Post-seed',
    'configure': 'Configure',
    'ca-cert': 'CA Cert',
    'hosts-map': 'Hosts Map',
    'passkey': 'Passkey',
    'verify': 'Verify',
    'checkpoint-3': 'Checkpoint #3',
  }
  return names[step] ?? step
}

export function InstallProgress({
  steps,
  stepStatuses,
  currentStep,
  checkpoints,
  logs,
  cursor,
  reconnectNote,
  streamError,
  terminal,
}: InstallProgressProps) {
  // Build ordered step list from known order + any extras from the stream
  const orderedSteps = useMemo(() => {
    const seen = new Set(steps)
    const known = stepOrder.filter((s) => seen.has(s))
    const extras = steps.filter((s) => !seen.has(s) || !stepOrder.includes(s))
    return [...known, ...extras.filter((s) => !known.includes(s))]
  }, [steps])

  // Checkpoints that are backup seals (#1, #2, #3) — render as quiet ticks
  const backupCheckpoints = checkpoints.filter((c) => c.n >= 2)

  return (
    <div className="grid gap-6">
      {/* Step rail */}
      <section className="rounded-[1.75rem] border border-border/70 bg-panel/80 p-5 shadow-glow backdrop-blur">
        <div className="mb-4 flex items-center justify-between gap-3">
          <div>
            <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Install progress</p>
            <p className="mt-1 text-sm text-muted">
              {currentStep ? `Current: ${stepDisplayName(currentStep)}` : 'Waiting for steps…'}
            </p>
          </div>
          {reconnectNote ? (
            <div className="rounded-full border border-border/70 bg-white/5 px-3 py-1 text-xs text-muted">
              {reconnectNote}
            </div>
          ) : null}
        </div>

        <div className="grid gap-3">
          {orderedSteps.length ? (
            orderedSteps.map((step) => {
              const status = stepStatuses[step] ?? 'pending'
              const isCurrent = currentStep === step
              // Checkpoint tick for #2
              const cp = checkpoints.find((c) => {
                if (step === 'checkpoint-2') return c.n === 2
                if (step === 'checkpoint-3') return c.n === 3
                return false
              })
              return (
                <div
                  key={step}
                  className={[
                    'rounded-2xl border p-4 transition',
                    isCurrent
                      ? 'border-accent/40 bg-accent/10'
                      : 'border-border/70 bg-black/20',
                  ].join(' ')}
                >
                  <div className="flex flex-wrap items-center gap-3">
                    <StatusDot status={status} />
                    <span className="text-sm font-medium text-text">
                      {stepDisplayName(step)}
                    </span>
                    {cp && (
                      <span className="rounded-full border border-accent/30 bg-accent/10 px-2.5 py-0.5 text-[11px] text-accentSoft">
                        ✓ backup #{cp.n} saved
                      </span>
                    )}
                  </div>
                </div>
              )
            })
          ) : (
            <div className="rounded-2xl border border-dashed border-border/70 bg-black/20 p-4 text-sm text-muted">
              Waiting for the run to stream its step list.
            </div>
          )}
        </div>

        {/* Backup checkpoint ticks */}
        {backupCheckpoints.length > 0 && (
          <div className="mt-4 flex flex-wrap gap-2">
            {backupCheckpoints.map((cp) => (
              <span
                key={cp.n}
                className="inline-flex items-center gap-2 rounded-full border border-accent/30 bg-accent/10 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.24em] text-accentSoft"
              >
                <span>✓ #{cp.n} sealed</span>
                <span className="normal-case tracking-normal text-muted">{cp.artifact_name}</span>
              </span>
            ))}
          </div>
        )}
      </section>

      {/* Terminal states */}
      {terminal?.kind === 'complete' && (
        <section className="rounded-3xl border border-emerald-400/30 bg-emerald-400/10 p-5">
          <p className="text-xs uppercase tracking-[0.34em] text-emerald-200">Complete</p>
          <h3 className="mt-2 text-2xl font-semibold text-text">Bootstrap verified.</h3>
          <p className="mt-3 text-sm leading-6 text-muted">
            {(terminal.checkpoints?.length ?? 0) > 0
              ? `${terminal.checkpoints!.map((c) => `#${c}`).join(' and ')} sealed. `
              : ''}
            Safe to delete the container once you have kept the sealed artifacts and passphrase
            record.
          </p>
          {terminal.runId && (
            <div className="mt-4 rounded-2xl border border-border/70 bg-white/5 p-4 text-sm text-muted">
              Run ID <span className="text-text">{terminal.runId}</span>
            </div>
          )}
        </section>
      )}

      {terminal?.kind === 'error' && (
        <section className="rounded-3xl border border-red-500/30 bg-red-500/10 p-5">
          <p className="text-xs uppercase tracking-[0.34em] text-red-200">Error</p>
          <h3 className="mt-2 text-2xl font-semibold text-text">Bootstrap stopped.</h3>
          <p className="mt-3 text-sm leading-6 text-red-100">
            {terminal.step ? `Step ${stepDisplayName(terminal.step)}: ` : ''}
            {terminal.error}
          </p>
          {/* Lift last useful log lines into visible content */}
          {logs.length > 0 && (
            <div className="mt-4">
              <p className="text-xs uppercase tracking-[0.28em] text-muted">Last log lines</p>
              <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded-2xl border border-border/70 bg-black/30 p-3 text-xs leading-6 text-text">
                {logs.slice(-10).map((e) => `${e.step}: ${e.line}`).join('\n')}
              </pre>
            </div>
          )}
        </section>
      )}

      {streamError && !terminal && (
        <section className="rounded-3xl border border-red-500/30 bg-red-500/10 p-5">
          <p className="text-xs uppercase tracking-[0.34em] text-red-200">Transport</p>
          <h3 className="mt-2 text-lg font-semibold text-text">Stream issue</h3>
          <p className="mt-3 text-sm leading-6 text-red-100">{streamError}</p>
        </section>
      )}

      {/* Collapsible log */}
      <Disclosure summary="Show details">
        <div className="rounded-[1.75rem] border border-border/70 bg-panel/80 p-5 shadow-glow backdrop-blur">
          <div className="mb-3 flex items-center justify-between">
            <div>
              <p className="text-xs uppercase tracking-[0.34em] text-accentSoft">Live log console</p>
              <p className="mt-1 text-sm text-muted">Server-redacted stream</p>
            </div>
            <div className="rounded-full border border-border/70 bg-white/5 px-3 py-1 text-xs text-muted">
              {cursor} events
            </div>
          </div>
          <LogConsole logs={logs} cursor={cursor} id="install-log-console" />
        </div>
      </Disclosure>
    </div>
  )
}
