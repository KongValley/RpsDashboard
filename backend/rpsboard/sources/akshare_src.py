"""akshare 数据源：当日全市场快照（仅用于展示增强，不参与 RPS 计算）。

**为什么单独一层**：akshare 底层是爬东方财富，WAF 严。社区实测暴力并发
（ThreadPoolExecutor max_workers=10）IP 封禁概率 > 92%。因此本模块：
- 永远单线程串行调用，绝不并发；
- **永不向调用方抛异常**——失败只置 stale 标记并返回空结果，
  让看板以「缺快照字段但 RPS 正常」的降级形态继续服务。

**为什么不用它算 RPS**：stock_zh_a_spot_em 返回的「最新价」是**未复权**价，
与 baostock 的后复权链混用会在除权除息日污染区间收益率。RPS 链 100% 走 baostock。
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

import pandas as pd

from .. import db

#: akshare 返回中文列名，这里映射为内部英文列名
COLUMN_MAP = {
    "代码": "symbol",
    "名称": "name",
    "最新价": "price",
    "涨跌幅": "pct_chg",
    "成交额": "amount",
    "换手率": "turnover_rate",
    "量比": "volume_ratio",
    "总市值": "total_mv",
    "流通市值": "float_mv",
}

#: 全 A 股应有数量级，低于此值说明拿到的是被截断的第一页
EXPECTED_MIN_ROWS = 5000


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """中文列名 → 内部列名，并做数值清洗。"""
    rename = {k: v for k, v in COLUMN_MAP.items() if k in df.columns}
    out = df.rename(columns=rename)
    keep = [c for c in set(rename.values()) if c in out.columns]
    out = out[keep].copy()
    out["symbol"] = out["symbol"].astype(str).str.zfill(6)
    for c in ("price", "pct_chg", "amount", "turnover_rate", "volume_ratio",
              "total_mv", "float_mv"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)


def _fetch_full_market() -> pd.DataFrame:
    """拿全市场快照。

    注意 akshare 1.19.1 的 `stock_zh_a_spot_em()` **没有任何参数**，
    社区资料里说的 `limit=None` 是旧版本/其它接口的写法——直接传参会 TypeError。
    这里先按签名探测，再决定要不要传参，避免每次都靠异常控制流。
    """
    import inspect

    import akshare as ak

    fn = ak.stock_zh_a_spot_em
    accepts_limit = "limit" in inspect.signature(fn).parameters

    last_err: Exception | None = None
    attempts = (
        (lambda: fn(limit=None), lambda: fn())
        if accepts_limit
        else (fn,)
    )
    for attempt in attempts:
        try:
            df = attempt()
            if df is not None and not df.empty:
                return _normalize(df)
            last_err = RuntimeError("stock_zh_a_spot_em 返回空 DataFrame")
        except TypeError as exc:      # 该版本不接受该参数
            last_err = exc
            continue
        except Exception as exc:      # noqa: BLE001
            last_err = exc
            break

    raise RuntimeError(f"akshare 快照获取失败: {last_err}")


def fetch_spot(conn: sqlite3.Connection, trade_date: str | None = None) -> dict[str, Any]:
    """采集并落库当日快照。

    **永不抛异常**：任何失败都记入 collect_log 并返回 stale 结果。
    """
    run_id = db.start_run(conn, task="spot", source="akshare")
    d = trade_date or time.strftime("%Y-%m-%d")

    try:
        df = _fetch_full_market()
        if len(df) < EXPECTED_MIN_ROWS:
            # 明显被截断，按失败处理，避免把残缺数据当全市场写进去
            raise RuntimeError(
                f"仅取到 {len(df)} 行（预期 >{EXPECTED_MIN_ROWS}），疑似接口分页截断"
            )

        payload = df.assign(date=d)[[
            "date", "symbol", "price", "pct_chg", "amount",
            "turnover_rate", "volume_ratio", "total_mv", "float_mv",
        ]]
        written = db.replace_date_partition(conn, "spot_snapshot", [d], payload)
        db.finish_run(conn, run_id, "success", written, f"{d} 快照 {written} 行")
        return {"ok": True, "rows": written, "date": d}

    except Exception as exc:  # noqa: BLE001 - 刻意吞掉全部异常
        db.finish_run(conn, run_id, "failed", 0, str(exc))
        print(f"[spot] 采集失败（不影响看板）：{exc}")
        return {"ok": False, "rows": 0, "date": None, "error": str(exc)}


def latest_spot_date(conn: sqlite3.Connection) -> str | None:
    if not db.table_exists(conn, "spot_snapshot"):
        return None
    row = conn.execute("SELECT MAX(date) AS d FROM spot_snapshot").fetchone()
    return row["d"] if row and row["d"] else None


def is_spot_stale(conn: sqlite3.Connection, last_bar_date: str | None) -> bool:
    """快照是否过期：最新快照日期早于最新日 K 日期。

    注意节假日/非交易日不算过期——以日 K 为基准比较即可。
    """
    spot = latest_spot_date(conn)
    if spot is None:
        return True
    if last_bar_date is None:
        return False
    return spot < last_bar_date
