/**
 * Audit-F wave 4 — useJobStream fixes (F25, F26, F30).
 * Each test pins a confirmed finding of docs/audit/AUDIT-2026-07-13.md.
 */
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { FakeEventSource, installFakeEventSource } from '../tests/fakeEventSource'
import { useJobStream } from './useJobStream'

beforeEach(() => {
  installFakeEventSource()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

// ---------------------------------------------------------------------------
// F25 — lines_done must use the per-chunk OWNED count (target_count),
// not line_count (which includes overlapping WINDOW context lines), and
// clamp to lines_total so the progress bar never exceeds 100%.
// ---------------------------------------------------------------------------

describe('F25 — progress uses target_count and clamps to total', () => {
  it('never overshoots lines_total when chunks carry context overlap', () => {
    const { result } = renderHook(() => useJobStream('job-1'))
    const es = FakeEventSource.last()

    act(() => {
      es.dispatch('document_parsed', {
        total_pages: 1,
        total_lines: 30,
        hyphen_pairs: 0,
      })
      // Two overlapping windows: line_count includes context (20 + 15 = 35),
      // but each chunk OWNS only 15 targets (target_count).
      es.dispatch('chunk_completed', {
        line_count: 20,
        target_count: 15,
        hyphen_pairs_reconciled: 0,
      })
      es.dispatch('chunk_completed', {
        line_count: 15,
        target_count: 15,
        hyphen_pairs_reconciled: 0,
      })
    })

    expect(result.current.progress.lines_done).toBe(30)
    expect(result.current.progress.lines_done).toBeLessThanOrEqual(
      result.current.progress.lines_total,
    )
  })

  it('falls back to line_count when target_count is absent', () => {
    const { result } = renderHook(() => useJobStream('job-1'))
    const es = FakeEventSource.last()
    act(() => {
      es.dispatch('document_parsed', { total_pages: 1, total_lines: 10, hyphen_pairs: 0 })
      es.dispatch('chunk_completed', { line_count: 4, hyphen_pairs_reconciled: 0 })
    })
    expect(result.current.progress.lines_done).toBe(4)
  })
})

// ---------------------------------------------------------------------------
// F26 — chunk_error and hyphen_partner_missing were subscribed but had no
// case (silently swallowed); a `default` case must log unknown events.
// ---------------------------------------------------------------------------

describe('F26 — diagnostic events surface in the log', () => {
  it('logs hyphen_partner_missing with the REAL backend payload fields', () => {
    const { result } = renderHook(() => useJobStream('job-1'))
    act(() => {
      // Wave-4 review — the backend emits {chunk_id, line_id,
      // missing_partner_id, direction}; the original test pinned a
      // fictional `message` field, hiding that the handler dropped the
      // useful information.
      FakeEventSource.last().dispatch('hyphen_partner_missing', {
        chunk_id: 'c1',
        line_id: 'L7',
        missing_partner_id: 'L8',
        direction: 'forward',
      })
    })
    const msgs = result.current.logs.map((l) => l.message).join('\n')
    expect(msgs).toMatch(/L7/)
    expect(msgs).toMatch(/L8/)
    expect(result.current.logs.some((l) => l.type === 'warning')).toBe(true)
  })

  it('logs chunk_error as a visible warning', () => {
    const { result } = renderHook(() => useJobStream('job-1'))
    act(() => {
      FakeEventSource.last().dispatch('chunk_error', {
        chunk_id: 'c1',
        message: 'boom',
      })
    })
    expect(result.current.logs.some((l) => /boom|chunk/i.test(l.message))).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// F30 — subscriber_cap_reached must back off progressively (2s → 30s),
// not reconnect every 2s forever, and must NOT mark the job failed.
// ---------------------------------------------------------------------------

describe('F30 — subscriber_cap_reached backs off, no failed', () => {
  it('does not set status=failed and increases the reconnect delay', () => {
    const { result } = renderHook(() => useJobStream('job-1'))
    const es = FakeEventSource.last()

    act(() => {
      es.open()
      es.dispatch('error', { reason: 'subscriber_cap_reached', message: 'cap' })
      es.error() // server closed the stream after the cap event
    })

    // A cap is a viewer-side limit, never a job failure.
    expect(result.current.status).not.toBe('failed')

    // First reconnect scheduled; capture how long until it fires.
    const before = FakeEventSource.instances.length
    act(() => {
      vi.advanceTimersByTime(2000)
    })
    // Reconnect happened (a new EventSource) — recovery preserved.
    expect(FakeEventSource.instances.length).toBeGreaterThan(before)

    // Second cap: the delay must be LONGER than the first (progressive).
    const es2 = FakeEventSource.last()
    act(() => {
      es2.open()
      es2.dispatch('error', { reason: 'subscriber_cap_reached', message: 'cap' })
      es2.error()
    })
    const before2 = FakeEventSource.instances.length
    act(() => {
      vi.advanceTimersByTime(2000)
    })
    // 2s is no longer enough for the 2nd reconnect (backoff grew).
    expect(FakeEventSource.instances.length).toBe(before2)
    act(() => {
      vi.advanceTimersByTime(30000)
    })
    expect(FakeEventSource.instances.length).toBeGreaterThan(before2)
    expect(result.current.status).not.toBe('failed')
  })
})

// ---------------------------------------------------------------------------
// Plan V1.2 — a lost SSE connection is a TRANSPORT failure, never a job
// outcome. Exhausted reconnects switch to polling GET /api/jobs/{id};
// only the server's answer (or a 404) may decide the job's fate.
// ---------------------------------------------------------------------------

/** Drive the stream through MAX_RETRIES consecutive failures → polling. */
function exhaustStreamRetries() {
  for (let i = 0; i < 4; i++) {
    act(() => {
      FakeEventSource.last().error()
    })
    act(() => {
      vi.advanceTimersByTime(10000)
    })
  }
}

function fetchResponding(payloads: Array<Record<string, unknown> | 404>) {
  let call = 0
  const fn = vi.fn(async () => {
    const p = payloads[Math.min(call, payloads.length - 1)]
    call += 1
    if (p === 404) return { ok: false, status: 404 } as Response
    return { ok: true, status: 200, json: async () => p } as unknown as Response
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

describe('V1.2 — stream loss falls back to polling, never fails the job', () => {
  it('reaches completed via polling when the job succeeded server-side', async () => {
    const fetchMock = fetchResponding([
      { job_id: 'job-1', status: 'running', total_lines: 10, lines_modified: 0 },
      {
        job_id: 'job-1',
        status: 'completed',
        total_lines: 10,
        lines_modified: 4,
        chunks_total: 2,
        retries: 0,
        fallbacks: 0,
        duration_seconds: 12.5,
        error: null,
      },
    ])
    const { result } = renderHook(() => useJobStream('job-1'))

    exhaustStreamRetries()
    expect(result.current.status).not.toBe('failed')
    expect(result.current.streamState).toBe('polling')

    // First poll fires immediately; second after the 5s interval.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5001)
    })

    expect(fetchMock).toHaveBeenCalled()
    expect(result.current.status).toBe('completed')
    // Structured stats from the snapshot — the download panel works.
    expect(result.current.finalStats).toEqual({
      lines_modified: 4,
      hyphen_pairs: 0,
      duration_seconds: 12.5,
    })
  })

  it('adopts the server verdict when the job really failed', async () => {
    fetchResponding([
      {
        job_id: 'job-1',
        status: 'failed',
        total_lines: 0,
        lines_modified: 0,
        chunks_total: 0,
        retries: 3,
        fallbacks: 0,
        duration_seconds: null,
        error: 'provider exploded',
      },
    ])
    const { result } = renderHook(() => useJobStream('job-1'))
    exhaustStreamRetries()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1)
    })
    expect(result.current.status).toBe('failed')
    expect(result.current.logs.some((l) => /provider exploded/.test(l.message))).toBe(true)
  })

  it('treats a 404 snapshot as the one authoritative dead end', async () => {
    fetchResponding([404])
    const { result } = renderHook(() => useJobStream('job-1'))
    exhaustStreamRetries()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1)
    })
    expect(result.current.status).toBe('failed')
  })

  it('keeps polling through transport errors instead of failing', async () => {
    const fn = vi.fn(async () => {
      throw new Error('network down')
    })
    vi.stubGlobal('fetch', fn)
    const { result } = renderHook(() => useJobStream('job-1'))
    exhaustStreamRetries()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000)
    })
    expect(fn.mock.calls.length).toBeGreaterThan(2)
    expect(result.current.status).not.toBe('failed')
    expect(result.current.streamState).toBe('polling')
  })

  it('reconnect() reopens a live stream from polling mode', async () => {
    fetchResponding([{ job_id: 'job-1', status: 'running', total_lines: 0, lines_modified: 0 }])
    const { result } = renderHook(() => useJobStream('job-1'))
    exhaustStreamRetries()
    expect(result.current.streamState).toBe('polling')

    const before = FakeEventSource.instances.length
    act(() => {
      result.current.reconnect()
    })
    expect(FakeEventSource.instances.length).toBe(before + 1)
    act(() => {
      FakeEventSource.last().open()
    })
    expect(result.current.streamState).toBe('live')
  })
})

