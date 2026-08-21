"""缠论 (czsc) 分析服务与 API 契约测试 — 全部离线 (合成数据), 无网络依赖。"""

from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.chan import router as chan_router
from app.services import chan_analyzer

pytestmark = pytest.mark.skipif(
    not chan_analyzer.CHAN_AVAILABLE, reason="czsc 未安装 (uv sync --extra chan)"
)

SYMBOL = "000001.SH"


def _trading_days(n: int, end: date) -> list[date]:
    """最近 n 个交易日 (跳过周末)。"""
    out: list[date] = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def _daily_df(n: int = 400, seed: int = 7) -> pl.DataFrame:
    rng = random.Random(seed)
    days = _trading_days(n, date.today())
    price = 10.0
    rows = []
    for d in days:
        o = price
        c = max(0.5, o * (1 + rng.gauss(0, 0.02)))
        h = max(o, c) * (1 + rng.random() * 0.01)
        lo = min(o, c) * (1 - rng.random() * 0.01)
        rows.append(
            {
                "symbol": SYMBOL,
                "date": d,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": rng.randint(100_000, 900_000),
                "amount": c * rng.randint(100_000, 900_000),
            }
        )
        price = c
    return pl.DataFrame(rows)


def _minute_df(days: int = 60, seed: int = 11) -> pl.DataFrame:
    """A 股交易时段 1m 分钟K: 09:30-11:29 + 13:00-14:59, 每日 240 根。"""
    rng = random.Random(seed)
    tdays = _trading_days(days, date.today())
    price = 10.0
    rows = []
    for d in tdays:
        times = [datetime(d.year, d.month, d.day, 9, 30) + timedelta(minutes=i) for i in range(120)]
        times += [datetime(d.year, d.month, d.day, 13, 0) + timedelta(minutes=i) for i in range(120)]
        for t in times:
            o = price
            c = max(0.5, o * (1 + rng.gauss(0, 0.002)))
            h = max(o, c) * 1.001
            lo = min(o, c) * 0.999
            rows.append(
                {
                    "symbol": SYMBOL,
                    "datetime": t,
                    "open": o,
                    "high": h,
                    "low": lo,
                    "close": c,
                    "volume": 1000,
                    "amount": c * 1000,
                }
            )
            price = c
    return pl.DataFrame(rows)


class _StubRepo:
    """仅暴露 chan_analyzer 使用的 repository 子集。"""

    def __init__(self, daily=None, minute=None, asset_type: str = "index", adj=None):
        self._daily = daily if daily is not None else pl.DataFrame()
        self._minute = minute if minute is not None else pl.DataFrame()
        self._adj = adj if adj is not None else pl.DataFrame()
        self._asset = asset_type
        self.generation = "gen-test-1"

    def resolve_asset_type(self, symbol: str) -> str:
        return self._asset

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        return self._daily

    def get_minute_range(self, symbols, start, end, asset_type="stock"):
        return self._minute

    def get_adj_factors(self, asset_type: str = "stock", symbols=None):
        return self._adj

    def get_matrix_data_generation(self, asset_type="stock") -> str:
        return self.generation


