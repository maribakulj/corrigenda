import { downloadJob } from '../api/client'
import type { JobStats } from '../types'

interface DownloadButtonProps {
  jobId: string
  stats: JobStats | null
}

export function DownloadButton({ jobId, stats }: DownloadButtonProps) {
  return (
    <div className="bg-slate-800 border border-green-800/50 rounded-lg p-4 space-y-3">
      {/* Stats */}
      {stats && (
        <div className="grid grid-cols-3 gap-2 font-mono text-xs">
          <div className="text-center">
            <div className="text-green-400 text-xl font-bold">{stats.lines_modified}</div>
            <div className="text-slate-500 uppercase tracking-wider text-xs">Lines modified</div>
          </div>
          <div className="text-center">
            <div className="text-green-400 text-xl font-bold">{stats.hyphen_pairs}</div>
            <div className="text-slate-500 uppercase tracking-wider text-xs">Hyphen pairs</div>
          </div>
          <div className="text-center">
            <div className="text-green-400 text-xl font-bold">
              {stats.duration_seconds.toFixed(1)}s
            </div>
            <div className="text-slate-500 uppercase tracking-wider text-xs">Duration</div>
          </div>
        </div>
      )}

      {/* Download button */}
      <button
        onClick={() => downloadJob(jobId)}
        className="w-full flex items-center justify-center gap-2 py-3 px-6
                   bg-amber-500 hover:bg-amber-400 text-slate-900 font-mono font-bold
                   text-sm rounded transition-colors uppercase tracking-wider"
      >
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
            strokeWidth={2.5}
            d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"
          />
        </svg>
        Download corrected ALTO
      </button>
    </div>
  )
}
