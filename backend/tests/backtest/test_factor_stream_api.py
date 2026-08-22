"""因子结果持久化 + SSE 流端点契约测试。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.backtest import _factor_job_key, factor_cancel
from app.api.backtest import router as backtest_router
from app.backtest import factor_results
from app.backtest.factor import FactorResult

# ---------------------------------------------------------------
# 持久化模块
# ---------------------------------------------------------------

def test_save_list_load_roundtrip(tmp_path: Path):
    payload = {
        "run_id": "abc123",
        "config": {"factor_name": "momentum_20d", "rebalance": "monthly"},
        "ic_mean": 0.05,
        "ir": 0.8,
        "long_short_stats": {"total_return": 0.3, "annual_return": 0.2, "sharpe": 1.2},
        "n_symbols": 100,
    }
    meta = factor_results.save_result(tmp_path, "abc123", {"kind": "single", "data": payload})
    assert meta["run_id"] == "abc123"
    assert meta["path"].endswith("backtest_results/factor/abc123.json")
    assert not (tmp_path / "backtest_results" / "factor" / "abc123.tmp").exists()

    items = factor_results.list_results(tmp_path)
    assert len(items) == 1
    summary = items[0]
    assert summary["run_id"] == "abc123"
    assert summary["kind"] == "single"
    assert summary["factor_name"] == "momentum_20d"
    assert summary["ic_mean"] == 0.05
    assert summary["ls_total_return"] == 0.3

    full = factor_results.load_result(tmp_path, "abc123")
    assert full is not None
    assert full["data"]["ic_mean"] == 0.05


def test_batch_summary_and_missing_load(tmp_path: Path):
    factor_results.save_result(tmp_path, "b1", {
        "kind": "batch",
        "data": {"factors": [{"name": "f1"}], "skipped": [], "config": {}},
    })
    items = factor_results.list_results(tmp_path)
    assert items[0]["kind"] == "batch"
    assert items[0]["factor_count"] == 1
    assert factor_results.load_result(tmp_path, "nope") is None


def test_trim_to_max_keep_and_tolerate_corrupt(tmp_path: Path):
    for i in range(55):
        factor_results.save_result(tmp_path, f"r{i:03d}", {"kind": "single", "data": {"ic_mean": 0.0}})
    # 触发新一轮裁剪
    factor_results.save_result(tmp_path, "r999", {"kind": "single", "data": {"ic_mean": 0.0}})
    assert len(factor_results.list_results(tmp_path, limit=100)) <= factor_results.MAX_KEEP

    (tmp_path / "backtest_results" / "factor" / "bad.json").write_text("{broken", encoding="utf-8")
    assert all(i["run_id"] for i in factor_results.list_results(tmp_path, limit=100))


def test_nan_becomes_null_in_stored_json(tmp_path: Path):
    factor_results.save_result(tmp_path, "nan1", {
        "kind": "single",
        "data": {"ic_mean": float("nan"), "ls": [float("inf")]},
    })
    raw = (tmp_path / "backtest_results" / "factor" / "nan1.json").read_text(encoding="utf-8")
    assert "NaN" not in raw and "Infinity" not in raw
    loaded = factor_results.load_result(tmp_path, "nan1")
    assert loaded["data"]["ic_mean"] is None
    assert loaded["data"]["ls"] == [None]


# ---------------------------------------------------------------
# job key / cancel 契约
# ---------------------------------------------------------------

def test_factor_job_key_deterministic_and_kind_sensitive():
    k1 = _factor_job_key("single", "f", "", None, None, None, 5, "monthly", "equal", 0.0002, 5, "stock")
    k2 = _factor_job_key("single", "f", "", None, None, None, 5, "monthly", "equal", 0.0002, 5, "stock")
    assert k1 == k2
    assert k1 != _factor_job_key("batch", "f", "f,g", None, None, None, 5, "monthly", "equal", 0.0002, 5, "stock")


def test_factor_cancel_by_echoed_key():
    import asyncio

    from app.api.backtest import _BacktestJob, _running_jobs

    class _Req:
        def __init__(self, body):
            self._body = body

        async def json(self):
            return self._body

    key = "factor-test-key"
    job = _BacktestJob(key)
    _running_jobs[key] = job
    try:
        assert asyncio.run(factor_cancel(_Req({"job_key": key})))["ok"] is True
        assert job.cancel_event.is_set()
        # 已完成的 job 再取消 → False (不抛出)
        job.done = True
        assert asyncio.run(factor_cancel(_Req({"job_key": key})))["ok"] is False
    finally:
        _running_jobs.pop(key, None)


# ---------------------------------------------------------------
# SSE 端点 (monkeypatch 服务, 断言帧协议与持久化)
# ---------------------------------------------------------------

def _fake_factor_result() -> FactorResult:
    return FactorResult(
        run_id="stream1",
        config={"factor_name": "f", "rebalance": "monthly"},
        ic_mean=0.05,
        ic_std=0.02,
        ir=2.5,
        ic_win_rate=0.6,
        ic_series=[{"date": "2026-05-04", "ic": 0.05}],
        group_stats=[],
        group_nav=[],
        long_short_stats={"total_return": 0.3},
        long_short_nav=[],
        elapsed_ms=1.0,
        n_symbols=10,
        n_dates=2,
    )


def test_single_stream_business_error_goes_error_event_and_no_persist(tmp_path, monkeypatch):
    """业务级失败 (result.error) → error 事件, 不落盘、无 done。"""
    app = _make_app(monkeypatch, tmp_path)  # 先建 app (内部 patch 成功 fake)

    def fake_run_error(self, config, progress_cb=None, cancel_event=None):
        progress_cb({"pct": 5, "stage": "pending", "message": "排队"})
        r = _fake_factor_result()
        r.run_id = "err1"
        r.error = "无数据，请检查日期范围或先运行盘后管道"
        return r

    # 再覆盖为 error fake (顺序必须在 _make_app 之后, 否则被其 patch 回成功 fake)
    monkeypatch.setattr("app.backtest.factor.FactorBacktestService.run", fake_run_error)
    client = TestClient(app)
    events = _collect_sse(
        client,
        # factor_name 与 success 用例不同, 避免同参任务在 300s TTL 内被复用
        "/api/backtest/factor/stream?factor_name=f2&start=2026-05-01&end=2026-05-31",
    )
    kinds = [e for e, _ in events]
    assert kinds[-1] == "error"
    assert "无数据" in events[-1][1]["message"]
    assert factor_results.load_result(tmp_path, "err1") is None


def _make_app(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from app.api import backtest as bt

    saved_runs: list[int] = []

    def fake_run(self, config, progress_cb=None, cancel_event=None):
        progress_cb({"pct": 10, "stage": "ic", "message": "fake ic"})
        progress_cb({"pct": 100, "stage": "done", "message": "fake done"})
        result = _fake_factor_result()
        result.run_id = "stream1"
        saved_runs.append(result.n_symbols)
        return result

    monkeypatch.setattr("app.backtest.factor.FactorBacktestService.run", fake_run)

    def fake_engine(request):
        return None

    monkeypatch.setattr(bt, "_get_engine", fake_engine)

    app = FastAPI()
    app.include_router(backtest_router)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.repo = repo
    # 路由内部会经 _get_engine 获取 (已被 monkeypatch) — 但构造 svc 仍走 FactorBacktestService(None)
    return app


def _collect_sse(client, path):
    events: list[tuple[str, dict]] = []
    with client.stream("GET", path) as resp:
        assert resp.status_code == 200
        current_event = None
        for line in resp.iter_lines():
            if line.startswith("event: "):
                current_event = line[len("event: "):]
            elif line.startswith("data: "):
                import json
                events.append((current_event, json.loads(line[len("data: "):])))
                if current_event in {"done", "error"}:
                    break
    return events


def test_single_stream_emits_start_progress_done_and_persists(tmp_path, monkeypatch):
    app = _make_app(monkeypatch, tmp_path)
    client = TestClient(app)
    events = _collect_sse(
        client,
        "/api/backtest/factor/stream"
        "?factor_name=f&start=2026-05-01&end=2026-05-31&rebalance=monthly",
    )
    kinds = [e for e, _ in events]
    assert kinds[0] == "start"
    assert "progress" in kinds
    assert kinds[-1] == "done"
    start_payload = events[0][1]
    assert start_payload["job_key"]
    progress_payloads = [p for e, p in events if e == "progress"]
    assert progress_payloads[0]["idx"] == 0
    assert progress_payloads[1]["idx"] == 1
    done_payload = events[-1][1]
    assert done_payload["result"]["ic_mean"] == 0.05
    # 已落盘
    assert factor_results.load_result(tmp_path, "stream1") is not None


def test_batch_stream_emits_done_with_batch(tmp_path, monkeypatch):

    monkeypatch.setattr("app.backtest.factor.FactorBacktestService.run_batch", (
        lambda self, names, config, progress_cb=None, cancel_event=None: {
            "run_id": "batch1",
            "config": {"factor_name": names[0]},
            "factors": [{"name": "f", "ic_mean": 0.1}],
            "skipped": [],
            "ic_corr": {"names": ["f"], "matrix": [[1.0]]},
            "n_symbols": 5,
            "n_dates": 3,
            "elapsed_ms": 1.0,
            "error": None,
        }
    ))
    app = _make_app(monkeypatch, tmp_path)
    client = TestClient(app)
    events = _collect_sse(
        client,
        "/api/backtest/factor/batch/stream"
        "?factor_names=f,g&start=2026-05-01&end=2026-05-31",
    )
    done = [p for e, p in events if e == "done"]
    assert done and "batch" in done[0]
    assert done[0]["batch"]["factors"][0]["ic_mean"] == 0.1
    assert factor_results.load_result(tmp_path, "batch1") is not None


def test_history_endpoints(tmp_path, monkeypatch):
    factor_results.save_result(tmp_path, "h1", {
        "kind": "single",
        "data": {"config": {"factor_name": "rsi_14"}, "ic_mean": 0.04},
    })
    factor_results.save_result(tmp_path, "h2", {
        "kind": "batch",
        "data": {"factors": [{"name": "f"}], "skipped": [], "config": {}},
    })
    from types import SimpleNamespace

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(backtest_router)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.repo = repo
    client = TestClient(app)

    r = client.get("/api/backtest/factor/history")
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["run_id"] for i in items] == ["h2", "h1"]  # 新到旧

    r2 = client.get("/api/backtest/factor/history/h1")
    assert r2.status_code == 200
    assert r2.json()["data"]["ic_mean"] == 0.04

    assert client.get("/api/backtest/factor/history/missing").status_code == 404