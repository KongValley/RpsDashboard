"""当日复盘接口：市场宽度 + 涨停/跌停清单 + 行业当日涨跌 + 信号名单。

一个页面对"某一天"做全局扫描。全部**只读库**，不联网、不现算 RPS；
`date` 走与全项目一致的 `resolve_date`（非交易日就近回退，响应里回显生效日）。

**信号名单的口径**：只做「RPS 阈值 + ST 过滤」这一层（`rps_daily` 直接筛），
**不含**共振/大盘择时——那两层要加载全市场矩阵，属于回测页的活。响应里用
`signals_note` 写明这件事，避免和回测口径看起来一样、实际不一样。
"""

from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, Query

from .. import config, db
from ..analytics.limits import daily_limit_lists
from . import jsonable, read_sql, resolve_date

router = APIRouter(prefix="/api/review", tags=["review"])

#: 连板统计往前看几个交易日（够区分 1 板与 2~5 板）
STREAK_WINDOW = 10

#: 信号名单上限（按区间涨幅排序取前 N）
DEFAULT_SIGNAL_LIMIT = 100


def _limit_streaks(conn, date: str, symbols: list[str]) -> dict[str, int]:
    """这些股票截至 `date` 的连续涨停天数（1 = 首板）。

    只看最近 `STREAK_WINDOW` 个交易日：连板是"情绪温度"，再往回翻没有意义。
    """
    if not symbols:
        return {}
    days = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM daily_bar WHERE date <= ?"
        " ORDER BY date DESC LIMIT ?", (date, STREAK_WINDOW))]
    if not days:
        return {}
    # 逐日取涨停集合（窗口很小，逐日查询比拼一条大 SQL 更好读）
    by_day = {d: {u["symbol"] for u in daily_limit_lists(conn, d)[0]} for d in days}
    out: dict[str, int] = {}
    for sym in symbols:
        n = 0
        for d in days:                     # days 是倒序：从当天往回数
            if sym in by_day[d]:
                n += 1
            else:
                break
        out[sym] = n
    return out


