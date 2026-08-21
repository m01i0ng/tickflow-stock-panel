import { useEffect, useRef, useMemo, useState } from 'react'
import { chartTheme, getTheme, useTheme } from '@/lib/theme'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import type { KlineRow, LevelSeries } from '@/lib/api'

/**
 * 个股分析专用日 K 图表。
 *
 * 与 StockDailyKChart/EChartsCandlestick 刻意不复用:
 *   - 那套图表面向「行情浏览」,强调全套指标副图(MA/MACD/KDJ/BOLL)、涨停标记等;
 *   - 本图表面向「分析决策」,核心是【关键价位】(压力/支撑/密集区/枢轴/前高前低),
 *     通过开关按钮控制各价位组的显隐,布局更简洁(主图 + 成交量即可)。
 *
 * 预留接口(类型已定义,渲染逻辑留 hook,后续实现):
 *   - markers: 日期标记点(新闻/暴雷/利好 → markPoint)
 *   - ranges:  区间高亮(事件区间 → markArea)
 *   - onDateClick: 点击日期回调(后续接消息面时间轴)
 *   - 指标副图: 后续如需 MACD/KDJ,按 SUB_CHARTS 模式扩展
 */

// ===== 配色(红涨绿跌, 双主题通用); 画布轴/网格主题相关色走 CT() =====
const THEME = {
  bull: '#C74040',
  bear: '#2D9B65',
  volUp: 'rgba(240,68,56,0.5)',
  volDown: 'rgba(18,183,106,0.5)',
}

/** 当前主题的图表调色板 (buildOption 渲染时调用; 切换由组件 effect 触发重建)。 */
const CT = () => chartTheme(getTheme())

// ===== 价位类型(与后端 levels.py 的 LEVEL_TYPES 对齐) =====
export type LevelType = 'sr' | 'pivot' | 'extreme' | 'boll' | 'keltner_s' | 'keltner_m' | 'keltner_l' | 'atr_stop' | 'gap' | 'fib' | 'round'

export interface PriceLevel {
  value: number
  label: string
  type: LevelType
  side: 'resistance' | 'support' | 'neutral'
  strength?: 'strong' | 'medium' | 'weak'
  /** 档位(仅 pivot 有):0=P, 1=R1/S1, 2=R2/S2, 3=R3/S3 */
  rank?: number
}

/** 价位组开关配置:label = 按钮文案,color = markLine 颜色 */
export const LEVEL_GROUPS: { key: LevelType; label: string; color: string }[] = [
  { key: 'sr',       label: '压力支撑',  color: '#F97316' },   // 橙(成交密集区,价量驱动)
  { key: 'pivot',    label: '枢轴点',    color: '#8B5CF6' },   // 紫
  { key: 'extreme',  label: '前高前低',  color: '#EAB308' },   // 黄
  { key: 'boll',     label: '布林带',    color: '#F97316' },   // 橙(MA20±2σ 曲线)
  { key: 'keltner_s',label: 'Keltner短期',  color: '#06B6D4' },   // 青(MA20±2ATR 曲线)
  { key: 'keltner_m',label: 'Keltner中期',  color: '#22D3EE' },   // 浅青(MA60±2.5ATR 曲线)
  { key: 'keltner_l',label: 'Keltner长期',  color: '#67E8F9' },   // 更浅青(MA120±3ATR 曲线)
  { key: 'atr_stop', label: 'ATR波动通道',  color: '#EF4444' },   // 红(警示)
  { key: 'gap',      label: '缺口位',    color: '#EC4899' },   // 粉
  { key: 'fib',      label: '斐波那契',  color: '#F59E0B' },   // 金
  { key: 'round',    label: '整数关口',  color: '#71717A' },   // 灰(心理位,弱视觉)
]

