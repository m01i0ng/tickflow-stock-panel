import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { GitBranch, Loader2, RefreshCw } from 'lucide-react'
import { api, type ChanAnalysis, type ChanLevel, type KlineRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { AnalysisKChart, type ChanBiLine, type ChartMarker, type ChartRange, type ZsBand } from './AnalysisKChart'

/**
 * 缠论 (czsc) 多级别结构面板 —— 指数页专用。
 *
 * 数据契约: 图表 bars 与缠论结构来自同一次 /api/chan/analysis 响应(同口径、同窗口),
 * 不在前端与其它 K 线请求拼装(避免复权/窗口错位画出悬空笔画)。
 *
 * 加载模型:
 *  - 级别**按需加载**: 首屏只请求日线, 点击某级别按钮才发起该级别的请求(1F 数据量最大);
 *  - **盘中自动刷新**: A 股交易时段每 30s 轮询已加载级别, 后端按"最后一根 bar 指纹"
 *    自动失效重算(日线随盘中 quote 变化更新, 分钟级随盘后同步的分区变化更新);
 *  - 点击分钟级别且本地无 1m 时自动同步指数分钟K (TickFlow count 上限);
 *  - 手动刷新按钮: 立即失效所有已加载级别并重拉, 并允许再次自动同步。
 */

const DEFAULT_LEVELS = ['日线', '1分钟', '5分钟', '10分钟', '15分钟', '30分钟', '60分钟', '120分钟']
const MINUTE_FREQS = new Set(DEFAULT_LEVELS.filter(f => f !== '日线'))

function needsAutoMinuteSync(data: ChanAnalysis | undefined, freq: string): boolean {
  if (!data || data.available === false) return false
  if (!MINUTE_FREQS.has(freq)) return false
  if (data.levels.some(l => l.freq === freq)) return false
  return (data.warnings ?? []).some(w => w.includes('分钟K'))
}

const minuteSyncInflight = new Map<string, Promise<unknown>>()

function chanSyncMinuteShared(symbol: string) {
  const hit = minuteSyncInflight.get(symbol)
  if (hit) return hit
  const pending = api.chanSyncMinute(symbol).finally(() => {
    minuteSyncInflight.delete(symbol)
  })
  minuteSyncInflight.set(symbol, pending)
  return pending
}

const FREQ_SHORT: Record<string, string> = {
  日线: '日线', '1分钟': '1F', '5分钟': '5F', '10分钟': '10F', '15分钟': '15F',
  '30分钟': '30F', '60分钟': '60F', '120分钟': '120F',
}

const BULL = '#C74040'
const BEAR = '#2D9B65'
const POLL_MS_TRADING = 30_000
const TICK_MS = 20_000

/** A 股交易时段 (北京时间, 与后端 market_time 会话一致): 09:15-11:35 / 12:45-15:05, 周一至周五。 */
function isTradingHoursNow(): boolean {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai', hour12: false, hourCycle: 'h23',
    weekday: 'short', hour: '2-digit', minute: '2-digit',
  }).formatToParts(new Date())
  const get = (t: string) => parts.find(p => p.type === t)?.value ?? ''
  const wd = get('weekday')
  if (wd === 'Sat' || wd === 'Sun') return false
  const mins = Number(get('hour')) * 60 + Number(get('minute'))
  return (mins >= 9 * 60 + 15 && mins <= 11 * 60 + 35) || (mins >= 12 * 60 + 45 && mins <= 15 * 60 + 5)
}

