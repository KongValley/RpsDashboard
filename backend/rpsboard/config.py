"""全局配置：路径、周期、并发数、过滤默认值。

所有可调参数集中在此，避免散落在各模块的魔法数字。
"""

from __future__ import annotations

import os
from pathlib import Path

# ── 路径 ────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parents[2]          # 项目根（含 backend/）


def _load_env_file() -> None:
    """把仓库根的 `.env.local` 读进环境变量（**不覆盖已有的**）。

    本机凭据（例如 ShowDoc 推送地址里的 token）放在这个文件里：
    gitignored、不进日志、不进仓库。已存在的环境变量优先，
    因此 `set SHOWDOC_PUSH_URL=...` 或 CI 注入的变量不会被文件里的值盖掉。
    """
    path = ROOT_DIR / ".env.local"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file()

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
# 欧奈尔原始定义为 250 日（52 周），国内实操常用 20/60/120；15 = 3 周短线。
# **必须升序**：compute_industry_rps 的 member_count 取「最后一个命中周期」的
# 成分数（见 analytics/rps.py），250 必须留在末尾；顺序变了口径就变了。
PERIODS: tuple[int, ...] = (15, 20, 60, 120, 250)
DEFAULT_PERIOD = 60

#: 个股 RPS 的最小横截面：同一交易日有效股票少于该数时**不产出 RPS**。
#:
#: 百分位是相对强度，20 只股票里排第一不代表强。把日K 窗口延长到 1300 根之后，
#: 少数长期停牌股的历史会把日期回填到 2018~2021 年，那些日子横截面只有几到几百只
#: —— 若不设门槛，2020 年的「RPS=100」会和今天的「RPS=100」并排画在同一条曲线上，
#: 回测也可能在这些日子上选中「假榜首」。行业/概念表复用同一批日期（成分股来自
#: 同一批个股，日期口径必须一致）。
MIN_CROSS_SECTION = int(os.environ.get("RPSBOARD_MIN_CROSS_SECTION", "1000"))

# ── 复权异常剔除门槛 ────────────────────────────────────────────────
#: 后复权价低于该值（元）时，**该端点不参与收益计算**。
#:
#: 机理：后复权价 = 原始价 × scale + offset，offset 可以为负。原始价接近「变换零点」
#: (‑offset/scale) 时后复权价被压成极小值，而 close(t)/close(t−N) 在接近零的分母上
#: 会被放大到失真 —— 实测 600076 在 2024-12-18 的 ret60 = +998%，而它当天的原始价
#: 只涨了约 8%。区间收益只取决于**两个端点**，所以这里按端点剔除，而不是「跨过
#: 某个异常日就整段作废」。
#:
#: 0 表示只剔除非正价（historic 行为）；想更保守可以调高（如 2.0）。
#:   Windows:     set RPSBOARD_ANOMALY_MIN_PRICE=2.0
#:   Linux/macOS: export RPSBOARD_ANOMALY_MIN_PRICE=2.0
ANOMALY_MIN_PRICE = float(os.environ.get("RPSBOARD_ANOMALY_MIN_PRICE", "1.0"))


def period_columns(prefix: str = "rps") -> list[str]:
    """按 PERIODS 生成列名，例如 period_columns() -> ['rps15', ..., 'rps250']。"""
    return [f"{prefix}{p}" for p in PERIODS]


# ── 性能监测 ────────────────────────────────────────────────────────
#: 性能日志目录（logs/perf.jsonl，见 api/perf.py）。要留 uvicorn 的 stdout/stderr
#: 也可以重定向到这里（启动脚本没有代做这件事）。
LOG_DIR = Path(os.environ.get("RPSBOARD_LOG_DIR", ROOT_DIR / "logs"))
#: 每个请求追加一行 JSON。logs/ 已在 .gitignore；写入侧按 5 MB 滚动为 `.1`
#: 并只留一份历史（见 api/perf.py 的 PERF_LOG_MAX_BYTES）。
PERF_LOG = Path(os.environ.get("RPSBOARD_PERF_LOG", LOG_DIR / "perf.jsonl"))
#: 超过该毫秒数记为慢请求（ASGI 全程耗时，含响应体发送）
PERF_SLOW_MS = float(os.environ.get("RPSBOARD_PERF_SLOW_MS", "300"))
#: 内存环形缓冲条数：服务端请求与浏览器上报分开计
PERF_RING_SERVER = int(os.environ.get("RPSBOARD_PERF_RING_SERVER", "1000"))
PERF_RING_CLIENT = int(os.environ.get("RPSBOARD_PERF_RING_CLIENT", "500"))

