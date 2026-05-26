// ESLint flat config — runs alongside the strict TypeScript checks
// in tsconfig.json. The TS compiler catches type errors; ESLint
// catches code-quality issues the compiler accepts (forgotten hooks
// deps, unused vars, suspicious patterns).
//
// Run:  npm run lint
// Fix:  npm run lint -- --fix
import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import prettier from 'eslint-config-prettier'
import globals from 'globals'

export default tseslint.config(
  {
    // Files ESLint should never visit.
    ignores: ['dist', 'node_modules', 'coverage', '.vite'],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser },
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      // React hooks correctness — exhaustive-deps catches the
      // single-most common useEffect bug.
      ...reactHooks.configs.recommended.rules,
      // Vite's fast-refresh requires components to be the only
      // export from a module; flag accidental sibling exports.
      'react-refresh/only-export-components': [
        'warn',
        { allowConstantExport: true },
      ],
      // TypeScript already enforces unused parameters via tsconfig
      // (noUnusedParameters). The ESLint rule's underscore-prefix
      // escape hatch matters when refactoring incrementally.
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
    },
  },
  // Prettier last — turns off any stylistic rules that would conflict
  // with the formatter so the two never argue.
  prettier,
)
