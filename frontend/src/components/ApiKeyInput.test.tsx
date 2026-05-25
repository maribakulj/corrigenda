/**
 * Tests for ApiKeyInput.
 *
 * Critical invariants this component must maintain:
 *  - Field is `type="password"` by default (no shoulder-surf leak).
 *  - autoComplete is off (no browser autofill stash).
 *  - Toggle button flips visibility on click.
 *  - localStorage is NEVER touched (no key persistence outside React state).
 *  - onChange is called with the typed value.
 *  - Disabled prop reaches both the input and the toggle.
 */
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { ApiKeyInput } from './ApiKeyInput'

describe('ApiKeyInput', () => {
  it('renders as type="password" by default', () => {
    render(<ApiKeyInput value="" onChange={() => {}} />)
    const input = screen.getByLabelText(/api key/i) as HTMLInputElement
    expect(input.type).toBe('password')
  })

  it('disables browser autocomplete', () => {
    render(<ApiKeyInput value="" onChange={() => {}} />)
    const input = screen.getByLabelText(/api key/i)
    expect(input).toHaveAttribute('autocomplete', 'off')
  })

  it('toggles visibility when the eye button is clicked', async () => {
    const user = userEvent.setup()
    render(<ApiKeyInput value="sk-secret" onChange={() => {}} />)
    const input = screen.getByLabelText(/api key/i) as HTMLInputElement
    expect(input.type).toBe('password')

    const toggle = screen.getByTitle(/show key/i)
    await user.click(toggle)
    expect(input.type).toBe('text')

    const hide = screen.getByTitle(/hide key/i)
    await user.click(hide)
    expect(input.type).toBe('password')
  })

  it('calls onChange with the typed value', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<ApiKeyInput value="" onChange={onChange} />)
    await user.type(screen.getByLabelText(/api key/i), 'sk-xyz')
    // userEvent.type emits one onChange per character — last call holds
    // the final character. We don't assert on intermediate calls.
    expect(onChange).toHaveBeenCalled()
    expect(onChange).toHaveBeenLastCalledWith('z')
  })

  it('never writes the value to localStorage', async () => {
    const user = userEvent.setup()
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    render(<ApiKeyInput value="" onChange={() => {}} />)
    await user.type(screen.getByLabelText(/api key/i), 'sk-leak-test')
    expect(setItem).not.toHaveBeenCalled()
    setItem.mockRestore()
  })

  it('disables the input and toggle when disabled is true', () => {
    render(<ApiKeyInput value="x" onChange={() => {}} disabled />)
    expect(screen.getByLabelText(/api key/i)).toBeDisabled()
    expect(screen.getByTitle(/show key/i)).toBeDisabled()
  })
})
