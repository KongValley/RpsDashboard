"""RPS 接口：排行榜、板块排行、热力图。"""

from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, Query

from .. import config, db
from . import jsonable, latest_bar_date, nth_previous_date, read_sql, resolve_date

router = APIRouter(prefix="/api/rps", tags=["rps"])

#: 排行榜单页上限。实测不设限时 limit=100000 会把全市场 5325 行一次性返回
#: （261 ms），分页语义与内存都没有保护。
MAX_PAGE_LIMIT = 500
DEFAULT_PAGE_LIMIT = 200

#: 排序键 → 外层可用的**输出列名**（不是原始表列名）。
#: 必须在 SELECT 里已经有对应别名，外层 ORDER BY 才能引用。
#:
#: 注意没有 turnover_rate（换手率）：通达信协议不提供，实测 daily_bar.turn
#: 非空行数为 0；而算换手率需要流通股本，stock_profile_table 要 94.5 秒/2 只、
#: latest_st() 又不给股数。宁可没有这一列，也不要留一列恒为 `—` 的假数据。
_SORTS = {
    "rps": "rps",
    "ret": "period_return",
    "pct_chg": "pct_chg",
    "amount": "amount",
    "close": "close",
}

#: 排序方向为 ASC 的键
_ASC_SORTS = {"pct_chg", "close"}

#: 这些列来自 LEFT JOIN 的表，可能为 NULL，排序时需要把 NULL 沉底。
#: rps / period_return 不在其中——WHERE 里已经要求 `rps IS NOT NULL`，
#: 而 rps 是 ret 的百分位排名，rps 非空必然意味着 ret 非空。
_NULLABLE_SORTS = {"pct_chg", "amount", "close"}


#: 三升底筛选可选窗口（交易日）。0 = 不筛选。
SANXINGDI_WINDOWS = (1, 5, 10, 20)


def _patterns_ready(conn) -> bool:
    """pattern_daily 是否已经算过。表不存在或为空都算没算好。

    没算好时前端要能区分「筛出来 0 只」和「还没算形态所以筛不出来」，
    否则用户会以为市场里根本没有三升底。
    """
    if not db.table_exists(conn, "pattern_daily"):
        return False
    return db.table_has_rows(conn, "pattern_daily")


def sanxingdi_filter_sql(
    conn, d: str, window: int, alias: str = "r"
) -> tuple[str, list]:
    """构造「近 N 个交易日内出现过三升底」的过滤子句。

    返回 (sql 片段, 参数)。window <= 0 时返回空串、无参数。
    窗口按**交易日**回退而不是日历日：周末与节假日会让两者越偏越远。
    """
    if window <= 0:
        return "", []
    from ..analytics import patterns as pat

    start = pat.window_start(conn, d, window)
    if not start:
        return "", []
    frag = (f" AND {alias}.symbol IN ("
            f"SELECT symbol FROM pattern_daily WHERE date >= ?)")
    return frag, [start]