# ── 推送（ShowDoc 推送服务 → 微信）──────────────────────────────────
#: 你的专属推送地址，形如 https://push.showdoc.com.cn/server/api/push/<token>。
#: **这是凭据**：放仓库根 `.env.local`（gitignored）或环境变量，空 = 不推送。
#: 代码与日志都不回显它（只报成功/失败），避免 token 泄漏到终端与 issue 里。
SHOWDOC_PUSH_URL = os.environ.get("SHOWDOC_PUSH_URL", "").strip()
#: 推送请求超时（秒）。手动命令，超时就报错退出，不重试。
SHOWDOC_PUSH_TIMEOUT = float(os.environ.get("SHOWDOC_PUSH_TIMEOUT", "10"))

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
#: 每只股票抓取的日 K 根数。单页上限 800 根，更大的值由 TdxSource.hfq_bars 用
#: `start` 偏移翻页补齐（实测 count=1300 → 2 次请求）。
#:
#: 为什么要 >800：800 根（≈3.2 年）会让**窗口起点之后的前 250 个交易日**没有
#: RPS250、让指数 MA200/250 在回测窗口开头算不出来（实测 6 个调仓点因此被跳过），
#: 也让 2023-02~06 那段横截面只剩几十只股票。1300 根（≈5.2 年）把这些都补上；
#: 再往上是全历史（600000 有 6405 根），库体和抓取时间翻几倍，收益仅在于更早的
#: 回测区间 —— 需要时用 RPSBOARD_BARS 调大即可。
BARS_LIMIT = int(os.environ.get("RPSBOARD_BARS", "1300"))

# ── 股票池过滤默认值 ────────────────────────────────────────────────
#: 股票池过滤的默认值不在这里硬编码：API/回测的 exclude_st 等以函数默认参数形式
#: 声明（backend/rpsboard/api/rps.py、analytics/backtest.py），改这里没人读。

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


# ── QMT 交易接入（只读 / 影子 / 券商模拟盘；**没有实盘分支**）──────
# 客户端目录。xtquant 随客户端自带，且只提供 cp36~cp311 的编译扩展，
# 而项目 venv 是 3.12 → QMT 调用必须发生在 3.11 子进程里（见 broker/bridge.py）。
QMT_DIR = Path(os.environ.get("RPSBOARD_QMT_DIR", r"D:\software\QMT"))
#: 桥传给 XtQuantTrader(path, …) 的目录（客户端登录态与行情服务都在这里）
QMT_USERDATA_MINI = Path(os.environ.get(
    "RPSBOARD_QMT_USERDATA_MINI", QMT_DIR / "userdata_mini"))
#: 跑桥的解释器。**必须与客户端 .pyd 的 ABI 一致**（客户端只提供到 cp311，
#: 而项目 venv 是 3.12）。该环境不需要装任何包（桥只用标准库 + 客户端 site-packages）。
#: **默认值刻意留空**：写死某个用户目录会把本机路径带进公开仓库（换机器也一定是错的）。
#: 按提示建一个空环境再指过来：`uv venv --python 3.11 backend/.venv-qmt`。
#: Windows 上 uv 的虚拟环境解释器在 `.venv-qmt\Scripts\python.exe`。
QMT_PYTHON: Path | None = None
if (_qmt_python := os.environ.get("RPSBOARD_QMT_PYTHON", "").strip()):
    QMT_PYTHON = Path(_qmt_python)
#: 资金账号。**凭据级**：只从环境变量 / `.env.local` 读，日志/接口/页面只留后 4 位。
QMT_ACCOUNT = os.environ.get("RPSBOARD_QMT_ACCOUNT", "").strip()
#: 交易模式：paper=影子（按限价模拟成交）、local=**自建模拟盘**（本项目自己撮合，
#: 不连任何券商）、sim=券商模拟盘（QMT）。
#: 没有 live —— 实盘不在本项目范围内，见 README「QMT 自动化边界」。
TRADE_MODE = os.environ.get("RPSBOARD_TRADE_MODE", "paper").strip().lower()
TRADE_MODES = ("paper", "local", "sim")
#: halt 哨兵文件：存在即禁止任何发单（CLI `trade halt --on`、页面上的开关都只碰它）
TRADE_HALT_FILE = Path(os.environ.get(
    "RPSBOARD_TRADE_HALT_FILE", DATA_DIR / "trade.halt"))


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
