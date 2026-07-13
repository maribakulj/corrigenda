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
      include: ['src/**/*.{ts,tsx}'],
      // The generated OpenAPI types carry no runtime logic; the drift CI
      // job is their gate, not unit coverage.
      exclude: [
        'src/main.tsx',
        'src/**/*.d.ts',
        'src/tests/**',
        'src/types/api.generated.ts',
      ],
      // Audit-F37 — coverage was collected but NEVER gated (unlike the
      // backend's fail_under=80). These thresholds GATE it now, set to
      // the current measured floor so CI stays green while blocking any
      // regression. NOTE: the audit's aspirational target is 70%; getting
      // there needs more component/hook tests (App effects, the SSE
      // reconnect ladder, viewers) — ratchet these up as they land rather
      // than red-lining CI today.
      thresholds: {
        statements: 48,
        branches: 32,
        functions: 38,
        lines: 50,
      },
    },
  },
})
