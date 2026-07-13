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
      // backend's fail_under=80). These thresholds GATE it at the audit's
      // target of 70%. The suite currently measures well above (95/90/92/97
      // stmts/branches/funcs/lines); the gap is headroom for legitimate
      // churn, not tolerated debt — never lower these to make CI pass.
      thresholds: {
        statements: 70,
        branches: 70,
        functions: 70,
        lines: 70,
      },
    },
  },
})
