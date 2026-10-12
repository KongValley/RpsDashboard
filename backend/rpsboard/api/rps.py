"""RPS 接口：排行榜、板块排行、热力图。"""

from __future__ import annotations

import time

import pandas as pd
from fastapi import APIRouter, Query

from .. import config, db
from . import jsonable, latest_bar_date, nth_previous_date, read_sql, resolve_date

router = APIRouter(prefix="/api/rps", tags=["rps"])

#: 排序键 → 外层可用的**输出列名**（不是原始表列名）。
#: 必须在 SELECT 里已经有对应别名，外层 ORDER BY 才能引用。
_SORTS = {
    "rps": "rps",
    "ret": "period_return",
    "pct_chg": "pct_chg",
    "amount": "amount",
    "turnover_rate": "turnover_rate",
    "close": "close",
}

#: 排序方向为 ASC 的键
_ASC_SORTS = {"pct_chg", "close"}

#: 这些列来自 LEFT JOIN 的表，可能为 NULL，排序时需要把 NULL 沉底。
#: rps / period_return 不在其中——WHERE 里已经要求 `rps IS NOT NULL`，
#: 而 rps 是 ret 的百分位排名，rps 非空必然意味着 ret 非空。
_NULLABLE_SORTS = {"pct_chg", "amount", "turnover_rate", "close"}

#: spot_snapshot 是否真的有数据（空表时不必 LEFT JOIN 一张空表）
_spot_state: dict = {"at": 0.0, "ok": False}
_SPOT_TTL = 60.0


def spot_available(conn) -> bool:
    """快照表是否可用；结果缓存 60 秒。

    本机实测 spot_snapshot 长期为 0 行（akshare 被网络阻断，eltdx 的
    stock_profile_table 慢到不可用），此时排行榜里那几列恒为 NULL，
    每行 LEFT JOIN 一张空表纯属浪费。
    """
    now = time.monotonic()
    if now - _spot_state["at"] < _SPOT_TTL:
        return _spot_state["ok"]
    ok = False
    try:
        if db.table_exists(conn, "spot_snapshot"):
            row = conn.execute("SELECT MAX(date) AS d FROM spot_snapshot").fetchone()
            ok = bool(row and row["d"])
    except Exception:  # noqa: BLE001
        ok = False
    _spot_state.update(at=now, ok=ok)
    return ok


def market_ranks(
    conn, date: str, period: int, compare_date: str | None = None
) -> pd.DataFrame:
    """某交易日全市场的 RPS 名次（可选对比基准日），返回 symbol/rank_now/rank_prev/rank_delta。

    名次用 SQL 窗口函数 `RANK() OVER (ORDER BY rps DESC)` 在**全市场**上算。
    早期版本是在分页返回的那 100 行里 rank，标成 rank_overall —— 那其实是
    「本页内名次」，名次会随翻页变化，用来做排名变化更是完全错的。
    """
    col = f"rps{period}"
    sql = (
        f"SELECT r.symbol AS symbol, RANK() OVER (ORDER BY r.{col} DESC) AS rank_overall "
        f"FROM rps_daily r WHERE r.date = ? AND r.{col} IS NOT NULL"
    )
    df = read_sql(conn, sql, (date,))
    if compare_date:
        prev = read_sql(
            conn,
            f"SELECT r.symbol AS symbol, RANK() OVER (ORDER BY r.{col} DESC) AS rank_prev "
            f"FROM rps_daily r WHERE r.date = ? AND r.{col} IS NOT NULL",
            (compare_date,),
        )
        df = df.merge(prev, on="symbol", how="left")
        # 名次变小 = 上升，所以 Δ = 前 - 后；正数代表名次前进
        df["rank_delta"] = df["rank_prev"] - df["rank_overall"]
    return df


