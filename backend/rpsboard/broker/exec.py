"""模拟盘执行（P3）：把计划变成券商**限价单**。

三道闸，任何一道不过就整批不发：
1. `risk.check`（halt / 模式 / 交易时段 / 现金 / 单票集中度 / 笔数 / 换手）
2. 对账：账户在建计划之后是否被外部动过
3. 幂等：同一计划里已经提交过的 (日期, 代码, 方向) 不再发第二次

单笔失败（QmtError）只把那一笔记 `rejected` 并继续下一笔 —— 整批中断会
让剩余的票永远停在「计划里有、账户里没有」的状态。汇总结果由调用方
按 `failed` 数量决定退出码。

**本模块只支持券商模拟盘**：没有实盘分支，代码里也不存在 `live` 字样的
执行路径（`risk.check` 要求 `plan["mode"] == "sim"`）。
"""

from __future__ import annotations

import sqlite3
import time as _time
from datetime import datetime

from .. import db
from . import qmt, risk
from .plan import PlanError, load_plan

#: 下单后轮询回报的次数与间隔（xtquant 的回报是异步推送的）
POLL_ROUNDS = 3
POLL_INTERVAL = 2.0

#: 刚提交、尚未收到回报的本地状态
STATUS_SUBMITTED = "submitted"


def _upsert_order(conn: sqlite3.Connection, plan_id: int, trade_date: str, item: dict,
                  side: str, volume: int, price: float) -> int:
    """插入 pending 行（幂等键冲突则复用该行）。返回行 id。"""
    now = db.now()
    conn.execute(
        "INSERT INTO trade_order(plan_id, trade_date, symbol, side, volume, price,"
        " status, created_at, updated_at) VALUES (?,?,?,?,?,?,'pending',?,?)"
        " ON CONFLICT(trade_date, symbol, side, plan_id)"
        " DO UPDATE SET volume=excluded.volume, price=excluded.price,"
        " status='pending', updated_at=excluded.updated_at",
        (plan_id, trade_date, item["symbol"], side, volume, price, now, now),
    )
    row = conn.execute(
        "SELECT id FROM trade_order WHERE trade_date=? AND symbol=? AND side=? AND plan_id=?",
        (trade_date, item["symbol"], side, plan_id),
    ).fetchone()
    conn.commit()
    return int(row["id"])


def _set_status(conn: sqlite3.Connection, order_id: int, status: str, *,
                broker_order_id: int | None = None,
                err_id: int | None = None, err_msg: str | None = None) -> None:
    conn.execute(
        "UPDATE trade_order SET status=?, broker_order_id=COALESCE(?, broker_order_id),"
        " err_id=?, err_msg=?, updated_at=? WHERE id=?",
        (status, broker_order_id, err_id, err_msg, db.now(), order_id),
    )
    conn.commit()


def _already_submitted(conn: sqlite3.Connection, trade_date: str, plan_id: int,
                       symbol: str, side: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM trade_order WHERE trade_date=? AND symbol=? AND side=? AND plan_id=?"
        " AND status NOT IN ('rejected','canceled')",
        (trade_date, symbol, side, plan_id),
    ).fetchone()
    return row is not None


def _sync_reports(conn: sqlite3.Connection, trade_date: str) -> tuple[int, int]:
    """拉一次券商回报，把委托状态与成交落库。返回 `(已成交笔数, 未终态笔数)`。"""
    local = {int(r["broker_order_id"]): r["id"] for r in conn.execute(
        "SELECT id, broker_order_id FROM trade_order"
        " WHERE trade_date=? AND broker_order_id IS NOT NULL", (trade_date,)
    )}
    filled = 0
    for o in qmt.orders() or []:
        oid = o.get("order_id")
        if oid is None or int(oid) not in local:
            continue
        _set_status(conn, local[int(oid)], o.get("status") or STATUS_SUBMITTED)
    for t in qmt.trades() or []:
        oid, tid = t.get("order_id"), t.get("traded_id")
        if oid is None or tid is None:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO trade_fill(trade_date, broker_order_id, broker_trade_id,"
            " symbol, side, volume, price, traded_at) VALUES (?,?,?,?,?,?,?,?)",
            (trade_date, int(oid), int(tid), t.get("symbol"),
             "buy" if (t.get("order_type") in (23, "23")) else "sell",
             int(t.get("traded_volume") or 0), float(t.get("traded_price") or 0.0),
             db.now()),
        )
        filled += 1
    conn.commit()
    open_n = conn.execute(
        "SELECT COUNT(*) AS c FROM trade_order WHERE trade_date=? AND status IN"
        " ('pending','submitted','part','canceling')", (trade_date,)
    ).fetchone()["c"]
    return filled, int(open_n)


def _cancel_remaining(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT id, broker_order_id FROM trade_order"
        " WHERE status IN ('submitted','part') AND broker_order_id IS NOT NULL"
    ).fetchall()
    n = 0
    for r in rows:
        try:
            qmt.cancel(int(r["broker_order_id"]))
            _set_status(conn, int(r["id"]), "canceling")
            n += 1
        except qmt.QmtError as exc:
            _set_status(conn, int(r["id"]), "unknown", err_msg=str(exc)[:200])
    return n


