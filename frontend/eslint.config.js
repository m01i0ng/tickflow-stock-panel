/**
 * 规则选择原则:
 *  - noUnusedLocals/noUnusedParameters 已由 tsc -b 拦截, eslint 只兜底阶段式误报
 *  - react-hooks 规则防闭包依赖错误 (tsc 查不出来)
 *  - 未启用 stylistic 规则 (历史代码风格统一由改动最小化原则约束)
 */
import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'
import globals from 'globals'

export default tseslint.config(
  // vite/vitest 的 .js / .d.ts 是 tsc -b 的编译产物 (不入库), 不参与 lint
  {
    ignores: [
      'dist/**',
      'vite.config.ts',
      'vite.config.js',
      'vite.config.d.ts',
      'vitest.config.ts',
      'vitest.config.js',
      'vitest.config.d.ts',
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: { globals: globals.browser },
    plugins: { 'react-hooks': reactHooks },
    rules: reactHooks.configs['recommended-latest'].rules,
  },
  {
    rules: {
      // 未使用变量由 tsc 编译期保证; 但解构省略字段 (const { x, ...rest }) 与
      // _ 前缀弃用绑定 (runtime_warning: _runtimeWarning) 是存量惯用法, 放行。
      'no-unused-vars': 'off',
      '@typescript-eslint/no-unused-vars': ['error', {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_',
        ignoreRestSiblings: true,
      }],
      // 存量代码大量使用 any（动态 DTO/扩展列等场景）。
      // 全量消除属于类型重构（单独开展），此处降级为 warn 跟踪数量，不阻塞 CI。
      '@typescript-eslint/no-explicit-any': 'warn',
    },
  },
)