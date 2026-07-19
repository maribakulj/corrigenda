// ---------------------------------------------------------------------------
// REST types — derived from the OpenAPI-generated file (Plan V3.5).
//
// The CI drift check regenerates api.generated.ts from the backend's
// live schema; aliasing here means a backend schema change breaks the
// frontend COMPILATION, not just an adjacent artefact. Only UI-local
// models and the SSE protocol (not exposed by OpenAPI) stay manual.
// ---------------------------------------------------------------------------

import type { components } from './api.generated'

export type Provider = components['schemas']['Provider']
export type JobStatus = components['schemas']['JobStatus']
export type ModelInfo = components['schemas']['ModelInfo']

export const PROVIDER_LABELS: Record<Provider, string> = {
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  mistral: 'Mistral',
  google: 'Google Gemini',
}

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

export type LogType = 'info' | 'warning' | 'error' | 'success'

export interface LogEntry {
  id: string
  type: LogType
  message: string
  timestamp: Date
}

// ---------------------------------------------------------------------------
// Job progress
// ---------------------------------------------------------------------------

export interface JobProgress {
  pages_total: number
  pages_done: number
  lines_total: number
  lines_done: number
  hyphen_pairs_total: number
  hyphen_pairs_reconciled: number
}

// ---------------------------------------------------------------------------
// SSE event data (discriminated union on `event`)
// ---------------------------------------------------------------------------

export interface SSEQueued {
  event: 'queued'
  job_id: string
}
export interface SSEStarted {
  event: 'started'
  job_id: string
}
export interface SSEDocumentParsed {
  event: 'document_parsed'
  total_pages: number
  total_blocks: number
  total_lines: number
  hyphen_pairs: number
}
export interface SSEPageStarted {
  event: 'page_started'
  page_id: string
  page_index: number
  line_count: number
  hyphen_pair_count: number
}
export interface SSEChunkPlanned {
  event: 'chunk_planned'
  page_id: string
  granularity: string
  chunk_count: number
}
export interface SSEChunkStarted {
  event: 'chunk_started'
  chunk_id: string
  granularity: string
  line_count: number
  attempt: number
}
export interface SSEChunkCompleted {
  event: 'chunk_completed'
  chunk_id: string
  line_count: number
  // Audit-F25 — the lines this chunk OWNS (excludes WINDOW context
  // overlap). Present on current backends; absent on older ones.
  target_count?: number
  hyphen_pairs_reconciled: number
  attempt: number
}
// Audit-F26 — diagnostic events the backend emits and the hook now
// surfaces (previously subscribed but unmodelled → silently swallowed).
export interface SSEChunkError {
  event: 'chunk_error'
  chunk_id?: string
  message?: string
}
// Wave-4 review — these payloads mirror the backend's ACTUAL emit sites
// (core/pipeline.py); the previous shapes pinned fields that were never
// sent (`granularity`, `message`).
export interface SSEChunkDowngraded {
  event: 'chunk_downgraded'
  chunk_id?: string
  from_granularity?: string
  to_granularity?: string
  line_count?: number
  target_count?: number
  budget_remaining?: number
}
export interface SSEHyphenPartnerMissing {
  event: 'hyphen_partner_missing'
  chunk_id?: string
  line_id?: string
  missing_partner_id?: string
  direction?: string
}
// Per-run statistics events — modelled so the stream switch can
// deliberately silence them (they carry no user-facing state).
export interface SSERewriterStats {
  event: 'rewriter_stats'
}
export interface SSEReconcileStats {
  event: 'reconcile_stats'
}
export interface SSERetry {
  event: 'retry'
  chunk_id: string
  attempt: number
  error: string
}
export interface SSEWarning {
  event: 'warning'
  message: string
}
export interface SSEPageCompleted {
  event: 'page_completed'
  page_id: string
  page_index: number
  corrections: number
}
export interface SSECompleted {
  event: 'completed'
  total_lines: number
  lines_modified: number
  hyphen_pairs_total: number
  duration_seconds: number
  // P0-1 — terminal status ('completed' | 'completed_with_fallbacks') and
  // the number of lines that kept their OCR source text.
  status?: JobStatus
  fallbacks?: number
}
export interface SSEFailed {
  event: 'failed'
  error: string
}
// Plan V2.2 — terminal event for a user-requested cancellation.
export interface SSECancelled {
  event: 'cancelled'
  job_id?: string
}
export interface SSEKeepalive {
  event: 'keepalive'
}
// Server-sent stream error (job_not_found / subscriber_cap_reached).
export interface SSEError {
  event: 'error'
  reason?: string
  message?: string
}

