"""元数据接口：状态、日期、行业、周期。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from .. import config, db
from . import latest_bar_date, read_sql

router = APIRouter(prefix="/api/meta", tags=["meta"])


@router.get("/status")
def status() -> dict:
    conn = db.connect()
    try:
        # 行数只用于展示（状态栏的「日K 427 万行」），走 60 秒 TTL 缓存：
        # 对 400 万行的表 COUNT(*) 要 235ms，不能每个请求都付一遍。
        bar_rows = db.count_rows_cached(conn, "daily_bar")
        rps_rows = db.count_rows_cached(conn, "rps_daily")
        ind_rows = db.count_rows_cached(conn, "industry_rps_daily")
        last_bar = latest_bar_date(conn)

        # 没有行业分类的股票：这些票会被排除在板块 RPS 之外，个股页显示「未分类」。
        # 实测 5580 只里有 356 只没有映射，是个值得摆在台面上的缺口而不是沉默。
        unmapped = 0
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM security_master m "
                "WHERE NOT EXISTS (SELECT 1 FROM industry_map i WHERE i.symbol = m.symbol)"
            ).fetchone()
            unmapped = int(row["c"]) if row else 0
        except Exception:  # noqa: BLE001
            unmapped = 0

        last = db.last_log(conn)
        return {
            "db_ready": bar_rows > 0,
            "bar_rows": bar_rows,
            "rps_rows": rps_rows,
            "industry_rows": ind_rows,
            "last_bar_date": last_bar,
            "unmapped_symbols": unmapped,
            "source": config.DATA_SOURCE,
            "periods": list(config.PERIODS),
            "last_collect_log": last,
        }
    finally:
        conn.close()


@router.get("/dates")
def dates(limit: int = Query(2000, ge=1, le=5000)) -> list[str]:
    """可用交易日（倒序）。

    默认 2000 覆盖全部 rps 历史（当前约 1000 天）；原来的 400 会把日期下拉
    截断，用户选不到更早的交易日。limit=-1 会被 Query 的 ge=1 直接拒掉。
    """
    conn = db.connect()
    try:
        df = read_sql(
            conn,
            "SELECT DISTINCT date FROM rps_daily ORDER BY date DESC LIMIT ?",
            (limit,),
        )
        return df["date"].tolist() if not df.empty else []
    finally:
        conn.close()


@router.get("/industries")
def industries() -> list[str]:
    conn = db.connect()
    try:
        df = read_sql(
            conn,
            "SELECT industry, COUNT(*) AS n FROM industry_map"
            " WHERE industry IS NOT NULL AND industry <> ''"
            " GROUP BY industry ORDER BY n DESC",
        )
        return df["industry"].tolist() if not df.empty else []
    finally:
        conn.close()


@router.get("/periods")
def periods() -> list[int]:
    return list(config.PERIODS)
