"""因子批量评估 / 进度回调 / 取消 测试 — 合成面板, 全程离线。"""
from __future__ import annotations

import threading
from datetime import date, timedelta

import polars as pl
import pytest

from app.backtest.factor import FactorBacktestService, FactorCancelled, FactorConfig


class _FakeEngine:
    """只提供 load_panel 的引擎桩, 记录加载列。"""

    def __init__(self, panel: pl.DataFrame):
        self._panel = panel
        self.loaded: dict | None = None

    def load_panel(self, symbols, start, end, columns=None, asset_type="stock"):
        self.loaded = {
            "symbols": symbols,
            "start": start,
            "end": end,
            "columns": columns,
            "asset_type": asset_type,
        }
        return self._panel.clone()


def _panel(dates, symbols, closes, factors) -> pl.DataFrame:
    rows = []
    for i, d in enumerate(dates):
        for s in symbols:
            c = closes[s][i]
            rows.append({
                "symbol": s, "date": d,
                "open": c, "high": c, "low": c, "close": c,
                "volume": 1_000_000.0, "amount": c * 1_000_000.0,
                "turnover_rate": 1.0,
                **{f: factors[f][s][i] for f in factors},
            })
    return pl.DataFrame(rows)


def _dates(n: int) -> list[date]:
    return [date(2026, 5, 4) + timedelta(days=k) for k in range(n)]


def _mkcfg(**kw) -> FactorConfig:
    """dataclasses.replace 风格, 避免手工展开。"""
    from dataclasses import replace
    return replace(
        FactorConfig(
            factor_name="f",
            symbols=None,
            start=date(2026, 5, 4),
            end=date(2026, 5, 8),
            n_groups=2,
            rebalance="daily",
        ),
        **kw,
    )


# ---------------------------------------------------------------
# 进度回调
# ---------------------------------------------------------------

def test_progress_events_monotonic_and_complete():
    events: list[dict] = []
    dates = _dates(4)
    closes = {"A": [10, 10.1, 10.2, 10.3]}
    panel = _panel(dates, ["A"], closes, {"f": {"A": [1, 2, 3, 4]}})
    svc = FactorBacktestService(_FakeEngine(panel))
    result = svc.run(_mkcfg(), progress_cb=lambda e: events.append(dict(e)))

    assert result.error is None
    assert events, "无进度事件"
    assert events[0]["pct"] == 2 and events[0]["stage"] == "pending"
    assert events[-1]["pct"] == 100 and events[-1]["stage"] == "done"
    pcts = [e["pct"] for e in events]
    assert pcts == sorted(pcts), "进度必须单调不减"
    assert all("stage" in e and "message" in e for e in events)


def test_cancel_event_aborts_with_factor_cancelled():
    dates = _dates(4)
    closes = {"A": [10, 10.1, 10.2, 10.3]}
    panel = _panel(dates, ["A"], closes, {"f": {"A": [1, 2, 3, 4]}})
    svc = FactorBacktestService(_FakeEngine(panel))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(FactorCancelled):
        svc.run(_mkcfg(), cancel_event=cancel)


# ---------------------------------------------------------------
# 批量评估
# ---------------------------------------------------------------

def test_batch_two_factors_and_ic_corr():
    """逐日 IC 序列带方差时, 相关矩阵 diag=1、对称且 f 与 -f 完全负相关。"""
    dates = _dates(3)
    # day1→2 收益随因子同序 (IC=+1); day2→3 逆序 (IC=-1) → f 的 IC 序列 [1,-1]
    closes = {
        "A": [10, 10.4, 10.504],   # +4% 然后 +1%
        "B": [10, 10.2, 10.404],   # +2% 然后 +2%
        "C": [10, 10.1, 10.5041],  # +1% 然后 +4.04%
    }
    factors = {
        "f": {"A": [3, 3, 3], "B": [2, 2, 2], "C": [1, 1, 1]},
        "g": {"A": [-3, -3, -3], "B": [-2, -2, -2], "C": [-1, -1, -1]},
    }
    panel = _panel(dates, ["A", "B", "C"], closes, factors)
    engine = _FakeEngine(panel)
    svc = FactorBacktestService(engine)

    out = svc.run_batch(["f", "g"], _mkcfg(factor_name="f"))
    assert out["error"] is None
    assert len(out["factors"]) == 2
    by_name = {r["name"]: r for r in out["factors"]}
    assert by_name["f"]["ic_obs"] == 2
    assert by_name["g"]["ic_obs"] == 2
    # f 的日 IC = [1, -1]; g = -f → IC 序列 [-1, 1], 两者 Pearson = -1
    assert by_name["f"]["ic_mean"] == pytest.approx(0.0)
    assert by_name["g"]["ic_mean"] == pytest.approx(0.0)

    names = out["ic_corr"]["names"]
    mat = out["ic_corr"]["matrix"]
    assert set(names) == {"f", "g"}
    assert len(mat) == 2 and all(len(row) == 2 for row in mat)
    diag = [mat[i][i] for i in range(len(names))]
    assert diag == pytest.approx([1.0, 1.0], abs=1e-6)
    assert mat[0][1] == pytest.approx(mat[1][0], abs=1e-6)
    assert mat[0][1] == pytest.approx(-1.0, abs=1e-6)

    # 面板只加载一次, 列包含全部因子
    assert engine.loaded is not None
    assert "f" in engine.loaded["columns"] and "g" in engine.loaded["columns"]


def test_batch_reports_uncomputable_as_skipped():
    dates = _dates(3)
    closes = {"A": [10, 10.2, 10.4]}
    factors = {"f": {"A": [1, 2, 3]}}
    panel = _panel(dates, ["A"], closes, factors)
    svc = FactorBacktestService(_FakeEngine(panel))

    out = svc.run_batch(["f", "not_a_factor"], _mkcfg(factor_name="f"))
    assert out["error"] is None
    assert [r["name"] for r in out["factors"]] == ["f"]
    assert "not_a_factor" in out["skipped"]


def test_batch_caps_factor_count():
    dates = _dates(2)
    closes = {"A": [10, 10.2]}
    factors = {f"x{i}": {"A": [1, 2]} for i in range(70)}
    panel = _panel(dates, ["A"], closes, factors)
    svc = FactorBacktestService(_FakeEngine(panel))

    out = svc.run_batch([f"x{i}" for i in range(70)], _mkcfg(factor_name="x0"))
    # 截断到 60, 全部有数据 → 60 个记录
    assert out["error"] is None
    assert len(out["factors"]) <= 60
    assert len(out["factors"]) == 60