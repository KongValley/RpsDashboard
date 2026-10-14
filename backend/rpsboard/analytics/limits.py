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

import pandas as pd

#: 判定容差（百分点）。用于吸收交易所价位四舍五入与后复权比率的偏差。
TOLERANCE_PCT = 0.2

#: 这些状态表示「当日没有涨跌停约束」，**不能**当成涨停或跌停
NO_LIMIT_STATUSES = ("no_price_limit", "missing_pre_close")

#: 按板块规则推算涨跌停比例时的北交所代码段（含 920 新段）
BSE_PREFIXES = ("43", "83", "87", "88", "92")

#: 新股上市后**前若干个交易日**没有涨跌幅限制（创业板/科创板为前 5 日，主板也是 5 日）
IPO_NO_LIMIT_DAYS = 5

#: 规则推算与实测的逐行一致率（2026-10-14 用已采集的 135 个交易日校验）
MODEL_VALIDATION_DAYS = 135
MODEL_VALIDATION_ROWS = 743959


def board_ratio(symbol: str) -> float:
    """按板块规则给出涨跌停比例（百分数）。

    实测校验（135 个交易日、743,959 行 `price_limit_daily`，非新股前 5 日）：
    **逐行完全一致**（0 处不一致）。要点：
    - 主板（含 ST/*ST，2024 新规后 ST 也是 10%）→ 10%
    - 创业板 300/301/302、科创板 688 → 20%
    - 科创板 689（CDR）→ 10%（实测 135 行全部是 10%，不要与 688 合并）
    - 北交所 43/83/87/88/92 → 30%
    """
    if symbol.startswith("688"):
        return 20.0
    if symbol.startswith("689"):
        return 10.0
    if symbol.startswith(("300", "301", "302")):
        return 20.0
    if symbol[:2] in BSE_PREFIXES:
        return 30.0
    return 10.0


def first_bar_dates(conn: sqlite3.Connection, symbols: list[str]) -> dict[str, list[str]]:
    """每个 symbol 最早的 IPO_NO_LIMIT_DAYS 个交易日 → {symbol: [date, ...]}。

    逐只走 (symbol, date) 主键索引 LIMIT 5：本机 5580 只实测不到 1 秒，
    比对全表跑 ROW_NUMBER() 窗口函数（4.3M 行、数秒）划算。
    """
    out: dict[str, list[str]] = {}
    for sym in symbols:
        rows = conn.execute(
            "SELECT date FROM daily_bar WHERE symbol = ? ORDER BY date LIMIT ?",
            (sym, IPO_NO_LIMIT_DAYS),
        ).fetchall()
        if rows:
            out[sym] = [r["date"] for r in rows]
    return out


def derive_ratio_frames(
    conn: sqlite3.Connection, dates: list[str], columns: list[str]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """按板块规则推算 date × symbol 的 (limit_ratio, status) 宽表。

    用途：`price_limit_daily` 的实测覆盖不全（接口单日 30~240 秒、服务端不缓存，
    全历史要几小时），未覆盖的日期以前是「整段不筛涨停」——回测照跑、结论照给，
    用户无从知道过滤没生效。规则推算可以把这些日子补上，**并且有实测校验兜底**：
    见 board_ratio 的 docstring。

    比「编造数据」更保守的地方：新股前 5 日判为 `no_price_limit`（实测 508 行 100%
    命中），因此不会被算成涨停；推算出的比例只用于「收益率是否触达限制」的判断。
    """
    if not dates or not columns:
        empty = pd.DataFrame(index=pd.to_datetime(dates), columns=columns, dtype="float64")
        return empty, empty.copy()

    idx = pd.to_datetime(dates)
    ratios = pd.DataFrame(
        {sym: board_ratio(sym) for sym in columns},
        index=idx, columns=columns, dtype="float64",
    )
    status = pd.DataFrame("normal", index=idx, columns=columns, dtype="object")
    # 新股前 5 个交易日：无涨跌幅限制（与实测 status='no_price_limit' 同义）
    for sym, first in first_bar_dates(conn, list(columns)).items():
        cols = [d for d in first if d in set(dates)]
        if cols:
            status.loc[cols, sym] = "no_price_limit"
    return ratios, status


def merge_ratio_frames(
    measured: tuple[pd.DataFrame, pd.DataFrame],
    derived: tuple[pd.DataFrame, pd.DataFrame],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """实测优先、缺失处回落推算值（按位置对齐）。"""
    m_ratio, m_status = measured
    d_ratio, d_status = derived
    if m_ratio.empty:
        return d_ratio, d_status
    if d_ratio.empty:
        return m_ratio, m_status
    d_ratio = d_ratio.reindex(index=m_ratio.index, columns=m_ratio.columns)
    d_status = d_status.reindex_like(d_ratio)
    ratio = m_ratio.where(m_ratio.notna(), d_ratio)
    status = m_status.where(m_ratio.notna(), d_status)
    return ratio, status


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


def coverage(conn: sqlite3.Connection, dates: list[str]) -> tuple[int, int]:
    """回测区间内 price_limit_daily 有数据的交易日数 / 总交易日数。

    没有这组数字时，部分覆盖会被静默当成「这些天不筛涨停」——
    回测照跑、结论照给，用户无从知道过滤其实没生效。
    """
    if not dates:
        return 0, 0
    row = conn.execute(
        "SELECT COUNT(DISTINCT date) AS c FROM price_limit_daily "
        "WHERE date >= ? AND date <= ?",
        (min(dates), max(dates)),
    ).fetchone()
    covered = int(row["c"]) if row and row["c"] is not None else 0
    return covered, len(dates)


def summarize(up: pd.DataFrame, dn: pd.DataFrame) -> dict:
    """统计区间内的涨停/跌停家数，用于回测告警与验证脚本。"""
    return {
        "limit_up_total": int(up.to_numpy().sum()),
        "limit_down_total": int(dn.to_numpy().sum()),
        "dates": int(len(up)),
        "max_limit_up_per_day": int(up.to_numpy().sum(axis=1).max()) if len(up) else 0,
    }