def _ranking_fast(
    conn, d: str, period: int, sort: str,
    rps_min: float, rps_max: float,
    exclude_st: bool, limit: int, page: int,
    with_spot: bool,
) -> tuple[pd.DataFrame, int]:
    """快路径：先在 rps_daily 上取 Top-N，再只对这 N 行做 JOIN。

    实测（本机 5580 只 / 427 万行日K）：
        原写法（先 JOIN 全部命中行再排序分页）  COUNT 92.7ms / 首页 57.1ms
        延迟连接（本写法）                      COUNT  0.7ms / 首页  7.3ms

    前提：**不能有需要 JOIN 之后才能判断的过滤条件**（行业、成交额）。
    有这类条件时必须走 ranking_filtered，否则 LIMIT 取到的行数会少于实际命中数。
    剔除 ST 可以留在内层——security_master 有覆盖索引，半连接很便宜。
    """
    rps_col = f"rps{period}"
    ret_col = f"ret{period}"
    # date 必须放在最前面并作为第一个参数 —— 漏掉它会返回**全历史**的行
    where = ["r.date = ?", f"r.{rps_col} IS NOT NULL",
             f"r.{rps_col} >= ?", f"r.{rps_col} <= ?"]
    params: list = [d, rps_min, rps_max]
    if exclude_st:
        # 必须用 NOT EXISTS，不能写成
        #   `r.symbol IN (SELECT symbol FROM security_master WHERE ...)`
        # IN 子查询会让优化器放弃 idx_rps_date、改用主键 (symbol, date) 逐只查找，
        # 实测 72.4ms → 4.9ms。security_master.symbol 是主键，LEFT JOIN 版本
        # 同样快（3.9ms）但会改变连接结构，NOT EXISTS 语义最干净。
        where.append(
            "NOT EXISTS (SELECT 1 FROM security_master s "
            "WHERE s.symbol = r.symbol AND s.is_st IS NOT NULL AND s.is_st <> 0)"
        )

    # 排序：能在 rps_daily 单表上排的（rps / ret）放内层，让 LIMIT 先取 Top-N；
    # 其余列必须 JOIN 之后才能拿到，只能排在外层。
    inner_order = ""
    outer_sort_col = sort if sort in _SORTS else _SORTS["rps"]
    if sort in ("rps", "ret"):
        direction = "DESC"
        col = f"r.{rps_col}" if sort == "rps" else f"r.{ret_col}"
        alias = "rps" if sort == "rps" else "period_return"
        # 必须补 symbol 作次级排序键：RPS 是百分位，**大量股票取值相同**，
        # 没有确定性次序时第 1 页和第 2 页会互相重复（实测翻到过重叠行）。
        inner_order = f" ORDER BY {col} {direction}, r.symbol ASC"
        outer_sort_col = alias
        outer_null_guard = ""
    else:
        direction = "ASC" if sort in _ASC_SORTS else "DESC"
        outer_null_guard = (
            f"{outer_sort_col} IS NULL, " if outer_sort_col in _NULLABLE_SORTS else ""
        )

    inner = (
        "SELECT r.symbol, "
        f"r.{rps_col} AS rps, r.{ret_col} AS period_return "
        f"FROM rps_daily r WHERE {' AND '.join(where)}"
        f"{inner_order} LIMIT ? OFFSET ?"
    )

    total_row = read_sql(
        conn,
        "SELECT COUNT(*) AS c FROM ("
        + inner.replace(f" LIMIT ? OFFSET ?", "")
        + ") t",
        tuple(params),
    )
    total_n = int(total_row["c"].iloc[0]) if not total_row.empty else 0

    # 与慢路径一致：快照不可用时保留列名、置 NULL，只是不 JOIN
    spot_cols = (
        " , p.volume_ratio, p.total_mv, p.float_mv, p.price "
        "LEFT JOIN spot_snapshot p ON p.symbol = t.symbol AND p.date = ? "
        if with_spot else
        " , CAST(NULL AS REAL) AS volume_ratio, CAST(NULL AS REAL) AS total_mv, "
        "CAST(NULL AS REAL) AS float_mv, CAST(NULL AS REAL) AS price "
    )
    spot_params: tuple = (d,) if with_spot else ()

    sql = (
        "SELECT t.symbol, t.rps, t.period_return, s.name, s.is_st, i.industry, "
        "       b.pct_chg AS pct_chg, b.amount AS amount, "
        "       b.turn AS turnover_rate, b.close AS close "
        f"{spot_cols}"
        "FROM (" + inner + ") t "
        "LEFT JOIN security_master s ON s.symbol = t.symbol "
        "LEFT JOIN industry_map i ON i.symbol = t.symbol "
        "LEFT JOIN daily_bar b ON b.symbol = t.symbol AND b.date = ?"
    )
    params_out = tuple(params) + (limit, (page - 1) * limit)
    # 外层必须重复同样的 ORDER BY：SQL 不保证「子查询的排序」在
    # 外层 JOIN 之后被保留，不写就会随机换序、分页出现重复行。
    # symbol 必须写成 t.symbol：外层四张表都有同名列，不限定会 ambiguous。
    sql += f" ORDER BY {outer_null_guard}{outer_sort_col} {direction}, t.symbol ASC"

    df = read_sql(
        conn, sql,
        params_out + spot_params + (d,),
    )
    return df, total_n


