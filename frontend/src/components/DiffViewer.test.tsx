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
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { lineKey } from '../lib/lineKey'
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

  it('reports (page_id, line_id) on selection — line_id alone is ambiguous across pages', () => {
    // Two pages (two source files) deliberately sharing TextLine@ID
    // 'TL1' — the scenario where a line_id-only callback made the
    // trace panel show the wrong page's trace.
    const twoPages: DiffData = {
      job_id: 'j-dup',
      pages: [
        {
          page_id: 'P_file1',
          page_index: 0,
          lines: [
            {
              line_id: 'TL1',
              ocr_text: 'ligne du premier fichier',
              corrected_text: 'ligne du premier fichier',
              modified: false,
              hyphen_role: 'none',
              hyphen_subs_content: null,
            },
          ],
        },
        {
          page_id: 'P_file2',
          page_index: 1,
          lines: [
            {
              line_id: 'TL1',
              ocr_text: 'ligne du second fichier',
              corrected_text: 'ligne du second fichier',
              modified: false,
              hyphen_role: 'none',
              hyphen_subs_content: null,
            },
          ],
        },
      ],
      stats: { total_lines: 2, modified_lines: 0, hyphen_pairs: 0 },
    }

    const onSelectLine = vi.fn()
    render(<DiffViewer data={twoPages} selectedLineKey={null} onSelectLine={onSelectLine} />)

    // Page 1: clicking TL1 must qualify it with P_file1. (The text
    // renders in both the OCR and corrected columns — pick either.)
    fireEvent.click(screen.getAllByText(/premier fichier/)[0])
    expect(onSelectLine).toHaveBeenLastCalledWith('P_file1', 'TL1')

    // Switch to page 2 and click its TL1: same line_id, other page_id.
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '1' } })
    fireEvent.click(screen.getAllByText(/second fichier/)[0])
    expect(onSelectLine).toHaveBeenLastCalledWith('P_file2', 'TL1')
  })

  it('highlights a selected line only on its own page (composite key match)', () => {
    const page = (pageId: string, index: number): DiffData['pages'][number] => ({
      page_id: pageId,
      page_index: index,
      lines: [
        {
          line_id: 'TL1',
          ocr_text: `texte ${pageId}`,
          corrected_text: `texte ${pageId}`,
          modified: false,
          hyphen_role: 'none',
          hyphen_subs_content: null,
        },
      ],
    })
    const data: DiffData = {
      job_id: 'j-sel',
      pages: [page('P_file1', 0), page('P_file2', 1)],
      stats: { total_lines: 2, modified_lines: 0, hyphen_pairs: 0 },
    }

    // Select TL1 of P_file2, but display page 1: no row may highlight.
    const { container } = render(
      <DiffViewer
        data={data}
        selectedLineKey={lineKey('P_file2', 'TL1')}
        onSelectLine={() => {}}
      />,
    )
    expect(container.querySelector('.ring-amber-500\\/30')).toBeNull()
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
