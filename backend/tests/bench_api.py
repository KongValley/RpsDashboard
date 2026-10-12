"""测量各接口的真实耗时，定位前端卡在 loading 的原因。"""
import sys, time, json, urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

PATHS = [
    "/api/meta/status",
    "/api/meta/periods",
    "/api/meta/industries",
    "/api/rps/ranking?period=60&rps_min=80&rps_max=100&exclude_st=true&limit=100&page=1",
    "/api/rps/ranking?period=60&rps_min=80&limit=100&page=1",
    "/api/rps/sector?period=60",
    "/api/rps/heatmap",
    "/api/stock/000993",
    "/api/stock/000993/curve?days=250",
    "/api/backtest/run?period=60&threshold=80&hold=20",
]

print(f"目标: {BASE}\n")
print(f"{'接口':<62}{'耗时':>10}")
print("-" * 74)
for p in PATHS:
    t0 = time.time()
    try:
        with urllib.request.urlopen(BASE + p, timeout=180) as r:
            body = r.read()
            dt = time.time() - t0
            j = json.loads(body)
            if isinstance(j, dict):
                n = len(j.get("rows", [])) or len(j.get("matrix", [])) or "-"
            else:
                n = len(j)
            print(f"{p:<62}{dt:>9.2f}s  n={n}")
    except Exception as e:
        print(f"{p:<62}{time.time()-t0:>9.2f}s  ERR {type(e).__name__}")
    sys.stdout.flush()
