"""性能监测点：请求耗时中间件 + 环形缓冲 + 查询接口。

`GET /api/meta/perf` 的统计窗口是内存环形缓冲里**最近 N 条请求**，不是全量累计；
需要跨重启的累计值请直接聚合 `logs/perf.jsonl`（每请求一行 JSON）。

前端在 `frontend/src/api/index.js` 的同一出口计时并批量 POST 到
`/api/meta/perf/client`，两端数据在同一个响应里对照：浏览器总耗时减去中间件
下发的 `x-perf-ms`，就是网络 + 解析那一段。
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .. import config

logger = logging.getLogger(__name__)

_SERVER: deque[dict] = deque(maxlen=config.PERF_RING_SERVER)
_CLIENT: deque[dict] = deque(maxlen=config.PERF_RING_CLIENT)
_STARTED = time.monotonic()
_log_lock = threading.Lock()
#: 落盘失败后置位，避免每次请求都重试并刷屏
_log_broken = False
#: 查询接口自身不入统计，否则轮询会把自己算进最慢的请求里
_EXCLUDE_PREFIX = "/api/meta/perf"
#: 路径过长（恶意/异常 query）只留前 300 字符
_PATH_MAX = 300
#: 性能日志单文件上限：超过就滚动为 `.1`（只留一份历史），避免无限增长
PERF_LOG_MAX_BYTES = 5 * 1024 * 1024


def reset() -> None:
    """清空缓冲与统计（测试用：模块级状态需要可复位）。"""
    global _log_broken
    _SERVER.clear()
    _CLIENT.clear()
    _log_broken = False


def _log_path() -> Path:
    """调用时读 config：测试用 monkeypatch 换路径才生效。"""
    return Path(config.PERF_LOG)


def _append_jsonl(entry: dict) -> None:
    """追加一行 JSON。**绝不向请求链路抛异常**：写不进去就停写并告警一次。"""
    global _log_broken
    with _log_lock:
        if _log_broken:
            return
        try:
            path = _log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            # 滚动：超过上限就改名为 `.1`（只留一份历史）。观测数据不做长期归档，
            # 无上限增长的唯一后果是慢慢吃掉磁盘（实测 715 KB / 一天）。
            if path.exists() and path.stat().st_size > PERF_LOG_MAX_BYTES:
                path.replace(path.with_name(path.name + ".1"))
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            _log_broken = True
            logger.warning("性能日志写入失败，本次运行不再落盘: %s", exc)


def record(
    src: str,
    path: str,
    ms: float,
    *,
    method: str = "GET",
    status: int = 0,
    bytes_: int = 0,
    ttfb_ms: float | None = None,
    ok: bool = True,
    error: str | None = None,
    ts: float | None = None,
) -> dict | None:
    """记一条请求。src 为 "server"（本进程处理的请求）或 "client"（浏览器上报）。

    ms 非有限值直接丢弃：NaN/Inf 会一路污染环形缓冲里的 p50/p95/max
    （排序后 NaN 排在末尾、Inf 直接成为 max），而且会让落盘 JSON 变成
    非标准 JSON。前端老版本上报过 `ms: NaN`（`performance.now()` 相减出问题）。
    """
    if not math.isfinite(ms):
        return None
    entry = {
        "ts": ts or time.time(),
        "src": src,
        "path": str(path)[:_PATH_MAX],
        "method": method,
        "status": status,
        "ms": round(ms, 1),
        "bytes": bytes_,
        "ttfb_ms": None if ttfb_ms is None else round(ttfb_ms, 1),
        "ok": ok,
        "error": error,
        "slow": ms >= config.PERF_SLOW_MS,
    }
    (_SERVER if src == "server" else _CLIENT).append(entry)
    _append_jsonl(entry)
    return entry


class PerfMiddleware:
    """记录每个 HTTP 请求的耗时。纯 ASGI 包装：不算 lifespan / websocket。

    耗时口径 = ASGI 收到 scope 到响应体发完；ttfb_ms 是到 http.response.start
    的耗时，作为 `x-perf-ms` 响应头下发——前端用它把浏览器侧总耗时拆成
    「服务端处理」与「网络 + 解析」两段。

    刻意用裸 ASGI 而不是 BaseHTTPMiddleware：后者要多包一层 body 流，
    对一个每个请求都跑的中间件来说没必要。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        # 预检请求是浏览器与 CORS 中间件之间的握手，不是应用的活儿
        if method == "OPTIONS" or path.startswith(_EXCLUDE_PREFIX):
            return await self.app(scope, receive, send)

        t0 = time.perf_counter()
        state = {"status": 0, "bytes": 0, "ttfb": None}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["ttfb"] = (time.perf_counter() - t0) * 1000
                message.setdefault("headers", []).append(
                    (b"x-perf-ms", f"{state['ttfb']:.1f}".encode())
                )
            elif message["type"] == "http.response.body":
                state["bytes"] += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException as exc:
            record(
                "server",
                path,
                (time.perf_counter() - t0) * 1000,
                method=method,
                status=state["status"],
                bytes_=state["bytes"],
                ttfb_ms=state["ttfb"],
                ok=False,
                error=type(exc).__name__,
            )
            raise
        record(
            "server",
            path,
            (time.perf_counter() - t0) * 1000,
            method=method,
            status=state["status"],
            bytes_=state["bytes"],
            ttfb_ms=state["ttfb"],
        )


