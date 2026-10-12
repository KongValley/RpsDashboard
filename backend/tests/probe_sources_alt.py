"""实测各 A 股数据源在本网络的可用性与速度。

背景：baostock 可用但 login 60~100s；东方财富（akshare 底层）被网络阻断。
本脚本横向对比候选源，给出「换哪个最快」的实测依据。
"""
import os, sys, time, socket

TIMEOUT = 12

# 关掉代理再测一遍对照（很多 *.com 站点走代理会失败）
NO_PROXY = os.environ.get("DIRECT") == "1"
if NO_PROXY:
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"
    import requests  # noqa
    import requests.utils
    session = requests.Session()
    session.trust_env = False
    print(">>> 直连模式（绕过本地代理）\n")
else:
    import requests
    session = requests.Session()
    print(">>> 使用当前代理设置\n")


def probe(label, fn, timeout=TIMEOUT):
    t0 = time.time()
    try:
        r = fn()
        print(f"[OK  ] {label:<44} {time.time()-t0:5.1f}s  {r}")
    except Exception as e:
        print(f"[FAIL] {label:<44} {time.time()-t0:5.1f}s  {type(e).__name__}: {str(e)[:60]}")
    sys.stdout.flush()


def get(url, headers=None, enc="utf-8", timeout=TIMEOUT):
    def f():
        r = session.get(url, headers=headers or {
            "User-Agent": "Mozilla/5.0"}, timeout=timeout)
        body = r.content[:160].decode(enc, errors="replace").replace("\n", " ")
        return f"http={r.status_code} len={len(r.content)} {body[:70]}"
    return f


# ── 腾讯财经 ──────────────────────────────────────────────────────
probe("腾讯 实时快照 qt.gtimg.cn",
      get("https://qt.gtimg.cn/q=sh600519,sz000001"))
probe("腾讯 日K web.ifzq.gtimg.cn",
      get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
          "?param=sh600519,day,2025-01-01,2026-10-12,640,qfq"))

# ── 新浪财经 ──────────────────────────────────────────────────────
probe("新浪 实时快照 hq.sinajs.cn",
      get("https://hq.sinajs.cn/list=sh600519,sz000001",
          headers={"Referer": "https://finance.sina.com.cn"}, enc="gbk"))

# ── 网易（老牌，行情接口较稳定）──────────────────────────────────
probe("网易 历史K线 quotes.money.163.com",
      get("https://quotes.money.163.com/service/chddata.html"
          "?code=0600519&start=20250101&end=20261012"
          "&fields=TCLOSE;HIGH;LOW;TOPEN;VOTURNOVER;VATURNOVER",
          enc="gbk"))

# ── 东财（对照，预期失败）────────────────────────────────────────
probe("东财 快照 push2.eastmoney.com",
      get("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=5&fs=m:1+t:2&fields=f12,f14"))

# ── Tushare（需 token，先测连通性）───────────────────────────────
probe("Tushare API 连通性",
      get("https://api.tushare.pro", enc="utf-8"))

# ── 本地通达信 ────────────────────────────────────────────────────
print("\n本地通达信数据目录探测:")
cands = [
    r"C:\new_tdx", r"C:\tdx", r"C:\通达信", r"D:\new_tdx", r"D:\tdx",
    r"C:\Program Files\new_tdx", r"C:\Program Files (x86)\new_tdx",
]
found = False
for c in cands:
    if os.path.isdir(c):
        print(f"  [找到] {c}")
        found = True
# 兜底：在常见盘根目录浅层搜索 vipdoc
for drive in ("C:", "D:", "E:", "F:"):
    try:
        for name in os.listdir(drive + "\\"):
            if "tdx" in name.lower() or "通达信" in name:
                print(f"  [候选] {drive}\\{name}")
                found = True
    except Exception:
        pass
if not found:
    print("  未发现本地通达信安装目录")

print("\nDONE")