def test_daily_analysis_contract():
    repo = _StubRepo(daily=_daily_df())
    result = chan_analyzer.analyze_symbol(repo, SYMBOL, days=400, freqs=("日线",))

    assert result["available"] is True
    assert result["asset_type"] == "index"
    assert not result["warnings"]
    assert len(result["levels"]) == 1
    level = result["levels"][0]
    assert level["freq"] == "日线"
    assert len(level["bars"]) == 400
    dts = [b["dt"] for b in level["bars"]]
    assert all(len(d) == 10 for d in dts)
    assert dts == sorted(dts)  # 交易日升序
    assert len(level["fx"]) >= 4
    assert len(level["bi"]) >= 1
    # 挥确认结构: 未确认笔至多 1 根 (末端)
    assert sum(1 for b in level["bi"] if b["confirmed"]) >= len(level["bi"]) - 1
    # 笔端点价格落在历史价格区间内 (防止复权/单位错位)
    lo = min(b["low"] for b in level["bars"])
    hi = max(b["high"] for b in level["bars"])
    for b in level["bi"]:
        assert lo <= b["sp"] <= hi
        assert lo <= b["ep"] <= hi
    # 分型价 = 该 bar 的极值附近 (缠论定义: 顶分型高点是三根中最高)
    for f in level["fx"]:
        assert lo <= f["price"] <= hi
    # 信号键存在 (不强制非空: 固定样本数据可能中性)
    assert isinstance(level["signals"], dict)
    # JSON 安全 (无 Timestamp 泄漏)
    json.dumps(result)


def test_daily_result_cached_and_fresh():
    repo = _StubRepo(daily=_daily_df())
    first = chan_analyzer.analyze_symbol(repo, SYMBOL, days=400, freqs=("日线",))
    second = chan_analyzer.analyze_symbol(repo, SYMBOL, days=400, freqs=("日线",))
    assert first is second  # 命中同一缓存对象

    # different days → miss
    other = chan_analyzer.analyze_symbol(repo, SYMBOL, days=250, freqs=("日线",))
    assert other is not first


def test_no_daily_data_fail_closed():
    repo = _StubRepo(daily=pl.DataFrame())
    result = chan_analyzer.analyze_symbol(repo, SYMBOL, freqs=("日线",))
    assert result["available"] is False
    assert not result["levels"]


# ================================================================
# 分钟多级别
# ================================================================

def test_minute_multilevel_synthesis():
    from app.services import kline_sync

    minute = _minute_df(days=60)
    repo = _StubRepo(daily=_daily_df(), minute=minute)
    result = chan_analyzer.analyze_symbol(repo, SYMBOL, days=400, freqs=("日线", "5分钟", "30分钟"))

    assert result["available"] is True
    freqs = [lv["freq"] for lv in result["levels"]]
    assert freqs == ["日线", "5分钟", "30分钟"]
    m30 = result["levels"][2]
    m5 = result["levels"][1]
    # 分钟窗口按 TickFlow 上限 10000 根 1m 截尾后再合成 (5F≈48/日, 30F=8/日)
    cap = kline_sync.TICKFLOW_KLINE_COUNT_MAX
    assert len(minute) > cap
    assert 1800 <= len(m5["bars"]) <= 2100
    assert 300 <= len(m30["bars"]) <= 360
    assert f"{len(m5['bars'])} 根" in m5["summary"]
    daily_dts = [b["dt"] for b in result["levels"][0]["bars"]]
    assert all(len(d) == 10 for d in daily_dts)
    for lv in (m5, m30):
        dts = [b["dt"] for b in lv["bars"]]
        assert len(dts) == len(set(dts)), f"{lv['freq']} dt 必须唯一, 不能裁成日期"
        assert all(" " in d and len(d) >= 16 for d in dts), f"{lv['freq']} dt 必须带时钟"
    same_day_bi = [
        b for b in m5["bi"]
        if b["sdt"][:10] == b["edt"][:10] and b["sdt"] != b["edt"]
    ]
    assert same_day_bi, "5F 必须能区分同一交易日的笔端点"
    json.dumps(result)


def test_minute_missing_graceful():
    repo = _StubRepo(daily=_daily_df(), minute=pl.DataFrame())
    result = chan_analyzer.analyze_symbol(repo, SYMBOL, days=400, freqs=("日线", "5分钟"))
    assert result["available"] is True
    assert [lv["freq"] for lv in result["levels"]] == ["日线"]
    assert any("分钟K" in w for w in result["warnings"])