def pattern_state(
    conn, symbols: list[str], date: str, window: int
) -> dict[str, dict]:
    """查这些股票在窗口内的形态命中情况，以及形态是否**仍然有效**。

    「有效」= 形态确认日之后，最低价没有跌破第一根阳线的最低价 base_low。
    只对当页返回的股票做计算（最多 MAX_PAGE_LIMIT 行），避免全市场扫描。
    """
    out: dict[str, dict] = {}
    if not symbols or window <= 0:
        return out
    from ..analytics import patterns as pat

    start = pat.window_start(conn, date, window)
    if not start:
        return out
    ph = ",".join("?" * len(symbols))
    rows = read_sql(
        conn,
        f"SELECT p.symbol, p.date, p.base_date, p.base_low, p.base_high, "
        f"       p.strength, p.body_ratio, MIN(b.low) AS min_low_after "
        f"FROM pattern_daily p "
        f"LEFT JOIN daily_bar b ON b.symbol = p.symbol AND b.date > p.date "
        f"WHERE p.symbol IN ({ph}) AND p.date >= ? AND p.date <= ? "
        f"GROUP BY p.symbol, p.date",
        tuple(symbols) + (start, date),
    )
    if rows.empty:
        return out
    # 同一只股票窗口内可能多次成立，取最近一次
    rows = rows.sort_values("date").drop_duplicates("symbol", keep="last")
    for _, r in rows.iterrows():
        after = r["min_low_after"]
        active = True if after is None or pd.isna(after) else bool(after >= r["base_low"])
        out[r["symbol"]] = {
            "hit": True,
            "date": r["date"],
            "base_date": r["base_date"],
            "base_low": r["base_low"],
            "base_high": r["base_high"],
            "strength": r["strength"],
            "body_ratio": r["body_ratio"],
            "active": active,
        }
    return out


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
    exclude_st: bool, limit: int, page: int, sanxingdi: int = 0,
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
    # 三升底筛选可以直接放进内层：它只依赖 rps_daily.symbol，
    # 不需要 JOIN 之后才能判断，因此不影响延迟连接的优化。
    frag, frag_params = sanxingdi_filter_sql(conn, d, sanxingdi)
    if frag:
        where.append(frag[5:])            # 去掉前导的 " AND "
        params += frag_params

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

    spot_params: tuple = ()

    sql = (
        "SELECT t.symbol, t.rps, t.period_return, s.name, s.is_st, i.industry, "
        "       b.pct_chg AS pct_chg, b.amount AS amount, "
        "       b.close AS close "
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
    limit: int, page: int, sanxingdi: int = 0,
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
    frag, frag_params = sanxingdi_filter_sql(conn, d, sanxingdi)
    if frag:
        where.append(frag[5:])
        params += frag_params

    sort_col = _SORTS.get(sort, _SORTS["rps"])
    base = (
        "SELECT r.symbol, r.rps{p} AS rps, r.ret{p} AS period_return, "
        "       s.name, s.is_st, i.industry, "
        "       b.pct_chg AS pct_chg, b.amount AS amount, "
        "       b.close AS close "
        "FROM rps_daily r "
        "LEFT JOIN security_master s ON s.symbol = r.symbol "
        "LEFT JOIN industry_map i ON i.symbol = r.symbol "
        "LEFT JOIN daily_bar b ON b.symbol = r.symbol AND b.date = r.date "
        "WHERE " + " AND ".join(where)
    ).format(p=period)

    total = read_sql(conn, f"SELECT COUNT(*) AS c FROM ({base}) t", tuple(params))
    total_n = int(total["c"].iloc[0]) if not total.empty else 0

    # 排序列必须用子查询包裹：外层多张表都有 `symbol` 列，
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
    limit: int = DEFAULT_PAGE_LIMIT,
    page: int = 1,
    delta_days: int = 0,
    sanxingdi: int = Query(
        0, description="三升底筛选窗口（交易日）：0 不筛选，可选 1/5/10/20",
    ),
) -> dict:
    """RPS 排行榜（多周期筛选 + 排序 + 分页）。

    delta_days > 0 时额外返回 rank_prev / rank_delta（相对 N 个交易日前的名次变化）。
    sanxingdi > 0 时只返回近 N 个交易日内出现过三升底的股票，
    并在每行附 sanxingdi_hit / sanxingdi_active 标记。
    """
    conn = db.connect()
    try:
        if period not in config.PERIODS:
            period = config.DEFAULT_PERIOD
        if sanxingdi and sanxingdi not in SANXINGDI_WINDOWS:
            sanxingdi = 0
        # 单页上限保护：不设限时 limit=100000 会把全市场一次性返回
        limit = max(1, min(int(limit), MAX_PAGE_LIMIT))
        page = max(1, int(page))
        d = resolve_date(conn, date)
        if not d:
            return {"total": 0, "rows": [], "data_date": None, "period": period,
                    "message": "数据库为空，请先执行 cli init + backfill + compute-rps"}

        # Query(None) 只在 FastAPI 里会被解析成 None；被脚本/测试直接调用时
        # 拿到的仍是 Query 对象，这里做一次归一化。
        industries = industries if isinstance(industries, str) else None
        names_filter = [x.strip() for x in (industries or "").split(",") if x.strip()]
        heavy_filter = bool(names_filter) or min_amount > 0
        if heavy_filter:
            df, total_n = _ranking_filtered(
                conn, d, period, sort, rps_min, rps_max,
                exclude_st, industries, min_amount, limit, page, sanxingdi,
            )
        else:
            df, total_n = _ranking_fast(
                conn, d, period, sort, rps_min, rps_max,
                exclude_st, limit, page, sanxingdi,
            )

        if df.empty:
            return {"total": total_n, "rows": [], "data_date": d, "period": period,
                    "rank_delta_days": 0, "sanxingdi": sanxingdi,
                    "pattern_missing": sanxingdi > 0 and not _patterns_ready(conn)}

        # 形态标记：命中 / 是否仍有效（未跌破第一根最低价）。
        # 只在启用筛选时才加这几列 —— 不筛选时返回三个全 False 的字段
        # 既没信息量又让响应变胖。
        if sanxingdi > 0:
            st = pattern_state(conn, df["symbol"].tolist(), d, sanxingdi)
            df["sanxingdi_hit"] = df["symbol"].isin(st.keys())
            df["sanxingdi_active"] = [bool(st.get(s, {}).get("active", False))
                                     for s in df["symbol"]]
            df["sanxingdi_date"] = [st.get(s, {}).get("date") for s in df["symbol"]]

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
            "sanxingdi": sanxingdi,
            "pattern_missing": not _patterns_ready(conn),
        }
    finally:
        conn.close()


