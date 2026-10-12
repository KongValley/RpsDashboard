"""测试：绕过本地代理能否直连东方财富 / 百度。

本机环境：HTTP_PROXY/HTTPS_PROXY = http://127.0.0.1:7890
NO_PROXY 含 .cn，但东财用的是 .com 域名 → 走代理 → ProxyError
"""
import os, sys, time

USE_PROXY = os.environ.get("TEST_NO_PROXY") == "1"
if USE_PROXY:
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    print(">>> 已禁用代理（直连模式）", flush=True)
else:
    print(">>> 使用当前环境代理", flush=True)

import requests

TARGETS = [
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://82.push2.eastmoney.com/api/qt/clist/get",
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://www.baidu.com",
    "https://pypi.org/simple/",
]

for url in TARGETS:
    t0 = time.time()
    try:
        r = requests.get(url, timeout=15,
                         params={"secid": "1.600519", "klt": "101", "fqt": "1",
                                 "lmt": "5", "fields1": "f1", "fields2": "f51,f53"}
                         if "eastmoney" in url else None)
        body = r.text[:70].replace("\n", " ")
        print(f"[OK  ] {url[:52]:<52} {time.time()-t0:5.1f}s http={r.status_code} {body}")
    except Exception as e:
        print(f"[FAIL] {url[:52]:<52} {time.time()-t0:5.1f}s {type(e).__name__}: {str(e)[:70]}")
    sys.stdout.flush()

# 有代理就直接退避，再用 akshare 实测
try:
    import akshare as ak
    t0 = time.time()
    df = ak.stock_zh_a_spot_em()
    print(f"\nakshare spot_em -> rows={len(df)} 用时 {time.time()-t0:.1f}s")
    print(df.head(2).to_string())
except Exception as e:
    print(f"\nakshare spot_em -> {type(e).__name__}: {str(e)[:120]}")
