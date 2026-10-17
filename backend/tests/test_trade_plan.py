"""交易计划测试：整手取整、逐票跳过规则、卖买顺序、模式与预算守卫。

用真实的 `entry_mask`（而不是 mock 掉）跑 —— 计划与回测**共用口径**这件事
本身就是要保的性质：一旦两边分叉，这些用例会先发现。
"""

from __future__ import annotations

import pytest
from rpsboard import db as dbmod
from rpsboard.analytics import backtest as bt
from rpsboard.broker import plan as plan_mod
from rpsboard.broker import qmt

from .conftest import TRADE_CASH as CASH


def _candidates(conn, plan_date, period=60, threshold=50.0, max_positions=5):
    """按 build_plan 同样的口径取出候选，供用例断言。"""
    ctx = bt.load_context(conn, None)
    entry = bt.entry_mask(ctx, conn, period, threshold)
    sel = entry.mask.loc[plan_date]
    names = sorted(sel.index[sel.fillna(False).astype(bool)])
    ret = bt.get_frames(ctx, True)[f"ret{period}"].loc[plan_date]
    names.sort(key=lambda s: (-(ret.get(s) or float("-inf")), s))
    return names[:max_positions]


def _build(conn, plan_date, **kw):
    args = dict(plan_date=plan_date, period=60, threshold=50.0, max_positions=5,
                cash_budget=CASH, mode="paper")
    args.update(kw)
    return plan_mod.build_plan(conn, **args)


# ── 基本形态 ────────────────────────────────────────────────────────
def test_target_volume_is_round_lot_and_under_budget(trade_db):
    path, plan_date, last_close = trade_db
    with dbmod.session(path) as conn:
        pid, n = _build(conn, plan_date)
        items = plan_mod.load_plan(conn, pid)[1]
    assert n == len(items) == 5
    for it in items:
        assert it["target_volume"] % 100 == 0, "股数必须是整手"
        assert it["reason"] == "ok"
        assert it["side"] == "buy"
        # 每只不超过 20% 预算（风控同一条口径）
        assert it["plan_price"] * it["target_volume"] <= CASH * 0.2
        # 买价 = 最新价 ×1.005，向上取两位
        assert it["plan_price"] == round(last_close[it["symbol"]] * 1.005, 2)


def test_plan_note_records_mouth_of_deviation(trade_db):
    """口径偏差必须写在 note 里：等权全市场 vs 取前 N、限价滑点、未建模项。"""
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        pid, _ = _build(conn, plan_date)
        plan = plan_mod.load_plan(conn, pid)[0]
    assert "等权" in plan["note"] and "限价" in plan["note"]
    assert "分红送转" in plan["note"]
    assert plan["mode"] == "paper" and plan["status"] == "planned"


# ── 逐票跳过规则 ────────────────────────────────────────────────────
def test_suspended_symbol_is_skipped_with_reason(trade_db):
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        top = _candidates(conn, plan_date)[0]
        conn.execute("UPDATE daily_bar SET tradestatus=0 WHERE symbol=? AND date=?",
                     (top, plan_date))
        conn.commit()
        bt.clear_context_cache()
        pid, _ = _build(conn, plan_date)
        item = {i["symbol"]: i for i in plan_mod.load_plan(conn, pid)[1]}[top]
    assert item["reason"] == "suspended" and item["target_volume"] == 0


def test_missing_limit_row_is_skipped_not_guessed(trade_db):
    """没有实测涨跌停价时宁可不出计划，也不推算一个价位去下单。"""
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        top = _candidates(conn, plan_date)[0]
        conn.execute("DELETE FROM price_limit_daily WHERE symbol=?", (top,))
        conn.commit()
        pid, _ = _build(conn, plan_date)
        item = {i["symbol"]: i for i in plan_mod.load_plan(conn, pid)[1]}[top]
    assert item["reason"] == "no_limit_row" and item["target_volume"] == 0


def test_local_mode_is_retired(trade_db):
    """`mode='local'` 已随自建模拟盘迁到多账户原价引擎，必须**显式拒绝**。

    旧实现里这条路径会用后复权价当钱、把本地委托塞进共用的 trade_order，
    并与 QMT 数据互相踩。留一个「静默接受但不走新引擎」的分支比报错更危险：
    用户会以为自己的计划生效了。
    """
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn, pytest.raises(plan_mod.PlanError, match="local"):
        _build(conn, plan_date, mode="local")


