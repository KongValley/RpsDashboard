"""有界数据源可用性测量（Windows 兼容：线程 + result(timeout) 放弃卡死的查询）。

已确认的事实（前几轮诊断）：
  - baostock login() 稳定耗时 ~76-85 秒
  - 单只股票 1.6 年全字段日 K 查询 >90s 未返回（进程空转 878s CPU）
  - 但同一会话里 query_stock_basic / query_stock_industry / query_trade_dates /
    query_history_k_data_plus(指数, date+close) 都成功返回了

本脚本回答：**哪一种「股票历史日 K」取法在本网络下真正可用**。
"""
import sys, time, os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout

sys.path.insert(0, "backend")


def timed(label, fn, timeout=90):
    t0 = time.time()
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn)
    try:
        r = fut.result(timeout=timeout)
        print(f"[OK  ] {label:<44} {time.time()-t0:6.1f}s -> {r}", flush=True)
        return True
    except FTimeout:
        print(f"[SLOW] {label:<44} >{timeout}s 超时放弃", flush=True)
        return False
    except Exception as e:
        print(f"[FAIL] {label:<44} {time.time()-t0:6.1f}s -> {type(e).__name__}: {str(e)[:60]}", flush=True)
        return False
    finally:
        # 卡死的线程无法强杀，放任它在进程退出时被回收
        ex.shutdown(wait=False)


print("=" * 74, flush=True)
print("A. akshare / 东方财富路径（无需 80 秒 login）", flush=True)
print("=" * 74, flush=True)
import akshare as ak
print("akshare", ak.__version__, flush=True)

timed("spot_em(limit=None) 全市场快照",
      lambda: f"rows={len(ak.stock_zh_a_spot_em(limit=None))}", 120)

timed("stock_zh_a_hist 600519 近1年 hfq",
      lambda: f"rows={len(ak.stock_zh_a_hist(symbol='600519', period='daily', start_date='20250912', end_date='20261012', adjust='hfq'))}", 120)

timed("stock_zh_a_hist 600519 近1年 不复权",
      lambda: f"rows={len(ak.stock_zh_a_hist(symbol='600519', period='daily', start_date='20250912', end_date='20261012', adjust=''))}", 120)

timed("stock_zh_a_hist 600519 近1周 hfq",
      lambda: f"rows={len(ak.stock_zh_a_hist(symbol='600519', period='daily', start_date='20261005', end_date='20261012', adjust='hfq'))}", 120)

timed("stock_board_industry_name_em 行业列表",
      lambda: f"rows={len(ak.stock_board_industry_name_em())}", 120)

print()
print("=" * 74, flush=True)
print("B. baostock：登录一次，逐个测股票历史查询", flush=True)
print("=" * 74, flush=True)
import baostock as bs

t0 = time.time()
lg = bs.login()
print(f"login: {lg.error_code} {lg.error_msg}  用时 {time.time()-t0:.1f}s", flush=True)

FLD_FULL = "date,open,high,low,close,volume,amount,turn,pctChg,isST,tradestatus"


def q(code, fields, start, end, adjust="1"):
    def f():
        rs = bs.query_history_k_data_plus(code, fields, start_date=start,
                                          end_date=end, frequency="d", adjustflag=adjust)
        n = 0
        while rs.error_code == "0" and rs.next():
            n += 1
        return f"rows={n} err={rs.error_code}"
    return f


timed("600519 近1年 全字段 hfq", q("sh.600519", FLD_FULL, "2025-10-12", "2026-10-12"), 75)
timed("600519 近1年 仅 date,close hfq", q("sh.600519", "date,close", "2025-10-12", "2026-10-12"), 75)
timed("600519 近1年 仅 date,close 不复权", q("sh.600519", "date,close", "2025-10-12", "2026-10-12", "3"), 75)
timed("600519 近1周 仅 date,close hfq", q("sh.600519", "date,close", "2026-10-05", "2026-10-12"), 75)
timed("000001 近1年 仅 date,close hfq", q("sz.000001", "date,close", "2025-10-12", "2026-10-12"), 75)

print()
print("第二轮：同会话内重复查询（判断是否首查慢、后续快）", flush=True)
timed("600519 近1年 date,close hfq（第二次）",
      q("sh.600519", "date,close", "2025-10-12", "2026-10-12"), 75)
timed("600519 近1年 date,close hfq（第三次）",
      q("sh.600519", "date,close", "2025-10-12", "2026-10-12"), 75)

print("\nDONE", flush=True)
os._exit(0)     # 有卡死线程，用 _exit 强制退出避免 join 挂起
