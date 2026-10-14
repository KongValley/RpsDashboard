"""涨跌停价采集（eltdx 通达信协议）。

为什么不用拍出来的阈值：A股的涨跌幅限制**不是一个常数**——主板 10%、
创业板/科创板 20%、北交所 30%、ST 5%、新股上市前五日根本没有限制。
早期版本用 `LIMIT_MAIN=9.8 / LIMIT_GROWTH=19.8` 硬编码，而且量纲还是错的
（百分数 vs daily_bar 里的小数），导致 `exclude_limit_up` 从未命中任何一只股票。

接口实测（本机 2026-10-12）：
    helpers.daily_price_limits(trade_date='20261012')
    → 31~35 秒，返回 5580 条，字段含
      limit_up_price / limit_down_price / limit_ratio_pct / limit_rule / limit_status
    实测 limit_rule 分布：
      main_10pct 3053 / chinext_20pct 1411 / star_20pct 617 /
      bse_30pct 350 / st_main_10pct 145 / ipo_first_5_days 4
    实测 limit_status 分布：
      normal 5569 / no_price_limit 4 / missing_pre_close 7

注意这个接口**每次都是冷启**（同一 trade_date 连调两次分别 35.3s / 31.4s，
服务端没有缓存）。因此只在每日更新时调一次，绝不能放进任何请求链路。
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import pandas as pd

from .. import db
from .tdxnet_src import TdxSource


def fetch_limit_rows(trade_date: str, src: TdxSource | None = None) -> pd.DataFrame:
    """拉取指定交易日的全市场涨跌停信息。

    trade_date 形如 '20261012'。返回列：
        date, symbol, limit_ratio, limit_up, limit_down, rule, status
    """
    ymd = trade_date.replace("-", "")
    own = src is None
    src = src or TdxSource()
    try:
        table = src._client.helpers.daily_price_limits(trade_date=ymd)  # noqa: SLF001
        rows = list(getattr(table, "rows", None) or [])
    finally:
        if own:
            src.close()

    if not rows:
        return pd.DataFrame(
            columns=["date", "symbol", "limit_ratio", "limit_up",
                     "limit_down", "rule", "status"]
        )

    iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
    recs = []
    for r in rows:
        full = str(getattr(r, "full_code", "") or "")
        if not full:
            continue
        recs.append({
            "date": iso,
            "symbol": full[2:],
            "limit_ratio": getattr(r, "limit_ratio_pct", None),
            "limit_up": getattr(r, "limit_up_price", None),
            "limit_down": getattr(r, "limit_down_price", None),
            "rule": getattr(r, "limit_rule", None),
            "status": getattr(r, "limit_status", None),
        })
    return pd.DataFrame(recs)


def sync_price_limits(trade_date: str | None = None) -> int:
    """采集并写入 price_limit_daily，返回写入行数。

    trade_date 缺省为「daily_bar 里最近有行情的交易日」——用真实交易日而不是
    今天，否则周末运行时会去查一个不存在的交易日。
    """
    conn = db.connect()
    try:
        if trade_date is None:
            row = conn.execute("SELECT MAX(date) AS d FROM daily_bar").fetchone()
            trade_date = row["d"] if row and row["d"] else date.today().isoformat()
        t0 = time.time()
        df = fetch_limit_rows(trade_date)
        if df.empty:
            print(f"[limits] {trade_date} 未取到涨跌停数据")
            return 0
        n = db.replace_date_partition(conn, "price_limit_daily", [trade_date], df)
        n_ok = int((df["status"] == "normal").sum())
        n_nolimit = int((df["status"] == "no_price_limit").sum())
        print(f"[limits] {trade_date} 写入 {n} 行，耗时 {time.time()-t0:.0f}s "
              f"（正常涨跌停 {n_ok}，无涨跌幅限制 {n_nolimit}）")
        return n
    finally:
        conn.close()


def sync_price_limits_chunk(
    dates: list[str], *, workers: int = 1
) -> tuple[int, list[str], list[str]]:
    """并发补齐一波交易日，返回 (写入行数, 失败日期, 返回 0 行的日期)。

    每个 worker 调一次 `sync_price_limits(d)`：协议接口**每次调用都是冷启**
    （实测单日 35 秒左右，服务端不缓存），网络是唯一的瓶颈，因此并发能近似线性地
    缩短墙钟时间。写库仍由各调用自己的连接完成（`db.connect` 带 30 秒 busy timeout，
    WAL 下并发写会退让重试）。

    并发度交给调用方按波控制（一波 ≤ workers 天）：时间预算、进度与「还剩多少天」
    都在调用方的波循环里判断，这里只负责跑完这一波。
    """
    total = 0
    failed: list[str] = []
    empty: list[str] = []
    if not dates:
        return 0, failed, empty
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futs = {pool.submit(sync_price_limits, d): d for d in dates}
        for fut in as_completed(futs):
            d = futs[fut]
            try:
                n = int(fut.result() or 0)
            except Exception as exc:  # noqa: BLE001
                n = 0
                failed.append(d)
                print(f"[limits] {d} 采集失败：{type(exc).__name__}: {exc}",
                      file=sys.stderr)
            total += n
            if n == 0:
                empty.append(d)
    return total, failed, empty
