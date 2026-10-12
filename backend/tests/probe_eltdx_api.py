"""探测 eltdx 的 API 全貌，避免接入时猜错方法名。"""
import sys, time
sys.path.insert(0, "backend")

import eltdx

print("eltdx", getattr(eltdx, "__version__", "?"))
print("顶层导出:", [n for n in dir(eltdx) if not n.startswith("_")])

client = eltdx.TdxClient.from_hosts(pool_size=8, timeout=8, probe_hosts=False)
client.connect()

for grp in ("helpers", "bars", "minutes", "quotes", "search", "finance", "meta"):
    obj = getattr(client, grp, None)
    if obj is None:
        continue
    meths = [m for m in dir(obj) if not m.startswith("_") and callable(getattr(obj, m, None))]
    print(f"\nclient.{grp}: {meths}")

print("\n=== 快照接口试调用 ===")
t0 = time.time()
try:
    prof = client.helpers.stock_profile_table()
    print(f"stock_profile_table -> 类型 {type(prof).__name__}，用时 {time.time()-t0:.1f}s")
    try:
        print(f"  行数 {len(prof)}")
        first = prof[0] if len(prof) else None
        print(f"  首行: {first}")
        cols = getattr(prof, "columns", None)
        if cols is not None:
            print(f"  列: {list(cols)}")
    except Exception as e:
        print(f"  读取失败: {e}")
except Exception as e:
    print(f"stock_profile_table 失败: {type(e).__name__}: {str(e)[:150]}")

print("\n=== 指数 K 线 ===")
for code in ("sh000300", "sh000001", "sz399001"):
    try:
        k = client.bars.get(code, period="day", adjust="hfq", count=5)
        bars = getattr(k, "bars", k)
        print(f"  {code}: {len(bars)} 根, 末根 close={bars[-1].close if len(bars) else '-'}")
    except Exception as e:
        print(f"  {code}: 失败 {type(e).__name__}: {str(e)[:80]}")

print("\n=== count=800 能否取更长历史 ===")
for cnt in (500, 800, 1200):
    try:
        k = client.bars.get("sh600519", period="day", adjust="hfq", count=cnt)
        bars = getattr(k, "bars", k)
        if len(bars):
            print(f"  count={cnt} -> {len(bars)} 根, 最早 {bars[0].time.date()}")
        else:
            print(f"  count={cnt} -> 0 根")
    except Exception as e:
        print(f"  count={cnt} -> 失败 {str(e)[:60]}")

try:
    client.close()
except Exception:
    pass
print("\nDONE")
