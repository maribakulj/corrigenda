/**
 * Audit-F29 — useModels must ignore out-of-order responses: when the
 * user switches provider mid-load, only the LAST request may write state
 * (a slow earlier request must not clobber it).
 */
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ModelInfo } from '../types'
import { useModels } from './useModels'

vi.mock('../api/client', () => ({
  listModels: vi.fn(),
}))

import { listModels } from '../api/client'

afterEach(() => {
  vi.clearAllMocks()
})

function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

const model = (id: string): ModelInfo => ({
  id,
  label: id,
  supports_structured_output: true,
  context_window: null,
})

describe('F29 — request staleness', () => {
  it('keeps the LAST request even if an earlier one resolves later', async () => {
    const a = deferred<ModelInfo[]>()
    const b = deferred<ModelInfo[]>()
    vi.mocked(listModels).mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise)

    const { result } = renderHook(() => useModels())

    act(() => {
      result.current.loadModels('openai', 'ka')
    })
    act(() => {
      result.current.loadModels('anthropic', 'kb')
    })

    // The fast (last) request resolves first…
    await act(async () => {
      b.resolve([model('claude-x')])
    })
    await waitFor(() => expect(result.current.models.map((m) => m.id)).toEqual(['claude-x']))

    // …then the slow (earlier) one resolves — it must NOT clobber state.
    await act(async () => {
      a.resolve([model('gpt-x')])
    })
    // Give the microtask queue a tick.
    await Promise.resolve()
    expect(result.current.models.map((m) => m.id)).toEqual(['claude-x'])
  })

  it('a stale error does not clobber the last request', async () => {
    const a = deferred<ModelInfo[]>()
    const b = deferred<ModelInfo[]>()
    vi.mocked(listModels).mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise)

    const { result } = renderHook(() => useModels())
    act(() => {
      result.current.loadModels('openai', 'ka')
    })
    act(() => {
      result.current.loadModels('anthropic', 'kb')
    })

    await act(async () => {
      b.resolve([model('claude-x')])
    })
    await waitFor(() => expect(result.current.models).toHaveLength(1))

    await act(async () => {
      a.resolve(Promise.reject(new Error('stale boom')) as unknown as ModelInfo[])
    })
    await Promise.resolve()
    expect(result.current.error).toBeNull()
    expect(result.current.models.map((m) => m.id)).toEqual(['claude-x'])
  })
})
