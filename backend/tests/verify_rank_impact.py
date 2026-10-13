"""验证：用「库中已落库的价格」与「现场重新从 eltdx 拉取的价格」分别算 RPS，
看横截面排名差异到底有多大。

绝对价不同无所谓（基准可能不同），收益率有 2% 量级偏差也未必致命，
关键是**横截面排名**是否稳定——排名稳，说明落库链路与增量/重算没有把
价格序列弄歪；排名大面积位移，先怀疑落库数据而不是策略。
"""
import sys
sys.path.insert(0, "backend")

import numpy as np
import pandas as pd
from rpsboard import db
from rpsboard.sources.tdxnet_src import TdxSource

PERIODS = (20, 60, 120, 250)

conn = db.connect()
src = TdxSource()
syms = [r[0] for r in conn.execute(
    "SELECT DISTINCT symbol FROM daily_bar ORDER BY symbol")][:60]
print(f"库中样本 {len(syms)} 只，开始对比\n")

# ── 库中落库价 ──────────────────────────────────────────────────
bs = pd.read_sql_query(
    "SELECT symbol, date, close FROM daily_bar WHERE tradestatus=1", conn)
bs_w = bs.pivot_table(index="date", columns="symbol", values="close",
                      aggfunc="last").sort_index().ffill()

# ── 现场 eltdx 重新拉取 ─────────────────────────────────────────
frames = []
for s in syms:
    d = src.hfq_bars(s)
    if not d.empty:
        d = d.assign(symbol=s)[["date", "symbol", "close"]]
        frames.append(d)
tx_long = pd.concat(frames, ignore_index=True)
tx_w = tx_long.pivot_table(index="date", columns="symbol", values="close",
                           aggfunc="last").sort_index().ffill()

common_dates = sorted(set(bs_w.index) & set(tx_w.index))
common_syms = sorted(set(bs_w.columns) & set(tx_w.columns))
bs_w = bs_w.loc[common_dates, common_syms]
tx_w = tx_w.loc[common_dates, common_syms]
print(f"对齐后: {len(common_dates)} 个交易日 × {len(common_syms)} 只股票")
print(f"日期范围 {common_dates[0]} ~ {common_dates[-1]}\n")

for n in PERIODS:
    rb = (bs_w / bs_w.shift(n) - 1).rank(axis=1, pct=True) * 100
    rt = (tx_w / tx_w.shift(n) - 1).rank(axis=1, pct=True) * 100
    d = common_dates[-1]
    a = rb.loc[d].dropna()
    b = rt.loc[d].dropna()
    idx = sorted(set(a.index) & set(b.index))
    a, b = a.loc[idx], b.loc[idx]
    if len(idx) < 10:
        continue

    pear = a.corr(b)
    spear = a.rank().corr(b.rank())
    rank_a = a.rank(ascending=False)
    rank_b = b.rank(ascending=False)
    rank_gap = (rank_a - rank_b).abs()

    print(f"--- RPS{n} @ {d}  (样本 {len(idx)}) ---")
    print(f"   相关系数 Pearson={pear:.4f}  Spearman={spear:.4f}")
    print(f"   排名位移中位数 {rank_gap.median():.1f} 名, "
          f"90分位 {rank_gap.quantile(0.9):.1f} 名, 最大 {rank_gap.max():.0f} 名")
    for topk in (10, 20, 50):
        if len(idx) < topk:
            continue
        ta = set(a.nlargest(topk).index)
        tb = set(b.nlargest(topk).index)
        ov = len(ta & tb)
        print(f"   Top{topk:<3} 重合 {ov}/{topk} = {ov/topk*100:.0f}%")
    # RPS 分档一致性
    ba = pd.cut(a, [0,50,70,80,90,100.01], labels=[0,1,2,3,4])
    bb = pd.cut(b, [0,50,70,80,90,100.01], labels=[0,1,2,3,4])
    print(f"   RPS 分档完全一致的股票: {(ba.values == bb.values).mean()*100:.0f}%\n")

src.close()
conn.close()
