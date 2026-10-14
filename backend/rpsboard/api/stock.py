"""个股接口：详情 + RPS/价格曲线。"""

from __future__ import annotations

import logging
import math

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException

from .. import config, db
from . import jsonable, read_sql

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stock", tags=["stock"])


def _clean(obj) -> dict:
    """dict / Series → 全部 NaN/inf 换成 None。

    Starlette 的 JSONResponse 用 allow_nan=False：库里某个周期是 NULL 时
    read_sql 给的是 NaN，直接返回就是 500（实测 157 行周期值为 NULL，
    688796 的 rps250 就是 NULL）。DataFrame 走 api.jsonable，dict/Series 走这里。
    """
    if obj is None:
        return {}
    data = obj.to_dict() if isinstance(obj, pd.Series) else dict(obj)
    return {
        k: (None if isinstance(v, float) and not math.isfinite(v) else v)
        for k, v in data.items()
    }


def _num_list(values) -> list:
    """数值序列 → JSON 安全列表（NaN/inf → None）。"""
    return [
        None if isinstance(v, float) and not math.isfinite(v) else v
        for v in values
    ]


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

        # 复权异常：这只票若有「后复权价被压到极小/穿零」的行，页面上必须看得见 ——
        # 那些区间收益会被放大（实测 600076 曾因此拿到全市场 rps60=100）。现在的口径
        # 是**端点自动剔除**（见 matrix.build_close_matrix），但用户至少要知道
        # 「这只票历史上有这个问题、哪一段时间受影响」。
        anomalies: list[dict] = []
        try:
            anomalies = [
                {"date": r["date"], "kind": r["kind"], "detail": r["detail"]}
                for r in conn.execute(
                    "SELECT date, kind, detail FROM adjust_event WHERE symbol = ? "
                    "ORDER BY date DESC LIMIT 12",
                    (symbol,),
                )
            ]
        except Exception as exc:  # noqa: BLE001 - 旧库没有这张表
            logger.debug("读取复权异常失败: %s", exc)

        # 各周期在全市场中的横向分位：**跑赢全市场百分之多少的股票**
        # = 该周期 RPS ≤ 我 的股票占比。旧实现算的是「≥ 我」的占比，
        # 语义正好反过来 —— 全市场第一（301234，rps60=100）显示成
        # 「跑赢全市场 0.0%」。
        ranks: dict[str, float | None] = {}
        for p in config.PERIODS:
            col = f"rps{p}"
            # 必须用 pd.isna：这个周期在库里是 NULL 时 read_sql 给的是 NaN，
            # `is None` 判不出来，NaN 会被当成查询参数 → CASE 全部不成立 → 分位恒 0.0
            if pd.isna(current.get(col)) or not current.get("date"):
                ranks[col] = None
                continue
            stats = read_sql(
                conn,
                f"SELECT COUNT(*) AS total, "
                f"SUM(CASE WHEN rps{p} <= ? THEN 1 ELSE 0 END) AS at_or_below "
                f"FROM rps_daily WHERE date = ? AND rps{p} IS NOT NULL",
                (current[col], current["date"]),
            )
            if stats.empty:
                ranks[col] = None
                continue
            total_n = int(stats.iloc[0]["total"] or 0)
            # 没有匹配行时 SUM 返回 NULL → pandas 给 NaN，int(NaN) 会抛 TypeError
            at_or_below = int(stats["at_or_below"].fillna(0).iloc[0])
            ranks[col] = round(100 * at_or_below / total_n, 1) if total_n else None

        # 所属行业当前 RPS
        industry_rps = None
        if info.get("industry") and current:
            ind = read_sql(
                conn,
                "SELECT " + ", ".join(config.period_columns())
                + " FROM industry_rps_daily WHERE date = ? AND industry = ?",
                (current["date"], info["industry"]),
            )
            if not ind.empty:
                industry_rps = ind.iloc[0].to_dict()

        return {
            "info": _clean(info),
            "current": _clean(current),
            "market_percentile": ranks,
            "industry_rps": _clean(industry_rps) if industry_rps else None,
            "adjust_anomalies": anomalies,
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
        # 一次取该股全部 (date, low) 后在内存里定位，替掉「每条形态一次 MIN()」：
        # days=250 时旧写法最坏会发 251 次查询（每条形态一次），而这里只需 2 次。
        # rev_min[i] = min(low[i:]) → 「形态确认日之后的最低价」= rev_min[i+1]，
        # 反向累计最小值一次 O(n) 扫描即可（列表按 date 升序）。
        lo_df = read_sql(
            conn,
            "SELECT date, low FROM daily_bar WHERE symbol = ? ORDER BY date",
            (symbol,),
        )
        dates = lo_df["date"].tolist() if not lo_df.empty else []
        lows = (pd.to_numeric(lo_df["low"], errors="coerce")
                .to_numpy(dtype="float64") if not lo_df.empty
                else np.zeros(0, dtype="float64"))
        # NaN 换成 +inf 再累计：np.minimum.accumulate 遇 NaN 会一路传染，
        # 而 SQL 的 MIN() 是忽略 NULL 的 —— 不换就会把「后面有真实数据」
        # 误判成「后面没有数据」。
        lows = np.where(np.isfinite(lows), lows, np.inf)
        rev_min = np.minimum.accumulate(lows[::-1])[::-1] if len(lows) else lows
        pos = {d: i for i, d in enumerate(dates)}
        for p in pats:
            i = pos.get(p["date"])
            after = None if i is None or i + 1 >= len(lows) else float(rev_min[i + 1])
            if after is not None and not math.isfinite(after):
                after = None          # 全是 +inf（后面没有有效 low）等价于没有数据
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
            # 全部过 _num_list：库里某周期是 NULL 时 pandas 给 NaN，
            # 而 Starlette 的 allow_nan=False 会直接 500（688796 的 rps250 就是 NULL）
            "close": _num_list(df["close"].round(4).tolist()) if "close" in df else [],
            "pct_chg": (_num_list(df["pct_chg"].round(4).tolist())
                        if "pct_chg" in df else []),
            "volume": _num_list(df["volume"].tolist()) if "volume" in df else [],
            **{f"rps{p}": _num_list(df[f"rps{p}"].tolist())
               for p in config.PERIODS},
        }
    finally:
        conn.close()