router = APIRouter(prefix="/api/meta/perf", tags=["perf"])


class ClientEntry(BaseModel):
    """浏览器上报的单条请求。字段全部来自前端，属信任边界外，故限长限条数。

    数值字段必须带 `allow_inf_nan=False`：JSON 里的 `NaN` / `Infinity`
    以前会被原样收下，进入环形缓冲后把 p50/p95/max 全变成 NaN，
    `GET /api/meta/perf` 也跟着坏掉。现在直接 422。
    """

    path: str = Field(max_length=300)
    ms: float = Field(ge=0, le=600_000, allow_inf_nan=False)
    server_ms: float | None = Field(default=None, ge=0, le=600_000, allow_inf_nan=False)
    status: int | None = Field(default=None, ge=0, le=599)
    ok: bool = True
    ts: float | None = Field(default=None, ge=0, le=4_102_444_800, allow_inf_nan=False)


class ClientBatch(BaseModel):
    entries: list[ClientEntry] = Field(max_length=200)


@router.post("/client")
def ingest(batch: ClientBatch) -> dict:
    """接收前端批量上报。超过 200 条 / 字段缺失由 pydantic 直接返回 422。"""
    for e in batch.entries:
        record(
            "client",
            e.path,
            e.ms,
            status=e.status or 0,
            ok=e.ok,
            ttfb_ms=e.server_ms,
            ts=e.ts,
        )
    return {"ok": True, "accepted": len(batch.entries)}


def _pct(values: list[float], q: float) -> float:
    """nearest-rank 分位数：小样本下比插值更贴近「用户真的遇到了几次慢」。"""
    s = sorted(values)
    idx = min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))
    return s[idx]


def _server_summary(rows: list[dict]) -> dict:
    by_path: dict[str, list[dict]] = {}
    for r in rows:
        by_path.setdefault(r["path"], []).append(r)

    paths = []
    for path, items in by_path.items():
        times = [i["ms"] for i in items]
        last = items[-1]
        paths.append(
            {
                "path": path,
                "n": len(items),
                "slow": sum(1 for i in items if i["slow"]),
                "error": sum(1 for i in items if i["error"]),
                "p50_ms": round(_pct(times, 0.5), 1),
                "p95_ms": round(_pct(times, 0.95), 1),
                "max_ms": round(max(times), 1),
                "last_ms": last["ms"],
                "last_bytes": last["bytes"],
                "last_ttfb_ms": last["ttfb_ms"],
            }
        )
    paths.sort(key=lambda p: p["p95_ms"], reverse=True)

    slow = [r for r in rows if r["slow"]][-20:]
    return {
        "n": len(rows),
        "slow": sum(1 for r in rows if r["slow"]),
        "error": sum(1 for r in rows if r["error"]),
        "paths": paths,
        "recent_slow": [
            {
                "ts": round(r["ts"], 3),
                "path": r["path"],
                "status": r["status"],
                "ms": r["ms"],
                "bytes": r["bytes"],
                "ttfb_ms": r["ttfb_ms"],
                "error": r["error"],
            }
            for r in reversed(slow)
        ],
    }


def _client_summary(rows: list[dict]) -> dict:
    def fin(v) -> float | None:
        """非有限值一律 None：被污染过的老缓冲（旧版本收下过 ms=NaN）不能顺出来，
        否则 round(NaN) 会让整个 JSON 响应变成非标准的 500。"""
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return v if math.isfinite(v) else None

    def one(r: dict) -> dict:
        server_ms = fin(r.get("ttfb_ms"))
        ms = fin(r.get("ms"))
        # 允许为负：两端时钟/量化差异是真实存在的，夹紧只会掩盖它
        net = None if ms is None or server_ms is None else round(ms - server_ms, 1)
        return {
            "ts": fin(r.get("ts")),
            "path": r.get("path"),
            "ms": ms,
            "server_ms": server_ms,
            "net_ms": net,
            "status": r.get("status"),
            "ok": r.get("ok"),
        }

    return {
        "n": len(rows),
        "slow": sum(1 for r in rows if r["slow"]),
        "recent": [one(r) for r in reversed(rows[-50:])],
    }


@router.get("")
def summary() -> dict:
    """前后端性能快照。只读内存缓冲，不查库、不读 jsonl。"""
    return {
        "uptime_s": round(time.monotonic() - _STARTED, 1),
        "slow_ms": config.PERF_SLOW_MS,
        "server": _server_summary(list(_SERVER)),
        "client": _client_summary(list(_CLIENT)),
    }
