import { useEffect, useRef, useState } from 'react'
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

export function stepDisplayName(step: string): string {
  const names: Record<string, string> = {
    'pre-seed': 'Pre-seed',
    'checkpoint-2': 'Checkpoint #2',
    'unseal': 'Unseal',
    'seed-bao': 'Seed Bao',
    'post-seed': 'Post-seed',
    'configure': 'Configure',
    'workstation': 'Workstation prep',
    'passkey': 'Passkey',
    'verify': 'Verify',
    'checkpoint-3': 'Checkpoint #3',
  }
  return names[step] ?? step
}

// Elapsed time hook
function useElapsed(startTime: number | null): string {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!startTime) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [startTime])
  if (!startTime) return ''
  const diff = Math.floor((now - startTime) / 1000)
  const m = Math.floor(diff / 60)
  const s = diff % 60
  return `${m}m ${s.toString().padStart(2, '0')}s`
}

// Logo splash component
function LogoSplash({ className }: { className?: string }) {
  return (
    <svg className={className} xmlns="http://www.w3.org/2000/svg" viewBox="0 0 342 300" role="img" aria-label="dmfdeploy">
      <g transform="translate(46 42) scale(0.75)" fill="#7dd3fc">
        <ellipse cx="101" cy="91" rx="52" ry="39" transform="rotate(-17 101 91)"/>
        <ellipse cx="63" cy="52" rx="24" ry="8.5" transform="rotate(-24 63 52)"/>
        <ellipse cx="146" cy="30" rx="22" ry="8.5" transform="rotate(3 146 30)"/>
        <ellipse cx="190" cy="69" rx="22.5" ry="18" transform="rotate(45 190 69)"/>
        <ellipse cx="166" cy="139" rx="30" ry="18.5" transform="rotate(-35 166 139)"/>
        <ellipse cx="226" cy="139" rx="21" ry="14.5" transform="rotate(-31 226 139)"/>
        <ellipse cx="86" cy="158" rx="26.5" ry="14.5" transform="rotate(-5 86 158)"/>
        <ellipse cx="120" cy="198" rx="20" ry="11.5" transform="rotate(-6 120 198)"/>
        <ellipse cx="37" cy="126" rx="17.5" ry="12" transform="rotate(68 37 126)"/>
        <ellipse cx="26" cy="163" rx="12" ry="8" transform="rotate(67 26 163)"/>
        <ellipse cx="202" cy="27" rx="13.5" ry="8" transform="rotate(22 202 27)"/>
        <ellipse cx="121" cy="11" rx="11" ry="4.5" transform="rotate(-9 121 11)"/>
        <ellipse cx="14" cy="94" rx="9.5" ry="4.8" transform="rotate(116 14 94)"/>
        <ellipse cx="50" cy="39" rx="10" ry="3.8" transform="rotate(144 50 39)"/>
      </g>
    </svg>
  )
}

