"""本地通达信日线数据源（`D:\\tdx\\vipdoc\\*\\lday\\*.day`）。

定位：**补充**腾讯接口拿不到的字段——主要是**成交额**（amount）。
它不是主源，原因有二：
  1. 通达信客户端数据**未更新**：本机最新交易日 2026-09-04，
     比 baostock/腾讯的 2026-10-09 落后约一个月；
  2. 是**不复权**价，要自己算复权因子。

但作为成交额/换手率的来源非常合适——本机读取全市场 9664 个文件
（约 1000 万行）只要 **22 秒**，是纯本地磁盘 IO，不依赖网络。

.day 二进制格式（每条记录固定 32 字节）：
    0-3   date      uint32   YYYYMMDD
    4-7   open      uint32   价格 × 100
    8-11  high      uint32   价格 × 100
    12-15 low       uint32   价格 × 100
    16-19 close     uint32   价格 × 100
    20-23 amount    float32  成交额（元）
    24-27 volume    uint32   成交量（股）
    28-31 reserved  uint32
"""

from __future__ import annotations

import os
import sqlite3
import struct
from pathlib import Path

import pandas as pd

TDX_ROOTS = [
    Path(r"D:\tdx"), Path(r"C:\new_tdx"), Path(r"C:\tdx"),
    Path(r"D:\new_tdx"), Path(r"C:\Program Files\new_tdx"),
]
REC = 32


def find_tdx_root() -> Path | None:
    for root in TDX_ROOTS:
        if (root / "vipdoc").is_dir():
            return root
    return None


def read_day(path: Path) -> pd.DataFrame:
    """解析单个 .day 文件。"""
    data = path.read_bytes()
    n = len(data) // REC
    if n == 0:
        return pd.DataFrame()
    # 一次性 unpack 全部记录，比逐条 struct.iter_unpack 快得多
    recs = struct.iter_unpack("<IIIIIfII", data[: n * REC])
    rows = []
    for d, o, h, l, c, amt, vol, _ in recs:
        if d == 0:
            break
        if not (19900101 <= d <= 21001231):
            continue
        rows.append((
            f"{d // 10000:04d}-{d // 100 % 100:02d}-{d % 100:02d}",
            o / 100.0, h / 100.0, l / 100.0, c / 100.0, amt, float(vol),
        ))
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(
        rows, columns=["date", "open", "high", "low", "close", "amount", "volume"]
    )


def scan_market(root: Path | None = None) -> dict[str, pd.DataFrame]:
    """扫描全市场 .day 文件，返回 {6位代码: DataFrame}。"""
    root = root or find_tdx_root()
    if root is None:
        return {}
    out: dict[str, pd.DataFrame] = {}
    vip = root / "vipdoc"
    for market in ("sh", "sz", "bj"):
        lday = vip / market / "lday"
        if not lday.is_dir():
            continue
        for f in lday.glob("*.day"):
            sym = f.stem[-6:]
            df = read_day(f)
            if not df.empty:
                out[sym] = df
    return out


def fill_amount(conn: sqlite3.Connection, root: Path | None = None) -> int:
    """用本地通达信数据补 daily_bar 缺失的 amount（成交额）。

    只 UPDATE 不 INSERT —— 绝不因为本地数据而覆盖/新增价格行，
    因为本地数据比腾讯旧一个月，价格以腾讯为准。
    """
    root = root or find_tdx_root()
    if root is None:
        print("[tdx] 未找到本地通达信目录，跳过成交额补全")
        return 0

    print(f"[tdx] 扫描 {root / 'vipdoc'} ...")
    store = scan_market(root)
    if not store:
        print("[tdx] 未解析到任何 .day 数据")
        return 0

    total_latest = max(
        (df["date"].iloc[-1] for df in store.values() if not df.empty), default="—"
    )
    print(f"[tdx] 解析 {len(store)} 只，最新交易日 {total_latest}")

    frames = []
    for sym, df in store.items():
        frames.append(df.assign(symbol=sym)[["symbol", "date", "amount"]])
    if not frames:
        return 0
    amt = pd.concat(frames, ignore_index=True)
    amt = amt[amt["amount"].notna() & (amt["amount"] > 0)]
    amt = amt.drop_duplicates(subset=["symbol", "date"], keep="last")

    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _tdx_amt "
                 "(symbol TEXT, date TEXT, amount REAL, PRIMARY KEY(symbol,date))")
    conn.execute("DELETE FROM _tdx_amt")
    amt.to_sql("_tdx_amt", conn, if_exists="append", index=False, chunksize=5000)

    cur = conn.execute(
        "UPDATE daily_bar SET amount = ("
        "  SELECT a.amount FROM _tdx_amt a"
        "  WHERE a.symbol = daily_bar.symbol AND a.date = daily_bar.date"
        ") WHERE amount IS NULL AND EXISTS ("
        "  SELECT 1 FROM _tdx_amt a"
        "  WHERE a.symbol = daily_bar.symbol AND a.date = daily_bar.date"
        ")"
    )
    updated = cur.rowcount or 0
    conn.commit()
    conn.execute("DROP TABLE IF EXISTS _tdx_amt")
    print(f"[tdx] 补全成交额 {updated} 行")
    return updated
