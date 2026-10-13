"""验证腾讯财经日K 接口是否满足 RPS 看板的全部需求。

必须满足：
  1. 后复权（hfq）——RPS 必须用后复权价，否则除权日会污染收益率
  2. 一次能取足够长的历史（≥250 交易日）
  3. 单次响应速度（决定全市场回填总时长）
  4. 能批量取多只（决定并发策略）
"""
import json, time, sys

sys.path.insert(0, "backend")            # 从仓库根目录运行
from rpsboard import config              # noqa: E402
import requests

S = requests.Session()
S.trust_env = False          # 直连，实测直连也通
H = {"User-Agent": "Mozilla/5.0"}
BASE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

print("=== 1. 后复权 hfq 是否支持 ===")
for adj, label in [("", "不复权 day"), ("qfq", "前复权 qfq"), ("hfq", "后复权 hfq")]:
    p = f"sh600519,day,2024-09-01,2026-10-12,640,{adj}" if adj else \
        "sh600519,day,2024-09-01,2026-10-12,640"
    t0 = time.time()
    try:
        r = S.get(BASE, params={"param": p}, headers=H, timeout=15)
        j = r.json()
        d = (j.get("data") or {}).get("sh600519") or {}
        keys = [k for k in d if "day" in k]
        arr = d.get(keys[0]) if keys else []
        last = arr[-1] if arr else None
        print(f"  {label:<12} keys={keys} 行数={len(arr)} 用时={time.time()-t0:.2f}s")
        print(f"               末行={last}")
    except Exception as e:
        print(f"  {label:<12} 失败 {type(e).__name__}: {str(e)[:70]}")

print("\n=== 2. 单次最大条数（count 参数）===")
for cnt in [640, 1000, 2000, 3000]:
    p = f"sh600519,day,2020-01-01,2026-10-12,{cnt},hfq"
    try:
        j = S.get(BASE, params={"param": p}, headers=H, timeout=15).json()
        d = (j.get("data") or {}).get("sh600519") or {}
        arr = d.get("hfqday") or d.get("day") or []
        print(f"  count={cnt:<5} 实际返回 {len(arr)} 行，"
              f"区间 {arr[0][0] if arr else '-'} ~ {arr[-1][0] if arr else '-'}")
    except Exception as e:
        print(f"  count={cnt:<5} 失败 {type(e).__name__}")

print("\n=== 3. 批量多只股票 ===")
codes = ["sh600519", "sz000001", "sz000002", "sz300750", "sh688981"]
for n in (1, 3, 5):
    ps = ";".join(f"{c},day,2025-01-01,2026-10-12,640,hfq" for c in codes[:n])
    try:
        t0 = time.time()
        j = S.get(BASE, params={"param": ps}, headers=H, timeout=20).json()
        got = {k: len(v.get("hfqday") or v.get("day") or [])
               for k, v in (j.get("data") or {}).items()}
        print(f"  {n} 只/次  用时={time.time()-t0:.2f}s  返回={got}")
    except Exception as e:
        print(f"  {n} 只/次  失败 {type(e).__name__}: {str(e)[:60]}")

print("\n=== 4. 字段结构（前复权日K的一行）===")
try:
    j = S.get(BASE, params={"param": "sz000001,day,2026-09-01,2026-10-12,30,hfq"},
              headers=H, timeout=15).json()
    d = (j.get("data") or {}).get("sz000001") or {}
    arr = d.get("hfqday") or d.get("day") or []
    if arr:
        print("  样例行:", arr[-1])
        print("  字段位: date open close high low volume(?) ...")
except Exception as e:
    print("  失败", e)

print(f"\n=== 5. 通达信本地数据 {config.TDX_DIR} ===")
import os
for sub in ["vipdoc", "T0002", "T0003"]:
    p = config.TDX_DIR / sub
    if os.path.isdir(p):
        items = os.listdir(p)
        print(f"  [有] {p}  ({len(items)} 项)  例: {items[:5]}")
    else:
        print(f"  [无] {p}")
# 找 .day 文件（通达信日线）
hits = []
for root in (config.TDX_DIR,):
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirpath:
            continue
        days = [f for f in filenames if f.lower().endswith(".day")]
        if days:
            hits.append((dirpath, len(days), days[:3]))
        if len(hits) >= 5:
            break
for h in hits:
    print(f"  {h[0]}  -> {h[1]} 个 .day 文件  例:{h[2]}")
if not hits:
    print("  未找到 .day 文件")

print("\nDONE")
