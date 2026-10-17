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

import math
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
#: 每个调仓点随响应返回的持仓名单上限。等权全市场模式下名单有几千只，
#: 全量塞进响应会把 payload 撑到几百 KB，而复盘想看的是「分数最高的那批」。
HOLDINGS_TOP = 50

#: 信号源：单因子 RPS / 多因子等权合成（方向取自滚动训练窗的 IC 符号）。
#: **单一真源**：回测、交易计划、模拟盘三处都引用它，避免各自抄一份字符串元组。
SIGNALS = ("rps", "composite")
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


def _rps_dates(conn: sqlite3.Connection, lo, hi) -> set[str]:
    """区间内**产出过 RPS** 的交易日（`YYYY-MM-DD` 字符串集合）。

    回测的调仓日以它为准：`daily_bar` 里还有横截面不足 1,000 只股票的稀疏日期
    （长期停牌股的历史延伸），那些日子没有信号，不该出现在窗口里。
    """
    rows = conn.execute(
        "SELECT DISTINCT date FROM rps_daily WHERE date BETWEEN ? AND ?",
        (lo.strftime("%Y-%m-%d"), hi.strftime("%Y-%m-%d")),
    ).fetchall()
    return {r["date"] for r in rows}


@dataclass
class BacktestResult:
    equity_curve: list[dict] = field(default_factory=list)
    #: 逐日盯市净值（调仓点之间的中间交易日也有一条），用于看**真实回撤路径**。
    #: 调仓点净值只在每期结束时取值，期内的下跌看不见，回撤因此被低估。
    daily_curve: list[dict] = field(default_factory=list)
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


def _ctx_key(conn: sqlite3.Connection, lookback_days: int | None, with_ohlcv: bool = False):
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
    # with_ohlcv **必须进 key**：不然先用不带 OHLCV 的 ctx 建过缓存、再用
    # with_ohlcv=True 取，会拿回没有 high/low/amount 的那份，合成因子随即 KeyError
    return (path, mtime, latest, lookback_days, bool(with_ohlcv))


def load_context(
    conn: sqlite3.Connection, lookback_days: int | None = None, *,
    with_ohlcv: bool = False,
) -> dict:
    """取回测需要的全部矩阵（带进程内缓存）。

    `with_ohlcv=True` 时额外带上 `high/low/volume/amount`（因子库用）：
    多读四列会让 660 万行的读取慢一截，只有真需要时才要。
    """
    key = _ctx_key(conn, lookback_days, with_ohlcv)
    now = time.monotonic()
    if _ctx_cache["key"] == key and now - _ctx_cache["at"] < _CTX_TTL:
        return _ctx_cache["data"]

    start = _lookback_start(conn, lookback_days)
    long = matrix.load_long(
        conn, start, extra_columns=("high", "low", "volume", "amount") if with_ohlcv else (),
    )
    close = matrix.build_close_matrix(long, matrix.negative_offsets(conn))
    data = {
        "start": start,
        # 负偏移股票（5 只）必须还原后再算比值，否则它们的区间收益整段失真
        "close": close,
        "tradable": matrix.build_tradable_matrix(long),
        "st_flags": matrix.build_st_flags(long),
        "pct": matrix.build_pctchg_matrix(long),
    }
    if with_ohlcv:
        # 一律 reindex 到 close 的 shape：pivot_table 会丢掉整列为空的那一列，
        # 与 close 的列对不齐时后续的 (high - low) / close 会静默错位
        for name, kw in (("high", {"ffill": True}), ("low", {"ffill": True}),
                         ("volume", {"fill": 0.0}), ("amount", {"fill": 0.0})):
            data[name] = matrix.build_value_matrix(long, name, **kw).reindex(
                index=close.index, columns=close.columns
            )
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


@dataclass(frozen=True)
class EntryMask:
    """买入候选掩码及其附属信息（回测与交易计划共用）。"""

    #: date × symbol 的布尔矩阵，True = 当日该股入选买入候选
    mask: pd.DataFrame
    #: 涨跌停覆盖率等**面向用户**的提示（回测直接并入 warnings）
    warnings: list[str]
    #: 产出过 RPS 的交易日；调仓日的口径来源（空集表示未取）
    sig_dates: set[str]
    #: 买入日涨停被剔除的格数；None = 本次没有应用该过滤
    limit_up_filtered: int | None
    #: 跌停掩码，卖出受阻判定用（None = 没有涨跌停数据）
    lim_dn: pd.DataFrame | None