def test_minute_new_levels_lunch_aware():
    """1F/10F/60F/120F 合成: A 股午休边界下的 bar 数量 (60m=4/日, 120m=2/日)。

    41 个完整交易日 = 9840 根 1m, 低于 TickFlow 10000 上限, 便于按整日断言。
    """
    days = 41
    minute = _minute_df(days=days)
    repo = _StubRepo(daily=_daily_df(), minute=minute)
    result = chan_analyzer.analyze_symbol(
        repo, SYMBOL, days=400, freqs=("1分钟", "10分钟", "60分钟", "120分钟")
    )

    assert result["available"] is True
    by_freq = {lv["freq"]: lv for lv in result["levels"]}
    assert set(by_freq) == {"1分钟", "10分钟", "60分钟", "120分钟"}
    assert len(by_freq["1分钟"]["bars"]) == days * 240
    assert len(by_freq["10分钟"]["bars"]) == days * 24
    assert len(by_freq["60分钟"]["bars"]) == days * 4
    assert len(by_freq["120分钟"]["bars"]) == days * 2
    assert f"{days * 240} 根" in by_freq["1分钟"]["summary"]
    for lv in result["levels"]:
        dts = [b["dt"] for b in lv["bars"]]
        assert len(dts) == len(set(dts))
        assert all(" " in d for d in dts)
    json.dumps(result)


def test_daily_prefers_configured_range_then_max():
    """配置 start/end 内够 60 根则用窗口; 不足则退回全部可得。"""
    daily = _daily_df(n=200)
    repo = _StubRepo(daily=daily)
    dates = sorted(daily["date"].to_list())
    start, end = dates[-80], dates[-1]
    ranged = chan_analyzer.analyze_symbol(
        repo, SYMBOL, days=400, freqs=("日线",), start=start, end=end,
    )
    n = len(ranged["levels"][0]["bars"])
    assert 70 <= n <= 80

    short = chan_analyzer.analyze_symbol(
        repo, SYMBOL, days=400, freqs=("日线",), start=dates[-10], end=dates[-1],
    )
    assert len(short["levels"][0]["bars"]) == 200


def test_native_period_preferred_over_1m_synth():
    """TickFlow 有 5m 时用原生 5m 根数, 不用 1m 合成。"""
    local_1m = _minute_df(days=50)
    native_5m = _minute_df(days=10)  # 2400 根, 大于 1m 上限合成的 ~2000 根 5F

    def loader(_symbol, period, _start, _end):
        if period == "5m":
            return native_5m
        return pl.DataFrame()

    repo = _StubRepo(daily=_daily_df(), minute=local_1m)
    result = chan_analyzer.analyze_symbol(
        repo, SYMBOL, days=400, freqs=("5分钟",), kline_loader=loader,
    )
    assert result["available"] is True
    assert len(result["levels"][0]["bars"]) == 10 * 240
    assert not any("本地 1m" in w for w in result["warnings"])


def test_120m_synthesizes_from_60m_when_no_tickflow_120():
    """TickFlow 无 120m: 用 60m 原生合成, 即使本地 1m 为空。"""
    df60 = chan_analyzer._synth_ohlc(_minute_df(days=20), "1分钟", "60分钟")

    def loader(_symbol, period, _start, _end):
        if period == "60m":
            return df60
        return pl.DataFrame()

    repo = _StubRepo(daily=_daily_df(), minute=pl.DataFrame())
    result = chan_analyzer.analyze_symbol(
        repo, SYMBOL, days=400, freqs=("120分钟",), kline_loader=loader,
    )
    assert result["available"] is True
    n = len(result["levels"][0]["bars"])
    assert 20 <= n <= 40
    assert any("60分钟" in w for w in result["warnings"])


