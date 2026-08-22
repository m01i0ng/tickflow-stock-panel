// 因子回测 SSE 任务 store — 与 backtestTask 同模式, 但简化:
// 单任务、EventSource 首事件回吐 job_key (取消契约)、进度按 idx 去重 (断线重连回放)。
import { useSyncExternalStore } from 'react'
import { api, type FactorBacktestResult, type FactorBatchResult } from './api'

export interface FactorProgressEvent {
  pct: number
  stage: string
  message: string
  idx: number
}

export type FactorTaskPhase = 'idle' | 'running' | 'done' | 'error'

export interface FactorTaskState {
  phase: FactorTaskPhase
  jobKey: string | null
  progress: FactorProgressEvent[]
  result?: FactorBacktestResult
  batch?: FactorBatchResult
  error: string | null
}

let _state: FactorTaskState = { phase: 'idle', jobKey: null, progress: [], error: null }
let _snapshot: FactorTaskState = _state
let _source: EventSource | null = null
const listeners = new Set<() => void>()

function emit() {
  _snapshot = { ..._state, progress: [..._state.progress] }
  listeners.forEach((fn) => fn())
}

function subscribe(fn: () => void) {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

function useFactorTask(): FactorTaskState {
  return useSyncExternalStore(subscribe, () => _snapshot, () => _snapshot)
}

function _connect(path: string, applyDone: (payload: Record<string, any>) => void) {
  if (_source) _source.close()
  _state = { phase: 'running', jobKey: null, progress: [], error: null }
  emit()

  const es = new EventSource(path)
  _source = es

  es.addEventListener('start', (e) => {
    _state.jobKey = JSON.parse((e as MessageEvent).data).job_key ?? null
    emit()
  })
  es.addEventListener('progress', (e) => {
    const p = JSON.parse((e as MessageEvent).data) as FactorProgressEvent
    // 服务端重连后从头回放进度, 按 idx 去重 (只追加新增的)
    if (p.idx === _state.progress.length) {
      _state.progress = [..._state.progress, p]
      emit()
    }
  })
  es.addEventListener('done', (e) => {
    const payload = JSON.parse((e as MessageEvent).data)
    applyDone(payload)
    _state.phase = 'done'
    _state.progress = [..._state.progress, { pct: 100, stage: 'done', message: '完成', idx: _state.progress.length }]
    es.close()
    _source = null
    emit()
  })
  es.addEventListener('error', (e) => {
    const raw = (e as MessageEvent).data
    let message: string | null = null
    if (raw) {
      try { message = JSON.parse(raw).message ?? null } catch { message = null }
    }
    // 命名 error 事件 = 任务失败; 无 data 的 error = 传输层 (仅 HTTP 失败才终止)
    if (message !== null) {
      _state.error = message
      _state.phase = 'error'
      es.close()
      _source = null
      emit()
    } else if (es.readyState === EventSource.CLOSED) {
      _state.error = '连接失败, 请重试'
      _state.phase = 'error'
      _source = null
      emit()
    }
  })
}

type SharedParams = { [k: string]: string | number | boolean | undefined }

function _qs(params: SharedParams, extra: SharedParams): string {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries({ ...params, ...extra })) {
    if (v !== undefined && v !== '') qs.set(k, String(v))
  }
  return qs.toString()
}

/** 单因子回测 (SSE)。 */
export function startFactorRun(params: {
  factor_name: string
  asset_type?: string
  symbols?: string
  start?: string
  end?: string
  n_groups?: number
  rebalance?: string
  weight?: string
  fees_pct?: number
  slippage_bps?: number
}) {
  const path = `/api/backtest/factor/stream?${_qs(params, {})}`
  _connect(path, (payload) => {
    _state.result = payload.result as FactorBacktestResult
  })
}

/** 多因子批量评估 (SSE)。 */
export function startFactorBatch(params: {
  factor_names: string
  asset_type?: string
  symbols?: string
  start?: string
  end?: string
  n_groups?: number
  rebalance?: string
  fees_pct?: number
  slippage_bps?: number
}) {
  const path = `/api/backtest/factor/batch/stream?${_qs(params, {})}`
  _connect(path, (payload) => {
    _state.batch = payload.batch as FactorBatchResult
  })
}

export async function cancelFactorTask(): Promise<void> {
  if (!_state.jobKey) return
  try {
    await api.factorCancel(_state.jobKey)
  } catch { /* toast 已由 request 层处理 */ }
}

export function resetFactorTask() {
  if (_source) _source.close()
  _source = null
  _state = { phase: 'idle', jobKey: null, progress: [], error: null }
  emit()
}

export { useFactorTask }