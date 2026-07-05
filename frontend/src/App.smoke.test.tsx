/**
 * App-level smoke test.
 *
 * Catches the most basic failure modes:
 *  - module imports a non-existent symbol from a sibling module
 *  - top-level `useEffect` throws synchronously on mount
 *  - the header copy that anchors the visual layout disappears
 *
 * Deeper component tests live in `components/*.test.tsx`.
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

// The job-stream hook opens an EventSource against /api/jobs/:id/events
// as soon as a job_id is set. The smoke render passes `jobId=null` so
// the hook returns empty state without touching network — no mock needed.
// We do mock `fetch` defensively in case a sub-component tries an early
// fetch (e.g. fetchDiff/fetchLayout when the test renders a completed
// state).
vi.stubGlobal(
  'fetch',
  vi.fn(() =>
    Promise.resolve({
      ok: false,
      status: 404,
      json: () => Promise.resolve({}),
      text: () => Promise.resolve(''),
    }),
  ),
)

import App from './App'

describe('App (smoke)', () => {
  it('mounts without throwing and shows the header brand', () => {
    render(<App />)
    expect(screen.getByText('Corrigenda')).toBeInTheDocument()
    expect(screen.getByText(/Post-OCR correction via LLM/i)).toBeInTheDocument()
  })

  it('exposes the volatile-storage warning above the upload zone', () => {
    // Verifies the Stage 4.F notice survived — it's the user-facing
    // explanation for why a Space restart loses their job.
    render(<App />)
    expect(screen.getByRole('note')).toHaveTextContent(/pas persistants/i)
  })
})
