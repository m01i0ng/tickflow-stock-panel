"""因子回测服务测试 — IC / 分层 / 成本 / 加权 / 调仓边界 / 未来函数反例。

被测: FactorBacktestService (app/backtest/factor.py)。
引擎以桩替换 (只提供 load_panel), 面板为手造合成数据, 所有断言均可手算。
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import polars as pl
import pytest

from app.backtest.factor import FactorBacktestService, FactorConfig

# ---------------------------------------------------------------
# 工具
# ---------------------------------------------------------------

class _FakeEngine:
    """只提供 load_panel 的引擎桩, 记录加载参数便于断言 warmup。"""

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


def make_panel(
    dates: list[date],
    symbols: list[str],
    closes: dict[str, list[float]],
    factors: dict[str, list[float]],
) -> pl.DataFrame:
    """合成面板: OHLC=close、volume=1e6、amount=close*1e6、turnover_rate=1.0。"""
    rows = []
    for i, d in enumerate(dates):
        for s in symbols:
            c = closes[s][i]
            rows.append({
                "symbol": s,
                "date": d,
                "open": c,
                "high": c,
                "low": c,
                "close": c,
                "volume": 1_000_000.0,
                "amount": c * 1_000_000.0,
                "turnover_rate": 1.0,
                "factor": factors[s][i],
            })
    return pl.DataFrame(rows)


def base_config(**kw) -> FactorConfig:
    cfg = FactorConfig(
        factor_name="factor",
        symbols=None,
        start=date(2026, 1, 1),
        end=date(2026, 3, 31),
        n_groups=5,
        rebalance="monthly",
    )
    return replace(cfg, **kw)


def run_service(svc: FactorBacktestService, cfg: FactorConfig):
    return svc.run(cfg)


# 2026-04-06 为周一 (genuine校验见测试内注释)
WK1 = [date(2026, 4, 6) + timedelta(days=k) for k in range(5)]
# 2026-04-13 周一休市 (假期周), 首交易日为周二 04-14
WK2 = [date(2026, 4, 14) + timedelta(days=k) for k in range(4)]
# 2026-04-20 周一, 正常周
WK3 = [date(2026, 4, 20) + timedelta(days=k) for k in range(5)]


# ---------------------------------------------------------------
# IC 分析
# ---------------------------------------------------------------

def test_rank_ic_equals_one_when_factor_predicts_next_return_perfectly():
    """因子序与下期收益序完全一致时, 每日 Rank IC = 1。"""
    dates = [date(2026, 5, 1) + timedelta(days=k) for k in range(3)]
    closes = {
        "A": [10.0, 10.1, 10.201],
        "B": [20.0, 20.4, 20.808],
        "C": [30.0, 30.9, 31.827],
    }
    # 各日因子值单调, 与次一日收益完全同序
    factors = {"A": [1.0, 1.0, 1.0], "B": [2.0, 2.0, 2.0], "C": [3.0, 3.0, 3.0]}
    panel = make_panel(dates, ["A", "B", "C"], closes, factors)
    cfg = base_config(start=dates[0], end=dates[-1], rebalance="daily")
    result = run_service(FactorBacktestService(_FakeEngine(panel)), cfg)

    assert result.error is None
    assert result.ic_mean == pytest.approx(1.0)
    assert result.ic_win_rate == pytest.approx(1.0)
    # 最后一个交易日没有下期收益, 不进入 IC
    assert len(result.ic_series) == 2


def test_rank_ic_negative_when_factor_inverts_next_return():
    """因子序与下期收益序完全相反时, Rank IC = -1。"""
    dates = [date(2026, 5, 1) + timedelta(days=k) for k in range(2)]
    # 因子大 → 收益小: A 收益最低, C 最高
    closes = {"A": [10.0, 10.033], "B": [20.0, 20.1], "C": [30.0, 30.3]}
    factors = {"A": [3.0, 3.0], "B": [2.0, 2.0], "C": [1.0, 1.0]}
    panel = make_panel(dates, ["A", "B", "C"], closes, factors)
    cfg = base_config(start=dates[0], end=dates[-1], rebalance="daily")
    result = run_service(FactorBacktestService(_FakeEngine(panel)), cfg)

    assert result.ic_mean == pytest.approx(-1.0)


# ---------------------------------------------------------------
# 调仓边界 (未来函数反例)
# ---------------------------------------------------------------

def test_next_return_only_marked_on_rebalance_dates_monthly():
    """月频下只有每月首个交易日的行才有下期收益 (其余为 null)。"""
    dates = [
        date(2026, 5, 4), date(2026, 5, 5),   # 5 月首交易日 = 5/4 (5/1 假期跳过亦可, 此处显式)
        date(2026, 6, 1), date(2026, 6, 2),   # 6 月首交易日 = 6/1
    ]
    closes = {"A": [10, 10.2, 11, 11.2, 12]}
    factors = {"A": [1, 1, 1, 1, 1]}
    panel = make_panel(dates, ["A"], closes, factors)
    svc = FactorBacktestService(_FakeEngine(panel))
    out = svc._calc_period_return(panel, "monthly")

    assert out.filter(pl.col("date") == date(2026, 5, 4))["_next_return"].to_list() == [pytest.approx(0.1)]
    assert out.filter(pl.col("date") == date(2026, 5, 5))["_next_return"].to_list() == [None]
    assert out.filter(pl.col("date") == date(2026, 6, 1))["_next_return"].to_list() == [None]


def test_last_rebalance_date_excluded_from_nav_and_ic():
    """最后一个调仓日没有下期收益, 不进入 IC / 净值。"""
    dates = [
        date(2026, 5, 4), date(2026, 6, 1), date(2026, 7, 1),
    ]
    closes = {"A": [10, 11, 12], "B": [10, 12, 14]}
    factors = {"A": [1, 2, 3], "B": [2, 1, 2]}
    panel = make_panel(dates, ["A", "B"], closes, factors)
    cfg = base_config(start=dates[0], end=dates[-1], rebalance="monthly", n_groups=2)
    result = run_service(FactorBacktestService(_FakeEngine(panel)), cfg)

    assert len(result.ic_series) == 2
    assert len(result.group_nav) == 2
    assert [r["date"] for r in result.group_nav] == ["2026-05-04", "2026-06-01"]


def test_weekly_rebalance_handles_holiday_week_starting_tuesday():
    """假期周首交易日为周二时, 该周必须照常调仓 (不按 weekday==0 整周跳过)。

    回归测试: 旧实现按 Monday 判定, 会漏掉 04-14 这一周。
    """
    dates = WK1 + WK2 + WK3
    symbols = ["A", "B"]
    closes = {"A": [10 + i * 0.1 for i in range(len(dates))],
              "B": [10 + i * 0.2 for i in range(len(dates))]}
    factors = {"A": [1.0] * len(dates), "B": [2.0] * len(dates)}
    panel = make_panel(dates, symbols, closes, factors)
    cfg = base_config(start=dates[0], end=dates[-1], rebalance="weekly", n_groups=2)
    result = run_service(FactorBacktestService(_FakeEngine(panel)), cfg)

    assert result.error is None
    # 三个 ISO 周 → 前两周有下期收益, 各成为净值点
    assert [r["date"] for r in result.group_nav] == ["2026-04-06", "2026-04-14"]


# ---------------------------------------------------------------
# 分组
# ---------------------------------------------------------------

def test_groups_are_equal_sized_and_monotonic():
    """序号分桶: 分组等量且按因子单调排序。"""
    dates = [date(2026, 5, 4), date(2026, 6, 1)]
    symbols = [f"S{i}" for i in range(10)]
    closes = {s: [10.0, 11.0] for s in symbols}
    factors = {f"S{i}": [float(i)] * 2 for i in range(10)}
    panel = make_panel(dates, symbols, closes, factors)
    svc = FactorBacktestService(_FakeEngine(panel))
    grouped = svc._add_groups(panel, "factor", 5)

    sizes = (
        grouped.filter(pl.col("date") == dates[0])
        .group_by("_group").agg(pl.len().alias("n"))
        .sort("_group")
    )
    assert sizes["n"].to_list() == [2, 2, 2, 2, 2]
    # S0 (factor=0) 在 Q1, S9 (factor=9) 在 Q5
    day0 = grouped.filter(pl.col("date") == dates[0])
    assert day0.filter(pl.col("symbol") == "S0")["_group"].to_list() == ["Q1"]
    assert day0.filter(pl.col("symbol") == "S9")["_group"].to_list() == ["Q5"]


# ---------------------------------------------------------------
# 成本 (换手率模型)
# ---------------------------------------------------------------

def test_group_turnover_full_and_zero():
    """换手率: 等量同组保留 → 0; 部分替换 → 1/3; 全替换 → 1; 首期标记 first。"""
    d1, d2, d3 = date(2026, 5, 4), date(2026, 6, 1), date(2026, 7, 1)
    reb = pl.DataFrame({
        "symbol": ["A", "B", "C", "A", "B", "D", "E", "F", "G"],
        "date": [d1, d1, d1, d2, d2, d2, d3, d3, d3],
        "_group": ["Q1", "Q1", "Q1", "Q1", "Q1", "Q1", "Q1", "Q1", "Q1"],
    })
    out = FactorBacktestService._calc_group_turnover(reb)

    t1 = out.filter(pl.col("date") == d1)
    assert t1["_first"].to_list() == [True]
    # 首期无旧持仓 → 记 1.0 以便建仓单边成本
    assert t1["_turnover"].to_list() == [pytest.approx(1.0)]

    t2 = out.filter(pl.col("date") == d2)
    assert t2["_turnover"].to_list() == [pytest.approx(1 / 3)]  # A,B 保留, C 换出

    t3 = out.filter(pl.col("date") == d3)
    assert t3["_turnover"].to_list() == [pytest.approx(1.0)]  # 全部换出


def test_cost_reduces_nav_by_known_formula():
    """费用按 '首期单边、后续双边×换手' 扣除, 数值可手算。

    全票收益率相同 r=0.10 → 各组毛收益均为 r; 因子序两月完全反转 → turnover=1。
    month1: net = r - leg (单边) ; month2: net = r - 2*leg (双边, leg=0.01)。
    NAV = (1+r-leg)(1+r-2leg)。
    """
    dates = [date(2026, 5, 4), date(2026, 6, 1), date(2026, 7, 1)]
    symbols = ["A", "B"]
    closes = {"A": [10.0, 11.0, 12.1], "B": [10.0, 11.0, 12.1]}  # 各月 +10%
    factors = {"A": [1.0, 2.0, 1.0], "B": [2.0, 1.0, 2.0]}       # 序号反转 → turnover=1
    panel = make_panel(dates, symbols, closes, factors)
    cfg = base_config(
        start=dates[0], end=dates[-1], rebalance="monthly",
        n_groups=2, fees_pct=0.01, slippage_bps=0.0,
    )
    result = run_service(FactorBacktestService(_FakeEngine(panel)), cfg)

    assert result.error is None
    # 两期净值: 首期单边成本 net=0.10-0.01; 次期双边成本 net=0.10-0.02
    nav1 = 1 + 0.10 - 0.01
    nav2 = nav1 * (1 + 0.10 - 0.02)
    assert len(result.group_nav) == 2
    assert result.group_nav[0]["Q1"] == pytest.approx(nav1, abs=1e-4)
    assert result.group_nav[0]["Q2"] == pytest.approx(nav1, abs=1e-4)
    assert result.group_nav[1]["Q1"] == pytest.approx(nav2, abs=1e-4)
    assert result.group_nav[1]["Q2"] == pytest.approx(nav2, abs=1e-4)


def test_zero_fees_no_slippage_keep_gross_nav():
    """"零成本时净值 = 毛收益累乘。"""
    dates = [date(2026, 5, 4), date(2026, 6, 1)]
    symbols = ["A", "B"]
    closes = {"A": [10.0, 11.0], "B": [10.0, 11.0]}
    factors = {"A": [1.0, 1.0], "B": [2.0, 2.0]}
    panel = make_panel(dates, symbols, closes, factors)
    cfg = base_config(
        start=dates[0], end=dates[-1], rebalance="monthly",
        n_groups=2, fees_pct=0.0, slippage_bps=0.0,
    )
    result = run_service(FactorBacktestService(_FakeEngine(panel)), cfg)
    assert result.group_nav[-1]["Q1"] == pytest.approx(1.10)


