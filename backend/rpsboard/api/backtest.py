"""回测接口 + 管理接口（手动触发采集）。"""

from __future__ import annotations

import math
import os
import sqlite3
import threading
import time

from fastapi import APIRouter, Body
from pydantic import BaseModel

from .. import config, db
from ..analytics import backtest as engine
from ..analytics.costs import default_cost_model as engine_cost_model
from ..cli import run_update_pipeline as cli_run_update_pipeline
from . import latest_bar_date

router = APIRouter(prefix="/api", tags=["backtest"])

#: 回测结果的进程内缓存。回测计算量在秒级，而页面上调阈值/持有期/周期
#: 会反复触发同一批参数的请求，参数不变时直接复用即可。
#: key 里带上 rps_daily 的最新日期，数据一旦更新就自动失效。
_CACHE: dict[tuple, tuple[str, dict]] = {}
_CACHE_MAX = 32


def _cache_token(conn) -> str:
    """数据版本指纹：rps / 涨跌停 / 基准三张表的最大日期 + 库文件（含 -wal）mtime/size。

    空库（刚 clone、还没 init/backfill）不能直查：调用方拿到 "" 会正常返回空结果，
    而不是抛 no such table 变成 500。

    只用 rps_daily.MAX(date) 是不够的：同日补采涨跌停、补基准、形态重算都不改
    RPS 的日期，同参数请求会继续返回旧结论（实测过）。-wal 必须一起看：
    WAL 模式下写入先进 db-wal，主库文件的 mtime/size 往往纹丝不动。
    """
    if not db.table_exists(conn, "rps_daily"):
        return ""
    parts: list[str] = []
    for sql in (
        "SELECT MAX(date) AS d FROM rps_daily",
        "SELECT MAX(date) AS d FROM price_limit_daily",
        "SELECT MAX(date) AS d FROM benchmark_bar",
    ):
        try:
            row = conn.execute(sql).fetchone()
            parts.append(str(row["d"]) if row and row["d"] else "-")
        except sqlite3.Error:
            parts.append("-")
    try:
        path = conn.execute("PRAGMA database_list").fetchone()[2] or ""
    except sqlite3.Error:
        path = ""
    stamp = 0
    for suffix in ("", "-wal"):
        try:
            st = os.stat(path + suffix)
        except OSError:
            continue
        stamp += int(st.st_mtime) * 1_000_000 + st.st_size
    parts.append(str(stamp))
    return "|".join(parts)


def clear_backtest_cache() -> None:
    """数据更新后清掉回测结果缓存（后台更新流程结束时调用）。"""
    _CACHE.clear()


#: 允许的基准指数。给了白名单外的字符串时 benchmark_bar 查不到 → 基准线全空
#: 且无提示，因此与 market_filter 一样做白名单回落。
BENCHMARKS = ("000300", "000001", "399001")


@router.get("/backtest/run")
def run(
    period: int = config.DEFAULT_PERIOD,
    threshold: float = 80.0,
    hold: int = 20,
    rebalance: int | None = None,
    resonance: bool = False,
    exclude_st: bool = True,
    exclude_limit_up: bool = True,
    benchmark: str = "000300",
    market_filter: str = "none",
    use_cost: bool = True,
    use_price_limits: bool = True,
    lookback_days: int | None = None,
    signal: str = "rps",
    select_top: int | None = None,
) -> dict:
    """执行 RPS 策略回测。

    同参数请求走进程内缓存直接返回；返回值中始终携带幸存者偏差警示。
    """
    if period not in config.PERIODS:
        period = config.DEFAULT_PERIOD
    hold = max(1, min(int(hold), 250))
    # nan 会被 min/max 静默吞掉（min(nan,100)=nan，max(0.0,nan)=0.0 → 变成 0.0），
    # 于是「阈值 0」这种从没被请求过的参数被当成有效输入拿去算回测。
    if not math.isfinite(float(threshold)):
        threshold = 80.0
    threshold = max(0.0, min(float(threshold), 100.0))
    # rebalance 完全不夹取时，负数会绕过引擎里的 step 计算
    if rebalance is not None:
        rebalance = max(1, min(int(rebalance), 250))
    if market_filter not in engine.MARKET_FILTERS:
        market_filter = "none"
    if benchmark not in BENCHMARKS:
        benchmark = "000300"
    # 负数会算出「未来」的起始日 → 空窗口 + 空曲线，静默返回没有意义的结果
    if lookback_days is not None:
        lookback_days = max(1, min(int(lookback_days), 2000))
    if signal not in engine.SIGNALS:
        signal = "rps"
    if select_top is not None:
        select_top = max(1, min(int(select_top), 500))

    key = (
        period, round(threshold, 4), hold, rebalance, resonance,
        exclude_st, exclude_limit_up, benchmark, market_filter,
        use_cost, use_price_limits, lookback_days, signal, select_top,
    )

    conn = db.connect()
    try:
        token = _cache_token(conn)
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == token:
            return dict(cached[1], cached=True)

        res = engine.run_backtest(
            conn,
            period=period,
            threshold=threshold,
            hold=hold,
            rebalance=rebalance,
            resonance=resonance,
            exclude_st=exclude_st,
            exclude_limit_up=exclude_limit_up,
            benchmark_symbol=benchmark,
            market_filter=market_filter,
            use_price_limits=use_price_limits,
            cost_model=engine_cost_model(use_cost),
            lookback_days=lookback_days,
            signal=signal,
            select_top=select_top,
        )
        payload = {
            "equity_curve": res.equity_curve,
            "benchmark_curve": res.benchmark_curve,
            "market_curve": res.market_curve,
            "metrics": res.metrics,
            "buckets": res.buckets,
            "buckets_note": res.buckets_note,
            "params": res.params,
            "warnings": res.warnings,
            "cached": False,
        }
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = (token, payload)
        return payload
    finally:
        conn.close()


