"""自建模拟盘的撮合规则测试。

撮合是这套东西的**全部价值**所在，所以每条规则都要有断言：限价挂不挂得进去、
涨停买不买得进、停牌顺不顺延、费用按不按最低佣金收、现金不够会不会砍成整手。
全部离线（合成日 K），不触网、不连 QMT。
"""

from __future__ import annotations

import sqlite3

import pytest
from rpsboard import db as dbmod
from rpsboard.broker import plan as plan_mod
from rpsboard.broker import sim

from .test_trade_plan import _build

CASH = 1_000_000.0


@pytest.fixture()
def simdb(tmp_path):
    """一个只含 daily_bar / price_limit_daily / 交易表的小库。"""
    path = tmp_path / "sim.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(dbmod.SCHEMA)
    yield path
    conn.close()


def _bars(conn, rows, date):
    """rows: (symbol, open, high, low, close, tradestatus)"""
    conn.executemany(
        "INSERT OR REPLACE INTO daily_bar(symbol, date, open, high, low, close,"
        " pct_chg, tradestatus, is_st) VALUES (?,?,?,?,?,?,0,?,0)",
        [(s, date, o, h, lo, c, ts) for s, o, h, lo, c, ts in rows])
    conn.commit()


def _limits(conn, rows, date):
    """rows: (symbol, ratio, status)"""
    conn.executemany(
        "INSERT OR REPLACE INTO price_limit_daily(date, symbol, limit_ratio, rule, status)"
        " VALUES (?,?,?,'main',?)",
        [(date, s, r, st) for s, r, st in rows])
    conn.commit()


def _order(conn, plan_id, trade_date, symbol, side, volume, price):
    oid = sim.next_order_id(conn)
    conn.execute(
        "INSERT INTO trade_order(plan_id, trade_date, symbol, side, volume, price, status,"
        " broker_order_id, created_at, updated_at) VALUES (?,?,?,?,?,?,'pending',?,?,?)",
        (plan_id, trade_date, symbol, side, volume, price, oid,
         dbmod.now(), dbmod.now()))
    conn.commit()


def _status(conn, symbol):
    return conn.execute("SELECT status FROM trade_order WHERE symbol=?",
                        (symbol,)).fetchone()["status"]


def _fill(conn, symbol):
    return conn.execute("SELECT * FROM trade_fill WHERE symbol=?", (symbol,)).fetchone()


# ── 限价撮合 ────────────────────────────────────────────────────────
def test_limit_buy_fills_when_day_low_reaches_price(simdb):
    with dbmod.session(simdb) as conn:
        sim.init_account(conn, CASH)
        # 委托价 10.05，当天最低 9.9 → 挂进去了
        _bars(conn, [("000001", 10.0, 10.4, 9.9, 10.2, 1)], "2026-01-05")
        _bars(conn, [("000001", 10.2, 10.3, 10.1, 10.2, 1)], "2026-01-02")
        _order(conn, 1, "2026-01-05", "000001", "buy", 1000, 10.05)
        acc = sim.load_account(conn)
        st = sim.settle_day(conn, acc, "2026-01-05", "2026-01-02")
        assert st["filled"] == 1
        assert acc.positions["000001"] == 1000
        assert acc.cash < CASH - 1000 * 10.05, "还要扣费用"
        assert _fill(conn, "000001")["price"] == pytest.approx(10.05)


def test_limit_buy_canceled_when_price_never_reaches(simdb):
    with dbmod.session(simdb) as conn:
        sim.init_account(conn, CASH)
        # 当天最低 10.2 > 委托价 10.05 → 挂不进去
        _bars(conn, [("000001", 10.3, 10.5, 10.2, 10.4, 1)], "2026-01-05")
        _bars(conn, [("000001", 10.2, 10.3, 10.1, 10.2, 1)], "2026-01-02")
        _order(conn, 1, "2026-01-05", "000001", "buy", 1000, 10.05)
        acc = sim.load_account(conn)
        st = sim.settle_day(conn, acc, "2026-01-05", "2026-01-02")
        assert st["canceled"] == 1 and st["filled"] == 0
        assert _status(conn, "000001") == "canceled"
        assert "000001" not in acc.positions


def test_limit_sell_fills_only_when_high_reaches(simdb):
    with dbmod.session(simdb) as conn:
        sim.init_account(conn, CASH)
        acc = sim.load_account(conn)
        acc.positions["000001"] = 1000
        acc.avg_cost["000001"] = 10.0
        conn.execute("UPDATE paper_account SET cash=0 WHERE id=1")
        _bars(conn, [("000001", 10.0, 10.2, 9.9, 10.1, 1)], "2026-01-05")
        _bars(conn, [("000001", 10.0, 10.1, 9.8, 10.0, 1)], "2026-01-02")
        _order(conn, 1, "2026-01-05", "000001", "sell", 1000, 9.95)
        st = sim.settle_day(conn, acc, "2026-01-05", "2026-01-02")
        assert st["filled"] == 1
        assert "000001" not in acc.positions
        assert acc.cash > 9_900, "卖出净收入要扣掉费用"
        assert _fill(conn, "000001")["side"] == "sell"