# ---------------------------------------------------------------
# 加权
# ---------------------------------------------------------------

def test_factor_weight_differs_from_equal_and_flat_falls_back():
    """绝对暴露度加权: 偏离组均值的样本权重更高; 组内等值时回落等权。"""
    d1, d2 = date(2026, 5, 4), date(2026, 6, 1)
    # 组内 3 只: 因子 0/5/10 → 中心化偏离 5/0/5 → 权重 0.5/0/0.5
    symbols = ["A", "B", "C"]
    closes = {"A": [10.0, 12.0], "B": [10.0, 11.0], "C": [10.0, 12.0]}
    factors = {"A": [0.0, 0.0], "B": [5.0, 5.0], "C": [10.0, 10.0]}
    panel = make_panel([d1, d2], symbols, closes, factors)

    cfg_equal = base_config(
        start=d1, end=d2, rebalance="monthly", n_groups=1,
        fees_pct=0.0, slippage_bps=0.0, weight="equal",
    )
    cfg_weighted = replace(cfg_equal, weight="factor_weight")

    svc = FactorBacktestService(_FakeEngine(panel))
    nav_equal = run_service(svc, cfg_equal).group_nav[-1]["Q1"]
    nav_weighted = run_service(svc, cfg_weighted).group_nav[-1]["Q1"]

    # 毛收益: equal=(0.2+0.1+0.2)/3=0.5/3; weighted=0.2*0.5+0.1*0+0.2*0.5=0.2
    # group_nav 舍入到 4 位小数 → 容差放宽
    assert nav_equal == pytest.approx(1 + 0.5 / 3, abs=1e-3)
    assert nav_weighted == pytest.approx(1.2, abs=1e-3)
    assert nav_weighted != pytest.approx(nav_equal)

    # 组内等值 → 回落等权, 二者一致
    flat_factors = {"A": [5.0, 5.0], "B": [5.0, 5.0], "C": [5.0, 5.0]}
    flat_panel = make_panel([d1, d2], symbols, closes, flat_factors)
    flat_svc = FactorBacktestService(_FakeEngine(flat_panel))
    flat_equal = run_service(flat_svc, cfg_equal).group_nav[-1]["Q1"]
    flat_weighted = run_service(flat_svc, cfg_weighted).group_nav[-1]["Q1"]
    assert flat_weighted == pytest.approx(flat_equal, abs=1e-9)


