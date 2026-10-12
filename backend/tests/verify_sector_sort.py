"""验证四个周期的行业排序确实各自独立（用户需求：各维护强弱进行排序）。"""
import sys, json, urllib.request

BASE = "http://127.0.0.1:8000"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))


hm = get("/api/rps/heatmap")
matrix = hm["matrix"]
print(f"行业数 {len(matrix)}，周期 {hm['periods']}\n")

periods = hm["periods"]


def v(row, p):
    x = row.get(f"rps{p}")
    return -1 if x is None else x


orders = {}
for p in periods:
    # 前端逻辑：按该周期降序，缺失沉底
    ordered = sorted(matrix, key=lambda r: (v(r, p) < 0, -v(r, p)))
    orders[p] = [r["industry"] for r in ordered]
    top5 = " / ".join(f"{r['industry'][:10]}({v(r,p):.0f})" for r in ordered[:5])
    bottom3 = " / ".join(f"{r['industry'][:10]}({v(r,p):.0f})" for r in ordered[-3:])
    print(f"RPS{p} 前5 : {top5}")
    print(f"RPS{p} 后3 : {bottom3}\n")

print("=" * 70)
# 各周期排序是否真的不同
base = orders[periods[0]]
for p in periods[1:]:
    same = sum(1 for a, b in zip(base, orders[p]) if a == b)
    rank_corr = 0
    pos_a = {name: i for i, name in enumerate(base)}
    pos_b = {name: i for i, name in enumerate(orders[p])}
    common = list(set(pos_a) & set(pos_b))
    for name in common:
        rank_corr += (pos_a[name] - pos_b[name]) ** 2
    n = len(common)
    from math import sqrt
    spearman = 1 - 6 * rank_corr / (n * (n * n - 1)) if n > 1 else 1
    print(f"RPS{periods[0]} vs RPS{p}: 首位相同 {same}/{n} 位, "
          f"Spearman 秩相关 {spearman:.3f}")

print("\n" + "=" * 70)
distinct = len({tuple(o) for o in orders.values()})
print(f"四个周期产生 {distinct} 种不同排序 → "
      f"{'各自独立排序，符合需求' if distinct == len(periods) else '存在重复排序，需检查'}")
