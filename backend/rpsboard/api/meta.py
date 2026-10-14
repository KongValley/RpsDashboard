"""元数据接口：状态、日期、行业、周期。"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Query

from .. import config, db
from . import latest_bar_date, read_sql

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/meta", tags=["meta"])

#: 质量块的缓存 TTL：`/api/meta/status` 被前端轮询，而质量块要扫
#: `price_limit_daily`（356 万行）与 `rps_daily`（626 万行）两棵索引的
#: `COUNT(DISTINCT date)` —— 未缓存时整个接口实测 **2.1 ~ 3.4 秒**（磁盘压力大时更慢），
#: 缓存命中后 **8 ms**。与 `db.count_rows_cached` 同样的取舍：数据每天才变一次，
#: 5 分钟延迟无感，而把 TTL 从 60 秒提到 300 秒能把「冷一次」的概率压到 1/5。
_QUALITY_TTL = 300.0
_quality_cache: dict = {}


def _compute_quality(conn) -> dict:
    """涨跌停实测覆盖 / 复权异常计数 / 概念覆盖。

    这些数字原先只有 CLI 看得到（cli status、cli quality），页面上完全不可见，
    于是「回测为什么没剔涨停」「这只票为什么没有行业」都无从判断。
    每一块单独 try：任何一块统计失败都要返回 None（而不是 0），
    谎报「0 个异常」比不给数字更糟。
    """
    quality: dict = {}
    try:
        lim = conn.execute(
            "SELECT COUNT(DISTINCT date) AS days, MIN(date) AS lo, MAX(date) AS hi "
            "FROM price_limit_daily"
        ).fetchone()
        # 分母取 **RPS 交易日**而不是 daily_bar 的日期数：后者含 2018~2021 那些
        # 只有少数停牌股有数据的稀疏日期（1,734 天），拿它当分母会把覆盖率
        # 从 48% 稀释成 35%，而回测真正会用的日子就是 RPS 那 1,285 天。
        total_days = conn.execute(
            "SELECT COUNT(DISTINCT date) AS c FROM rps_daily"
        ).fetchone()
        quality["price_limit"] = {
            "days": int(lim["days"]) if lim and lim["days"] else 0,
            "total_days": int(total_days["c"]) if total_days and total_days["c"] else 0,
            "first": lim["lo"] if lim else None,
            "last": lim["hi"] if lim else None,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("统计涨跌停覆盖失败: %s", exc)
        quality["price_limit"] = None
    try:
        quality["adjust_events"] = {
            f"{r['source']}/{r['kind']}": int(r["n"])
            for r in conn.execute(
                "SELECT source, kind, COUNT(*) AS n FROM adjust_event "
                "GROUP BY source, kind"
            )
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("统计复权异常失败: %s", exc)
        quality["adjust_events"] = None
    try:
        with_rps = conn.execute(
            "SELECT COUNT(DISTINCT concept) AS c FROM concept_rps_daily"
        ).fetchone()
        total = conn.execute(
            "SELECT COUNT(DISTINCT concept) AS c FROM concept_map"
        ).fetchone()
        quality["concept"] = {
            "with_rps": int(with_rps["c"]) if with_rps and with_rps["c"] else 0,
            "total": int(total["c"]) if total and total["c"] else 0,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("统计概念覆盖失败: %s", exc)
        quality["concept"] = None
    return quality


def _quality(conn) -> dict:
    key = str(config.DB_PATH)
    now = time.monotonic()
    hit = _quality_cache.get(key)
    if hit is not None and now - hit[0] < _QUALITY_TTL:
        return hit[1]
    value = _compute_quality(conn)
    _quality_cache[key] = (now, value)
    return value


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
        # 统计失败时返回 None（不是 0）：谎报「0 只无分类」会让前端的缺口提示消失。
        unmapped: int | None = 0
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM security_master m "
                "WHERE NOT EXISTS (SELECT 1 FROM industry_map i WHERE i.symbol = m.symbol)"
            ).fetchone()
            unmapped = int(row["c"]) if row else 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("统计无行业分类的股票失败: %s", exc)
            unmapped = None

        last = db.last_log(conn)
        quality = _quality(conn)

        return {
            "db_ready": bar_rows > 0,
            "bar_rows": bar_rows,
            "rps_rows": rps_rows,
            "industry_rows": ind_rows,
            "last_bar_date": last_bar,
            "unmapped_symbols": unmapped,
            "quality": quality,
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
