"""缠论分钟级自动同步: TickFlow count 上限 + 个股/指数资产路由。离线, 无网络。"""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timedelta

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.chan import router as chan_router
from app.services import kline_sync
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

STOCK = "000001.SZ"
INDEX = "000001.SH"


def _caps() -> CapabilitySet:
    return CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits(rpm=30, batch=50)})


def _minute_rows(symbol: str, n: int, start: datetime | None = None) -> pl.DataFrame:
    start = start or datetime.now().replace(hour=9, minute=30, second=0, microsecond=0)
    rows = []
    t = start
    price = 10.0
    while len(rows) < n:
        if t.weekday() >= 5:
            t += timedelta(days=1)
            continue
        rows.append({
            "symbol": symbol,
            "datetime": t,
            "open": price,
            "high": price + 0.01,
            "low": price - 0.01,
            "close": price,
            "volume": 1000,
            "amount": price * 1000,
        })
        t += timedelta(minutes=1)
        if t.hour >= 11 and t.minute >= 30:
            t = t.replace(hour=13, minute=0)
        if t.hour >= 15:
            t = (t + timedelta(days=1)).replace(hour=9, minute=30)
    return pl.DataFrame(rows)


def test_tickflow_kline_count_max_from_sdk_docs():
    """生产常量必须等于 TickFlow SDK 声明的 klines.get count 上限 (max 10000)。"""
    from tickflow.resources.klines import Klines

    doc = inspect.getdoc(Klines.get) or ""
    match = re.search(r"max\s+(\d+)", doc, re.I)
    assert match, f"TickFlow SDK klines.get 未声明 count 上限: {doc!r}"
    sdk_max = int(match.group(1))
    assert sdk_max == kline_sync.TICKFLOW_KLINE_COUNT_MAX
    assert sdk_max == 10000


def test_tickflow_chan_period_map_and_synth_parent():
    """TickFlow 原生周期与 120分钟合成父级 (SDK Period 无 120m)。"""
    from typing import get_args

    from tickflow.generated_model import Period

    periods = set(get_args(Period))
    assert {"1m", "5m", "10m", "15m", "30m", "60m", "1d"} <= periods
    assert "120m" not in periods

    assert kline_sync.tickflow_period_for_chan_freq("1分钟") == "1m"
    assert kline_sync.tickflow_period_for_chan_freq("5分钟") == "5m"
    assert kline_sync.tickflow_period_for_chan_freq("60分钟") == "60m"
    assert kline_sync.tickflow_period_for_chan_freq("日线") == "1d"
    assert kline_sync.tickflow_period_for_chan_freq("120分钟") is None
    assert kline_sync.chan_synth_parent("120分钟") == "60分钟"
    assert kline_sync.chan_synth_parent("5分钟") == "1分钟"
    assert kline_sync.chan_synth_parent("1分钟") is None


def test_tickflow_minute_lookback_fits_single_request():
    """自然日回溯换算后的 1m 根数不超过单次 count 上限。"""
    days = kline_sync.tickflow_minute_max_calendar_days()
    bars = days * 5 / 7 * kline_sync.A_SHARE_MINUTE_BARS_PER_DAY
    assert bars <= kline_sync.TICKFLOW_KLINE_COUNT_MAX
    assert bars >= kline_sync.TICKFLOW_KLINE_COUNT_MAX * 0.8


def test_ensure_stock_minute_rejected(tmp_path, monkeypatch):
    """缠论分钟同步拒绝个股, 不得写入 kline_minute。"""
    store = DataStore(data_dir=tmp_path)
    repo = KlineRepository(store)

    def boom(*args, **kwargs):
        raise AssertionError("个股不得走缠论分钟同步")

    monkeypatch.setattr(kline_sync, "sync_minute_batch", boom)
    result = kline_sync.ensure_symbol_minute_for_chan(repo, _caps(), STOCK)
    assert result["status"] == "error"
    assert "指数" in str(result["reason"])
    assert not any((tmp_path / "kline_minute").rglob("*.parquet"))
    assert not any((tmp_path / "kline_index_minute").rglob("*.parquet"))


