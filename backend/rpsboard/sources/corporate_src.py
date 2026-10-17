"""公司行为（权息）采集：eltdx 的股本变迁接口（0x000f）。

**只发现事件，绝不猜经济日期。** 接口给的是事件日期、类别与四个业务字段，
推不出股权登记日、派息到账日、送股可卖日，更算不出个人分红税 —— 实测文档
（`7709-股本变迁GBBQ.md`）明确只到这一步。真正的入账时间表由用户按公告在
「模拟持仓 → 公司行为核对」里填（落 `paper_action`），缺核对就停在该日之前。

所以这里只做两件事：
1. 把事件原样落进 `corporate_event`（同日同类多条存数组，不覆盖）；
2. 推进 `corporate_sync.checked_through` —— **只**推进到本轮行情确实取全的
   日期（`raw_coverage.date_to`），这样「已经查过且没有事件」是可证的。
"""

from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import db
from .base import with_retry
from .tdxnet_src import TdxSource, to_full_code

#: 同日同类的多条事件按原始顺序存成一个 JSON 数组。
EVENT_PAYLOAD_KEYS = ("date", "category_raw", "category_name",
                      "c1_value", "c2_value", "c3_value", "c4_value")


def _record_dict(rec) -> dict:
    """把 SDK 的 CapitalChangeRecord 压成稳定、可 JSON 化的字典。"""
    out = {}
    for k in EVENT_PAYLOAD_KEYS:
        v = getattr(rec, k, None)
        out[k] = v.isoformat() if hasattr(v, "isoformat") else v
    return out


def fetch_capital_changes(src: TdxSource, symbol: str) -> list[dict] | None:
    """取一只股票的权息事件。

    返回：
    - `list[dict]`（可能为空列表）：**查过**了，空列表就是「没有事件」；
    - `None`：这次查询**失败/返回结构不认识**，等于未知，绝不能当成无事件。
    """
    try:
        block = src._client.corporate.capital_changes(to_full_code(symbol))
    except Exception:  # noqa: BLE001 - 网络/协议异常一律按「未知」处理
        return None
    if block is None:
        return None
    records = getattr(block, "records", None)
    if records is None:
        records = getattr(block, "items", None)
    if records is None:
        return None
    return [_record_dict(r) for r in records]


def _write_events(conn: sqlite3.Connection, symbol: str, events: list[dict]) -> int:
    """按 (symbol, event_date, category) 落库；同日同类合并成数组。"""
    buckets: dict[tuple[str, int], list[dict]] = {}
    for e in events:
        date = e.get("date")
        cat = e.get("category_raw")
        if not date or cat is None:
            continue
        buckets.setdefault((str(date), int(cat)), []).append(e)
    if not buckets:
        return 0
    now = db.now()
    conn.executemany(
        "INSERT OR REPLACE INTO corporate_event(symbol, event_date, category,"
        " payload, updated_at) VALUES (?,?,?,?,?)",
        [(symbol, date, cat, json.dumps(items, ensure_ascii=False), now)
         for (date, cat), items in buckets.items()])
    return len(buckets)


def sync_corporate(
    conn: sqlite3.Connection,
    symbols: list[str] | None = None,
    *,
    workers: int = 8,
    progress_every: int = 300,
) -> dict:
    """采集公司行为事件并推进查全标记。

    `symbols=None` 时取全部有行情覆盖的股票（`raw_coverage`）。每只股票的
    `checked_through` 推进到自己的 `raw_coverage.date_to`（读不到覆盖时退回
    上证指数的最新交易日）；失败的股票**保留旧标记**，不冒充查过。

    返回 `{"ok": n, "failed": [...], "events": n}`。
    """
    if symbols is None:
        symbols = [r["symbol"] for r in conn.execute(
            "SELECT symbol FROM raw_coverage ORDER BY symbol")]
    if not symbols:
        return {"ok": 0, "failed": [], "events": 0}

    coverage = {r["symbol"]: r["date_to"] for r in conn.execute(
        "SELECT symbol, date_to FROM raw_coverage")}
    fallback = db.market_date(conn)

    t0 = time.time()
    src = TdxSource()
    ok: list[str] = []
    failed: list[str] = []
    n_events = 0
    done = 0
    try:
        def one(sym: str):
            # 单只失败重试：全市场 5000+ 只里偶发超时很常见。
            return sym, with_retry(
                lambda: fetch_capital_changes(src, sym),
                max_retries=2, base_wait=1.0)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(one, s) for s in symbols]
            for fut in as_completed(futs):
                sym, events = fut.result()
                done += 1
                if events is None:
                    failed.append(sym)
                else:
                    n_events += _write_events(conn, sym, events)
                    through = coverage.get(sym) or fallback
                    if through:
                        conn.execute(
                            "INSERT OR REPLACE INTO corporate_sync(symbol,"
                            " checked_through, updated_at) VALUES (?,?,?)",
                            (sym, through, db.now()))
                    ok.append(sym)
                if progress_every and done % progress_every == 0:
                    el = time.time() - t0
                    eta = (len(symbols) - done) / max(done / max(el, 1e-9), 1e-9)
                    print(f"[corporate] {done}/{len(symbols)} 失败 {len(failed)} "
                          f"| 预计剩余 {eta/60:.1f} 分钟", flush=True)
        conn.commit()
    finally:
        src.close()
    # 汇总由调用方（cmd_corporate）打印：它还要写 collect_log 并决定退出码，
    # 这里再打一遍会让同一条完成信息在终端出现两次。
    return {"ok": len(ok), "failed": failed, "events": n_events}