def _ranking_filtered(
    conn, d: str, period: int, sort: str,
    rps_min: float, rps_max: float,
    exclude_st: bool, industries: str | None, min_amount: float,
    limit: int, page: int, with_spot: bool,
) -> tuple[pd.DataFrame, int]:
    """慢路径：行业 / 成交额过滤必须先 JOIN 再排序分页。"""
    where = [f"r.date = ?"]
    params: list = [d]
    where.append(f"r.rps{period} IS NOT NULL")
    where.append(f"r.rps{period} >= ? AND r.rps{period} <= ?")
    params += [rps_min, rps_max]
    if exclude_st:
        where.append("(s.is_st IS NULL OR s.is_st = 0)")
    if industries:
        names = [x.strip() for x in industries.split(",") if x.strip()]
        if names:
            where.append("i.industry IN (%s)" % ",".join("?" * len(names)))
            params += names
    if min_amount > 0:
        where.append("b.amount >= ?")
        params.append(min_amount)

    sort_col = _SORTS.get(sort, _SORTS["rps"])
    base = (
        "SELECT r.symbol, r.rps{p} AS rps, r.ret{p} AS period_return, "
        "       s.name, s.is_st, i.industry, "
        "       b.pct_chg AS pct_chg, b.amount AS amount, "
        "       b.turn AS turnover_rate, b.close AS close "
        # 快照不可用时不 JOIN（空表逐行回表纯属浪费），但**保留列名并置 NULL**，
        # 保证响应结构与可用时完全一致，前端不必为两种情况写两套取值逻辑。
        + (" , p.volume_ratio, p.total_mv, p.float_mv, p.price " if with_spot
           else " , CAST(NULL AS REAL) AS volume_ratio, CAST(NULL AS REAL) AS total_mv, "
                "CAST(NULL AS REAL) AS float_mv, CAST(NULL AS REAL) AS price ")
        + "FROM rps_daily r "
        "LEFT JOIN security_master s ON s.symbol = r.symbol "
        "LEFT JOIN industry_map i ON i.symbol = r.symbol "
        "LEFT JOIN daily_bar b ON b.symbol = r.symbol AND b.date = r.date "
        + ("LEFT JOIN spot_snapshot p ON p.symbol = r.symbol AND p.date = r.date "
           if with_spot else "")
        + "WHERE " + " AND ".join(where)
    ).format(p=period)

    total = read_sql(conn, f"SELECT COUNT(*) AS c FROM ({base}) t", tuple(params))
    total_n = int(total["c"].iloc[0]) if not total.empty else 0

    # 排序列必须用子查询包裹：`amount` 同时存在于 daily_bar 与 spot_snapshot，
    # 直接 ORDER BY amount 会被 SQLite 判为 ambiguous column name。
    order = "ASC" if sort in _ASC_SORTS else "DESC"
    null_guard = f"{sort_col} IS NULL, " if sort_col in _NULLABLE_SORTS else ""
    # symbol 作为次级排序键，保证分页稳定（百分位取值大量相同，
    # 没有确定性次序时相邻页会互相重复）
    sql = (f"SELECT * FROM ({base}) t "
           f"ORDER BY {null_guard}{sort_col} {order}, symbol ASC LIMIT ? OFFSET ?")
    df = read_sql(conn, sql, tuple(params) + (limit, (page - 1) * limit))
    return df, total_n


