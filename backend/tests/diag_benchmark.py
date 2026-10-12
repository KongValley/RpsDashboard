"""检查 benchmark_bar：年化波动 202% 明显不可能，必��是数据坏了。"""
import sys
sys.path.insert(0, "backend")

import pandas as pd
from rpsboard import db

conn = db.connect()

for sym in ("000300", "000001", "399001"):
    df = pd.read_sql_query(
        "SELECT date, close FROM benchmark_bar WHERE symbol=? ORDER BY date",
        conn, params=(sym,))
    if df.empty:
        print(f"{sym}: 无数据")
        continue
    df["date"] = pd.to_datetime(df["date"])
    r = df["close"].pct_change()
    print(f"\n=== {sym}  {len(df)} 行  {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}")
    print(f"    close 首={df['close'].iloc[0]:.2f} 末={df['close'].iloc[-1]:.2f} "
          f"min={df['close'].min():.2f} max={df['close'].max():.2f}")
    print(f"    日收益 std={r.std():.6f} -> 年化波动 {r.std()*252**0.5*100:.2f}%")
    bad = df[r.abs() > 0.11]
    print(f"    单日涨跌幅 >11% 的异常行: {len(bad)}")
    if not bad.empty:
        print(bad.head(10).to_string())
    # 看跳变最大的几行
    df["chg"] = df["close"].pct_change()
    print("    跳变最大 Top5:")
    print(df.reindex(df["chg"].abs().nlargest(5).index)[
        ["date", "close", "chg"]].to_string(index=False))

conn.close()
