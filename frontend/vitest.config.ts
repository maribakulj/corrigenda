/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vitest config kept separate from vite.config.ts so the dev server
// stays minimal and test infra changes don't ripple into prod builds.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/tests/setup.ts'],
    css: false, // we don't render Tailwind in tests
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html'],
      // Don't gate locally yet — the bar will rise in Stage 6.C once
      // the critical components have meaningful coverage.
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/main.tsx', 'src/**/*.d.ts', 'src/tests/**'],
    },
  },
})
