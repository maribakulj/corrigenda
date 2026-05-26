/**
 * Vitest setup file — runs before every test file.
 *
 * - `@testing-library/jest-dom` adds matchers like `toBeInTheDocument()`
 *   so tests read naturally against rendered DOM.
 * - The `cleanup` after each test removes mounted React components so
 *   one test can't leak DOM state into the next.
 */
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => {
  cleanup()
})
