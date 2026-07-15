import { useState } from 'react'
import { downloadJob } from '../api/client'
import type { JobStats } from '../types'

interface DownloadButtonProps {
  jobId: string
  stats: JobStats | null
}

export function DownloadButton({ jobId, stats }: DownloadButtonProps) {
  // Plan V2.4 — the download is a fetch (token in a header, blob to the
  // browser): it can fail like any request, so surface that instead of
  // a dead click.
  const [downloading, setDownloading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleDownload() {
    if (downloading) return
    setDownloading(true)
    setError(null)
    try {
      await downloadJob(jobId)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Download failed')
    } finally {
      setDownloading(false)
    }
  }

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

      {/* Download error */}
      {error && (
        <p className="font-mono text-xs text-red-400 bg-red-900/20 border border-red-800/40 rounded px-3 py-2">
          {error}
        </p>
      )}

      {/* Download button */}
      <button
        onClick={handleDownload}
        disabled={downloading}
        className="w-full flex items-center justify-center gap-2 py-3 px-6
                   bg-amber-500 hover:bg-amber-400 disabled:bg-slate-700 disabled:text-slate-500
                   text-slate-900 font-mono font-bold
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
