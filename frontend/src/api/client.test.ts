/**
 * api/client — token plumbing, fetch helpers, error mapping, download.
 *
 * The module holds the capability token in module state, so every test
 * resets it via setJobToken(null). fetch is stubbed per-test.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createJob,
  downloadJob,
  eventsUrlFor,
  fetchDiff,
  fetchLayout,
  fetchTrace,
  listModels,
  setEventsUrl,
  setJobToken,
} from './client'

function jsonResponse(
  body: unknown,
  init: { ok?: boolean; status?: number; statusText?: string } = {},
) {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    statusText: init.statusText ?? 'OK',
    json: () => Promise.resolve(body),
  } as Response
}

function brokenJsonResponse(init: { status: number; statusText: string }) {
  return {
    ok: false,
    status: init.status,
    statusText: init.statusText,
    json: () => Promise.reject(new Error('not json')),
  } as unknown as Response
}

const fetchMock = vi.fn()

beforeEach(() => {
  setJobToken(null)
  setEventsUrl(null)
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

// ---------------------------------------------------------------------------
// Token plumbing
// ---------------------------------------------------------------------------

describe('eventsUrlFor (Plan V2.4 — no capability token in URLs)', () => {
  it('falls back to the bare events route when no signed url is held', () => {
    expect(eventsUrlFor('j1')).toBe('/api/jobs/j1/events')
  })

  it('returns the signed events_url captured at creation', () => {
    setEventsUrl('/api/jobs/j1/events?sig=123.abc')
    expect(eventsUrlFor('j1')).toBe('/api/jobs/j1/events?sig=123.abc')
  })

  it('never embeds the capability token in any URL', () => {
    setJobToken('secret-token')
    expect(eventsUrlFor('j1')).not.toContain('secret-token')
  })
})

// ---------------------------------------------------------------------------
// listModels
// ---------------------------------------------------------------------------

describe('listModels', () => {
  it('POSTs provider + api_key and returns the models array', async () => {
    const models = [
      { id: 'm1', label: 'M1', supports_structured_output: true, context_window: 1000 },
    ]
    fetchMock.mockResolvedValue(jsonResponse({ models }))

    const out = await listModels('anthropic', 'sk-key')

    expect(out).toEqual(models)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/providers/models',
      expect.objectContaining({ method: 'POST' }),
    )
    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)
    expect(body).toEqual({ provider: 'anthropic', api_key: 'sk-key' })
  })

  it('throws the server detail on a non-ok response', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: 'invalid api key' }, { ok: false, status: 401 }),
    )
    await expect(listModels('openai', 'bad')).rejects.toThrow('invalid api key')
  })

  it('falls back to statusText when the error body is not JSON', async () => {
    fetchMock.mockResolvedValue(brokenJsonResponse({ status: 502, statusText: 'Bad Gateway' }))
    await expect(listModels('openai', 'k')).rejects.toThrow('Bad Gateway')
  })

  it('falls back to the generic message when the error body has no detail', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}, { ok: false, status: 500 }))
    await expect(listModels('openai', 'k')).rejects.toThrow('Failed to load models')
  })
})

// ---------------------------------------------------------------------------
// createJob
// ---------------------------------------------------------------------------

describe('createJob', () => {
  it('sends multipart form data and stores the token + signed events url', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        job_id: 'j9',
        job_token: 'tok-1',
        events_url: '/api/jobs/j9/events?sig=9.mac',
      }),
    )
    const file = new File(['<alto/>'], 'page.xml', { type: 'application/xml' })

    const res = await createJob([file], 'mistral', 'sk-m', 'small')

    expect(res.job_id).toBe('j9')
    const form = (fetchMock.mock.calls[0][1] as RequestInit).body as FormData
    expect(form.getAll('files')).toHaveLength(1)
    expect(form.get('provider')).toBe('mistral')
    expect(form.get('api_key')).toBe('sk-m')
    expect(form.get('model')).toBe('small')
    // Token captured for headers; signed url captured for EventSource.
    fetchMock.mockResolvedValue(jsonResponse({}))
    await fetchDiff('j9')
    expect(fetchMock).toHaveBeenLastCalledWith('/api/jobs/j9/diff', {
      headers: { 'X-Job-Token': 'tok-1' },
    })
    expect(eventsUrlFor('j9')).toBe('/api/jobs/j9/events?sig=9.mac')
  })

  it('clears the held token and events url when the server returns none', async () => {
    setJobToken('stale')
    setEventsUrl('/api/jobs/old/events?sig=1.a')
    fetchMock.mockResolvedValue(jsonResponse({ job_id: 'j9' }))
    await createJob([], 'openai', 'k', 'gpt')
    fetchMock.mockResolvedValue(jsonResponse({}))
    await fetchDiff('j9')
    expect(fetchMock).toHaveBeenLastCalledWith('/api/jobs/j9/diff', { headers: {} })
    expect(eventsUrlFor('j9')).toBe('/api/jobs/j9/events')
  })

  it('throws the server detail on failure', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'no files' }, { ok: false, status: 422 }))
    await expect(createJob([], 'openai', 'k', 'gpt')).rejects.toThrow('no files')
  })

  it('falls back to the generic message when the error body has no detail', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}, { ok: false, status: 500 }))
    await expect(createJob([], 'openai', 'k', 'gpt')).rejects.toThrow('Failed to create job')
  })
})

// ---------------------------------------------------------------------------
// fetchLayout / fetchDiff / fetchTrace (shared apiGet)
// ---------------------------------------------------------------------------

describe('apiGet-backed endpoints', () => {
  it.each([
    ['fetchLayout', fetchLayout, '/api/jobs/j1/layout'],
    ['fetchDiff', fetchDiff, '/api/jobs/j1/diff'],
    ['fetchTrace', fetchTrace, '/api/jobs/j1/trace'],
  ] as const)('%s GETs its endpoint and returns the payload', async (_name, fn, url) => {
    const payload = { job_id: 'j1' }
    fetchMock.mockResolvedValue(jsonResponse(payload))
    await expect(fn('j1')).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith(url, { headers: {} })
  })

  it('sends the X-Job-Token header when a token is held', async () => {
    setJobToken('tok-9')
    fetchMock.mockResolvedValue(jsonResponse({}))
    await fetchDiff('j1')
    expect(fetchMock).toHaveBeenCalledWith('/api/jobs/j1/diff', {
      headers: { 'X-Job-Token': 'tok-9' },
    })
  })

  it('throws the server detail on failure', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'job gone' }, { ok: false, status: 404 }))
    await expect(fetchTrace('j1')).rejects.toThrow('job gone')
  })

  it('falls back to the per-endpoint message when detail is missing', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}, { ok: false, status: 500 }))
    await expect(fetchLayout('j1')).rejects.toThrow('Failed to fetch layout')
  })
})

// ---------------------------------------------------------------------------
// downloadJob
// ---------------------------------------------------------------------------

describe('downloadJob (signed URL — the browser streams, the token stays in a header)', () => {
  it('mints the signed URL with the token in a HEADER, then navigates to it', async () => {
    setJobToken('dl-tok')
    fetchMock.mockResolvedValue(jsonResponse({ download_url: '/api/jobs/j7/download?sig=123.abc' }))
    const clicked: string[] = []
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicked.push(this.getAttribute('href') ?? '')
    })

    await downloadJob('j7')

    // The mint request carries the token as a header; the token itself
    // appears in NO URL. The download then streams browser-natively via
    // the short-lived ?sig= — never buffered through fetch().blob().
    expect(fetchMock).toHaveBeenCalledWith('/api/jobs/j7/download-url', {
      headers: { 'X-Job-Token': 'dl-tok' },
    })
    expect(clicked).toEqual(['/api/jobs/j7/download?sig=123.abc'])
    expect(clicked[0]).not.toContain('dl-tok')
    expect(document.querySelector('a[href*="download"]')).toBeNull()
    clickSpy.mockRestore()
  })

  it('throws the server detail on failure', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: 'not completed' }, { ok: false, status: 409 }),
    )
    await expect(downloadJob('j7')).rejects.toThrow('not completed')
  })
})