# ---------------------------------------------------------------
# 缺列因子运行时计算
# ---------------------------------------------------------------

def test_missing_factor_computed_from_ohlc():
    """面板缺 momentum_5d 时, 走 compute_indicators(needed) 补算, IC 可算。"""
    dates = [date(2026, 5, 1) + timedelta(days=k) for k in range(8)]
    closes = {"A": [10 + 0.1 * i for i in range(8)], "B": [10 + 0.2 * i for i in range(8)]}
    factors = {"A": [0.0] * 8, "B": [0.0] * 8}
    panel = make_panel(dates, ["A", "B"], closes, factors).drop("factor")
    cfg = base_config(
        factor_name="momentum_5d", start=dates[5], end=dates[-1], rebalance="daily",
    )
    result = run_service(FactorBacktestService(_FakeEngine(panel)), cfg)

    assert result.error is None
    assert result.ic_mean is not None
    assert -1.0 <= result.ic_mean <= 1.0
    # momentum 需要 5 日窗口 → 加载起点提前 warmup 之外
    assert result.n_dates >= 2


# ---------------------------------------------------------------
# 多空统计
# ---------------------------------------------------------------

def test_long_short_stats_populated():
    """多空组合补齐年化/夏普/胜率。"""
    dates = [date(2026, 5, 4), date(2026, 6, 1), date(2026, 7, 1)]
    symbols = ["A", "B"]
    closes = {"A": [10.0, 11.0, 12.1], "B": [10.0, 11.0, 12.1]}
    factors = {"A": [1.0, 2.0, 1.0], "B": [2.0, 1.0, 2.0]}
    panel = make_panel(dates, symbols, closes, factors)
    cfg = base_config(
        start=dates[0], end=dates[-1], rebalance="monthly",
        n_groups=2, fees_pct=0.0, slippage_bps=0.0,
    )
    result = run_service(FactorBacktestService(_FakeEngine(panel)), cfg)

    stats = result.long_short_stats
    assert stats != {}
    for key in ("annual_return", "sharpe", "win_rate", "max_drawdown"):
        assert key in stats and stats[key] is not None