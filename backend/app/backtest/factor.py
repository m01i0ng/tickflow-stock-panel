"""因子回测服务 — IC/IR 分析 + 分层回测 + 多空组合。

纯 Polars 向量化实现，无 pandas 依赖。
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

import numpy as np
import polars as pl

from app.backtest.engine import BacktestEngine

logger = logging.getLogger(__name__)

# 可用因子列 (从 ENRICHED_COLUMNS 过滤出数值型指标)
FACTOR_COLUMNS: list[dict] = [
    {"id": "momentum_5d",  "label": "5日动量",     "group": "动量", "desc": "5日涨跌幅，正值表示上涨趋势"},
    {"id": "momentum_10d", "label": "10日动量",    "group": "动量", "desc": "10日涨跌幅，中短期趋势指标"},
    {"id": "momentum_20d", "label": "20日动量",    "group": "动量", "desc": "月度涨跌幅，常用因子"},
    {"id": "momentum_30d", "label": "30日动量",    "group": "动量", "desc": "30日涨跌幅"},
    {"id": "momentum_60d", "label": "60日动量",    "group": "动量", "desc": "季度涨跌幅，中期动量"},
    {"id": "rsi_6",        "label": "RSI(6)",      "group": "超买超卖", "desc": "6日相对强弱指标，敏感度高"},
    {"id": "rsi_14",       "label": "RSI(14)",     "group": "超买超卖", "desc": "14日相对强弱指标，经典周期"},
    {"id": "rsi_24",       "label": "RSI(24)",     "group": "超买超卖", "desc": "24日相对强弱指标"},
    {"id": "annual_vol_20d","label": "20日波动率", "group": "波动率",   "desc": "20日年化波动率"},
    {"id": "atr_14",       "label": "ATR(14)",     "group": "波动率",   "desc": "14日平均真实波幅"},
    {"id": "vol_ratio_5d", "label": "量比(5日)",   "group": "量价",     "desc": "当日成交量 / 5日均量"},
    {"id": "turnover_rate", "label": "换手率",     "group": "量价",     "desc": "当日换手率"},
    {"id": "macd_hist",    "label": "MACD柱",      "group": "趋势",     "desc": "MACD柱状图值"},
    {"id": "kdj_k",        "label": "KDJ-K",       "group": "趋势",     "desc": "KDJ指标K值"},
    {"id": "change_pct",   "label": "日涨跌幅",    "group": "基础",     "desc": "当日涨跌幅"},
    {"id": "amplitude",    "label": "日振幅",      "group": "基础",     "desc": "当日振幅 (最高-最低)/昨收"},
]

FACTOR_WARMUP_DAYS = 120

# 批量评估单次最多因子数 (防止宽表/计算时间失控)
FACTOR_BATCH_MAX = 60


class FactorCancelled(Exception):
    """任务被用户取消 (job.cancel_event 置位), 与计算错误区分。"""


@dataclass
class FactorConfig:
    factor_name: str
    symbols: list[str] | None
    start: date
    end: date
    n_groups: int = 5
    rebalance: Literal["daily", "weekly", "monthly"] = "monthly"
    weight: Literal["equal", "factor_weight"] = "equal"
    fees_pct: float = 0.0002
    slippage_bps: float = 5.0
    asset_type: str = "stock"


@dataclass
class GroupStats:
    group: int
    label: str
    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe: float
    win_rate: float


@dataclass
class FactorResult:
    run_id: str
    config: dict
    # IC 分析
    ic_mean: float | None = None
    ic_std: float | None = None
    ir: float | None = None
    ic_win_rate: float | None = None
    ic_series: list[dict] = field(default_factory=list)
    # 分层
    group_stats: list[dict] = field(default_factory=list)
    group_nav: list[dict] = field(default_factory=list)
    # 多空
    long_short_stats: dict = field(default_factory=dict)
    long_short_nav: list[dict] = field(default_factory=list)
    # 元信息
    elapsed_ms: float = 0.0
    n_symbols: int = 0
    n_dates: int = 0
    error: str | None = None


class FactorBacktestService:
    def __init__(self, engine: BacktestEngine) -> None:
        self.engine = engine

    def run(
        self,
        config: FactorConfig,
        progress_cb=None,
        cancel_event=None,
    ) -> FactorResult:
        """单因子回测。

        progress_cb(event): 可选进度回调, event = {"pct", "stage", "message"}。
        cancel_event(threading.Event): 置位后在阶段边界抛出 FactorCancelled。
        """
        t0 = time.perf_counter()
        run_id = uuid.uuid4().hex[:10]

        def _emit(pct: int, stage: str, message: str) -> None:
            if progress_cb is not None:
                progress_cb({"pct": pct, "stage": stage, "message": message})

        def _check() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise FactorCancelled()

        def _err(msg: str) -> FactorResult:
            return FactorResult(
                run_id=run_id,
                config=self._config_to_dict(config),
                error=msg,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        # 加载基础面板: 当前 enriched parquet 只持久化基础列, 指标因子可能需要运行时计算。
        panel_columns = ["symbol", "date", "open", "high", "low", "close", "volume", "turnover_rate"]
        if config.factor_name not in panel_columns:
            panel_columns.append(config.factor_name)
        load_start = config.start
        if config.factor_name not in {"turnover_rate"}:
            load_start = config.start - timedelta(days=FACTOR_WARMUP_DAYS)

        _emit(2, "pending", "任务排队")
        _check()
        panel = self.engine.load_panel(
            config.symbols,
            load_start,
            config.end,
            columns=panel_columns,
            asset_type=config.asset_type,
        )
        if panel.is_empty():
            return _err("无数据，请检查日期范围或先运行盘后管道")

        factor_col = config.factor_name
        if factor_col not in panel.columns:
            panel = self._compute_missing_factor(panel, factor_col)
        _check()
        _emit(30, "factor", f"因子值就绪: {factor_col}")
        if factor_col not in panel.columns:
            return _err(f"因子列 '{factor_col}' 不存在于 enriched 数据中, 且无法从基础行情计算")
        if "close" not in panel.columns:
            return _err("enriched 数据缺少收盘价 close")
        panel = panel.select(["symbol", "date", "close", factor_col])
        panel = panel.filter((pl.col("date") >= config.start) & (pl.col("date") <= config.end))

        # 过滤有效行
        panel = panel.filter(
            pl.col(factor_col).is_not_null()
            & pl.col("close").is_not_null()
            & (pl.col("close") > 0)
        )
        if panel.is_empty():
            return _err("过滤后无有效数据")

        panel = panel.sort(["symbol", "date"])

        n_symbols = panel["symbol"].n_unique()
        n_dates = panel["date"].n_unique()

        # 计算下期收益
        # 根据调仓频率计算不同周期的 forward return
        _emit(42, "returns", "计算下期收益")
        if config.rebalance == "daily":
            panel = panel.with_columns(
                (pl.col("close").shift(-1).over("symbol") / pl.col("close") - 1)
                .alias("_next_return")
            )
        else:
            # weekly/monthly: 计算到下个调仓日的收益
            panel = self._calc_period_return(panel, config.rebalance)

        # ── 1. IC 分析 ──
        ic_df = self._calc_ic(panel, factor_col)
        ic_series = [
            {"date": str(row["date"]), "ic": round(float(row["ic"]), 4)}
            for row in ic_df.iter_rows(named=True)
            if row["ic"] is not None and not np.isnan(float(row["ic"]))
        ]
        ic_values = [r["ic"] for r in ic_series]
        ic_mean = float(np.mean(ic_values)) if ic_values else None
        ic_std = float(np.std(ic_values)) if ic_values else None
        ir = (ic_mean / ic_std) if (ic_mean is not None and ic_std and ic_std > 1e-8) else None
        ic_win_rate = (sum(1 for v in ic_values if v > 0) / len(ic_values)) if ic_values else None
        _check()
        _emit(60, "ic", f"Rank IC 均值 {ic_mean:.4f}" if ic_mean is not None else "Rank IC 完成")

        # ── 2. 分层回测 ──
        panel = self._add_groups(panel, factor_col, config.n_groups)
        if config.weight == "factor_weight":
            panel = self._add_factor_weights(panel, factor_col)
        group_nav = self._calc_group_nav(panel, config)
        _emit(78, "groups", "分层净值完成")
        group_stats = self._calc_group_stats(group_nav, config.start, config.end, config.rebalance)

        # ── 3. 多空组合 ──
        long_short_nav, long_short_stats = self._calc_long_short(group_nav, config)
        _check()
        _emit(95, "ls", "多空组合完成")

        elapsed = (time.perf_counter() - t0) * 1000
        _emit(100, "done", "完成")
        return FactorResult(
            run_id=run_id,
            config=self._config_to_dict(config),
            ic_mean=round(ic_mean, 4) if ic_mean is not None else None,
            ic_std=round(ic_std, 4) if ic_std is not None else None,
            ir=round(ir, 4) if ir is not None else None,
            ic_win_rate=round(ic_win_rate, 4) if ic_win_rate is not None else None,
            ic_series=ic_series,
            group_stats=group_stats,
            group_nav=group_nav,
            long_short_stats=long_short_stats,
            long_short_nav=long_short_nav,
            elapsed_ms=round(elapsed, 1),
            n_symbols=n_symbols,
            n_dates=n_dates,
        )

    @staticmethod
    def _compute_missing_factor(panel: pl.DataFrame, factor_col: str) -> pl.DataFrame:
        required = {"symbol", "date", "open", "high", "low", "close", "volume"}
        if not required.issubset(panel.columns):
            missing = sorted(required - set(panel.columns))
            logger.warning("factor %s cannot be computed, missing columns: %s", factor_col, missing)
            return panel

        from app.indicators.pipeline import compute_indicators

        # 只需要单个因子列 → 用 needed 裁剪, 跳过无关的 EMA/KDJ/RSI 等计算 pass
        computed = compute_indicators(panel, needed={factor_col})
        if factor_col not in computed.columns:
            return panel
        return computed.select(["symbol", "date", "close", factor_col])

    # ── IC 计算 ──

    @staticmethod
    def _calc_ic(panel: pl.DataFrame, factor_col: str) -> pl.DataFrame:
        """计算截面 Rank IC (因子值 rank vs 下期收益 rank 的相关系数)。"""
        return (
            panel.filter(pl.col("_next_return").is_not_null())
            .group_by("date")
            .agg(
                pl.corr(
                    pl.col(factor_col).rank(method="average"),
                    pl.col("_next_return").rank(method="average"),
                ).alias("ic")
            )
            .sort("date")
        )

    @staticmethod
    def _rebalance_dates(all_dates: list, rebalance: str) -> list:
        """调仓日 = 每个交易周期(ISO 周 / 自然月)的首个交易日。

        weekly 不能直接按 weekday==0(Monday) 判定: A 股节假日周(春节/国庆)
        的第一个交易日往往是周二甚至周三, 按周一判定会整周跳过调仓。
        这里按交易日所属的 ISO 周分组, 每组第一个交易日即调仓日;
        monthly 按 (年, 月) 分组同理(与"每月首个交易日"一致)。
        """
        if rebalance == "weekly":

            def _key(d):
                return (d.isocalendar().year, d.isocalendar().week)
        else:

            def _key(d):
                return (d.year, d.month)

        result: list = []
        seen: set = set()
        for d in sorted(all_dates):
            k = _key(d)
            if k not in seen:
                seen.add(k)
                result.append(d)
        return result

    # ── 调仓期收益 ──

    @staticmethod
    def _calc_period_return(panel: pl.DataFrame, rebalance: str) -> pl.DataFrame:
        """计算到下个调仓日的收益。

        weekly: 下个 ISO 周首交易日 close / 今日 close - 1
        monthly: 下个月首交易日 close / 今日 close - 1
        只在调仓日标记行有效，其他行为 null。
        """
        all_dates = sorted(panel["date"].unique().to_list())
        rebalance_dates = FactorBacktestService._rebalance_dates(all_dates, rebalance)

        if not rebalance_dates:
            panel = panel.with_columns(pl.lit(None).cast(pl.Float64).alias("_next_return"))
            return panel

        # 对每个调仓日，找到下一个调仓日 (仅在 unique 日期上做, 成本极低)
        sorted_rebalance = sorted(rebalance_dates)
        reb_dates: list = []
        next_dates: list = []
        for i, d in enumerate(sorted_rebalance):
            if i + 1 < len(sorted_rebalance):
                reb_dates.append(d)
                next_dates.append(sorted_rebalance[i + 1])
            # 最后一个调仓日没有下一个，不计算收益

        if not reb_dates:
            panel = panel.with_columns(pl.lit(None).cast(pl.Float64).alias("_next_return"))
            return panel

        panel = panel.sort(["symbol", "date"])
        date_dtype = panel.schema["date"]

        # 调仓日 → 下一调仓日 的映射表 (向量化 JOIN, 替代 Python 逐行 price_map 循环)
        rebal_df = pl.DataFrame(
            {"date": reb_dates, "_next_reb_date": next_dates}
        ).with_columns(
            pl.col("date").cast(date_dtype),
            pl.col("_next_reb_date").cast(date_dtype),
        )

        # (symbol, 下一调仓日) → 该日 close 的查找表 (等价于原 price_map, 重复取 last)
        price_lookup = (
            panel.select(
                pl.col("symbol"),
                pl.col("date").alias("_next_reb_date"),
                pl.col("close").alias("_next_close"),
            )
            .unique(subset=["symbol", "_next_reb_date"], keep="last")
        )

        # 只在调仓日标记行有效: 下一调仓日该股 close / 当日 close - 1; 缺价或非调仓日为 null
        panel = (
            panel.join(rebal_df, on="date", how="left")
            .join(price_lookup, on=["symbol", "_next_reb_date"], how="left")
            .with_columns(
                pl.when(
                    pl.col("_next_reb_date").is_not_null()
                    & pl.col("_next_close").is_not_null()
                    & (pl.col("close") > 0)
                )
                .then(pl.col("_next_close") / pl.col("close") - 1.0)
                .otherwise(None)
                .cast(pl.Float64)
                .alias("_next_return")
            )
            .drop(["_next_reb_date", "_next_close"])
            .sort(["symbol", "date"])
        )
        return panel

    # ── 分组 ──

    @staticmethod
    def _add_groups(panel: pl.DataFrame, factor_col: str, n_groups: int) -> pl.DataFrame:
        """截面序号分桶，避免 qcut 在重复因子值截面上抛错。"""
        return (
            panel.sort(["date", factor_col, "symbol"])
            .with_columns(
                (pl.cum_count("symbol").over("date") - 1).alias("_factor_ord"),
                pl.len().over("date").alias("_factor_count"),
            )
            .with_columns(
                (
                    pl.lit("Q")
                    + (
                        ((pl.col("_factor_ord") * n_groups) / pl.col("_factor_count"))
                        .floor()
                        .cast(pl.Int64)
                        + 1
                    )
                    .clip(1, n_groups)
                    .cast(pl.Utf8)
                )
                .alias("_group")
            )
            .drop(["_factor_ord", "_factor_count"])
        )

    @staticmethod
    def _group_sort_key(group: str) -> int:
        if group.startswith("Q"):
            try:
                return int(group[1:])
            except ValueError:
                pass
        return 0

    # ── 因子加权 ──

    @staticmethod
    def _add_factor_weights(panel: pl.DataFrame, factor_col: str) -> pl.DataFrame:
        """组内按 |因子 − 组均值| 归一作为持仓权重 (绝对暴露度加权)。

        暴露偏离组均值越大, 权重越高; 组内因子无差异(分母≈0)时回落等权。
        只有调仓日行参与收益聚合, 权重仅在这些行上有意义。
        """
        d = pl.col(factor_col) - pl.col(factor_col).mean().over(["date", "_group"])
        s = d.abs().sum().over(["date", "_group"])
        n = pl.len().over(["date", "_group"])
        w = (d.abs() / s).fill_nan(0.0)
        return panel.with_columns(
            pl.when(s > 1e-12).then(w).otherwise(1.0 / n).alias("_weight")
        )

    # ── 换手率与成本 ──

    @staticmethod
    def _calc_group_turnover(reb: pl.DataFrame) -> pl.DataFrame:
        """计算每 (date, group) 的换手率。

        换手率 = 1 - |A_t ∩ A_{t-1}| / |A_t|: 相对上一调仓日、同组持仓中被替换
        的比例。组规模逐期近似不变, 换出与换入对称, 故一个周期产生一次买入成本
        与一次卖出成本(首期除外)。首个调仓日没有旧持仓, turnover 缺省记 1.0
        并置 _first=True, 只计建仓单边成本。
        """
        dates = sorted(reb["date"].unique().to_list())
        prev_map = {dates[i + 1]: dates[i] for i in range(len(dates) - 1)}
        dtype = reb.schema["date"]

        prev_df = (
            pl.DataFrame({"date": list(prev_map.keys()), "_prev_date": list(prev_map.values())})
            .with_columns(pl.col("date").cast(dtype), pl.col("_prev_date").cast(dtype))
        )
        same_group_held = (
            reb.join(prev_df, on="date", how="inner")
            .join(
                reb.select(
                    pl.col("symbol"),
                    pl.col("date").alias("_prev_date"),
                    pl.col("_group").alias("_prev_group"),
                ),
                on=["symbol", "_prev_date"],
                how="inner",
            )
            .filter(pl.col("_prev_group") == pl.col("_group"))
        )
        matched = (
            same_group_held.group_by(["date", "_group"])
            .len()
            .rename({"len": "_stayed_count"})
        )
        total = reb.group_by(["date", "_group"]).len().rename({"len": "_total_count"})

        first_date = dates[0]
        return (
            total.join(matched, on=["date", "_group"], how="left")
            .with_columns(pl.col("_stayed_count").fill_null(0))
            .with_columns(
                (1.0 - pl.col("_stayed_count") / pl.col("_total_count")).alias("_turnover"),
                (pl.col("date") == first_date).alias("_first"),
            )
            .select(["date", "_group", "_turnover", "_first"])
        )

    # ── 分组净值 ──

    @staticmethod
    def _calc_group_nav(panel: pl.DataFrame, config: FactorConfig) -> list[dict]:
        """计算分组净值曲线 — 只在调仓日更新净值。

        毛收益: equal → 组内下期收益均值; factor_weight → 按 _weight 加权。
        成本: 每个调仓周期扣除 换手率 × (fees_pct + slippage/10000) 的双边
        成本(首期只计建仓单边)。净值由扣费后的净收益累乘得到。
        """
        reb = (
            panel.filter(pl.col("_next_return").is_not_null() & pl.col("_group").is_not_null())
        )
        if reb.is_empty():
            return []

        if config.weight == "factor_weight":
            agg = reb.group_by(["date", "_group"]).agg(
                (pl.col("_next_return") * pl.col("_weight")).sum().alias("group_return")
            )
        else:
            agg = reb.group_by(["date", "_group"]).agg(
                pl.col("_next_return").mean().alias("group_return")
            )

        turnover = FactorBacktestService._calc_group_turnover(reb)
        agg = agg.join(turnover, on=["date", "_group"], how="left")
        leg = config.fees_pct + config.slippage_bps / 10000.0
        agg = agg.with_columns(
            (
                pl.col("group_return")
                - leg
                * pl.col("_turnover")
                * (2.0 - pl.col("_first").cast(pl.Float64))
            ).alias("group_return")
        )

        # pivot: date × group
        pivot = agg.pivot(index="date", on="_group", values="group_return").sort("date")

        if pivot.is_empty():
            return []

        group_cols = sorted(
            [c for c in pivot.columns if c != "date"],
            key=FactorBacktestService._group_sort_key,
        )

        # 累乘净值曲线
        result: list[dict] = []
        nav_values: dict[str, float] = {c: 1.0 for c in group_cols}

        for row in pivot.iter_rows(named=True):
            entry: dict = {"date": str(row["date"])[:10]}
            for c in group_cols:
                ret = float(row[c]) if row[c] is not None else 0.0
                nav_values[c] *= (1 + ret)
                entry[c] = round(nav_values[c], 4)
            result.append(entry)

        return result

    # ── 分组统计 ──

    @staticmethod
    def _calc_group_stats(
        group_nav: list[dict], start: date, end: date,
        rebalance: str = "monthly",
    ) -> list[dict]:
        if not group_nav:
            return []

        group_cols = sorted(
            [k for k in group_nav[0] if k != "date"],
            key=FactorBacktestService._group_sort_key,
        )
        n_days = max((end - start).days, 1)
        years = n_days / 365.25

        stats = []
        for i, c in enumerate(group_cols):
            values = [r[c] for r in group_nav if r.get(c) is not None]
            if not values:
                continue
            total_return = values[-1] - 1.0
            annual_return = (values[-1]) ** (1 / max(years, 0.01)) - 1 if values[-1] > 0 else 0.0

            # 最大回撤
            peak = 1.0
            max_dd = 0.0
            for v in values:
                peak = max(peak, v)
                dd = (v - peak) / peak
                max_dd = min(max_dd, dd)

            # 日收益序列
            daily_rets = []
            for j in range(1, len(values)):
                if values[j - 1] > 0:
                    daily_rets.append(values[j] / values[j - 1] - 1)

            # 夏普 — 年化系数必须匹配 group_nav 的调仓频率 (每个净值点 = 一个调仓周期收益);
            # 周/月频收益若乘 √252 会把 Sharpe 高估 √(252/期数) 倍 (月频 ≈4.6x, 周频 ≈2.2x)。
            if daily_rets:
                arr = np.array(daily_rets)
                _ann = {"daily": 252, "weekly": 52, "monthly": 12}.get(rebalance, 252)
                sharpe = float(np.mean(arr) / np.std(arr)) * np.sqrt(_ann) if np.std(arr) > 0 else 0.0
                win_rate = float(np.mean(arr > 0))
            else:
                sharpe = 0.0
                win_rate = 0.0

            stats.append({
                "group": i + 1,
                "label": c,
                "total_return": round(total_return, 4),
                "annual_return": round(annual_return, 4),
                "max_drawdown": round(max_dd, 4),
                "sharpe": round(sharpe, 2),
                "win_rate": round(win_rate, 4),
            })

        return stats

    # ── 多空组合 ──

    @staticmethod
    def _calc_long_short(
        group_nav: list[dict], config: FactorConfig,
    ) -> tuple[list[dict], dict]:
        """多空组合: 做多最高组 + 做空最低组。"""
        if not group_nav:
            return [], {}

        group_cols = sorted(
            [k for k in group_nav[0] if k != "date"],
            key=FactorBacktestService._group_sort_key,
        )
        if len(group_cols) < 2:
            return [], {}

        top_col = group_cols[-1]  # Q5 (最高)
        bottom_col = group_cols[0]  # Q1 (最低)

        # 独立计算 top 和 bottom 的日收益，然后合成
        ls_value = 1.0
        prev_top = 1.0
        prev_bot = 1.0
        peak = 1.0
        max_dd = 0.0
        ls_nav: list[dict] = []

        for row in group_nav:
            top_nav = float(row.get(top_col, 1.0)) if row.get(top_col) is not None else 1.0
            bot_nav = float(row.get(bottom_col, 1.0)) if row.get(bottom_col) is not None else 1.0

            # top 组收益 (做多)
            top_ret = (top_nav / prev_top - 1) if prev_top > 0 else 0.0
            # bottom 组收益 (做空 = 取反)
            bot_ret = -(bot_nav / prev_bot - 1) if prev_bot > 0 else 0.0
            # 多空组合收益
            ls_ret = (top_ret + bot_ret) / 2  # 各分配 50% 资金
            ls_value *= (1 + ls_ret)

            prev_top = top_nav
            prev_bot = bot_nav

            peak = max(peak, ls_value)
            dd = (ls_value - peak) / peak if peak > 0 else 0.0
            max_dd = min(max_dd, dd)

            ls_nav.append({"date": row["date"], "value": round(ls_value, 4)})

        total_ret = ls_value - 1.0
        values = [r["value"] for r in ls_nav]
        years = max((config.end - config.start).days, 1) / 365.25
        annual = (values[-1] ** (1 / years) - 1) if values and values[-1] > 0 else 0.0
        ls_rets = [
            values[i] / values[i - 1] - 1
            for i in range(1, len(values))
            if values[i - 1] > 0
        ]
        if ls_rets:
            arr = np.array(ls_rets)
            _ann = {"daily": 252, "weekly": 52, "monthly": 12}.get(config.rebalance, 252)
            sharpe = float(np.mean(arr) / np.std(arr)) * np.sqrt(_ann) if np.std(arr) > 0 else 0.0
            win_rate = float(np.mean(arr > 0))
        else:
            sharpe = 0.0
            win_rate = 0.0
        ls_stats = {
            "total_return": round(total_ret, 4),
            "annual_return": round(annual, 4),
            "max_drawdown": round(max_dd, 4),
            "sharpe": round(sharpe, 2),
            "win_rate": round(win_rate, 4),
            "top_group": top_col,
            "bottom_group": bottom_col,
        }

        return ls_nav, ls_stats

    @staticmethod
    def _config_to_dict(c: FactorConfig) -> dict:
        return {
            "factor_name": c.factor_name,
            "symbols": c.symbols,
            "start": str(c.start),
            "end": str(c.end),
            "n_groups": c.n_groups,
            "rebalance": c.rebalance,
            "weight": c.weight,
            "fees_pct": c.fees_pct,
            "slippage_bps": c.slippage_bps,
            "asset_type": c.asset_type,
        }

    def run_batch(
        self,
        factors: list[str],
        config: FactorConfig,
        progress_cb=None,
        cancel_event=None,
    ) -> dict:
        """多因子批量评估 — 共享面板与下期收益, 逐因子算 IC / 分层 / 多空。

        与单因子复用同一套口径 (Rank IC、序号分桶、换手率成本、Q5-Q1 多空),
        额外输出因子间「日 IC 序列」的相关矩阵。共用的面板与 forward return
        保证各因子在同一股票池/日期网格上可比。

        progress_cb / cancel_event 语义与 run() 一致。
        返回 dict:
          {run_id, config, factors:[{name, ic_mean, ic_std, ir, ic_win_rate,
             ic_obs, ls_total_return, ls_annual_return, ls_sharpe, ls_win_rate}],
           skipped:[...], ic_corr:{names, matrix}, n_symbols, n_dates,
           elapsed_ms, error}
        """
        t0 = time.perf_counter()
        run_id = uuid.uuid4().hex[:10]

        def _emit(pct: int, stage: str, message: str) -> None:
            if progress_cb is not None:
                progress_cb({"pct": pct, "stage": stage, "message": message})

        def _check() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise FactorCancelled()

        def _err(msg: str) -> dict:
            return {
                "run_id": run_id,
                "config": self._config_to_dict(config),
                "factors": [],
                "skipped": list(factors),
                "ic_corr": {"names": [], "matrix": []},
                "n_symbols": 0,
                "n_dates": 0,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": msg,
            }

        names = list(dict.fromkeys(factors))
        if not names:
            return _err("未指定因子")
        truncated = False
        if len(names) > FACTOR_BATCH_MAX:
            names = names[:FACTOR_BATCH_MAX]
            truncated = True

        base_cols = ["symbol", "date", "open", "high", "low", "close",
                     "volume", "turnover_rate"]
        panel_columns = base_cols + [f for f in names if f not in base_cols]
        load_start = config.start - timedelta(days=FACTOR_WARMUP_DAYS)

        _emit(2, "pending", "任务排队")
        _check()
        panel = self.engine.load_panel(
            config.symbols, load_start, config.end,
            columns=panel_columns, asset_type=config.asset_type,
        )
        if panel.is_empty():
            return _err("无数据，请检查日期范围或先运行盘后管道")

        # 缺列因子一次性补算 (compute_indicators needed 支持批量)。
        # 未知/不可计算的因子名会被流水线拒绝: try/except 兜底, 这类因子
        # 保持缺失, 后续逐因子标记 skipped (不允许一个坏名字拖垮整批)。
        missing = [f for f in names if f not in panel.columns]
        if missing:
            try:
                from app.indicators.pipeline import compute_indicators

                required = {"symbol", "date", "open", "high", "low", "close", "volume"}
                if required.issubset(panel.columns):
                    computed = compute_indicators(panel, needed=set(missing))
                    new_cols = [c for c in computed.columns if c in missing]
                    if new_cols:
                        add = computed.select(
                            ["symbol", "date"] + [c for c in new_cols if c not in ("symbol", "date")]
                        )
                        panel = panel.join(add, on=["symbol", "date"], how="left")
            except Exception as e:  # noqa: BLE001
                logger.warning("factor batch: 批量补算失败 (未知因子将标记 skipped): %s", e)
        _check()
        _emit(30, "panel", f"面板就绪: {len(names)} 个因子")

        panel = panel.filter(
            (pl.col("date") >= config.start) & (pl.col("date") <= config.end)
        ).filter(pl.col("close").is_not_null() & (pl.col("close") > 0))
        if panel.is_empty():
            return _err("过滤后无有效数据")

        # 下期收益只取决于日期网格: 全因子共享, 计算一次
        _emit(40, "returns", "计算下期收益")
        if config.rebalance == "daily":
            ret_df = panel.select(["symbol", "date", "close"]).with_columns(
                (pl.col("close").shift(-1).over("symbol") / pl.col("close") - 1)
                .alias("_next_return")
            )
        else:
            ret_df = self._calc_period_return(
                panel.select(["symbol", "date", "close"]), config.rebalance
            ).select(["symbol", "date", "_next_return"])

        n_symbols = panel["symbol"].n_unique()
        n_dates = panel["date"].n_unique()

        records: list[dict] = []
        skipped: list[str] = []
        ic_series_by: dict[str, dict] = {}
        total = len(names)
        for idx, name in enumerate(names):
            _check()
            if name not in panel.columns:
                skipped.append(name)
                continue
            sub = panel.select(["symbol", "date", "close", name]).join(
                ret_df, on=["symbol", "date"], how="left"
            )
            sub = sub.filter(
                pl.col(name).is_not_null()
                & pl.col("_next_return").is_not_null()
                & pl.col("close").is_not_null()
                & (pl.col("close") > 0)
            )
            if sub.is_empty():
                skipped.append(name)
                continue

            ic_df = self._calc_ic(sub, name)
            ic_rows = [
                (str(row["date"]), float(row["ic"]))
                for row in ic_df.iter_rows(named=True)
                if row["ic"] is not None and not np.isnan(float(row["ic"]))
            ]
            ic_vals = [v for _, v in ic_rows]
            ic_mean = float(np.mean(ic_vals)) if ic_vals else None
            ic_std = float(np.std(ic_vals)) if ic_vals else None
            ir = (ic_mean / ic_std) if (ic_mean is not None and ic_std and ic_std > 1e-8) else None
            ic_win = (sum(1 for v in ic_vals if v > 0) / len(ic_vals)) if ic_vals else None
            ic_series_by[name] = dict(ic_rows)

            grouped = self._add_groups(sub, name, config.n_groups)
            nav = self._calc_group_nav(grouped, config)
            _, ls_stats = self._calc_long_short(nav, config)

            records.append({
                "name": name,
                "ic_mean": round(ic_mean, 4) if ic_mean is not None else None,
                "ic_std": round(ic_std, 4) if ic_std is not None else None,
                "ir": round(ir, 4) if ir is not None else None,
                "ic_win_rate": round(ic_win, 4) if ic_win is not None else None,
                "ic_obs": len(ic_vals),
                "ls_total_return": ls_stats.get("total_return"),
                "ls_annual_return": ls_stats.get("annual_return"),
                "ls_sharpe": ls_stats.get("sharpe"),
                "ls_win_rate": ls_stats.get("win_rate"),
            })
            pct = 45 + int(idx / total * 45)
            _emit(pct, "factor", f"完成 {name} ({idx + 1}/{total})")

        corr_names: list[str] = []
        corr_matrix: list[list] = []
        with_values = [name for name in names if ic_series_by.get(name)]
        if len(with_values) >= 2:
            _emit(95, "corr", "计算 IC 相关矩阵")
            # 对齐公共 IC 日期后两两 Pearson
            common = None
            for name in with_values:
                ds = set(ic_series_by[name])
                common = ds if common is None else common & ds
            aligned = {
                name: [ic_series_by[name][d] for d in sorted(common)]
                for name in with_values
                if all(d in ic_series_by[name] for d in common)
            }
            corr_names = list(aligned)
            arrs = np.array([aligned[n] for n in corr_names], dtype=float)
            cm = np.corrcoef(arrs)
            if arrs.shape[1] >= 2 and not np.isnan(cm).all():
                corr_matrix = [
                    [round(float(v), 4) if not np.isnan(v) else None for v in row]
                    for row in cm.tolist()
                ]

        skipped = list(dict.fromkeys(skipped))
        message = f"完成 {len(records)}/{total} 个因子"
        if truncated:
            message += f", 已截断到 {FACTOR_BATCH_MAX} 个"
        if skipped:
            message += f", 跳过 {len(skipped)} 个"
        _emit(100, "done", message)
        return {
            "run_id": run_id,
            "config": self._config_to_dict(config),
            "factors": records,
            "skipped": skipped,
            "ic_corr": {"names": corr_names, "matrix": corr_matrix},
            "n_symbols": n_symbols,
            "n_dates": n_dates,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            "error": None,
        }
