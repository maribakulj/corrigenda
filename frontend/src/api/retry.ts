/**
 * Bounded retry-with-backoff (Audit-F27/F28).
 *
 * App's post-completion effects fetch /diff, /layout and /trace. Each
 * effect's guard depended on its own loading flag, so on a PERSISTENT
 * endpoint failure the loading true→false transition re-ran the effect,
 * whose guard was satisfied again — an unbounded request storm plus
 * continuous re-renders against a degraded backend.
 *
 * ``retryFetch`` bounds the attempts (default 3) with exponential
 * backoff and resolves to ``null`` after the last failure, so the caller
 * can settle into a stable error state instead of looping forever.
 */

export interface RetryOptions {
  attempts?: number
  baseDelayMs?: number
  /** Injectable sleep for deterministic tests. */
  sleep?: (ms: number) => Promise<void>
}

const defaultSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

export async function retryFetch<T>(
  fn: () => Promise<T>,
  { attempts = 3, baseDelayMs = 500, sleep = defaultSleep }: RetryOptions = {},
): Promise<T | null> {
  let lastError: unknown
  for (let attempt = 1; attempt <= attempts; attempt++) {
    try {
      return await fn()
    } catch (err) {
      lastError = err
      if (attempt < attempts) {
        await sleep(baseDelayMs * 2 ** (attempt - 1))
      }
    }
  }
  // Swallow the final error and signal exhaustion; the caller renders a
  // stable error state rather than re-triggering the effect.
  void lastError
  return null
}
