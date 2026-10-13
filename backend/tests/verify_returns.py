"""验证：库中落库价格与现场重新从 eltdx 拉取的价格，在 **N 日收益率** 上是否一致。

为什么比绝对价更重要：
  RPS = ret_N 的横截面百分位，而 ret_N = close[t]/close[t-N] - 1。
  绝对后复权基准不同无所谓，只要同一只票内部的价格序列是「连续复权」的，
  收益率就应当一致。

  真正会导致 RPS 出错的是**除权日处理时点不一致**：
  库里的历史与现场重新拉取的历史若对同一次除权处理在不同日期，
  跨除权日的收益率就会失真。所以这里直接比 ret20/ret60/ret120/ret250。
"""
import sys
sys.path.insert(0, "backend")

import numpy as np
import pandas as pd
from rpsboard import db
from rpsboard.sources.tdxnet_src import TdxSource

conn = db.connect()
src = TdxSource()
PERIODS = (20, 60, 120, 250)

print("逐只对比 ret_N（库中落库价 vs 现场 eltdx 重新拉取，仅比较重叠区间）\n")

summary = []
for s in ["000011", "000993", "001368", "001326", "000560", "001216",
          "000678", "001258", "000710", "000004"]:
    bs = pd.read_sql_query(
        "SELECT date, close FROM daily_bar WHERE symbol=? ORDER BY date",
        conn, params=(s,),
    )
    if len(bs) < 260:
        continue
    tx = src.hfq_bars(s)
    if tx.empty:
        continue

    bs = bs.rename(columns={"close": "bs"})
    tx = tx[["date", "close"]].rename(columns={"close": "live"})
    m = bs.merge(tx, on="date").sort_values("date").reset_index(drop=True)
    if len(m) < 260:
        print(f"{s}: 重叠仅 {len(m)} 天，跳过")
        continue

    rows = []
    for n in PERIODS:
        rb = m["bs"].pct_change(n)
        rt = m["live"].pct_change(n)
        d = (rb - rt).abs().dropna()
        if d.empty:
            rows.append((n, None, None, None))
            continue
        rel = (d / rb.abs().reindex(d.index)).max()
        rows.append((n, float(d.mean()), float(rel), int((d / rb.abs().reindex(d.index) > 1e-4).sum())))

    print(f"{s}  重叠 {len(m)} 天  ({m['date'].iloc[0]} ~ {m['date'].iloc[-1]})")
    for n, amean, relmax, badn in rows:
        if amean is None:
            print(f"   ret{n:<4} 无重叠")
        else:
            verdict = "OK" if relmax < 1e-3 else "CHECK"
            print(f"   ret{n:<4} 平均绝对差 {amean:.6f}  最大相对差 {relmax:.6f}  "
                  f"超0.01%的天数 {badn}  [{verdict}]")
    summary.append((s, rows))

print("\n" + "=" * 64)
worst = []
for s, rows in summary:
    for n, amean, relmax, badn in rows:
        if relmax is not None:
            worst.append((relmax, s, n, badn))
worst.sort(reverse=True)
print("最差的 5 个 (期数,股票) 的最大相对偏差:")
for relmax, s, n, badn in worst[:5]:
    print(f"   {s} ret{n:<4} 最大相对差 {relmax:.6f}  超阈值天数 {badn}")

src.close()
conn.close()
