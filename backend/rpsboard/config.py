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
CACHE_DIR = DATA_DIR / "backtest_cache"

# ── RPS 周期 ────────────────────────────────────────────────────────
# 欧奈尔原始定义为 250 日（52 周），国内实操常用 20/60/120
PERIODS: tuple[int, ...] = (20, 60, 120, 250)
DEFAULT_PERIOD = 60

# ── 采集 ────────────────────────────────────────────────────────────
#: **主数据源：腾讯财经日K**。实测 0.09 秒/只、后复权、无需登录、无并发互斥。
DATA_SOURCE = os.environ.get("RPSBOARD_SOURCE", "tdxnet")   # tdxnet | baostock | tencent
#: eltdx 采集并发线程（客户端自带 8 连接池，线程只是流水并行）
TDX_WORKERS = int(os.environ.get("RPSBOARD_TDX_WORKERS", "8"))
#: 腾讯接口是纯 HTTP，可以并发（不像 baostock 有进程间登录互斥）。
#: 实测 12 线程齐发会触发限流（大量空响应），6 线程 + 限速最稳。
TENCENT_WORKERS = int(os.environ.get("RPSBOARD_TENCENT_WORKERS", "3"))
#: 全局最小请求间隔（秒）。限流闸门，防止突发并发被拒。
TENCENT_MIN_INTERVAL = float(os.environ.get("RPSBOARD_TENCENT_GAP", "0.5"))
#: 本地通达信目录（用于补成交额）。留空则自动探测常见路径。
TDX_ROOT = os.environ.get("RPSBOARD_TDX_ROOT", "") or None

# ���─ 备选源 baostock 的参数 ────────────────────────────────────────
#: 每个短生命周期子进程抓多少只股票
CHUNK_SIZE = int(os.environ.get("RPSBOARD_CHUNK_SIZE", "300"))
#: baostock 相邻查询的最小间隔（秒）。实测连续高频查询会触发
#: `10001011 服务器繁忙，请稍后再试`，必须显式限速。
BAOSTOCK_QPS_GAP = float(os.environ.get("RPSBOARD_BAOSTOCK_GAP", "0.25"))

#: 单个子进程的硬超时（秒）
CHUNK_TIMEOUT_SEC = float(os.environ.get("RPSBOARD_CHUNK_TIMEOUT", "900"))
BACKFILL_WORKERS = int(os.environ.get("RPSBOARD_WORKERS", "1"))
LOGIN_STAGGER_SEC = float(os.environ.get("RPSBOARD_LOGIN_STAGGER", "130"))
#: 回填的起始交易日往前推的日历天数。需覆盖 250 个交易日 + 缓冲，
#: 365*2 足够（约 730 个日历日 ≈ 490 个交易日）
BACKFILL_DAYS = int(os.environ.get("RPSBOARD_BACKFILL_DAYS", "730"))
#: 单只股票最大重试次数
MAX_RETRIES = 3

# ── 股票池过滤默认值 ────────────────────────────────────────────────
#: 默认剔除 ST/*ST（API 可用 exclude_st=false 放开）
DEFAULT_EXCLUDE_ST = True
#: 默认剔除上市不足 N 个交易日的股票（新股 RPS 天然为 NaN，此项仅为显式语义）
MIN_LISTED_BARS = max(PERIODS)

# ── 涨跌停阈值（用于回测入池过滤）────────────────────────────────────
#: 主板 ±10%，创业板/科创板 ±20%
LIMIT_MAIN = 9.8
LIMIT_GROWTH = 19.8


def is_growth_board(symbol: str) -> bool:
    """判断是否创业板/科创板（涨跌停幅度 20%）。

    300/301 创业板（深），688/689 科创板（沪），北交所 8xx/4xx 也是 30% 但
    本项目默认池不含北交所，故仅按前两段处理。
    """
    return symbol.startswith(("300", "301", "688", "689"))


def limit_threshold(symbol: str) -> float:
    """返回该股当日可视为「涨停」的涨跌幅阈值（百分数）。"""
    return LIMIT_GROWTH if is_growth_board(symbol) else LIMIT_MAIN


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
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
