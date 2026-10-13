"""涨跌停判定。

**核心口径：必须比「收益率」，不能比「价格」。**

`daily_bar.close` 存的是**后复权**收盘价，而 eltdx 返回的 `limit_up_price`
是基于**不复权**前收盘价算出来的。两者不在同一个价格基准上，直接比会得到
荒谬的结果——实测当日会算出 4303 家「涨停」（占全市场 77%）。

收益**比率**与复权基准无关，所以正确做法是：

    ret(t)      = close(t) / close(t-1) - 1
    is_limit_up = ret(t) >=  limit_ratio(t)/100 - TOL
    is_limit_dn = ret(t) <= -limit_ratio(t)/100 + TOL

实测同一交易日该法得到 **7 涨停 / 14 跌停**（占 5559 只的 0.38%），
与 A 股实况吻合。

容差 TOL 的必要性：交易所的涨跌停价按四舍五入保留两位小数，
`10%` 的边界值与后复权收益比率之间会有万级分位点数的偏差，
不留容差会把「准涨停」误判成涨停。
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

#: 判定容差（百分点）。用于吸收交易所价位四舍五入与后复权比率的偏差。
TOLERANCE_PCT = 0.2

#: 这些状态表示「当日没有涨跌停约束」，**不能**当成涨停或跌停
NO_LIMIT_STATUSES = ("no_price_limit", "missing_pre_close")


def limit_mask(
    close_wide: pd.DataFrame,
    ratios: pd.DataFrame,
    status: pd.DataFrame | None = None,
    tolerance_pct: float = TOLERANCE_PCT,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (涨停掩码, 跌停掩码)，均为 date × symbol 的布尔矩阵。

    close_wide : date × symbol 后复权收盘价（已 ffill）
    ratios     : date × symbol 当日 limit_ratio（百分数，如 10.0）
    status     : date × symbol 状态文本；None 表示不区分（全部按正常处理）

    没有涨跌停约束（新股前 5 日 / 缺前收盘）的位置一律返回 False，
    既不算涨停也不算跌停——把它们算成涨跌停会凭空制造一批「买不进」的标的。
    """
    if close_wide.empty or ratios.empty:
        empty = pd.DataFrame(False, index=close_wide.index, columns=close_wide.columns)
        return empty, empty.copy()

    r = ratios.reindex(index=close_wide.index, columns=close_wide.columns)
    ret = close_wide.pct_change() * 100.0            # 百分数

    ratio_pct = r / 100.0 * 100.0                    # 归一到百分数刻度
    tol = tolerance_pct

    valid = r.notna() & (r > 0)
    if status is not None and not status.empty:
        st = status.reindex(index=close_wide.index, columns=close_wide.columns)
        bad = st.isin(list(NO_LIMIT_STATUSES))
        valid = valid & ~bad.fillna(False)

    up = valid & (ret >= ratio_pct - tol)
    dn = valid & (ret <= -ratio_pct + tol)
    return up.fillna(False), dn.fillna(False)


def load_ratio_frames(
    conn: sqlite3.Connection, dates: list[str], columns: list[str]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """从 price_limit_daily 读出指定日期范围的 limit_ratio / status 宽表。"""
    if not dates:
        empty = pd.DataFrame(columns=columns, index=pd.to_datetime(dates))
        return empty, empty.copy()
    lo, hi = min(dates), max(dates)
    df = pd.read_sql_query(
        "SELECT date, symbol, limit_ratio, status FROM price_limit_daily "
        "WHERE date >= ? AND date <= ?",
        conn, params=(lo, hi),
    )
    idx = pd.to_datetime(dates)
    if df.empty:
        empty = pd.DataFrame(index=idx, columns=columns, dtype="float64")
        return empty, empty.copy()
    df["date"] = pd.to_datetime(df["date"])
    ratio = df.pivot(index="date", columns="symbol", values="limit_ratio")
    stat = df.pivot(index="date", columns="symbol", values="status")
    return (
        ratio.reindex(index=idx, columns=columns),
        stat.reindex(index=idx, columns=columns),
    )


def summarize(up: pd.DataFrame, dn: pd.DataFrame) -> dict:
    """统计区间内的涨停/跌停家数，用于回测告警与验证脚本。"""
    return {
        "limit_up_total": int(up.to_numpy().sum()),
        "limit_down_total": int(dn.to_numpy().sum()),
        "dates": int(len(up)),
        "max_limit_up_per_day": int(up.to_numpy().sum(axis=1).max()) if len(up) else 0,
    }


def cost_of_blocked_exit(up: pd.DataFrame, dn: pd.DataFrame) -> pd.Series:
    """返回每个日期「买入即被涨停挡住」的股票数（date → 数量）。"""
    if up.empty:
        return pd.Series(dtype="int64")
    return up.sum(axis=1).astype("int64")


def _ratio_to_series(ratios: pd.DataFrame) -> np.ndarray:
    """测试辅助：把 ratio 矩阵转成 numpy，便于构造预期值。"""
    return ratios.to_numpy(dtype="float64")
