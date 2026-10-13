"""个股接口：详情 + RPS/价格曲线。"""

from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, HTTPException

from .. import config, db
from . import jsonable, read_sql

router = APIRouter(prefix="/api/stock", tags=["stock"])


@router.get("/{symbol}")
def detail(symbol: str) -> dict:
    conn = db.connect()
    try:
        row = read_sql(
            conn,
            "SELECT s.symbol, s.name, s.exchange, s.ipo_date, s.is_st, i.industry "
            "FROM security_master s LEFT JOIN industry_map i ON i.symbol = s.symbol "
            "WHERE s.symbol = ?",
            (symbol,),
        )
        if row.empty:
            raise HTTPException(status_code=404, detail=f"未找到股票 {symbol}")

        info = row.iloc[0].to_dict()
        # 数据源不给上市日（security_master.ipo_date 恒为 NULL），
        # 显示「上市日 —」等于给用户一个假字段；这里给出库中该股首根 K 线日期，
        # 语义明确（单只股票走 (symbol,date) 主键索引，代价可忽略）。
        first = conn.execute(
            "SELECT MIN(date) AS d FROM daily_bar WHERE symbol = ?", (symbol,)
        ).fetchone()
        info["first_bar_date"] = first["d"] if first else None
        rps_row = read_sql(
            conn,
            "SELECT date, " + ", ".join(f"rps{p}" for p in config.PERIODS)
            + ", " + ", ".join(f"ret{p}" for p in config.PERIODS)
            + " FROM rps_daily WHERE symbol = ? ORDER BY date DESC LIMIT 1",
            (symbol,),
        )

        current: dict = {}
        if not rps_row.empty:
            current = rps_row.iloc[0].to_dict()

        # 各周期在全市场中的横向分位（跑赢全市场百分之多少的股票）
        ranks: dict[str, float | None] = {}
        for p in config.PERIODS:
            col = f"rps{p}"
            if current.get(col) is None:
                ranks[col] = None
                continue
            stats = read_sql(
                conn,
                f"SELECT COUNT(*) AS total, "
                f"SUM(CASE WHEN rps{p} >= ? THEN 1 ELSE 0 END) AS at_or_above "
                f"FROM rps_daily WHERE date = ? AND rps{p} IS NOT NULL",
                (current[col], current["date"]),
            )
            total_n = int(stats["total"].iloc[0]) if not stats.empty else 0
            at_or_above = int(stats["at_or_above"].iloc[0]) if not stats.empty else 0
            ranks[col] = round(100 * at_or_above / total_n, 1) if total_n else None

        # 所属行业当前 RPS
        industry_rps = None
        if info.get("industry") and current:
            ind = read_sql(
                conn,
                "SELECT rps20, rps60, rps120, rps250 FROM industry_rps_daily"
                " WHERE date = ? AND industry = ?",
                (current["date"], info["industry"]),
            )
            if not ind.empty:
                industry_rps = ind.iloc[0].to_dict()

        return {
            "info": info,
            "current": current,
            "market_percentile": ranks,
            "industry_rps": industry_rps,
        }
    finally:
        conn.close()


@router.get("/{symbol}/patterns")
def patterns(symbol: str, days: int = 250) -> dict:
    """该股最近的三升底形态记录（用于详情页标注形态位置与状态）。

    每条包含：形态确认日、第一根阳线日期、失效位（第一根最低价）、
    形态确认日之后的最低价，据此判定形态是否仍然有效。
    """
    from .rps import _patterns_ready

    conn = db.connect()
    try:
        if not _patterns_ready(conn):
            return {"symbol": symbol, "patterns": [], "current": None,
                    "pattern_missing": True}
        rows = read_sql(
            conn,
            "SELECT date, base_date, base_low, base_high, strength, body_ratio "
            "FROM pattern_daily WHERE symbol = ? ORDER BY date DESC LIMIT ?",
            (symbol, max(1, min(int(days), 2000))),
        )
        if rows.empty:
            return {"symbol": symbol, "patterns": [], "current": None,
                    "pattern_missing": False}
        pats = jsonable(rows)
        # 用形态确认日之后的最低价判断是否失效
        for p in pats:
            lo = read_sql(
                conn,
                "SELECT MIN(low) AS m FROM daily_bar WHERE symbol = ? AND date > ?",
                (symbol, p["date"]),
            )
            after = lo["m"].iloc[0] if not lo.empty else None
            after = None if after is None or pd.isna(after) else float(after)
            p["min_low_after"] = after
            p["active"] = True if after is None else bool(after >= p["base_low"])
        latest = conn.execute("SELECT MAX(date) AS d FROM rps_daily").fetchone()["d"]
        current = None
        if pats and pats[0]["active"]:
            # 最近一次形态是否就在当前交易日（或很近）
            from ..analytics.patterns import window_start
            near = window_start(conn, latest, 3)
            if pats[0]["date"] >= (near or pats[0]["date"]):
                current = pats[0]
        return {"symbol": symbol, "patterns": pats, "current": current,
                "pattern_missing": False}
    finally:
        conn.close()


@router.get("/{symbol}/curve")
def curve(symbol: str, days: int = 250) -> dict:
    """RPS 多周期曲线 + 后复权价格 + 涨跌幅（日期轴严格对齐）。"""
    # 夹紧：days=-1 时 SQLite 的 LIMIT -1 表示「无上限」，一次会把该股全部历史
    # 拉出来（同文件 patterns() 已经有同样的保护）
    days = max(1, min(int(days), 2000))
    conn = db.connect()
    try:
        df = read_sql(
            conn,
            "SELECT date, " + ", ".join(f"rps{p}" for p in config.PERIODS)
            + " FROM rps_daily WHERE symbol = ? ORDER BY date DESC LIMIT ?",
            (symbol, days),
        )
        if df.empty:
            raise HTTPException(status_code=404, detail=f"该股票暂无 RPS 数据: {symbol}")
        df = df.sort_values("date").reset_index(drop=True)

        # 关键：价格必须按 **RPS 已有的那些日期** 取，而不是对 daily_bar
        # 独立 LIMIT —— 两张表的日期覆盖范围不同，各自取最后 N 条会错位，
        # 导致曲线一条有值一条全是空。
        dates = df["date"].tolist()
        ph = ",".join("?" * len(dates))
        prices = read_sql(
            conn,
            f"SELECT date, close, pct_chg, volume FROM daily_bar "
            f"WHERE symbol = ? AND date IN ({ph})",
            (symbol, *dates),
        )
        if not prices.empty:
            df = df.merge(
                prices[["date", "close", "pct_chg", "volume"]], on="date", how="left"
            )

        return {
            "symbol": symbol,
            "dates": df["date"].tolist(),
            "close": df["close"].round(4).tolist() if "close" in df else [],
            "pct_chg": df["pct_chg"].round(4).tolist() if "pct_chg" in df else [],
            "volume": df["volume"].tolist() if "volume" in df else [],
            **{f"rps{p}": df[f"rps{p}"].tolist() for p in config.PERIODS},
        }
    finally:
        conn.close()
