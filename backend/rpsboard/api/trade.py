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
    """最近一次**成功的账户状态**（`cli trade status`）的结果。

    `collect_log.task='trade'` 下有四个写入者：`cli trade status`（账户/资金
    JSON）、页面快照、`cli trade execute`、失败任务。只有 status 的成功
    message 是账户状态；取最新一行会让一次持仓快照把账号和资金清成 None。
    这里按 source='qmt' + status='success' 且 message 是**含 account_masked
    与 asset 的 JSON 对象**过滤，跳过其余记录。
    """
    rows = _rows(
        conn,
        "SELECT run_id, status, started_at, finished_at, rows_affected, message"
        " FROM collect_log WHERE task='trade' AND source='qmt' AND status='success'"
        " ORDER BY started_at DESC, rowid DESC",
    )
    for out in rows:
        try:
            payload = json.loads(out.get("message") or "")
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        if "account_masked" not in payload or not isinstance(payload.get("asset"), dict):
            continue
        out["payload"] = payload
        return out
    return {}



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


def _paper_date(conn: sqlite3.Connection, raw: str | None) -> str | None:
    """把请求日期解析为净值曲线上**实际存在**的一天。

    与 `api/__init__.py::resolve_date` 同一条就近回退策略：优先不晚于该日的
    最近一天（周末/长假落回），一个都没有才取更早的下一天。直接退回"最新一天"
    是错的 —— 用户选 2026-05-05 会被无声换成 2026-10-14，看到的完全是另一天。
    """
    latest_row = conn.execute("SELECT MAX(trade_date) AS d FROM paper_nav").fetchone()
    latest = latest_row["d"] if latest_row and latest_row["d"] else None
    if not raw or not str(raw).strip():
        return latest
    raw = str(raw).strip()
    hit = conn.execute("SELECT 1 FROM paper_nav WHERE trade_date = ?", (raw,)).fetchone()
    if hit:
        return raw
    near = conn.execute(
        "SELECT trade_date AS d FROM paper_nav WHERE trade_date <= ?"
        " ORDER BY trade_date DESC LIMIT 1", (raw,)).fetchone()
    if near and near["d"]:
        return near["d"]
    near = conn.execute(
        "SELECT trade_date AS d FROM paper_nav WHERE trade_date > ?"
        " ORDER BY trade_date ASC LIMIT 1", (raw,)).fetchone()
    return near["d"] if near and near["d"] else latest


@router.get("/paper")
def paper(date: str | None = Query(
        default=None, description="YYYY-MM-DD：回看历史某日；缺省=最新一天")) -> dict:
    """自建模拟盘：净值曲线 + 权益摘要 + 复盘指标 + 当前/指定日持仓。

    **只读库，不连任何券商。** `date` 落在非交易日时按就近回退，并在
    `date` 字段回显实际生效的交易日。
    """
    from ..broker import sim

    conn = get_conn()
    try:
        out = sim.summary(conn)
        out["review"] = sim.review(conn)
        effective = _paper_date(conn, date)
        out["date"] = effective
        if out.get("days") and effective and effective != out.get("to"):
            out["positions"] = sim.positions_at(conn, effective)
            row = conn.execute(
                "SELECT trade_date, cash, market_value, total, nav, n_positions"
                " FROM paper_nav WHERE trade_date = ?", (effective,)).fetchone()
            out["nav_row"] = dict(row) if row else None
        return out
    except sim.SimError as exc:
        return {"error": str(exc), "days": 0, "curve": [], "positions": [],
                "round_trips": [], "review": {}}


@router.get("/paper/archives")
def paper_archives(limit: int = Query(default=10, ge=1, le=50)) -> dict:
    """模拟盘曲线归档列表（**含曲线**，供策略对照页叠加画图）。只读库。"""
    from ..broker import sim

    conn = get_conn()
    try:
        return {"rows": sim.list_archives(conn, limit=int(limit))}
    except sim.SimError as exc:
        return {"rows": [], "error": str(exc)}


class ArchiveBody(BaseModel):
    label: str | None = None


@router.post("/paper/archive")
def paper_archive(body: ArchiveBody | None = None) -> dict:
    """把**当前**曲线归档一份。写库（与 halt / snapshot 同类）。

    模拟盘只有一个活动账户：跑第二种策略前先归档，否则 `--reset` 之后
    这条曲线就再也拿不回来了。
    """
    from ..broker import sim

    with db.session() as conn:
        try:
            res = sim.archive(conn, (body.label if body else None) or None)
        except sim.SimError as exc:
            return {"ok": False, "error": str(exc)}
    _summary_cache.pop("v", None)
    return {"ok": True, **res}


@router.delete("/paper/archives/{arch_id}")
def paper_archive_delete(arch_id: int) -> dict:
    from ..broker import sim

    with db.session() as conn:
        removed = sim.delete_archive(conn, int(arch_id))
    return {"ok": removed, "id": int(arch_id)}


@router.get("/snapshot/status")
def snapshot_status() -> dict:
    return dict(_snapshot_state)
