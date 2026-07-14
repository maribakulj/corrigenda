import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { ModelInfo } from '../types'
import { ModelSelector } from './ModelSelector'

const models: ModelInfo[] = [
  { id: 'big', label: 'Big Model', supports_structured_output: true, context_window: 200000 },
  { id: 'tiny', label: 'Tiny Model', supports_structured_output: true, context_window: 512 },
  { id: 'nolimit', label: 'No Ctx', supports_structured_output: false, context_window: null },
]

function renderSelector(over: Partial<React.ComponentProps<typeof ModelSelector>> = {}) {
  const props = {
    models,
    loading: false,
    selectedModel: null,
    onLoad: vi.fn(),
    onSelect: vi.fn(),
    ...over,
  }
  render(<ModelSelector {...props} />)
  return props
}

describe('ModelSelector', () => {
  it('formats context windows (k-suffix, raw, absent)', () => {
    renderSelector()
    expect(screen.getByRole('option', { name: 'Big Model (200k ctx)' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Tiny Model (512 ctx)' })).toBeInTheDocument()
    // No parenthesis when context_window is null.
    expect(screen.getByRole('option', { name: 'No Ctx' })).toBeInTheDocument()
  })

  it('disables the select and shows the hint while no models are loaded', () => {
    renderSelector({ models: [] })
    expect(screen.getByRole('combobox')).toBeDisabled()
    expect(screen.getByRole('option', { name: /load models first/i })).toBeInTheDocument()
  })

  it('shows the pick-one placeholder once models exist', () => {
    renderSelector()
    expect(screen.getByRole('combobox')).toBeEnabled()
    expect(screen.getByRole('option', { name: /select a model/i })).toBeInTheDocument()
  })

  it('fires onSelect with the chosen model id', () => {
    const props = renderSelector()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'tiny' } })
    expect(props.onSelect).toHaveBeenCalledWith('tiny')
  })

  it('fires onLoad from the Load models button', () => {
    const props = renderSelector()
    fireEvent.click(screen.getByRole('button', { name: /load models/i }))
    expect(props.onLoad).toHaveBeenCalledTimes(1)
  })

  it('shows the spinner label and blocks re-clicks while loading', () => {
    const props = renderSelector({ loading: true })
    const button = screen.getByRole('button')
    expect(button).toHaveTextContent(/loading/i)
    expect(button).toBeDisabled()
    fireEvent.click(button)
    expect(props.onLoad).not.toHaveBeenCalled()
  })

  it('disables everything when the disabled prop is set', () => {
    renderSelector({ disabled: true })
    expect(screen.getByRole('combobox')).toBeDisabled()
    expect(screen.getByRole('button')).toBeDisabled()
  })
})
