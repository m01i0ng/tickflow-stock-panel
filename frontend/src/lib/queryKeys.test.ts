/**
 * Query key 集中管理契约测试 — 防止在组件里拼出平行键、破坏 SSE 失效范围。
 *
 * 断言的是 queryKeys.ts 头注释里声明的设计约束:
 *  - 分钟批量键刻意不带 watchlist- 前缀 (躲 SSE 高频失效限流)
 *  - screener-cached 系列不进 SSE 失效列表 (防策略页逐 tick 双刷闪烁)
 *  - SSE_INVALIDATE_PREFIXES 里每个前缀都有真实的 QK 条目落地
 */
import { describe, it, expect } from 'vitest'
import { QK, SSE_INVALIDATE_PREFIXES } from './queryKeys'

describe('SSE 失效前缀与查询键对齐', () => {
  it('每个 SSE 失效前缀都有至少一个 QK 条目实际使用', () => {
    const allFirst: string[] = []
    for (const factory of Object.values(QK)) {
      const key = typeof factory === 'function'
        ? (factory as (...args: unknown[]) => unknown)('测试入参')
        : factory
      const first = Array.isArray(key) ? key[0] : undefined
      if (typeof first === 'string') allFirst.push(first)
    }
    for (const prefix of SSE_INVALIDATE_PREFIXES) {
      expect(allFirst).toContain(prefix)
    }
  })

  it('watchlistEnriched 走 watchlist- 前缀, 会被 SSE 失效', () => {
    const key = QK.watchlistEnriched('ext1')
    expect(key[0]).toBe('watchlist-enriched')
    expect(SSE_INVALIDATE_PREFIXES.some(p => String(key[0]).startsWith(p))).toBe(true)
  })

  it('minutes 批量键刻意不带 watchlist 前缀 (避开高频限流失效)', () => {
    const key = QK.minuteBatch('000001.SZ,000002.SZ')
    expect(key[0]).toBeDefined()
    for (const prefix of SSE_INVALIDATE_PREFIXES) {
      expect(String(key[0]).startsWith(prefix)).toBe(false)
    }
  })

  it('screener-cached 不进 SSE 失效列表 (防逐 tick 双刷闪烁)', () => {
    const key = QK.screenerCachedSummary
    expect(key[0]).toBe('screener-cached')
    for (const prefix of SSE_INVALIDATE_PREFIXES) {
      expect(String(key[0]).startsWith(prefix)).toBe(false)
    }
  })

  it('regime 等日级离线数据同样不受 SSE 失效影响', () => {
    for (const prefix of SSE_INVALIDATE_PREFIXES) {
      expect(String(QK.regimeLatest[0]).startsWith(prefix)).toBe(false)
    }
  })
})

describe('查询键形状契约', () => {
  it('参数进入查询键 (防止跨上下文合计缓存)', () => {
    expect(QK.kline('000001.SZ', '2024-01-01', '2024-01-31')).toEqual([
      'kline', '000001.SZ', '2024-01-01', '2024-01-31', '',
    ])
    expect(QK.kline('000002.SZ', '2024-01-01', '2024-01-31')[1]).toBe('000002.SZ')
    expect(QK.alerts('depth')[1]).toBe('depth')
    expect(QK.regimeHistory(30)[1]).toBe(30)
  })

  it('同一资产类型/参数变化产生不同 key (序列化差异可被结构共享捕获)', () => {
    expect(QK.kline('000001.SZ', '2024-01-01', '2024-01-31'))
      .not.toEqual(QK.kline('000001.SZ', '2024-01-02', '2024-01-31'))
  })
})