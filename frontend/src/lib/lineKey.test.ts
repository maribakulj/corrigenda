/**
 * (page_id, line_id) identity — line_id alone is NOT unique across
 * pages: two uploaded ALTO files routinely both contain L1, L2, …
 * The old Map<line_id, LineTrace> silently collapsed those lines
 * (last-write-wins), so clicking a line could show another file's
 * trace. These tests pin the composite-key contract.
 */
import { describe, expect, it } from 'vitest'

import type { LineTrace } from '../types'
import { buildTraceMap, lineKey } from './lineKey'

function trace(pageId: string, lineId: string, ocr: string): LineTrace {
  return {
    line_id: lineId,
    page_id: pageId,
    source_ocr_text: ocr,
    model_input_text: null,
    model_corrected_text: null,
    projected_text: null,
    output_alto_text: null,
    hyphen_role: null,
    rewriter_path: null,
    validation_status: null,
    fallback_reason: null,
  }
}

describe('lineKey', () => {
  it('produces distinct keys for the same line_id on different pages', () => {
    expect(lineKey('file1.xml#P1', 'L1')).not.toBe(lineKey('file2.xml#P1', 'L1'))
  })
})

describe('buildTraceMap', () => {
  it('keeps every trace when two files share the same TextLine@IDs', () => {
    // Deliberately duplicated IDs across two source files — the exact
    // scenario the old line_id-only map collapsed.
    const lines = [
      trace('P_file1', 'L1', 'texte du fichier 1, ligne 1'),
      trace('P_file1', 'L2', 'texte du fichier 1, ligne 2'),
      trace('P_file2', 'L1', 'texte du fichier 2, ligne 1'),
      trace('P_file2', 'L2', 'texte du fichier 2, ligne 2'),
    ]

    const map = buildTraceMap(lines)

    expect(map.size).toBe(4)
    expect(map.get(lineKey('P_file1', 'L1'))?.source_ocr_text).toBe(
      'texte du fichier 1, ligne 1',
    )
    // Selecting L1 of file 2 must return file 2's trace, never file 1's.
    expect(map.get(lineKey('P_file2', 'L1'))?.source_ocr_text).toBe(
      'texte du fichier 2, ligne 1',
    )
  })
})
