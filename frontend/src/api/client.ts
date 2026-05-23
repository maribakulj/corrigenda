import type { DiffData, LayoutData, ModelInfo, Provider, TraceData } from '../types'

// proxied via vite → http://localhost:8000
const BASE = ''

// ---------------------------------------------------------------------------
// Generic GET helper — fetchLayout / fetchDiff / fetchTrace share this pattern
// ---------------------------------------------------------------------------

async function apiGet<T>(url: string, errorMsg: string): Promise<T> {
  const resp = await fetch(url)
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
): Promise<{ job_id: string }> {
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
  return resp.json() as Promise<{ job_id: string }>
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
// downloadJob — triggers browser download
// ---------------------------------------------------------------------------

export function downloadJob(jobId: string): void {
  const url = `${BASE}/api/jobs/${jobId}/download`
  const a = document.createElement('a')
  a.href = url
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
}
