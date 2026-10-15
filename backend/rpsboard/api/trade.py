"""QMT 交易接入的只读接口 + 两个写入口（halt 开关、重取快照）。

**除 `POST /halt` 与 `POST /snapshot` 外全部只读 SQLite，且不连 QMT**：
页面每 10 秒轮询一次 summary，若每次都去连客户端，一次卡住整页就废了。
`connected` / `account_masked` / `asset` 来自**最近一次 `cli trade status`
写进 `collect_log` 的结果**，因此页面上看到的是「上一次真实拉取时」的数字，
而不是假装实时。

`POST /snapshot` 不在请求线程里连 QMT：那是唯一会起子进程的端点，
按 `api/backtest.py` 的后台线程 + 状态轮询范式实现。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from .. import config, db
from . import get_conn

router = APIRouter(prefix="/api/trade", tags=["trade"])

#: summary 的缓存 TTL。持仓快照每天只刷几次，页面 10 秒轮询一次；
#: 缓存让每次轮询退化成「读已缓存的 dict」，而不是重查 6 张表。
_SUMMARY_TTL = 10.0
_summary_cache: dict = {}


class HaltBody(BaseModel):
    on: bool


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    """只读查询。表不存在（尚未 `cli init`）时返回空表；其它 SQL 错误照抛。"""
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise


def _last_trade_run(conn: sqlite3.Connection) -> dict[str, Any]:
    """最近一次 `cli trade status` 的结果（账号/资金存在它的 message 里）。"""
    row = conn.execute(
        "SELECT run_id, status, started_at, finished_at, rows_affected, message"
        " FROM collect_log WHERE task='trade' ORDER BY started_at DESC, rowid DESC"
        " LIMIT 1"
    ).fetchone()
    if not row:
        return {}
    out = dict(row)
    try:
        out["payload"] = json.loads(out.get("message") or "{}")
    except ValueError:
        out["payload"] = {}
    return out


def _summary(conn: sqlite3.Connection) -> dict:
    last = _last_trade_run(conn)
    payload = last.get("payload") or {}
    plans = _rows(conn, "SELECT * FROM trade_plan ORDER BY id DESC LIMIT 1")
    plan = plans[0] if plans else None
    if plan:
        plan["params"] = json.loads(plan["params"] or "{}")
        plan["items"] = _rows(
            conn, "SELECT * FROM trade_plan_item WHERE plan_id=? ORDER BY rowid",
            (plan["id"],))
    positions = _rows(conn, "SELECT * FROM position_snapshot WHERE snap_date ="
                           " (SELECT MAX(snap_date) FROM position_snapshot)")
    reconcile = []
    if plan and positions:
        reconcile = _reconcile(plan, positions)
    return {
        "mode": config.TRADE_MODE,
        "halt": config.TRADE_HALT_FILE.exists(),
        "connected": last.get("status") == "success",
        "account_masked": payload.get("account_masked"),
        "asset": payload.get("asset"),
        "last_run": {k: last.get(k) for k in
                     ("run_id", "status", "started_at", "finished_at", "rows_affected")},
        "positions": positions,
        "plan": plan,
        "orders": _rows(conn, "SELECT * FROM trade_order ORDER BY id DESC LIMIT 200"),
        "fills": _rows(conn, "SELECT * FROM trade_fill ORDER BY traded_at DESC LIMIT 200"),
        "reconcile": reconcile,
    }


def _reconcile(plan: dict, positions: list[dict]) -> list[dict]:
    """现持 vs 计划绝对目标（只算计划里的票）。纯库内计算，不连 QMT。"""
    held = {p["symbol"]: int(p.get("volume") or 0) for p in positions}
    out = []
    for it in plan.get("items") or []:
        if it["reason"] != "ok" or not it["plan_price"]:
            continue
        have = held.get(it["symbol"], 0)
        target = int(it["target_volume"] or 0)
        if have != target:
            out.append({"symbol": it["symbol"], "side": it["side"], "have": have,
                        "target": target, "delta": target - have})
    return out


@router.get("/summary")
def summary() -> dict:
    conn = get_conn()
    now = time.monotonic()
    hit = _summary_cache.get("v")
    if hit is not None and now - _summary_cache["at"] < _SUMMARY_TTL:
        return hit
    payload = _summary(conn)
    _summary_cache.update(v=payload, at=now)
    return payload


@router.get("/positions")
def positions() -> dict:
    return {"rows": _rows(
        get_conn(), "SELECT * FROM position_snapshot WHERE snap_date ="
                    " (SELECT MAX(snap_date) FROM position_snapshot)")}


@router.get("/orders")
def orders(date: str | None = Query(default=None, description="YYYY-MM-DD，缺省全部")) -> dict:
    if date:
        return {"rows": _rows(get_conn(),
                              "SELECT * FROM trade_order WHERE trade_date=?"
                              " ORDER BY id DESC", (date,))}
    return {"rows": _rows(get_conn(), "SELECT * FROM trade_order ORDER BY id DESC LIMIT 200")}


@router.get("/fills")
def fills(date: str | None = Query(default=None, description="YYYY-MM-DD，缺省全部")) -> dict:
    if date:
        return {"rows": _rows(get_conn(),
                              "SELECT * FROM trade_fill WHERE trade_date=?"
                              " ORDER BY traded_at DESC", (date,))}
    return {"rows": _rows(get_conn(), "SELECT * FROM trade_fill ORDER BY traded_at DESC LIMIT 200")}


@router.get("/plans")
def plans(limit: int = Query(default=20, ge=1, le=200)) -> dict:
    rows = _rows(get_conn(), "SELECT * FROM trade_plan ORDER BY id DESC LIMIT ?", (int(limit),))
    for r in rows:
        r["params"] = json.loads(r["params"] or "{}")
    return {"rows": rows}


@router.post("/halt")
def halt(body: HaltBody) -> dict:
    """发单总开关。只增删哨兵文件 —— 不连 QMT、不动任何数据。"""
    if body.on:
        config.TRADE_HALT_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.TRADE_HALT_FILE.write_text(f"halted at {db.now()}\n", encoding="utf-8")
    elif config.TRADE_HALT_FILE.exists():
        config.TRADE_HALT_FILE.unlink()
    with db.session() as conn:
        run_id = db.start_run(conn, task="trade-halt", source="api")
        db.finish_run(conn, run_id, "success", 0, "halt ON" if body.on else "halt OFF")
    _summary_cache.pop("v", None)
    return {"ok": True, "halt": config.TRADE_HALT_FILE.exists()}


# ── 重取快照（唯一会连 QMT 的端点，走后台线程）──────────────────────
_snapshot_lock = threading.Lock()
_snapshot_state: dict = {"running": False, "error": None, "rows": 0,
                         "started_at": None, "finished_at": None}


def _run_snapshot_bg(run_id: str) -> None:
    """异常必须自己吞掉并写状态，否则前端会永远轮询到 running。"""
    from ..broker import exec as trade_exec

    try:
        with db.session() as conn:
            rows = trade_exec.snapshot_positions(conn)
            db.finish_run(conn, run_id, "success", rows, "持仓快照（页面触发）")
        _snapshot_state.update(rows=rows, error=None)
    except Exception as exc:  # noqa: BLE001
        _snapshot_state.update(rows=0, error=f"{type(exc).__name__}: {exc}")
        with db.session() as conn:
            db.finish_run(conn, run_id, "failed", 0, f"{type(exc).__name__}: {exc}")
    finally:
        _snapshot_state["running"] = False
        _snapshot_state["finished_at"] = db.now()
        _summary_cache.pop("v", None)


@router.post("/snapshot")
def start_snapshot() -> dict:
    """立即返回 run_id；进度用 GET /snapshot/status 轮询。"""
    if not _snapshot_lock.acquire(blocking=False):
        return {"ok": False, "error": "已有快照任务在执行中", "running": True}
    try:
        if _snapshot_state.get("running"):
            return {"ok": False, "error": "已有快照任务在执行中", "running": True}
        with db.session() as conn:
            run_id = db.start_run(conn, task="trade", source="api")
        _snapshot_state.update(running=True, error=None, rows=0,
                               started_at=db.now(), finished_at=None)
        threading.Thread(target=_run_snapshot_bg, args=(run_id,), daemon=True).start()
        return {"ok": True, "run_id": run_id, "running": True}
    finally:
        _snapshot_lock.release()


@router.get("/paper")
def paper() -> dict:
    """自建模拟盘：净值曲线 + 权益摘要 + 当前持仓。**只读库，不连任何券商。**"""
    from ..broker import sim

    conn = get_conn()
    try:
        return sim.summary(conn)
    except sim.SimError as exc:
        return {"error": str(exc), "days": 0, "curve": [], "positions": []}


@router.get("/snapshot/status")
def snapshot_status() -> dict:
    return dict(_snapshot_state)
