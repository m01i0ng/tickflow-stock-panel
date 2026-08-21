import { defineConfig } from 'vitest/config'
import path from 'node:path'

export default defineConfig({
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
    // 测试里用真实的 fetch/Response/ReadableStream (Node 18+ 全局可用),
    // 不需要 happy-dom/jsdom。
    pool: 'threads',
  },
})