import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchJobStatus, withToken } from '../api/client'
import type {
  JobProgress,
  JobStats,
  JobStatus,
  LogEntry,
  LogType,
  SSEEventData,
  StreamState,
} from '../types'

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

/** Terminal job states — the stream/poller never restarts after these. */
function isTerminalStatus(s: JobStatus | null): boolean {
  return (
    s === 'completed' || s === 'completed_with_fallbacks' || s === 'failed' || s === 'cancelled'
  )
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
  // Plan V1.2 — transport state, separate from job status. 'polling'
  // means the live stream is gone and the job is followed via GET.
  streamState: StreamState
  // Terminal statistics from the STRUCTURED completed payload (SSE) or
  // the status snapshot (polling) — never parsed out of a log message.
  finalStats: JobStats | null
  // Manual attempt to re-open the SSE stream while polling.
  reconnect: () => void
}

export function useJobStream(jobId: string | null): UseJobStreamReturn {
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [progress, setProgress] = useState<JobProgress>(INITIAL_PROGRESS)
  const [status, setStatus] = useState<JobStatus | null>(null)
  const [streamState, setStreamState] = useState<StreamState>('idle')
  const [finalStats, setFinalStats] = useState<JobStats | null>(null)

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
  // Plan V1.2 — polling fallback timer + the manual-reconnect closure
  // (rebuilt on every effect run so it captures the current jobId).
  const pollTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const reconnectFnRef = useRef<(() => void) | null>(null)

  // Mirror status into a ref so the error handler can read the latest
  // value without triggering an effect re-run on every status change.
  useEffect(() => {
    statusRef.current = status
  }, [status])

  // Mirror progress for the polling path: the status snapshot carries
  // no hyphen count, so completion-via-poll reuses the streamed value.
  const progressRef = useRef<JobProgress>(INITIAL_PROGRESS)
  useEffect(() => {
    progressRef.current = progress
  }, [progress])

  useEffect(() => {
    if (!jobId) {
      // Reset all state when job is cleared (e.g. "New correction" clicked)
      setLogs([])
      setProgress(INITIAL_PROGRESS)
      setStatus(null)
      setStreamState('idle')
      setFinalStats(null)
      reconnectFnRef.current = null
      return
    }

    // Reset on new job
    setLogs([])
    setProgress(INITIAL_PROGRESS)
    setStatus('queued')
    setStreamState('live')
    setFinalStats(null)
    retryCountRef.current = 0
    capBackoffRef.current = false
    capAttemptRef.current = 0

    let cancelled = false
    const MAX_RETRIES = 3
    const CAP_BACKOFF_MAX_MS = 30_000
    const POLL_INTERVAL_MS = 5000
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
      'cancelled',
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
          // Plan V1.2 — keep the STRUCTURED terminal payload. The UI
          // used to regex-parse the log sentence below; any rewording
          // silently zeroed the displayed statistics.
          setFinalStats({
            lines_modified: linesModified,
            hyphen_pairs: hyphenPairs,
            duration_seconds: duration,
          })
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

        case 'cancelled':
          // Plan V2.2 — user-requested outcome, terminal like failed
          // but not an error.
          setStatus('cancelled')
          setLogs((l) => appendLog(l, makeLog('warning', 'Job cancelled on user request')))
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

    // Plan V1.2 — polling fallback. A lost SSE connection is a transport
    // problem, never a job outcome: the server task keeps running. When
    // reconnects are exhausted, follow the job via its authoritative
    // status endpoint instead of declaring it failed (which made the
    // download/diff/layout of a SUCCESSFUL job unreachable).
    function startPolling() {
      setStreamState('polling')
      let transportErrorLogged = false
      const poll = async () => {
        if (cancelled) return
        let snap: Awaited<ReturnType<typeof fetchJobStatus>> | undefined
        try {
          snap = await fetchJobStatus(jobId as string)
        } catch {
          // Transport failure — keep polling; log it once, not per tick.
          if (!transportErrorLogged) {
            transportErrorLogged = true
            setLogs((l) =>
              appendLog(l, makeLog('warning', 'Status poll failed — will keep retrying')),
            )
          }
        }
        if (cancelled) return
        if (snap === null) {
          // 404: evicted or unknown — the one authoritative dead end.
          setStatus('failed')
          setLogs((l) =>
            appendLog(l, makeLog('error', 'Job no longer exists on the server (evicted?)')),
          )
          return
        }
        if (snap !== undefined) {
          transportErrorLogged = false
          setStatus(snap.status)
          if (snap.status === 'completed' || snap.status === 'completed_with_fallbacks') {
            const stats: JobStats = {
              lines_modified: snap.lines_modified,
              // The snapshot has no hyphen count; reuse the last streamed value.
              hyphen_pairs: progressRef.current.hyphen_pairs_reconciled,
              duration_seconds: snap.duration_seconds ?? 0,
            }
            setFinalStats(stats)
            setProgress((p) => ({
              ...p,
              lines_done: snap.total_lines || p.lines_total || p.lines_done,
            }))
            setLogs((l) =>
              appendLog(
                l,
                makeLog(
                  'success',
                  `Completed — ${stats.lines_modified} line(s) modified, ${stats.hyphen_pairs} hyphen pair(s), ${stats.duration_seconds.toFixed(1)}s`,
                ),
              ),
            )
            if (snap.status === 'completed_with_fallbacks') {
              setLogs((l) =>
                appendLog(
                  l,
                  makeLog(
                    'warning',
                    `Degraded success — ${snap.fallbacks} line(s) fell back to their OCR source text.`,
                  ),
                ),
              )
            }
            return
          }
          if (snap.status === 'failed') {
            setLogs((l) =>
              appendLog(l, makeLog('error', `Failed: ${snap.error ?? 'unknown error'}`)),
            )
            return
          }
          if (snap.status === 'cancelled') {
            setLogs((l) => appendLog(l, makeLog('warning', 'Job cancelled on user request')))
            return
          }
        }
        pollTimeoutRef.current = setTimeout(() => {
          pollTimeoutRef.current = null
          void poll()
        }, POLL_INTERVAL_MS)
      }
      void poll()
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
        setStreamState('live')
      }
      es.onerror = () => {
        es.close()
        if (cancelled) return
        if (isTerminalStatus(statusRef.current)) return

        // Audit-F30 — a subscriber-cap close: reconnect with a
        // progressive backoff (2s → 30s), NEVER mark failed. Auto-
        // recovery is preserved (a slot may free); the growing delay
        // stops the 2s reconnect storm the old code produced.
        if (capBackoffRef.current) {
          capBackoffRef.current = false
          capAttemptRef.current += 1
          setStreamState('reconnecting')
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
          // Plan V1.2 — do NOT mark the job failed: the stream is gone,
          // the job isn't. Fall back to polling the authoritative status.
          setLogs((l) =>
            appendLog(
              l,
              makeLog(
                'warning',
                'Live stream lost after multiple retries — following the job via status polling',
              ),
            ),
          )
          startPolling()
          return
        }

        retryCountRef.current += 1
        setStreamState('reconnecting')
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

    // Plan V1.2 — manual reconnect (UI button while polling): stop the
    // poller, reset the retry budget and try a fresh EventSource. If it
    // fails again, onerror re-enters the retry→polling ladder.
    reconnectFnRef.current = () => {
      if (cancelled) return
      if (isTerminalStatus(statusRef.current)) return
      if (pollTimeoutRef.current !== null) {
        clearTimeout(pollTimeoutRef.current)
        pollTimeoutRef.current = null
      }
      retryCountRef.current = 0
      setStreamState('reconnecting')
      setLogs((l) => appendLog(l, makeLog('info', 'Reconnecting to the live stream…')))
      const fresh = new EventSource(withToken(`/api/jobs/${jobId}/events`))
      esRef.current = fresh
      attach(fresh)
    }

    return () => {
      cancelled = true
      if (retryTimeoutRef.current !== null) {
        clearTimeout(retryTimeoutRef.current)
        retryTimeoutRef.current = null
      }
      if (pollTimeoutRef.current !== null) {
        clearTimeout(pollTimeoutRef.current)
        pollTimeoutRef.current = null
      }
      reconnectFnRef.current = null
      esRef.current?.close()
      esRef.current = null
    }
  }, [jobId])

  const reconnect = useCallback(() => {
    reconnectFnRef.current?.()
  }, [])

  const isRunning =
    status === 'queued' ||
    status === 'started' ||
    status === 'running' ||
    // Plan V2.2 — the pipeline is still executing until its probe trips.
    status === 'cancel_requested'

  return { logs, progress, status, isRunning, streamState, finalStats, reconnect }
}
