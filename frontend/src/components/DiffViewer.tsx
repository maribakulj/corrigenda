import { useMemo, useState } from 'react'
import type { DiffData, DiffLine } from '../types'

// ---------------------------------------------------------------------------
// Word-by-word diff (no external library)
// ---------------------------------------------------------------------------

interface Token {
  text: string
  changed: boolean
}

function tokenDiff(
  ocr: string,
  corrected: string,
): { ocrTokens: Token[]; corrTokens: Token[] } {
  const a = ocr.split(' ')
  const b = corrected.split(' ')
  const m = a.length
  const n = b.length

  // LCS table
  const dp: number[][] = Array.from({ length: m + 1 }, () =>
    new Array(n + 1).fill(0),
  )
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      dp[i][j] =
        a[i - 1] === b[j - 1]
          ? dp[i - 1][j - 1] + 1
          : Math.max(dp[i - 1][j], dp[i][j - 1])
    }
  }

  // Traceback
  const ocrTokens: Token[] = []
  const corrTokens: Token[] = []
  let i = m
  let j = n
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && a[i - 1] === b[j - 1]) {
      ocrTokens.unshift({ text: a[i - 1], changed: false })
      corrTokens.unshift({ text: b[j - 1], changed: false })
      i--
      j--
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      corrTokens.unshift({ text: b[j - 1], changed: true })
      j--
    } else {
      ocrTokens.unshift({ text: a[i - 1], changed: true })
      i--
    }
  }

  return { ocrTokens, corrTokens }
}

// ---------------------------------------------------------------------------
// TokenSpan
// ---------------------------------------------------------------------------

function TokenSpan({ token }: { token: Token }) {
  if (!token.changed) {
    return <span>{token.text} </span>
  }
  return (
    <span className="bg-amber-200/20 text-amber-200 rounded px-0.5">
      {token.text}{' '}
    </span>
  )
}

// ---------------------------------------------------------------------------
// DiffRow — one TextLine
// ---------------------------------------------------------------------------

function DiffRow({ line, selected, onSelect }: { line: DiffLine; selected: boolean; onSelect?: () => void }) {
  const isModified = line.modified
  const hasHyphen = line.hyphen_role !== 'none'

  const rowBase = [
    'grid grid-cols-[5rem_1fr_1fr] gap-x-3 items-start py-2 border-b border-slate-800/60 text-sm',
    onSelect ? 'cursor-pointer hover:bg-slate-700/30' : '',
    selected ? 'bg-amber-500/10 ring-1 ring-amber-500/30' : '',
  ].join(' ')

  // Hook MUST be called unconditionally (Rules of Hooks). When the line
  // is unchanged we skip the LCS work but still pay the hook bookkeeping.
  const tokens = useMemo(
    () => (isModified ? tokenDiff(line.ocr_text, line.corrected_text) : null),
    [isModified, line.ocr_text, line.corrected_text],
  )

  if (!isModified || tokens == null) {
    return (
      <div className={rowBase} onClick={onSelect}>
        {/* line_id */}
        <span className="font-mono text-[10px] text-slate-600 pt-0.5 truncate">
          {line.line_id}
        </span>
        {/* OCR */}
        <span className="text-slate-500">{line.ocr_text}</span>
        {/* Corrected */}
        <span className="text-slate-500">{line.corrected_text}</span>
      </div>
    )
  }

  const { ocrTokens, corrTokens } = tokens

  return (
    <div className={rowBase} onClick={onSelect}>
      {/* line_id + hyphen badge */}
      <div className="flex flex-col gap-1 pt-0.5">
        <span className="font-mono text-[10px] text-slate-600 truncate">
          {line.line_id}
        </span>
        {hasHyphen && (
          <span className="font-mono text-[9px] text-amber-600/80 border border-amber-700/40 rounded px-1 py-0.5 leading-none w-fit">
            césure
          </span>
        )}
      </div>
      {/* OCR */}
      <span className="text-slate-300 leading-relaxed">
        {ocrTokens.map((t, idx) => (
          <TokenSpan key={idx} token={t} />
        ))}
      </span>
      {/* Corrected */}
      <span className="text-slate-100 leading-relaxed">
        {corrTokens.map((t, idx) => (
          <TokenSpan key={idx} token={t} />
        ))}
      </span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// DiffViewer
// ---------------------------------------------------------------------------

interface DiffViewerProps {
  data: DiffData
  selectedLineId?: string | null
  onSelectLine?: (lineId: string) => void
}

export function DiffViewer({ data, selectedLineId, onSelectLine }: DiffViewerProps) {
  const [pageIdx, setPageIdx] = useState(0)
  const currentPage = data.pages[pageIdx] ?? data.pages[0]
  const { total_lines, modified_lines, hyphen_pairs } = data.stats

  return (
    <div className="rounded-lg border border-slate-700/60 bg-slate-800/40 overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 border-b border-slate-700/60 flex items-center justify-between gap-4 flex-wrap">
        <h3 className="font-serif text-sm font-semibold text-slate-200">
          Résultats de correction
        </h3>
        <div className="flex items-center gap-4">
          {/* Stats */}
          <span className="font-mono text-xs text-amber-400">
            {modified_lines} ligne{modified_lines !== 1 ? 's' : ''} modifiée
            {modified_lines !== 1 ? 's' : ''} sur {total_lines}
            {hyphen_pairs > 0 && (
              <span className="text-slate-500 ml-2">
                · {hyphen_pairs} paire{hyphen_pairs !== 1 ? 's' : ''} de césure
              </span>
            )}
          </span>
          {/* Page selector */}
          {data.pages.length > 1 && (
            <select
              value={pageIdx}
              onChange={(e) => setPageIdx(Number(e.target.value))}
              className="font-mono text-xs bg-slate-700 border border-slate-600 text-slate-200
                         rounded px-2 py-1 focus:outline-none focus:border-amber-500"
            >
              {data.pages.map((p, i) => (
                <option key={p.page_id} value={i}>
                  Page {i + 1} — {p.page_id}
                </option>
              ))}
            </select>
          )}
        </div>
      </div>

      {/* Column headers */}
      <div className="grid grid-cols-[5rem_1fr_1fr] gap-x-3 px-4 py-2 border-b border-slate-700/40 bg-slate-800/60">
        <span className="font-mono text-[10px] text-slate-600 uppercase tracking-wider">ID</span>
        <span className="font-mono text-[10px] text-slate-500 uppercase tracking-wider">OCR source</span>
        <span className="font-mono text-[10px] text-slate-500 uppercase tracking-wider">Corrigé</span>
      </div>

      {/* Lines */}
      <div className="px-4 divide-y divide-slate-800/0 max-h-[32rem] overflow-y-auto">
        {currentPage.lines.map((line) => (
          <DiffRow
            key={line.line_id}
            line={line}
            selected={selectedLineId === line.line_id}
            onSelect={onSelectLine ? () => onSelectLine(line.line_id) : undefined}
          />
        ))}
      </div>
    </div>
  )
}
