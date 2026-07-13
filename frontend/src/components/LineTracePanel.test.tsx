import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { LineTrace } from '../types'
import { LineTracePanel } from './LineTracePanel'

const baseTrace: LineTrace = {
  line_id: 'P1_TL04',
  page_id: 'P1',
  source_ocr_text: 'La Fravce est graude',
  model_input_text: 'La Fravce est graude',
  model_corrected_text: 'La France est grande',
  projected_text: 'La France est grande',
  output_alto_text: 'La France est grande',
  hyphen_role: 'HypPart1',
  rewriter_path: 'slow_path',
  validation_status: 'corrected',
  fallback_reason: null,
}

describe('LineTracePanel', () => {
  it('renders the five pipeline stages in order with their texts', () => {
    render(<LineTracePanel trace={baseTrace} onClose={vi.fn()} />)
    expect(screen.getByText(/1\. Source OCR/)).toBeInTheDocument()
    expect(screen.getByText(/2\. Model input/)).toBeInTheDocument()
    expect(screen.getByText(/3\. Model output/)).toBeInTheDocument()
    expect(screen.getByText(/4\. Projected/)).toBeInTheDocument()
    expect(screen.getByText(/5\. Output ALTO/)).toBeInTheDocument()
    expect(screen.getAllByText('La Fravce est graude')).toHaveLength(2)
    expect(screen.getAllByText('La France est grande')).toHaveLength(3)
    expect(screen.getByText('page: P1')).toBeInTheDocument()
  })

  it('marks exactly the stages whose text differs from the previous one', () => {
    render(<LineTracePanel trace={baseTrace} onClose={vi.fn()} />)
    // input==source, corrected!=input, projected==corrected, output==projected
    expect(screen.getAllByText('changed')).toHaveLength(1)
  })

  it('shows the header badges for role, status and rewriter path', () => {
    render(<LineTracePanel trace={baseTrace} onClose={vi.fn()} />)
    expect(screen.getByText('P1_TL04')).toBeInTheDocument()
    expect(screen.getByText('HypPart1')).toBeInTheDocument()
    expect(screen.getByText('corrected')).toBeInTheDocument()
    expect(screen.getByText('slow_path')).toBeInTheDocument()
  })

  it('renders null stages as "(not captured)" with a null badge and no changed flag', () => {
    const t: LineTrace = {
      ...baseTrace,
      model_input_text: null,
      model_corrected_text: null,
      projected_text: null,
      output_alto_text: null,
    }
    render(<LineTracePanel trace={t} onClose={vi.fn()} />)
    expect(screen.getAllByText('(not captured)')).toHaveLength(4)
    expect(screen.getAllByText('null')).toHaveLength(4)
    // textsDiffer is null-safe: nothing is flagged as changed.
    expect(screen.queryByText('changed')).not.toBeInTheDocument()
  })

  it('shows the fallback banner only when a fallback_reason exists', () => {
    const { unmount } = render(
      <LineTracePanel
        trace={{
          ...baseTrace,
          validation_status: 'fallback',
          fallback_reason: 'absorbs_next_line',
        }}
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByText(/Fallback: absorbs_next_line/)).toBeInTheDocument()
    unmount()

    render(<LineTracePanel trace={baseTrace} onClose={vi.fn()} />)
    expect(screen.queryByText(/Fallback:/)).not.toBeInTheDocument()
  })

  it('falls back to placeholder badges when role/status/path are null', () => {
    const t: LineTrace = {
      ...baseTrace,
      hyphen_role: null,
      rewriter_path: null,
      validation_status: null,
    }
    render(<LineTracePanel trace={t} onClose={vi.fn()} />)
    expect(screen.getByText('none')).toBeInTheDocument()
    expect(screen.getByText('pending')).toBeInTheDocument()
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it.each([
    ['failed', 'HypPart2', 'fast_path'],
    ['fallback', 'HypBoth', 'subs_only'],
  ] as const)('renders the %s/%s/%s badge variants', (status, role, path) => {
    render(
      <LineTracePanel
        trace={{ ...baseTrace, validation_status: status, hyphen_role: role, rewriter_path: path }}
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
