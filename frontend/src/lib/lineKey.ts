import type { LineTrace } from '../types'

// A line_id is only unique WITHIN one page: ALTO TextLine@ID is an XML
// NCName scoped to its own document, so two uploaded files routinely
// both contain L1, L2, … The pipeline qualifies every line operation by
// (page_id, line_id); any UI state keyed on line_id alone collapses
// homonymous lines onto each other (last-write-wins traces, cross-page
// selection bleed). NCNames cannot contain ':', so the joined key is
// unambiguous.
export type LineKey = `${string}:${string}`

export function lineKey(pageId: string, lineId: string): LineKey {
  return `${pageId}:${lineId}`
}

export function buildTraceMap(lines: LineTrace[]): Map<LineKey, LineTrace> {
  const map = new Map<LineKey, LineTrace>()
  for (const lt of lines) {
    map.set(lineKey(lt.page_id, lt.line_id), lt)
  }
  return map
}