# ── 涨跌停 ──────────────────────────────────────────────────────────
def test_limit_up_day_blocks_buy_and_defers_order(simdb):
    """涨停日买不进 —— 与回测同一条规则，不能比回测宽松。"""
    with dbmod.session(simdb) as conn:
        sim.init_account(conn, CASH)
        # 昨收 10.0，当天开盘 11.0（+10% 涨停），委托价 10.55 挂不进去
        _bars(conn, [("000001", 11.0, 11.0, 11.0, 11.0, 1)], "2026-01-05")
        _bars(conn, [("000001", 10.0, 10.1, 9.9, 10.0, 1)], "2026-01-02")
        _limits(conn, [("000001", 10.0, "normal")], "2026-01-05")
        _order(conn, 1, "2026-01-05", "000001", "buy", 1000, 10.55)
        acc = sim.load_account(conn)
        st = sim.settle_day(conn, acc, "2026-01-05", "2026-01-02")
        assert st["deferred"] == 1 and st["filled"] == 0
        assert _status(conn, "000001") == "pending", "顺延不是撤单"
        assert "000001" not in acc.positions


def test_limit_down_day_blocks_sell(simdb):
    with dbmod.session(simdb) as conn:
        sim.init_account(conn, CASH)
        acc = sim.load_account(conn)
        acc.positions["000001"] = 1000
        acc.avg_cost["000001"] = 10.0
        _bars(conn, [("000001", 9.0, 9.0, 9.0, 9.0, 1)], "2026-01-05")
        _bars(conn, [("000001", 10.0, 10.1, 9.9, 10.0, 1)], "2026-01-02")
        _limits(conn, [("000001", 10.0, "normal")], "2026-01-05")
        _order(conn, 1, "2026-01-05", "000001", "sell", 1000, 9.45)
        st = sim.settle_day(conn, acc, "2026-01-05", "2026-01-02")
        assert st["deferred"] == 1
        assert acc.positions["000001"] == 1000


def test_no_price_limit_status_is_not_blocked(simdb):
    """新股前 5 日无涨跌幅限制：不能因为缺 ratio 就拦掉。"""
    with dbmod.session(simdb) as conn:
        sim.init_account(conn, CASH)
        _bars(conn, [("300001", 11.0, 11.2, 10.4, 11.0, 1)], "2026-01-05")
        _bars(conn, [("300001", 10.0, 10.1, 9.9, 10.0, 1)], "2026-01-02")
        _limits(conn, [("300001", 20.0, "no_price_limit")], "2026-01-05")
        _order(conn, 1, "2026-01-05", "300001", "buy", 1000, 10.55)
        acc = sim.load_account(conn)
        st = sim.settle_day(conn, acc, "2026-01-05", "2026-01-02")
        assert st["filled"] == 1, "无涨跌幅限制的日子不该被涨跌停拦掉"


# ── 停牌顺延 ────────────────────────────────────────────────────────
def test_suspended_symbol_defers_to_next_trading_day(simdb):
    with dbmod.session(simdb) as conn:
        sim.init_account(conn, CASH)
        _bars(conn, [("000001", 10.0, 10.1, 9.9, 10.0, 0)], "2026-01-05")  # 停牌
        _bars(conn, [("000001", 10.0, 10.1, 9.9, 10.0, 1)], "2026-01-02")
        _order(conn, 1, "2026-01-05", "000001", "buy", 1000, 10.05)
        acc = sim.load_account(conn)
        assert sim.settle_day(conn, acc, "2026-01-05", "2026-01-02")["deferred"] == 1
        # 复牌那天同一笔单还在表里
        rows = conn.execute(
            "SELECT volume, price FROM trade_order WHERE symbol='000001'"
            " AND status='pending'").fetchall()
        assert len(rows) == 1 and rows[0]["volume"] == 1000
        assert sim.settle_day(conn, acc, "2026-01-06", "2026-01-05")["filled"] == 0, \
            "复牌日无 bar 时仍不能成交"


