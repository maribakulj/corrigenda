import type { LineOutcome } from '../types'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Report v2 (§9): the outcome's stages flattened for display, in
 * pipeline order. Absent stages (no producer call, no rendered output)
 * surface as null texts. */
function stagesOf(outcome: LineOutcome): { label: string; text: string | null }[] {
  return [
    { label: 'Source', text: outcome.source_text },
    { label: 'Producer input', text: outcome.proposal?.input_text ?? null },
    { label: 'Producer output', text: outcome.proposal?.output_text ?? null },
    { label: 'Decision (retained)', text: outcome.decision.final_text },
    { label: 'Output (extracted)', text: outcome.projection?.extracted_text ?? null },
  ]
}

function Badge({ children, color }: { children: React.ReactNode; color: string }) {
  return (
    <span
      className={`inline-block font-mono text-[10px] leading-none px-1.5 py-0.5 rounded border ${color}`}
    >
      {children}
    </span>
  )
}

function statusColor(status: string | null): string {
  switch (status) {
    case 'corrected':
      return 'text-emerald-400 border-emerald-700/50'
    case 'fallback':
      return 'text-orange-400 border-orange-700/50'
    case 'failed':
      return 'text-red-400 border-red-700/50'
    default:
      return 'text-slate-400 border-slate-700/50'
  }
}

function hyphenColor(role: string | null): string {
  switch (role) {
    case 'HypPart1':
      return 'text-amber-400 border-amber-700/50'
    case 'HypPart2':
      return 'text-amber-300 border-amber-700/50'
    case 'HypBoth':
      return 'text-amber-200 border-amber-600/50'
    default:
      return 'text-slate-500 border-slate-700/50'
  }
}

function pathColor(path: string | null): string {
  switch (path) {
    case 'fast_path':
      return 'text-sky-400 border-sky-700/50'
    case 'slow_path':
      return 'text-violet-400 border-violet-700/50'
    case 'subs_only':
      return 'text-teal-400 border-teal-700/50'
    default:
      return 'text-slate-500 border-slate-700/50'
  }
}

/** Returns true if two text values differ (null-safe). */
function textsDiffer(a: string | null, b: string | null): boolean {
  if (a == null || b == null) return false
  return a !== b
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface LineTracePanelProps {
  trace: LineOutcome
  onClose: () => void
}

export function LineTracePanel({ trace, onClose }: LineTracePanelProps) {
  const stages = stagesOf(trace)
  const reason = trace.decision.reason
  return (
    <div className="rounded-lg border border-slate-700/60 bg-slate-800/50 overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 border-b border-slate-700/60 flex items-center justify-between">
        <div className="flex items-center gap-3 flex-wrap">
          <h3 className="font-mono text-xs font-bold text-slate-200">{trace.line_id}</h3>
          <Badge color={hyphenColor(trace.hyphen_role)}>{trace.hyphen_role ?? 'none'}</Badge>
          <Badge color={statusColor(trace.decision.status)}>{trace.decision.status}</Badge>
          <Badge color={pathColor(trace.projection?.rewriter_path ?? null)}>
            {trace.projection?.rewriter_path ?? '—'}
          </Badge>
        </div>
        <button
          onClick={onClose}
          className="text-slate-500 hover:text-slate-300 transition-colors ml-4"
          aria-label="Close trace panel"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            className="w-4 h-4"
            viewBox="0 0 20 20"
            fill="currentColor"
          >
            <path
              fillRule="evenodd"
              d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z"
              clipRule="evenodd"
            />
          </svg>
        </button>
      </div>

      {/* Structured decision reason (v2: code + optional detail) */}
      {reason && (
        <div className="px-4 py-2 border-b border-slate-700/40 bg-orange-900/10">
          <span className="font-mono text-[10px] text-orange-400">
            Fallback: {reason.code}
            {reason.detail ? `: ${reason.detail}` : ''}
          </span>
        </div>
      )}

      {/* 5 text stages */}
      <div className="divide-y divide-slate-800/60">
        {stages.map(({ label, text }, idx) => {
          const prevText = idx > 0 ? stages[idx - 1].text : null
          const changed = idx > 0 && textsDiffer(prevText, text)

          return (
            <div key={label} className="px-4 py-2.5">
              <div className="flex items-center gap-2 mb-1">
                <span className="font-mono text-[10px] text-slate-500 uppercase tracking-wider">
                  {idx + 1}. {label}
                </span>
                {changed && (
                  <span className="font-mono text-[9px] text-amber-500 border border-amber-700/40 rounded px-1 py-px leading-none">
                    changed
                  </span>
                )}
                {text == null && (
                  <span className="font-mono text-[9px] text-red-500/70 border border-red-800/40 rounded px-1 py-px leading-none">
                    null
                  </span>
                )}
              </div>
              <p
                className={[
                  'font-mono text-sm leading-relaxed break-all',
                  text == null
                    ? 'text-slate-600 italic'
                    : changed
                      ? 'text-amber-200'
                      : 'text-slate-300',
                ].join(' ')}
              >
                {text ?? '(not captured)'}
              </p>
            </div>
          )
        })}
      </div>

      {/* Page ID */}
      <div className="px-4 py-2 border-t border-slate-700/40">
        <span className="font-mono text-[10px] text-slate-600">page: {trace.page_id}</span>
      </div>
    </div>
  )
}