@router.get("/ranking/export")
def ranking_export(
    date: str | None = None,
    period: int = config.DEFAULT_PERIOD,
    rps_min: float = 0.0,
    rps_max: float = 100.0,
    industries: str | None = Query(None, description="逗号分隔的行业名，空=全部"),
    exclude_st: bool = True,
    min_amount: float = 0.0,
    sort: str = "rps",
) -> "StreamingResponse":
    """按当前筛选条件导出**全量** CSV。

    之前前端导出的是 `rows.value`，也就是**当前页那 100 行**，按钮却写「导出 CSV」，
    用户会以为拿到了全市场。这里改为服务端按同一套筛选条件取全量。
    """
    from fastapi.responses import StreamingResponse

    industries = industries if isinstance(industries, str) else None
    if period not in config.PERIODS:
        period = config.DEFAULT_PERIOD
    conn = db.connect()
    try:
        d = resolve_date(conn, date)
        if not d:
            return StreamingResponse(iter([_csv_bytes([], None)], ), media_type="text/csv")
        names_filter = [x.strip() for x in (industries or "").split(",") if x.strip()]
        # 导出没有分页，limit 用一个足够大的值；单次上限 200000 防止误用
        limit = 200_000
        if names_filter or min_amount > 0:
            df, _ = _ranking_filtered(
                conn, d, period, sort, rps_min, rps_max,
                exclude_st, industries, min_amount, limit, 1,
            )
        else:
            df, _ = _ranking_fast(
                conn, d, period, sort, rps_min, rps_max, exclude_st, limit, 1,
            )
        if not df.empty:
            ranks = market_ranks(conn, d, period)
            df = df.merge(ranks, on="symbol", how="left")
    finally:
        conn.close()

    headers = ["代码", "名称", "行业", "RPS", "区间涨幅%", "当日涨跌%",
               "成交额", "全市场名次"]
    fname = f"rps{period}_{d}.csv"
    payload = _csv_bytes(df, headers)
    return StreamingResponse(
        iter([payload]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _csv_bytes(df: pd.DataFrame, headers: list[str] | None) -> bytes:
    """生成带 BOM 的 CSV。

    Excel 打开 UTF-8 CSV 时若没有 BOM，中文会显示成乱码；
    带 BOM 后它会自动识别编码（代价是首列名前会多一个不可见字符）。
    """
    if headers is None:
        return b"\xef\xbb\xbf"
    if df is None or df.empty:
        body = ",".join(headers)
        return ("\ufeff" + body + "\r\n").encode("utf-8")

    def fmt(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        if isinstance(v, float):
            return f"{v:.4f}"
        return str(v)

    lines = [",".join(headers)]
    for _, row in df.iterrows():
        lines.append(",".join([
            fmt(row.get("symbol")), fmt(row.get("name")),
            fmt(row.get("industry")), fmt(row.get("rps")),
            fmt(None if row.get("period_return") is None
                else row["period_return"] * 100),
            fmt(None if row.get("pct_chg") is None else row["pct_chg"] * 100),
            fmt(row.get("amount")), fmt(row.get("rank_overall")),
        ]))
    return ("\ufeff" + "\r\n".join(lines) + "\r\n").encode("utf-8")


@router.get("/concepts")
def concepts() -> list[str]:
    """可用概念题材清单（含成分股数），按覆盖股票数降序。"""
    conn = db.connect()
    try:
        df = read_sql(
            conn,
            "SELECT concept, COUNT(*) AS n FROM concept_map "
            "WHERE concept IS NOT NULL AND concept <> '' "
            "GROUP BY concept ORDER BY n DESC",
        )
        return df["concept"].tolist() if not df.empty else []
    finally:
        conn.close()


@router.get("/concept-ranking")
def concept_ranking(date: str | None = None, period: int = config.DEFAULT_PERIOD) -> dict:
    """概念板块 RPS 排行 + 领涨股。"""
    conn = db.connect()
    try:
        d = resolve_date(conn, date)
        if not d or not db.table_exists(conn, "concept_rps_daily"):
            return {"rows": [], "data_date": d, "period": period}
        if period not in config.PERIODS:
            period = config.DEFAULT_PERIOD
        df = read_sql(
            conn,
            "SELECT concept, rps{p} AS rps, ret{p} AS period_return, "
            "member_count, leader_symbol, leader_name "
            "FROM concept_rps_daily WHERE date = ? "
            "ORDER BY (rps{p} IS NULL), rps{p} DESC".format(p=period),
            (d,),
        )
        return {"rows": jsonable(df), "data_date": d, "period": period}
    finally:
        conn.close()


@router.get("/concept-heatmap")
def concept_heatmap(date: str | None = None) -> dict:
    """概念 × 周期 RPS 热力图矩阵（结构与 /heatmap 一致）。"""
    conn = db.connect()
    try:
        d = resolve_date(conn, date)
        if not d or not db.table_exists(conn, "concept_rps_daily"):
            return {"concepts": [], "periods": list(config.PERIODS),
                    "matrix": [], "data_date": d}
        df = read_sql(
            conn,
            "SELECT concept, rps20, rps60, rps120, rps250 FROM concept_rps_daily "
            "WHERE date = ?",
            (d,),
        )
        if df.empty:
            return {"concepts": [], "periods": list(config.PERIODS),
                    "matrix": [], "data_date": d}
        # 与行业热力图一致：后端按 rps60 排好，前端再按当前周期独立重排
        df = df.sort_values("rps60", ascending=False)
        return {
            "concepts": df["concept"].tolist(),
            "periods": list(config.PERIODS),
            "matrix": jsonable(df[["concept", "rps20", "rps60", "rps120", "rps250"]]),
            "data_date": d,
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
