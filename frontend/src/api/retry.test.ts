/**
 * Audit-F27/F28 — retryFetch bounds attempts and stops (no storm).
 */
import { describe, expect, it, vi } from 'vitest'

import { retryFetch } from './retry'

const noSleep = () => Promise.resolve()

describe('retryFetch', () => {
  it('stops after the bounded number of attempts and returns null', async () => {
    const fn = vi.fn().mockRejectedValue(new Error('500'))
    const result = await retryFetch(fn, { attempts: 3, sleep: noSleep })
    expect(result).toBeNull()
    expect(fn).toHaveBeenCalledTimes(3) // NOT unbounded
  })

  it('returns the value on the first success', async () => {
    const fn = vi.fn().mockResolvedValue({ ok: true })
    const result = await retryFetch(fn, { attempts: 3, sleep: noSleep })
    expect(result).toEqual({ ok: true })
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('recovers on a later attempt', async () => {
    const fn = vi.fn().mockRejectedValueOnce(new Error('x')).mockResolvedValueOnce('ok')
    const result = await retryFetch(fn, { attempts: 3, sleep: noSleep })
    expect(result).toBe('ok')
    expect(fn).toHaveBeenCalledTimes(2)
  })
})