def entry_mask(
    ctx: dict,
    conn: sqlite3.Connection,
    period: int,
    threshold: float,
    *,
    exclude_st: bool = True,
    resonance: bool = False,
    use_price_limits: bool = True,
    exclude_limit_up: bool = True,
    score: pd.DataFrame | None = None,
) -> EntryMask:
    """买入候选掩码：RPS 阈值 → ST 过滤 → 涨停过滤 → 可交易性。

    回测与交易计划（broker/plan.py）**共用本函数**，保证「回测能买的」与
    「计划允许买的」是同一口径 —— 两边各写一套时，最先出问题的就是
    「回测显示买得进、计划却空手」这类无法排查的偏差。

    `score` 给出时用它取代 `rps{period}` 作阈值判据（多因子合成分的百分位版本），
    其后 ST / 涨停 / 可交易性三段硬约束**一字不变** —— 换的只是「谁算高分」，
    不换「什么不能买」。
    """
    close_wide = ctx["close"]
    tradable = ctx["tradable"]
    st_flags = ctx["st_flags"]
    warnings: list[str] = []

    frames = get_frames(ctx, exclude_st)
    if score is not None:
        mask = score.reindex_like(frames[f"rps{period}"]) >= threshold
    elif resonance:
        mask = resonance_mask(frames, period, threshold)
    else:
        mask = frames[f"rps{period}"] >= threshold

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
    limit_up_filtered: int | None = None
    #: 产出过 RPS 的交易日（信号日）：覆盖率分母与调仓日都用它，
    #: 口径必须一致（见下方 coverage 与 dates 两处使用）
    sig_dates: set[str] = set()
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
        # 调仓日与覆盖率的分母都只用**产出过 RPS 的交易日**：daily_bar 里还有
        # 横截面不足 config.MIN_CROSS_SECTION 的稀疏日期（长期停牌股的历史延伸），
        # 那些日子没有信号、现在也不参与调仓 —— 若仍把它们算进分母，
        # 警告里的覆盖率会从 50% 被稀释成 37%，与真实可交易区间对不上。
        sig_dates = _rps_dates(conn, close_wide.index[0], close_wide.index[-1])
        if sig_dates:
            dates = [d for d in dates if d in sig_dates]
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
                warnings.append(
                    f"涨跌停：实测覆盖 {covered}/{total} 个交易日，其余 {total - covered} 天"
                    f"按板块规则推算（与已采集的 {MODEL_VALIDATION_DAYS} 个交易日 / "
                    f"{MODEL_VALIDATION_ROWS:,} 行逐行一致率 100%；新股前 5 日按"
                    f"「无涨跌幅限制」处理）。用 `cli price-limits --fill --workers 6` 可继续补实测"
                )
        else:
            # 一行实测都没有：整段用规则推算，并明确说出来（可校验性差一档）
            ratios, statuses = derive_ratio_frames(conn, dates, cols)
            warnings.append(
                "price_limit_daily 为空：涨跌停按板块规则推算（无实测数据可校验），"
                "请执行 cli price-limits 采集实测值"
            )
        if not ratios.empty:
            lim_up, lim_dn = limit_mask(close_wide, ratios, statuses)
            if exclude_limit_up:
                mask &= ~lim_up.reindex(index=mask.index, columns=mask.columns) \
                    .fillna(False).astype(bool)
                limit_up_filtered = int(lim_up.to_numpy().sum())
        else:
            warnings.append(
                "涨跌停比例为空（推算也失败），未应用「剔除涨停」过滤"
            )

    if tradable is not None and not tradable.empty:
        mask &= tradable.reindex_like(mask).fillna(False).astype(bool)

    return EntryMask(
        mask=mask, warnings=warnings, sig_dates=sig_dates,
        limit_up_filtered=limit_up_filtered, lim_dn=lim_dn,
    )