def test_all_minute_levels_use_tickflow_count_max_window():
    """所有分钟级别都从同一段 TickFlow 上限 1m 计算; 1F 导出 10000 根, 派生级不超过该窗口。"""
    from app.services import kline_sync

    cap = kline_sync.TICKFLOW_KLINE_COUNT_MAX
    minute = _minute_df(days=50)  # 12000 根 > 10000
    repo = _StubRepo(daily=_daily_df(), minute=minute)
    result = chan_analyzer.analyze_symbol(
        repo, SYMBOL, days=400, freqs=("1分钟", "5分钟", "10分钟", "15分钟", "30分钟", "60分钟", "120分钟"),
    )
    assert result["available"] is True
    by_freq = {lv["freq"]: lv for lv in result["levels"]}
    assert len(by_freq["1分钟"]["bars"]) == cap
    assert f"{cap} 根" in by_freq["1分钟"]["summary"]
    # 派生级由这 10000 根 1m 合成, 不是 50 个交易日全量 (5F 全量会到 2400)
    assert 1800 <= len(by_freq["5分钟"]["bars"]) <= 2100
    assert 900 <= len(by_freq["10分钟"]["bars"]) <= 1100
    assert 600 <= len(by_freq["15分钟"]["bars"]) <= 720
    assert 300 <= len(by_freq["30分钟"]["bars"]) <= 400
    assert 150 <= len(by_freq["60分钟"]["bars"]) <= 180
    assert 70 <= len(by_freq["120分钟"]["bars"]) <= 90
    for freq, lv in by_freq.items():
        assert f"{len(lv['bars'])} 根" in lv["summary"], freq
        assert len(lv["bars"]) <= cap


class _MutableRepo(_StubRepo):
    """日线数据可变的 stub: 模拟盘中 quote 刷新写入 enriched 的最后一根 bar。"""

    def set_daily(self, df: pl.DataFrame) -> None:
        self._daily = df


def test_intraday_refresh_invalidates_cache():
    """盘中刷新语义: 最后一根日 bar 值变化 → 指纹变化 → 缓存自动失效重算。"""
    base = _daily_df()
    repo = _MutableRepo(daily=base)
    first = chan_analyzer.analyze_symbol(repo, SYMBOL, days=400, freqs=("日线",))
    assert first["available"] is True

    new_close = float(first["levels"][0]["bars"][-1]["close"]) * 1.05
    mutated = base.with_columns(
        pl.when(pl.col("date") == pl.col("date").max())
        .then(pl.lit(new_close))
        .otherwise(pl.col("close"))
        .alias("close")
    )
    repo.set_daily(mutated)

    second = chan_analyzer.analyze_symbol(repo, SYMBOL, days=400, freqs=("日线",))
    assert second is not first
    assert abs(second["levels"][0]["bars"][-1]["close"] - new_close) < 1e-4


def test_index_minute_supported_with_data():
    """指数分钟落盘后: 与个股同链路的多级别分析。"""
    repo = _StubRepo(daily=_daily_df(), minute=_minute_df(days=10), asset_type="index")
    result = chan_analyzer.analyze_symbol(repo, "000001.SH", days=400, freqs=("日线", "5分钟"))
    assert result["available"] is True
    assert [lv["freq"] for lv in result["levels"]] == ["日线", "5分钟"]
    assert not any("未持久化" in w for w in result["warnings"])


def test_index_minute_missing_hints_sync():
    repo = _StubRepo(daily=_daily_df(), minute=pl.DataFrame(), asset_type="index")
    result = chan_analyzer.analyze_symbol(repo, "000001.SH", days=400, freqs=("日线", "5分钟"))
    assert result["available"] is True
    assert [lv["freq"] for lv in result["levels"]] == ["日线"]
    assert any("分钟K" in w for w in result["warnings"])
    assert any("自动同步" in w for w in result["warnings"])


# ================================================================
# 依赖缺失 / API 层
# ================================================================

def test_unavailable_when_czsc_missing(monkeypatch):
    monkeypatch.setattr(chan_analyzer, "CHAN_AVAILABLE", False)
    repo = _StubRepo(daily=_daily_df())
    result = chan_analyzer.analyze_symbol(repo, SYMBOL, freqs=("日线",))
    assert result["available"] is False
    assert "未启用" in result["reason"]


