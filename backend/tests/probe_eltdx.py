"""实测 eltdx（通达信协议）在本网络下能否拉到全市场数据。

这���对所有网页爬虫源（baostock/腾讯/东财）的替代方案：
TCP 直连行情服务器，README 称单连接并发 80 只/秒、无 IP 限流。
"""
import sys, time, traceback

sys.path.insert(0, "backend")

import eltdx


def full(code):
    c = str(code).zfill(6)
    if c[0] == "6":
        return "sh" + c
    if c[0] in "03":
        return "sz" + c
    return "bj" + c


print(f"eltdx {getattr(eltdx, '__version__', '?')}\n")

t0 = time.time()
try:
    client = eltdx.TdxClient.from_hosts(pool_size=8, timeout=8, probe_hosts=False)
    client.connect()
    print(f"[OK] connect 用时 {time.time()-t0:.1f}s")
except Exception as e:
    print(f"[FAIL] connect: {type(e).__name__}: {e}")
    traceback.print_exc()
    sys.exit(1)

try:
    # 1) 全市场清单
    t0 = time.time()
    lst = client.helpers.latest_stock_list()
    codes = list(lst) if not hasattr(lst, "__len__") else list(lst)
    print(f"[OK] latest_stock_list -> {len(codes)} 只，用时 {time.time()-t0:.1f}s")
    sample = codes[:3]
    print(f"     样例: {sample}")
except Exception as e:
    print(f"[FAIL] latest_stock_list: {type(e).__name__}: {str(e)[:120]}")
    codes = ["600519", "000001", "300750"]

# 2) 后复权日K
for code in ["600519", "000001", "300750"]:
    t0 = time.time()
    try:
        k = client.bars.get(full(code), period="day", adjust="hfq", count=300)
        rows = getattr(k, "bars", k)
        try:
            n = len(rows)
            last = rows[-1] if n else None
        except TypeError:
            n, last = 0, None
        print(f"[OK] bars.get({full(code)}, hfq) -> {n} 根，用时 {time.time()-t0:.2f}s")
        print(f"     末根: {last}")
    except Exception as e:
        print(f"[FAIL] bars.get({full(code)}): {type(e).__name__}: {str(e)[:120]}")

# 3) 吞吐实测：连拉 20 只
print("\n--- 吞吐实测（20 只）---")
pool = [str(c).zfill(6) for c in (codes[:20] if codes else [])]
if not pool:
    pool = ["600519", "000001", "300750"]
t0 = time.time()
ok = 0
for c in pool:
    try:
        k = client.bars.get(full(c), period="day", adjust="hfq", count=300)
        if len(getattr(k, "bars", k) or []):
            ok += 1
    except Exception:
        pass
dt = time.time() - t0
print(f"  {ok}/{len(pool)} 成功，用时 {dt:.1f}s -> {len(pool)/max(dt,1e-9):.1f} 只/秒（单线程）")
print(f"  外推全市场 5563 只单线程 ≈ {5563/max(len(pool)/max(dt,1e-9),1e-9)/60:.1f} 分钟")

try:
    client.close()
except Exception:
    pass
print("\nDONE")
