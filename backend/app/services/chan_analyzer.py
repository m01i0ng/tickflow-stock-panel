"""缠论 (czsc) 多级别结构分析服务。

定位: enriched / 分钟数据的**单向下游消费者**。数据一律经 KlineRepository 读取
(provider 抽象内), czsc 为可选依赖 (`uv sync --extra chan`):
未安装时 CHAN_AVAILABLE=False, /api/chan 返回"未启用"状态, 主流程不受影响。

口径约定 (与 CONTRIBUTING §3 对齐):
- 日线级吃 enriched 前复权价格 (qfq), 与图表 / 指标口径一致;
- 仅指数 (不复权)。日期窗口优先用调用方 start/end, 根数不足时退回可得最大条数
  (TickFlow count 上限); 分钟级优先拉 TickFlow 原生周期 (1m/5m/10m/15m/30m/60m),
  不提供的级别 (120分钟) 由上一级 (60分钟) 按 A 股时段合成; 个股/ETF fail-closed;
  导出 dt 日线为日期、分钟级带时钟;
- RawBar 的 upper/lower 涨跌停价不填充 (历史 ST 状态无维表, 不虚构口径),
  缠论内部的涨停分类不作为监控/回测的权威裁决;
- 结构只输出已确认分型/笔 + 末端未确认项的标记 (防未来函数展示错误)。

缓存语义:
- 进程内 LRU, 组合键覆盖 symbol/asset_type/请求级别集/窗口/数据指纹/引擎版本;
- 日线指纹 = 最后一根 bar 的 日期|OHLC|量 → 盘中 quote 刷新写入 enriched 后指纹变化
  自动失效, 支撑盘中刷新 (无需手动清缓存);
- 分钟指纹 = 本地分区覆盖 (条数 + 最大 dt) → 盘后管道追加分区后自动失效。
"""

from __future__ import annotations

import contextlib
import logging
import math
import threading
from collections import OrderedDict
from datetime import date, datetime, timedelta
from typing import Any

import polars as pl

from app.services.kline_sync import TICKFLOW_KLINE_COUNT_MAX, tickflow_minute_max_calendar_days

logger = logging.getLogger(__name__)

try:
    from czsc import CZSC, BarGenerator, Freq, format_standard_kline, generate_czsc_signals

    CHAN_AVAILABLE = True
except ImportError:  # pragma: no cover - czsc 未安装的环境 (uv sync --extra chan)
    CHAN_AVAILABLE = False

CHAN_ENGINE_VERSION = "czsc-1.x-dto-6"

# 允许请求的级别: 日线 + 分钟级 (1F/5F/10F/15F/30F/60F/120F, 由本地 1m 合成)
ALLOWED_FREQS = ("日线", "1分钟", "5分钟", "10分钟", "15分钟", "30分钟", "60分钟", "120分钟")
_MINUTE_FREQS = tuple(f for f in ALLOWED_FREQS if f != "日线")

DEFAULT_DAILY_BARS = TICKFLOW_KLINE_COUNT_MAX
# 每标的每级别分析结果内存缓存 (LRU; 组合键含数据指纹)
_CACHE_MAX = 256
_cache: OrderedDict[tuple[str, ...], dict[str, Any]] = OrderedDict()
_cache_lock = threading.Lock()

# 日线级默认信号 (v1 保守集: 函数名在 1.0.x 稳定存在; 配置前按 list_signal_names 校验)
# 全部先算后取最后一根 bar -> 无未来函数。
_SIGNAL_FNS = ("bar_classify_V240607", "bar_amount_acc_V230214")
_RAW_BAR_COLS = ("symbol", "dt", "open", "high", "low", "close", "vol", "amount")


def default_signals_config() -> list[dict[str, Any]]:
    """日线级默认信号配置; czsc 未安装返回 []。"""
    if not CHAN_AVAILABLE:
        return []
    from czsc._native import signals as signals_ns

    names = set(signals_ns.list_signal_names())
    cfg: list[dict[str, Any]] = []
    for fn in _SIGNAL_FNS:
        if fn not in names:
            continue
        cat = signals_ns.get_signal_category(fn)
        cfg.append({"name": f"czsc._native.signals.{cat}.{fn}", "freq": "日线", "params": {}})
    return cfg


