"""模拟盘执行测试：三重闸（halt / 时段 / 对账）、幂等、小单剔除、断线。

下单调到假的 `qmt.place`，时间由 `now` 参数注入 —— 风控里**刻意没有**
绕过开关（`--force` / `--now`），所以时段这类规则只能这样验证。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from rpsboard import config
from rpsboard import db as dbmod
from rpsboard.broker import exec as trade_exec
from rpsboard.broker import plan as plan_mod
from rpsboard.broker import qmt, risk

from .conftest import TRADE_CASH as CASH

#: 交易时段内的两个时间点（周一 10:00 与 14:00）
IN_HOURS = datetime(2026, 10, 15, 10, 0)
OUT_OF_HOURS = datetime(2026, 10, 15, 12, 0)


def _sim_plan(conn, plan_date, **kw):
    """建一份 sim 模式的计划（paper 会被风控直接拒掉）。"""
    args = dict(plan_date=plan_date, period=60, threshold=50.0, max_positions=5,
                cash_budget=CASH, mode="sim")
    args.update(kw)
    return plan_mod.build_plan(conn, **args)[0]


@pytest.fixture()
def placed(monkeypatch):
    """记录每次 place 调用；现金给到预算的 2 倍，别被 insufficient_cash 挡住。"""
    calls: list[dict] = []

    def fake_place(*, code, side, volume, price, strategy="", remark=""):
        calls.append({"code": code, "side": side, "volume": volume, "price": price})
        return 900000 + len(calls)

    monkeypatch.setattr(qmt, "place", fake_place)
    monkeypatch.setattr(qmt, "asset", lambda: {"cash": 2 * CASH})
    monkeypatch.setattr(qmt, "orders", lambda: [])
    monkeypatch.setattr(qmt, "trades", lambda: [])
    monkeypatch.setattr(qmt, "cancel", lambda order_id: 0)
    # 轮询回报本来要 sleep 2 秒 ×3 轮；测试里没有「等回报」这件事
    monkeypatch.setattr(trade_exec, "POLL_ROUNDS", 1)
    monkeypatch.setattr(trade_exec, "POLL_INTERVAL", 0.0)
    return calls


@pytest.fixture()
def no_halt(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRADE_HALT_FILE", tmp_path / "trade.halt")
    return config.TRADE_HALT_FILE


# ── 三重闸 ──────────────────────────────────────────────────────────
def test_without_confirm_flag_nothing_is_written(trade_db, placed, no_halt):
    """不带 --i-know-this-is-sim → 拒绝，且**一行 trade_order 都不写**。"""
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        pid = _sim_plan(conn, plan_date)
        with pytest.raises(plan_mod.PlanError, match="i-know-this-is-sim"):
            trade_exec.execute(conn, pid, confirm=False, now=IN_HOURS)
        n = conn.execute("SELECT COUNT(*) AS c FROM trade_order").fetchone()["c"]
    assert n == 0 and placed == []


def test_halt_file_blocks_whole_batch(trade_db, placed, no_halt):
    path, plan_date, _ = trade_db
    no_halt.write_text("halted\n", encoding="utf-8")
    with dbmod.session(path) as conn:
        pid = _sim_plan(conn, plan_date)
        with pytest.raises(plan_mod.PlanError, match="halted"):
            trade_exec.execute(conn, pid, confirm=True, now=IN_HOURS)
        status = conn.execute("SELECT status FROM trade_plan WHERE id=?",
                              (pid,)).fetchone()["status"]
    assert status == "halted" and placed == []


def test_outside_trading_hours_blocks_batch(trade_db, placed, no_halt):
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        pid = _sim_plan(conn, plan_date)
        with pytest.raises(plan_mod.PlanError, match="outside_trading_hours"):
            trade_exec.execute(conn, pid, confirm=True, now=OUT_OF_HOURS)
    assert placed == []


def test_position_changed_after_plan_blocks_batch(trade_db, placed, no_halt, monkeypatch):
    """建计划之后账户被外部动过 → 拒绝，不自动补单。"""
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        pid = _sim_plan(conn, plan_date)
        sym = plan_mod.load_plan(conn, pid)[1][0]["symbol"]
        monkeypatch.setattr(qmt, "positions", lambda: [{
            "symbol": sym, "volume": 9999, "can_use_volume": 9999}])
        with pytest.raises(plan_mod.PlanError, match="不一致"):
            trade_exec.execute(conn, pid, confirm=True, now=IN_HOURS)
    assert placed == []


def test_cash_shortfall_blocks_whole_batch(trade_db, placed, no_halt, monkeypatch):
    """cash_budget 大于可用现金 → 风控拦下（不是逐笔拒）。"""
    path, plan_date, _ = trade_db
    monkeypatch.setattr(qmt, "asset", lambda: {"cash": 1_000.0})
    with dbmod.session(path) as conn:
        pid = _sim_plan(conn, plan_date)
        with pytest.raises(plan_mod.PlanError, match="insufficient_cash"):
            trade_exec.execute(conn, pid, confirm=True, now=IN_HOURS)
    assert placed == []


# ── 正常执行与幂等 ──────────────────────────────────────────────────
def test_execute_places_all_buys_and_marks_executed(trade_db, placed, no_halt):
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        pid = _sim_plan(conn, plan_date)
        res = trade_exec.execute(conn, pid, confirm=True, now=IN_HOURS)
        rows = conn.execute("SELECT * FROM trade_order").fetchall()
        status = conn.execute("SELECT status FROM trade_plan WHERE id=?",
                              (pid,)).fetchone()["status"]
    assert res["status"] == "executed" and res["submitted"] == 5
    assert len(rows) == 5
    # 回报还没回来 → 提交后进入 canceling（撤单开关默认开着）
    assert all(r["status"] in ("submitted", "canceling") for r in rows)
    assert all(r["broker_order_id"] for r in rows)
    assert status == "executed"
    assert {c["side"] for c in placed} == {"buy"}


def test_repeat_execute_is_idempotent(trade_db, placed, no_halt):
    """同一计划重复执行不新增委托行（幂等键 = 日期+代码+方向+计划）。"""
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        pid = _sim_plan(conn, plan_date)
        first = trade_exec.execute(conn, pid, confirm=True, now=IN_HOURS)
        second = trade_exec.execute(conn, pid, confirm=True, now=IN_HOURS)
        n = conn.execute("SELECT COUNT(*) AS c FROM trade_order").fetchone()["c"]
    assert first["submitted"] == 5 and second["submitted"] == 0
    assert second["skipped"] == 5
    assert n == 5 and len(placed) == 5


def test_small_orders_are_dropped_without_blocking_others(trade_db, placed, no_halt):
    """金额不足 5000 的票只剔除自身，其余照发。"""
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        pid = _sim_plan(conn, plan_date)
        # 让其中一只的目标缩到 5000 元以下
        items = plan_mod.load_plan(conn, pid)[1]
        thin = items[0]["symbol"]
        conn.execute("UPDATE trade_plan_item SET target_volume=100 WHERE plan_id=?"
                     " AND symbol=?", (pid, thin))
        conn.commit()
        res = trade_exec.execute(conn, pid, confirm=True, now=IN_HOURS)
        thin_row = conn.execute("SELECT * FROM trade_order WHERE symbol=?", (thin,)).fetchone()
    assert thin not in [c["code"] for c in placed], "太小的单不该发出去"
    assert thin_row is None
    assert res["submitted"] == 4 and res["status"] == "executed"


def test_one_failed_order_does_not_abort_the_batch(trade_db, placed, no_halt, monkeypatch):
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        pid = _sim_plan(conn, plan_date)
        sym = plan_mod.load_plan(conn, pid)[1][0]["symbol"]
        real = qmt.place

        def flaky(**kw):
            if kw["code"] == sym:
                raise qmt.QmtError("余额不足")
            return real(**kw)

        monkeypatch.setattr(qmt, "place", flaky)
        res = trade_exec.execute(conn, pid, confirm=True, now=IN_HOURS)
        bad = conn.execute("SELECT * FROM trade_order WHERE symbol=?", (sym,)).fetchone()
        status = conn.execute("SELECT status FROM trade_plan WHERE id=?",
                              (pid,)).fetchone()["status"]
    assert bad["status"] == "rejected" and "余额不足" in (bad["err_msg"] or "")
    assert res["failed"] == 1 and res["submitted"] == 4
    assert status == "partial"


def test_broker_reports_update_orders_and_fills(trade_db, placed, no_halt, monkeypatch):
    """轮询把券商的委托状态与成交落库。"""
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        pid = _sim_plan(conn, plan_date)
        trade_exec.execute(conn, pid, confirm=True, now=IN_HOURS, cancel_remaining=False)
        oid = conn.execute("SELECT broker_order_id FROM trade_order LIMIT 1").fetchone()[0]
        monkeypatch.setattr(qmt, "orders", lambda: [{
            "order_id": oid, "symbol": "000001", "order_type": 23,
            "order_status": 56, "status": "filled", "order_volume": 100,
            "traded_volume": 100, "price": 12.6, "order_time": 93000, "status_msg": ""}])
        monkeypatch.setattr(qmt, "trades", lambda: [{
            "order_id": oid, "traded_id": 7, "symbol": "000001", "order_type": 23,
            "traded_volume": 100, "traded_price": 12.6, "traded_amount": 1260.0,
            "traded_time": 93001}])
        filled, open_n = trade_exec._sync_reports(conn, plan_date)
        row = conn.execute("SELECT status FROM trade_order WHERE broker_order_id=?",
                           (oid,)).fetchone()
        fill = conn.execute("SELECT * FROM trade_fill WHERE broker_order_id=?",
                            (oid,)).fetchone()
    assert row["status"] == "filled" and filled == 1
    assert open_n == 4, "只有回报里报出来的那 1 笔成交，其余 4 笔仍挂着"
    assert fill["broker_trade_id"] == 7 and fill["side"] == "buy" and fill["volume"] == 100


# ── 对账 ────────────────────────────────────────────────────────────
def test_reconcile_reports_target_gap(trade_db, no_halt):
    """against='target'：只报「还差多少没调到位」。"""
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        pid = _sim_plan(conn, plan_date)
        items = plan_mod.load_plan(conn, pid)[1]
        sym = items[0]["symbol"]
        live = [{"symbol": it["symbol"], "volume": it["target_volume"]} for it in items]
        live[0]["volume"] -= 100
        gap = risk.reconcile_positions(conn, pid, live=live, against="target")
    assert [d["symbol"] for d in gap] == [sym] and gap[0]["delta"] == 100


def test_reconcile_detects_change_since_plan_was_built(trade_db, no_halt):
    """against='held'：建计划时是空仓，后来多出 100 股 = 被外部动过。"""
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        pid = _sim_plan(conn, plan_date)
        items = plan_mod.load_plan(conn, pid)[1]
        sym = items[0]["symbol"]
        live = [{"symbol": it["symbol"], "volume": it["held"]} for it in items]
        live[0]["volume"] += 100
        drift = risk.reconcile_positions(conn, pid, live=live, against="held")
    assert [d["symbol"] for d in drift] == [sym] and drift[0]["delta"] == -100


def test_reconcile_is_empty_when_positions_match(trade_db, no_halt):
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        pid = _sim_plan(conn, plan_date)
        live = [{"symbol": it["symbol"], "volume": it["target_volume"]}
                for it in plan_mod.load_plan(conn, pid)[1]]
        assert risk.reconcile_positions(conn, pid, live=live, against="target") == []


# ── 断线 ────────────────────────────────────────────────────────────
def test_disconnect_during_execution_marks_plan_halted(trade_db, placed, no_halt,
                                                       monkeypatch):
    """发单途中连接断了 → 计划标 halted（并推送，若已配置）。"""
    path, plan_date, _ = trade_db

    def drop_then_place(**kw):
        monkeypatch.setattr(qmt, "LAST_DISCONNECT_AT", datetime.now(), raising=False)
        return 900001

    monkeypatch.setattr(qmt, "place", drop_then_place)
    monkeypatch.setattr(qmt, "LAST_DISCONNECT_AT", None, raising=False)
    with dbmod.session(path) as conn:
        pid = _sim_plan(conn, plan_date)
        res = trade_exec.execute(conn, pid, confirm=True, now=IN_HOURS)
        status = conn.execute("SELECT status FROM trade_plan WHERE id=?",
                              (pid,)).fetchone()["status"]
    assert res["status"] == "halted" and status == "halted"


# ── 快照 ────────────────────────────────────────────────────────────
def test_snapshot_positions_replaces_same_day(trade_db, no_halt, monkeypatch):
    """同一天重跑覆盖，不堆重复行。"""
    path, _, _ = trade_db
    with dbmod.session(path) as conn:
        monkeypatch.setattr(qmt, "positions", lambda: [
            {"symbol": "000001", "volume": 1000, "can_use_volume": 1000,
             "frozen_volume": 0, "avg_price": 12.5, "market_value": 12500.0}])
        assert trade_exec.snapshot_positions(conn, "2026-10-15") == 1
        assert trade_exec.snapshot_positions(conn, "2026-10-15") == 1
        n = conn.execute("SELECT COUNT(*) AS c FROM position_snapshot").fetchone()["c"]
        trade_exec.snapshot_positions(conn, "2026-10-16")
        n2 = conn.execute("SELECT COUNT(*) AS c FROM position_snapshot").fetchone()["c"]
    assert n == 1 and n2 == 2
