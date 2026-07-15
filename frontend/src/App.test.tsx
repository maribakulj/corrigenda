/**
 * App orchestration — upload → configure → run → SSE completion → results.
 *
 * The api/client module is fully mocked (network) and EventSource is the
 * controllable fake; everything else (hooks, components) is real, so these
 * tests exercise the actual wiring between the pieces.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App'
import { FakeEventSource, installFakeEventSource } from './tests/fakeEventSource'
import type { DiffData, LayoutData, ModelInfo, TraceData } from './types'

vi.mock('./api/client', () => ({
  createJob: vi.fn(),
  fetchDiff: vi.fn(),
  fetchLayout: vi.fn(),
  fetchTrace: vi.fn(),
  listModels: vi.fn(),
  downloadJob: vi.fn(),
  cancelJob: vi.fn(),
  setJobToken: vi.fn(),
  setEventsUrl: vi.fn(),
  eventsUrlFor: (jobId: string) => `/api/jobs/${jobId}/events`,
  fetchJobStatus: vi.fn(),
}))

import { createJob, fetchDiff, fetchLayout, fetchTrace, listModels } from './api/client'

const mocked = {
  createJob: vi.mocked(createJob),
  fetchDiff: vi.mocked(fetchDiff),
  fetchLayout: vi.mocked(fetchLayout),
  fetchTrace: vi.mocked(fetchTrace),
  listModels: vi.mocked(listModels),
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const MODELS: ModelInfo[] = [
  { id: 'm-large', label: 'Large', supports_structured_output: true, context_window: 128000 },
]

const DIFF: DiffData = {
  job_id: 'job-1',
  pages: [
    {
      page_id: 'P1',
      page_index: 0,
      lines: [
        {
          line_id: 'L1',
          ocr_text: 'ancien texte',
          corrected_text: 'nouveau texte',
          modified: true,
          hyphen_role: 'none',
          hyphen_subs_content: null,
        },
      ],
    },
  ],
  stats: { total_lines: 10, modified_lines: 3, hyphen_pairs: 1 },
}

const LAYOUT: LayoutData = { job_id: 'job-1', pages: [] }

const TRACE: TraceData = {
  report_version: '1',
  run_id: 'job-1',
  total_lines: 1,
  lines: [
    {
      line_id: 'L1',
      page_id: 'P1',
      source_ocr_text: 'ancien texte',
      model_input_text: 'ancien texte',
      model_corrected_text: 'nouveau texte',
      projected_text: 'nouveau texte',
      output_alto_text: 'nouveau texte',
      hyphen_role: null,
      rewriter_path: 'fast_path',
      validation_status: 'corrected',
      fallback_reason: null,
    },
  ],
}

beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn()
})

beforeEach(() => {
  installFakeEventSource()
  mocked.createJob.mockReset().mockResolvedValue({ job_id: 'job-1', job_token: null })
  mocked.fetchDiff.mockReset().mockResolvedValue(DIFF)
  mocked.fetchLayout.mockReset().mockResolvedValue(LAYOUT)
  mocked.fetchTrace.mockReset().mockResolvedValue(TRACE)
  mocked.listModels.mockReset().mockResolvedValue(MODELS)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Fill upload + provider + key + model so the play button becomes enabled. */
async function configureJob(container: HTMLElement) {
  const file = new File(['<alto/>'], 'page.xml', { type: 'application/xml' })
  fireEvent.change(container.querySelector('input[type="file"]')!, {
    target: { files: [file] },
  })

  const [providerSelect] = screen.getAllByRole('combobox')
  fireEvent.change(providerSelect, { target: { value: 'anthropic' } })
  fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'sk-test' } })

  fireEvent.click(screen.getByRole('button', { name: /load models/i }))
  await screen.findByRole('option', { name: /Large/ })
  const modelSelect = screen.getAllByRole('combobox')[1]
  fireEvent.change(modelSelect, { target: { value: 'm-large' } })
}

/** Configure + start a job; returns the FakeEventSource of the SSE stream. */
async function startJob(container: HTMLElement): Promise<FakeEventSource> {
  await configureJob(container)
  fireEvent.click(screen.getByRole('button', { name: /start correction/i }))
  await screen.findByText('Progress')
  return FakeEventSource.last()
}

function completePayload(over: Record<string, unknown> = {}) {
  return {
    total_lines: 10,
    lines_modified: 3,
    hyphen_pairs_total: 1,
    duration_seconds: 2.5,
    ...over,
  }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('App — initial state', () => {
  it('renders the sections with the play button disabled', () => {
    render(<App />)
    expect(screen.getByText('Corrigenda')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /start correction/i })).toBeDisabled()
    expect(screen.queryByText('Progress')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /new correction/i })).not.toBeInTheDocument()
  })

  it('keeps the play button disabled until every prerequisite is set', async () => {
    const { container } = render(<App />)
    await configureJob(container)
    expect(screen.getByRole('button', { name: /start correction/i })).toBeEnabled()
  })
})