describe('V1.2 — terminal stats are structured, not parsed from log text', () => {
  it('exposes finalStats from the SSE completed payload', () => {
    const { result } = renderHook(() => useJobStream('job-1'))
    act(() => {
      FakeEventSource.last().dispatch('completed', {
        total_lines: 20,
        lines_modified: 5,
        hyphen_pairs_total: 3,
        duration_seconds: 7.25,
      })
    })
    // Survives ANY rewording of the log sentence.
    expect(result.current.finalStats).toEqual({
      lines_modified: 5,
      hyphen_pairs: 3,
      duration_seconds: 7.25,
    })
  })
})

// ---------------------------------------------------------------------------
// Event lifecycle — every switch arm drives status/progress/logs
// ---------------------------------------------------------------------------

describe('event lifecycle', () => {
  it('walks queued → started → running → completed with progress totals', () => {
    const { result } = renderHook(() => useJobStream('job-1'))
    const es = FakeEventSource.last()

    expect(result.current.status).toBe('queued')

    act(() => {
      es.dispatch('queued', { job_id: 'job-1' })
      es.dispatch('started', { job_id: 'job-1' })
    })
    expect(result.current.status).toBe('started')
    expect(result.current.isRunning).toBe(true)

    act(() => {
      es.dispatch('document_parsed', { total_pages: 2, total_lines: 20, hyphen_pairs: 3 })
    })
    expect(result.current.status).toBe('running')
    expect(result.current.progress.pages_total).toBe(2)
    expect(result.current.progress.lines_total).toBe(20)
    expect(result.current.progress.hyphen_pairs_total).toBe(3)

    act(() => {
      es.dispatch('page_started', { page_id: 'P1', page_index: 0, line_count: 10 })
      es.dispatch('page_completed', { page_id: 'P1', page_index: 0, corrections: 4 })
    })
    expect(result.current.progress.pages_done).toBe(1)
    const msgs = result.current.logs.map((l) => l.message)
    expect(msgs.some((m) => /page 1 started/i.test(m))).toBe(true)
    expect(msgs.some((m) => /page 1 completed — 4 correction/i.test(m))).toBe(true)

    act(() => {
      es.dispatch('completed', {
        total_lines: 20,
        lines_modified: 5,
        hyphen_pairs_total: 3,
        duration_seconds: 7.25,
      })
    })
    expect(result.current.status).toBe('completed')
    expect(result.current.isRunning).toBe(false)
    expect(result.current.progress.lines_done).toBe(20)
    const success = result.current.logs.find((l) => l.type === 'success')
    expect(success?.message).toContain('5 line(s) modified')
    expect(success?.message).toContain('7.3s')
    // The stream is closed after a terminal event.
    expect(es.closed).toBe(true)
  })

  it('logs retry and warning events as warnings', () => {
    const { result } = renderHook(() => useJobStream('job-1'))
    act(() => {
      FakeEventSource.last().dispatch('retry', { chunk_id: 'c1', attempt: 2, error: 'timeout' })
      FakeEventSource.last().dispatch('warning', { message: 'quota is low' })
      // Wave-4 review — the REAL payload carries from/to_granularity
      // (the old handler read a nonexistent `granularity` field and
      // always printed the fallback).
      FakeEventSource.last().dispatch('chunk_downgraded', {
        chunk_id: 'c1',
        from_granularity: 'PAGE',
        to_granularity: 'BLOCK',
        line_count: 30,
        target_count: 30,
        budget_remaining: 4,
      })
    })
    const warnings = result.current.logs.filter((l) => l.type === 'warning')
    expect(warnings.map((w) => w.message)).toEqual([
      'Retry (attempt 2) — timeout',
      'quota is low',
      'Chunk downgraded (PAGE → BLOCK)',
    ])
  })

  it('adopts completed_with_fallbacks and logs the degraded-success warning', () => {
    const { result } = renderHook(() => useJobStream('job-1'))
    act(() => {
      FakeEventSource.last().dispatch('completed', {
        total_lines: 4,
        lines_modified: 1,
        hyphen_pairs_total: 0,
        duration_seconds: 1,
        status: 'completed_with_fallbacks',
        fallbacks: 3,
      })
    })
    expect(result.current.status).toBe('completed_with_fallbacks')
    expect(
      result.current.logs.some(
        (l) => l.type === 'warning' && /degraded success — 3 line\(s\)/i.test(l.message),
      ),
    ).toBe(true)
  })

  it('survives a synthetic terminal event with a partial payload', () => {
    const { result } = renderHook(() => useJobStream('job-1'))
    act(() => {
      // Late subscriber: the server replays only a bare terminal marker.
      FakeEventSource.last().dispatch('completed', {})
    })
    expect(result.current.status).toBe('completed')
    const success = result.current.logs.find((l) => l.type === 'success')
    // Defaulted fields — never `undefined.toFixed` crashes.
    expect(success?.message).toContain('0 line(s) modified')
    expect(success?.message).toContain('0.0s')
  })

  it('handles failed with an error log and closes the stream', () => {
    const { result } = renderHook(() => useJobStream('job-1'))
    const es = FakeEventSource.last()
    act(() => {
      es.dispatch('failed', { error: 'kaboom' })
    })
    expect(result.current.status).toBe('failed')
    expect(result.current.logs.some((l) => l.type === 'error' && /kaboom/.test(l.message))).toBe(
      true,
    )
    expect(es.closed).toBe(true)
  })

  it('treats job_not_found as terminal failure', () => {
    const { result } = renderHook(() => useJobStream('job-1'))
    const es = FakeEventSource.last()
    act(() => {
      es.dispatch('error', { reason: 'job_not_found', message: 'gone' })
    })
    expect(result.current.status).toBe('failed')
    expect(result.current.logs.some((l) => /stream error: gone/i.test(l.message))).toBe(true)
    expect(es.closed).toBe(true)
  })

  it('ignores a native error event without a reason payload', () => {
    const { result } = renderHook(() => useJobStream('job-1'))
    act(() => {
      FakeEventSource.last().dispatch('error', {})
    })
    expect(result.current.status).toBe('queued')
    expect(result.current.logs).toHaveLength(0)
  })

  it('keeps known high-frequency diagnostics OUT of the visible log', () => {
    // Wave-4 review — the default arm turned chunk_planned/chunk_started/
    // rewriter_stats/reconcile_stats (per-page and per-chunk events)
    // into hundreds of "Event: …" junk lines that competed with real
    // entries for the MAX_LOGS budget. They are intentionally silent,
    // like keepalive; the default arm stays for genuinely NEW backend
    // event names (which arrive when EVENTS grows before the switch).
    const { result } = renderHook(() => useJobStream('job-1'))
    act(() => {
      FakeEventSource.last().dispatch('rewriter_stats', { fast: 10 })
      FakeEventSource.last().dispatch('reconcile_stats', { pairs: 2 })
      FakeEventSource.last().dispatch('keepalive', {})
      FakeEventSource.last().dispatch('chunk_planned', { page_id: 'P1', chunk_count: 2 })
      FakeEventSource.last().dispatch('chunk_started', { chunk_id: 'c1', attempt: 1 })
    })
    expect(result.current.logs).toHaveLength(0)
  })

  it('tolerates malformed JSON payloads', () => {
    const { result } = renderHook(() => useJobStream('job-1'))
    const es = FakeEventSource.last()
    act(() => {
      // Bypass dispatch's JSON.stringify with a raw broken payload.
      for (const fn of (
        es as unknown as { listeners: Map<string, ((e: MessageEvent) => void)[]> }
      ).listeners.get('warning') ?? []) {
        fn({ data: '{not json' } as MessageEvent)
      }
    })
    // Falls back to {} → message undefined but no crash.
    expect(result.current.logs).toHaveLength(1)
  })

  it('does not reconnect when the stream errors after a terminal status', () => {
    const { result } = renderHook(() => useJobStream('job-1'))
    const es = FakeEventSource.last()
    act(() => {
      es.dispatch('completed', {})
    })
    const count = FakeEventSource.instances.length
    act(() => {
      es.error()
      vi.advanceTimersByTime(60_000)
    })
    expect(FakeEventSource.instances.length).toBe(count)
    expect(result.current.status).toBe('completed')
  })

  it('bounds the log to MAX_LOGS entries', () => {
    const { result } = renderHook(() => useJobStream('job-1'))
    const es = FakeEventSource.last()
    act(() => {
      for (let i = 0; i < 505; i++) {
        es.dispatch('warning', { message: `w${i}` })
      }
    })
    expect(result.current.logs).toHaveLength(500)
    // Oldest entries dropped, newest kept.
    expect(result.current.logs[result.current.logs.length - 1].message).toBe('w504')
    expect(result.current.logs[0].message).toBe('w5')
  })
})

