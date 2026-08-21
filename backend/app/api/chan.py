"""缠论 (czsc) 多级别分析 API。薄层: 参数校验 + 响应映射, 计算在 services/chan_analyzer。"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request

from app.services import chan_analyzer, kline_sync

router = APIRouter(prefix="/api/chan", tags=["chan"])


@router.get("/status")
def chan_status() -> dict:
    """缠论能力状态: 依赖是否安装、支持的级别、分钟数据支持范围。"""
    return {
        "installed": chan_analyzer.CHAN_AVAILABLE,
        "engine": chan_analyzer.CHAN_ENGINE_VERSION,
        "supported_freqs": list(chan_analyzer.ALLOWED_FREQS),
        "minute_support": {"stock": False, "etf": False, "index": True},
        "default_days": chan_analyzer.DEFAULT_DAILY_BARS,
        "minute_count_max": kline_sync.TICKFLOW_KLINE_COUNT_MAX,
        "bar_count_max": kline_sync.TICKFLOW_KLINE_COUNT_MAX,
        "tickflow_freqs": [f for f, p in kline_sync.TICKFLOW_CHAN_PERIODS.items() if p != "1d"],
        "synth_from": {"120分钟": "60分钟"},
    }


@router.get("/analysis")
def chan_analysis(
    request: Request,
    symbol: str = Query(..., min_length=1, max_length=20, description="指数代码, 如 000001.SH"),
    days: int = Query(
        kline_sync.TICKFLOW_KLINE_COUNT_MAX,
        ge=60,
        le=kline_sync.TICKFLOW_KLINE_COUNT_MAX,
        description="K线根数上限 (日线请求根数; 分钟级窗口同为 TickFlow count 上限)",
    ),
    freqs: str = Query("日线", max_length=64, description="级别列表, 逗号分隔: 日线,5分钟,15分钟,30分钟,60分钟"),
    start: date | None = Query(None, description="配置起始日期 (优先窗口)"),  # noqa: B008
    end: date | None = Query(None, description="配置结束日期 (优先窗口)"),  # noqa: B008
) -> dict:
    """指数多级别缠论结构分析。个股/ETF 返回 available=false。

    日期优先用 start/end; 窗口内根数不足则退回可得最大条数。分钟级优先 TickFlow
    原生周期, 120分钟由 60分钟合成。
    """
    repo = request.app.state.repo

    def loader(sym: str, period: str, start_dt, end_dt):
        return kline_sync.fetch_index_period(sym, period, start=start_dt, end=end_dt)

    freq_list = tuple(f.strip() for f in freqs.split(",") if f.strip())
    return chan_analyzer.analyze_symbol(
        repo, symbol.strip(), days=days, freqs=freq_list,
        start=start, end=end, kline_loader=loader,
    )


@router.post("/sync_minute")
def chan_sync_minute(
    request: Request,
    symbol: str = Query(..., min_length=1, max_length=20, description="指数代码, 如 000001.SH"),
) -> dict:
    """点击分钟级别且本地无 1m 时由前端调用: 按 TickFlow count 上限同步指数分钟K。

    仅指数落盘 kline_index_minute; 已有可用 1m 时跳过。无分钟权限返回 403。
    """
    result = kline_sync.ensure_symbol_minute_for_chan(
        request.app.state.repo, request.app.state.capabilities, symbol.strip(),
    )
    if result.get("status") == "forbidden":
        raise HTTPException(status_code=403, detail=str(result.get("reason") or "需要分钟K权限"))
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=str(result.get("reason") or "同步失败"))
    return result