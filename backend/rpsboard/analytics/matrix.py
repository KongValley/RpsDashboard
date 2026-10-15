"""从 daily_bar 构建「日期 × 股票」宽矩阵。

RPS 本质是横截面统计，需要的是宽表而非长表。这里把 SQLite 里的长表
一次读进内存转成宽表，供 RPS 与回测共用。
"""

from __future__ import annotations

import sqlite3

import pandas as pd

from .. import config

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


#: `load_long` 默认读出的列（其余调用方都靠这几列建矩阵）
BASE_COLUMNS: tuple[str, ...] = ("symbol", "date", "close", "pct_chg", "tradestatus", "is_st")
#: `load_long(extra_columns=…)` 的列白名单。列名要拼进 SQL，因此**不接受任意字符串**
#: （注入面），也不接受 close/pct_chg 这类已在 BASE_COLUMNS 里的列。
EXTRA_COLUMNS: tuple[str, ...] = ("open", "high", "low", "volume", "amount")


def load_long(
    conn: sqlite3.Connection,
    start: str | None = None,
    *,
    seed_before_start: bool = False,
    extra_columns: tuple[str, ...] = (),
) -> pd.DataFrame:
    """读取日 K 长表（可选按起始日期裁剪）。

    注意列名是 daily_bar 的**内部命名** pct_chg（数据源侧的原始字段名不同，
    在 sources/tdxnet_src.py 的入库转换处已统一为这套内部命名）。

    `extra_columns` 是 `EXTRA_COLUMNS` 的**子集**（默认空 = 逐字保持旧行为），
    供因子库拿 OHLCV —— 多读四列会让 660 万行的读取慢一点，只有真需要时才要。

    `seed_before_start=True` 时额外带上「每个 symbol 在 start 之前的最后一根 bar」：
    增量重算只取窗口内的行，`build_close_matrix` 的 ffill 在窗口起点没有种子，
    「窗口第一天没有 bar（停牌）」的股票会被误判成不可交易 → 不参与排名。
    种子行不属于调用方声明的 target_dates，落库时会被增量过滤掉。
    """
    bad = [c for c in extra_columns if c not in EXTRA_COLUMNS]
    if bad:
        raise ValueError(f"不支持的列: {', '.join(bad)}（可用 {list(EXTRA_COLUMNS)}）")
    cols = ", ".join([*BASE_COLUMNS, *extra_columns])
    sql = f"SELECT {cols} FROM daily_bar"
    params: tuple = ()
    if start:
        sql += " WHERE date >= ?"
        params = (start,)
        if _prefer_seq_scan(conn, start):
            # NOT INDEXED 只能放在 FROM 表名与 WHERE 之间
            sql = f"SELECT {cols} FROM daily_bar NOT INDEXED WHERE date >= ?"
    out = pd.read_sql_query(sql, conn, params=params)
    if seed_before_start and start:
        # 每个 symbol 在 start 之前的最后一根 bar（一条相关子查询扫完，走主键索引）
        seed = pd.read_sql_query(
            f"SELECT {cols} "
            "FROM daily_bar d WHERE d.date < ? AND d.date = ("
            "  SELECT MAX(e.date) FROM daily_bar e "
            "  WHERE e.symbol = d.symbol AND e.date < ?)",
            conn, params=(start, start),
        )
        if not seed.empty:
            out = pd.concat([seed, out], ignore_index=True)
    return out


