import type { ReactNode } from 'react'
import { StepProgress } from '../shared/StepProgress'
import type { CreatePhase } from '../hooks/useCreateFlow'

const CREATE_STEPS = [
  { key: 'configure', label: 'Configure' },
  { key: 'installing', label: 'Install' },
  { key: 'connect', label: 'Connect' },
  { key: 'finish', label: 'Finish' },
]

type ShellProps = {
  mode: 'create' | 'manage'
  onModeChange: (mode: 'create' | 'manage') => void
  createPhase?: CreatePhase
  children: ReactNode
  envId?: string | null
}

// Map the create-flow phase onto rail statuses. The rail keys are UI phases,
// not backend step ids, so statuses must be derived here — never from the
// bootstrap stepStatuses (different key space). During the verify tail the
// active dot returns to Install while Connect stays done.
function railState(phase: CreatePhase): {
  activeKey: string
  statuses: Record<string, string>
} {
  switch (phase) {
    case 'configure':
      return { activeKey: 'configure', statuses: { configure: 'running' } }
    case 'installing':
      return {
        activeKey: 'installing',
        statuses: { configure: 'ok', installing: 'running' },
      }
    case 'connect':
      return {
        activeKey: 'connect',
        statuses: { configure: 'ok', installing: 'ok', connect: 'running' },
      }
    case 'verifying':
      return {
        activeKey: 'installing',
        statuses: { configure: 'ok', installing: 'running', connect: 'ok' },
      }
    case 'finish':
    case 'validating':
      return {
        activeKey: 'finish',
        statuses: { configure: 'ok', installing: 'ok', connect: 'ok', finish: 'ok' },
      }
  }
}

export function Shell({
  mode,
  onModeChange,
  createPhase,
  children,
  envId,
}: ShellProps) {
  const modeDescription =
    mode === 'create'
      ? 'Set up a new environment: enter your details, run the install, connect your workstation, then download your recovery package.'
      : 'Restore an existing environment from a backup to run checks, upgrades, or teardown.'

  return (
    <main className="min-h-screen bg-[radial-gradient(circle_at_top_left,rgba(14,165,233,0.18),transparent_24%),radial-gradient(circle_at_top_right,rgba(45,212,191,0.12),transparent_22%),linear-gradient(180deg,#050816_0%,#081122_45%,#050816_100%)] px-4 py-6 text-text sm:px-6 lg:px-8">
      <div className="mx-auto flex min-h-[calc(100vh-3rem)] w-full max-w-7xl flex-col overflow-hidden rounded-[2rem] border border-border/60 bg-bg/70 shadow-glow backdrop-blur-xl">
        {/* Header */}
        <header className="border-b border-border/60 px-6 py-5 sm:px-8">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
            <div>
              <p className="text-xs uppercase tracking-[0.35em] text-accentSoft">DMF Init</p>
              <h1 className="mt-2 text-3xl font-semibold tracking-tight sm:text-4xl">
                {mode === 'create' ? 'Create new or manage an existing env.' : 'Manage an existing env.'}
              </h1>
            </div>
            <div className="flex flex-col items-stretch gap-3 sm:items-end">
              {/* Mode toggle */}
              <div className="inline-flex rounded-full border border-border/70 bg-white/5 p-1 text-sm text-muted">
                <button
                  type="button"
                  onClick={() => onModeChange('create')}
                  className={[
                    'rounded-full px-4 py-2 transition',
                    mode === 'create' ? 'bg-accent text-bg shadow-glow' : 'text-muted hover:text-text',
                  ].join(' ')}
                >
                  Create new
                </button>
                <button
                  type="button"
                  onClick={() => onModeChange('manage')}
                  className={[
                    'rounded-full px-4 py-2 transition',
                    mode === 'manage' ? 'bg-accent text-bg shadow-glow' : 'text-muted hover:text-text',
                  ].join(' ')}
                >
                  Manage
                </button>
              </div>
              {/* Status pill */}
              <div className="rounded-full border border-border/70 bg-white/5 px-4 py-2 text-sm text-muted">
                <span className="text-text">
                  {createPhase ? createPhase.charAt(0).toUpperCase() + createPhase.slice(1) : 'Idle'}
                </span>
                {envId ? <span className="ml-2">· {envId}</span> : null}
              </div>
            </div>
          </div>
          <p className="mt-4 max-w-3xl text-sm leading-6 text-muted">{modeDescription}</p>
        </header>

        {/* StepProgress rail — persistent across all create phases */}
        {mode === 'create' && createPhase && (
          <div className="border-b border-border/40 px-6 py-3 sm:px-8">
            <StepProgress
              steps={CREATE_STEPS}
              activeKey={railState(createPhase).activeKey}
              stepStatuses={railState(createPhase).statuses}
            />
          </div>
        )}

        {/* Content */}
        <div className="flex-1 px-6 py-6 lg:px-8">{children}</div>
      </div>
    </main>
  )
}
