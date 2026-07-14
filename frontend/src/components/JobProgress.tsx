import type { JobProgress, JobStatus } from '../types'

interface JobProgressProps {
  progress: JobProgress
  status: JobStatus | null
}

const STATUS_COLORS: Record<string, string> = {
  queued: 'text-slate-400',
  started: 'text-amber-400',
  running: 'text-amber-400',
  completed: 'text-green-400',
  // Audit-F24 — degraded terminal success rendered a blank badge with a
  // literal `undefined` class before this key existed. Amber signals
  // "done, but review the fallbacks".
  completed_with_fallbacks: 'text-amber-300',
  failed: 'text-red-400',
}

const STATUS_LABELS: Record<string, string> = {
  queued: 'QUEUED',
  started: 'STARTED',
  running: 'RUNNING',
  completed: 'COMPLETED',
  completed_with_fallbacks: 'COMPLETED (WITH FALLBACKS)',
  failed: 'FAILED',
}

// Defensive fallbacks so a status the maps don't know about (a future
// backend value) never yields an empty label or an `undefined` class.
const FALLBACK_COLOR = 'text-slate-400'

export function JobProgressPanel({ progress, status }: JobProgressProps) {
  const pct =
    progress.lines_total > 0 ? Math.round((progress.lines_done / progress.lines_total) * 100) : 0

  const statusColor = (status && STATUS_COLORS[status]) || FALLBACK_COLOR
  const statusLabel = status ? (STATUS_LABELS[status] ?? status.toUpperCase()) : '—'

  return (
    <div className="space-y-3">
      {/* Status badge */}
      <div className="flex items-center justify-between">
        <span className={`font-mono text-xs tracking-widest ${statusColor}`}>{statusLabel}</span>
        <span className="font-mono text-xs text-slate-400">{pct}%</span>
      </div>

      {/* Progress bar */}
      <div className="h-2 bg-slate-700 rounded-full overflow-hidden">
        <div
          className="h-full bg-amber-500 rounded-full transition-all duration-300"
          style={{ width: `${pct}%` }}
        />
      </div>

      {/* Counters */}
      <div className="grid grid-cols-3 gap-2 font-mono text-xs">
        <div className="bg-slate-800 rounded p-2 text-center">
          <div className="text-amber-400 text-lg font-bold">
            {progress.pages_done}
            <span className="text-slate-500 text-sm">/{progress.pages_total || '—'}</span>
          </div>
          <div className="text-slate-500 uppercase tracking-wider text-xs mt-0.5">Pages</div>
        </div>
        <div className="bg-slate-800 rounded p-2 text-center">
          <div className="text-amber-400 text-lg font-bold">
            {progress.lines_done}
            <span className="text-slate-500 text-sm">/{progress.lines_total || '—'}</span>
          </div>
          <div className="text-slate-500 uppercase tracking-wider text-xs mt-0.5">Lines</div>
        </div>
        <div className="bg-slate-800 rounded p-2 text-center">
          {progress.hyphen_pairs_total > 0 ? (
            <>
              <div className="text-amber-400 text-lg font-bold">
                {progress.hyphen_pairs_reconciled}
                <span className="text-slate-500 text-sm">/{progress.hyphen_pairs_total}</span>
              </div>
              <div className="text-slate-500 uppercase tracking-wider text-xs mt-0.5">
                Hyphen pairs
              </div>
            </>
          ) : (
            <>
              <div className="text-slate-600 text-lg font-bold">—</div>
              <div className="text-slate-600 uppercase tracking-wider text-xs mt-0.5">
                Hyphen pairs
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
