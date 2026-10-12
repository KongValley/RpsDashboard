"""验证通达信 .day 二进制格式解析是否正确。

通达信日线文件格式（每个记录固定 32 字节）：
  0-3   date   uint32  YYYYMMDD
  4-7   open   uint32  价格 × 100
  8-11  high   uint32  价格 × 100
  12-15 low    uint32  价格 × 100
  16-19 close  uint32  价格 × 100
  20-23 amount float32 成交额（元）
  24-27 vol    uint32  成交量（股）
  28-31 reserved uint32
"""
import struct, os, time, glob

SH = r"D:\tdx\vipdoc\sh\lday"
SZ = r"D:\tdx\vipdoc\sz\lday"
BJ = r"D:\tdx\vipdoc\bj\lday"


def read_day(path):
    recs = []
    with open(path, "rb") as f:
        data = f.read()
    for i in range(0, len(data) - 31, 32):
        (d, o, h, l, c, amt, vol, _r) = struct.unpack("<IIIIIfII", data[i:i + 32])
        if d == 0:
            break
        recs.append({
            "date": f"{d//10000:04d}-{d//100%100:02d}-{d%100:02d}",
            "open": o / 100.0, "high": h / 100.0, "low": l / 100.0,
            "close": c / 100.0, "amount": amt, "vol": vol,
        })
    return recs


print("=== 抽样验证解析 ===")
for path in [r"D:\tdx\vipdoc\sh\lday\sh600519.day",
             r"D:\tdx\vipdoc\sz\lday\sz000001.day",
             r"D:\tdx\vipdoc\sz\lday\sz300750.day",
             r"D:\tdx\vipdoc\bj\lday\bj920000.day"]:
    if not os.path.exists(path):
        print(f"  [缺] {path}")
        continue
    recs = read_day(path)
    if not recs:
        print(f"  [空] {path}")
        continue
    last = recs[-1]
    print(f"  {os.path.basename(path):<18} {len(recs):>5} 行  "
          f"{recs[0]['date']} ~ {last['date']}")
    print(f"    末行 O={last['open']} H={last['high']} L={last['low']} "
          f"C={last['close']} 额={last['amount']:.0f} 量={last['vol']}")

print("\n=== 全量读取耗时估算 ===")
for base, label in [(SH, "沪"), (SZ, "深"), (BJ, "北")]:
    files = glob.glob(os.path.join(base, "*.day"))
    t0 = time.time()
    n = rows = 0
    latest = ""
    for p in files:
        try:
            r = read_day(p)
            rows += len(r)
            n += 1
            if r and r[-1]["date"] > latest:
                latest = r[-1]["date"]
        except Exception:
            pass
    dt = time.time() - t0
    print(f"  {label}: {n} 文件 / {rows} 行 / 用时 {dt:.1f}s  最新交易日 {latest}")

print("\n=== 与腾讯 hfq 对照（收盘价是否一致）===")
try:
    import json
    import requests
    S = requests.Session(); S.trust_env = False
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    j = S.get(url, params={"param": "sh600519,day,2024-09-01,2026-10-12,640,hfq"},
              headers={"User-Agent": "Mozilla/5.0"}, timeout=15).json()
    tx = {r[0]: float(r[2]) for r in
          (j["data"]["sh600519"]["hfqday"])}
    tdx = {r["date"]: r["close"] for r in
           read_day(r"D:\tdx\vipdoc\sh\lday\sh600519.day")}
    common = sorted(set(tx) & set(tdx))[-5:]
    print(f"  {'日期':<12}{'腾讯hfq':>12}{'通达信不复权':>14}{'比值':>10}")
    for d in common:
        ratio = tx[d] / tdx[d] if tdx[d] else 0
        print(f"  {d:<12}{tx[d]:>12.2f}{tdx[d]:>14.2f}{ratio:>10.4f}")
except Exception as e:
    print("  对照失败:", type(e).__name__, str(e)[:80])

print("\nDONE")