// 通道曲线元数据(单一数据源):供 buildOption 画线 + 右侧面板取最新值共用。
//   alignedKey: alignedSeries 中的 key(由 series.boll/keltner/atr 对齐而来)
//   group:      属于哪个价位开关组(开关该组即开关这条曲线)
//   endLabel:   右侧端点标签(显示最新值的文字)
const CURVE_DEFS: { alignedKey: string; group: LevelType; endLabel: string; color: string; dashed?: boolean }[] = [
  { alignedKey: 'boll_upper',     group: 'boll',      endLabel: '布林上轨', color: '#F97316', dashed: true },
  { alignedKey: 'boll_lower',     group: 'boll',      endLabel: '布林下轨', color: '#F97316', dashed: true },
  { alignedKey: 'boll_mid',       group: 'boll',      endLabel: '布林中轨', color: '#FB923C', dashed: false },
  { alignedKey: 'keltner_s_upper',group: 'keltner_s', endLabel: 'Keltner短上', color: '#06B6D4', dashed: true },
  { alignedKey: 'keltner_s_lower',group: 'keltner_s', endLabel: 'Keltner短下', color: '#06B6D4', dashed: true },
  { alignedKey: 'keltner_m_upper',group: 'keltner_m', endLabel: 'Keltner中上', color: '#22D3EE', dashed: true },
  { alignedKey: 'keltner_m_lower',group: 'keltner_m', endLabel: 'Keltner中下', color: '#22D3EE', dashed: true },
  { alignedKey: 'keltner_l_upper',group: 'keltner_l', endLabel: 'Keltner长上', color: '#67E8F9', dashed: true },
  { alignedKey: 'keltner_l_lower',group: 'keltner_l', endLabel: 'Keltner长下', color: '#67E8F9', dashed: true },
  { alignedKey: 'atr_stop',       group: 'atr_stop',  endLabel: 'ATR下轨', color: '#EF4444', dashed: true },
  { alignedKey: 'atr_tp',         group: 'atr_stop',  endLabel: 'ATR上轨', color: '#F87171', dashed: true },
]

// ===== 预留:标记 / 区间(后续新闻面、事件区间用) =====
export interface ChartMarker {
  date: string
  label?: string
  color?: string
  /** 标记点 y 坐标(价格);缺省时 above=false 取 bar 低点, 否则取 bar 高点 */
  price?: number
  above?: boolean
}
export interface ChartRange {
  start: string
  end: string
  label?: string
  color?: string
  /** 有界矩形 y 区间(缠论中枢 zg/zd); 都不传则全高度高亮 */
  from?: number
  to?: number
}

/** 缠论中枢(zg/zd 跨度渲染为区间内的水平线段) */
export interface ZsBand {
  sdt: string
  edt: string
  zg: number
  zd: number
}

/** 缠论笔连线: 与 rows 日期对齐的折线段(sp -> ep 线性插值, 段间断开)。 */
export interface ChanBiLine {
  sdt: string
  edt: string
  sp: number
  ep: number
  dir: '向上' | '向下' | string
  /** false = 未确认笔(虚线渲染) */
  confirmed: boolean
}

interface Props {
  rows: KlineRow[]
  levels?: Record<LevelType, PriceLevel[]>
  /** 带状曲线指标(布林带/Keltner/ATR)的每日序列 —— 画成跟随时间漂移的曲线 */
  series?: LevelSeries
  /** series 数据对应的日期数组(与 series 各数组对齐) */
  seriesDates?: string[]
  /** 默认开启的价位组 */
  defaultLevelTypes?: LevelType[]
  /** 预留:新闻/暴雷/利好日期标记 */
  markers?: ChartMarker[]
  /** 预留:事件区间高亮 */
  ranges?: ChartRange[]
  /** 缠论笔连线(按级别选中后传入; 实线=已确认, 虚线=未确认) */
  biLines?: ChanBiLine[]
  /** 缠论中枢(区间 zg/zd 跨度线) */
  zsBands?: ZsBand[]
  /** true: x 轴/叠加用 rows.date 原文 (缠论分钟级含时钟); false: 日K 仍按 YYYY-MM-DD */
  useRawAxisKeys?: boolean
  /** 默认可见 K 线根数; 不传则日K 约 120 根 (近 6 个月)。分钟级传入全量以按 TickFlow 上限展示。 */
  defaultVisibleBars?: number
  /** 预留:点击某根 K 线 */
  onDateClick?: (date: string) => void
  height?: number
  className?: string
}

const VOL_PANE_H = 90