// Respect prefers-reduced-motion
function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
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
  const [installStart, setInstallStart] = useState<number | null>(null)
  const elapsed = useElapsed(installStart)

  // Track install start: when first step becomes running
  useEffect(() => {
    if (!installStart && currentStep) {
      setInstallStart(Date.now())
    }
  }, [currentStep, installStart])

  // Completed step count for honest progress
  const completedCount = Object.values(stepStatuses).filter((s) => s === 'ok').length
  const totalSteps = steps.length
  const progressPct = totalSteps > 0 ? Math.round((completedCount / totalSteps) * 100) : 0

  // Backup checkpoint ticks (shown under progress bar)
  const backupCheckpoints = checkpoints.filter((c) => c.n >= 2)

  // Latest meaningful activity line for the one-line ticker: skip blank lines
  // and Ansible's asterisk banners; strip trailing ***-runs from TASK/PLAY
  // headers so their useful part still shows. DOM updates are throttled to
  // 2/s — fast ansible bursts otherwise repaint the line many times a second,
  // which reads as flicker.
  const latestRef = useRef<string | null>(null)
  useEffect(() => {
    for (let i = logs.length - 1; i >= 0; i--) {
      const cleaned = logs[i].line.replace(/\*{3,}/g, ' ').replace(/\s+/g, ' ').trim()
      if (cleaned) {
        latestRef.current = cleaned
        return
      }
    }
  }, [logs])
  const [latestActivity, setLatestActivity] = useState<string | null>(null)
  useEffect(() => {
    const id = setInterval(() => {
      setLatestActivity((prev) => (latestRef.current !== prev ? latestRef.current : prev))
    }, 500)
    return () => clearInterval(id)
  }, [])

  // Preparing state: covers wizard render + backup + bootstrap start. The
  // ticker already streams the render output here, so the operator sees
  // activity from the first second after Deploy.
  if (!currentStep && !terminal) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4">
        <LogoSplash className={`h-16 w-auto ${prefersReducedMotion() ? '' : 'animate-breathe'}`} />
        <div className="text-center">
          <p className="text-xs uppercase tracking-[0.2em] text-muted">Preparing</p>
          <p className="mt-0.5 text-base font-medium text-text">Setting up the environment…</p>
        </div>
        <div className="h-5 w-full max-w-lg overflow-hidden text-center" aria-hidden="true">
          {latestActivity && (
            <p className="truncate font-mono text-xs text-muted">{latestActivity}</p>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col items-center gap-5">
      {/* Splash logo */}
      <LogoSplash className={`h-14 w-auto ${prefersReducedMotion() ? '' : 'animate-breathe'}`} />

      {/* Current step label */}
      <div className="text-center">
        <p className="text-xs uppercase tracking-[0.2em] text-muted">Installing</p>
        <p className="mt-0.5 text-base font-medium text-text">
          {currentStep ? stepDisplayName(currentStep) : 'Finalizing…'}
        </p>
        {elapsed && <p className="mt-0.5 text-xs text-muted">Elapsed: {elapsed}</p>}
      </div>

      {/* Honest progress bar */}
      <div className="w-full max-w-lg">
        <div className="mb-1 flex items-center justify-between text-xs text-muted">
          <span>step {completedCount} of {totalSteps}</span>
          <span>{progressPct}%</span>
        </div>
        <div className="h-2 overflow-hidden rounded-full bg-border">
          <div
            className="h-full rounded-full bg-accent transition-[width] duration-500"
            style={{ width: `${progressPct}%` }}
          />
        </div>
        {/* One-line activity ticker — fixed height slot, no reflow. Hidden
            from screen readers (changes every second); 'step N of M' above
            stays the accessible progress signal. */}
        <div className="mt-2 h-5 overflow-hidden text-center" aria-hidden="true">
          {latestActivity && (
            <p className="truncate font-mono text-xs text-muted">{latestActivity}</p>
          )}
        </div>
      </div>

      {/* Backup checkpoint ticks */}
      {backupCheckpoints.length > 0 && (
        <div className="flex flex-wrap items-center justify-center gap-2">
          {backupCheckpoints.map((cp) => (
            <span
              key={cp.n}
              className="inline-flex items-center gap-1.5 rounded-md border border-accent/30 bg-accent/10 px-2.5 py-1 text-[11px] font-medium text-accentSoft"
            >
              <span className="text-emerald-400">✓</span> backup #{cp.n} saved
            </span>
          ))}
        </div>
      )}

      {/* Reconnect note */}
      {reconnectNote && (
        <span className="rounded-md border border-border bg-panel px-2.5 py-1 text-xs text-muted">
          {reconnectNote}
        </span>
      )}

      {/* Step list lives in the left sidebar as sub-items of Install/Connect. */}

      {/* Log behind disclosure */}
      <Disclosure summary="Show details" defaultOpen={false}>
        <div className="w-full max-w-lg">
          <LogConsole logs={logs} cursor={cursor} id="install-log-console" maxHeight="16rem" />
        </div>
      </Disclosure>

      {/* Terminal: complete */}
      {terminal?.kind === 'complete' && (
        <div className="w-full max-w-lg rounded-lg border border-emerald-400/30 bg-emerald-400/10 p-4 text-center">
          <p className="text-xs uppercase tracking-[0.2em] text-emerald-200">Complete</p>
          <h3 className="mt-1 text-lg font-semibold text-text">Bootstrap verified.</h3>
          <p className="mt-1.5 text-sm text-muted">
            {(terminal.checkpoints?.length ?? 0) > 0
              ? `${terminal.checkpoints!.map((c) => `#${c}`).join(' and ')} sealed. `
              : ''}
            Safe to delete the container once you have kept the sealed artifacts and passphrase record.
          </p>
        </div>
      )}

      {/* Terminal: error */}
      {terminal?.kind === 'error' && (
        <div className="w-full max-w-lg rounded-lg border border-red-500/30 bg-red-500/10 p-4">
          <p className="text-xs uppercase tracking-[0.2em] text-red-200">Error</p>
          <h3 className="mt-1 text-lg font-semibold text-text">Bootstrap stopped.</h3>
          <p className="mt-1.5 text-sm text-red-100">
            {terminal.step ? `Step ${stepDisplayName(terminal.step)}: ` : ''}
            {terminal.error}
          </p>
          {/* Lift last useful log lines */}
          {logs.length > 0 && (
            <pre className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-bg/80 p-2 text-xs leading-5 text-text">
              {logs.slice(-10).map((e) => `${e.step}: ${e.line}`).join('\n')}
            </pre>
          )}
        </div>
      )}

      {/* Stream error (non-terminal) */}
      {streamError && !terminal && (
        <div className="w-full max-w-lg rounded-lg border border-red-500/30 bg-red-500/10 p-4">
          <p className="text-xs uppercase tracking-[0.2em] text-red-200">Transport</p>
          <h3 className="mt-1 text-base font-semibold text-text">Stream issue</h3>
          <p className="mt-1.5 text-sm text-red-100">{streamError}</p>
        </div>
      )}
    </div>
  )
}