export type SSEEventData =
  | SSEQueued
  | SSEStarted
  | SSEDocumentParsed
  | SSEPageStarted
  | SSEChunkPlanned
  | SSEChunkStarted
  | SSEChunkCompleted
  | SSEChunkError
  | SSEChunkDowngraded
  | SSEHyphenPartnerMissing
  | SSERetry
  | SSEWarning
  | SSEPageCompleted
  | SSECompleted
  | SSEFailed
  | SSECancelled
  | SSEKeepalive
  | SSEError
  | SSERewriterStats
  | SSEReconcileStats

// ---------------------------------------------------------------------------
// Layout viewer
// ---------------------------------------------------------------------------

export interface LayoutLine {
  line_id: string
  hpos: number
  vpos: number
  width: number
  height: number
  ocr_text: string
  corrected_text: string
  modified: boolean
  hyphen_role: 'none' | 'HypPart1' | 'HypPart2'
}

export interface LayoutBlock {
  block_id: string
  hpos: number
  vpos: number
  width: number
  height: number
  lines: LayoutLine[]
}

export interface LayoutPage {
  page_id: string
  page_index: number
  page_width: number
  page_height: number
  image_url: string | null
  blocks: LayoutBlock[]
}

export interface LayoutData {
  job_id: string
  pages: LayoutPage[]
}

// ---------------------------------------------------------------------------
// Diff viewer
// ---------------------------------------------------------------------------

export interface DiffLine {
  line_id: string
  ocr_text: string
  corrected_text: string
  modified: boolean
  hyphen_role: 'none' | 'HypPart1' | 'HypPart2'
  hyphen_subs_content: string | null
}

export interface DiffPage {
  page_id: string
  page_index: number
  lines: DiffLine[]
}

export interface DiffData {
  job_id: string
  pages: DiffPage[]
  stats: {
    total_lines: number
    modified_lines: number
    hyphen_pairs: number
  }
}

// ---------------------------------------------------------------------------
// Final job stats (for DownloadButton)
// ---------------------------------------------------------------------------

export interface JobStats {
  lines_modified: number
  hyphen_pairs: number
  duration_seconds: number
}

// ---------------------------------------------------------------------------
// GET /api/jobs/{job_id} — authoritative status snapshot (Plan V3.5:
// aliased to the generated JobStatusResponse, never maintained by hand)
// ---------------------------------------------------------------------------

export type JobStatusData = components['schemas']['JobStatusResponse']

// Plan V1.2 — connection state of the SSE stream, deliberately separate
// from JobStatus: losing the stream is a transport problem, never a job
// outcome. 'polling' means SSE reconnects were exhausted and the hook
// now follows the job via GET /api/jobs/{id}.
export type StreamState = 'idle' | 'live' | 'reconnecting' | 'polling'

// ---------------------------------------------------------------------------
// Line outcomes (report v2 — §9 P3.5, Sprint 6 debug panel)
// ---------------------------------------------------------------------------

export interface ProposalStage {
  input_text: string | null
  output_text: string | null
}

export interface DecisionReason {
  code: string
  detail: string | null
}

export interface DecisionStage {
  status: string // corrected / fallback
  final_text: string
  reason: DecisionReason | null
}

export interface ProjectionStage {
  extracted_text: string | null
  rewriter_path: string | null
}

// One line's staged journey: source → proposal → decision → projection.
export interface LineOutcome {
  line_id: string
  page_id: string
  hyphen_role: string | null
  source_text: string
  proposal: ProposalStage | null
  decision: DecisionStage
  projection: ProjectionStage | null
}

// The /trace endpoint returns corrigenda's versioned CorrectionReport
// (§9, report_version 2.0) verbatim — run_id equals the server job_id.
export interface TraceData {
  report_version: string
  run_id: string
  total_lines: number
  lines: LineOutcome[]
  format_losses?: Record<string, number> | null
}
