"""从 daily_bar 构建「日期 × 股票」宽矩阵。

RPS 本质是横截面统计，需要的是宽表而非长表。这里把 SQLite 里的长表
一次读进内存转成宽表，供 RPS 与回测共用。
"""

from __future__ import annotations

import sqlite3

import pandas as pd

#: 窗口占比超过这个比例时改走顺序扫描（见 load_long 的说明）
SEQ_SCAN_RATIO = 0.2


def _prefer_seq_scan(conn: sqlite3.Connection, start: str) -> bool:
    """窗口较大时是否该绕开 date 索引。

    daily_bar 的主键是 (symbol, date)，物理顺序按 symbol 聚簇；
    `idx_bar_date` 只索引 date。带 `WHERE date >= ?` 时 SQLite 会先用日期索引定位，
    再对**每一行**回主键/表取数据 —— 143 万行就是 143 万次随机 I/O。
    实测本机 427 万行库：
        带索引      13.09s / 1,439,082 行
        NOT INDEXED  3.46s / 1,439,082 行     ← 顺序扫描快 3.8 倍

    但窗口很小时（比如只想看最后一天）顺序扫描要读完整个表，反而更慢，
    因此按「窗口内交易日数 / 全库交易日数」判断。两个 COUNT 都走覆盖索引，
    只扫约 900 个不同的 date，耗时可忽略。
    """
    try:
        row = conn.execute(
            "SELECT (SELECT COUNT(DISTINCT date) FROM daily_bar WHERE date >= ?) AS win, "
            "       (SELECT COUNT(DISTINCT date) FROM daily_bar) AS total"
        , (start,)).fetchone()
        if not row or not row["total"]:
            return False
        return (row["win"] / row["total"]) >= SEQ_SCAN_RATIO
    except sqlite3.Error:
        return False


def load_long(
    conn: sqlite3.Connection,
    start: str | None = None,
    *,
    seed_before_start: bool = False,
) -> pd.DataFrame:
    """读取日 K 长表（可选按起始日期裁剪）。

    注意列名是 daily_bar 的**内部命名** pct_chg（数据源侧的原始字段名不同，
    在 sources/tdxnet_src.py 的入库转换处已统一为这套内部命名）。

    `seed_before_start=True` 时额外带上「每个 symbol 在 start 之前的最后一根 bar」：
    增量重算只取窗口内的行，`build_close_matrix` 的 ffill 在窗口起点没有种子，
    「窗口第一天没有 bar（停牌）」的股票会被误判成不可交易 → 不参与排名。
    种子行不属于调用方声明的 target_dates，落库时会被增量过滤掉。
    """
    sql = ("SELECT symbol, date, close, pct_chg, tradestatus, is_st "
           "FROM daily_bar")
    params: tuple = ()
    if start:
        sql += " WHERE date >= ?"
        params = (start,)
        if _prefer_seq_scan(conn, start):
            # NOT INDEXED 只能放在 FROM 表名与 WHERE 之间
            sql = ("SELECT symbol, date, close, pct_chg, tradestatus, is_st "
                   "FROM daily_bar NOT INDEXED WHERE date >= ?")
    out = pd.read_sql_query(sql, conn, params=params)
    if seed_before_start and start:
        # 每个 symbol 在 start 之前的最后一根 bar（一条相关子查询扫完，走主键索引）
        seed = pd.read_sql_query(
            "SELECT symbol, date, close, pct_chg, tradestatus, is_st "
            "FROM daily_bar d WHERE d.date < ? AND d.date = ("
            "  SELECT MAX(e.date) FROM daily_bar e "
            "  WHERE e.symbol = d.symbol AND e.date < ?)",
            conn, params=(start, start),
        )
        if not seed.empty:
            out = pd.concat([seed, out], ignore_index=True)
    return out


def build_close_matrix(long: pd.DataFrame) -> pd.DataFrame:
    """构建后复权收盘价宽矩阵（index=date, columns=symbol）。

    停牌日用前值填充（ffill）——停牌期间价格不变是合理的近似，
    否则整只股票的区间收益会被 NaN 污染成缺失。
    """
    if long.empty:
        return pd.DataFrame()
    df = long.copy()
    df["date"] = pd.to_datetime(df["date"])
    wide = df.pivot_table(index="date", columns="symbol", values="close", aggfunc="last")
    return wide.sort_index().ffill()


def build_tradable_matrix(long: pd.DataFrame) -> pd.DataFrame:
    """可交易日掩码矩阵：True = 该股票当日正常交易。

    判据是「当天有 bar 且收盘价有效」——**不要再用 `tradestatus`**：那一列的含义是
    「该 bar 之前缺了交易日（停牌复牌当天）」（见 sources/base.py），复牌当天本身是
    真实成交日（实测 000582 2026-10-12 有成交量、+2.91%），把它剔出去会让龙头榜漏掉
    「复牌补涨当天」的股票（实测 1,299 个股票日）。
    """
    if long.empty:
        return pd.DataFrame()
    df = long.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["ok"] = pd.to_numeric(df["close"], errors="coerce") > 0
    m = df.pivot_table(index="date", columns="symbol", values="ok", aggfunc="last")
    return m.sort_index().fillna(False).astype(bool)


def build_st_flags(long: pd.DataFrame) -> pd.DataFrame:
    """ST 标记矩阵（True = 当日为 ST/*ST）。"""
    if long.empty:
        return pd.DataFrame()
    df = long.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["st"] = pd.to_numeric(df["is_st"], errors="coerce").fillna(0).astype(bool)
    return df.pivot_table(index="date", columns="symbol", values="st", aggfunc="last").sort_index()


def build_pctchg_matrix(long: pd.DataFrame) -> pd.DataFrame:
    """当日涨跌幅矩阵（回测过滤涨跌停用）。"""
    if long.empty:
        return pd.DataFrame()
    df = long.copy()
    df["date"] = pd.to_datetime(df["date"])
    col = "pct_chg" if "pct_chg" in df.columns else "pctChg"
    df["pct"] = pd.to_numeric(df[col], errors="coerce")
    return df.pivot_table(index="date", columns="symbol", values="pct", aggfunc="last").sort_index()


def load_industry_map(conn: sqlite3.Connection) -> pd.Series:
    """symbol → industry 的映射。"""
    try:
        df = pd.read_sql_query("SELECT symbol, industry FROM industry_map", conn)
    except sqlite3.Error:
        return pd.Series(dtype="object")
    if df.empty:
        return pd.Series(dtype="object")
    return df.set_index("symbol")["industry"]
