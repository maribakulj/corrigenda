import { useCallback, useRef, useState } from 'react'

interface FileUploadProps {
  onFilesChange: (files: File[]) => void
  disabled?: boolean
}

const ACCEPTED = ['.xml', '.alto', '.zip']
const ACCEPTED_MIME = ['application/xml', 'text/xml', 'application/zip']

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function isAllowed(file: File): boolean {
  const name = file.name.toLowerCase()
  return ACCEPTED.some((ext) => name.endsWith(ext)) || ACCEPTED_MIME.includes(file.type)
}

export function FileUpload({ onFilesChange, disabled }: FileUploadProps) {
  const [files, setFiles] = useState<File[]>([])
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  function updateFiles(next: File[]) {
    setFiles(next)
    onFilesChange(next)
  }

  const addFiles = useCallback(
    (incoming: FileList | null) => {
      if (!incoming) return
      const valid = Array.from(incoming).filter(isAllowed)
      const merged = [...files]
      for (const f of valid) {
        if (!merged.some((existing) => existing.name === f.name)) {
          merged.push(f)
        }
      }
      updateFiles(merged)
    },
    // updateFiles closes over `setFiles` (stable) and `onFilesChange`
    // (a prop, deliberately re-captured each render since the parent
    // controls its identity). `files` is the only changing capture
    // we need to track here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [files],
  )

  function removeFile(index: number) {
    updateFiles(files.filter((_, i) => i !== index))
  }

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault()
      setDragging(false)
      if (!disabled) addFiles(e.dataTransfer.files)
    },
    [addFiles, disabled],
  )

  const onDragOver = (e: React.DragEvent) => {
    e.preventDefault()
    setDragging(true)
  }
  const onDragLeave = () => setDragging(false)

  return (
    <div className="space-y-3">
      {/* Drop zone */}
      <div
        onClick={() => !disabled && inputRef.current?.click()}
        onDrop={onDrop}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        className={[
          'border-2 border-dashed rounded-lg p-8 text-center cursor-pointer transition-colors',
          disabled
            ? 'opacity-40 cursor-not-allowed border-slate-600'
            : dragging
              ? 'border-amber-400 bg-amber-500/10'
              : 'border-slate-600 hover:border-amber-500/60 hover:bg-slate-800/50',
        ].join(' ')}
      >
        <div className="text-3xl mb-2">📄</div>
        <p className="text-slate-300 font-mono text-sm">
          Drop files here or <span className="text-amber-400 underline">browse</span>
        </p>
        <p className="text-slate-500 font-mono text-xs mt-1">
          {ACCEPTED.join(' · ')} — ZIP can include images (.jpg .png .tif)
        </p>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept={ACCEPTED.join(',')}
          className="hidden"
          onChange={(e) => addFiles(e.target.files)}
          disabled={disabled}
        />
      </div>

      {/* File list */}
      {files.length > 0 && (
        <ul className="space-y-1">
          {files.map((f, i) => (
            <li
              key={`${f.name}-${i}`}
              className="flex items-center justify-between bg-slate-800 rounded px-3 py-2"
            >
              <div className="flex items-center gap-2 min-w-0">
                <span className="text-amber-400 font-mono text-xs flex-shrink-0">
                  {String(i + 1).padStart(2, '0')}
                </span>
                <span className="text-slate-200 font-mono text-sm truncate">{f.name}</span>
              </div>
              <div className="flex items-center gap-3 flex-shrink-0 ml-2">
                <span className="text-slate-500 font-mono text-xs">{formatSize(f.size)}</span>
                {!disabled && (
                  <button
                    onClick={() => removeFile(i)}
                    className="text-slate-500 hover:text-red-400 transition-colors font-mono text-xs"
                    title="Remove"
                  >
                    ✕
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
