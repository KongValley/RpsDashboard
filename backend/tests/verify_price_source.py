"""交叉验证：库中已落库的后复权收盘价 vs **现场重新从 eltdx 拉取**的收盘价。

同一只票、同一个源重复取数，两个序列应当**完全一致**（比值恒为 1.0）。
若比值是常数但 ≠1 → 库里的历史被换过源（基准不同），收益率仍可用；
若比值随时间漂移 → 复权口径有问题，不能用来算 RPS。
"""
import sys
sys.path.insert(0, "backend")

import pandas as pd
from rpsboard import db
from rpsboard.sources.tdxnet_src import TdxSource

conn = db.connect()
src = TdxSource()

for s in ["600519", "000001", "300750", "601398", "000002", "688981"]:
    bs = pd.read_sql_query(
        "SELECT date, close FROM daily_bar WHERE symbol = ? ORDER BY date",
        conn, params=(s,),
    )
    if bs.empty:
        print(f"{s}: 库中无数据")
        continue

    tx = src.hfq_bars(s)
    if tx.empty:
        print(f"{s}: eltdx 无数据")
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
    print(f"   库中    hfq 末值 {m['close'].iloc[-1]:>12.4f}")
    print(f"   eltdx   hfq 末值 {m['tx'].iloc[-1]:>12.4f}")
    print(f"   比值 min={r.min():.6f} max={r.max():.6f} std={r.std():.2e}")
    # 若比值几乎恒定 -> 口径一致；若漂移 -> 有问题
    drift = (r.max() - r.min()) / r.mean()
    print(f"   漂移幅度 {drift*100:.4f}%  -> "
          f"{'口径一致 ✅' if drift < 0.01 else '口径不一致 ❌'}")

src.close()
conn.close()
