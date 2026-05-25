import type { ModelInfo } from '../types'

interface ModelSelectorProps {
  models: ModelInfo[]
  loading: boolean
  selectedModel: string | null
  onLoad: () => void
  onSelect: (modelId: string) => void
  disabled?: boolean
}

function formatContext(ctx: number | null): string {
  if (!ctx) return ''
  if (ctx >= 1000) return `${Math.round(ctx / 1000)}k ctx`
  return `${ctx} ctx`
}

export function ModelSelector({
  models,
  loading,
  selectedModel,
  onLoad,
  onSelect,
  disabled,
}: ModelSelectorProps) {
  return (
    <div className="flex flex-col gap-1">
      <label className="text-slate-400 font-mono text-xs uppercase tracking-wider">Model</label>
      <div className="flex gap-2">
        <select
          value={selectedModel ?? ''}
          onChange={(e) => onSelect(e.target.value)}
          disabled={disabled || loading || models.length === 0}
          className="flex-1 bg-slate-800 border border-slate-600 rounded px-3 py-2 font-mono text-sm
                     text-slate-200 focus:outline-none focus:border-amber-500
                     disabled:opacity-40 disabled:cursor-not-allowed appearance-none cursor-pointer"
        >
          <option value="" disabled>
            {models.length === 0 ? 'Load models first…' : 'Select a model…'}
          </option>
          {models.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label}
              {m.context_window ? ` (${formatContext(m.context_window)})` : ''}
            </option>
          ))}
        </select>
        <button
          onClick={onLoad}
          disabled={disabled || loading}
          className="px-4 py-2 bg-slate-700 hover:bg-slate-600 border border-slate-600
                     text-slate-200 font-mono text-sm rounded transition-colors
                     disabled:opacity-40 disabled:cursor-not-allowed whitespace-nowrap"
        >
          {loading ? (
            <span className="flex items-center gap-2">
              <span className="inline-block w-3 h-3 border-2 border-amber-400 border-t-transparent rounded-full animate-spin" />
              Loading…
            </span>
          ) : (
            'Load models'
          )}
        </button>
      </div>
    </div>
  )
}
