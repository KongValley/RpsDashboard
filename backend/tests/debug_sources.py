"""数据源连通性与速度诊断。

背景：本机 baostock login 约 85s，单只股票 1.6 年历史查询 >90s 未返回，
与计划假设（12 分钟回填全市场）严重不符。本脚本定量对比各备选路径。

只读操作，不写数据库。
"""
import sys, time, traceback

sys.path.insert(0, "backend")


def timed(label, fn):
    t0 = time.time()
    try:
        r = fn()
        print(f"[OK  ] {label:<52} {time.time()-t0:6.1f}s  -> {r}")
    except Exception as e:
        print(f"[FAIL] {label:<52} {time.time()-t0:6.1f}s  -> {type(e).__name__}: {str(e)[:90]}")
    sys.stdout.flush()


print("=" * 78)
print("A. baostock：短区间 vs 长区间（区分「网络往返」与「数据量」）")
print("=" * 78)

import baostock as bs

t0 = time.time()
lg = bs.login()
print(f"login: {lg.error_code} {lg.error_msg}  用时 {time.time()-t0:.1f}s")
sys.stdout.flush()


def bs_query(start, end, label):
    def f():
        t = time.time()
        rs = bs.query_history_k_data_plus(
            "sh.600000", "date,open,high,low,close,volume,amount,turn,pctChg,isST,tradestatus",
            start_date=start, end_date=end, frequency="d", adjustflag="1")
        n = 0
        while rs.error_code == "0" and rs.next():
            n += 1
        return f"rows={n} err={rs.error_code} inner={time.time()-t:.1f}s"
    return timed(label, f)


bs_query("2026-09-01", "2026-10-12", "sh.600000 近 1.5 个月")
bs_query("2026-01-01", "2026-10-12", "sh.600000 近 10 个月")
bs_query("2024-09-01", "2026-10-12", "sh.600000 近 2 年(全字段)")

# 轻字段对照：只取 date,close
def light():
    t = time.time()
    rs = bs.query_history_k_data_plus(
        "sh.600000", "date,close", start_date="2024-09-01", end_date="2026-10-12",
        frequency="d", adjustflag="1")
    n = 0
    while rs.error_code == "0" and rs.next():
        n += 1
    return f"rows={n} inner={time.time()-t:.1f}s"

timed("sh.600000 近 2 年（仅 date,close 轻字段）", light)

print(f"\nlogout ...", flush=True)
try:
    bs.logout()
except Exception:
    pass

print()
print("=" * 78)
print("B. akshare：东方财富路径（备选主源）")
print("=" * 78)

import akshare as ak
print("akshare", ak.__version__)

timed("stock_zh_a_hist 600519 近1年 hfq",
      lambda: f"rows={len(ak.stock_zh_a_hist(symbol='600519', period='daily', start_date='20250912', end_date='20261012', adjust='hfq'))}")

timed("stock_zh_a_spot_em(limit=None) 全市场快照",
      lambda: f"rows={len(ak.stock_zh_a_spot_em(limit=None))}")

timed("stock_board_industry_name_em 行业列表",
      lambda: f"rows={len(ak.stock_board_industry_name_em())}")

print()
print("=" * 78)
print("C. 网络基线")
print("=" * 78)
import urllib.request, time as _t
for url in ["https://pypi.org/simple/baostock/", "http://baostock.com/"]:
    def ping(u=url):
        t = _t.time()
        try:
            with urllib.request.urlopen(u, timeout=15) as r:
                r.read(200)
            return f"http={r.status} ping={(_t.time()-t)*1000:.0f}ms"
        except Exception as e:
            return f"{type(e).__name__}: {str(e)[:60]}"
    timed(f"GET {url}", ping)

print("\nDONE")
