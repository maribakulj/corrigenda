import { useEffect, useRef, useState } from 'react'
import type { JobProgress, JobStatus, LogEntry, LogType, SSEEventData } from '../types'

const MAX_LOGS = 500

let _logCounter = 0
function makeLog(type: LogType, message: string): LogEntry {
  return {
    id: String(++_logCounter),
    type,
    message,
    timestamp: new Date(),
  }
}

/** Append a log entry, keeping the array bounded to MAX_LOGS. */
function appendLog(prev: LogEntry[], entry: LogEntry): LogEntry[] {
  const next = [...prev, entry]
  return next.length > MAX_LOGS ? next.slice(next.length - MAX_LOGS) : next
}

const INITIAL_PROGRESS: JobProgress = {
  pages_total: 0,
  pages_done: 0,
  lines_total: 0,
  lines_done: 0,
  hyphen_pairs_total: 0,
  hyphen_pairs_reconciled: 0,
}

interface UseJobStreamReturn {
  logs: LogEntry[]
  progress: JobProgress
  status: JobStatus | null
  isRunning: boolean
}

export function useJobStream(jobId: string | null): UseJobStreamReturn {
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [progress, setProgress] = useState<JobProgress>(INITIAL_PROGRESS)
  const [status, setStatus] = useState<JobStatus | null>(null)

  // Refs survive StrictMode's double-effect: the OLD code kept retryCount
  // and the reconnect setTimeout in closures so a setStatus updater
  // (which StrictMode runs twice in dev) doubled-incremented the counter
  // and scheduled two reconnects per failure. Refs also let the cleanup
  // close whichever EventSource is current — original OR reconnect.
  const esRef = useRef<EventSource | null>(null)
  const retryTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const retryCountRef = useRef(0)
  const statusRef = useRef<JobStatus | null>(null)

  // Mirror status into a ref so the error handler can read the latest
  // value without triggering an effect re-run on every status change.
  useEffect(() => {
    statusRef.current = status
  }, [status])

  useEffect(() => {
    if (!jobId) {
      // Reset all state when job is cleared (e.g. "New correction" clicked)
      setLogs([])
      setProgress(INITIAL_PROGRESS)
      setStatus(null)
      return
    }

    // Reset on new job
    setLogs([])
    setProgress(INITIAL_PROGRESS)
    setStatus('queued')
    retryCountRef.current = 0

    let cancelled = false
    const MAX_RETRIES = 3
    const EVENTS = [
      'queued',
      'started',
      'document_parsed',
      'page_started',
      'chunk_planned',
      'chunk_started',
      'chunk_completed',
      'retry',
      'warning',
      'page_completed',
      'completed',
      'failed',
      'keepalive',
    ]

    function handleEvent(eventName: string, rawData: string) {
      let data: Record<string, unknown>
      try {
        data = JSON.parse(rawData)
      } catch {
        data = {}
      }

      const ev = { event: eventName, ...data } as SSEEventData

      switch (ev.event) {
        case 'queued':
          setStatus('queued')
          setLogs((l) => appendLog(l, makeLog('info', 'Job queued')))
          break

        case 'started':
          setStatus('started')
          setLogs((l) => appendLog(l, makeLog('info', 'Correction started')))
          break

        case 'document_parsed':
          setStatus('running')
          setProgress((p) => ({
            ...p,
            pages_total: ev.total_pages,
            lines_total: ev.total_lines,
            hyphen_pairs_total: ev.hyphen_pairs,
          }))
          setLogs((l) =>
            appendLog(
              l,
              makeLog(
                'info',
                `Document parsed — ${ev.total_pages} page(s), ${ev.total_lines} lines, ${ev.hyphen_pairs} hyphen pair(s)`,
              ),
            ),
          )
          break

        case 'page_started':
          setLogs((l) =>
            appendLog(
              l,
              makeLog('info', `Page ${ev.page_index + 1} started (${ev.line_count} lines)`),
            ),
          )
          break

        case 'chunk_completed':
          setProgress((p) => ({
            ...p,
            lines_done: p.lines_done + ev.line_count,
            hyphen_pairs_reconciled: p.hyphen_pairs_reconciled + ev.hyphen_pairs_reconciled,
          }))
          setLogs((l) =>
            appendLog(
              l,
              makeLog(
                'info',
                `Chunk done — ${ev.line_count} lines corrected${ev.hyphen_pairs_reconciled ? `, ${ev.hyphen_pairs_reconciled} hyphen pair(s)` : ''}`,
              ),
            ),
          )
          break

        case 'page_completed':
          setProgress((p) => ({ ...p, pages_done: p.pages_done + 1 }))
          setLogs((l) =>
            appendLog(
              l,
              makeLog(
                'info',
                `Page ${ev.page_index + 1} completed — ${ev.corrections} correction(s)`,
              ),
            ),
          )
          break

        case 'retry':
          setLogs((l) =>
            appendLog(l, makeLog('warning', `Retry (attempt ${ev.attempt}) — ${ev.error}`)),
          )
          break

        case 'warning':
          setLogs((l) => appendLog(l, makeLog('warning', ev.message)))
          break

        case 'completed':
          setStatus('completed')
          setProgress((p) => ({
            ...p,
            lines_done: ev.total_lines,
            hyphen_pairs_reconciled: ev.hyphen_pairs_total,
          }))
          setLogs((l) =>
            appendLog(
              l,
              makeLog(
                'success',
                `Completed — ${ev.lines_modified} line(s) modified, ${ev.hyphen_pairs_total} hyphen pair(s), ${ev.duration_seconds.toFixed(1)}s`,
              ),
            ),
          )
          esRef.current?.close()
          break

        case 'failed':
          setStatus('failed')
          setLogs((l) => appendLog(l, makeLog('error', `Failed: ${ev.error}`)))
          esRef.current?.close()
          break

        case 'keepalive':
          break
      }
    }

    function attach(es: EventSource) {
      for (const name of EVENTS) {
        es.addEventListener(name, (e: MessageEvent) => handleEvent(name, e.data))
      }
      es.onerror = () => {
        es.close()
        if (cancelled) return
        const s = statusRef.current
        if (s === 'completed' || s === 'failed') return

        if (retryCountRef.current >= MAX_RETRIES) {
          setLogs((l) =>
            appendLog(l, makeLog('error', 'Connection to server lost after multiple retries')),
          )
          setStatus('failed')
          return
        }

        retryCountRef.current += 1
        const attempt = retryCountRef.current
        const delay = attempt * 2000
        setLogs((l) =>
          appendLog(
            l,
            makeLog(
              'warning',
              `Connection lost — reconnecting in ${delay / 1000}s (attempt ${attempt}/${MAX_RETRIES})`,
            ),
          ),
        )

        retryTimeoutRef.current = setTimeout(() => {
          retryTimeoutRef.current = null
          if (cancelled) return
          const reconnect = new EventSource(`/api/jobs/${jobId}/events`)
          esRef.current = reconnect
          attach(reconnect)
        }, delay)
      }
    }

    const es = new EventSource(`/api/jobs/${jobId}/events`)
    esRef.current = es
    attach(es)

    return () => {
      cancelled = true
      if (retryTimeoutRef.current !== null) {
        clearTimeout(retryTimeoutRef.current)
        retryTimeoutRef.current = null
      }
      esRef.current?.close()
      esRef.current = null
    }
  }, [jobId])

  const isRunning = status === 'queued' || status === 'started' || status === 'running'

  return { logs, progress, status, isRunning }
}