class CollectRequest(BaseModel):
    task: str = "update"


# ── 一键更新（后台线程 + 状态轮询）─────────────────────────────────
# 早期实现是**同步**跑完整流程：eltdx 拉全市场日K 约 209 秒 + RPS 增量 10 秒。
# 同步 HTTP 必然超时，而且采集任务跑在请求线程里会阻塞整个服务。
_update_lock = threading.Lock()
_update_state: dict = {"running": False, "phase": None, "run_id": None,
                      "error": None, "started_at": None, "finished_at": None}


def _run_update_bg(run_id: str) -> None:
    """后台线程体。异常必须自己吞掉并写状态，否则前端会永远轮询到 running。

    run_id 由**请求线程**生成后传进来：以前在线程里 start_run 之后立刻把
    `_update_state["run_id"]` 读出去返回，竞态下响应里的 run_id 是 None。
    """
    t0 = time.time()
    try:
        def progress(phase: str) -> None:
            _update_state["phase"] = phase
        res = cli_run_update_pipeline(progress)
        _update_state.update(phase="done", error=None,
                             result=res, elapsed=round(time.time() - t0, 1))
        with db.session() as conn:
            db.finish_run(conn, run_id, "success", int(res.get("price_limit_rows", 0)),
                          f"更新完成；预警命中 {res.get('alert_hits', 0)} 条")
    except Exception as exc:  # noqa: BLE001
        _update_state.update(phase="failed", error=str(exc),
                             elapsed=round(time.time() - t0, 1))
        with db.session() as conn:
            db.finish_run(conn, run_id, "failed", 0, f"{type(exc).__name__}: {exc}")
    finally:
        _update_state["running"] = False
        _update_state["finished_at"] = db.now()
        # 数据变了 → 回测结果缓存必须失效（指纹能拦掉大部分，这里更直接）
        clear_backtest_cache()


@router.post("/admin/update")
def start_update(req: CollectRequest = Body(default=CollectRequest())) -> dict:
    """启动完整更新流程：日K → 涨跌停 → RPS。

    **立即返回** run_id，不阻塞请求；进度用 GET /admin/update/status 轮询。
    这是本地单人使用的看板、不做鉴权，因此只允许触发白名单内的幂等更新，
    不接受任意命令执行。
    """
    if req.task != "update":
        return {"ok": False, "error": f"不支持的任务: {req.task}，仅支持 update"}
    if not _update_lock.acquire(blocking=False):
        return {"ok": False, "error": "已有更新任务在执行中，请等待完成",
                "phase": _update_state.get("phase"), "running": True}
    try:
        # 互斥判定必须在锁内做：以前只在启动瞬间持锁，第二次 POST 拿到锁后又
        # 直接覆盖状态并起第二个线程（两个采集进程写同一个库）。
        if _update_state.get("running"):
            return {"ok": False, "error": "已有更新任务在执行中，请等待完成",
                    "phase": _update_state.get("phase"), "running": True}
        with db.session() as conn:
            run_id = db.start_run(conn, task="update", source="internal")
        _update_state.update(running=True, phase="starting", error=None,
                             run_id=run_id, started_at=db.now(),
                             finished_at=None, elapsed=None)
        threading.Thread(target=_run_update_bg, args=(run_id,), daemon=True).start()
        return {"ok": True, "run_id": run_id, "running": True}
    finally:
        # 锁只在启动瞬间持有，线程本身不持锁；真正的互斥靠上面的 running 检查
        _update_lock.release()


@router.get("/admin/update/status")
def update_status() -> dict:
    """轮询更新进度。"""
    conn = db.connect()
    try:
        last = db.last_log(conn)
        # 必须复用同一个连接：原来写成 latest_bar_date(db.connect())，
        # 每轮询一次就漏一个连接（前端 3 秒一次）。
        last_bar = latest_bar_date(conn)
    finally:
        conn.close()
    return {
        "running": _update_state["running"],
        "phase": _update_state["phase"],
        "error": _update_state["error"],
        "run_id": _update_state["run_id"],
        "started_at": _update_state["started_at"],
        "finished_at": _update_state["finished_at"],
        "elapsed": _update_state.get("elapsed"),
        "last_bar_date": last_bar,
        "last_collect_log": last,
    }
