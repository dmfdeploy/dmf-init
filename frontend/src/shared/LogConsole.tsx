import { useEffect, useRef } from 'react'

type LogEntry = {
  step: string
  line: string
}

export function LogConsole({
  logs,
  cursor: _cursor,
  id,
  maxHeight = '30rem',
}: {
  logs: LogEntry[]
  cursor: number
  id?: string
  maxHeight?: string
}) {
  const logBoxRef = useRef<HTMLPreElement>(null)

  useEffect(() => {
    if (!logs.length || !logBoxRef.current) return
    logBoxRef.current.scrollTop = logBoxRef.current.scrollHeight
  }, [logs])

  return (
    <pre
      id={id}
      ref={logBoxRef}
      style={{ maxHeight }}
      className="overflow-auto whitespace-pre-wrap rounded-3xl border border-border/70 bg-black/30 p-4 text-xs leading-6 text-text"
    >
      {logs.length
        ? logs.map((entry) => `${entry.step}: ${entry.line}`).join('\n')
        : 'Waiting for logs...'}
    </pre>
  )
}

export type { LogEntry }
