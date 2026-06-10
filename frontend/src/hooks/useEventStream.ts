import { useCallback, useEffect, useRef, useState } from 'react'
import { readNdjson } from '../ndjson'
import { describeFetchError } from '../shared/errors'

type LogEntry = { step: string; line: string }
type EventCallback = (event: Record<string, unknown>) => void

type UseEventStreamOptions = {
  runId: string | null
  onEvent: EventCallback
  onTerminal?: () => void
}

type UseEventStreamResult = {
  cursor: number
  logs: LogEntry[]
  reconnectNote: string | null
  streamError: string | null
}

async function readError(response: Response): Promise<string> {
  const text = await response.text()
  try {
    const payload = JSON.parse(text) as { detail?: unknown; error?: unknown }
    if (payload.detail !== undefined) return String(payload.detail)
    if (payload.error !== undefined) return String(payload.error)
    return text || JSON.stringify(payload)
  } catch {
    return text
  }
}

export function useEventStream({
  runId,
  onEvent,
  onTerminal,
}: UseEventStreamOptions): UseEventStreamResult {
  const [cursor, setCursor] = useState(0)
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [reconnectNote, setReconnectNote] = useState<string | null>(null)
  const [streamError, setStreamError] = useState<string | null>(null)

  const cursorRef = useRef(0)
  const onEventRef = useRef(onEvent)
  const onTerminalRef = useRef(onTerminal)

  useEffect(() => {
    onEventRef.current = onEvent
  }, [onEvent])

  useEffect(() => {
    onTerminalRef.current = onTerminal
  }, [onTerminal])

  // Reset when runId changes
  useEffect(() => {
    if (!runId) return
    cursorRef.current = 0
    setCursor(0)
    setLogs([])
    setReconnectNote(null)
    setStreamError(null)
  }, [runId])

  const pushLog = useCallback((step: string, line: string) => {
    setLogs((prev) => [...prev, { step, line }])
  }, [])

  // Main streaming effect
  useEffect(() => {
    if (!runId) return

    let cancelled = false
    const controller = new AbortController()

    async function waitForRetry(delayMs: number) {
      await new Promise<void>((resolve) => {
        const timer = window.setTimeout(resolve, delayMs)
        controller.signal.addEventListener(
          'abort',
          () => {
            window.clearTimeout(timer)
            resolve()
          },
          { once: true },
        )
      })
    }

    async function streamOnce(from: number): Promise<boolean> {
      const response = await fetch(
        `/api/bootstrap/stream/${runId}?from=${from}`,
        {
          method: 'GET',
          credentials: 'same-origin',
          headers: { accept: 'application/x-ndjson' },
          signal: controller.signal,
        },
      )

      if (!response.ok) {
        throw new Error(await readError(response))
      }

      setReconnectNote(null)
      let sawTerminal = false

      await readNdjson(response, (event) => {
        if (cancelled) return

        // Increment cursor ONLY after the event is applied
        const eventType = event.event as string | undefined

        // Handle log events specially for the local log buffer
        if (eventType === 'log') {
          const logEvent = event as { step?: string; line?: string }
          pushLog(logEvent.step ?? '', logEvent.line ?? '')
        }

        // Dispatch to the consumer's reducer
        onEventRef.current(event)

        // Now increment cursor (after event is applied)
        cursorRef.current += 1
        setCursor(cursorRef.current)

        if (eventType === 'error' || eventType === 'complete') {
          sawTerminal = true
          onTerminalRef.current?.()
        }
      })

      return sawTerminal
    }

    async function runStream() {
      let retries = 0
      while (!cancelled) {
        try {
          const sawTerminal = await streamOnce(cursorRef.current)
          if (cancelled || sawTerminal) return
          if (retries >= 5) {
            setStreamError('Stream disconnected before completion after 5 retries.')
            return
          }
          retries += 1
          setReconnectNote(`Reconnecting after drop (${retries}/5)…`)
          await waitForRetry(250 * retries)
        } catch (error) {
          if (cancelled || controller.signal.aborted) return
          if (retries >= 5) {
            setStreamError(describeFetchError(error))
            return
          }
          retries += 1
          setReconnectNote(`Reconnecting after error (${retries}/5)…`)
          await waitForRetry(250 * retries)
        }
      }
    }

    void runStream()

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [runId, pushLog])

  return { cursor, logs, reconnectNote, streamError }
}
