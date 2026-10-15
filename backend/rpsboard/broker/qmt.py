"""QMT 桥的项目侧客户端（3.12，纯标准库）。

项目里**只有这一层**知道「QMT 需要另一个解释器」这件事：所有 xtquant 调用
都经 `run()` 起子进程执行 `python -m rpsboard.broker.bridge`（见 bridge.py），
协议是一行 JSON 进、一行 JSON 出。

断线不会被静默吞掉：`on_disconnected` 事件会置 `LAST_DISCONNECT_AT`，
本次 `run` 直接抛 `QmtError("连接已断开")`，由执行器据此 halt。
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from .. import config

#: `python -m rpsboard.broker.bridge` 需要能 import 到 rpsboard 包
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: 最近一次被桥报告断线的时间（None = 本进程内没见过断线）。
#: 执行器在发单前后比对它，以此判定「这次执行期间连接断过」。
LAST_DISCONNECT_AT: datetime | None = None

#: 只读查询默认超时；下单/撤单走同一上限（客户端不在线时 xtquant 会立刻返回）
DEFAULT_TIMEOUT = 60.0


class QmtError(RuntimeError):
    """桥调用失败：未配置、解释器缺失、超时、连接断开、xtquant 报错。"""


def preflight() -> None:
    """发任何请求前的前置检查。缺什么直接说缺什么，不让 xtquant 报天书。"""
    if not config.QMT_ACCOUNT:
        raise QmtError(
            "未设置 RPSBOARD_QMT_ACCOUNT：把资金账号写进仓库根 `.env.local`"
            "（gitignored）或设为环境变量"
        )
    if config.TRADE_MODE not in config.TRADE_MODES:
        raise QmtError(
            f"RPSBOARD_TRADE_MODE={config.TRADE_MODE!r} 非法，"
            f"只允许 {list(config.TRADE_MODES)}（本项目没有实盘模式）"
        )
    if config.QMT_PYTHON is None:
        raise QmtError(
            "未设置 RPSBOARD_QMT_PYTHON：xtquant 的编译扩展只到 cp311，"
            "项目 venv 是 3.12 跑不了。用 `uv venv --python 3.11 backend/.venv-qmt` "
            "建一个空环境（桥只用标准库 + 客户端 site-packages），"
            "再把 RPSBOARD_QMT_PYTHON 指到它的 Scripts\\python.exe"
        )
    if not config.QMT_PYTHON.exists():
        raise QmtError(
            f"找不到 QMT 解释器 {config.QMT_PYTHON}：xtquant 的编译扩展只到 cp311，"
            "项目 venv 是 3.12 跑不了。用 `uv venv --python 3.11 backend/.venv-qmt` "
            "建一个空环境（桥只用标准库 + 客户端 site-packages），"
            "再把 RPSBOARD_QMT_PYTHON 指到它的 Scripts\\python.exe"
        )


def run(op: str, payload: dict | None = None, *, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """执行一个桥操作，返回 `data`；失败一律抛 QmtError。"""
    preflight()
    body = dict(payload or {})
    body.setdefault("qmt_dir", str(config.QMT_DIR))
    body.setdefault("site_packages", str(config.QMT_DIR / "bin.x64" / "Lib" / "site-packages"))
    body.setdefault("userdata_mini", str(config.QMT_USERDATA_MINI))
    body["account"] = config.QMT_ACCOUNT
    # 官方要求每个策略使用唯一 session_id：每次调用是一个新进程，用 pid 天然唯一
    body.setdefault("session_id", os.getpid())

    request = json.dumps({"op": op, "payload": body}, ensure_ascii=False)
    env = {**os.environ, "PYTHONPATH": str(BACKEND_DIR)}
    try:
        proc = subprocess.run(
            [str(config.QMT_PYTHON), "-m", "rpsboard.broker.bridge"],
            input=request,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise QmtError(
            f"QMT 解释器不可用：{config.QMT_PYTHON}（文件在，但不是可执行的 Python）"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise QmtError(
            f"QMT 桥超时（{timeout:.0f}s）：客户端无响应。"
            "确认 QMT 客户端已启动并已登录"
        ) from exc

    if _note_disconnect(proc.stderr or ""):
        raise QmtError("连接已断开：QMT 客户端在本次调用期间掉线")

    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        raise QmtError(f"QMT 桥无输出（退出码 {proc.returncode}）：{(proc.stderr or '').strip()[-500:]}")
    try:
        resp = json.loads(lines[-1])
    except ValueError as exc:
        raise QmtError(f"QMT 桥输出不是 JSON：{lines[-1][:200]}") from exc

    if not resp.get("ok"):
        raise QmtError(str(resp.get("error") or "未知错误"))
    return resp.get("data")


def _note_disconnect(stderr: str) -> bool:
    """桥在断线时往 stderr 写一行事件；置位全局时间戳并告知调用方。

    返回 True 表示本次调用期间断过线 —— 调用方必须当作失败，不能当成
    「查询结果为空」。
    """
    global LAST_DISCONNECT_AT
    for line in stderr.splitlines():
        line = line.strip()
        if not line.startswith('{"event"'):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") == "disconnected":
            LAST_DISCONNECT_AT = datetime.now()
            return True
    return False


# ── 薄封装：每个 op 一个函数，省得调用方重复拼 payload ───────────────
def status() -> dict:
    return run("status")


def asset() -> dict:
    return run("asset")


def positions() -> list[dict]:
    return run("positions")


def orders() -> list[dict]:
    return run("orders")


def trades() -> list[dict]:
    return run("trades")


def tick(codes: list[str]) -> dict:
    return run("tick", {"codes": list(codes)})


def place(*, code: str, side: str, volume: int, price: float,
          strategy: str = "rpsboard", remark: str = "") -> int | None:
    data = run("place", {
        "code": code, "side": side, "volume": int(volume),
        "price": float(price), "strategy": strategy, "remark": remark,
    })
    return (data or {}).get("order_id")


def cancel(order_id: int) -> int | None:
    data = run("cancel", {"order_id": int(order_id)})
    return (data or {}).get("cancel_result")
