import { useEffect, useRef } from 'react'
import type { LogEntry, LogType } from '../types'

interface LogPanelProps {
  logs: LogEntry[]
}

const TYPE_COLORS: Record<LogType, string> = {
  info: 'text-slate-400',
  warning: 'text-amber-400',
  error: 'text-red-400',
  success: 'text-green-400',
}

const TYPE_ICONS: Record<LogType, string> = {
  info: '·',
  warning: '▲',
  error: '✕',
  success: '✓',
}

function formatTime(d: Date): string {
  return d.toTimeString().slice(0, 8)
}

export function LogPanel({ logs }: LogPanelProps) {
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [logs])

  return (
    <div className="bg-slate-950 border border-slate-700 rounded-lg overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2 border-b border-slate-700 bg-slate-900">
        <span className="font-mono text-xs text-slate-500 uppercase tracking-wider">Event log</span>
        <span className="font-mono text-xs text-slate-600">{logs.length} entries</span>
      </div>
      <div className="h-56 overflow-y-auto p-2 space-y-0.5 font-mono text-xs">
        {logs.length === 0 ? (
          <div className="text-slate-700 text-center py-8">No events yet</div>
        ) : (
          logs.map((log) => (
            <div key={log.id} className="flex items-start gap-2 py-0.5">
              <span className="text-slate-600 flex-shrink-0 select-none">
                {formatTime(log.timestamp)}
              </span>
              <span className={`flex-shrink-0 ${TYPE_COLORS[log.type]}`}>
                {TYPE_ICONS[log.type]}
              </span>
              <span className={TYPE_COLORS[log.type]}>{log.message}</span>
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
