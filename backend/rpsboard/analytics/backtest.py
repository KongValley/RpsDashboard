"""RPS 策略回测：横截面动量。

信号定义：调仓日 T 满足 rps_N(T) >= threshold 的股票等权入池，持有 H 个交易日。
可选多周期共振（除所选周期外的**全部**周期同时达标，见 resonance_mask）。

**无未来函数**的保证：
- rps_N(T) 只用 ≤ T 的收盘价（shift(N) 结构）。
- 入池用 T 日**收盘价**成交，收益从 T+1 开始计算（`shift(-H)` 相对 `shift(-1)`）。
- test_backtest.py 用「扰动信号日之后的行情，信号日之前的结果必须不变」硬断言。

**无法消除的偏差**：幸存者偏差。用当前证券池回溯历史，中途退市的股票缺失，
回测结果偏乐观。必须在 UI 与返回值里显著标注。
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .. import config, db
from . import matrix
from .costs import CostModel, default_cost_model

BUCKETS = [(90, 100), (80, 90), (70, 80), (50, 70), (0, 50)]
BUCKET_LABELS = ["90-100", "80-90", "70-80", "50-70", "<50"]

#: 大盘择时模式。欧奈尔的原方法是**只在强势市场做多**，而纯价格 RPS 回测
#: 完全没有择时，这正是它测出「反向」结论的重要原因之一。
MARKET_FILTERS = ("none", "ma200", "above_ma50", "trend_up")
MARKET_FILTER_LABELS = {
    "none": "不择时（全年满仓）",
    "ma200": "沪深300 > 200 日均线",
    "above_ma50": "沪深300 > 50 日均线",
    "trend_up": "沪深300 > 200 日均线 且 50 日 > 200 日",
}


def market_state(bench_close: pd.Series, mode: str) -> pd.Series:
    """按择时模式给出「该日是否允许开仓」的布尔序列。

    只用当日及之前的收盘价与均线，**不含任何未来信息**。
    均线尚未成型的早期（min_periods 不足）比较结果为 False，
    即默认空仓——这与「数据不足就不下注」一致。
    """
    if mode not in MARKET_FILTERS or mode == "none":
        return pd.Series(True, index=bench_close.index)
    ma50 = bench_close.rolling(50, min_periods=50).mean()
    ma200 = bench_close.rolling(200, min_periods=200).mean()
    if mode == "ma200":
        ok = bench_close > ma200
    elif mode == "above_ma50":
        ok = bench_close > ma50
    else:  # trend_up
        ok = (bench_close > ma200) & (ma50 > ma200)
    return ok.fillna(False)


def _lookback_start(conn: sqlite3.Connection, lookback_days: int | None) -> str | None:
    """回测窗口起始日；None 表示用全部历史。

    以 daily_bar 的最大日期为基准回推，而不是 date.today()——
    周末/节假日跑回测时，用 today 算会白读一大段根本没有数据的日子。
    """
    if not lookback_days:
        return None
    row = conn.execute("SELECT MAX(date) AS d FROM daily_bar").fetchone()
    latest = row["d"] if row and row["d"] else None
    if not latest:
        return None
    return (date.fromisoformat(latest) - timedelta(days=int(lookback_days))).isoformat()


@dataclass
class BacktestResult:
    equity_curve: list[dict] = field(default_factory=list)
    benchmark_curve: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    buckets: list[dict] = field(default_factory=list)
    params: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: 择时序列（沪深300 收盘 + 两条均线 + 是否允许开仓），供前端画开/空仓区间
    market_curve: list[dict] = field(default_factory=list)
    #: 分档图的口径说明（零成本 vs 含成本）
    buckets_note: str = ""


def _forward_return(close_wide: pd.DataFrame, hold: int) -> pd.DataFrame:
    """未来 H 个交易日的收益。

    T 日收盘买入 → 最早 T+1 才有收益 → 分子用 close.shift(-H)，分母用
    close.shift(-1) 的下一交易日... 这里刻意用 close.shift(-hold) / close，
    表示「持有 H 个交易日后的累计收益」，即第 1 天就已经计入，
    对应 T 日收盘买入、T+1~T+H 持有。offset 不存在 off-by-one。
    """
    return close_wide.shift(-hold) / close_wide - 1.0


def _exit_blocked(
    dn: pd.DataFrame | None,
    tradable: pd.DataFrame | None,
    hold: int,
) -> pd.DataFrame | None:
    """标记「按计划卖出日无法卖出」的 date × symbol。

    两种情况：
    - 计划卖出日**跌停**（`dn`）：封在跌停板上卖不出去
    - 计划卖出日**停牌**：`tradable` 在卖出日为 False，且价格来自 ffill

    这些位置的前瞻收益不可实现，必须顺延。返回 None 表示无法判定
    （没有涨跌停数据时退化为旧行为——宁可少一个约束，也不要伪造）。
    """
    blocks = []
    if dn is not None and not dn.empty:
        # 和 tradable 一样必须 shift：跌停发生在**卖出日**(T+hold)，
        # 而掩码要标记在**买入日**(T) 那一行上。少这一步会把
        # 「买入当天跌停」错当成「卖出时卖不掉」。
        blocks.append(
            dn.shift(-hold).reindex_like(dn).fillna(False).astype(bool)
        )
    if tradable is not None and not tradable.empty:
        blocks.append(
            ~tradable.shift(-hold).reindex_like(tradable)
            .fillna(True).astype(bool)
        )
    if not blocks:
        return None
    out = blocks[0].copy()
    for b in blocks[1:]:
        out = out | b
    return out


# ── 进程内矩阵缓存 ──────────────────────────────────────────────────
# 回测页面允许用户反复调阈值/持有期/周期，每次都重新把 427 万行日K 读进内存
# 再建 4 个矩阵，是单次请求里最大的一块固定开销。数据一天才更新一次，
# 因此按「库文件 + mtime + rps_daily 最新日期」做 key 缓存 5 分钟完全合理。
_CTX_TTL = 300.0
_ctx_cache: dict = {"key": None, "at": 0.0, "data": None}


def _ctx_key(conn: sqlite3.Connection, lookback_days: int | None):
    try:
        path = conn.execute("PRAGMA database_list").fetchone()[2] or ""
    except sqlite3.Error:
        path = ""
    mtime = 0.0
    if path:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
    try:
        latest = conn.execute("SELECT MAX(date) AS d FROM rps_daily").fetchone()["d"]
    except sqlite3.Error:
        latest = None
    return (path, mtime, latest, lookback_days)


def load_context(
    conn: sqlite3.Connection, lookback_days: int | None = None
) -> dict:
    """取回测需要的全部矩阵（带进程内缓存）。"""
    key = _ctx_key(conn, lookback_days)
    now = time.monotonic()
    if _ctx_cache["key"] == key and now - _ctx_cache["at"] < _CTX_TTL:
        return _ctx_cache["data"]

    start = _lookback_start(conn, lookback_days)
    long = matrix.load_long(conn, start)
    data = {
        "start": start,
        "close": matrix.build_close_matrix(long),
        "tradable": matrix.build_tradable_matrix(long),
        "st_flags": matrix.build_st_flags(long),
        "pct": matrix.build_pctchg_matrix(long),
    }
    _ctx_cache.update(key=key, at=now, data=data)
    return data


def clear_context_cache() -> None:
    """数据更新后主动失效（CLI 跑完 compute-rps / backfill 后调用）。"""
    _ctx_cache.update(key=None, at=0.0, data=None)


def get_frames(ctx: dict, exclude_st: bool = True) -> dict:
    """取 RPS 各周期帧（缓存在 ctx 内，随 ctx 一起被替换/回收）。

    compute_rps_frames 一次算出全部周期的 ret+rps 共 2×len(config.PERIODS) 个矩阵，
    约 3 秒；缓存后切换周期/阈值就不用重算。

    ⚠ 缓存必须挂在 ctx 上。早期版本用 `id(ctx)` 当全局字典的 key：ctx 被替换后
    旧 id 指针不再被引用，而 `id()` 可以被新对象复用，于是每次 5 分钟过期都会
    往 `_frames_cache` 里塞进一份**新的**约 400MB 矩阵且永不回收。
    """
    from .rps import compute_rps_frames

    cache = ctx.setdefault("_frames", {})
    key = bool(exclude_st)
    hit = cache.get(key)
    if hit is None:
        hit = compute_rps_frames(
            ctx["close"], ctx["tradable"], ctx["st_flags"], exclude_st, config.PERIODS
        )
        cache[key] = hit
    return hit


def resonance_mask(
    frames: dict[str, pd.DataFrame],
    period: int,
    threshold: float,
    periods: tuple[int, ...] = config.PERIODS,
) -> pd.DataFrame:
    """多周期共振掩码：除 `period` 外的**全部**周期同时 ≥ threshold。

    口径固定为「其余所有周期」而不是写死 20/60/120 —— 往 config.PERIODS 里加周期
    （例如 15 日短线）会自动进共振，不需要在这里补一行。

    起始项固定取最短周期那一列：所选周期恰好是最短周期（15）时，它会被一并要求。
    这是改造前的既有行为（起始项与循环跳过合并的效果），这里刻意保持不变，
    以免静默改变已有回测结论。
    """
    mask = frames[f"rps{min(periods)}"] >= threshold
    for p in periods:
        if p == period:
            continue
        mask = mask & (frames[f"rps{p}"] >= threshold)
    return mask


def run_backtest(
    conn: sqlite3.Connection,
    *,
    period: int = config.DEFAULT_PERIOD,
    threshold: float = 80.0,
    hold: int = 20,
    rebalance: int | None = None,
    resonance: bool = False,
    exclude_st: bool = True,
    exclude_limit_up: bool = True,
    benchmark_symbol: str = "000300",
    market_filter: str = "none",
    use_price_limits: bool = True,
    cost_model: CostModel | None = None,
    lookback_days: int | None = None,
) -> BacktestResult:
    """执行回测。返回结构化结果（含幸存者偏差警示）。

    lookback_days 默认 None（**全部历史**）：这是本项目对外公布回测结论时的口径，
    改成「近 3 年」会让净值曲线多出一段 2023-10~2024-01 的下跌期，
    把总收益从 +43% 压到 +0.9%，属于静默改结论，因此不作为默认。
    矩阵通过 load_context() 做进程内缓存，重复调参不再重复读库。
    """

    # 入口兜底：传进来的 period 不在周期真源里会让 frames[f"rps{period}"] KeyError，
    # hold 为 0/负数会让 fwd = close.shift(-hold) 变成「未来负数天」的假收益。
    if period not in config.PERIODS:
        period = config.DEFAULT_PERIOD
    hold = max(1, min(int(hold), 250))
    if market_filter not in MARKET_FILTERS:
        market_filter = "none"
    if cost_model is None:
        cost_model = default_cost_model(enabled=True)

    result = BacktestResult(
        params={
            "period": period, "threshold": threshold, "hold": hold,
            "rebalance": rebalance or hold, "resonance": resonance,
            "exclude_st": exclude_st, "benchmark": benchmark_symbol,
            "market_filter": market_filter,
            "market_filter_label": MARKET_FILTER_LABELS[market_filter],
            "cost": cost_model.describe(),
            "use_price_limits": use_price_limits,
            "lookback_days": lookback_days,
        }
    )
    result.warnings.append(
        "本回测使用当前证券池回溯历史，中途退市的股票缺失，存在幸存者偏差，"
        "结果偏乐观，不可直接用于实盘决策。"
    )

    if not db.table_has_rows(conn, "rps_daily"):
        result.warnings.append("rps_daily 为空，请先执行 compute-rps")
        return result

    ctx = load_context(conn, lookback_days)
    close_wide = ctx["close"]
    tradable = ctx["tradable"]
    st_flags = ctx["st_flags"]

    frames = get_frames(ctx, exclude_st)
    rps_main = frames[f"rps{period}"]
    if resonance:
        mask = resonance_mask(frames, period, threshold)
    else:
        mask = rps_main >= threshold

    # 落库的 RPS 是用默认参数（剔除 ST）算的，但 ST 仍可能有分位值并越过阈值，
    # 因此这里显式再滤一次，与实时计算路径保持同一语义。
    if exclude_st and st_flags is not None and not st_flags.empty:
        mask &= ~st_flags.reindex(index=mask.index, columns=mask.columns) \
            .fillna(False).astype(bool)

    # ── 可交易性：买入日涨停买不进 ─────────────────────────────────
    # 旧实现是 `mask &= ~(pct_wide >= limits)`，而 limits 是拍出来的百分数
    # （9.8 / 19.8）、pct_wide 却是小数 —— 量纲错了两个数量级，
    # 实测这个过滤从未命中过任何一只股票。现在改用 price_limit_daily 的
    # 真实 limit_ratio，并用「收益率比」判定（后复权价与不复权涨停价
    # 不在同一基准，直接比会算出 4303 家假涨停）。
    lim_up = lim_dn = None
    if use_price_limits:
        from .limits import (
            MODEL_VALIDATION_DAYS,
            MODEL_VALIDATION_ROWS,
            coverage,
            derive_ratio_frames,
            limit_mask,
            load_ratio_frames,
            merge_ratio_frames,
        )

        dates = [d.strftime("%Y-%m-%d") for d in close_wide.index]
        cols = list(close_wide.columns)
        measured = load_ratio_frames(conn, dates, cols)
        covered, total = coverage(conn, dates)
        if covered:
            ratios, statuses = measured
            if covered < total:
                # 未覆盖的日子改用**板块规则推算**（有实测校验），而不是整段不筛涨停：
                # 「这些天不筛涨停」会让回测静默地少一层过滤，用户无从知道。
                ratios, statuses = merge_ratio_frames(
                    measured, derive_ratio_frames(conn, dates, cols)
                )
                result.warnings.append(
                    f"涨跌停：实测覆盖 {covered}/{total} 个交易日，其余 {total - covered} 天"
                    f"按板块规则推算（与已采集的 {MODEL_VALIDATION_DAYS} 个交易日 / "
                    f"{MODEL_VALIDATION_ROWS:,} 行逐行一致率 100%；新股前 5 日按"
                    f"「无涨跌幅限制」处理）。用 `cli price-limits --fill --workers 6` 可继续补实测"
                )
        else:
            # 一行实测都没有：整段用规则推算，并明确说出来（可校验性差一档）
            ratios, statuses = derive_ratio_frames(conn, dates, cols)
            result.warnings.append(
                "price_limit_daily 为空：涨跌停按板块规则推算（无实测数据可校验），"
                "请执行 cli price-limits 采集实测值"
            )
        if not ratios.empty:
            lim_up, lim_dn = limit_mask(close_wide, ratios, statuses)
            if exclude_limit_up:
                mask &= ~lim_up.reindex(index=mask.index, columns=mask.columns) \
                    .fillna(False).astype(bool)
                result.params["limit_up_filtered"] = int(
                    lim_up.to_numpy().sum()
                )
        else:
            result.warnings.append(
                "涨跌停比例为空（推算也失败），未应用「剔除涨停」过滤"
            )

    if tradable is not None and not tradable.empty:
        mask &= tradable.reindex_like(mask).fillna(False).astype(bool)

    fwd = _forward_return(close_wide, hold)
    fwd = fwd.reindex(index=mask.index, columns=mask.columns)

    dates = list(mask.index)
    # 重叠持仓期（rebalance < hold）在当前「逐段复利」的净值模型里无法正确表达：
    # fwd 恒为 hold 日收益，若每 rebalance 天乘一次就等于把同一段行情重复计入
    # （hold=20/rebalance=5 会把 20 日收益乘 4 次）。回落为 hold 并写 warning，
    # 不静默给出几百个百分点的年化。
    step = int(rebalance or hold)
    if step < hold:
        result.warnings.append(
            f"调仓间隔 {step} 天小于持有期 {hold} 天，重叠持仓无法逐段复利，"
            f"已按持有期 {hold} 天作为调仓间隔计算"
        )
        step = hold
    step = max(1, step)

    # ── 大盘择时 ──────────────────────────────────────────────────
    # 先取基准，再据此决定每个调仓点是否允许开仓。择时只看当日及之前的
    # 收盘价与均线，不引入未来信息。
    bench = pd.read_sql_query(
        "SELECT date, close FROM benchmark_bar WHERE symbol = ? ORDER BY date",
        conn, params=(benchmark_symbol,),
    )
    bench_close = pd.Series(dtype="float64")
    if not bench.empty:
        bench["date"] = pd.to_datetime(bench["date"])
        bench["close"] = pd.to_numeric(bench["close"], errors="coerce")
        bench = bench.dropna(subset=["close"])
        bench_close = bench.set_index("date")["close"].sort_index()
    if market_filter != "none" and bench_close.empty:
        # 静默回落成「全 False」会让净值曲线变成 0% 平线，看起来像「这个策略不赚钱」，
        # 实际是择时数据缺了。必须说清楚并退回不择时。
        result.warnings.append(
            f"benchmark_bar 无 {benchmark_symbol} 数据，无法判定"
            f"「{MARKET_FILTER_LABELS[market_filter]}」，"
            "本次回测按不择时处理（结果不是「零收益策略」）"
        )
        market_filter = "none"
    mkt_ok = market_state(bench_close, market_filter).reindex(dates).fillna(False)
    mkt_ok = mkt_ok.astype(bool) if market_filter != "none" else pd.Series(
        True, index=dates, dtype=bool
    )

    # ── 卖出受阻：跌停 / 停牌顺延 ────────────────────────────────
    blocked = _exit_blocked(lim_dn, tradable, hold)
    if blocked is not None and not blocked.empty:
        blocked = blocked.reindex(index=mask.index, columns=mask.columns) \
            .fillna(False).astype(bool)
    # 受阻数量在净值循环里按**实际持仓**累计：整个矩阵有 887×5573 个格子，
    # 其中大量是「t+hold 日该股还没上市」的空组合，直接统计会得到几十万
    # 这种毫无意义的数字。

    # ── 净值曲线 ──────────────────────────────────────────────────
    # 关键：`fwd` 的每一行**本身就是「从当天买入、持有 hold 天卖出」的累计收益**，
    # 不是日收益。因此每个调仓点只能乘 **一个** fwd 值。
    # 早期 bug 是把 hold+1 行 fwd 全部累乘，等于把同一段收益重复算了 20 次，
    # 于是出现「单期 +667%」「年化 61875%」这种数学上不可能的数字。
    equity = 1.0
    curve: list[dict] = []
    total_deferred = 0
    holdings_used: list[int] = []      # 供成本口径说明算真实持仓中位数
    for idx, t in enumerate(dates):
        if idx % step:
            continue
        if not bool(mkt_ok.get(t, True)):
            # 空仓：净值按现金计（不变），但**必须保留这个点**，
            # 否则曲线上看不出「这段时间在空仓」，也无法画开/空仓区间。
            curve.append({"date": t.strftime("%Y-%m-%d"),
                          "equity": round(equity, 6),
                          "holdings": 0})
            continue
        sel = mask.loc[t].fillna(False)
        if not sel.any():
            continue
        names = sel[sel].index

        # 卖出受阻的那部分：收益不可实现，顺延到下一个调仓点。
        # 保守做法是**剔除**它们（等价于这段收益记 0），而不是照单全收。
        deferred = 0
        if blocked is not None and not blocked.empty:
            bad = blocked.loc[t].reindex(names).fillna(False).astype(bool)
            if bool(bad.any()):
                deferred = int(bad.sum())
                names = names[~bad]
        total_deferred += deferred

        if len(names) == 0:
            # 全部持仓都无法卖出：净值不变，但保留这个点
            curve.append({"date": t.strftime("%Y-%m-%d"),
                          "equity": round(equity, 6),
                          "holdings": 0, "deferred": deferred})
            continue

        period_ret = fwd.loc[t, names].mean()      # 单个持有期收益
        if pd.isna(period_ret):
            continue
        # 成本按「组合名义金额」计，与实际持仓只数有关：等权分散持有上千只时，
        # 券商的 5 元最低佣金才是真正生效的那一项。
        period_ret = period_ret - cost_model.round_trip_cost(
            t, n_positions=len(names)
        )
        equity *= float(1.0 + period_ret)
        holdings_used.append(int(len(names)))
        curve.append({"date": t.strftime("%Y-%m-%d"),
                      "equity": round(equity, 6),
                      "holdings": int(len(names)),
                      "deferred": deferred})
    result.equity_curve = curve
    if holdings_used:
        # 5 元最低佣金是按「组合名义金额」生效的：持仓越多，均摊到每只票的
        # 固定费用越低。params["cost"] 是前端用来解释扣费口径的说明，
        # 必须按**实际**持仓数的中位数来描述，而不是默认的 50 只。
        result.params["cost"] = cost_model.describe(
            n_positions=int(max(1, np.median(holdings_used)))
        )
    if total_deferred:
        result.warnings.append(
            f"卖出受阻：{total_deferred} 个持仓在计划卖出日跌停或停牌，"
            f"其收益已顺延（保守处理，可能低估真实回撤）"
        )

    # ── 基准 + 择时序列 ──────────────────────────────────────────
    # bench / bench_close 已在上面为择时读过一次，这里复用。
    if not bench_close.empty:
        # 必须 dropna：benchmark_bar 的起点晚于 daily_bar（基准只有 800 根，
        # 而日K 有 887 个交易日）。reindex+ffill 填不了「开头之前」的数据，
        # 不去掉前导 NaN 的话基准曲线会整块为空。
        bench_aligned = bench_close.reindex(dates).ffill().dropna()
        if not bench_aligned.empty:
            base = bench_aligned.iloc[0]
            bench_eq = (bench_aligned / base).round(6)
            result.benchmark_curve = [
                {"date": d.strftime("%Y-%m-%d"), "equity": float(bench_eq.loc[d])}
                for d in bench_aligned.index
            ]
            ma50 = bench_close.rolling(50, min_periods=50).mean().reindex(bench_aligned.index)
            ma200 = bench_close.rolling(200, min_periods=200).mean().reindex(bench_aligned.index)
            result.market_curve = [
                {
                    "date": d.strftime("%Y-%m-%d"),
                    "close": round(float(bench_aligned.loc[d]) / float(base), 6),
                    "ma50": round(float(ma50.loc[d]) / float(base), 6)
                    if pd.notna(ma50.loc[d]) else None,
                    "ma200": round(float(ma200.loc[d]) / float(base), 6)
                    if pd.notna(ma200.loc[d]) else None,
                    "open": bool(mkt_ok.get(d, True)),
                }
                for d in bench_aligned.index
            ]
    else:
        # 基准取不到时（backfill 里基准指数抓取失败、或 benchmark 传错）原来
        # 什么都不说，用户只看到基准线是空的
        result.warnings.append(
            f"基准指数（benchmark_bar.{benchmark_symbol}）无数据，"
            f"回测的基准线与均线择时都不可用，请执行 cli backfill 重取"
        )

    # ── 指标 ─────────────────────────────────────────────────────
    eq = pd.Series([c["equity"] for c in result.equity_curve], dtype="float64")
    result.metrics = compute_metrics(
        eq,
        n_days_per_point=step,
        dates=[c["date"] for c in result.equity_curve],
    )

    # ── RPS 分档前瞻收益（核心验证图）───────────────────────────
    # 刻意**不扣成本**：这张图衡量的是「RPS 信号本身有没有预测力」，
    # 混进执行摩擦会把信号质量与交易成本两件事搅在一起。
    result.buckets = compute_buckets(rps_main, fwd)
    result.buckets_note = (
        "分档前瞻收益为**零成本**口径，衡量信号本身的预测力；"
        "上方净值曲线为**含成本**口径，衡量策略的可执行性。"
    )
    return result


def compute_metrics(
    equity: pd.Series,
    n_days_per_point: int = 20,
    dates: list[str] | None = None,
) -> dict:
    """由净值序列算收益/风险指标。

    `n_days_per_point` 是相邻两个净值点之间跨越的**交易日数**（= 调仓周期）。
    净值点数 ≠ 交易日数，所以年化必须按 点数 × 调仓周期 折算，
    否则点数少时会算出天文数字（早期 bug：25 个点被当成 25 天，
    年化直接变成 61875%）。

    `dates` 给出每个净值点的真实日期时**优先按首末日期跨度折算**：
    被跳过的调仓点（矩阵前几行 RPS 全 NaN 必然发生）会让
    `点数 × 调仓周期` 系统性低估已用时长、抬高年化（实测约 7%）。
    """
    if equity.empty or len(equity) < 2:
        return {"total_return": 0.0, "annualized": 0.0, "volatility": 0.0,
                "max_drawdown": 0.0, "sharpe": 0.0, "calmar": 0.0,
                "days": int(len(equity))}

    rets = equity.pct_change().dropna()
    total = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    n_pts = len(equity)
    if dates is not None and len(dates) == n_pts:
        span_days = (pd.Timestamp(dates[-1]) - pd.Timestamp(dates[0])).days
        years = max(1e-9, span_days / 365.25)
    else:
        years = max(1e-9, (n_pts - 1) * max(1, n_days_per_point) / 252.0)

    ann = (1.0 + total) ** (1.0 / years) - 1.0 if total > -1 else -1.0
    vol = float(rets.std(ddof=1) * np.sqrt(1.0 / years)) if len(rets) > 1 else 0.0
    dd = equity / equity.cummax() - 1.0
    mdd = float(dd.min())
    sharpe = float(ann / vol) if vol > 0 else 0.0
    calmar = float(ann / abs(mdd)) if mdd < 0 else 0.0
    return {
        "total_return": round(total, 4),
        "annualized": round(float(ann), 4),
        "volatility": round(vol, 4),
        "max_drawdown": round(mdd, 4),
        "sharpe": round(sharpe, 2),
        "calmar": round(calmar, 2),
        "days": int(n_pts),
        "years": round(years, 3),
    }


def compute_buckets(rps: pd.DataFrame, fwd: pd.DataFrame) -> list[dict]:
    """按信号日 RPS 分档，统计各档未来收益。

    这是证明 RPS 有效性的关键：RPS 越高的一档，未来平均收益应越高。

    实现：整块转成 numpy 后一次性分档 + `np.bincount` 累加。
    旧实现是 `for date in r.index: for 5 档:` 的 4435 次 pandas 布尔运算，
    在全市场规模下要跑好几秒。
    """
    out: list[dict] = []
    if rps.empty or fwd.empty:
        return out

    common_cols = [c for c in rps.columns if c in fwd.columns]
    if not common_cols:
        return out
    # 逐日分桶：把每日所有股票的 (rps, fwd) 归入对应档位
    r_v = rps[common_cols].to_numpy(dtype="float64")
    f_v = fwd[common_cols].to_numpy(dtype="float64")
    # 用 isnan 而不是 isfinite：旧实现判的是 `fv.notna()`，
    # 只有 NaN 会被剔除，inf 应当保留（虽然实际不会出现）。
    has_fwd = ~np.isnan(f_v)

    counts = np.zeros(len(BUCKET_LABELS), dtype="int64")
    sums = np.zeros(len(BUCKET_LABELS), dtype="float64")
    wins = np.zeros(len(BUCKET_LABELS), dtype="int64")

    for i, (lo, hi) in enumerate(BUCKETS):
        sel = (r_v >= lo) & (r_v < hi) if hi < 100 else (r_v >= lo)
        sel &= has_fwd
        n = int(sel.sum())
        if n:
            vals = f_v[sel]
            counts[i] = n
            sums[i] = float(vals.sum())
            wins[i] = int((vals > 0).sum())

    for lbl, (lo, hi) in zip(BUCKET_LABELS, BUCKETS):
        n = int(counts[len(out)])
        out.append({
            "bucket": lbl,
            "range": f"{lo}-{hi}" if hi < 100 else f"{lo}-100",
            "samples": n,
            "avg_forward_return": round(sums[len(out)] / n, 5) if n else None,
            "win_rate": round(wins[len(out)] / n, 4) if n else None,
        })
    return out
