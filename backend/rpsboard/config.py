"""全局配置：路径、周期、并发数、过滤默认值。

所有可调参数集中在此，避免散落在各模块的魔法数字。
"""

from __future__ import annotations

import os
from pathlib import Path

# ── 路径 ────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parents[2]          # 项目根（含 backend/）
DATA_DIR = Path(os.environ.get("RPSBOARD_DATA_DIR", ROOT_DIR / "data"))
DB_PATH = Path(os.environ.get("RPSBOARD_DB_PATH", DATA_DIR / "rpsboard.db"))

#: 本机通达信安装目录。**只有 backend/tests 下的探针脚本用它**——主源 eltdx 是
#: TCP 直连，不依赖本机通达信安装。换机器/换盘符用环境变量覆盖：
#:   Windows:     set RPSBOARD_TDX_DIR=E:\new_tdx
#:   Linux/macOS: export RPSBOARD_TDX_DIR=/opt/tdx
TDX_DIR = Path(os.environ.get("RPSBOARD_TDX_DIR", r"D:\tdx"))
#: eltdx 的板块资料缓存目录（infoharbor_block.dat / tdxhy.cfg / security_list.json）
DOWNLOAD_DIR = Path(os.environ.get("RPSBOARD_DOWNLOAD_DIR", ROOT_DIR / "downloads"))
# eltdx 默认把板块资料缓存写到「进程 CWD 下的 downloads」（见其 helpers/boards.py），
# 固定到 <仓库>/downloads：本机位置不变，别人从任何目录运行也不会多出一份缓存。
# 用户显式设置 ELTDX_BOARD_DATA_DIR 时不覆盖。
os.environ.setdefault("ELTDX_BOARD_DATA_DIR", str(DOWNLOAD_DIR))

# ── RPS 周期 ────────────────────────────────────────────────────────
# 欧奈尔原始定义为 250 日（52 周），国内实操常用 20/60/120
PERIODS: tuple[int, ...] = (20, 60, 120, 250)
DEFAULT_PERIOD = 60

# ── 性能监测 ────────────────────────────────────────────────────────
#: 性能日志目录（logs/perf.jsonl，见 api/perf.py）。要留 uvicorn 的 stdout/stderr
#: 也可以重定向到这里（启动脚本没有代做这件事）。
LOG_DIR = Path(os.environ.get("RPSBOARD_LOG_DIR", ROOT_DIR / "logs"))
#: 每个请求追加一行 JSON。logs/ 已在 .gitignore，刻意不做轮转：
#: 一行约 0.2 KB，个人盯盘的使用强度下一天也就几百 KB。
PERF_LOG = Path(os.environ.get("RPSBOARD_PERF_LOG", LOG_DIR / "perf.jsonl"))
#: 超过该毫秒数记为慢请求（ASGI 全程耗时，含响应体发送）
PERF_SLOW_MS = float(os.environ.get("RPSBOARD_PERF_SLOW_MS", "300"))
#: 内存环形缓冲条数：服务端请求与浏览器上报分开计
PERF_RING_SERVER = int(os.environ.get("RPSBOARD_PERF_RING_SERVER", "1000"))
PERF_RING_CLIENT = int(os.environ.get("RPSBOARD_PERF_RING_CLIENT", "500"))

# ── 采集 ────────────────────────────────────────────────────────────
#: **唯一数据源**：eltdx（通达信行情协议，TCP 直连）。
#:
#: 本机 2026-10-12 实测曾评估过五个源，最终只保留 eltdx：
#:   eltdx    209 秒全市场、后复权、无反爬     ← 保留
#:   腾讯     0.09s/只，但并发直接触发 waf.tencent.com 封禁   ← 删除
#:   baostock 约 70 分钟，多进程并发登录返回 10001001 未登录  ← 删除
#:   本地 .day 22 秒但不复权、数据滞后一个月，仅用于补成交额  ← 删除
#:   akshare  本机网络阻断（ProxyError / RemoteDisconnected） ← 删除
#: 已删除的适配器仍可从 git 历史取回（`git show <commit>:backend/rpsboard/sources/...`）。
DATA_SOURCE = "tdxnet"
#: eltdx 采集并发线程（客户端自带 8 连接池，线程只是流水并行）
TDX_WORKERS = int(os.environ.get("RPSBOARD_TDX_WORKERS", "8"))
#: 单只股票最大重试次数
MAX_RETRIES = 3

# ── 股票池过滤默认值 ────────────────────────────────────────────────
#: 默认剔除 ST/*ST（API 可用 exclude_st=false 放开）
DEFAULT_EXCLUDE_ST = True
#: 默认剔除上市不足 N 个交易日的股票（新股 RPS 天然为 NaN，此项仅为显式语义）
MIN_LISTED_BARS = max(PERIODS)

# ── 涨跌停 ──────────────────────────────────────────────────────────
#: 涨跌停**不再用硬编码阈值**。原来的 LIMIT_MAIN=9.8 / LIMIT_GROWTH=19.8 是
#: 拍出来的，而且量纲就是错的：它们是百分数，而 daily_bar.pct_chg 存的是小数，
#: 导致 `exclude_limit_up` 这个过滤**从未命中过任何一只股票**（实测当日命中 0 家，
#: 真实涨停 7 家）。
#:
#: 现在改为读 `price_limit_daily` —— eltdx 的 daily_price_limits 返回每只票
#: 当日真实的 limit_ratio_pct / limit_rule，覆盖主板 10%、创业板/科创板 20%、
#: 北交所 30%、ST、新股前五日无限制共六种规则。见 analytics/limits.py。


def exchange_of(symbol: str) -> str:
    """由 6 位代码判定交易所。

    刻意不采用「9 开头→上海」的简化规则：920xxx 是北交所新代码段，
    归到上交所是错的。
    """
    if symbol.startswith(("6", "9")) and not symbol.startswith("92"):
        return "sh"
    if symbol.startswith(("4", "8", "92")):
        return "bj"
    return "sz"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
