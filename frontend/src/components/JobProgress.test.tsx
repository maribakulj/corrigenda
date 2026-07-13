/**
 * Audit-F24 — JobProgress badge must render a label + colour for the
 * completed_with_fallbacks terminal state (and any unknown status),
 * never a blank badge with a literal `undefined` class.
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { JobProgress, JobStatus } from '../types'
import { JobProgressPanel } from './JobProgress'

const progress: JobProgress = {
  pages_total: 1,
  pages_done: 1,
  lines_total: 10,
  lines_done: 10,
  hyphen_pairs_total: 0,
  hyphen_pairs_reconciled: 0,
}

function badge(status: JobStatus | null) {
  const { container } = render(<JobProgressPanel progress={progress} status={status} />)
  return container.querySelector('span.font-mono') as HTMLElement
}

describe('F24 — status badge covers every terminal state', () => {
  it('renders a non-empty label for completed_with_fallbacks', () => {
    render(<JobProgressPanel progress={progress} status="completed_with_fallbacks" />)
    // A visible, non-empty degraded-success label.
    expect(screen.getByText(/fallback|repli|degrad/i)).toBeInTheDocument()
  })

  it('applies a real colour class (never the literal "undefined")', () => {
    const el = badge('completed_with_fallbacks')
    expect(el.className).not.toContain('undefined')
    expect(el.textContent?.trim()).not.toBe('')
  })

  it('degrades gracefully for an unknown status', () => {
    const el = badge('some_future_status' as JobStatus)
    expect(el.className).not.toContain('undefined')
    expect(el.textContent?.trim()).not.toBe('')
  })

  it('still renders the known statuses', () => {
    for (const s of ['queued', 'running', 'completed', 'failed'] as JobStatus[]) {
      const el = badge(s)
      expect(el.className).not.toContain('undefined')
      expect(el.textContent?.trim()).not.toBe('')
    }
  })

  it('renders a dash before any status is known', () => {
    const el = badge(null)
    expect(el.textContent?.trim()).toBe('—')
    expect(el.className).not.toContain('undefined')
  })
})

describe('progress arithmetic', () => {
  it('shows 0% (not NaN) before totals are known and dashes for empty counters', () => {
    render(
      <JobProgressPanel
        progress={{
          pages_total: 0,
          pages_done: 0,
          lines_total: 0,
          lines_done: 0,
          hyphen_pairs_total: 0,
          hyphen_pairs_reconciled: 0,
        }}
        status="queued"
      />,
    )
    expect(screen.getByText('0%')).toBeInTheDocument()
    // pages and lines denominators degrade to a dash; the hyphen counter
    // renders its whole cell as a dash.
    expect(screen.getAllByText('/—')).toHaveLength(2)
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('rounds the percentage and renders the hyphen counter when pairs exist', () => {
    render(
      <JobProgressPanel
        progress={{
          pages_total: 3,
          pages_done: 1,
          lines_total: 30,
          lines_done: 10,
          hyphen_pairs_total: 4,
          hyphen_pairs_reconciled: 2,
        }}
        status="running"
      />,
    )
    expect(screen.getByText('33%')).toBeInTheDocument()
    expect(screen.getByText('2')).toBeInTheDocument()
    expect(screen.getByText('/4')).toBeInTheDocument()
    expect(screen.getByText('Hyphen pairs')).toBeInTheDocument()
  })
})