export function AnalysisKChart({
  rows,
  levels,
  series,
  seriesDates,
  defaultLevelTypes = ['sr', 'pivot', 'keltner_s'],
  markers,
  ranges,
  biLines,
  zsBands,
  useRawAxisKeys = false,
  defaultVisibleBars,
  onDateClick,
  height = 460,
  className,
}: Props) {
  const chartRef = useRef<HTMLDivElement>(null)
  const chartInstRef = useRef<ECharts | null>(null)
  /** seriesIndex → levelKey 映射, buildOption 填充, ECharts hover 事件反查 */
  const seriesKeyMapRef = useRef<Map<number, string>>(new Map())
  // 主题: buildOption 内部用 CT() 动态取色, 这里只负责切换时触发重建
  const theme = useTheme()
  const [activeTypes, setActiveTypes] = useState<Set<LevelType>>(new Set(defaultLevelTypes))
  /** 枢轴点显示到第几档:1=只P+R1/S1, 2=到R2/S2, 3=全档(R3/S3) */
  const [pivotRank, setPivotRank] = useState<1 | 2 | 3>(1)
  /** 双向联动高亮: hover 价位标签 ↔ hover 下方文字行。值为 levelKey, null=无高亮 */
  const [hoveredKey, setHoveredKey] = useState<string | null>(null)

  // 数据预处理 + 带状曲线序列对齐(后端 series 的日期范围可能与 rows 不同,需映射)
  const { dates, candle, vols, dateIndex, zoomStart, alignedSeries } = useMemo(() => {
    const dates = rows.map(r => {
      const s = typeof r.date === 'string' ? r.date : String(r.date)
      return useRawAxisKeys ? s : s.slice(0, 10)
    })
    const candle = rows.map(r => [r.open, r.close, r.low, r.high])
    const vols = rows.map(r => ({
      value: r.volume ?? 0,
      itemStyle: { color: r.close >= r.open ? THEME.volUp : THEME.volDown },
    }))
    const dateIndex = new Map(dates.map((d, i) => [d, i]))
    // 日K 默认最近 6 个月 ≈ 120 根; 缠论分钟级传入全量 (TickFlow 上限)。
    const showBars = defaultVisibleBars ?? 120
    const zoomStart = dates.length > showBars && showBars > 0
      ? Math.round((1 - showBars / dates.length) * 100)
      : 0

    // 把后端 series(按 seriesDates 对齐)映射到前端 rows 的 dates 顺序
    const alignedSeries: Record<string, (number | null)[]> = {}
    if (series && seriesDates && seriesDates.length > 0) {
      // 构建 seriesDates 索引
      const sIdx = new Map(seriesDates.map((d, i) => [d, i]))
      // 通用对齐:给定 series 里某条数组,返回与 rows dates 对齐的版本
      const align = (arr: (number | null)[] | undefined): (number | null)[] => {
        if (!arr) return dates.map(() => null)
        return dates.map(d => {
          const i = sIdx.get(d)
          return i != null ? arr[i] : null
        })
      }
      if (series.boll) {
        alignedSeries['boll_upper'] = align(series.boll.upper)
        alignedSeries['boll_lower'] = align(series.boll.lower)
        if (series.boll.mid) alignedSeries['boll_mid'] = align(series.boll.mid)
      }
      if (series.keltner_s) {
        alignedSeries['keltner_s_upper'] = align(series.keltner_s.upper)
        alignedSeries['keltner_s_lower'] = align(series.keltner_s.lower)
      }
      if (series.keltner_m) {
        alignedSeries['keltner_m_upper'] = align(series.keltner_m.upper)
        alignedSeries['keltner_m_lower'] = align(series.keltner_m.lower)
      }
      if (series.keltner_l) {
        alignedSeries['keltner_l_upper'] = align(series.keltner_l.upper)
        alignedSeries['keltner_l_lower'] = align(series.keltner_l.lower)
      }
      if (series.atr) {
        alignedSeries['atr_stop'] = align(series.atr.stop_loss)
        alignedSeries['atr_tp'] = align(series.atr.take_profit)
      }
    }

    return { dates, candle, vols, dateIndex, zoomStart, alignedSeries }
  }, [rows, series, seriesDates, useRawAxisKeys, defaultVisibleBars])

  // 构建 option
  const buildOption = (): EChartsOption => {
    const priceLines = collectPriceLines(levels, activeTypes, pivotRank)

    // 三段布局:主图 / 成交量 / 缩放条,从上到下累加,各段之间留间距,互不遮挡
    //   [16 顶部] [mainH 主图] [8 间距] [volH 成交量] [12 间距] [SLIDER_H 缩放条] [8 底部]
    const SLIDER_H = 22
    const PAD_TOP = 16
    const GAP_MAIN_VOL = 8        // 主图 ↔ 成交量
    const GAP_VOL_SLIDER = 12     // 成交量 ↔ 缩放条(留足,避免遮挡)
    const PAD_BOTTOM = 8
    const volH = VOL_PANE_H
    const mainH = height - PAD_TOP - GAP_MAIN_VOL - volH - GAP_VOL_SLIDER - SLIDER_H - PAD_BOTTOM
    const volTop = PAD_TOP + mainH + GAP_MAIN_VOL
    const sliderBottom = PAD_BOTTOM

    // 预留:markPoint(新闻/缠论分型标记)。coord y: price 优先, above=false 取 bar 低点, 否则 bar 高点。
    const markPointData: any[] = (markers ?? [])
      .filter(m => dateIndex.has(m.date))
      .map(m => {
        const idx = dateIndex.get(m.date)!
        const y = m.price ?? (m.above === false ? rows[idx].low : rows[idx].high)
        return {
          coord: [m.date, y],
          symbol: 'pin', symbolSize: 26,
          itemStyle: { color: m.color ?? '#EAB308' },
          label: { show: !!m.label, formatter: m.label ?? '', fontSize: 9, color: '#fff' },
        }
      })

    // 预留:markArea(事件区间/缠论中枢矩形)。from/to 齐备时画出有界矩形(yAxis), 否则全高度高亮。
    // 起止日做窗口钳制: 起点早于可见窗口时贴到窗口首日, 终点不在窗口则整段跳过。
    const markAreaData: any[] = (ranges ?? [])
      .filter(r => dateIndex.has(r.end))
      .map(r => {
        const bounded = r.from != null && r.to != null
        const startX = dateIndex.has(r.start) ? r.start : dates[0]
        const first = {
          xAxis: startX,
          ...(bounded ? { yAxis: r.from } : {}),
          name: r.label ?? '',
          itemStyle: {
            color: r.color ?? 'rgba(139,92,246,0.12)',
            borderColor: 'rgba(139,92,246,0.45)',
            borderWidth: 1,
          },
          label: r.label ? {
            show: true, position: 'insideTop', distance: 6, color: '#A78BFA', fontSize: 10,
          } : undefined,
        }
        const last = { xAxis: r.end, ...(bounded ? { yAxis: r.to } : {}) }
        return [first, last]
      })

    const series: any[] = [
      {
        name: 'K', type: 'candlestick', data: candle, animation: false,
        // z=2 让蜡烛始终在价位线(z=1)之上, hover 高亮价位线时不会被遮挡/变淡
        z: 2,
        itemStyle: {
          color: THEME.bull, color0: THEME.bear,
          borderColor: THEME.bull, borderColor0: THEME.bear,
        },
        markPoint: markPointData.length ? { data: markPointData, animation: false } : undefined,
        markArea: markAreaData.length ? { silent: true, data: markAreaData } : undefined,
      },
      {
        name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
        data: vols, animation: false,
      },
    ]

    // 价位水平线 —— 用 line series(恒定值)画水平线,endLabel 显示标签文字;
    // 与通道曲线一致,标签落在右侧 grid.right 预留带(外侧),不压蜡烛。
    // hoveredKey 非空时:命中线加粗高亮,其它线淡化(opacity 0.15),形成聚焦效果。
    const dimming = hoveredKey != null
    for (const p of priceLines) {
      const k = levelKey(p.type, p.value)
      const hit = hoveredKey === k
      const opacity = dimming ? (hit ? 1 : 0.12) : 0.7
      const width = hit ? 2 : 1
      series.push({
        name: p.label, type: 'line', silent: false, animation: false,
        symbol: 'none',
        data: dates.map(() => p.value),
        // 默认 z=1 在蜡烛(z=2)之下; 命中时 zlevel=10 提到独立顶层, 标签不再被遮挡
        z: 1,
        zlevel: hit ? 10 : 0,
        lineStyle: { width, color: p.color, type: 'dashed', opacity },
        itemStyle: { color: p.color },
        endLabel: {
          show: true,
          formatter: () => `${p.label} ${p.value.toFixed(2)}`,
          color: p.color, fontSize: hit ? 10 : 9, fontFamily: 'JetBrains Mono, monospace',
          fontWeight: hit ? 'bold' : 'normal',
          backgroundColor: hit ? CT().tooltipBg : CT().infoBarBg,
          borderColor: hit ? p.color : 'transparent',
          borderWidth: hit ? 1 : 0,
          padding: [2, 5], borderRadius: 2,
          distance: 6,
        },
      })
    }

    // 带状曲线指标(布林带 / Keltner通道 / ATR波动通道) —— 跟随行情漂移的曲线
    // 单一数据源 CURVE_DEFS 驱动:每条曲线带 endLabel(右侧端点标签),显示最新数值
    for (const def of CURVE_DEFS) {
      if (!activeTypes.has(def.group)) continue
      const data = alignedSeries[def.alignedKey]
      if (!data || !data.some(v => v != null)) continue
      // 取最后一个有效值作为右侧端点显示文字
      let lastVal: number | null = null
      for (let i = data.length - 1; i >= 0; i--) {
        if (data[i] != null) { lastVal = data[i]; break }
      }
      // 曲线 key 用 group(同组上下轨联动),hover 命中时高亮
      const hit = hoveredKey === def.group
      const opacity = dimming ? (hit ? 1 : 0.12) : 0.8
      const width = hit ? 1.8 : 1
      series.push({
        name: def.endLabel, type: 'line', data: data.map(v => v ?? '-'),
        smooth: true, symbol: 'none', silent: false, animation: false,
        z: 1,
        zlevel: hit ? 10 : 0,
        lineStyle: { width, color: def.color, type: def.dashed === false ? 'solid' : 'dashed', opacity },
        itemStyle: { color: def.color },
        // 右侧端点标签:显示该通道的最新数值,距绘图区右缘留 6px 间距
        endLabel: lastVal != null ? {
          show: true,
          formatter: () => `${lastVal!.toFixed(2)}`,
          color: def.color, fontSize: hit ? 10 : 9, fontFamily: 'JetBrains Mono, monospace',
          fontWeight: hit ? 'bold' : 'normal',
          backgroundColor: hit ? CT().tooltipBg : CT().infoBarBg,
          borderColor: hit ? def.color : 'transparent',
          borderWidth: hit ? 1 : 0,
          padding: [2, 5], borderRadius: 2,
          distance: 6,
        } : undefined,
      })
    }

    // 缠论笔连线 —— 分型端点间的直线段 (sp -> ep 按日期等分插值, 段间断开)。
    // 升级要点: 笔贴着价格路径走, 必须画在蜡烛(z=2)之上, 否则被实体完全遮挡;
    // 颜色按方向拆分到 4 个系列(上/下 × 确认/未确认), 系列级 lineStyle 染色最可靠。
    if (biLines && biLines.length > 0) {
      const Z_BI = 3
      const buildSeg = (wantDir: string, confirmed: boolean): (number | null)[] => {
        const data: (number | null)[] = dates.map(() => null)
        for (const bi of biLines) {
          if (bi.dir !== wantDir || !!bi.confirmed !== confirmed) continue
          let si = dateIndex.get(useRawAxisKeys ? bi.sdt : bi.sdt.slice(0, 10))
          const ei = dateIndex.get(useRawAxisKeys ? bi.edt : bi.edt.slice(0, 10))
          if (ei == null) continue
          if (si == null) si = 0 // 笔起点早于窗口: 贴到窗口首日(只影响末端展示, 结构本身来自后端)
          if (si >= ei) continue
          const n = ei - si
          for (let i = si; i <= ei; i++) {
            data[i] = Number((bi.sp + ((bi.ep - bi.sp) * (i - si)) / n).toFixed(3))
          }
        }
        return data
      }
      const pushBi = (data: (number | null)[], color: string, dashed: boolean, name: string) => {
        if (!data.some(v => v != null)) return
        series.push({
          name, type: 'line', data, animation: false, symbol: 'none', silent: true, z: Z_BI,
          lineStyle: { width: dashed ? 1.4 : 1.8, color, type: dashed ? 'dashed' : 'solid', opacity: 0.95 },
        })
      }
      pushBi(buildSeg('向上', true), THEME.bull, false, '向上笔')
      pushBi(buildSeg('向下', true), THEME.bear, false, '向下笔')
      pushBi(buildSeg('向上', false), THEME.bull, true, '向上笔(未确认)')
      pushBi(buildSeg('向下', false), THEME.bear, true, '向下笔(未确认)')
    }

    // 缠论中枢 —— zg/zd 在 [sdt, edt] 区间内的跨度线(与价位线同族渲染, 但置于蜡烛之上)。
    // 早期用全高度 markArea 高亮: 10% 透明wash在整根价格轴上近似不可见, 这里改为明确线段。
    if (zsBands && zsBands.length > 0) {
      const Z_ZS = 2.5
      for (const band of zsBands.slice(-6)) {
        const spanData = (value: number): (number | null)[] => {
          const data: (number | null)[] = dates.map(() => null)
          let si = dateIndex.get(useRawAxisKeys ? band.sdt : band.sdt.slice(0, 10))
          const ei = dateIndex.get(useRawAxisKeys ? band.edt : band.edt.slice(0, 10))
          if (ei == null) return data
          if (si == null) si = 0
          if (si >= ei) return data
          for (let i = si; i <= ei; i++) data[i] = value
          return data
        }
        for (const [value, dashed, name] of [[band.zg, false, '中枢上沿'], [band.zd, true, '中枢下沿']] as const) {
          const data = spanData(value)
          if (!data.some(v => v != null)) continue
          series.push({
            name, type: 'line', data, animation: false, symbol: 'none', silent: true, z: Z_ZS,
            lineStyle: { width: 1.1, color: '#8B5CF6', type: dashed ? 'dashed' : 'solid', opacity: 0.9 },
            endLabel: {
              show: true,
              formatter: () => (dashed ? 'ZD ' : 'ZG ') + Number(value).toFixed(2),
              color: '#A78BFA', fontSize: 9, fontFamily: 'JetBrains Mono, monospace',
              backgroundColor: CT().infoBarBg, padding: [2, 5], borderRadius: 2, distance: 6,
            },
          })
        }
      }
    }

    // 填充 seriesIndex → levelKey 映射(K/成交量索引 0/1 不参与联动)
    const keyMap = new Map<number, string>()
    // series[0]=K线, series[1]=成交量, 之后是按 priceLines + CURVE_DEFS 顺序 push 的
    let si = 2
    for (const p of priceLines) {
      keyMap.set(si++, levelKey(p.type, p.value))
    }
    for (const def of CURVE_DEFS) {
      if (!activeTypes.has(def.group)) continue
      const data = alignedSeries[def.alignedKey]
      if (!data || !data.some(v => v != null)) continue
      keyMap.set(si++, def.group)
    }
    seriesKeyMapRef.current = keyMap

    return {
      animation: false,
      backgroundColor: 'transparent',
      // grid.right 留出足够宽度给价位标签文字区:蜡烛只占左侧主区域,
      // 价位线右端的标签文字显示在这条预留带里,不压在蜡烛上。
      // 预留 ~144px:最长标签(如「成交密集区(POC) 12.34」)约 13 字符,fontSize 9 等宽。
      grid: [
        { left: 56, right: 144, top: 16, height: mainH },
        { left: 56, right: 144, top: volTop, height: volH },
      ],
      xAxis: [
        {
          type: 'category', data: dates, boundaryGap: true,
          axisLine: { lineStyle: { color: CT().grid } },
          axisLabel: { color: CT().text, fontSize: 10 },
          splitLine: { show: false },
          axisPointer: { show: true, label: { show: false } },
        },
        {
          type: 'category', gridIndex: 1, data: dates, boundaryGap: true,
          axisLabel: { show: false }, axisLine: { show: false }, axisTick: { show: false },
        },
      ],
      yAxis: [
        { scale: true, splitLine: { lineStyle: { color: CT().grid } },
          axisLabel: { color: CT().text, fontSize: 10, fontFamily: 'JetBrains Mono, monospace' } },
        { scale: true, gridIndex: 1, splitNumber: 2,
          // 成交量区不画背景横线
          splitLine: { show: false },
          axisLabel: { color: CT().text, fontSize: 9, fontFamily: 'JetBrains Mono, monospace',
                       formatter: (v: number) => fmtVol(v) } },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: zoomStart, end: 100 },
        { type: 'slider', xAxisIndex: [0, 1], bottom: sliderBottom, height: SLIDER_H, start: zoomStart, end: 100,
          borderColor: 'transparent', fillerColor: CT().zoomFill,
          handleStyle: { color: '#52525B' }, textStyle: { color: CT().text, fontSize: 10 } },
      ],
      // 不弹 hover tooltip(用户要求);但保留十字线 axisPointer 作为缩放/定位参照
      tooltip: { show: false },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      series,
    }
  }

  // 初始化 + 数据更新
  useEffect(() => {
    if (!chartRef.current) return
    if (!chartInstRef.current) {
      chartInstRef.current = echarts.init(chartRef.current, undefined, { renderer: 'canvas' })
      chartInstRef.current.on('click', (params: any) => {
        // 预留:点击 K 线(非 markPoint/markLine)回调
        if (params.componentType === 'series' && params.seriesType === 'candlestick' && onDateClick) {
          onDateClick(dates[params.dataIndex])
        }
      })
      // hover 价位线/曲线 endLabel → 联动高亮(与下方文字行双向联动)
      chartInstRef.current.on('mouseover', (params: any) => {
        if (params.componentType === 'series') {
          const k = seriesKeyMapRef.current.get(params.seriesIndex as number)
          if (k) setHoveredKey(k)
        }
      })
      chartInstRef.current.on('globalout', () => setHoveredKey(null))
    }
    chartInstRef.current.setOption(buildOption(), true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, levels, series, seriesDates, activeTypes, pivotRank, markers, ranges, biLines, zsBands, useRawAxisKeys, height, theme, hoveredKey])

  // resize
  useEffect(() => {
    const inst = chartInstRef.current
    if (!inst) return
    const onResize = () => inst.resize()
    window.addEventListener('resize', onResize)
    return () => { window.removeEventListener('resize', onResize); inst.dispose(); chartInstRef.current = null }
  }, [])

  const toggleType = (t: LevelType) => {
    setActiveTypes(prev => {
      const next = new Set(prev)
      if (next.has(t)) next.delete(t)
      else next.add(t)
      return next
    })
  }

  return (
    <div className={className}>
      {/* 价位开关按钮组 */}
      {levels && (
        <div className="flex flex-wrap items-center gap-1.5 mb-2">
          <span className="text-[10px] text-muted mr-1">关键价位</span>
          {LEVEL_GROUPS.map(g => {
            const active = activeTypes.has(g.key)
            // 枢轴点数量按当前档位过滤显示;其他组显示原始数量
            const raw = levels[g.key] ?? []
            const count = g.key === 'pivot'
              ? raw.filter(p => p.rank === undefined || p.rank <= pivotRank).length
              : raw.length
            return (
              <button
                key={g.key}
                onClick={() => toggleType(g.key)}
                disabled={raw.length === 0}
                title={`${g.label} (${count} 个)`}
                className={`inline-flex items-center gap-1 h-6 px-2 rounded-md text-[10px] font-medium border transition-all disabled:opacity-30 disabled:cursor-not-allowed ${
                  active
                    ? 'text-foreground'
                    : 'text-muted bg-base/40 border-border/30 hover:border-border/60'
                }`}
                style={active ? { borderColor: g.color + '66', backgroundColor: g.color + '1a' } : undefined}
              >
                <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: active ? g.color : '#52525B' }} />
                {g.label}
                <span className="opacity-50">{count}</span>
              </button>
            )
          })}

          {/* 枢轴点档位选择器 —— 仅当枢轴点开启时显示 */}
          {activeTypes.has('pivot') && (levels.pivot?.length ?? 0) > 0 && (
            <div className="inline-flex items-center gap-0.5 ml-1 pl-2 border-l border-border/40">
              <span className="text-[10px] text-muted mr-1">档位</span>
              {([1, 2, 3] as const).map(r => (
                <button
                  key={r}
                  onClick={() => setPivotRank(r)}
                  title={r === 1 ? 'P + R1/S1(3 个)' : r === 2 ? '到 R2/S2(5 个)' : '全档 R3/S3(7 个)'}
                  className={`h-6 px-2 rounded-md text-[10px] font-mono border transition-all ${
                    pivotRank === r
                      ? 'bg-[#8B5CF6]/15 border-[#8B5CF6]/40 text-[#c4b5fd]'
                      : 'text-muted bg-base/40 border-border/30 hover:border-border/60'
                  }`}
                >
                  {r}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
      {/* 图表:右侧预留带(grid.right 预留)显示价位标签文字,不压蜡烛 */}
      <div ref={chartRef} style={{ width: '100%', height }} />

      {/* 价位统计面板:把当前开启的点位按"压力 / 支撑"结构化列出 */}
      {levels && (
        <LevelOverview
          levels={levels}
          activeTypes={activeTypes}
          pivotRank={pivotRank}
          close={rows.length ? rows[rows.length - 1].close : undefined}
          hoveredKey={hoveredKey}
          onHover={setHoveredKey}
        />
      )}
    </div>
  )
}

// ===== 价位统计面板(图表下方,结构化文本展示) =====
function LevelOverview({
  levels, activeTypes, pivotRank, close, hoveredKey, onHover,
}: {
  levels: Record<LevelType, PriceLevel[]>
  activeTypes: Set<LevelType>
  pivotRank: 1 | 2 | 3
  close?: number
  hoveredKey: string | null
  onHover: (k: string | null) => void
}) {
  // 收集当前显示的点位(同 collectPriceLines 的过滤逻辑)
  const visible: PriceLevel[] = []
  for (const g of LEVEL_GROUPS) {
    if (!activeTypes.has(g.key)) continue
    for (const p of levels[g.key] ?? []) {
      if (p.type === 'pivot' && p.rank !== undefined && p.rank > pivotRank) continue
      visible.push(p)
    }
  }
  if (visible.length === 0) return null

  // 按方向分两组:压力位(在当前价之上) / 支撑位(之下),各自按距当前价远近排序
  const cur = close ?? visible[0].value
  const resistances = visible
    .filter(p => p.side === 'resistance')
    .sort((a, b) => a.value - b.value)        // 由近及远(低→高)
  const supports = visible
    .filter(p => p.side === 'support')
    .sort((a, b) => b.value - a.value)         // 由近及远(高→低)
  const neutrals = visible.filter(p => p.side === 'neutral')

  const fmtPct = (v: number) => {
    if (!cur) return ''
    const pct = ((v - cur) / cur) * 100
    const sign = pct >= 0 ? '+' : ''
    return `${sign}${pct.toFixed(1)}%`
  }

  const Row = ({ p }: { p: PriceLevel }) => {
    const color = LEVEL_GROUPS.find(g => g.key === p.type)?.color ?? CT().text
    const k = levelKey(p.type, p.value)
    const hit = hoveredKey === k
    const dim = hoveredKey != null && !hit
    return (
      <div
        onMouseEnter={() => onHover(k)}
        onMouseLeave={() => onHover(null)}
        className={`flex items-center gap-2 py-0.5 px-1.5 -mx-1.5 rounded transition-colors cursor-default ${
          hit ? 'bg-elevated/60' : ''
        }`}
        style={dim ? { opacity: 0.35 } : undefined}
      >
        <span className="h-1.5 w-1.5 rounded-full shrink-0 transition-transform" style={{ backgroundColor: color, transform: hit ? 'scale(1.5)' : 'scale(1)' }} />
        <span className={`text-[11px] w-24 shrink-0 truncate ${hit ? 'text-foreground font-medium' : 'text-secondary'}`}>{p.label}</span>
        <span className={`text-[11px] font-mono ${hit ? 'text-foreground font-bold' : 'text-foreground'}`}>{p.value.toFixed(2)}</span>
        <span className="text-[9px] font-mono text-muted">{fmtPct(p.value)}</span>
      </div>
    )
  }

  return (
    <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1 rounded-lg border border-border/40 bg-base/20 px-3 py-2">
      {/* 当前价 */}
      <div className="sm:col-span-2 flex items-center gap-2 pb-1 border-b border-border/30 mb-0.5">
        <span className="text-[10px] text-muted">当前价</span>
        <span className="text-xs font-mono font-medium text-foreground">{cur.toFixed(2)}</span>
      </div>
      {/* 压力位(从近到远,即从低到高)倒序展示:最高的在最上 */}
      {resistances.length > 0 && (
        <div>
          <div className="text-[10px] font-medium text-bear mb-0.5">压力位 ↑</div>
          {[...resistances].reverse().map((p, i) => <Row key={`r-${i}`} p={p} />)}
        </div>
      )}
      {/* 支撑位 + 中性(枢轴位 P) */}
      <div>
        {supports.length > 0 && (
          <>
            <div className="text-[10px] font-medium text-bull mb-0.5">支撑位 ↓</div>
            {supports.map((p, i) => <Row key={`s-${i}`} p={p} />)}
          </>
        )}
        {neutrals.length > 0 && (
          <div className={supports.length > 0 ? 'mt-2' : ''}>
            {supports.length === 0 && <div className="text-[10px] font-medium text-muted mb-0.5">枢轴位</div>}
            {neutrals.map((p, i) => <Row key={`n-${i}`} p={p} />)}
          </div>
        )}
      </div>
    </div>
  )
}

// ===== 工具:收集要画的水平价位线(按开启的组 + 档位 + 强度配色) =====
// 注意:带状指标(布林带/Keltner/ATR)改用曲线渲染,不在此画水平线,避免重复。
function collectPriceLines(
  levels: Record<LevelType, PriceLevel[]> | undefined,
  active: Set<LevelType>,
  pivotRank: 1 | 2 | 3,
): { value: number; label: string; color: string; type: string }[] {
  if (!levels) return []
  const out: { value: number; label: string; color: string; type: string }[] = []
  for (const g of LEVEL_GROUPS) {
    if (!active.has(g.key)) continue
    for (const p of levels[g.key] ?? []) {
      // 枢轴点:按档位过滤(rank>P 的,只显示到选定的档位)
      if (p.type === 'pivot' && p.rank !== undefined && p.rank > pivotRank) continue
      // 波动通道类(boll / keltner三档 / atr_stop)整组走曲线渲染,不画水平线;
      // sr 组现为成交密集区水平点,直接画线即可,无需特判。
      if (p.type === 'boll' || p.type === 'keltner_s' || p.type === 'keltner_m'
          || p.type === 'keltner_l' || p.type === 'atr_stop') continue
      out.push({ value: p.value, label: p.label, color: strengthColor(p.strength, g.color), type: p.type })
    }
  }
  return out
}

function strengthColor(strength: string | undefined, base: string): string {
  // strong 用实色,medium 用 0.85,weak 用 0.55 透明
  if (strength === 'weak') return base + '8C'
  if (strength === 'medium') return base + 'D9'
  return base
}

/** 价位唯一标识: 同类型同价格视为同一点位(用于联动高亮)。 */
function levelKey(type: string, value: number): string {
  return `${type}-${value.toFixed(2)}`
}

function fmtVol(v: number): string {
  if (!v) return '0'
  if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (v >= 1e4) return (v / 1e4).toFixed(0) + '万'
  return v.toFixed(0)
}