def _chan_app(repo: _StubRepo) -> FastAPI:
    app = FastAPI()
    app.include_router(chan_router)
    app.state.repo = repo
    return app


def test_chan_api_status_and_analysis():
    client = TestClient(_chan_app(_StubRepo(daily=_daily_df())))
    status = client.get("/api/chan/status")
    assert status.status_code == 200
    body = status.json()
    assert body["installed"] is True
    assert set(body["supported_freqs"]) == set(chan_analyzer.ALLOWED_FREQS)
    assert body["minute_support"] == {"stock": False, "etf": False, "index": True}
    assert body["bar_count_max"] == 10000

    r = client.get("/api/chan/analysis", params={"symbol": SYMBOL, "days": 300, "freqs": "日线,30分钟"})
    assert r.status_code == 200
    payload = r.json()
    assert payload["available"] is True
    assert payload["symbol"] == SYMBOL
    assert payload["levels"][0]["freq"] == "日线"

    # 参数校验: days 越界 → 422
    bad = client.get("/api/chan/analysis", params={"symbol": SYMBOL, "days": 1})
    assert bad.status_code == 422
    assert status.json()["minute_count_max"] == 10000
    assert status.json()["default_days"] == 10000
    over = client.get("/api/chan/analysis", params={"symbol": SYMBOL, "days": 10001})
    assert over.status_code == 422

    stock_client = TestClient(_chan_app(_StubRepo(daily=_daily_df(), asset_type="stock")))
    denied = stock_client.get("/api/chan/analysis", params={"symbol": "000001.SZ", "days": 300, "freqs": "日线"})
    assert denied.status_code == 200
    assert denied.json()["available"] is False
    assert "指数" in denied.json()["reason"]


def test_index_minute_persist_routing_and_chan(tmp_path, monkeypatch):
    """指数分钟落盘全链路: 同步 → kline_index_minute 分区 → 资产路由 → 缠论指数分钟级。"""
    from app.services import index_sync, kline_sync
    from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore(data_dir=tmp_path)
    repo = KlineRepository(store)
    repo.save_index_instruments(
        pl.DataFrame({
            "symbol": ["000001.SH"], "name": ["上证指数"], "code": ["000001"], "asset_type": ["index"],
        })
    )
    assert repo.resolve_asset_type("000001.SH") == "index"

    minute = _minute_df(days=10).with_columns(symbol=pl.lit("000001.SH"))  # 2400 根, A 股交易时段
    captured: list[str] = []

    def fake_sync_minute_batch(symbols, start_time=None, end_time=None, batch_size=None, rpm=None,
                               on_chunk_done=None, segment_trading_days=None, on_segment=None,
                               asset_type="stock"):
        captured.append(asset_type)
        if on_segment:
            on_segment(minute)
        return pl.DataFrame()

    monkeypatch.setattr(kline_sync, "sync_minute_batch", fake_sync_minute_batch)
    caps = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits(rpm=30, batch=50)})

    rows1 = index_sync.sync_and_persist_index_minute(repo, caps, ["000001.SH"], days=10)
    assert rows1 == len(minute)
    assert captured == ["index"]
    # 幂等: 同区间二次同步去重后行数不变
    rows2 = index_sync.sync_and_persist_index_minute(repo, caps, ["000001.SH"], days=10)
    assert rows2 == len(minute)

    # 资产路由: index 读 kline_index_minute, 股票目录不受污染
    end = date.today()
    start = end - timedelta(days=20)
    idx_df = repo.get_minute_range(["000001.SH"], start, end, asset_type="index")
    assert idx_df.height == minute.height
    stock_df = repo.get_minute_range(["000001.SH"], start, end, asset_type="stock")
    assert stock_df.is_empty()

    # 缠论: 指数 + 本地分钟数据 → 5分钟级可产出 (日线级依赖 kline_index_daily, 本用例不请求)
    result = chan_analyzer.analyze_symbol(repo, "000001.SH", days=400, freqs=("5分钟",))
    assert result["available"] is True
    assert [lv["freq"] for lv in result["levels"]] == ["5分钟"]
    assert not any("同步指数分钟" in w for w in result["warnings"])
    json.dumps(result)


