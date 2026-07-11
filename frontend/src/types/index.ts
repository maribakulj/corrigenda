// ---------------------------------------------------------------------------
// Enums
// ---------------------------------------------------------------------------

export type Provider = 'openai' | 'anthropic' | 'mistral' | 'google'

export const PROVIDER_LABELS: Record<Provider, string> = {
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  mistral: 'Mistral',
  google: 'Google Gemini',
}

export type JobStatus =
  | 'queued'
  | 'started'
  | 'running'
  | 'completed'
  // P0-1 — terminal success where some lines fell back to their OCR source
  // text: outputs are valid and downloadable, but the run is degraded.
  | 'completed_with_fallbacks'
  | 'failed'

// ---------------------------------------------------------------------------
// Model info
// ---------------------------------------------------------------------------

export interface ModelInfo {
  id: string
  label: string
  supports_structured_output: boolean
  context_window: number | null
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
  hyphen_pairs_reconciled: number
  attempt: number
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
export interface SSEKeepalive {
  event: 'keepalive'
}

export type SSEEventData =
  | SSEQueued
  | SSEStarted
  | SSEDocumentParsed
  | SSEPageStarted
  | SSEChunkPlanned
  | SSEChunkStarted
  | SSEChunkCompleted
  | SSERetry
  | SSEWarning
  | SSEPageCompleted
  | SSECompleted
  | SSEFailed
  | SSEKeepalive

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
// Line trace (Sprint 5bis / Sprint 6 debug)
// ---------------------------------------------------------------------------

export interface LineTrace {
  line_id: string
  page_id: string
  source_ocr_text: string
  model_input_text: string | null
  model_corrected_text: string | null
  projected_text: string | null
  output_alto_text: string | null
  hyphen_role: string | null
  rewriter_path: string | null
  validation_status: string | null
  fallback_reason: string | null
}

// The /trace endpoint now returns corrigenda's versioned CorrectionReport
// (§9) verbatim — run_id equals the server job_id.
export interface TraceData {
  report_version: string
  run_id: string
  total_lines: number
  lines: LineTrace[]
  format_losses?: Record<string, number> | null
}
