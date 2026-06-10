import { StatusDotMinimal, type StatusState } from './StatusDot'

type StepDef = {
  key: string
  label: string
}

type StepProgressProps = {
  steps: StepDef[]
  activeKey: string
  stepStatuses: Record<string, string>
}

export function StepProgress({ steps, activeKey, stepStatuses }: StepProgressProps) {
  return (
    <nav aria-label="Progress steps" className="flex items-center gap-0">
      {steps.map((step, i) => {
        const isActive = step.key === activeKey
        const status = (stepStatuses[step.key] ?? (isActive ? 'running' : 'pending')) as StatusState
        const isLast = i === steps.length - 1
        return (
          <span key={step.key} className="flex items-center">
            <span
              className={[
                'flex items-center gap-2 px-3 py-1.5 text-sm transition',
                isActive ? 'text-text' : 'text-muted',
              ].join(' ')}
              aria-current={isActive ? 'step' : undefined}
            >
              <StatusDotMinimal status={status} />
              <span className="font-medium">{step.label}</span>
            </span>
            {!isLast && (
              <span
                className="h-px w-6 bg-border/60"
                aria-hidden="true"
              />
            )}
          </span>
        )
      })}
    </nav>
  )
}