def test_price_above_limit_up_is_skipped(trade_db):
    path, plan_date, last_close = trade_db
    with dbmod.session(path) as conn:
        top = _candidates(conn, plan_date)[0]
        # 涨停价压到最新价之下：委托价必然越界
        conn.execute("UPDATE price_limit_daily SET limit_up=? WHERE symbol=?",
                     (last_close[top] * 0.99, top))
        conn.commit()
        pid, _ = _build(conn, plan_date)
        item = {i["symbol"]: i for i in plan_mod.load_plan(conn, pid)[1]}[top]
    assert item["reason"] == "price_out_of_limit" and item["target_volume"] == 0


def test_symbol_without_tick_is_skipped(trade_db, monkeypatch):
    path, plan_date, last_close = trade_db
    with dbmod.session(path) as conn:
        top = _candidates(conn, plan_date)[0]
        monkeypatch.setattr(qmt, "tick", lambda codes: {
            c: (None if c == top else last_close[c]) for c in codes})
        pid, _ = _build(conn, plan_date)
        item = {i["symbol"]: i for i in plan_mod.load_plan(conn, pid)[1]}[top]
    assert item["reason"] == "no_tick" and item["target_volume"] == 0


def test_budget_too_small_for_one_lot_is_skipped(trade_db):
    """每只预算买不起一手 → too_small（不向下取整成 0 股）。"""
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        pid, _ = _build(conn, plan_date, cash_budget=5_000.0)
        items = plan_mod.load_plan(conn, pid)[1]
    assert items and all(i["reason"] == "too_small" for i in items)
    assert all(i["target_volume"] == 0 for i in items)


# ── 卖出与顺序 ──────────────────────────────────────────────────────
def test_overheld_symbol_becomes_sell_and_sells_come_first(trade_db, monkeypatch):
    path, plan_date, last_close = trade_db
    with dbmod.session(path) as conn:
        first_pid, _ = _build(conn, plan_date)
        target = plan_mod.load_plan(conn, first_pid)[1][0]
        over = target["symbol"]
        held = target["target_volume"] * 3
        monkeypatch.setattr(qmt, "positions", lambda: [{
            "symbol": over, "volume": held, "can_use_volume": held,
            "frozen_volume": 0, "avg_price": 9.0, "market_value": 9.0 * held}])
        pid, _ = _build(conn, plan_date)
        items = plan_mod.load_plan(conn, pid)[1]
        by = {i["symbol"]: i for i in items}
        sides = [i["side"] for i in items]
    assert by[over]["side"] == "sell" and by[over]["target_volume"] > 0
    assert by[over]["plan_price"] == round(last_close[over] * 0.995, 2), "卖价要向下取整两位"
    assert sides.index("sell") < sides.index("buy"), "卖单必须排在买单前面"


# ── 守卫 ────────────────────────────────────────────────────────────
def test_illegal_mode_is_rejected(trade_db):
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn, pytest.raises(plan_mod.PlanError, match="非法"):
        _build(conn, plan_date, mode="live")


def test_cash_budget_zero_without_account_raises_no_account(trade_db, monkeypatch):
    def boom():
        raise qmt.QmtError("未设置 RPSBOARD_QMT_ACCOUNT")

    monkeypatch.setattr(qmt, "asset", boom)
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn, pytest.raises(plan_mod.PlanError, match="no_account"):
        _build(conn, plan_date, cash_budget=0.0)


def test_non_signal_date_is_rejected(trade_db):
    path, _, _ = trade_db
    with dbmod.session(path) as conn, pytest.raises(plan_mod.PlanError, match="不是信号日"):
        plan_mod.build_plan(conn, plan_date="1990-01-01", period=60, threshold=50.0,
                            max_positions=5, cash_budget=CASH, mode="paper")


# ── 影子成交 ────────────────────────────────────────────────────────
def test_shadow_fills_buy_side_with_negative_broker_id(trade_db):
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        pid, _ = _build(conn, plan_date)
        res = plan_mod.shadow_fill(conn, pid)
        fills = conn.execute(
            "SELECT * FROM trade_fill WHERE broker_order_id=?", (-pid,)).fetchall()
    assert res["fills"] == len(fills) == 5
    assert all(f["broker_order_id"] < 0 for f in fills)
    assert all(f["side"] == "buy" for f in fills)
    # 影子往返摩擦 = (买价−卖价)/买价 = 1%/1.005（以买入价为分母）
    assert res["round_trip"] == pytest.approx(0.01 / 1.005, abs=1e-9)


def test_shadow_is_idempotent_on_repeat(trade_db):
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        pid, _ = _build(conn, plan_date)
        plan_mod.shadow_fill(conn, pid)
        plan_mod.shadow_fill(conn, pid)
        n = conn.execute("SELECT COUNT(*) AS c FROM trade_fill").fetchone()["c"]
    assert n == 5