def test_ensure_index_minute_writes_index_store(tmp_path, monkeypatch):
    store = DataStore(data_dir=tmp_path)
    repo = KlineRepository(store)
    repo.save_index_instruments(pl.DataFrame({
        "symbol": [INDEX], "name": ["上证指数"], "code": ["000001"], "asset_type": ["index"],
    }))
    assert repo.resolve_asset_type(INDEX) == "index"

    def fake_batch(symbols, start_time=None, end_time=None, count=None, batch_size=None,
                   rpm=None, on_chunk_done=None, segment_trading_days=None, on_segment=None,
                   asset_type="stock"):
        assert asset_type == "index"
        if on_segment:
            on_segment(_minute_rows(INDEX, 240))
        return pl.DataFrame()

    monkeypatch.setattr(kline_sync, "sync_minute_batch", fake_batch)
    result = kline_sync.ensure_symbol_minute_for_chan(repo, _caps(), INDEX)
    assert result["status"] == "ok"
    assert result["asset_type"] == "index"
    assert any((tmp_path / "kline_index_minute").rglob("*.parquet"))
    assert not (tmp_path / "kline_minute").exists() or not any((tmp_path / "kline_minute").rglob("*.parquet"))


def test_ensure_skips_when_local_minute_usable(tmp_path, monkeypatch):
    store = DataStore(data_dir=tmp_path)
    repo = KlineRepository(store)
    repo.save_index_instruments(pl.DataFrame({
        "symbol": [INDEX], "name": ["上证指数"], "code": ["000001"], "asset_type": ["index"],
    }))
    kline_sync.write_minute_partition(_minute_rows(INDEX, 240), tmp_path / "kline_index_minute")

    def boom(*args, **kwargs):
        raise AssertionError("已有可用 1m 时不应再打数据源")

    monkeypatch.setattr(kline_sync, "sync_minute_batch", boom)
    result = kline_sync.ensure_symbol_minute_for_chan(repo, _caps(), INDEX)
    assert result["skipped"] is True
    assert result["status"] == "skipped"
    assert result["rows_written"] == 0


def test_ensure_forbidden_without_minute_capability(tmp_path, monkeypatch):
    monkeypatch.setattr(kline_sync.preferences, "get_minute_data_provider", lambda: "tickflow")
    store = DataStore(data_dir=tmp_path)
    repo = KlineRepository(store)
    repo.save_index_instruments(pl.DataFrame({
        "symbol": [INDEX], "name": ["上证指数"], "code": ["000001"], "asset_type": ["index"],
    }))
    result = kline_sync.ensure_symbol_minute_for_chan(repo, CapabilitySet(), INDEX)
    assert result["status"] == "forbidden"


def _app(repo, capset) -> FastAPI:
    app = FastAPI()
    app.include_router(chan_router)
    app.state.repo = repo
    app.state.capabilities = capset
    return app


def test_chan_sync_minute_api_403(tmp_path, monkeypatch):
    monkeypatch.setattr(kline_sync.preferences, "get_minute_data_provider", lambda: "tickflow")
    repo = KlineRepository(DataStore(data_dir=tmp_path))
    repo.save_index_instruments(pl.DataFrame({
        "symbol": [INDEX], "name": ["上证指数"], "code": ["000001"], "asset_type": ["index"],
    }))
    client = TestClient(_app(repo, CapabilitySet()))
    r = client.post("/api/chan/sync_minute", params={"symbol": INDEX})
    assert r.status_code == 403
    assert "分钟" in r.json()["detail"]


def test_chan_sync_minute_api_rejects_stock(tmp_path):
    repo = KlineRepository(DataStore(data_dir=tmp_path))
    client = TestClient(_app(repo, _caps()))
    r = client.post("/api/chan/sync_minute", params={"symbol": STOCK})
    assert r.status_code == 400
    assert "指数" in r.json()["detail"]


def test_chan_sync_minute_api_ok(tmp_path, monkeypatch):
    repo = KlineRepository(DataStore(data_dir=tmp_path))

    def fake_ensure(repo_in, capset, symbol):
        return {
            "status": "ok",
            "symbol": symbol,
            "asset_type": "index",
            "rows_written": 240,
            "skipped": False,
            "count_max": kline_sync.TICKFLOW_KLINE_COUNT_MAX,
            "reason": "",
        }

    monkeypatch.setattr(kline_sync, "ensure_symbol_minute_for_chan", fake_ensure)
    client = TestClient(_app(repo, _caps()))
    r = client.post("/api/chan/sync_minute", params={"symbol": INDEX})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["rows_written"] == 240
    assert body["count_max"] == 10000
    assert body["asset_type"] == "index"
