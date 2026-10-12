"""交叉验证：腾讯 hfq 收盘价 vs baostock hfq 收盘价。

两者都是后复权，理论上在同一天应当**完全一致**（或极接近）。
若比值是常数 → 口径一致，只是基准不同（仍可用于算收益率）；
若比值随时间漂移 → 复权口径有问题，不能用来算 RPS。
"""
import sys
sys.path.insert(0, "backend")

import pandas as pd
from rpsboard import db
from rpsboard.sources.tencent_src import fetch_hfq_day

conn = db.connect()

for s in ["600519", "000001", "300750", "601398", "000002", "688981"]:
    bs = pd.read_sql_query(
        "SELECT date, close FROM daily_bar WHERE symbol = ? ORDER BY date",
        conn, params=(s,),
    )
    if bs.empty:
        print(f"{s}: 库中无 baostock 数据")
        continue

    tx = fetch_hfq_day(s)
    if tx.empty:
        print(f"{s}: 腾讯无数据")
        continue
    tx = tx[["date", "close"]].rename(columns={"close": "tx"})

    m = bs.merge(tx, on="date")
    m = m[m["tx"] > 0]
    if len(m) < 5:
        print(f"{s}: 重叠仅 {len(m)} 天")
        continue
    m["ratio"] = m["tx"] / m["close"]
    r = m["ratio"]
    print(f"\n{s}  重叠 {len(m)} 个交易日  ({m['date'].iloc[0]} ~ {m['date'].iloc[-1]})")
    print(f"   baostock hfq 末值 {m['close'].iloc[-1]:>12.4f}")
    print(f"   腾讯   hfq 末值 {m['tx'].iloc[-1]:>12.4f}")
    print(f"   比值 min={r.min():.6f} max={r.max():.6f} std={r.std():.2e}")
    # 若比值几乎恒定 -> 口径一致；若漂移 -> 有问题
    drift = (r.max() - r.min()) / r.mean()
    print(f"   漂移幅度 {drift*100:.4f}%  -> "
          f"{'口径一致 ✅' if drift < 0.01 else '口径不一致 ❌'}")

conn.close()