# ── 资金与费用 ──────────────────────────────────────────────────────
def test_insufficient_cash_shrinks_to_round_lot(simdb):
    """现金不够时按整手缩减，绝不下成零股或透支。"""
    with dbmod.session(simdb) as conn:
        sim.init_account(conn, 5000.0)
        _bars(conn, [("000001", 10.0, 10.2, 9.9, 10.1, 1)], "2026-01-05")
        _order(conn, 1, "2026-01-05", "000001", "buy", 1000, 10.05)
        acc = sim.load_account(conn)
        st = sim.settle_day(conn, acc, "2026-01-05", "2026-01-05")
        assert st["filled"] == 1
        vol = _fill(conn, "000001")["volume"]
        assert vol % 100 == 0 and 0 < vol < 1000
        assert acc.cash >= 0


def test_minimum_commission_is_charged_on_small_orders(simdb):
    """100 万摊到几十只时，最低 5 元佣金才是真正生效的那一项。"""
    with dbmod.session(simdb) as conn:
        sim.init_account(conn, CASH)
        _bars(conn, [("000001", 10.0, 10.2, 9.9, 10.1, 1)], "2026-01-05")
        _order(conn, 1, "2026-01-05", "000001", "buy", 1000, 10.05)   # 1 万元
        acc = sim.load_account(conn)
        sim.settle_day(conn, acc, "2026-01-05", "2026-01-05")
        spent = CASH - acc.cash
        notional = 1000 * 10.05
        assert spent - notional == pytest.approx(5.0 + notional * 0.00001
                                                 + notional * 0.0005, abs=1e-6), \
            "1 万元的买单：佣金取最低 5 元，另加过户费与滑点"


def test_sell_prints_stamp_tax_at_current_rate(simdb):
    """2023-08-28 之后卖出印花税是 0.05%，不是 0.1%。"""
    from rpsboard.analytics.costs import default_cost_model

    model = default_cost_model(enabled=True)
    with dbmod.session(simdb) as conn:
        sim.init_account(conn, CASH)
        acc = sim.load_account(conn)
        acc.positions["000001"] = 1000
        acc.avg_cost["000001"] = 10.0
        acc.cash = 0.0
        _bars(conn, [("000001", 10.5, 10.6, 10.4, 10.5, 1)], "2026-01-05")
        _order(conn, 1, "2026-01-05", "000001", "sell", 1000, 10.45)
        sim.settle_day(conn, acc, "2026-01-05", "2026-01-05")
        expect_fee = model.sell_cost(1000 * 10.45, sim._as_date("2026-01-05"))
        assert acc.cash == pytest.approx(1000 * 10.45 - expect_fee, abs=1e-6)


# ── 账务 ────────────────────────────────────────────────────────────
def test_mark_to_market_writes_nav_and_positions(simdb):
    with dbmod.session(simdb) as conn:
        sim.init_account(conn, CASH)
        _bars(conn, [("000001", 10.0, 10.0, 10.0, 11.0, 1)], "2026-01-05")
        acc = sim.load_account(conn)
        acc.positions["000001"] = 1000
        acc.avg_cost["000001"] = 10.0
        acc.cash = CASH - 10_000
        row = sim.mark_to_market(conn, acc, "2026-01-05")
        assert row["market_value"] == 11_000
        assert row["total"] == pytest.approx(CASH + 1_000)
        assert sim.positions_at(conn)[0]["close"] == 11.0
        # 重跑同一天不堆行
        sim.mark_to_market(conn, acc, "2026-01-05")
        n = conn.execute("SELECT COUNT(*) AS c FROM paper_nav").fetchone()["c"]
        assert n == 1


def test_initial_cash_is_not_overwritten_on_reinit(simdb):
    """重跑整个区间从库里已有的进度接着跑，而不是重新注资。"""
    with dbmod.session(simdb) as conn:
        sim.init_account(conn, CASH)
        conn.execute("UPDATE paper_account SET cash = 12345 WHERE id = 1")
        conn.commit()
        assert sim.init_account(conn, CASH).cash == 12345


def test_order_ids_are_monotonic_and_out_of_broker_range(simdb):
    with dbmod.session(simdb) as conn:
        a = sim.next_order_id(conn)
        b = sim.next_order_id(conn)
        assert b == a + 1
        assert a >= sim.ORDER_ID_BASE


def test_reset_clears_everything(simdb):
    with dbmod.session(simdb) as conn:
        sim.init_account(conn, CASH)
        sim.reset_account(conn)
        assert conn.execute("SELECT COUNT(*) AS c FROM paper_account").fetchone()["c"] == 0
        with pytest.raises(sim.SimError, match="模拟账户不存在"):
            sim.load_account(conn)