def test_chan_rejects_stock_and_etf():
    daily = _daily_df()
    minute = _minute_df(days=10)
    stock = chan_analyzer.analyze_symbol(
        _StubRepo(daily=daily, minute=minute, asset_type="stock"),
        "000001.SZ", days=400, freqs=("日线", "5分钟"),
    )
    assert stock["available"] is False
    assert "指数" in stock["reason"]
    assert not stock["levels"]

    etf = chan_analyzer.analyze_symbol(
        _StubRepo(daily=daily, minute=minute, asset_type="etf"),
        "510300.SH", days=400, freqs=("日线", "5分钟"),
    )
    assert etf["available"] is False
    assert "指数" in etf["reason"]


def test_index_minute_ignores_adj_factor():
    """指数分钟保持原始价, 不套用除权因子。"""
    tdays = _trading_days(10, date.today())
    ex_date = tdays[5]
    ex_s = ex_date.isoformat()
    factors = pl.DataFrame({
        "symbol": [SYMBOL],
        "trade_date": [ex_date],
        "ex_factor": [2.0],
    })
    minute = _minute_df(days=10)
    raw = chan_analyzer.analyze_symbol(
        _StubRepo(minute=minute, asset_type="index"),
        SYMBOL, days=400, freqs=("5分钟",),
    )
    index = chan_analyzer.analyze_symbol(
        _StubRepo(minute=minute, adj=factors, asset_type="index"),
        SYMBOL, days=400, freqs=("5分钟",),
    )
    assert raw["available"] and index["available"]
    raw_pre = next(b for b in raw["levels"][0]["bars"] if b["dt"][:10] < ex_s)
    idx_pre = next(b for b in index["levels"][0]["bars"] if b["dt"][:10] < ex_s)
    assert abs(idx_pre["close"] - raw_pre["close"]) < 1e-3


def test_index_daily_overlays_live_last_bar(tmp_path):
    """指数日线吃内存 live 最后一根, 不依赖监控规则。"""
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore(data_dir=tmp_path)
    repo = KlineRepository(store)
    repo.save_index_instruments(
        pl.DataFrame({
            "symbol": ["000001.SH"], "name": ["上证指数"], "code": ["000001"], "asset_type": ["index"],
        })
    )
    daily = _daily_df(n=80).with_columns(symbol=pl.lit("000001.SH"))
    parquet_path = store.data_dir / "kline_index_enriched" / "part.parquet"
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    daily.write_parquet(parquet_path)

    last_date = daily["date"].max()
    parquet_close = float(daily.filter(pl.col("date") == last_date)["close"][0])
    live_close = parquet_close * 1.07
    live = daily.filter(pl.col("date") == last_date).with_columns(pl.lit(live_close).alias("close"))
    repo._index_enriched_cache = live
    repo._index_enriched_cache_date = last_date

    result = chan_analyzer.analyze_symbol(repo, "000001.SH", days=80, freqs=("日线",))
    assert result["available"] is True
    assert result["asset_type"] == "index"
    assert abs(result["levels"][0]["bars"][-1]["close"] - live_close) < 1e-4
    assert abs(result["levels"][0]["bars"][-1]["close"] - parquet_close) > 1e-3


def test_index_minute_sync_default_lookback():
    import inspect

    from app.api.indices import sync_index_minute

    default = inspect.signature(sync_index_minute).parameters["days"].default
    assert default.default >= 40
    assert default.default <= 60