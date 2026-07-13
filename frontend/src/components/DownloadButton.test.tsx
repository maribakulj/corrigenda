import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { JobStats } from '../types'
import { DownloadButton } from './DownloadButton'

// downloadJob triggers a real browser download — stub it out.
vi.mock('../api/client', () => ({
  downloadJob: vi.fn(),
}))

import { downloadJob } from '../api/client'

const stats: JobStats = {
  lines_modified: 12,
  hyphen_pairs: 3,
  duration_seconds: 4.56,
}

describe('DownloadButton', () => {
  it('renders the stats grid when stats are present', () => {
    render(<DownloadButton jobId="j1" stats={stats} />)
    expect(screen.getByText('12')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
    // duration_seconds formatted to one decimal
    expect(screen.getByText('4.6s')).toBeInTheDocument()
    expect(screen.getByText(/lines modified/i)).toBeInTheDocument()
  })

  it('omits the stats grid when stats are null', () => {
    render(<DownloadButton jobId="j1" stats={null} />)
    expect(screen.queryByText(/lines modified/i)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /download corrected alto/i })).toBeInTheDocument()
  })

  it('starts the download for its jobId on click', () => {
    render(<DownloadButton jobId="job-42" stats={null} />)
    fireEvent.click(screen.getByRole('button', { name: /download corrected alto/i }))
    expect(downloadJob).toHaveBeenCalledWith('job-42')
  })
})