# ── 计划侧：孤儿清仓 + 权益口径预算（两个真实 bug 的回归锁）─────────
def test_orphan_holding_is_liquidated_when_it_leaves_top_n(trade_db):
    """掉出候选的旧持仓必须清掉，否则组合只进不出（实测 91 天涨到 42 只）。"""
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        first_pid, _ = _build(conn, plan_date)
        top = {i["symbol"] for i in plan_mod.load_plan(conn, first_pid)[1]}
        outside = next(r["symbol"] for r in conn.execute(
            "SELECT DISTINCT symbol FROM daily_bar WHERE date=? ORDER BY symbol",
            (plan_date,)) if r["symbol"] not in top)
        conn.execute(
            "INSERT OR REPLACE INTO paper_account(id, cash, created_at, updated_at)"
            " VALUES (1, 950000, '2026-01-01', '2026-01-01')")
        conn.execute(
            "INSERT OR REPLACE INTO paper_nav(trade_date, cash, market_value, total, nav,"
            " n_positions) VALUES (?, 950000, 27000, 977000, 1.0, 1)", (plan_date,))
        conn.execute(
            "INSERT OR REPLACE INTO paper_position(trade_date, symbol, volume, avg_price,"
            " close, market_value) VALUES (?,?,?,?,?,?)",
            (plan_date, outside, 3000, 9.0, 9.0, 27000.0))
        conn.commit()
        pid, _ = _build(conn, plan_date, mode="local", cash_budget=0.0,
                        liquidate_orphans=True)
        item = {i["symbol"]: i for i in plan_mod.load_plan(conn, pid)[1]}[outside]
    assert item["reason"] == "orphan" and item["side"] == "sell"
    assert item["target_volume"] == 0 and item["held"] == 3000
    assert item["plan_price"] is not None, "清仓也要挂限价单，取不到价就卖不掉"


def test_budget_uses_total_equity_not_just_cash(trade_db):
    """等权再平衡分的是**全部权益**；只看现金会让组合再也买不进票。"""
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO paper_account(id, cash, created_at, updated_at)"
            " VALUES (1, 100000, '2026-01-01', '2026-01-01')")
        conn.execute(
            "INSERT OR REPLACE INTO paper_position(trade_date, symbol, volume, avg_price,"
            " close, market_value) VALUES (?, '000001', 1000, 10.0, 10.0, 900000)",
            (plan_date,))
        conn.execute(
            "INSERT OR REPLACE INTO paper_nav(trade_date, cash, market_value, total, nav,"
            " n_positions) VALUES (?, 100000, 900000, 1000000, 1.0, 1)", (plan_date,))
        conn.commit()
        pid, _ = _build(conn, plan_date, mode="local", cash_budget=0.0,
                        max_positions=20)
        plan = plan_mod.load_plan(conn, pid)[0]
    # 权益 100 万 ×0.95 = 95 万，每只 4.75 万；只看现金 10 万的话每只只有 4750
    assert plan["cash_budget"] == pytest.approx(950_000, rel=1e-6)
    assert plan["params"]["per_name"] == pytest.approx(47_500, rel=1e-6)


# ── 信号源：多因子合成分 ────────────────────────────────────────────
def test_build_plan_composite_ranking(trade_db, monkeypatch):
    """signal=composite：取前 M 只的排序键换成合成分，并落进 params/note。

    两处都要覆盖：调用方传入 `score`（逐日循环复用）与不传（`cli trade plan`
    的默认路径，函数内部自己算）必须得到同一份候选。
    """
    from rpsboard.analytics import backtest as bt
    from rpsboard.analytics import factors as fac

    monkeypatch.setattr(fac, "MIN_CROSS_IC", 3)      # 合成库只有 40 只票
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn:
        _, five = plan_mod.load_plan(conn, _build(
            conn, plan_date, threshold=0.0, max_positions=5, signal="composite")[0])
        plan_all, all_items = plan_mod.load_plan(conn, _build(
            conn, plan_date, threshold=0.0, max_positions=40, signal="composite")[0])
        ctx = bt.load_context(conn, None, with_ohlcv=True)
        score = fac.composite_score(ctx, conn, period=60, hold=20, exclude_st=True)

    assert plan_all["params"]["signal"] == "composite"
    assert "合成分" in plan_all["note"]
    full = [it["symbol"] for it in all_items]
    scores = [float(score.loc[plan_date, s]) for s in full]
    assert scores == sorted(scores, reverse=True), "候选必须按合成分降序"
    assert full[:5] == [it["symbol"] for it in five], "max_positions=5 应恰好是全量前 5 只"


def test_build_plan_rejects_unknown_signal(trade_db):
    """非法信号源硬报错（不像回测那样静默回落）：计划要落库，不能带错口径。"""
    path, plan_date, _ = trade_db
    with dbmod.session(path) as conn, pytest.raises(plan_mod.PlanError, match="signal="):
        _build(conn, plan_date, signal="ml")
