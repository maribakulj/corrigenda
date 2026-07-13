import { render, screen } from '@testing-library/react'
import { beforeAll, describe, expect, it, vi } from 'vitest'

import type { LogEntry } from '../types'
import { LogPanel } from './LogPanel'

// jsdom has no scrollIntoView; the panel auto-scrolls on every log append.
beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn()
})

function entry(id: string, type: LogEntry['type'], message: string): LogEntry {
  return { id, type, message, timestamp: new Date('2026-07-13T10:20:30') }
}

describe('LogPanel', () => {
  it('shows the empty state with a zero count', () => {
    render(<LogPanel logs={[]} />)
    expect(screen.getByText(/no events yet/i)).toBeInTheDocument()
    expect(screen.getByText('0 entries')).toBeInTheDocument()
  })

  it('renders one row per entry with its type icon and timestamp', () => {
    const logs: LogEntry[] = [
      entry('1', 'info', 'job queued'),
      entry('2', 'warning', 'retrying chunk'),
      entry('3', 'error', 'provider exploded'),
      entry('4', 'success', 'all done'),
    ]
    render(<LogPanel logs={logs} />)

    expect(screen.getByText('4 entries')).toBeInTheDocument()
    expect(screen.getByText('job queued')).toBeInTheDocument()
    expect(screen.getByText('retrying chunk')).toBeInTheDocument()
    expect(screen.getByText('provider exploded')).toBeInTheDocument()
    expect(screen.getByText('all done')).toBeInTheDocument()

    // One icon per log type.
    expect(screen.getByText('·')).toBeInTheDocument()
    expect(screen.getByText('▲')).toBeInTheDocument()
    expect(screen.getByText('✕')).toBeInTheDocument()
    expect(screen.getByText('✓')).toBeInTheDocument()

    // Timestamps are HH:MM:SS (local time of the fixed Date).
    expect(screen.getAllByText('10:20:30')).toHaveLength(4)
  })

  it('auto-scrolls to the newest entry', () => {
    const spy = window.HTMLElement.prototype.scrollIntoView as ReturnType<typeof vi.fn>
    spy.mockClear()
    render(<LogPanel logs={[entry('1', 'info', 'x')]} />)
    expect(spy).toHaveBeenCalledWith({ behavior: 'smooth' })
  })
})
