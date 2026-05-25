import { useId, useState } from 'react'

interface ApiKeyInputProps {
  value: string
  onChange: (value: string) => void
  disabled?: boolean
}

export function ApiKeyInput({ value, onChange, disabled }: ApiKeyInputProps) {
  const [visible, setVisible] = useState(false)
  // useId() gives a stable, unique label/input link without colliding
  // across multiple instances on the same page — also makes the input
  // queryable via getByLabelText() in tests.
  const inputId = useId()

  return (
    <div className="flex flex-col gap-1">
      <label
        htmlFor={inputId}
        className="text-slate-400 font-mono text-xs uppercase tracking-wider"
      >
        API Key
      </label>
      <div className="relative">
        <input
          id={inputId}
          type={visible ? 'text' : 'password'}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
          placeholder="sk-…"
          autoComplete="off"
          className="w-full bg-slate-800 border border-slate-600 rounded px-3 py-2 pr-10
                     font-mono text-sm text-slate-200 placeholder-slate-600
                     focus:outline-none focus:border-amber-500 disabled:opacity-40 disabled:cursor-not-allowed"
        />
        <button
          type="button"
          onClick={() => setVisible((v) => !v)}
          disabled={disabled}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 hover:text-amber-400
                     transition-colors disabled:opacity-40"
          title={visible ? 'Hide key' : 'Show key'}
        >
          {visible ? (
            <svg
              xmlns="http://www.w3.org/2000/svg"
              className="w-4 h-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21"
              />
            </svg>
          ) : (
            <svg
              xmlns="http://www.w3.org/2000/svg"
              className="w-4 h-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"
              />
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"
              />
            </svg>
          )}
        </button>
      </div>
    </div>
  )
}