def execute(conn: sqlite3.Connection, plan_id: int, *, confirm: bool,
            cancel_remaining: bool = True, now: datetime | None = None) -> dict:
    """执行一份计划。`confirm=False`（没带 `--i-know-this-is-sim`）直接拒绝。"""
    if not confirm:
        raise PlanError("模拟盘发单需要 --i-know-this-is-sim（确认 QMT 客户端登的是模拟账户）")

    plan, items = load_plan(conn, plan_id)
    now = now or datetime.now()
    todo = risk.actionable(items)

    run_id = db.start_run(conn, task="trade", source="qmt")
    try:
        drift = risk.reconcile_positions(conn, plan_id, live=None, against="held")
        if drift:
            _finish(conn, run_id, plan_id, "halted", 0, "对账差异", 0)
            raise PlanError(
                "持仓与计划不一致，拒绝发单："
                + "；".join(f"{d['symbol']} 现持 {d['have']} ≠ 建计划时 {d['reference']}"
                            for d in drift[:5])
            )

        reasons = risk.check({"asset": qmt.asset()}, plan, items, now=now)
        if reasons:
            _finish(conn, run_id, plan_id, "halted", 0, "；".join(reasons), 0)
            raise PlanError("风控拒绝：" + "；".join(reasons))

        submitted = skipped = failed = 0
        action_symbols = {it["symbol"] for it in todo}
        for it in items:
            if it["symbol"] not in action_symbols or not it["plan_price"]:
                continue
            side, target = it["side"], int(it["target_volume"] or 0)
            if _already_submitted(conn, plan["plan_date"], plan_id, it["symbol"], side):
                skipped += 1
                continue
            order_id = _upsert_order(conn, plan_id, plan["plan_date"], it, side,
                                     target, float(it["plan_price"]))
            try:
                broker_id = qmt.place(
                    code=it["symbol"], side=side, volume=target,
                    price=float(it["plan_price"]),
                    strategy=f"rpsboard-{plan_id}", remark=plan["plan_date"],
                )
            except qmt.QmtError as exc:
                _set_status(conn, order_id, "rejected", err_msg=str(exc)[:200])
                failed += 1
                continue
            _set_status(conn, order_id, STATUS_SUBMITTED, broker_order_id=broker_id)
            submitted += 1

        filled = open_n = 0
        for _ in range(POLL_ROUNDS):
            filled, open_n = _sync_reports(conn, plan["plan_date"])
            if open_n == 0:
                break
            _time.sleep(POLL_INTERVAL)
        if cancel_remaining and open_n:
            _cancel_remaining(conn)
            _sync_reports(conn, plan["plan_date"])

        if qmt.LAST_DISCONNECT_AT and qmt.LAST_DISCONNECT_AT >= now:
            status = "halted"
            _notify_disconnect()
        elif failed:
            status = "partial"
        else:
            status = "executed"
        _finish(conn, run_id, plan_id, status, filled, f"提交 {submitted} 笔", skipped + failed)
        return {"plan_id": plan_id, "status": status, "submitted": submitted,
                "skipped": skipped, "failed": failed, "filled": filled}
    except PlanError:
        raise
    except qmt.QmtError as exc:
        _finish(conn, run_id, plan_id, "halted", 0, str(exc)[:200], 0)
        raise PlanError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 —— 任何意外都要留下 failed 记录
        _finish(conn, run_id, plan_id, "partial", 0, f"{type(exc).__name__}: {exc}"[:200], 0)
        raise


def _finish(conn: sqlite3.Connection, run_id: str, plan_id: int, status: str,
            rows: int, message: str, extra: int = 0) -> None:
    conn.execute("UPDATE trade_plan SET status=? WHERE id=?", (status, plan_id))
    conn.commit()
    db.finish_run(conn, run_id, "failed" if status in ("halted", "partial") else "success",
                  rows + extra, message)


def _notify_disconnect() -> None:
    from .. import notify

    if not notify.configured():
        return
    try:
        notify.push("QMT 连接断开，模拟盘执行中止",
                    "断线发生在本次执行窗口内，未成交的委托请在客户端确认后再决定是否重发。")
    except notify.NotifyError:
        pass


def snapshot_positions(conn: sqlite3.Connection, snap_date: str | None = None) -> int:
    """把券商持仓写进 `position_snapshot`（同一天重跑覆盖，不堆重复行）。

    返回写入行数。同一事务里先删当天分区再批量插 —— 与
    `db.replace_date_partition` 同样的「先清后写」语义。
    """
    from datetime import date as _date

    snap_date = snap_date or _date.today().isoformat()
    rows = qmt.positions() or []
    conn.execute("DELETE FROM position_snapshot WHERE snap_date=?", (snap_date,))
    conn.executemany(
        "INSERT OR REPLACE INTO position_snapshot(snap_date, taken_at, symbol, volume,"
        " can_use_volume, avg_price, market_value) VALUES (?,?,?,?,?,?,?)",
        [(snap_date, db.now(), p["symbol"], int(p.get("volume") or 0),
          p.get("can_use_volume"), p.get("avg_price"), p.get("market_value"))
         for p in rows],
    )
    conn.commit()
    return len(rows)