// ---------------------------------------------------------------------------
// jobId lifecycle
// ---------------------------------------------------------------------------

describe('jobId lifecycle', () => {
  it('stays idle with no jobId and opens no stream', () => {
    const { result } = renderHook(() => useJobStream(null))
    expect(result.current.status).toBeNull()
    expect(result.current.isRunning).toBe(false)
    expect(FakeEventSource.instances).toHaveLength(0)
  })

  it('resets logs, progress and status when the job is cleared', () => {
    const { result, rerender } = renderHook(({ id }: { id: string | null }) => useJobStream(id), {
      initialProps: { id: 'job-1' as string | null },
    })
    act(() => {
      FakeEventSource.last().dispatch('document_parsed', {
        total_pages: 1,
        total_lines: 5,
        hyphen_pairs: 0,
      })
    })
    expect(result.current.progress.lines_total).toBe(5)

    rerender({ id: null })

    expect(result.current.status).toBeNull()
    expect(result.current.logs).toHaveLength(0)
    expect(result.current.progress.lines_total).toBe(0)
    // The old stream was closed by the effect cleanup.
    expect(FakeEventSource.last().closed).toBe(true)
  })

  it('closes the stream and cancels pending reconnects on unmount', () => {
    const { unmount } = renderHook(() => useJobStream('job-1'))
    act(() => {
      FakeEventSource.last().error() // schedules a reconnect
    })
    const count = FakeEventSource.instances.length
    unmount()
    act(() => {
      vi.advanceTimersByTime(60_000)
    })
    // The scheduled reconnect never fires after cleanup.
    expect(FakeEventSource.instances.length).toBe(count)
  })
})