export function ChanPanel({ symbol, supportedFreqs, height = 420, start, end }: {
  symbol: string
  /** 面板提供的级别按钮列表; 缺省为后端支持的全部级别。 */
  supportedFreqs?: string[]
  height?: number
  /** 指数页配置的起始/结束日期; 优先窗口, 根数不足时后端退回最大条数。 */
  start?: string
  end?: string
}) {
  const [open, setOpen] = useState(true)
  const [freqSel, setFreqSel] = useState('日线')
  const [loaded, setLoaded] = useState<string[]>(['日线'])
  const [tick, setTick] = useState(0)
  const qc = useQueryClient()
  const syncAttempted = useRef('')

  // 支撑级别集: 调用方指定的优先, 否则以后端 /api/chan/status 为准, 未就绪时用内置清单兜底
  const statusQ = useQuery({
    queryKey: ['chan-status'],
    queryFn: api.chanStatus,
    staleTime: Infinity,
  })
  const supported = supportedFreqs ?? statusQ.data?.supported_freqs ?? DEFAULT_LEVELS
  const barCap = statusQ.data?.bar_count_max ?? statusQ.data?.minute_count_max ?? 10000

  // 切换标的重置面板状态(级别选择回日线, 清空已加载的其它级别)
  useEffect(() => {
    setFreqSel('日线')
    setLoaded(['日线'])
    syncAttempted.current = ''
  }, [symbol])

  const syncMinute = useMutation({
    mutationFn: () => chanSyncMinuteShared(symbol),
    onSuccess: () => {
      for (const f of loaded) {
        qc.invalidateQueries({ queryKey: QK.chanAnalysis(symbol, f, barCap, start, end) })
      }
    },
  })

  // 交易时段检测: 20s 心跳, 在时段内每 30s 轮询已加载级别
  useEffect(() => {
    const timer = setInterval(() => setTick(x => x + 1), TICK_MS)
    return () => clearInterval(timer)
  }, [])
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const pollMs = useMemo(() => (isTradingHoursNow() ? POLL_MS_TRADING : 0), [tick])

  const chanQueries = useQueries({
    queries: loaded.map(f => ({
      queryKey: QK.chanAnalysis(symbol, f, barCap, start, end),
      queryFn: () => api.chanAnalysis(symbol, barCap, f, start, end),
      enabled: !!symbol,
      staleTime: 0,
      refetchInterval: pollMs,
    })),
  })

  // 聚合已加载级别的结果: freq → level / result / warnings
  const perFreq = useMemo(() => {
    const m = new Map<string, { level?: ChanLevel; data?: ChanAnalysis; loading: boolean; error: boolean }>()
    loaded.forEach((f, i) => {
      const q = chanQueries[i]
      const data = q?.data
      m.set(f, {
        level: data?.levels.find(l => l.freq === f) || data?.levels[0],
        data,
        loading: q?.isLoading ?? false,
        error: q?.isError ?? false,
      })
    })
    return m
  }, [loaded, chanQueries])

  const warnings = useMemo(() => {
    const seen = new Set<string>()
    for (const q of chanQueries) {
      for (const w of q?.data?.warnings ?? []) {
        if (!seen.has(w)) seen.add(w)
      }
    }
    return [...seen]
  }, [chanQueries])

  const selected = perFreq.get(freqSel)
  const level = selected?.level
  const selectedUnavailable = selected?.data != null && selected.data.available === false
  const anyFetching = chanQueries.some(q => q?.isFetching)
  const countMax = barCap

  useEffect(() => {
    if (!needsAutoMinuteSync(selected?.data, freqSel)) return
    if (selected?.loading || syncMinute.isPending) return
    if (syncAttempted.current === symbol) return
    syncAttempted.current = symbol
    syncMinute.mutate()
  }, [freqSel, selected?.data, selected?.loading, symbol, syncMinute])
  // 级别按钮: 提供调用方/后端声明的全部级别; 分钟数据缺失时点击后由 warnings 引导同步
  const visibleLevels = supported

  const selectLevel = useCallback((f: string) => {
    setFreqSel(f)
    setLoaded(prev => (prev.includes(f) ? prev : [...prev, f]))
  }, [])

  const refresh = useCallback(() => {
    syncAttempted.current = ''
    for (const f of loaded) {
      qc.invalidateQueries({ queryKey: QK.chanAnalysis(symbol, f, barCap, start, end) })
    }
  }, [loaded, symbol, qc, barCap, start, end])

  const rows = useMemo<KlineRow[]>(() => (level
    ? level.bars.map(b => ({
        symbol,
        date: b.dt,
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
        volume: b.volume,
      }))
    : []), [level, symbol])

  const biLines = useMemo<ChanBiLine[]>(() => (level
    ? level.bi.map(b => ({ sdt: b.sdt, edt: b.edt, sp: b.sp, ep: b.ep, dir: b.dir, confirmed: b.confirmed }))
    : []), [level])

  // 分型标记只取最近 60 个已确认的, 避免密集区过于杂乱; price 用分型自身价位(底分型标记不必浮在高点)
  const fxMarkers = useMemo<ChartMarker[]>(() => (level
    ? level.fx.filter(f => f.confirmed).slice(-60).map(f => {
        const isTop = f.mark.includes('顶')
        return {
          date: f.dt,
          label: isTop ? '顶' : '底',
          color: isTop ? BULL : BEAR,
          price: f.price,
        }
      })
    : []), [level])

  const zsBands = useMemo<ZsBand[]>(() => (level
    ? level.zs.map(z => ({ sdt: z.sdt, edt: z.edt, zg: z.zg, zd: z.zd }))
    : []), [level])

  // 中枢矩形框: [sdt, edt] × [zd, zg] 有界 markArea, 与 ZG/ZD 跨度线互补(线给端点标签, 框给区间形态)
  const zsRanges = useMemo<ChartRange[]>(() => (level
    ? level.zs.map(z => ({
        start: z.sdt,
        end: z.edt,
        label: '中枢',
        color: 'rgba(139,92,246,0.10)',
        from: z.zd,
        to: z.zg,
      }))
    : []), [level])

  const signalEntries = Object.entries(level?.signals ?? {})

  return (
    <div className="mt-4 rounded-card border border-border/60 bg-surface/40 overflow-hidden">
      <div className="px-4 py-3 border-b border-border/40">
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <button
            onClick={() => setOpen(!open)}
            className="flex items-center gap-2 text-left"
            title="展开 / 收起缠论结构"
          >
            <GitBranch className="h-4 w-4 text-[#8B5CF6] shrink-0" />
            <span className="text-sm font-medium text-foreground">缠论结构分析</span>
            <span className={`text-[10px] text-muted transition-transform ${open ? 'rotate-180' : ''}`}>▼</span>
          </button>
          <div className="flex items-center gap-1.5 flex-wrap">
            {visibleLevels.map(f => {
              const q = perFreq.get(f)
              const active = f === freqSel
              const count = q?.level ? q.level.bars.length : null
              return (
                <button
                  key={f}
                  onClick={() => selectLevel(f)}
                  title={q?.level?.summary ?? (q?.error ? '加载失败' : '点击加载该级别')}
                  className={`h-6 px-2 rounded-md text-[10px] font-medium border transition-all ${
                    active ? 'text-foreground' : 'text-muted bg-base/40 border-border/30 hover:border-border/60'
                  }`}
                  style={active ? { borderColor: '#8B5CF666', backgroundColor: '#8B5CF61a' } : undefined}
                >
                  {FREQ_SHORT[f] ?? f}
                  <span className="opacity-60 ml-1 font-mono">
                    {count != null ? count : (q?.loading ? '…' : '·')}
                  </span>
                </button>
              )
            })}
            {anyFetching && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" />}
            <button
              onClick={refresh}
              title="立即刷新已加载的级别(盘中可看到最新一笔/分型变化)"
              className="inline-flex items-center gap-1 h-6 px-2 rounded-md text-[10px] text-muted bg-base/40 border border-border/30 hover:border-border/60 transition-all"
            >
              <RefreshCw className={`h-3 w-3 ${anyFetching ? 'animate-spin' : ''}`} />
              刷新
            </button>
          </div>
        </div>
        {level && <div className="mt-1 text-[11px] text-secondary">{level.summary}</div>}
      </div>

      {open && (
        <div className="p-3">
          {selected?.loading && !level && (
            <div className="flex items-center justify-center py-16"><Loader2 className="h-5 w-5 animate-spin text-muted" /></div>
          )}

          {!selected?.loading && selected?.error && (
            <div className="py-8 text-center text-xs text-muted">缠论数据加载失败,请稍后重试或点刷新。</div>
          )}

          {!selected?.loading && !selected?.error && selectedUnavailable && (
            <div className="py-8 text-center text-xs text-muted">
              {selected?.data?.reason ?? '缠论分析当前不可用'}
            </div>
          )}

          {!selected?.loading && !selected?.error && !selectedUnavailable && level && rows.length > 0 && (
            <>
              <AnalysisKChart
                rows={rows}
                biLines={biLines}
                markers={fxMarkers}
                zsBands={zsBands}
                ranges={zsRanges}
                useRawAxisKeys
                defaultVisibleBars={rows.length}
                height={height}
              />

              {warnings.length > 0 && (
                <div className="mt-2 space-y-1">
                  {warnings.map((w, i) => (
                    <div key={i} className="text-[11px] text-[#EAB308]/90">{w}</div>
                  ))}
                </div>
              )}

              {signalEntries.length > 0 && (
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  <span className="text-[10px] text-muted mr-1">信号</span>
                  {signalEntries.map(([k, v]) => (
                    <span
                      key={k}
                      className={`inline-flex items-center gap-1 h-5 px-1.5 rounded text-[10px] border ${signalColor(v)}`}
                      title={k}
                    >
                      {signalLabel(k)}
                      <span className="font-mono">{v.split('_')[0]}</span>
                    </span>
                  ))}
                </div>
              )}

              {level.zs.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1">
                  {level.zs.slice(-4).map((z, i) => (
                    <span key={i} className="text-[10px] font-mono text-muted">
                      中枢 {z.zg}~{z.zd} ({z.sdt.slice(5)}~{z.edt.slice(5)})
                    </span>
                  ))}
                </div>
              )}
            </>
          )}

          {!selected?.loading && !selected?.error && !selectedUnavailable && !level && syncMinute.isPending && (
            <div className="flex items-center justify-center gap-2 py-16 text-xs text-muted">
              <Loader2 className="h-4 w-4 animate-spin" />
              正在同步分钟K{countMax ? `（上限 ${countMax} 根）` : ''}…
            </div>
          )}

          {!selected?.loading && !selected?.error && !selectedUnavailable && !level && !syncMinute.isPending && (
            <div className="py-8 text-center text-xs text-muted">
              该级别暂无结构{MINUTE_FREQS.has(freqSel) ? '（正在等待分钟数据）' : ''}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function signalLabel(key: string): string {
  return key.replace(/^日线_/, '').replace(/V\d+$/, '')
}

function signalColor(value: string): string {
  if (value.startsWith('看多')) return 'border-bull/40 text-bull'
  if (value.startsWith('看空')) return 'border-bear/40 text-bear'
  return 'border-border/40 text-secondary'
}