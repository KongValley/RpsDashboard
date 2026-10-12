"""端到端接口验收：用真实回填数据逐个打接口并打印关键结果。

不是单元测试——这是「跑通真实链路」的冒烟检查。
"""
import json
import urllib.request

BASE = "http://127.0.0.1:8000"


def get(path, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    url = f"{BASE}{path}" + (f"?{qs}" if qs else "")
    with urllib.request.urlopen(url, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))


print("=" * 70)
st = get("/api/meta/status")
print("STATUS")
for k in ("db_ready", "bar_rows", "rps_rows", "industry_rows", "last_bar_date",
          "last_spot_date", "spot_stale"):
    print(f"  {k:16} = {st[k]}")

dates = get("/api/meta/dates")
print(f"\nDATES      共 {len(dates)} 个交易日，最新 {dates[0]}，最早 {dates[-1]}")
inds = get("/api/meta/industries")
print(f"INDUSTRIES {len(inds)} 个，前 8: {inds[:8]}")

print("\n" + "=" * 70)
rk = get("/api/rps/ranking", period=60, rps_min=90, limit=10)
print(f"RANKING    total={rk['total']}  data_date={rk['data_date']}  period={rk['period']}")
print(f"{'代码':<8}{'名称':<10}{'行业':<12}{'RPS60':>7}{'区间涨幅':>10}{'成交额':>10}")
for r in rk["rows"][:10]:
    print(f"{r['symbol']:<8}{(r['name'] or '')[:8]:<10}{(r['industry'] or '')[:10]:<12}"
          f"{r['rps']:>7.2f}{(r['period_return'] or 0)*100:>9.2f}%{r['amount'] or 0:>10.0f}")

# 幂等性：同一请求两次结果必须完全一致
rk2 = get("/api/rps/ranking", period=60, rps_min=90, limit=10)
same = json.dumps(rk["rows"], sort_keys=True) == json.dumps(rk2["rows"], sort_keys=True)
print(f"\nIDEMPOTENT 两次请求结果一致: {same}")

# 多周期交叉验证：同一只票在不同周期的排名
if rk["rows"]:
    sym = rk["rows"][0]["symbol"]
    d = get(f"/api/stock/{sym}")
    print(f"\nSTOCK {sym} {d['info']['name']} / {d['info']['industry']}")
    for k, v in d["current"].items():
        if k.startswith("rps"):
            print(f"  {k:8} = {v}")
    print("  跑赢全市场分位:", d["market_percentile"])
    print("  所属行业RPS  :", d["industry_rps"])

    cv = get(f"/api/stock/{sym}/curve", days=250)
    n = len(cv["dates"])
    lens = {k: len(v) for k, v in cv.items() if k not in ("symbol",)}
    print(f"\nCURVE  日期数={n}  各数组长度={set(lens.values())}  "
          f"对齐={len(set(lens.values())) == 1}")
    print(f"  最后日期 {cv['dates'][-1]}  最新收盘 {cv['close'][-1]}  RPS60 {cv['rps60'][-1]}")

print("\n" + "=" * 70)
sec = get("/api/rps/sector", period=60)
print(f"SECTOR     {len(sec['rows'])} 个行业，data_date={sec['data_date']}")
for r in sec["rows"][:8]:
    print(f"  {(r['industry'] or '')[:14]:<16} RPS {r['rps']:>6.2f} "
          f"涨幅 {(r['period_return'] or 0)*100:>7.2f}% "
          f"成分 {r['member_count']:>4}  领涨 {r['leader_symbol']} {r['leader_name']}")

hm = get("/api/rps/heatmap")
print(f"\nHEATMAP    {len(hm['industries'])} 行业 × {len(hm['periods'])} 周期")

print("\n" + "=" * 70)
bt = get("/api/backtest/run", period=60, threshold=80, hold=20)
print("BACKTEST   params:", bt["params"])
print("  metrics:", bt["metrics"])
print(f"  净值曲线点数 {len(bt['equity_curve'])}  基准点数 {len(bt['benchmark_curve'])}")
print("  warnings:", bt["warnings"][0][:60], "...")
print("\n  RPS 分档前瞻收益（验证 RPS 是否有预测力）:")
print(f"  {'区间':<10}{'样本':>9}{'平均前瞻收益':>14}{'上涨概率':>10}")
for b in bt["buckets"]:
    ar = f"{b['avg_forward_return']*100:.2f}%" if b["avg_forward_return"] is not None else "-"
    wr = f"{b['win_rate']*100:.1f}%" if b["win_rate"] is not None else "-"
    print(f"  {b['range']:<10}{b['samples']:>9}{ar:>14}{wr:>10}")

print("\n" + "=" * 70)
print("ALL ENDPOINTS OK")