# ================================================================
# 数值 / 日期清洗 (JSON 安全)
# ================================================================

def _dstr(v: Any) -> str:
    """Timestamp/date/datetime → 'YYYY-MM-DD' (指纹 / 日线轴)."""
    s = str(v).replace("T", " ")
    return s.split(" ")[0][:10]


def _bar_dt(v: Any, freq_label: str) -> str:
    """日线保持日期; 分钟级保留到秒, 同一交易日的 bar / 笔端点可区分。"""
    s = str(v).replace("T", " ").strip()
    date_part = s.split(" ")[0][:10]
    if freq_label == "日线" or " " not in s:
        return date_part
    time_part = s.split(" ", 1)[1][:8]
    if len(time_part) == 5:
        time_part += ":00"
    return f"{date_part} {time_part}"


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if not math.isfinite(f) else round(f, 4)


def _bar_rows(bars: list[Any], cap: int, freq_label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for b in bars[-cap:]:
        rows.append(
            {
                "dt": _bar_dt(b.dt, freq_label),
                "open": _num(b.open),
                "high": _num(b.high),
                "low": _num(b.low),
                "close": _num(b.close),
                "volume": int(getattr(b, "vol", 0) or 0),
            }
        )
    return rows


# ================================================================
# polars → czsc 边界 (pandas 第二边界: 全在服务入口, 一次性转换)
# ================================================================

def _df_to_bars(df: pl.DataFrame, freq: Any, time_col: str) -> list[Any]:
    """把仓库 DataFrame 转成 czsc RawBar 列表。

    仓库列契约 (daily: date/enriched 列; minute: datetime) → czsc 必需列:
    symbol, dt, open, high, low, close, vol, amount。amount 缺失时填 0。
    """
    rename = {time_col: "dt", "volume": "vol"}
    renamed = df.rename(rename)
    out = renamed.select(
        [
            c
            for c in ("symbol", "dt", "open", "high", "low", "close", "vol", "amount")
            if c in renamed.columns
        ]
    )
    if "amount" not in out.columns:
        out = out.with_columns(amount=pl.lit(0.0, dtype=pl.Float64))
    else:
        out = out.with_columns(pl.col("amount").cast(pl.Float64).fill_null(0.0))
    for c in ("open", "high", "low", "close"):
        if c in out.columns:
            out = out.with_columns(pl.col(c).cast(pl.Float64))
    pdf = out.to_pandas()
    return format_standard_kline(pdf[list(_RAW_BAR_COLS)], freq=freq)


# ================================================================
# 结构与信号提取
# ================================================================

def _extract_level(c: Any, freq_label: str, bars: list[Any], cap: int) -> dict[str, Any]:
    """从 CZSC 对象提取 {bars, fx, bi, zs, summary} (JSON 安全)。

    bars 为喂给引擎的完整窗口 (c.bars_raw 在原生侧可能被裁剪), 导出以入参为准;
    各级别按 cap (TickFlow 单次上限) 截尾, 结构与导出 bars 保持一致窗口。
    """
    fx_list = list(c.fx_list)
    bi_list = list(c.bi_list)
    zs_list = list(c.zs_list)

    # 末端未确认项: ubi = 未完成笔 (含未确认分型); finished_bis = 已确认笔
    prov_fx: set[str] = set()
    with contextlib.suppress(Exception):
        ubi_fxs = (c.ubi or {}).get("fxs") or []
        prov_fx = {_bar_dt(f.dt, freq_label) for f in ubi_fxs}
    finished: set[tuple[str, str]] = set()
    with contextlib.suppress(Exception):
        finished = {(_bar_dt(b.sdt, freq_label), _bar_dt(b.edt, freq_label)) for b in c.finished_bis}

    fx_dto = [
        {
            "dt": _bar_dt(f.dt, freq_label),
            "price": _num(f.fx),
            "mark": str(f.mark) if hasattr(f, "mark") else "",
            "confirmed": _bar_dt(f.dt, freq_label) not in prov_fx,
        }
        for f in fx_list
    ]
    bi_dto = [
        {
            "sdt": _bar_dt(b.sdt, freq_label),
            "edt": _bar_dt(b.edt, freq_label),
            "dir": str(b.direction),
            "sp": _num(b.fx_a.fx),
            "ep": _num(b.fx_b.fx),
            "confirmed": (_bar_dt(b.sdt, freq_label), _bar_dt(b.edt, freq_label)) in finished,
        }
        for b in bi_list
    ]
    zs_dto = [
        {
            "sdt": _bar_dt(z.sdt, freq_label),
            "edt": _bar_dt(z.edt, freq_label),
            "zg": _num(z.zg),
            "zd": _num(z.zd),
            "gg": _num(z.gg),
            "dd": _num(z.dd),
            "dir": str(getattr(z, "sdir", "") or ""),
        }
        for z in zs_list
    ]

    n_fx = len(fx_dto)
    n_bi = len(bi_dto)
    n_conf = sum(1 for b in bi_dto if b["confirmed"])
    n_zs = len(zs_dto)
    exported = _bar_rows(bars, cap, freq_label)
    n_bars = len(exported)
    last_bi = bi_dto[-1] if bi_dto else None
    latest = ""
    if last_bi:
        latest = f"{last_bi['dir']} {'未确认' if not last_bi['confirmed'] else '已确认'}, {last_bi['ep']}"
    summary = (
        f"{freq_label}: {n_bars} 根 · {n_fx} 分型 · {n_conf}/{n_bi} 笔确认 · {n_zs} 中枢"
        f"{(' · 最近一笔 ' + latest) if latest else ''}"
    )

    return {
        "freq": freq_label,
        "bars": exported,
        "fx": fx_dto,
        "bi": bi_dto,
        "zs": zs_dto,
        "summary": summary,
    }


def _latest_signals(bars: list[Any]) -> dict[str, str]:
    """日线级最新信号 (最后一根已完成 bar 的信号值)。失败 → {} (fail-closed)。"""
    cfg = default_signals_config()
    if not cfg or len(bars) < 120:
        return {}
    try:
        sdt = _dstr(bars[0].dt).replace("-", "")
        rows = generate_czsc_signals(bars, cfg, sdt=sdt, init_n=100)
    except Exception as e:  # 信号引擎接口漂移时降级, 不阻断结构分析
        logger.warning("chan signals generation failed: %s", e)
        return {}
    if not rows:
        return {}
    last = rows[-1]
    standard = {"id", "high", "open", "dt", "close", "vol", "amount", "symbol", "freq", "low"}
    return {k: str(v) for k, v in last.items() if k not in standard and v is not None}


# ================================================================
# 数据加载 (经 repository, 不直连数据源)
# ================================================================

def _min_bars(freq_label: str) -> int:
    if freq_label == "日线":
        return 60
    if freq_label == "1分钟":
        return 240
    return 12


def _filter_by_range(df: pl.DataFrame, start: date | None, end: date | None, col: str) -> pl.DataFrame:
    if df.is_empty() or col not in df.columns or (start is None and end is None):
        return df
    work = df
    if start is not None:
        if col == "datetime":
            work = work.filter(pl.col(col).dt.date() >= start)
        else:
            work = work.filter(pl.col(col) >= start)
    if end is not None:
        if col == "datetime":
            work = work.filter(pl.col(col).dt.date() <= end)
        else:
            work = work.filter(pl.col(col) <= end)
    return work


def _prefer_range_or_max(
    df: pl.DataFrame,
    start: date | None,
    end: date | None,
    col: str,
    min_bars: int,
    cap: int = TICKFLOW_KLINE_COUNT_MAX,
) -> pl.DataFrame:
    """配置日期优先; 窗口内根数不足则退回全量可得 (最多 cap)。"""
    if df.is_empty() or col not in df.columns:
        return df
    df = df.sort(col)
    ranged = _filter_by_range(df, start, end, col)
    if ranged.height >= min_bars:
        return ranged.tail(cap)
    return df.tail(cap)


def _load_daily(
    repo: Any,
    symbol: str,
    asset_type: str,
    days: int,
    start: date | None = None,
    end: date | None = None,
) -> pl.DataFrame:
    end_d = end or date.today()
    min_n = _min_bars("日线")
    if start is not None:
        df = repo.get_daily_asset(asset_type, symbol, start, end_d)
        if not df.is_empty() and "date" in df.columns:
            ranged = _filter_by_range(df.sort("date"), start, end_d, "date")
            if ranged.height >= min_n:
                return ranged.tail(days)
    wide_start = end_d - timedelta(days=max(60, int(days * 2.2)))
    df = repo.get_daily_asset(asset_type, symbol, wide_start, end_d)
    if df.is_empty() or "date" not in df.columns:
        return pl.DataFrame()
    return df.sort("date").tail(days)


def _qfq_minute(df: pl.DataFrame, repo: Any, asset_type: str, symbol: str) -> pl.DataFrame:
    """个股/ETF 分钟 OHLC 与日线 enriched 同口径前复权; 指数不复权。"""
    if asset_type not in ("stock", "etf") or df.is_empty() or "datetime" not in df.columns:
        return df
    getter = getattr(repo, "get_adj_factors", None)
    if getter is None:
        return df
    try:
        factors = getter(asset_type, [symbol])
    except TypeError:
        factors = getter(asset_type)
    if factors is None or getattr(factors, "is_empty", lambda: True)():
        return df
    from app.indicators.pipeline import _apply_adj_factor

    work = df.with_columns(pl.col("datetime").dt.date().alias("date"))
    adjusted = _apply_adj_factor(work, factors)
    drop = [c for c in ("date", "ex_rights") if c in adjusted.columns]
    return adjusted.drop(drop) if drop else adjusted


def _load_minute(repo: Any, symbol: str, asset_type: str, days: int) -> pl.DataFrame:
    """本地 1m 分钟K, 窗口与 TickFlow 单次上限对齐 (最多 10000 根)。

    `days` 是日线请求根数, 分钟级不复用该口径, 避免 300 日线把 1m 窗口拉飞。
    """
    del days
    end = date.today()
    lookback = tickflow_minute_max_calendar_days()
    start = end - timedelta(days=max(10, lookback + 10))
    df = repo.get_minute_range([symbol], start, end, asset_type=asset_type)
    if df.is_empty() or "datetime" not in df.columns:
        return _qfq_minute(df, repo, asset_type, symbol)
    df = df.sort("datetime").tail(TICKFLOW_KLINE_COUNT_MAX)
    return _qfq_minute(df, repo, asset_type, symbol)


def _daily_fingerprint(df: pl.DataFrame) -> str:
    """日线数据指纹: 最后一根 bar 的 日期|收|高|低|量 → 盘中刷新自动失效。"""
    if df.is_empty():
        return "empty"
    last = df.tail(1).row(0, named=True)
    vals = [last.get("date"), last.get("close"), last.get("high"), last.get("low"), last.get("volume")]
    return "|".join("" if v is None else (f"{v:.4f}" if isinstance(v, float) else str(v)) for v in vals)


def _minute_cover(df: pl.DataFrame) -> tuple[int, str]:
    if df.is_empty():
        return 0, ""
    return len(df), _dstr(df["datetime"].max())


def _adj_fingerprint(repo: Any, asset_type: str, symbol: str) -> str:
    if asset_type not in ("stock", "etf"):
        return ""
    getter = getattr(repo, "get_adj_factors", None)
    if getter is None:
        return ""
    try:
        factors = getter(asset_type, [symbol])
    except TypeError:
        factors = getter(asset_type)
    if factors is None or getattr(factors, "is_empty", lambda: True)():
        return "none"
    last = factors.sort("trade_date").tail(1).row(0, named=True)
    return f"{last.get('trade_date')}|{last.get('ex_factor')}|{len(factors)}"


# ================================================================
# 级别构建
# ================================================================

def _level_cap(_freq_label: str) -> int:
    return TICKFLOW_KLINE_COUNT_MAX


def _freq_enum(freq_label: str) -> Any:
    return {
        "日线": Freq.D,
        "1分钟": Freq.F1,
        "5分钟": Freq.F5,
        "10分钟": Freq.F10,
        "15分钟": Freq.F15,
        "30分钟": Freq.F30,
        "60分钟": Freq.F60,
        "120分钟": Freq.F120,
    }[freq_label]


def _rawbars_to_df(bars: list[Any]) -> pl.DataFrame:
    if not bars:
        return pl.DataFrame()
    return pl.DataFrame({
        "symbol": [getattr(b, "symbol", "") for b in bars],
        "datetime": [b.dt for b in bars],
        "open": [float(b.open) for b in bars],
        "high": [float(b.high) for b in bars],
        "low": [float(b.low) for b in bars],
        "close": [float(b.close) for b in bars],
        "volume": [float(getattr(b, "vol", 0) or 0) for b in bars],
        "amount": [float(getattr(b, "amount", 0) or 0) for b in bars],
    })


def _synth_ohlc(df: pl.DataFrame, from_freq: str, to_freq: str) -> pl.DataFrame:
    """用更细级别 OHLC 按 A 股时段合成目标级别。"""
    if df.is_empty() or "datetime" not in df.columns or from_freq == to_freq:
        return df
    bars = _df_to_bars(df.sort("datetime"), _freq_enum(from_freq), "datetime")
    bg = BarGenerator(
        base_freq=from_freq, freqs=[to_freq], max_count=len(bars) + 480, market="A股",
    )
    for b in bars:
        bg.update(b)
    return _rawbars_to_df(list(bg.bars.get(to_freq) or []))


def _call_loader(
    loader: Any,
    symbol: str,
    period: str,
    start: date | None,
    end: date | None,
) -> pl.DataFrame:
    if loader is None:
        return pl.DataFrame()
    start_dt = datetime.combine(start, datetime.min.time().replace(hour=9, minute=15)) if start else None
    end_dt = datetime.combine(end, datetime.min.time().replace(hour=15, minute=5)) if end else None
    try:
        df = loader(symbol, period, start_dt, end_dt)
    except TypeError:
        df = loader(symbol, period)
    except Exception as e:  # pragma: no cover
        logger.warning("chan kline_loader(%s, %s) failed: %s", symbol, period, e)
        return pl.DataFrame()
    return df if df is not None else pl.DataFrame()


def _obtain_freq_df(
    freq_label: str,
    repo: Any,
    symbol: str,
    asset_type: str,
    start: date | None,
    end: date | None,
    loader: Any,
    memo: dict[str, tuple[pl.DataFrame, str, str | None]],
) -> tuple[pl.DataFrame, str, str | None]:
    """优先 TickFlow 原生周期; 否则从上一个更细级别合成; 最后退回本地 1m。

    返回 (df, source, parent_freq). source 为 native / synth / local。
    """
    from app.services import kline_sync

    if freq_label in memo:
        return memo[freq_label]

    cap = _level_cap(freq_label)
    min_n = _min_bars(freq_label)
    period = kline_sync.tickflow_period_for_chan_freq(freq_label)
    if period:
        native = _call_loader(loader, symbol, period, start, end)
        native = _prefer_range_or_max(native, start, end, "datetime", min_n, cap)
        if native.height < min_n and loader is not None:
            extra = _call_loader(loader, symbol, period, None, None)
            extra = _prefer_range_or_max(extra, None, None, "datetime", min_n, cap)
            if extra.height > native.height:
                native = extra
        if native.height >= min_n:
            hit = (native, "native", None)
            memo[freq_label] = hit
            return hit

    parent = kline_sync.chan_synth_parent(freq_label)
    while parent:
        parent_df, _, _ = _obtain_freq_df(
            parent, repo, symbol, asset_type, start, end, loader, memo,
        )
        if not parent_df.is_empty():
            synth = _synth_ohlc(parent_df, parent, freq_label)
            synth = _prefer_range_or_max(synth, start, end, "datetime", min_n, cap)
            if not synth.is_empty():
                hit = (synth, "synth", parent)
                memo[freq_label] = hit
                return hit
        parent = kline_sync.chan_synth_parent(parent)

    if freq_label != "1分钟":
        local_1m = _load_minute(repo, symbol, asset_type, cap)
        local_1m = _prefer_range_or_max(local_1m, start, end, "datetime", _min_bars("1分钟"), cap)
        if local_1m.height >= _min_bars("1分钟"):
            synth = _synth_ohlc(local_1m, "1分钟", freq_label)
            synth = _prefer_range_or_max(synth, start, end, "datetime", min_n, cap)
            if not synth.is_empty():
                hit = (synth, "local", "1分钟")
                memo[freq_label] = hit
                return hit
    elif freq_label == "1分钟":
        local_1m = _load_minute(repo, symbol, asset_type, cap)
        local_1m = _prefer_range_or_max(local_1m, start, end, "datetime", min_n, cap)
        if not local_1m.is_empty():
            hit = (local_1m, "local", None)
            memo[freq_label] = hit
            return hit

    hit = (pl.DataFrame(), "none", None)
    memo[freq_label] = hit
    return hit


def _build_daily_level(
    repo: Any,
    symbol: str,
    asset_type: str,
    days: int,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any] | None:
    df = _load_daily(repo, symbol, asset_type, days, start=start, end=end)
    if df.is_empty() or len(df) < 60:
        return None
    bars = _df_to_bars(df, Freq.D, "date")
    # max_bi_num 默认 50 会裁剪早期 bars (结构+导出都不完整), 这里按窗口全量保留
    c = CZSC(bars, max_bi_num=len(bars))
    level = _extract_level(c, "日线", bars, _level_cap("日线"))
    level["signals"] = _latest_signals(c.bars_raw)
    return level


def _build_minute_level(df: pl.DataFrame, freq_label: str, *, from_1m: bool = True) -> dict[str, Any] | None:
    """from_1m=True: 入参为 1m, 合成到目标级别 (测试兜底)。False: 入参已是目标级别 OHLC。"""
    if df.is_empty() or "datetime" not in df.columns:
        return None
    cap = _level_cap(freq_label)
    if from_1m:
        if len(df) < 240:
            return None
        bars1m = _df_to_bars(df.sort("datetime"), Freq.F1, "datetime")[-cap:]
        if freq_label == "1分钟":
            window = bars1m
        else:
            bg = BarGenerator(
                base_freq="1分钟", freqs=[freq_label], max_count=len(bars1m) + 480, market="A股",
            )
            for b in bars1m:
                bg.update(b)
            window = list(bg.bars.get(freq_label) or [])[-cap:]
    else:
        if len(df) < _min_bars(freq_label):
            return None
        window = _df_to_bars(df.sort("datetime"), _freq_enum(freq_label), "datetime")[-cap:]
    if len(window) < 12:
        return None
    c = CZSC(window, max_bi_num=len(window))
    if len(c.fx_list) < 4:
        return None
    return _extract_level(c, freq_label, window, cap)


# ================================================================
# 聚合入口 + 缓存
# ================================================================

def _cache_get(key: tuple[str, ...]) -> dict[str, Any] | None:
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
        return hit


def _cache_put(key: tuple[str, ...], value: dict[str, Any]) -> None:
    with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


def _minute_fail_reason(minute_df: pl.DataFrame, freq: str, asset_type: str) -> str:
    rows = len(minute_df)
    need = _min_bars(freq)
    if rows == 0:
        return f"{freq} 未生成: 本地无分钟K数据(点击该级别后将自动同步)"
    if rows < need:
        return f"{freq} 未生成: K线覆盖不足({rows} 根 < {need} 根)"
    return f"{freq} 未生成: 结构不足, 已跳过"


def analyze_symbol(
    repo: Any,
    symbol: str,
    days: int = DEFAULT_DAILY_BARS,
    freqs: tuple[str, ...] = ("日线",),
    start: date | None = None,
    end: date | None = None,
    kline_loader: Any | None = None,
) -> dict[str, Any]:
    """分析单个指数的请求级别缠论结构 (逐级计算 + 组合缓存)。返回 JSON 安全 DTO。"""
    if not CHAN_AVAILABLE:
        return {
            "available": False,
            "symbol": symbol,
            "reason": "缠论分析未启用: 后端未安装 czsc (uv sync --extra chan)",
            "levels": [],
            "warnings": [],
        }

    asset_type = repo.resolve_asset_type(symbol)
    if asset_type != "index":
        return {
            "available": False,
            "symbol": symbol,
            "asset_type": asset_type,
            "reason": "缠论分析仅支持指数",
            "levels": [],
            "warnings": [],
        }
    freqs = tuple(f for f in freqs if f in ALLOWED_FREQS) or ("日线",)
    has_daily = "日线" in freqs
    minute_freqs = tuple(f for f in freqs if f in _MINUTE_FREQS)

    generation = ""
    with contextlib.suppress(Exception):
        generation = str(repo.get_matrix_data_generation(asset_type) or "")

    daily_df = (
        _load_daily(repo, symbol, asset_type, days, start=start, end=end)
        if has_daily else pl.DataFrame()
    )
    daily_fp = _daily_fingerprint(daily_df)
    memo: dict[str, tuple[pl.DataFrame, str, str | None]] = {}
    minute_cover_parts: list[str] = []
    for f in minute_freqs:
        df, src, parent = _obtain_freq_df(
            f, repo, symbol, asset_type, start, end, kline_loader, memo,
        )
        minute_cover_parts.append(f"{f}:{src}:{parent or ''}:{_minute_cover(df)[0]}")
    adj_fp = _adj_fingerprint(repo, asset_type, symbol)

    key = (
        symbol,
        asset_type,
        ",".join(freqs),
        str(days),
        start.isoformat() if start else "",
        end.isoformat() if end else "",
        daily_fp,
        "|".join(minute_cover_parts),
        adj_fp,
        generation,
        CHAN_ENGINE_VERSION,
    )
    cached = _cache_get(key)
    if cached is not None:
        return cached

    warnings: list[str] = []
    levels: list[dict[str, Any]] = []
    if has_daily:
        daily = _build_daily_level(repo, symbol, asset_type, days, start=start, end=end)
        if daily is None:
            result = {
                "available": False,
                "symbol": symbol,
                "reason": "本地日K数据不足(少于 60 根), 请先在数据页同步日K与 enriched",
                "levels": [],
                "warnings": warnings,
            }
            _cache_put(key, result)
            return result
        levels.append(daily)
    for f in minute_freqs:
        df, src, parent = _obtain_freq_df(
            f, repo, symbol, asset_type, start, end, kline_loader, memo,
        )
        level = _build_minute_level(df, f, from_1m=False)
        if src == "synth" and parent:
            warnings.append(f"{f} 由 {parent} 合成 (TickFlow 不提供该周期)")
        elif src == "local" and f != "1分钟":
            warnings.append(f"{f} 由本地 1m 合成")
        if level is None:
            warnings.append(_minute_fail_reason(df, f, asset_type))
        else:
            levels.append(level)

    result = {
        "available": True,
        "symbol": symbol,
        "asset_type": asset_type,
        "days": days,
        "levels": levels,
        "warnings": warnings,
    }
    _cache_put(key, result)
    return result