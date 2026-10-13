"""排查采集链路：单进程直连 eltdx，逐段计时看日K 拉取是否正常。

多进程回填卡死/变慢时先跑这个：只用一条连接、一只股票，先把并发因素排除掉，
就能判断是协议侧不通/慢，还是本地并发调度的问题。
"""
import sys, time
sys.path.insert(0, "backend")

from rpsboard import db
from rpsboard.sources.tdxnet_src import TdxSource

conn = db.connect()
print("rows in daily_bar:", db.count_rows(conn, "daily_bar"))

print("\n--- connect + latest_stock_list ---")
t0 = time.time()
src = TdxSource()
t_conn = time.time() - t0
t0 = time.time()
lst = src.stock_list()
print(f"connect={t_conn:.2f}s  stock_list rows={len(lst)} elapsed={time.time()-t0:.2f}s")

print("\n--- hfq_bars('600000') ---")
t0 = time.time()
df = src.hfq_bars("600000")
print(f"rows={len(df)} elapsed={time.time()-t0:.3f}s")
if not df.empty:
    print(f"区间 {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}")
    print("末行:", df.iloc[-1].to_dict())

src.close()
conn.close()