describe('App — happy path', () => {
  it('runs a job to completion and shows download, diff, layout and stats', async () => {
    const { container } = render(<App />)
    const es = await startJob(container)

    expect(mocked.createJob).toHaveBeenCalledWith(
      [expect.any(File)],
      'anthropic',
      'sk-test',
      'm-large',
    )

    act(() => {
      es.dispatch('document_parsed', { total_pages: 1, total_lines: 10, hyphen_pairs: 1 })
      es.dispatch('completed', completePayload())
    })

    // Terminal UI: download + diff + layout sections.
    await screen.findByRole('button', { name: /download corrected alto/i })
    await screen.findByText('Résultats de correction')
    await screen.findByText(/aucune mise en page/i)

    // Stats parsed back from the success log line.
    expect(screen.getByText('3')).toBeInTheDocument()
    expect(screen.getByText('2.5s')).toBeInTheDocument()

    // Event log rendered.
    expect(screen.getByText(/completed — 3 line\(s\) modified/i)).toBeInTheDocument()
  })

  it('supports the debug trace flow: toggle, select a line, close the panel', async () => {
    const { container } = render(<App />)
    const es = await startJob(container)
    act(() => {
      es.dispatch('completed', completePayload())
    })
    await screen.findByText('Résultats de correction')

    // Debug toggle appears only once done.
    fireEvent.click(screen.getByRole('button', { name: 'Debug' }))
    await screen.findByText(/click a line above to inspect its trace \(1 lines loaded\)/i)
    expect(mocked.fetchTrace).toHaveBeenCalledWith('job-1')

    // Clicking a diff row opens its trace panel.
    fireEvent.click(screen.getByText('L1'))
    await screen.findByText(/1\. Source OCR/)

    // Closing it returns to the hint.
    fireEvent.click(screen.getByRole('button', { name: /close trace panel/i }))
    expect(screen.queryByText(/1\. Source OCR/)).not.toBeInTheDocument()
    await screen.findByText(/click a line above/i)
  })

  it('resets everything with "New correction"', async () => {
    const { container } = render(<App />)
    const es = await startJob(container)
    act(() => {
      es.dispatch('completed', completePayload())
    })
    await screen.findByRole('button', { name: /download corrected alto/i })

    fireEvent.click(screen.getByRole('button', { name: /new correction/i }))

    await waitFor(() => {
      expect(screen.queryByText('Progress')).not.toBeInTheDocument()
    })
    expect(
      screen.queryByRole('button', { name: /download corrected alto/i }),
    ).not.toBeInTheDocument()
    expect(screen.queryByText('Résultats de correction')).not.toBeInTheDocument()
    // FileUpload remounted empty → play disabled again.
    expect(screen.getByRole('button', { name: /start correction/i })).toBeDisabled()
  })
})

describe('App — degraded and failure paths', () => {
  it('surfaces createJob errors under the play button', async () => {
    mocked.createJob.mockRejectedValue(new Error('quota exceeded'))
    const { container } = render(<App />)
    await configureJob(container)

    fireEvent.click(screen.getByRole('button', { name: /start correction/i }))

    await screen.findByText('quota exceeded')
    expect(screen.queryByText('Progress')).not.toBeInTheDocument()
  })

  it('shows FAILED without any download section when the job fails', async () => {
    const { container } = render(<App />)
    const es = await startJob(container)

    act(() => {
      es.dispatch('failed', { error: 'provider exploded' })
    })

    await screen.findByText('FAILED')
    expect(screen.getByText(/failed: provider exploded/i)).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /download corrected alto/i }),
    ).not.toBeInTheDocument()
    // Reset is still offered.
    expect(screen.getByRole('button', { name: /new correction/i })).toBeInTheDocument()
  })

  it('marks a degraded completion and still offers the download', async () => {
    const { container } = render(<App />)
    const es = await startJob(container)

    act(() => {
      es.dispatch(
        'completed',
        completePayload({ status: 'completed_with_fallbacks', fallbacks: 2 }),
      )
    })

    await screen.findByText('COMPLETED (WITH FALLBACKS)')
    expect(screen.getByText(/degraded success — 2 line\(s\)/i)).toBeInTheDocument()
    await screen.findByRole('button', { name: /download corrected alto/i })
  })

  it('renders a visible trace error and retries after a debug toggle', async () => {
    mocked.fetchTrace.mockRejectedValue(new Error('boom'))
    const { container } = render(<App />)
    const es = await startJob(container)
    act(() => {
      es.dispatch('completed', completePayload())
    })
    await screen.findByText('Résultats de correction')

    fireEvent.click(screen.getByRole('button', { name: 'Debug' }))

    // Wave-4 review — traceError was latched but NEVER rendered: the
    // spinner vanished and the debug feature died silently for the
    // whole session. A bounded failure must be visible…
    await screen.findByText(/impossible de charger les traces/i, {}, { timeout: 4000 })
    expect(mocked.fetchTrace).toHaveBeenCalledTimes(3)

    // …and toggling debug off/on is an explicit retry intent: the
    // latch must clear so the fetch runs again.
    fireEvent.click(screen.getByRole('button', { name: 'Debug' }))
    fireEvent.click(screen.getByRole('button', { name: 'Debug' }))
    await waitFor(() => expect(mocked.fetchTrace).toHaveBeenCalledTimes(6), {
      timeout: 4000,
    })
  })

  it('latches bounded diff/layout fetch failures into visible errors', async () => {
    mocked.fetchDiff.mockRejectedValue(new Error('boom'))
    mocked.fetchLayout.mockRejectedValue(new Error('boom'))
    const { container } = render(<App />)
    const es = await startJob(container)

    act(() => {
      es.dispatch('completed', completePayload())
    })

    // retryFetch: 3 attempts each (delays 500ms + 1000ms) then latch.
    await screen.findByText(/impossible de charger le diff/i, {}, { timeout: 4000 })
    await screen.findByText(/impossible de charger la mise en page/i, {}, { timeout: 4000 })

    // Audit-F27 — bounded: exactly 3 attempts, no infinite refetch loop.
    expect(mocked.fetchDiff).toHaveBeenCalledTimes(3)
    expect(mocked.fetchLayout).toHaveBeenCalledTimes(3)
  })
})
