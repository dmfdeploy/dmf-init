type StatusState = 'pending' | 'running' | 'ok' | 'error'

const dotClassMap: Record<StatusState, string> = {
  running: 'bg-accent',
  ok: 'bg-emerald-400',
  error: 'bg-red-400',
  pending: 'bg-slate-600',
}

const labelMap: Record<StatusState, string> = {
  running: 'Running',
  ok: 'Done',
  error: 'Error',
  pending: 'Pending',
}

export function StatusDot({
  status,
  label,
}: {
  status: StatusState
  label?: string
}) {
  const displayLabel = label ?? labelMap[status]
  return (
    <span className="inline-flex items-center gap-2">
      <span
        className={['h-3 w-3 rounded-full', dotClassMap[status]].join(' ')}
        aria-hidden="true"
      />
      <span className="sr-only">{displayLabel}</span>
      <span
        className="rounded-md border border-border bg-panel px-2 py-0.5 text-[11px] uppercase tracking-[0.18em] text-muted"
        aria-hidden="true"
      >
        {displayLabel}
      </span>
    </span>
  )
}

export function StatusDotMinimal({ status }: { status: StatusState }) {
  return (
    <span
      className={['h-3 w-3 rounded-full', dotClassMap[status]].join(' ')}
      aria-hidden="true"
      title={labelMap[status]}
    />
  )
}

export type { StatusState }
