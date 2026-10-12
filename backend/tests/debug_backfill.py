"""排查多进程采集卡死：单独跑一个 worker，看 baostock 是否正常返回。"""
import sys, time, sqlite3
sys.path.insert(0, "backend")
from rpsboard import db, config
from rpsboard.sources import baostock_src

print("rows in daily_bar:", db.count_rows(db.connect(), "daily_bar"))

print("\n--- single-process direct query (sh.600000) ---")
t0 = time.time()
import baostock as bs
lg = bs.login()
print("login:", lg.error_code, lg.error_msg, f"{time.time()-t0:.1f}s")
t0 = time.time()
rs = bs.query_history_k_data_plus(
    "sh.600000", "date,open,high,low,close,volume,amount,turn,pctChg,isST,tradestatus",
    start_date="2025-01-01", end_date="2026-10-12", frequency="d", adjustflag="1")
n = 0
while rs.error_code == "0" and rs.next():
    n += 1
print(f"rows={n} error={rs.error_code} elapsed={time.time()-t0:.1f}s")
bs.logout()

print("\n--- worker function direct call ---")
t0 = time.time()
res = baostock_src._fetch_batch(([("600000", "sh.600000", "2025-01-01", "2026-10-12")], 3))
print(f"worker rows={len(res)} elapsed={time.time()-t0:.1f}s")