@router.get("/ranking")
def ranking(
    date: str | None = None,
    period: int = config.DEFAULT_PERIOD,
    rps_min: float = 0.0,
    rps_max: float = 100.0,
    industries: str | None = Query(None, description="逗号分隔的行业名，空=全部"),
    exclude_st: bool = True,
    min_amount: float = 0.0,
    sort: str = "rps",
    limit: int = 200,
    page: int = 1,
    delta_days: int = 0,
) -> dict:
    """RPS 排行榜（多周期筛选 + 排序 + 分页）。

    delta_days > 0 时额外返回 rank_prev / rank_delta（相对 N 个交易日前的名次变化）。
    """
    conn = db.connect()
    try:
        if period not in config.PERIODS:
            period = config.DEFAULT_PERIOD
        d = resolve_date(conn, date)
        if not d:
            return {"total": 0, "rows": [], "data_date": None, "period": period,
                    "message": "数据库为空，请先执行 cli init + backfill + compute-rps"}

        # 当日快照（可选）：只补日K 里没有的字段——量比、总市值、流通市值。
        # 成交额与换手率直接取自 daily_bar，因此即使快照完全不可用，
        # 排行榜的核心列依然齐全。快照表为空时直接不 JOIN。
        with_spot = spot_available(conn)

        # Query(None) 只在 FastAPI 里会被解析成 None；被脚本/测试直接调用时
        # 拿到的仍是 Query 对象，这里做一次归一化。
        industries = industries if isinstance(industries, str) else None
        names_filter = [x.strip() for x in (industries or "").split(",") if x.strip()]
        heavy_filter = bool(names_filter) or min_amount > 0
        if heavy_filter:
            df, total_n = _ranking_filtered(
                conn, d, period, sort, rps_min, rps_max,
                exclude_st, industries, min_amount, limit, page, with_spot,
            )
        else:
            df, total_n = _ranking_fast(
                conn, d, period, sort, rps_min, rps_max,
                exclude_st, limit, page, with_spot,
            )

        if df.empty:
            return {"total": total_n, "rows": [], "data_date": d, "period": period,
                    "rank_delta_days": 0}

        # 全市场名次（不是本页内名次）；delta_days > 0 时带上对比日
        prev_date = None
        if delta_days and delta_days > 0:
            prev_date = nth_previous_date(conn, d, delta_days)
        ranks = market_ranks(conn, d, period, prev_date)
        df = df.merge(ranks, on="symbol", how="left")

        # 行业内排名（按当前周期的 rps，仅针对本页返回的股票）
        df["rank_in_industry"] = (
            df.groupby("industry")["rps"].rank(ascending=False, method="min")
        )
        return {
            "total": total_n,
            "rows": jsonable(df),
            "data_date": d,
            "period": period,
            "page": page,
            "limit": limit,
            "delta_days": delta_days,
            "rank_delta_base_date": prev_date,
        }
    finally:
        conn.close()


@router.get("/sector")
def sector(date: str | None = None, period: int = config.DEFAULT_PERIOD) -> dict:
    """板块 RPS 排行 + 领涨股。"""
    conn = db.connect()
    try:
        d = resolve_date(conn, date)
        if not d:
            return {"rows": [], "data_date": None, "period": period}
        if period not in config.PERIODS:
            period = config.DEFAULT_PERIOD
        df = read_sql(
            conn,
            "SELECT industry, rps{p} AS rps, ret{p} AS period_return, "
            "member_count, leader_symbol, leader_name "
            "FROM industry_rps_daily WHERE date = ? "
            "ORDER BY (rps{p} IS NULL), rps{p} DESC".format(p=period),
            (d,),
        )
        return {"rows": jsonable(df), "data_date": d, "period": period}
    finally:
        conn.close()


@router.get("/heatmap")
def heatmap(date: str | None = None) -> dict:
    """行业 × 周期 RPS 热力图矩阵。"""
    conn = db.connect()
    try:
        d = resolve_date(conn, date)
        if not d:
            return {"industries": [], "periods": list(config.PERIODS),
                    "matrix": [], "data_date": None}
        df = read_sql(
            conn,
            "SELECT industry, rps20, rps60, rps120, rps250 FROM industry_rps_daily"
            " WHERE date = ?",
            (d,),
        )
        if df.empty:
            return {"industries": [], "periods": list(config.PERIODS),
                    "matrix": [], "data_date": d}
        df = df.sort_values("rps60", ascending=False)
        return {
            "industries": df["industry"].tolist(),
            "periods": list(config.PERIODS),
            "matrix": jsonable(df[["industry", "rps20", "rps60", "rps120", "rps250"]]),
            "data_date": d,
        }
    finally:
        conn.close()
