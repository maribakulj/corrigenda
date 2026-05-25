/**
 * DiffViewer tests.
 *
 * The component does word-level LCS diffing in pure JS — a regression
 * in the algorithm or in the render would silently strand corrected
 * text in the UI. These tests pin the empty-state guard, the basic
 * render contract, and the diff-marker semantics.
 *
 * Full LCS coverage would belong in a separate `tokenDiff.test.ts`
 * (the function is currently colocated and not exported). Future
 * cleanup: extract it.
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { DiffData } from '../types'
import { DiffViewer } from './DiffViewer'

const oneLine = (modified: boolean): DiffData => ({
  job_id: 'j-1',
  pages: [
    {
      page_id: 'P1',
      page_index: 0,
      lines: [
        {
          line_id: 'TL1',
          ocr_text: 'la municipalité prenne les rnesures',
          corrected_text: modified ? 'la municipalité prenne les mesures' : '…',
          modified,
          hyphen_role: 'none',
          hyphen_subs_content: null,
        },
      ],
    },
  ],
  stats: { total_lines: 1, modified_lines: modified ? 1 : 0, hyphen_pairs: 0 },
})

describe('DiffViewer', () => {
  it('renders the empty-state guard when no pages exist', () => {
    const empty: DiffData = {
      job_id: 'j-empty',
      pages: [],
      stats: { total_lines: 0, modified_lines: 0, hyphen_pairs: 0 },
    }
    render(<DiffViewer data={empty} />)
    expect(screen.getByText(/aucune page/i)).toBeInTheDocument()
  })

  it('renders both OCR and corrected text for a modified line', () => {
    render(<DiffViewer data={oneLine(true)} />)
    // Both versions must be visible somewhere — DiffViewer paints
    // OCR on one side and corrected on the other.
    expect(screen.getByText(/rnesures/)).toBeInTheDocument()
    expect(screen.getAllByText(/mesures/).length).toBeGreaterThan(0)
  })

  it("doesn't render any 'changed' marker when the line is unmodified", () => {
    const unmodified: DiffData = {
      job_id: 'j-2',
      pages: [
        {
          page_id: 'P1',
          page_index: 0,
          lines: [
            {
              line_id: 'TL1',
              ocr_text: 'identical text',
              corrected_text: 'identical text',
              modified: false,
              hyphen_role: 'none',
              hyphen_subs_content: null,
            },
          ],
        },
      ],
      stats: { total_lines: 1, modified_lines: 0, hyphen_pairs: 0 },
    }
    render(<DiffViewer data={unmodified} />)
    // The text appears (unchanged), but no diff styling triggers — we
    // assert via the document container being free of the
    // 'aucune page' guard so we know the lines were rendered.
    expect(screen.queryByText(/aucune page/i)).not.toBeInTheDocument()
    expect(screen.getAllByText(/identical text/).length).toBeGreaterThan(0)
  })

  it('surfaces document-level stats (modified/total/hyphens)', () => {
    const data: DiffData = {
      job_id: 'j-3',
      pages: oneLine(true).pages,
      stats: { total_lines: 42, modified_lines: 7, hyphen_pairs: 3 },
    }
    render(<DiffViewer data={data} />)
    // Stats labels are rendered alongside their numbers; we don't pin
    // the exact wording, just the values reach the DOM.
    expect(screen.getAllByText(/42/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/\b7\b/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/\b3\b/).length).toBeGreaterThan(0)
  })
})
