import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { LineOutcome } from '../types'
import { LineTracePanel } from './LineTracePanel'

const baseTrace: LineOutcome = {
  line_id: 'P1_TL04',
  page_id: 'P1',
  hyphen_role: 'HypPart1',
  source_text: 'La Fravce est graude',
  proposal: {
    input_text: 'La Fravce est graude',
    output_text: 'La France est grande',
  },
  decision: {
    status: 'corrected',
    final_text: 'La France est grande',
    reason: null,
    features: null,
  },
  projection: { extracted_text: 'La France est grande', rewriter_path: 'slow_path' },
}

describe('LineTracePanel', () => {
  it('renders the five pipeline stages in order with their texts', () => {
    render(<LineTracePanel trace={baseTrace} onClose={vi.fn()} />)
    expect(screen.getByText(/1\. Source/)).toBeInTheDocument()
    expect(screen.getByText(/2\. Producer input/)).toBeInTheDocument()
    expect(screen.getByText(/3\. Producer output/)).toBeInTheDocument()
    expect(screen.getByText(/4\. Decision \(retained\)/)).toBeInTheDocument()
    expect(screen.getByText(/5\. Output \(extracted\)/)).toBeInTheDocument()
    expect(screen.getAllByText('La Fravce est graude')).toHaveLength(2)
    expect(screen.getAllByText('La France est grande')).toHaveLength(3)
    expect(screen.getByText('page: P1')).toBeInTheDocument()
  })

  it('marks exactly the stages whose text differs from the previous one', () => {
    render(<LineTracePanel trace={baseTrace} onClose={vi.fn()} />)
    // input==source, output!=input, decision==output, extracted==decision
    expect(screen.getAllByText('changed')).toHaveLength(1)
  })

  it('shows the header badges for role, status and rewriter path', () => {
    render(<LineTracePanel trace={baseTrace} onClose={vi.fn()} />)
    expect(screen.getByText('P1_TL04')).toBeInTheDocument()
    expect(screen.getByText('HypPart1')).toBeInTheDocument()
    expect(screen.getByText('corrected')).toBeInTheDocument()
    expect(screen.getByText('slow_path')).toBeInTheDocument()
  })

  it('renders absent stages as "(not captured)" with a null badge and no changed flag', () => {
    const t: LineOutcome = {
      ...baseTrace,
      proposal: null,
      projection: null,
    }
    render(<LineTracePanel trace={t} onClose={vi.fn()} />)
    // Producer input, producer output and extracted text are absent —
    // source and the (always present) decision text remain.
    expect(screen.getAllByText('(not captured)')).toHaveLength(3)
    expect(screen.getAllByText('null')).toHaveLength(3)
    // textsDiffer is null-safe: nothing is flagged as changed.
    expect(screen.queryByText('changed')).not.toBeInTheDocument()
  })

  it('shows the fallback banner only when a structured reason exists', () => {
    const { unmount } = render(
      <LineTracePanel
        trace={{
          ...baseTrace,
          decision: {
            status: 'fallback',
            final_text: 'La Fravce est graude',
            reason: { code: 'absorbs_next_line', detail: null },
            features: null,
          },
        }}
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByText(/Fallback: absorbs_next_line/)).toBeInTheDocument()
    unmount()

    render(<LineTracePanel trace={baseTrace} onClose={vi.fn()} />)
    expect(screen.queryByText(/Fallback:/)).not.toBeInTheDocument()
  })

  it('renders the reason detail after the code when present', () => {
    render(
      <LineTracePanel
        trace={{
          ...baseTrace,
          decision: {
            status: 'fallback',
            final_text: 'La Fravce est graude',
            reason: { code: 'too_different_from_source', detail: '0.42 < 0.75' },
            features: null,
          },
        }}
        onClose={vi.fn()}
      />,
    )
    expect(
      screen.getByText(/Fallback: too_different_from_source: 0\.42 < 0\.75/),
    ).toBeInTheDocument()
  })

  it('falls back to placeholder badges when role/path are absent', () => {
    const t: LineOutcome = {
      ...baseTrace,
      hyphen_role: null,
      projection: null,
    }
    render(<LineTracePanel trace={t} onClose={vi.fn()} />)
    expect(screen.getByText('none')).toBeInTheDocument()
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it.each([
    ['failed', 'HypPart2', 'fast_path'],
    ['fallback', 'HypBoth', 'subs_only'],
  ] as const)('renders the %s/%s/%s badge variants', (status, role, path) => {
    render(
      <LineTracePanel
        trace={{
          ...baseTrace,
          hyphen_role: role,
          decision: { ...baseTrace.decision, status },
          projection: { extracted_text: 'x', rewriter_path: path },
        }}
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByText(status)).toBeInTheDocument()
    expect(screen.getByText(role)).toBeInTheDocument()
    expect(screen.getByText(path)).toBeInTheDocument()
  })

  it('invokes onClose from the close button', () => {
    const onClose = vi.fn()
    render(<LineTracePanel trace={baseTrace} onClose={onClose} />)
    fireEvent.click(screen.getByRole('button', { name: /close trace panel/i }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
