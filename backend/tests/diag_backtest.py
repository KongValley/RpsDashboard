"""诊断回测：净值曲线为何出现 -99.6% 回撤却总收益 +119%？

重点检查：
  1. 净值曲线逐点，看暴跌发生在哪一段
  2. 持仓股票里是否有价格异常（长期停牌 / 复牌跳空 / 单日暴涨暴跌）
  3. 前瞻收益的极端值分布
"""
import sys
sys.path.insert(0, "backend")

import numpy as np
import pandas as pd
from rpsboard import db
from rpsboard.analytics import backtest as engine
from rpsboard.analytics.matrix import (
    build_close_matrix, build_pctchg_matrix, build_st_flags, build_tradable_matrix,
)

conn = db.connect()
res = engine.run_backtest(conn, period=60, threshold=80, hold=20)
eq = pd.DataFrame(res.equity_curve)
eq["chg"] = eq["equity"].pct_change()

print("=== 净值曲线（每点：日期/净值/持仓数/单期涨跌）===")
for _, r in eq.iterrows():
    chg = f"{r['chg']*100:+8.2f}%" if pd.notna(r["chg"]) else "     --  "
    flag = "  <<< 暴跌" if (pd.notna(r["chg"]) and r["chg"] < -0.3) else ""
    print(f"  {r['date']}  {r['equity']:>10.4f}  持仓{r['holdings']:>4}  {chg}{flag}")

worst = eq.nsmallest(3, "chg")
print(f"\n最差 3 个调仓期: {worst['date'].tolist()}")

# ── 找异常个股 ───────────────────────────────────────────────────
print("\n=== 检查持仓池中的价格异常 ===")
long = pd.read_sql_query(
    "SELECT symbol, date, close, pct_chg, tradestatus FROM daily_bar", conn)
close_w = build_close_matrix(long)
pct_w = build_pctchg_matrix(long)

# 单日涨跌幅极值
moves = long.pct_chg.abs().nlargest(8)
print("全库单日涨跌幅 Top8:")
for _, r in moves.iterrows():
    print(f"  {r['symbol']} {r['date']} pct={r['pct_chg']:.2f}% close={r['close']:.2f}")

# 20 日收益率极值
ret20 = (close_w / close_w.shift(20) - 1)
extreme = ret20.stack().abs().nlargest(8)
print("\n20日收益率极值 Top8:")
for (d, s), v in extreme.items():
    print(f"  {s} {d} ret20={v*100:.1f}%")

# 长停牌：连续相同收盘价
print("\n=== 长期无变化（疑似长期停牌）的股票 ===")
flat = []
for sym in close_w.columns[:200]:
    c = close_w[sym].dropna()
    if len(c) < 100:
        continue
    same = (c.diff() == 0).sum()
    if same > 30:
        flat.append((sym, same, len(c)))
flat.sort(key=lambda x: -x[1])
print(f"  200 只样本中有 {len(flat)} 只出现 >30 天收盘价不变")
for f in flat[:5]:
    print(f"    {f[0]}  {f[1]}/{f[2]} 天无变化")

# 具体某天的持仓
print("\n=== 暴跌日前后的持仓明细 ===")
bad_date = worst.iloc[0]["date"]
print(f"  最差调仓日: {bad_date}")

conn.close()