def _select_names(
    mask: pd.DataFrame, score: pd.DataFrame, t, select_top: int | None
) -> pd.Index:
    """当日入池的股票。`select_top=None` 时是「掩码内全选」，否则按 score 取前 N。

    并列时按 symbol 升序（`sort_values(kind="mergesort")` 是稳定排序，输入按
    symbol 排好即可）：否则同一份数据两次回测会抽出不同的票，结论不可复现。

    `score` 为 NaN 的票在 Top-N 模式下被**排除**（排不出名次就不该占名额），
    非 Top-N 模式下保持原样入选。
    """
    sel = mask.loc[t].fillna(False).astype(bool)
    names = pd.Index(sorted(sel.index[sel]))
    if select_top is None or len(names) <= select_top:
        return names
    s = score.loc[t].reindex(names).dropna()
    if s.empty:
        return names[:0]
    return s.sort_values(ascending=False, kind="mergesort").index[:select_top]


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
    signal: str = "rps",
    select_top: int | None = None,
) -> BacktestResult:
    """执行回测。返回结构化结果（含幸存者偏差警示）。

    lookback_days 默认 None（**全部历史**）：这是本项目对外公布回测结论时的口径，
    改成「近 3 年」会让净值曲线多出一段 2023-10~2024-01 的下跌期，
    把总收益从 +43% 压到 +0.9%，属于静默改结论，因此不作为默认。
    矩阵通过 load_context() 做进程内缓存，重复调参不再重复读库。

    `signal="composite"` 换用多因子合成分作信号（方向取自滚动训练窗 IC 符号）；
    `select_top=N` 只保留打分最高的 N 只（None = 掩码内全选，旧行为）。
    """
    # 入口兜底：传进来的 period 不在周期真源里会让 frames[f"rps{period}"] KeyError，
    # hold 为 0/负数会让 fwd = close.shift(-hold) 变成「未来负数天」的假收益。
    if period not in config.PERIODS:
        period = config.DEFAULT_PERIOD
    hold = max(1, min(int(hold), 250))
    if market_filter not in MARKET_FILTERS:
        market_filter = "none"
    # 与 market_filter 同款兜底：API 层已白名单过滤，这里不抛异常
    if signal not in SIGNALS:
        signal = "rps"
    if select_top is not None:
        select_top = max(1, min(int(select_top), 500))
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
            "signal": signal, "select_top": select_top,
        }
    )
    result.warnings.append(
        "本回测使用当前证券池回溯历史，中途退市的股票缺失，存在幸存者偏差，"
        "结果偏乐观，不可直接用于实盘决策。"
    )

    if not db.table_has_rows(conn, "rps_daily"):
        result.warnings.append("rps_daily 为空，请先执行 compute-rps")
        return result

    ctx = load_context(conn, lookback_days, with_ohlcv=(signal == "composite"))
    close_wide = ctx["close"]
    # 下面净值循环与分档表还要用到这两样；get_frames 缓存在 ctx 上，重取不花钱
    tradable = ctx["tradable"]
    rps_main = get_frames(ctx, exclude_st)[f"rps{period}"]

    # 前瞻收益先算：合成因子的滚动 IC 就建立在它上面（方向 = 训练窗里的 IC 符号）
    fwd = _forward_return(close_wide, hold)

    score_pct = None
    if signal == "composite":
        from . import factors as fac

        score_pct = fac.composite_score(
            ctx, conn, period=period, hold=hold, exclude_st=exclude_st
        )
        if int(score_pct.notna().to_numpy().sum()) == 0:
            result.warnings.append(
                f"合成分全部为 NaN：滚动训练窗（{fac.DEFAULT_TRAIN_DAYS} 个交易日）"
                "不足，请增大 lookback_days 或减小 factors.DEFAULT_TRAIN_DAYS"
            )
        result.params["train_days"] = fac.DEFAULT_TRAIN_DAYS

    # RPS 模式下的排序键是 rps{period} 本身（不是 ret{period}），
    # 这样 --select-top 20 单独用也能表达「RPS Top-20」
    score_for_rank = score_pct if score_pct is not None else rps_main

    entry = entry_mask(
        ctx, conn, period, threshold,
        exclude_st=exclude_st, resonance=resonance,
        use_price_limits=use_price_limits, exclude_limit_up=exclude_limit_up,
        score=score_pct,
    )
    result.warnings.extend(entry.warnings)
    if entry.limit_up_filtered is not None:
        result.params["limit_up_filtered"] = entry.limit_up_filtered
    mask = entry.mask
    lim_dn = entry.lim_dn
    sig_dates = entry.sig_dates

    fwd = fwd.reindex(index=mask.index, columns=mask.columns)

    dates = list(mask.index)
    # 调仓日同样只取信号日：矩阵里还包含只有历史延伸的稀疏日期（2018~2021，
    # 横截面不足 config.MIN_CROSS_SECTION，RPS 不落库）。在那些日子调仓等于
    # 按空信号调仓，却把回测窗口拉长到 7.4 年、覆盖率分母撑到 1,734 天 ——
    # 看板上的「窗口 / 覆盖率」必须对应真实可交易区间。
    if dates:
        if not sig_dates:
            sig_dates = _rps_dates(conn, dates[0], dates[-1])
        dates = [d for d in dates if d.strftime("%Y-%m-%d") in sig_dates]
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
    daily: list[dict] = []
    # 矩阵的完整交易日历：逐日盯市要按它走（`dates` 已被滤成信号日）
    all_dates = list(mask.index)
    _all_pos = {d: i for i, d in enumerate(all_dates)}
    total_deferred = 0
    holdings_used: list[int] = []      # 供成本口径说明算真实持仓中位数

    def _flat_daily(window: list, include_start: bool) -> None:
        """空仓/无持仓的窗口：逐日净值按现金计（平线）。

        必须补点，否则「这段时间在空仓」在逐日曲线上会变成一段缺失。
        窗口首日**只有第一个窗口才发** —— 它是上一个窗口的末日，重复发
        会让同一天出现两条互相矛盾的净值。
        """
        for d in (window if include_start else window[1:]):
            daily.append({"date": d.strftime("%Y-%m-%d"), "equity": round(equity, 6)})

    def _mark_daily(window: list, t, names: list, eq_end: float, entry_px,
                    include_start: bool) -> None:
        """把这一段持仓按**逐日**收盘价盯市，并整段**锚定**在该期期末净值上。

        `eq_end` 是该期公布在 `equity_curve` 上的净值。不能简单地在建仓值上乘
        `(1-cost)`：公布值走的是 `1 + 收益 - 成本`（加性），乘性写法会差一个
        `成本 × 收益` 的交叉项（实测 0.015%），两条线就会互相矛盾。
        这里反过来由期末值推出建仓值，两端都与公布口径严格相等。
        """
        ratios: dict = {}
        for d in window:
            if d == t:
                ratios[d] = None
                continue
            r = close_wide.loc[d, names] / entry_px
            ratios[d] = float(r.mean()) if r.notna().any() else 1.0
        end_ratio = next((v for v in reversed(list(ratios.values())) if v), 1.0)
        base = eq_end / end_ratio if end_ratio else eq_end
        for d in (window if include_start else window[1:]):
            v = ratios[d]
            daily.append({"date": d.strftime("%Y-%m-%d"),
                          "equity": round(base if v is None else base * v, 6)})

    for idx, t in enumerate(dates):
        if idx % step:
            continue
        # 窗口必须按**矩阵的交易日历**取（不是上面那份滤过信号日的 `dates`）：
        # 两者在有缺失信号日的区间里会错位，导致期末净值与 `fwd` 的
        # t+hold 对不上（实测差 0.015%）。首日只在第一个窗口发：它是上一段的末日。
        include_start = idx == 0
        p0 = _all_pos.get(t, 0)
        window = all_dates[p0:min(p0 + step, len(all_dates) - 1) + 1]
        if not bool(mkt_ok.get(t, True)):
            # 空仓：净值按现金计（不变），但**必须保留这个点**，
            # 否则曲线上看不出「这段时间在空仓」，也无法画开/空仓区间。
            curve.append({"date": t.strftime("%Y-%m-%d"),
                          "equity": round(equity, 6),
                          "holdings": 0})
            _flat_daily(window, include_start)
            continue
        names = _select_names(mask, score_for_rank, t, select_top)
        if len(names) == 0:
            _flat_daily(window, include_start)
            continue

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
            _flat_daily(window, include_start)
            continue

        period_ret = fwd.loc[t, names].mean()      # 单个持有期收益
        if pd.isna(period_ret):
            _flat_daily(window, include_start)
            continue
        # 成本按「组合名义金额」计，与实际持仓只数有关：等权分散持有上千只时，
        # 券商的 5 元最低佣金才是真正生效的那一项。
        cost = cost_model.round_trip_cost(t, n_positions=len(names))
        period_ret = period_ret - cost
        equity *= float(1.0 + period_ret)
        holdings_used.append(int(len(names)))
        # 逐日盯市：整段锚定在该期期末净值上（建仓值由期末值反推）
        _mark_daily(window, t, names, equity,
                    close_wide.loc[t, names], include_start)
        curve.append({"date": t.strftime("%Y-%m-%d"),
                      "equity": round(equity, 6),
                      "holdings": int(len(names)),
                      # 每期买入名单（前 50 只，已按排序键降序）：复盘要能回答
                      # 「那天到底买了哪几只」，全量名单在等权全市场模式下有几千只，
                      # 塞进响应会把 payload 撑到几百 KB。
                      "holdings_top": list(names[:HOLDINGS_TOP]),
                      "deferred": deferred})
    result.equity_curve = curve
    result.daily_curve = daily
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
    # 主口径统一到**逐日**净值：调仓点净值看不见期内的下跌，回撤会被低估
    # （实测同一段行情两个数字能差几个百分点）。调仓点口径另存一份，
    # 保证 README 里已公布过的数字仍然查得到。
    rebalance_eq = pd.Series([c["equity"] for c in result.equity_curve],
                             dtype="float64")
    rebalance = compute_metrics(
        rebalance_eq,
        n_days_per_point=step,
        dates=[c["date"] for c in result.equity_curve],
    )
    if len(result.daily_curve) > 2:
        result.metrics = compute_metrics_from_points(
            [c["equity"] for c in result.daily_curve],
            [c["date"] for c in result.daily_curve])
        result.metrics["max_drawdown_daily"] = result.metrics["max_drawdown"]
    else:
        result.metrics = dict(rebalance)
        result.metrics["max_drawdown_daily"] = result.metrics["max_drawdown"]
    result.metrics["max_drawdown_rebalance"] = rebalance["max_drawdown"]
    result.metrics["annualized_rebalance"] = rebalance["annualized"]

    # ── 分档前瞻收益（核心验证图）───────────────────────────────
    # 刻意**不扣成本**：这张图衡量的是「信号本身有没有预测力」，
    # 混进执行摩擦会把信号质量与交易成本两件事搅在一起。
    result.buckets = compute_buckets(score_for_rank, fwd)
    result.buckets_note = (
        "分档前瞻收益为**零成本**口径，分档依据是**合成分的横截面百分位**；"
        "上方净值曲线为**含成本**口径。"
        if signal == "composite" else
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

    波动与夏普**按序列自身的单期收益**算，再乘 `sqrt(252 / n_days_per_point)`
    折算到年 —— 这样同一套公式同时适用于「逐日净值」（n=1）与「调仓点净值」
    （n=hold），模拟盘与回测不再各写一份、各得一个数。

    `dates` 给出每个净值点的真实日期时**优先按首末日期跨度折算年数**：
    被跳过的调仓点会让 `点数 × 调仓周期` 系统性低估已用时长、抬高年化。
    """
    if equity.empty or len(equity) < 2:
        return {"total_return": 0.0, "annualized": None, "volatility": None,
                "max_drawdown": 0.0, "max_drawdown_daily": 0.0,
                "sharpe": None, "calmar": None, "days": int(len(equity)),
                "years": 0.0, "metrics_basis": "daily-v1"}
    rets = equity.pct_change().dropna()
    total = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    n_pts = len(equity)
    if dates is not None and len(dates) == n_pts:
        span_days = (pd.Timestamp(dates[-1]) - pd.Timestamp(dates[0])).days
        years = max(1e-9, span_days / 365.25)
    else:
        years = max(1e-9, (n_pts - 1) * max(1, n_days_per_point) / 252.0)

    ann = (1.0 + total) ** (1.0 / years) - 1.0 if total > -1 else -1.0
    scale = math.sqrt(252.0 / max(1, n_days_per_point))
    if len(rets) > 1:
        sd = float(rets.std(ddof=1))
        vol = sd * scale
    else:
        sd, vol = 0.0, 0.0
    dd = equity / equity.cummax() - 1.0
    mdd = float(dd.min())
    # 样本不足（<2 个收益）或零方差时不编造一个 0 当成「无风险」
    sharpe = (float(rets.mean()) / sd * scale) if (len(rets) > 1 and sd > 0) else None
    return {
        "total_return": round(total, 4),
        "annualized": round(float(ann), 4) if len(rets) > 1 else None,
        "volatility": round(vol, 4) if len(rets) > 1 else None,
        "max_drawdown": round(mdd, 4),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "calmar": round(float(ann) / abs(mdd), 2) if mdd < 0 else None,
        "days": n_pts,
        "years": round(years, 3),
        # 口径标记：旧存档没有这个键，页面据此显示「旧口径」，不与新数混比。
        "metrics_basis": "daily-v1",
    }


def compute_metrics_from_points(equity: list[float],
                                dates: list[str]) -> dict:
    """逐日净值点 → 指标（模拟盘与回测统一入口）。

    曲线本身就是逐日的（每个结算交易日一行），所以 `n_days_per_point=1`。
    """
    return compute_metrics(pd.Series(equity, dtype="float64"),
                           n_days_per_point=1, dates=list(dates))


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
