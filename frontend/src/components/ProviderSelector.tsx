import type { Provider } from '../types'
import { PROVIDER_LABELS } from '../types'

interface ProviderSelectorProps {
  value: Provider | null
  onChange: (provider: Provider) => void
  disabled?: boolean
}

const PROVIDERS: Provider[] = ['openai', 'anthropic', 'mistral', 'google']

export function ProviderSelector({ value, onChange, disabled }: ProviderSelectorProps) {
  return (
    <div className="flex flex-col gap-1">
      <label className="text-slate-400 font-mono text-xs uppercase tracking-wider">Provider</label>
      <select
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value as Provider)}
        disabled={disabled}
        className="bg-slate-800 border border-slate-600 rounded px-3 py-2 font-mono text-sm text-slate-200
                   focus:outline-none focus:border-amber-500 disabled:opacity-40 disabled:cursor-not-allowed
                   appearance-none cursor-pointer"
      >
        <option value="" disabled>
          Select a provider…
        </option>
        {PROVIDERS.map((p) => (
          <option key={p} value={p}>
            {PROVIDER_LABELS[p]}
          </option>
        ))}
      </select>
    </div>
  )
}
