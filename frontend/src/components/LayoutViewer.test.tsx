import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { LayoutBlock, LayoutData, LayoutLine, LayoutPage } from '../types'
import { LayoutViewer } from './LayoutViewer'

// ---------------------------------------------------------------------------
// Builders
// ---------------------------------------------------------------------------

function line(over: Partial<LayoutLine> = {}): LayoutLine {
  return {
    line_id: 'L1',
    hpos: 10,
    vpos: 10,
    width: 300,
    height: 20,
    ocr_text: 'texte ocr',
    corrected_text: 'texte corrigé',
    modified: false,
    hyphen_role: 'none',
    ...over,
  }
}

function block(lines: LayoutLine[], over: Partial<LayoutBlock> = {}): LayoutBlock {
  return { block_id: 'B1', hpos: 5, vpos: 5, width: 400, height: 200, lines, ...over }
}

function page(blocks: LayoutBlock[], over: Partial<LayoutPage> = {}): LayoutPage {
  return {
    page_id: 'p1',
    page_index: 0,
    page_width: 500,
    page_height: 700,
    image_url: null,
    blocks,
    ...over,
  }
}

function data(pages: LayoutPage[]): LayoutData {
  return { job_id: 'j1', pages }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('LayoutViewer', () => {
  it('shows the empty message when there are no pages', () => {
    render(<LayoutViewer data={data([])} />)
    expect(screen.getByText(/aucune mise en page/i)).toBeInTheDocument()
  })

  it('reports missing coordinates instead of rendering an empty SVG', () => {
    const p = page([], { page_width: 0, page_height: 0 })
    render(<LayoutViewer data={data([p])} />)
    // Both panels (OCR + corrected) fall back to the explanatory message.
    expect(screen.getAllByText(/coordonnées alto absentes/i)).toHaveLength(2)
  })

  it('renders OCR text on the left panel and corrected text on the right', () => {
    const p = page([block([line({ modified: true })])])
    const { container } = render(<LayoutViewer data={data([p])} />)
    expect(container.querySelectorAll('svg')).toHaveLength(2)
    expect(screen.getByText('texte ocr')).toBeInTheDocument()
    expect(screen.getByText('texte corrigé')).toBeInTheDocument()
    // Legend present.
    expect(screen.getByText('ligne modifiée')).toBeInTheDocument()
    expect(screen.getByText('césure')).toBeInTheDocument()
  })

  it('derives page size from blocks when page_width/height are missing', () => {
    const p = page([block([line()])], { page_width: 0, page_height: 0 })
    const { container } = render(<LayoutViewer data={data([p])} />)
    const svg = container.querySelector('svg')
    // W = block.hpos + block.width = 405, H = 205
    expect(svg?.getAttribute('viewBox')).toBe('0 0 405 205')
  })

  it('draws the hyphen bar and the modified highlight in SVG-only mode', () => {
    const p = page([
      block([
        line({ line_id: 'Lmod', modified: true }),
        line({ line_id: 'Lhyp', vpos: 40, hyphen_role: 'HypPart1' }),
      ]),
    ])
    const { container } = render(<LayoutViewer data={data([p])} />)
    const rects = Array.from(container.querySelectorAll('rect'))
    // Modified-line highlight (per panel).
    expect(rects.filter((r) => r.getAttribute('fill') === 'rgba(253,230,138,0.25)')).toHaveLength(2)
    // Hyphen bar: fixed 8px-wide amber rect (per panel).
    const bars = rects.filter(
      (r) => r.getAttribute('fill') === '#f59e0b' && r.getAttribute('width') === '8',
    )
    expect(bars).toHaveLength(2)
  })

  it('omits textLength on lines too narrow to justify', () => {
    const p = page([block([line({ width: 6 })])])
    const { container } = render(<LayoutViewer data={data([p])} />)
    const texts = Array.from(container.querySelectorAll('text'))
    expect(texts.length).toBeGreaterThan(0)
    for (const t of texts) {
      expect(t.getAttribute('textLength')).toBeNull()
    }
  })

  it('updates the overlay opacity from the slider', () => {
    const p = page([block([line()])])
    const { container } = render(<LayoutViewer data={data([p])} />)
    expect(screen.getByText('85%')).toBeInTheDocument()

    fireEvent.change(container.querySelector('input[type="range"]')!, { target: { value: '40' } })

    expect(screen.getByText('40%')).toBeInTheDocument()
    const groups = Array.from(container.querySelectorAll('svg > g'))
    expect(groups.some((g) => g.getAttribute('opacity') === '0.4')).toBe(true)
  })

  it('switches pages through the selector when several pages exist', () => {
    const p1 = page([block([line({ ocr_text: 'page un' })])])
    const p2 = page([block([line({ line_id: 'L2', ocr_text: 'page deux' })])], {
      page_id: 'p2',
      page_index: 1,
    })
    render(<LayoutViewer data={data([p1, p2])} />)

    expect(screen.getByText('page un')).toBeInTheDocument()
    expect(screen.queryByText('page deux')).not.toBeInTheDocument()

    fireEvent.change(screen.getByRole('combobox'), { target: { value: '1' } })

    expect(screen.getByText('page deux')).toBeInTheDocument()
    expect(screen.queryByText('page un')).not.toBeInTheDocument()
  })

  it('hides the page selector for a single page', () => {
    render(<LayoutViewer data={data([page([block([line()])])])} />)
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })

  it('renders the scan image behind a transparent overlay when image_url is set', () => {
    const p = page([block([line({ modified: true })])], { image_url: '/img/p1.jpg' })
    const { container } = render(<LayoutViewer data={data([p])} />)

    const imgs = Array.from(container.querySelectorAll('img'))
    expect(imgs).toHaveLength(2)
    expect(imgs[0]).toHaveAttribute('src', '/img/p1.jpg')
    // Column labels flag scan mode.
    expect(screen.getByText(/ocr source \(scan\)/i)).toBeInTheDocument()

    // Overlay mode: line background is the semi-opaque readability rect.
    const rects = Array.from(container.querySelectorAll('rect'))
    expect(rects.some((r) => r.getAttribute('fill') === 'rgba(251,191,36,0.70)')).toBe(true)
  })

  it('synchronises scroll positions between the two panels', () => {
    const p = page([block([line()])])
    const { container } = render(<LayoutViewer data={data([p])} />)
    const [left, right] = Array.from(container.querySelectorAll('.overflow-auto'))

    fireEvent.scroll(left, { target: { scrollTop: 120 } })
    expect(right.scrollTop).toBe(120)

    fireEvent.scroll(right, { target: { scrollTop: 40 } })
    expect(left.scrollTop).toBe(40)
  })
})
