import { useState, useMemo, useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { Play, BarChart3, Clock, Square, History as HistoryIcon } from 'lucide-react'
import {
  api,
  type FactorColumn,
  type FactorBacktestResult,
  type FactorBatchResult,
  type FactorHistoryItem,
  type GroupStat,
} from '@/lib/api'
import { fmtPct, priceColorClass } from '@/lib/format'
import { EmptyState } from '@/components/EmptyState'
import { DatePicker } from '@/components/DatePicker'
import { FactorICChart } from './charts/FactorICChart'
import { FactorGroupNavChart } from './charts/FactorGroupNavChart'
import {
  useFactorTask,
  startFactorRun,
  startFactorBatch,
  cancelFactorTask,
  type FactorProgressEvent,
} from '@/lib/factorTask'

const formatDate = (date: Date) => date.toISOString().slice(0, 10)
const monthsAgo = (months: number) => {
  const date = new Date()
  date.setMonth(date.getMonth() - months)
  return formatDate(date)
}
const TODAY = formatDate(new Date())
const THREE_MONTHS_AGO = monthsAgo(3)

const INPUT_CLS = `w-full px-2.5 py-1.5 rounded-input bg-surface border border-border text-xs
  focus:outline-none focus:border-accent transition-colors duration-150 ease-smooth`

const STAGE_LABELS: Record<string, string> = {
  pending: '排队等待',
  panel: '行情面板加载',
  factor: '因子值计算',
  returns: '下期收益',
  ic: '截面 Rank IC',
  groups: '分层净值',
  nav: '分层统计',
  ls: '多空组合',
  corr: 'IC 相关矩阵',
  done: '汇总完成',
}

function StatCard({ label, value, highlight }: {
  label: string
  value: string | null | undefined
  highlight?: 'bull' | 'bear' | 'neutral'
}) {
  const colorCls = highlight === 'bull'
    ? 'text-bull' : highlight === 'bear' ? 'text-bear' : ''
  return (
    <div>
      <div className="text-[11px] text-muted">{label}</div>
      <div className={`mt-1 text-lg font-mono font-semibold tracking-tight num ${colorCls}`}>
        {value ?? '—'}
      </div>
    </div>
  )
}

function LoadingPanel({ symbolsText, progress }: {
  symbolsText: string
  progress: FactorProgressEvent[]
}) {
  const latest = progress[progress.length - 1]
  const currentStage = latest?.stage ?? 'pending'
  const stageOrder = ['pending', 'panel', 'factor', 'returns', 'ic', 'groups', 'nav', 'ls', 'done']
  return (
    <div className="space-y-4">
      <div className="rounded-card border border-accent/25 bg-accent/[0.04] p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-sm font-medium text-foreground">正在计算因子分析</div>
            <div className="mt-1 text-xs text-muted">{symbolsText} · {latest?.message ?? '准备中'}</div>
          </div>
          <div className="h-8 w-8 rounded-full border-2 border-accent/25 border-t-accent animate-spin" />
        </div>
        <div className="mt-4 h-1.5 overflow-hidden rounded-full bg-base">
          <div
            className="h-full rounded-full bg-accent/70 transition-all duration-300"
            style={{ width: `${Math.max(latest?.pct ?? 0, 4)}%` }}
          />
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {stageOrder.map((stage) => {
          const hit = progress.find(p => p.stage === stage)
          const done = stageOrder.indexOf(stage) < stageOrder.indexOf(currentStage) || stage === 'done' && latest?.pct === 100
          return (
            <div
              key={stage}
              className={`rounded-btn border border-border bg-surface p-3
                ${hit || done ? 'opacity-100' : 'opacity-60'}`}
            >
              <div className={`h-2 w-10 rounded ${hit || done ? 'bg-accent/70' : 'bg-accent/20'}`} />
              <div className="mt-3 text-xs text-secondary">{STAGE_LABELS[stage] ?? stage}</div>
              {hit && <div className="mt-0.5 text-[10px] text-accent num">{hit.pct}%</div>}
            </div>
          )
        })}
      </div>

      <div className="rounded-card border border-border bg-surface p-4">
        <div className="flex items-center justify-between">
          <div className="text-xs font-medium text-secondary">分层净值预览</div>
          <div className="text-[11px] text-muted">等待后端返回完整结果</div>
        </div>
        <div className="mt-4 h-[260px] rounded-btn border border-border bg-base/60 p-4">
          <div className="flex h-full items-end gap-2 opacity-70">
            {[46, 38, 54, 50, 64, 58, 74, 68, 84, 78, 90, 86].map((h, i) => (
              <div key={i} className="flex-1 rounded-t bg-accent/20 animate-pulse" style={{ height: `${h}%` }} />
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

function BatchResults({ batch }: { batch: FactorBatchResult }) {
  const names = batch.ic_corr?.names ?? []
  const matrix = batch.ic_corr?.matrix ?? []
  return (
    <div className="space-y-4">
      <div className="rounded-card border border-border overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-elevated">
            <tr className="text-left text-secondary">
              <th className="px-4 py-2.5 font-medium">因子</th>
              <th className="px-4 py-2.5 font-medium text-right">IC 均值</th>
              <th className="px-4 py-2.5 font-medium text-right">ICIR</th>
              <th className="px-4 py-2.5 font-medium text-right">IC 胜率</th>
              <th className="px-4 py-2.5 font-medium text-right">多空总收益</th>
              <th className="px-4 py-2.5 font-medium text-right">多空年化</th>
              <th className="px-4 py-2.5 font-medium text-right">多空夏普</th>
            </tr>
          </thead>
          <tbody>
            {batch.factors.map((r) => (
              <tr key={r.name} className="border-t border-border hover:bg-elevated/50 transition-colors">
                <td className="px-4 py-2 font-medium">{r.name}</td>
                <td className={`px-4 py-2 text-right num ${priceColorClass(r.ic_mean ?? 0)}`}>
                  {r.ic_mean != null ? fmtPct(r.ic_mean) : '—'}
                </td>
                <td className={`px-4 py-2 text-right num ${priceColorClass(r.ir ?? 0)}`}>
                  {r.ir != null ? r.ir.toFixed(2) : '—'}
                </td>
                <td className="px-4 py-2 text-right num">{r.ic_win_rate != null ? fmtPct(r.ic_win_rate) : '—'}</td>
                <td className={`px-4 py-2 text-right num ${priceColorClass(r.ls_total_return ?? 0)}`}>
                  {r.ls_total_return != null ? fmtPct(r.ls_total_return) : '—'}
                </td>
                <td className={`px-4 py-2 text-right num ${priceColorClass(r.ls_annual_return ?? 0)}`}>
                  {r.ls_annual_return != null ? fmtPct(r.ls_annual_return) : '—'}
                </td>
                <td className="px-4 py-2 text-right num">{r.ls_sharpe != null ? r.ls_sharpe.toFixed(2) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {names.length >= 2 && (
        <div className="rounded-card border border-border bg-surface p-4">
          <div className="text-xs font-medium text-secondary mb-3">日 IC 相关矩阵</div>
          <div
            className="grid gap-px overflow-x-auto"
            style={{ gridTemplateColumns: `minmax(4rem, auto) repeat(${names.length}, minmax(3rem, 1fr))` }}
          >
            <div />
            {names.map(n => (
              <div key={`h-${n}`} className="px-1 pb-1 text-center text-[10px] text-muted truncate" title={n}>{n}</div>
            ))}
            {names.map((n, i) => (
              <FragmentRow key={`row-${n}`} name={n} row={matrix[i] ?? []} />
            ))}
          </div>
        </div>
      )}
      {batch.skipped.length > 0 && (
        <div className="text-[11px] text-muted">已跳过无法计算: {batch.skipped.join(', ')}</div>
      )}
    </div>
  )
}

function FragmentRow({ name, row }: { name: string; row: (number | null)[] }) {
  return (
    <>
      <div className="pr-2 text-right text-[10px] text-muted truncate" title={name}>{name}</div>
      {row.map((v, j) => {
        const alpha = v == null ? 0.05 : Math.min(Math.abs(v), 1)
        const bg = v == null
          ? 'rgba(128,128,128,0.08)'
          : v >= 0
            ? `rgba(240,68,56,${alpha})`
            : `rgba(18,183,106,${alpha})`
        return (
          <div
            key={j}
            className="flex items-center justify-center py-1.5 text-[10px] num"
            style={{ backgroundColor: bg }}
          >
            {v != null ? v.toFixed(2) : '—'}
          </div>
        )
      })}
    </>
  )
}

export function FactorBacktest() {
  const [factorName, setFactorName] = useState('momentum_20d')
  const [symbols, setSymbols] = useState('')
  const [assetType, setAssetType] = useState<'stock' | 'etf'>('stock')
  const [start, setStart] = useState(THREE_MONTHS_AGO)
  const [end, setEnd] = useState(TODAY)
  const [nGroups, setNGroups] = useState(5)
  const [rebalance, setRebalance] = useState<'daily' | 'weekly' | 'monthly'>('monthly')
  const [weight, setWeight] = useState<'equal' | 'factor_weight'>('equal')
  const [fees, setFees] = useState('2')
  const [slippage, setSlippage] = useState('5')
  const [result, setResult] = useState<FactorBacktestResult | null>(null)
  const [batchNames, setBatchNames] = useState<string[]>([])
  const [batchView, setBatchView] = useState<FactorBatchResult | null>(null)
  const [mode, setMode] = useState<'single' | 'batch' | null>(null)

  const task = useFactorTask()
  const qc = useQueryClient()

  const columns = useQuery({
    queryKey: ['backtest-factor-columns'],
    queryFn: api.factorColumns,
  })

  const history = useQuery({
    queryKey: ['backtest-factor-history'],
    queryFn: () => api.factorHistory(20),
  })

  // 任务完成 → 收敛到组件状态
  useEffect(() => {
    if (task.phase === 'done') {
      if (task.result) setResult(task.result)
      if (task.batch) setBatchView(task.batch)
      qc.invalidateQueries({ queryKey: ['backtest-factor-history'] })
    }
  }, [task.phase, task.result, task.batch, qc])

  // 按 group 分类的因子
  const factorGroups = useMemo(() => {
    const cols = columns.data?.columns ?? []
    const groups: Record<string, FactorColumn[]> = {}
    for (const c of cols) {
      ;(groups[c.group] ??= []).push(c)
    }
    return groups
  }, [columns.data])

  const allFactorIds = useMemo(
    () => (columns.data?.columns ?? []).map(c => c.id),
    [columns.data],
  )

  // 当前因子描述
  const factorDesc = useMemo(() => {
    return columns.data?.columns.find(c => c.id === factorName)?.desc ?? ''
  }, [columns.data, factorName])

  const running = task.phase === 'running'
  const sharedParams = {
    asset_type: assetType,
    symbols: symbols.trim() || undefined,
    start: start || undefined,
    end: end || undefined,
    n_groups: nGroups,
    rebalance,
    fees_pct: Number(fees) / 10000,
    slippage_bps: Number(slippage) / 10000,
  }

  const beginRun = () => {
    setMode('single')
    startFactorRun({ factor_name: factorName, weight, ...sharedParams })
  }

  const beginBatch = () => {
    if (batchNames.length === 0) return
    setMode('batch')
    startFactorBatch({ factor_names: batchNames.join(','), ...sharedParams })
  }

  const toggleBatchName = (id: string) => {
    setBatchNames(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id])
  }

  const openHistory = async (item: FactorHistoryItem) => {
    try {
      const record = await api.factorHistoryItem(item.run_id)
      const data = record.data
      if (record.kind === 'batch') {
        setMode('batch')
        setBatchView(data as FactorBatchResult)
        return
      }
      setMode('single')
      const cfg = data.config ?? {}
      if (cfg.factor_name) setFactorName(cfg.factor_name)
      if (Array.isArray(cfg.symbols)) setSymbols(cfg.symbols.join(', '))
      if (cfg.start) setStart(cfg.start)
      if (cfg.end) setEnd(cfg.end)
      if (cfg.n_groups) setNGroups(cfg.n_groups)
      if (cfg.rebalance) setRebalance(cfg.rebalance)
      if (cfg.weight) setWeight(cfg.weight)
      setFees(String(Math.round((cfg.fees_pct ?? 0.0002) * 10000)))
      setSlippage(String(Math.round(cfg.slippage_bps ?? 5)))
      if (cfg.asset_type === 'etf' || cfg.asset_type === 'stock') setAssetType(cfg.asset_type)
      setResult(data as FactorBacktestResult)
    } catch { /* toast 已由 request 层处理 */ }
  }

  const applyRange = (months: number) => {
    setStart(monthsAgo(months))
    setEnd(formatDate(new Date()))
  }

  const applyAllRange = () => {
    setStart('')
    setEnd(formatDate(new Date()))
  }

  const rangeKey = end === TODAY && start === THREE_MONTHS_AGO
    ? '3m'
    : end === TODAY && start === monthsAgo(6)
      ? '6m'
      : end === TODAY && start === monthsAgo(12)
        ? '1y'
        : end === TODAY && start === ''
          ? 'all'
          : 'custom'
  const rangeTitle = rangeKey === '3m'
    ? '近 3 个月'
    : rangeKey === '6m'
      ? '近 6 个月'
      : rangeKey === '1y'
        ? '近 1 年'
        : rangeKey === 'all'
          ? '全部历史'
          : '自定义区间'
  const rangeButtonCls = (key: string) => `rounded-btn px-2 py-1 text-[11px] font-medium transition-colors ${rangeKey === key
    ? 'bg-accent/15 text-accent'
    : 'text-muted hover:bg-elevated/70 hover:text-secondary'
  }`

  const showBatch = mode === 'batch'

  return (
    <div className="h-full min-h-0 overflow-hidden rounded-card border border-border bg-surface/80 grid grid-cols-1 xl:grid-cols-[18rem_minmax(0,1fr)]">
      {/* 配置面板 */}
      <section className="space-y-3 border-b xl:border-b-0 xl:border-r border-border bg-base/25 px-3 py-3 xl:overflow-y-auto">
        <div className="border-b border-border/70 pb-2">
          <div className="text-xs font-semibold text-foreground">因子配置</div>
          <div className="mt-0.5 text-[10px] leading-4 text-muted">选择因子、区间和分组方式。默认最近 3 个月。</div>
        </div>

        <div>
          <label className="text-xs font-medium text-secondary block mb-1.5">因子</label>
          <select
            value={factorName}
            onChange={e => setFactorName(e.target.value)}
            className={INPUT_CLS}
          >
            {Object.entries(factorGroups).map(([group, cols]) => (
              <optgroup key={group} label={group}>
                {cols.map(c => (
                  <option key={c.id} value={c.id}>{c.label}</option>
                ))}
              </optgroup>
            ))}
          </select>
          {factorDesc && (
            <p className="mt-1 text-[11px] text-muted">{factorDesc}</p>
          )}
        </div>

        <div>
          <label className="text-xs font-medium text-secondary block mb-1.5">资产类型</label>
          <div className="inline-flex h-8 rounded-btn border border-border overflow-hidden mb-2">
            {(['stock', 'etf'] as const).map(t => (
              <button
                key={t}
                type="button"
                onClick={() => { setAssetType(t); setSymbols('') }}
                className={`h-full px-3 text-xs font-medium transition-colors cursor-pointer
                  ${assetType === t ? 'bg-accent/10 text-accent' : 'text-muted hover:text-foreground'}`}
              >
                {t === 'stock' ? '股票' : 'ETF'}
              </button>
            ))}
          </div>
          <label className="text-xs font-medium text-secondary block mb-1.5">
            标的(逗号分隔，留空=全市场{assetType === 'etf' ? ' ETF' : ''})
          </label>
          <input
            type="text"
            value={symbols}
            onChange={e => setSymbols(e.target.value)}
            placeholder="留空则使用全市场，建议最近3个月"
            className={`w-full px-2.5 py-1.5 rounded-input bg-surface border border-border text-xs font-mono
              focus:outline-none focus:border-accent transition-colors duration-150 ease-smooth`}
          />
        </div>

        <div className="rounded-btn border border-border bg-surface p-2.5">
          <div className="flex items-center justify-between gap-2">
            <div className="text-xs font-medium text-foreground">回测区间</div>
            <span className="shrink-0 rounded-full border border-accent/25 bg-accent/10 px-2 py-0.5 text-[10px] font-medium text-accent">
              {rangeTitle}
            </span>
          </div>

          <div className="mt-2 grid grid-cols-2 gap-2">
            <div>
              <label className="text-[11px] text-secondary block mb-1">开始</label>
              <DatePicker
                value={start}
                onChange={setStart}
                max={end || undefined}
                placeholder="全部历史"
                className="w-full"
                buttonClassName="w-full justify-start"
                align="left"
              />
            </div>
            <div>
              <label className="text-[11px] text-secondary block mb-1">结束</label>
              <DatePicker
                value={end}
                onChange={setEnd}
                min={start || undefined}
                className="w-full"
                buttonClassName="w-full justify-start"
              />
            </div>
          </div>

          <div className="mt-2 flex rounded-input bg-base/60 p-0.5">
            <button type="button" onClick={() => applyRange(3)} className={`${rangeButtonCls('3m')} flex-1`}>3个月</button>
            <button type="button" onClick={() => applyRange(6)} className={`${rangeButtonCls('6m')} flex-1`}>6个月</button>
            <button type="button" onClick={() => applyRange(12)} className={`${rangeButtonCls('1y')} flex-1`}>1年</button>
            <button type="button" onClick={applyAllRange} className={`${rangeButtonCls('all')} flex-1`}>全部</button>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="text-xs font-medium text-secondary block mb-1.5">分组数</label>
            <select value={nGroups} onChange={e => setNGroups(Number(e.target.value))} className={INPUT_CLS}>
              <option value={3}>3组</option>
              <option value={5}>5组</option>
              <option value={10}>10组</option>
            </select>
          </div>
          <div>
            <label className="text-xs font-medium text-secondary block mb-1.5">调仓频率</label>
            <select value={rebalance} onChange={e => setRebalance(e.target.value as any)} className={INPUT_CLS}>
              <option value="daily">日度</option>
              <option value="weekly">周度</option>
              <option value="monthly">月度</option>
            </select>
          </div>
          <div>
            <label className="text-xs font-medium text-secondary block mb-1.5">权重</label>
            <select value={weight} onChange={e => setWeight(e.target.value as any)} className={INPUT_CLS}>
              <option value="equal">等权</option>
              <option value="factor_weight">因子加权</option>
            </select>
          </div>
          <div>
            <label className="text-xs font-medium text-secondary block mb-1.5">佣金(万分之)</label>
            <input type="number" value={fees} onChange={e => setFees(e.target.value)}
              className={INPUT_CLS} />
          </div>
          <div>
            <label className="text-xs font-medium text-secondary block mb-1.5">滑点(万分之)</label>
            <input type="number" value={slippage} onChange={e => setSlippage(e.target.value)}
              className={INPUT_CLS} />
          </div>
        </div>

        {running ? (
          <button
            onClick={() => cancelFactorTask()}
            className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-2 rounded-btn
              bg-danger/15 text-danger text-sm font-medium hover:bg-danger/25
              transition-colors duration-150 ease-smooth"
          >
            <Square className="h-3.5 w-3.5" />
            取消分析
          </button>
        ) : (
          <button
            onClick={beginRun}
            disabled={running}
            className="w-full inline-flex items-center justify-center gap-1.5 px-3 py-2 rounded-btn
              bg-accent text-sm font-medium hover:bg-accent/90
              transition-colors duration-150 ease-smooth disabled:opacity-50"
          >
            <Play className="h-3.5 w-3.5" />
            开始因子分析
          </button>
        )}

        {/* 批量评估 */}
        <div className="rounded-btn border border-border bg-surface p-2.5">
          <div className="flex items-center justify-between">
            <div className="text-xs font-medium text-foreground">批量评估</div>
            <div className="flex gap-2">
              <button
                type="button"
                className="text-[10px] text-accent hover:underline cursor-pointer"
                onClick={() => setBatchNames(allFactorIds)}
              >全选</button>
              <button
                type="button"
                className="text-[10px] text-muted hover:underline cursor-pointer"
                onClick={() => setBatchNames([])}
              >清空</button>
            </div>
          </div>
          <div className="mt-2 grid grid-cols-3 gap-1.5">
            {(columns.data?.columns ?? []).map(c => (
              <button
                key={c.id}
                type="button"
                onClick={() => toggleBatchName(c.id)}
                className={`rounded-input border px-1 py-1 text-[10px] transition-colors cursor-pointer
                  ${batchNames.includes(c.id)
                    ? 'border-accent/50 bg-accent/10 text-accent'
                    : 'border-border text-muted hover:text-secondary'}`}
              >
                {c.label}
              </button>
            ))}
          </div>
          <button
            onClick={beginBatch}
            disabled={running || batchNames.length === 0}
            className="mt-2 w-full inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-btn
              bg-accent/15 text-accent text-xs font-medium hover:bg-accent/25
              transition-colors duration-150 ease-smooth disabled:opacity-40 cursor-pointer"
          >
            <Play className="h-3 w-3" />
            批量评估 {batchNames.length > 0 ? `(${batchNames.length})` : ''}
          </button>
        </div>

        {/* 历史记录 */}
        <div className="rounded-btn border border-border bg-surface p-2.5">
          <div className="flex items-center gap-1.5 text-xs font-medium text-foreground">
            <HistoryIcon className="h-3.5 w-3.5" />
            历史记录
          </div>
          <div className="mt-2 space-y-1.5 max-h-56 overflow-y-auto">
            {(history.data?.items ?? []).map(item => (
              <button
                key={item.run_id}
                type="button"
                onClick={() => openHistory(item)}
                className="w-full rounded-input border border-border px-2 py-1.5 text-left hover:bg-elevated/60 transition-colors cursor-pointer"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono text-[11px] text-foreground truncate">
                    {item.kind === 'batch' ? `批量 × ${item.factor_count}` : item.factor_name}
                  </span>
                  <span className="shrink-0 text-[10px] text-muted">{item.created_at?.slice(5, 16)}</span>
                </div>
                <div className="mt-0.5 flex items-center gap-2 text-[10px] text-muted">
                  {item.ic_mean != null && (
                    <span className={priceColorClass(item.ic_mean)}>IC {fmtPct(item.ic_mean)}</span>
                  )}
                  {item.ls_total_return != null && (
                    <span className={priceColorClass(item.ls_total_return)}>多空 {fmtPct(item.ls_total_return)}</span>
                  )}
                </div>
              </button>
            ))}
            {!history.data?.items?.length && (
              <div className="text-[11px] text-muted py-2 text-center">暂无历史结果</div>
            )}
          </div>
        </div>
      </section>

      {/* 结果面板 */}
      <section className="min-w-0 space-y-3 bg-base/15 px-3 py-3 xl:overflow-y-auto">
        {task.phase === 'error' && task.error && (
          <div className="text-sm text-danger bg-danger/10 border border-danger/30 rounded-btn px-3 py-2">
            {task.error}
          </div>
        )}

        {!result && !batchView && !running && (
          <EmptyState
            icon={BarChart3}
            title="选择因子并开始分析"
            hint="因子回测分析因子的预测能力 ( IC/IR ) 和分层收益差异。服务器建议优先使用最近3个月；长周期建议本机或 8GB 以上内存环境运行。"
          />
        )}

        {running && !result && !showBatch && (
          <LoadingPanel
            symbolsText={symbols ? `${symbols.split(',').length} 只标的` : '全市场 · 当前区间'}
            progress={task.progress}
          />
        )}
        {running && result && (
          <div className="rounded-card border border-accent/25 bg-accent/[0.04] px-4 py-3 text-xs text-secondary">
            正在重新计算，当前暂时展示上一次因子分析结果，完成后会自动替换。
          </div>
        )}
        {running && !result && showBatch && (
          <LoadingPanel
            symbolsText={`批量 ${batchNames.length} 个因子`}
            progress={task.progress}
          />
        )}

        {/* 批量视图 */}
        {showBatch && !running && batchView && (
          <div className="rounded-card border border-border bg-surface p-4">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-medium text-foreground">批量因子评估</h3>
              <div className="flex items-center gap-2 text-[11px] text-muted">
                {batchView.n_symbols} 只 · {batchView.n_dates} 日 · {batchView.elapsed_ms} ms
              </div>
            </div>
            <BatchResults batch={batchView} />
          </div>
        )}
        {showBatch && !running && !batchView && (
          <EmptyState
            icon={BarChart3}
            title="批量因子评估"
            hint="在左侧勾选多个因子后点击「批量评估」，一次比较各因子的 IC/IR 与多空收益，并给出因子间 IC 相关矩阵。"
          />
        )}

        {/* 单因子结果 */}
        {result && result.ic_mean != null && !showBatch && (
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
            className="space-y-4"
          >
            {/* IC/IR 指标 */}
            <div className="rounded-card border border-border bg-surface p-4">
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-sm font-medium text-foreground">因子预测能力</h3>
                <div className="flex items-center gap-2">
                  {/* 频率标签跟随实际执行配置 (后端回显的 result.config), 不再硬编码 */}
                  <span className="text-[11px] text-muted">
                    Rank IC · {({ daily: '日度调仓', weekly: '周度调仓', monthly: '月度调仓' } as const)[
                      (result.config?.rebalance as 'daily' | 'weekly' | 'monthly') ?? 'daily'
                    ] ?? '日度调仓'}
                  </span>
                  {result.elapsed_ms > 0 && (
                    <span className="flex items-center gap-1 text-[11px] text-muted">
                      <Clock className="h-3 w-3" />
                      <span className="num">{result.elapsed_ms.toFixed(0)} ms</span>
                    </span>
                  )}
                </div>
              </div>
              <div className="grid grid-cols-4 gap-4">
                <StatCard
                  label="IC 均值"
                  value={result.ic_mean != null ? fmtPct(result.ic_mean) : null}
                  highlight={result.ic_mean != null
                    ? result.ic_mean > 0.03 ? 'bull' : result.ic_mean < -0.03 ? 'bear' : 'neutral'
                    : undefined}
                />
                <StatCard label="IC 标准差" value={result.ic_std != null ? fmtPct(result.ic_std) : null} />
                <StatCard
                  label="ICIR"
                  value={result.ir != null ? result.ir.toFixed(2) : null}
                  highlight={result.ir != null
                    ? Math.abs(result.ir) > 0.5 ? (result.ir > 0 ? 'bull' : 'bear') : 'neutral'
                    : undefined}
                />
                <StatCard label="IC 胜率" value={result.ic_win_rate != null ? fmtPct(result.ic_win_rate) : null} />
              </div>
            </div>

            {/* IC 时序图 */}
            {result.ic_series.length > 0 && (
              <div className="rounded-card border border-border overflow-hidden">
                <div className="bg-elevated px-4 py-2">
                  <span className="text-xs font-medium text-secondary">IC 时序</span>
                </div>
                <div className="p-2">
                  <FactorICChart result={result} />
                </div>
              </div>
            )}

            {/* 分层净值 */}
            {result.group_nav.length > 0 && (
              <div className="rounded-card border border-border overflow-hidden">
                <div className="bg-elevated px-4 py-2">
                  <span className="text-xs font-medium text-secondary">分层净值曲线</span>
                </div>
                <div className="p-2">
                  <FactorGroupNavChart result={result} />
                </div>
              </div>
            )}

            {/* 分层统计表 */}
            {result.group_stats.length > 0 && (
              <div className="rounded-card border border-border overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="bg-elevated">
                    <tr className="text-left text-secondary">
                      <th className="px-4 py-2.5 font-medium">分组</th>
                      <th className="px-4 py-2.5 font-medium text-right">总收益</th>
                      <th className="px-4 py-2.5 font-medium text-right">年化</th>
                      <th className="px-4 py-2.5 font-medium text-right">最大回撤</th>
                      <th className="px-4 py-2.5 font-medium text-right">夏普</th>
                      <th className="px-4 py-2.5 font-medium text-right">胜率</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.group_stats.map((g: GroupStat) => (
                      <tr key={g.group} className="border-t border-border hover:bg-elevated/50 transition-colors">
                        <td className="px-4 py-2 text-sm font-medium">{g.label}</td>
                        <td className={`px-4 py-2 text-right num ${priceColorClass(g.total_return)}`}>
                          {fmtPct(g.total_return)}
                        </td>
                        <td className={`px-4 py-2 text-right num ${priceColorClass(g.annual_return)}`}>
                          {fmtPct(g.annual_return)}
                        </td>
                        <td className="px-4 py-2 text-right num text-bear">{fmtPct(g.max_drawdown)}</td>
                        <td className="px-4 py-2 text-right num">{g.sharpe?.toFixed(2)}</td>
                        <td className="px-4 py-2 text-right num">{fmtPct(g.win_rate)}</td>
                      </tr>
                    ))}
                    {/* 多空行 */}
                    {result.long_short_stats?.total_return != null && (
                      <tr className="border-t-2 border-accent/30 bg-accent/[0.03]">
                        <td className="px-4 py-2 text-sm font-medium text-accent">
                          多空({result.long_short_stats.top_group ?? ''}-{result.long_short_stats.bottom_group ?? ''})
                        </td>
                        <td className={`px-4 py-2 text-right num font-medium ${priceColorClass(result.long_short_stats.total_return)}`}>
                          {fmtPct(result.long_short_stats.total_return as number)}
                        </td>
                        <td className={`px-4 py-2 text-right num ${priceColorClass(result.long_short_stats.annual_return as number)}`}>
                          {result.long_short_stats.annual_return != null
                            ? fmtPct(result.long_short_stats.annual_return as number) : '—'}
                        </td>
                        <td className="px-4 py-2 text-right num text-bear">
                          {fmtPct(result.long_short_stats.max_drawdown as number)}
                        </td>
                        <td className="px-4 py-2 text-right num">
                          {result.long_short_stats.sharpe != null
                            ? (result.long_short_stats.sharpe as number).toFixed(2) : '—'}
                        </td>
                        <td className="px-4 py-2 text-right num">
                          {result.long_short_stats.win_rate != null
                            ? fmtPct(result.long_short_stats.win_rate as number) : '—'}
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            )}

            {/* 数据概要 */}
            <div className="flex items-center gap-4 text-[11px] text-muted">
              <span>{result.n_symbols} 只标的</span>
              <span>{result.n_dates} 个交易日</span>
              <span>run_id: {result.run_id}</span>
            </div>
          </motion.div>
        )}
      </section>
    </div>
  )
}