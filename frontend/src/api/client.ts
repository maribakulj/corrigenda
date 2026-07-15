import type { DiffData, JobStatusData, LayoutData, ModelInfo, Provider, TraceData } from '../types'

// proxied via vite → http://localhost:8000
const BASE = ''

// ---------------------------------------------------------------------------
// P1-7 — capability token. Returned ONCE by POST /api/jobs; required by
// every job endpoint afterwards, ALWAYS as the X-Job-Token header.
// Plan V2.4 — the token never travels in a URL any more (query strings
// leak into proxy/ingress logs): EventSource uses the events-scoped
// signed events_url minted at creation, and <img> URLs carry an
// images-scoped ?sig= appended by the layout endpoint. Held in module
// state (the app drives one job at a time) and never persisted.
// ---------------------------------------------------------------------------

let currentJobToken: string | null = null
let currentEventsUrl: string | null = null

export function setJobToken(token: string | null): void {
  currentJobToken = token
}

export function setEventsUrl(url: string | null): void {
  currentEventsUrl = url
}

/** SSE URL for a job: the signed events_url from creation, or the bare
 * route as fallback (pre-V2.4 backends / tests without a token). */
export function eventsUrlFor(jobId: string): string {
  return currentEventsUrl ?? `/api/jobs/${jobId}/events`
}

function tokenHeaders(): Record<string, string> {
  return currentJobToken ? { 'X-Job-Token': currentJobToken } : {}
}

// ---------------------------------------------------------------------------
// Generic GET helper — fetchLayout / fetchDiff / fetchTrace share this pattern
// ---------------------------------------------------------------------------

async function apiGet<T>(url: string, errorMsg: string): Promise<T> {
  const resp = await fetch(url, { headers: tokenHeaders() })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error((err as { detail?: string }).detail ?? errorMsg)
  }
  return resp.json() as Promise<T>
}

// ---------------------------------------------------------------------------
// listModels
// ---------------------------------------------------------------------------

export async function listModels(provider: Provider, apiKey: string): Promise<ModelInfo[]> {
  const resp = await fetch(`${BASE}/api/providers/models`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider, api_key: apiKey }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error((err as { detail?: string }).detail ?? 'Failed to load models')
  }
  const data = await resp.json()
  return (data as { models: ModelInfo[] }).models
}

// ---------------------------------------------------------------------------
// createJob
// ---------------------------------------------------------------------------

export async function createJob(
  files: File[],
  provider: Provider,
  apiKey: string,
  model: string,
): Promise<{ job_id: string; job_token?: string | null; events_url?: string | null }> {
  const form = new FormData()
  for (const f of files) {
    form.append('files', f)
  }
  form.append('provider', provider)
  form.append('api_key', apiKey)
  form.append('model', model)

  const resp = await fetch(`${BASE}/api/jobs`, {
    method: 'POST',
    body: form,
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error((err as { detail?: string }).detail ?? 'Failed to create job')
  }
  const data = (await resp.json()) as {
    job_id: string
    job_token?: string | null
    events_url?: string | null
  }
  setJobToken(data.job_token ?? null)
  setEventsUrl(data.events_url ?? null)
  return data
}

// ---------------------------------------------------------------------------
// fetchLayout / fetchDiff / fetchTrace — all use apiGet<T>
// ---------------------------------------------------------------------------

export function fetchLayout(jobId: string): Promise<LayoutData> {
  return apiGet<LayoutData>(`${BASE}/api/jobs/${jobId}/layout`, 'Failed to fetch layout')
}

export function fetchDiff(jobId: string): Promise<DiffData> {
  return apiGet<DiffData>(`${BASE}/api/jobs/${jobId}/diff`, 'Failed to fetch diff')
}

export function fetchTrace(jobId: string): Promise<TraceData> {
  return apiGet<TraceData>(`${BASE}/api/jobs/${jobId}/trace`, 'Failed to fetch trace')
}

// ---------------------------------------------------------------------------
// fetchJobStatus — authoritative job status (polling fallback when SSE dies)
// ---------------------------------------------------------------------------

/**
 * Plan V1.2 — returns `null` on 404 (job evicted/unknown: the ONE case
 * where giving up is correct) and throws on transport errors so the
 * caller can keep polling: a dead network is never a job outcome.
 */
export async function fetchJobStatus(jobId: string): Promise<JobStatusData | null> {
  const resp = await fetch(`${BASE}/api/jobs/${jobId}`, { headers: tokenHeaders() })
  if (resp.status === 404) return null
  if (!resp.ok) {
    throw new Error(`Status poll failed: ${resp.status}`)
  }
  return resp.json() as Promise<JobStatusData>
}

// ---------------------------------------------------------------------------
// cancelJob — cooperative cancellation (Plan V2.2). Idempotent.
// ---------------------------------------------------------------------------

export async function cancelJob(jobId: string): Promise<void> {
  const resp = await fetch(`${BASE}/api/jobs/${jobId}/cancel`, {
    method: 'POST',
    headers: tokenHeaders(),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error((err as { detail?: string }).detail ?? 'Failed to cancel job')
  }
}

// ---------------------------------------------------------------------------
// downloadJob — triggers browser download
// ---------------------------------------------------------------------------

/** Plan V2.4 — fetch with the token in a HEADER, then hand the payload
 * to the browser as a blob: the capability token never appears in a
 * URL (address bar, proxy logs, copied links). */
export async function downloadJob(jobId: string): Promise<void> {
  const resp = await fetch(`${BASE}/api/jobs/${jobId}/download`, { headers: tokenHeaders() })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error((err as { detail?: string }).detail ?? 'Failed to download')
  }
  const disposition = resp.headers.get('content-disposition') ?? ''
  const m = disposition.match(/filename="?([^";]+)"?/)
  const filename = m?.[1] ?? `corrigenda-${jobId}.zip`
  const blob = await resp.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}
