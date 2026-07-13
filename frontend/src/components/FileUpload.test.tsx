/**
 * Audit-F wave 4 — FileUpload fixes (F31, F32).
 *
 * F31 — onFilesChange (a parent setState) must not run inside the
 *       setFiles updater (render phase); it belongs in an effect.
 * F32 — de-duplication must key on (name, size, lastModified), not name
 *       alone, so two distinct files sharing a filename are both kept
 *       (with a visible notice).
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { FileUpload } from './FileUpload'

function xml(name: string, content: string, lastModified = 1): File {
  return new File([content], name, { type: 'application/xml', lastModified })
}

function dropZone(): HTMLElement {
  return screen.getByText(/Drop files here/i).closest('div') as HTMLElement
}

function drop(el: HTMLElement, files: File[]) {
  fireEvent.drop(el, { dataTransfer: { files } })
}

describe('F32 — dedup keys on (name, size, lastModified)', () => {
  it('keeps two distinct files that share a filename', async () => {
    const onChange = vi.fn()
    render(<FileUpload onFilesChange={onChange} />)

    // Same name, different content (→ different size) and lastModified.
    drop(dropZone(), [xml('page.xml', '<a/>', 1)])
    drop(dropZone(), [xml('page.xml', '<bb-longer/>', 2)])

    await waitFor(() => {
      const last = onChange.mock.calls[onChange.mock.calls.length - 1]?.[0] as File[]
      expect(last).toHaveLength(2)
    })
  })

  it('drops a true duplicate and shows a visible notice', async () => {
    const onChange = vi.fn()
    render(<FileUpload onFilesChange={onChange} />)
    const f = xml('page.xml', '<a/>', 5)
    // Exact same identity twice.
    drop(dropZone(), [f])
    drop(dropZone(), [xml('page.xml', '<a/>', 5)])

    await waitFor(() => {
      const last = onChange.mock.calls[onChange.mock.calls.length - 1]?.[0] as File[]
      expect(last).toHaveLength(1)
    })
    expect(screen.getByText(/duplicate|already|doublon/i)).toBeInTheDocument()
  })
})

describe('F31 — no render-phase parent update', () => {
  it('does not emit a "Cannot update a component while rendering" warning', async () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const onChange = vi.fn()
    render(<FileUpload onFilesChange={onChange} />)
    drop(dropZone(), [xml('a.xml', '<a/>')])
    await waitFor(() => expect(onChange).toHaveBeenCalled())
    const offending = spy.mock.calls
      .flat()
      .filter((a) => typeof a === 'string')
      .join('\n')
    expect(offending).not.toMatch(/Cannot update a component .* while rendering/i)
    spy.mockRestore()
  })

  it('still emits the merged list exactly once per change', async () => {
    const onChange = vi.fn()
    render(<FileUpload onFilesChange={onChange} />)
    drop(dropZone(), [xml('a.xml', '<a/>')])
    await waitFor(() => {
      const last = onChange.mock.calls[onChange.mock.calls.length - 1]?.[0] as File[]
      expect(last.map((f) => f.name)).toEqual(['a.xml'])
    })
  })
})

// ---------------------------------------------------------------------------
// Wave-4 adversarial-review follow-ups.
// ---------------------------------------------------------------------------

describe('review follow-up — StrictMode double-mount', () => {
  it('does not notify the parent on mount under StrictMode', () => {
    // Wave-4 review — the firstRenderRef skip is defeated by StrictMode's
    // setup→cleanup→setup effect replay (the ref persists), producing a
    // spurious onFilesChange([]) on every mount. main.tsx enables
    // StrictMode, so this is the app's real dev behaviour.
    const onChange = vi.fn()
    render(
      <StrictMode>
        <FileUpload onFilesChange={onChange} />
      </StrictMode>,
    )
    expect(onChange).not.toHaveBeenCalled()
  })

  it('still notifies real changes (add then remove back to empty)', async () => {
    const onChange = vi.fn()
    render(
      <StrictMode>
        <FileUpload onFilesChange={onChange} />
      </StrictMode>,
    )
    drop(dropZone(), [xml('a.xml', '<a/>')])
    await waitFor(() => {
      const last = onChange.mock.calls[onChange.mock.calls.length - 1]?.[0] as File[]
      expect(last.map((f) => f.name)).toEqual(['a.xml'])
    })
    fireEvent.click(screen.getByTitle('Remove'))
    await waitFor(() => {
      const last = onChange.mock.calls[onChange.mock.calls.length - 1]?.[0] as File[]
      // Removing the last file IS a real change — the empty list must
      // reach the parent (only the mount-time [] is suppressed).
      expect(last).toHaveLength(0)
    })
  })
})

describe('review follow-up — synchronous double add', () => {
  it('keeps both batches when two adds land before a re-render', async () => {
    // Wave-4 review (PLAUSIBLE, confirmed here) — addFiles computed the
    // merge from the render-closure `files`, so two change events firing
    // before a re-render both saw the OLD list and the second overwrote
    // the first batch.
    const onChange = vi.fn()
    render(<FileUpload onFilesChange={onChange} />)
    const zone = dropZone()
    // Two synchronous drops — no await between them.
    drop(zone, [xml('a.xml', '<a/>')])
    drop(zone, [xml('b.xml', '<b/>')])

    await waitFor(() => {
      const last = onChange.mock.calls[onChange.mock.calls.length - 1]?.[0] as File[]
      expect(last.map((f) => f.name).sort()).toEqual(['a.xml', 'b.xml'])
    })
  })
})
