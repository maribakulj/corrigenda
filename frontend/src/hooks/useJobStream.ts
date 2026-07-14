import { useEffect, useRef, useState } from 'react'
import { withToken } from '../api/client'
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
  // Audit-F30 — set when the current stream closed on a
  // subscriber_cap_reached; drives a progressive (2s→30s) reconnect
  // backoff that never marks the job failed. capAttemptRef persists
  // across cap cycles (onopen must NOT reset it — the cap stream opens
  // 200 before erroring, which is exactly why a per-open reset churned
  // at 2s forever).
  const capBackoffRef = useRef(false)
  const capAttemptRef = useRef(0)

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
    capBackoffRef.current = false
    capAttemptRef.current = 0

    let cancelled = false
    const MAX_RETRIES = 3
    const CAP_BACKOFF_MAX_MS = 30_000
    // Mirror of the backend's emit sites. Synchronisation is enforced
    // by backend/tests/test_sse_event_contract.py — any drift fails CI.
    const EVENTS = [
      'queued',
      'started',
      'document_parsed',
      'page_started',
      'chunk_planned',
      'chunk_started',
      'chunk_completed',
      'chunk_error',
      'chunk_downgraded',
      'hyphen_partner_missing',
      'retry',
      'warning',
      'page_completed',
      'completed',
      'failed',
      'keepalive',
      'error',
      'rewriter_stats',
      'reconcile_stats',
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
          setProgress((p) => {
            // Audit-F25 — count only the lines this chunk OWNS
            // (target_count), not line_count which includes overlapping
            // WINDOW context lines; falling back to line_count for older
            // backends. Clamp to lines_total so the bar never exceeds 100%.
            const owned = ev.target_count ?? ev.line_count
            const nextDone = p.lines_done + owned
            return {
              ...p,
              lines_done: p.lines_total > 0 ? Math.min(nextDone, p.lines_total) : nextDone,
              hyphen_pairs_reconciled: p.hyphen_pairs_reconciled + ev.hyphen_pairs_reconciled,
            }
          })
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

        case 'completed': {
          // P0-1 — the server distinguishes clean success from degraded
          // success (some lines kept their OCR text); adopt its status.
          const terminal = ev.status ?? 'completed'
          setStatus(terminal)
          // Audit P1 — a synthetic terminal event (emitted when a client
          // subscribes AFTER the job already finished: fast job, reload,
          // late reconnect) carries only a partial payload. Default every
          // field so a missing duration_seconds/total_lines can never
          // throw (`undefined.toFixed` used to crash the whole UI into
          // the ErrorBoundary for a job that actually succeeded).
          const totalLines = ev.total_lines ?? 0
          const linesModified = ev.lines_modified ?? 0
          const hyphenPairs = ev.hyphen_pairs_total ?? 0
          const duration = ev.duration_seconds ?? 0
          setProgress((p) => ({
            ...p,
            lines_done: totalLines,
            hyphen_pairs_reconciled: hyphenPairs,
          }))
          setLogs((l) =>
            appendLog(
              l,
              makeLog(
                'success',
                `Completed — ${linesModified} line(s) modified, ${hyphenPairs} hyphen pair(s), ${duration.toFixed(1)}s`,
              ),
            ),
          )
          if (terminal === 'completed_with_fallbacks') {
            const n = ev.fallbacks ?? 0
            setLogs((l) =>
              appendLog(
                l,
                makeLog(
                  'warning',
                  `Degraded success — ${n} line(s) fell back to their OCR source text (provider output rejected). Review them in the trace panel.`,
                ),
              ),
            )
          }
          esRef.current?.close()
          break
        }

        case 'failed':
          setStatus('failed')
          setLogs((l) => appendLog(l, makeLog('error', `Failed: ${ev.error}`)))
          esRef.current?.close()
          break

        case 'keepalive':
          break

        // Audit-F26 — these diagnostic events were subscribed (attach())
        // but had no case, so they were silently swallowed. Surface them.
        case 'chunk_error':
          setLogs((l) =>
            appendLog(l, makeLog('warning', `Chunk error: ${ev.message ?? ev.chunk_id ?? ''}`)),
          )
          break

        case 'chunk_downgraded':
          // Wave-4 review — the backend emits from_granularity /
          // to_granularity (never `granularity`): the old read always
          // printed the fallback and dropped the actual information.
          setLogs((l) =>
            appendLog(
              l,
              makeLog(
                'warning',
                `Chunk downgraded (${ev.from_granularity ?? '?'} → ${ev.to_granularity ?? 'finer granularity'})`,
              ),
            ),
          )
          break

        case 'hyphen_partner_missing':
          // Wave-4 review — surface the fields the backend actually
          // sends (missing_partner_id / direction), not a fictional
          // `message`.
          setLogs((l) =>
            appendLog(
              l,
              makeLog(
                'warning',
                `Hyphen partner missing for ${ev.line_id ?? 'a line'}${
                  ev.missing_partner_id ? ` (partner ${ev.missing_partner_id} not in chunk)` : ''
                } — reverted to OCR`,
              ),
            ),
          )
          break

        // Wave-4 review — known high-frequency diagnostics (per page /
        // per chunk / per file): deliberately silent like keepalive,
        // or they flood the 500-entry log with junk and evict real
        // entries. The default arm below stays for genuinely NEW
        // backend event names.
        case 'chunk_planned':
        case 'chunk_started':
        case 'rewriter_stats':
        case 'reconcile_stats':
          break

        case 'error':
          // Audit P3 — server-sent SSE error events (job_not_found /
          // subscriber_cap_reached) used to fall through the switch
          // silently. Native EventSource connection errors also reach
          // this listener but carry no parsed data, so discriminate on a
          // present `reason`. job_not_found is terminal (gone or wrong
          // token); a cap is a viewer-side limit, not a job failure.
          if (typeof ev.reason === 'string') {
            setLogs((l) =>
              appendLog(l, makeLog('error', `Stream error: ${ev.message ?? ev.reason}`)),
            )
            if (ev.reason === 'job_not_found') {
              setStatus('failed')
              esRef.current?.close()
            } else if (ev.reason === 'subscriber_cap_reached') {
              // Audit-F30 — the server yields this over a 200 OK stream
              // then closes it. Auto-recovery is preserved (a slot may
              // free), but reconnecting every 2 s forever is a churn
              // storm. Flag it so onerror applies a progressive backoff.
              capBackoffRef.current = true
            }
          }
          break

        default:
          // Audit-F26 — any backend event the switch doesn't model is
          // logged (visible) instead of vanishing; keepalives are the one
          // high-frequency event we intentionally ignore above.
          setLogs((l) => appendLog(l, makeLog('info', `Event: ${eventName}`)))
          break
      }
    }

    function attach(es: EventSource) {
      for (const name of EVENTS) {
        es.addEventListener(name, (e: MessageEvent) => handleEvent(name, e.data))
      }
      // Audit P2 — reset the retry counter on a successful (re)connection
      // so intermittent drops over a long job don't accumulate toward a
      // spurious 'failed' (three drops hours apart used to trip MAX_RETRIES
      // even though every reconnection succeeded).
      es.onopen = () => {
        retryCountRef.current = 0
      }
      es.onerror = () => {
        es.close()
        if (cancelled) return
        const s = statusRef.current
        if (s === 'completed' || s === 'completed_with_fallbacks' || s === 'failed') return

        // Audit-F30 — a subscriber-cap close: reconnect with a
        // progressive backoff (2s → 30s), NEVER mark failed. Auto-
        // recovery is preserved (a slot may free); the growing delay
        // stops the 2s reconnect storm the old code produced.
        if (capBackoffRef.current) {
          capBackoffRef.current = false
          capAttemptRef.current += 1
          const delay = Math.min(2000 * 2 ** (capAttemptRef.current - 1), CAP_BACKOFF_MAX_MS)
          setLogs((l) =>
            appendLog(
              l,
              makeLog(
                'warning',
                `Viewer limit reached — retrying in ${delay / 1000}s (auto-recovers when a slot frees)`,
              ),
            ),
          )
          retryTimeoutRef.current = setTimeout(() => {
            retryTimeoutRef.current = null
            if (cancelled) return
            const reconnect = new EventSource(withToken(`/api/jobs/${jobId}/events`))
            esRef.current = reconnect
            attach(reconnect)
          }, delay)
          return
        }

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
          const reconnect = new EventSource(withToken(`/api/jobs/${jobId}/events`))
          esRef.current = reconnect
          attach(reconnect)
        }, delay)
      }
    }

    const es = new EventSource(withToken(`/api/jobs/${jobId}/events`))
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