def negative_offsets(conn: sqlite3.Connection) -> dict[str, float]:
    """负偏移档位的股票 → 该档位的 |offset|（`adjust_event.ratio` 存的就是 offset）。

    后复权价 = 原始价 × scale + offset，offset < 0 时原始价接近变换零点
    （`-offset/scale`）会让 `close(t)/close(t−N)` 的比值被严重放大。
    **还原不需要 scale**：(p+r)/(q+r) 里 scale 被约掉，只要给整列加 |offset|，
    得到的序列就与原始价成比例、比值正确。

    同一只股票有多档时取**最新**一档：库内序列由当前因子档位算出来（每天全量重取），
    旧档位的偏移用不上。
    """
    try:
        rows = conn.execute(
            "SELECT a.symbol AS symbol, -a.ratio AS off FROM adjust_event a "
            "WHERE a.kind = 'negative_offset' AND a.ratio < 0 AND a.date = ("
            "  SELECT MAX(b.date) FROM adjust_event b "
            "  WHERE b.symbol = a.symbol AND b.kind = 'negative_offset')"
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {r["symbol"]: float(r["off"]) for r in rows}


def build_close_matrix(
    long: pd.DataFrame, offsets: dict[str, float] | None = None
) -> pd.DataFrame:
    """构建后复权收盘价宽矩阵（index=date, columns=symbol）。

    停牌日用前值填充（ffill）——停牌期间价格不变是合理的近似，
    否则整只股票的区间收益会被 NaN 污染成缺失。

    **负偏移还原**：`offsets`（见 `negative_offsets`）里列出的股票先整列加上
    |offset|。实测 5 只（000793/600076/600136/600180/600734）的收益**几乎整段失真**
    （每周期 900~1,270 行偏差 >20pp，最大 1,640pp），且它们能拿到 `rps=100`
    被回测买入 —— 加回偏移后比值口径就是原始价的真实涨幅。

    **端点剔除**：低于 `config.ANOMALY_MIN_PRICE`（默认 1 元）的价视为无效，
    连同非正价一起置为 NaN。机理与上面同一个：原始价接近变换零点时比值被放大到
    失真（实测 600076 的 `ret60 = +998%`，当天原始价只涨了约 8%）。
    区间收益只取决于两个端点，所以按端点剔除而不是「跨过异常日整段作废」；
    同一条规则同时覆盖 RPS、回测的前瞻收益与涨跌停判定的 `pct_change`。
    """
    if long.empty:
        return pd.DataFrame()
    df = long.copy()
    df["date"] = pd.to_datetime(df["date"])
    wide = df.pivot_table(index="date", columns="symbol", values="close", aggfunc="last")
    wide = wide.sort_index().ffill()
    if offsets:
        for sym, off in offsets.items():
            if sym in wide.columns:
                wide[sym] = wide[sym] + float(off)
    floor = max(config.ANOMALY_MIN_PRICE, 0.0)
    # 放在 ffill 之后：停牌日沿用前值，前值若已被剔除，停牌期间也不该「复活」
    return wide.where(wide >= floor) if floor > 0 else wide.where(wide > 0)


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


def build_value_matrix(
    long: pd.DataFrame, col: str, *, ffill: bool = False, fill: float | None = None
) -> pd.DataFrame:
    """`col` 列的 date × symbol 宽矩阵（因子库的 OHLCV 输入）。

    `ffill=True` 用于价格类（high/low）：停牌期间价格不变，与 `build_close_matrix`
    同口径。`volume`/`amount` 用 `fill=0.0` —— 停牌当日**没有成交额**，
    沿用前值是错的（会把停牌前的巨量一直算成「当前量能」）。

    **刻意不复用 `build_close_matrix`**：它内含负偏移还原与 `ANOMALY_MIN_PRICE`
    端点剔除两条口径，改它会动到 RPS 与回测的既有结论。
    """
    if long.empty or col not in long.columns:
        return pd.DataFrame()
    df = long[["date", "symbol", col]].copy()
    df["date"] = pd.to_datetime(df["date"])
    wide = df.pivot_table(index="date", columns="symbol", values=col, aggfunc="last")
    wide = wide.sort_index()
    if ffill:
        wide = wide.ffill()
    if fill is not None:
        wide = wide.fillna(fill)
    return wide


def load_industry_map(conn: sqlite3.Connection) -> pd.Series:
    """symbol → industry 的映射。"""
    try:
        df = pd.read_sql_query("SELECT symbol, industry FROM industry_map", conn)
    except sqlite3.Error:
        return pd.Series(dtype="object")
    if df.empty:
        return pd.Series(dtype="object")
    return df.set_index("symbol")["industry"]
