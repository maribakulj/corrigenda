import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ErrorBoundary } from './ErrorBoundary'

// React logs caught render errors (and componentDidCatch console.errors on
// purpose) — silence them so test output stays readable.
beforeEach(() => {
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  vi.restoreAllMocks()
})

let shouldThrow = true

function Bomb() {
  if (shouldThrow) throw new Error('kaboom in render')
  return <div>recovered content</div>
}

describe('ErrorBoundary', () => {
  it('renders its children when nothing throws', () => {
    render(
      <ErrorBoundary>
        <div>all good</div>
      </ErrorBoundary>,
    )
    expect(screen.getByText('all good')).toBeInTheDocument()
  })

  it('catches a render error and shows the message', () => {
    shouldThrow = true
    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>,
    )
    expect(screen.getByText(/something went wrong/i)).toBeInTheDocument()
    expect(screen.getByText('kaboom in render')).toBeInTheDocument()
    // componentDidCatch logged the error for diagnostics.
    expect(console.error).toHaveBeenCalled()
  })

  it('re-renders the children after "Try again"', () => {
    shouldThrow = true
    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>,
    )
    expect(screen.getByText(/something went wrong/i)).toBeInTheDocument()

    // The underlying cause is gone — retry must escape the error state.
    shouldThrow = false
    fireEvent.click(screen.getByRole('button', { name: /try again/i }))
    expect(screen.getByText('recovered content')).toBeInTheDocument()
    expect(screen.queryByText(/something went wrong/i)).not.toBeInTheDocument()
  })
})
