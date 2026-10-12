"""单发判定：全新进程 → 一次 login → 一条股票日K查询。

用于排除「前一条卡死的查询仍占用连接，污染后续测量」的可能。
用法: python clean_probe.py <start> <end> <fields> [adjust]
"""
import sys, time

sys.path.insert(0, "backend")

start = sys.argv[1] if len(sys.argv) > 1 else "2026-10-05"
end = sys.argv[2] if len(sys.argv) > 2 else "2026-10-12"
fields = sys.argv[3] if len(sys.argv) > 3 else "date,close"
adjust = sys.argv[4] if len(sys.argv) > 4 else "1"

import baostock as bs

t0 = time.time()
lg = bs.login()
print(f"login {lg.error_code} {lg.error_msg} {time.time()-t0:.1f}s", flush=True)

t1 = time.time()
rs = bs.query_history_k_data_plus(
    "sh.600519", fields, start_date=start, end_date=end,
    frequency="d", adjustflag=adjust,
)
n = 0
first = None
while rs.error_code == "0" and rs.next():
    row = rs.get_row_data()
    if first is None:
        first = row
    n += 1
print(f"QUERY start={start} end={end} fields={fields} adjust={adjust} "
      f"-> rows={n} err={rs.error_code} elapsed={time.time()-t1:.1f}s", flush=True)
if first:
    print("first row:", first, flush=True)
