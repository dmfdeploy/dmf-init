import type { ReactNode } from 'react'
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

function dotColor(status: string): string {
  switch (status) {
    case 'ok': return 'bg-emerald-400'
    case 'running': return 'bg-accent animate-pulse motion-reduce:animate-none'
    case 'error': return 'bg-red-400'
    default: return 'bg-slate-600'
  }
}

function labelFor(status: string): string {
  switch (status) {
    case 'ok': return 'Done'
    case 'running': return 'Active'
    case 'error': return 'Error'
    default: return 'Pending'
  }
}

function StepRail({ steps, activeKey, statuses }: {
  steps: typeof CREATE_STEPS
  activeKey: string
  statuses: Record<string, string>
}) {
  return (
    <nav aria-label="Installation steps" className="flex flex-col gap-1 py-2">
      {steps.map((step) => {
        const isActive = step.key === activeKey
        const status = statuses[step.key] ?? (isActive ? 'running' : 'pending')
        return (
          <div
            key={step.key}
            className={[
              'flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition',
              isActive ? 'bg-accent/10 text-text' : 'text-muted',
            ].join(' ')}
            aria-current={isActive ? 'step' : undefined}
          >
            <span
              className={['h-2.5 w-2.5 shrink-0 rounded-full', dotColor(status)].join(' ')}
              aria-hidden="true"
            />
            <span className="font-medium">{step.label}</span>
            <span className="ml-auto text-[11px] uppercase tracking-[0.18em]" aria-hidden="true">
              {labelFor(status)}
            </span>
          </div>
        )
      })}
    </nav>
  )
}

function LogoIcon({ className }: { className?: string }) {
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

export function Shell({
  mode,
  onModeChange,
  createPhase,
  children,
  envId,
}: ShellProps) {
  const rail = createPhase ? railState(createPhase) : null

  return (
    <div className="flex h-screen flex-col bg-bg text-text">
      {/* Slim topbar */}
      <header className="flex h-14 shrink-0 items-center justify-between border-b border-border px-4">
        <div className="flex items-center gap-3">
          <LogoIcon className="h-7 w-auto" />
          <span className="text-sm font-semibold tracking-wide text-text">dmfdeploy <span className="text-muted font-normal">init</span></span>
        </div>
        <div className="flex items-center gap-3">
          {envId && <span className="hidden text-xs text-muted sm:inline">{envId}</span>}
          <div className="inline-flex rounded-lg border border-border bg-panel p-0.5 text-xs">
            <button
              type="button"
              onClick={() => onModeChange('create')}
              className={[
                'rounded-md px-3 py-1.5 transition',
                mode === 'create' ? 'bg-accent/15 text-accent font-semibold' : 'text-muted hover:text-text',
              ].join(' ')}
            >
              Create
            </button>
            <button
              type="button"
              onClick={() => onModeChange('manage')}
              className={[
                'rounded-md px-3 py-1.5 transition',
                mode === 'manage' ? 'bg-accent/15 text-accent font-semibold' : 'text-muted hover:text-text',
              ].join(' ')}
            >
              Manage
            </button>
          </div>
        </div>
      </header>

      {/* Body: left rail + content */}
      <div className="flex flex-1 overflow-hidden">
        {mode === 'create' && rail ? (
          <aside className="hidden w-56 shrink-0 flex-col border-r border-border bg-sidebar lg:flex">
            <StepRail steps={CREATE_STEPS} activeKey={rail.activeKey} statuses={rail.statuses} />
          </aside>
        ) : null}

        {/* Central content pane — scroll only internally where needed */}
        <main className="flex-1 overflow-y-auto p-4 lg:p-6">
          {children}
        </main>
      </div>
    </div>
  )
}
