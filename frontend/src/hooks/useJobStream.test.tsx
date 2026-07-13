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
  it('logs hyphen_partner_missing as a visible warning', () => {
    const { result } = renderHook(() => useJobStream('job-1'))
    act(() => {
      FakeEventSource.last().dispatch('hyphen_partner_missing', {
        line_id: 'L7',
        message: 'partner not found',
      })
    })
    const msgs = result.current.logs.map((l) => l.message).join('\n')
    expect(msgs).toMatch(/hyphen|partner|L7/i)
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

// Regression: a genuine connection failure still fails after MAX_RETRIES.
describe('F30 regression — real connection loss still fails', () => {
  it('marks failed after repeated onerror without open', () => {
    const { result } = renderHook(() => useJobStream('job-1'))
    // Three drops WITHOUT a successful open each schedule a reconnect;
    // the fourth trips the MAX_RETRIES=3 ceiling → failed.
    for (let i = 0; i < 4; i++) {
      act(() => {
        FakeEventSource.last().error()
      })
      act(() => {
        vi.advanceTimersByTime(10000)
      })
    }
    expect(result.current.status).toBe('failed')
  })
})
