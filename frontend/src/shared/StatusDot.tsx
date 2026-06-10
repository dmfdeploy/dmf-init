type StatusState = 'pending' | 'running' | 'ok' | 'error'

const dotClassMap: Record<StatusState, string> = {
  running: 'bg-accent shadow-[0_0_0_4px_rgba(125,211,252,0.15)] animate-pulse',
  ok: 'bg-emerald-400 shadow-[0_0_0_4px_rgba(52,211,153,0.15)]',
  error: 'bg-red-400 shadow-[0_0_0_4px_rgba(248,113,113,0.15)]',
  pending: 'bg-slate-500/80',
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
        className="rounded-full border border-border/70 bg-white/5 px-2.5 py-0.5 text-[11px] uppercase tracking-[0.22em] text-muted"
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