@router.get("/day")
def day(
    date: str | None = None,
    period: int = config.DEFAULT_PERIOD,
    threshold: float = Query(default=80.0, description="信号名单的 RPS 阈值"),
    exclude_st: bool = True,
    signal_limit: int = Query(default=DEFAULT_SIGNAL_LIMIT, ge=1, le=500),
) -> dict:
    """某一天的复盘数据：宽度、涨跌停清单（含连板）、行业当日涨跌、信号名单。"""
    if period not in config.PERIODS:
        period = config.DEFAULT_PERIOD
    conn = db.connect()
    try:
        d = resolve_date(conn, date)
        if not d:
            return {"date": None, "breadth": None, "limit_up": [], "limit_down": [],
                    "sectors": [], "signals": [], "signals_note": "",
                    "message": "数据库为空，请先执行 cli init + backfill + compute-rps"}

        # ── 市场宽度：当日有行情的全部分位 ──────────────────────────────
        breadth_df = read_sql(
            conn,
            "SELECT pct_chg FROM daily_bar WHERE date = ? AND pct_chg IS NOT NULL"
            " AND tradestatus <> 0",
            (d,),
        )
        ups, downs = daily_limit_lists(conn, d)
        # 宽度里必须看得见"停牌"：分母不是全市场股票数，而是**当日有行情**的只数
        pct = (pd.to_numeric(breadth_df["pct_chg"], errors="coerce")
               if not breadth_df.empty else pd.Series(dtype="float64"))
        breadth = {
            "traded": int(len(pct)),
            "up": int((pct > 0).sum()),
            "down": int((pct < 0).sum()),
            "flat": int((pct == 0).sum()),
            "limit_up": len(ups),
            "limit_down": len(downs),
            "median_pct": round(float(pct.median()) * 100, 2) if len(pct) else None,
        }

        # ── 涨跌停清单（带名称/行业/连板）──────────────────────────────
        def enrich(items: list[dict]) -> list[dict]:
            if not items:
                return []
            syms = [x["symbol"] for x in items]
            marks = ",".join("?" * len(syms))
            meta = {r["symbol"]: dict(r) for r in conn.execute(
                f"SELECT m.symbol AS symbol, m.name AS name, i.industry AS industry"
                f" FROM security_master m LEFT JOIN industry_map i"
                f"   ON i.symbol = m.symbol WHERE m.symbol IN ({marks})", syms)}
            streaks = _limit_streaks(conn, d, syms)
            out = []
            for x in sorted(items, key=lambda y: -y["pct"]):
                info = meta.get(x["symbol"]) or {}
                out.append({
                    "symbol": x["symbol"],
                    "name": info.get("name"),
                    "industry": info.get("industry"),
                    "pct": x["pct"],
                    "limit_ratio": x["ratio"],
                    "streak": streaks.get(x["symbol"], 0),
                })
            return out

        # ── 行业当日涨跌（等权）────────────────────────────────────────
        sectors_df = read_sql(
            conn,
            "SELECT i.industry AS industry, b.pct_chg AS pct_chg "
            "FROM daily_bar b JOIN industry_map i ON i.symbol = b.symbol "
            "WHERE b.date = ? AND b.pct_chg IS NOT NULL AND b.tradestatus <> 0"
            " AND i.industry IS NOT NULL AND i.industry <> ''",
            (d,),
        )
        sectors: list[dict] = []
        if not sectors_df.empty:
            sectors_df["pct_chg"] = pd.to_numeric(sectors_df["pct_chg"],
                                                  errors="coerce")
            tmp = sectors_df.assign(up=(sectors_df["pct_chg"] > 0).astype("int64"))
            agg = tmp.groupby("industry").agg(
                avg=("pct_chg", "mean"), n=("pct_chg", "count"), up=("up", "sum"))
            sectors = [
                {"industry": str(name),
                 "avg_pct": round(float(row["avg"]) * 100, 2),
                 "n": int(row["n"]),
                 "up": int(row["up"])}
                for name, row in agg.iterrows()
            ]
            sectors.sort(key=lambda x: -x["avg_pct"])

        # ── 信号名单：只做 RPS 阈值 + ST 过滤（口径写在 note 里）─────────
        col = f"rps{period}"
        sql = (
            f"SELECT r.symbol AS symbol, m.name AS name, i.industry AS industry,"
            f" r.{col} AS rps, r.ret{period} AS period_return, b.pct_chg AS pct_chg"
            f" FROM rps_daily r"
            f" LEFT JOIN security_master m ON m.symbol = r.symbol"
            f" LEFT JOIN industry_map i ON i.symbol = r.symbol"
            f" LEFT JOIN daily_bar b ON b.symbol = r.symbol AND b.date = r.date"
            f" WHERE r.date = ? AND r.{col} IS NOT NULL AND r.{col} >= ?"
        )
        params: list = [d, float(threshold)]
        if exclude_st:
            sql += (" AND NOT EXISTS (SELECT 1 FROM security_master s"
                    " WHERE s.symbol = r.symbol AND s.is_st = 1)")
        sql += f" ORDER BY r.ret{period} DESC, r.symbol ASC LIMIT ?"
        params.append(int(signal_limit))
        signals = jsonable(read_sql(conn, sql, tuple(params)))

        return {
            "date": d,
            "period": period,
            "threshold": float(threshold),
            "exclude_st": bool(exclude_st),
            "breadth": breadth,
            "limit_up": enrich(ups),
            "limit_down": enrich(downs),
            "sectors": sectors,
            "signals": signals,
            "signals_note": (
                f"这里只筛「RPS{period} ≥ {threshold:g}"
                + ("、非 ST" if exclude_st else "")
                + "」，按区间涨幅排序；**不含**共振与大盘择时 —— "
                "那两层要加载全市场矩阵，口径见「策略回测」页。"),
        }
    finally:
        conn.close()
