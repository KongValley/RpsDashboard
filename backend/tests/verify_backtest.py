"""修复后复验回测：检查净值曲线量级是否合理、分档结论是否稳定。"""
import sys
sys.path.insert(0, "backend")

import pandas as pd
from rpsboard import db
from rpsboard.analytics import backtest as engine

conn = db.connect()

for th in (80, 90):
    res = engine.run_backtest(conn, period=60, threshold=th, hold=20)
    m = res.metrics
    print(f"--- RPS60 >= {th} ---")
    print(f"  总收益 {m['total_return']*100:>8.2f}%   年化 {m['annualized']*100:>7.2f}%"
          f"   波动 {m['volatility']*100:>6.2f}%   回撤 {m['max_drawdown']*100:>7.2f}%"
          f"   夏普 {m['sharpe']:>6.2f}   调仓 {m['days']} 次  ({m['years']} 年)")
    eq = [c["equity"] for c in res.equity_curve]
    jumps = [eq[i] / eq[i - 1] for i in range(1, len(eq))]
    print(f"  单期净值变化: 最小 {min(jumps):.3f}  最大 {max(jumps):.3f}"
          f"  中位 {sorted(jumps)[len(jumps)//2]:.3f}")
    print("  分档前瞻收益:")
    for b in res.buckets:
        ar = f"{b['avg_forward_return']*100:.2f}%" if b["avg_forward_return"] is not None else "-"
        wr = f"{b['win_rate']*100:.1f}%" if b["win_rate"] is not None else "-"
        print(f"    {b['range']:<9} n={b['samples']:>7}  均值 {ar:>8}  胜率 {wr:>7}")
    print()

print("--- 基准对照（沪深300 同期）---")
# 注意：必须按 symbol 过滤，否则三条指数序列会混排成锯齿，
# 收益率乱跳算出 200%+ 的年化波动。
bench = pd.read_sql_query(
    "SELECT date, close FROM benchmark_bar WHERE symbol=? ORDER BY date",
    conn, params=("000300",))
if not bench.empty:
    bench["date"] = pd.to_datetime(bench["date"])
    r = bench["close"].pct_change().dropna()
    yrs = (bench["date"].iloc[-1] - bench["date"].iloc[0]).days / 365
    tot = bench["close"].iloc[-1] / bench["close"].iloc[0] - 1
    ann = (1 + tot) ** (1 / max(yrs, 0.01)) - 1
    print(f"  区间 {bench['date'].iloc[0].date()} ~ {bench['date'].iloc[-1].date()}")
    print(f"  累计 {tot*100:.2f}%  年化 {ann*100:.2f}%  年化波动 {r.std()*252**0.5*100:.2f}%")

conn.close()
