import js from '@eslint/js'
import { defineConfig, globalIgnores } from 'eslint/config'
import globals from 'globals'
import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'

export default defineConfig([
  globalIgnores(['dist', 'coverage', 'playwright-report', 'test-results']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [js.configs.recommended, tseslint.configs.recommended],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
    rules: { '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }] },
  },
  {
    files: ['src/**/*.tsx'],
    extends: [reactHooks.configs.flat.recommended, reactRefresh.configs.vite],
  },
  {
    files: ['src/**/*.{ts,tsx}'],
    ignores: ['src/api/**', 'src/**/*.test.*', 'src/test/**'],
    rules: {
      'no-restricted-globals': ['error', {
        name: 'fetch', message: 'API 호출은 src/api의 typed client를 사용하세요.',
      }, {
        name: 'XMLHttpRequest', message: 'API 호출은 src/api의 typed client를 사용하세요.',
      }],
      'no-restricted-properties': ['error', {
        property: 'fetch', message: 'API 호출은 src/api의 typed client를 사용하세요.',
      }],
    },
  },
])
