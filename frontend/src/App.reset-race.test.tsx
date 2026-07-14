/**
 * Wave-4 adversarial-review follow-up — stale in-flight retryFetch
 * promises must not cross a reset (F27/F28 hole).
 *
 * retryFetch is mocked with hand-resolved deferreds so the tests control
 * exactly WHEN a (possibly stale) fetch settles relative to a reset:
 * - a stale failure must not latch the error flags of the NEXT job
 *   (phantom "Impossible de charger le diff" + blocked refetch);
 * - a stale success must not leak job-1's diff under job-2.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App'
import { FakeEventSource, installFakeEventSource } from './tests/fakeEventSource'
import type { DiffData, ModelInfo } from './types'

vi.mock('./api/client', () => ({
  createJob: vi.fn(),
  fetchDiff: vi.fn(),
  fetchLayout: vi.fn(),
  fetchTrace: vi.fn(),
  listModels: vi.fn(),
  downloadJob: vi.fn(),
  setJobToken: vi.fn(),
  withToken: (u: string) => u,
}))
vi.mock('./api/retry', () => ({
  retryFetch: vi.fn(),
}))

import { createJob, listModels } from './api/client'
import { retryFetch } from './api/retry'

const mockedRetry = vi.mocked(retryFetch)

interface Deferred {
  promise: Promise<unknown>
  resolve: (v: unknown) => void
}

function deferred(): Deferred {
  let resolve!: (v: unknown) => void
  const promise = new Promise<unknown>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

const MODELS: ModelInfo[] = [
  { id: 'm-large', label: 'Large', supports_structured_output: true, context_window: null },
]

function diffFor(jobId: string, text: string): DiffData {
  return {
    job_id: jobId,
    pages: [
      {
        page_id: 'P1',
        page_index: 0,
        lines: [
          {
            line_id: 'L1',
            ocr_text: text,
            corrected_text: text,
            modified: false,
            hyphen_role: 'none',
            hyphen_subs_content: null,
          },
        ],
      },
    ],
    stats: { total_lines: 1, modified_lines: 0, hyphen_pairs: 0 },
  }
}

let pending: Deferred[] = []

beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn()
})

beforeEach(() => {
  installFakeEventSource()
  pending = []
  mockedRetry.mockReset().mockImplementation(() => {
    const d = deferred()
    pending.push(d)
    return d.promise as Promise<never>
  })
  vi.mocked(listModels).mockReset().mockResolvedValue(MODELS)
  vi.mocked(createJob).mockReset()
})

async function configureAndStart(container: HTMLElement, jobId: string) {
  vi.mocked(createJob).mockResolvedValueOnce({ job_id: jobId, job_token: null })
  const file = new File(['<alto/>'], 'page.xml', { type: 'application/xml' })
  fireEvent.change(container.querySelector('input[type="file"]')!, {
    target: { files: [file] },
  })
  const [providerSelect] = screen.getAllByRole('combobox')
  fireEvent.change(providerSelect, { target: { value: 'anthropic' } })
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'sk-test' } })
  fireEvent.click(screen.getByRole('button', { name: /load models/i }))
  await screen.findByRole('option', { name: /Large/ })
  fireEvent.change(screen.getAllByRole('combobox')[1], { target: { value: 'm-large' } })
  fireEvent.click(screen.getByRole('button', { name: /start correction/i }))
  await screen.findByText('Progress')
  return FakeEventSource.last()
}

function completeJob(es: FakeEventSource) {
  act(() => {
    es.dispatch('completed', {
      total_lines: 1,
      lines_modified: 0,
      hyphen_pairs_total: 0,
      duration_seconds: 1,
    })
  })
}

describe('App — stale fetches must not cross a reset', () => {
  it('a stale failure settling after reset neither latches errors nor blocks the next job', async () => {
    const { container } = render(<App />)
    const es1 = await configureAndStart(container, 'job-1')
    completeJob(es1)

    // Job-1's diff + layout fetches are in flight (deferred).
    await waitFor(() => expect(mockedRetry).toHaveBeenCalledTimes(2))

    fireEvent.click(screen.getByRole('button', { name: /new correction/i }))

    // The stale attempts exhaust AFTER the reset (retryFetch → null).
    await act(async () => {
      pending[0].resolve(null)
      pending[1].resolve(null)
    })

    // No phantom error for a job whose fetch was never attempted.
    expect(screen.queryByText(/impossible de charger le diff/i)).not.toBeInTheDocument()

    const es2 = await configureAndStart(container, 'job-2')
    completeJob(es2)

    // Job-2 must fetch its own diff + layout (the stale null must not
    // have latched diffError/layoutError).
    await waitFor(() => expect(mockedRetry).toHaveBeenCalledTimes(4))

    await act(async () => {
      pending[2].resolve(diffFor('job-2', 'contenu du job 2'))
      pending[3].resolve(null)
    })
    expect(await screen.findAllByText('contenu du job 2')).not.toHaveLength(0)
    expect(screen.queryByText(/impossible de charger le diff/i)).not.toBeInTheDocument()
  })

  it('a stale success settling after reset does not leak job-1 data into job-2', async () => {
    const { container } = render(<App />)
    const es1 = await configureAndStart(container, 'job-1')
    completeJob(es1)
    await waitFor(() => expect(mockedRetry).toHaveBeenCalledTimes(2))

    fireEvent.click(screen.getByRole('button', { name: /new correction/i }))

    const es2 = await configureAndStart(container, 'job-2')
    completeJob(es2)
    await waitFor(() => expect(mockedRetry).toHaveBeenCalledTimes(4))

    // Job-1's SLOW diff finally lands — after the reset, during job-2.
    await act(async () => {
      pending[0].resolve(diffFor('job-1', 'JOB1-STALE-CONTENT'))
      pending[1].resolve(null)
    })
    expect(screen.queryByText('JOB1-STALE-CONTENT')).not.toBeInTheDocument()

    // Job-2's own diff renders normally.
    await act(async () => {
      pending[2].resolve(diffFor('job-2', 'contenu frais du job 2'))
      pending[3].resolve(null)
    })
    expect(await screen.findAllByText('contenu frais du job 2')).not.toHaveLength(0)
    expect(screen.queryByText('JOB1-STALE-CONTENT')).not.toBeInTheDocument()
  })
})
