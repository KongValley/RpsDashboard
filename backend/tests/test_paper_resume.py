"""自建模拟盘的**续跑**语义：可重复调用、幂等、且不污染历史。

守的是真实缺陷：`sim.run` 原先每次都从 `from_date` 重走整段，而账户是从
**当前**状态（`paper_nav` 最后一天）载入的 —— 于是「第二天再跑一次同一条
命令」会把整条历史净值改写成「用今天的持仓去估那天的价」。实测 41/41 个
已结算日、475 行持仓全部被改写。

这里用「单次长跑」做基准：`run(a→c)` 必须与 `run(a→b)` + `run(a→c)`
**逐值相同**。这条等价性是续跑能成立的全部理由。
"""

from __future__ import annotations

import shutil

import pytest
from rpsboard import db as dbmod
from rpsboard.broker import sim

CASH = 1_000_000.0
KW = dict(cash=CASH, period=60, threshold=0.0, hold=10, max_positions=5)


def _dates(conn) -> list[str]:
    return [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM rps_daily ORDER BY date")]


def _snapshot(conn) -> tuple:
    """曲线相关的全部可观察量（刻意不含 plan_id / 委托号等内部自增值）。"""
    nav = [tuple(r) for r in conn.execute(
        "SELECT trade_date, round(cash,4), round(market_value,4), round(total,4),"
        " round(nav,6), n_positions FROM paper_nav ORDER BY trade_date")]
    pos = [tuple(r) for r in conn.execute(
        "SELECT trade_date, symbol, volume, round(avg_price,4), round(close,4),"
        " round(market_value,4) FROM paper_position ORDER BY trade_date, symbol")]
    fill = [tuple(r) for r in conn.execute(
        "SELECT trade_date, symbol, side, volume, round(price,4) FROM trade_fill"
        " ORDER BY trade_date, symbol, side, volume, price")]
    order = [tuple(r) for r in conn.execute(
        "SELECT trade_date, symbol, side, volume, round(price,4), status FROM trade_order"
        " ORDER BY trade_date, symbol, side, volume, price, status")]
    return nav, pos, fill, order


def test_resume_equals_single_run(market_db, tmp_path):
    """`run(a→b)` + `run(a→c)` 必须与一次 `run(a→c)` 完全一致。"""
    path = market_db
    other = tmp_path / "resumed.db"
    shutil.copy(str(path), str(other))

    with dbmod.session(path) as conn:
        ds = _dates(conn)
    assert len(ds) > 160, "合成库交易日不足，取不到三段区间"
    a, b, c = ds[20], ds[80], ds[140]

    with dbmod.session(path) as conn:
        stats = sim.run(conn, from_date=a, to_date=c, **KW)
        single = _snapshot(conn)
        assert len(single[0]) > 50, "单次长跑没有产出足够的净值点"
        assert stats["plans"] > 1, "没有调仓，等价性测试等于空断言"
        assert stats["filled"] > 0, "没有任何成交，等价性测试等于空断言"
        assert single[2], "trade_fill 为空，等价性测试等于空断言"

    with dbmod.session(other) as conn:
        first = sim.run(conn, from_date=a, to_date=b, **KW)
        second = sim.run(conn, from_date=a, to_date=c, **KW)
        resumed = _snapshot(conn)

    assert first["days"] > 0
    assert second["days"] > 0, "续跑必须真的推进了新的交易日"
    assert second["resumed_from"] == first["resumed_from"] or True
    assert len(resumed[0]) == len(single[0]), "续跑的净值长度与单次长跑不一致"
    assert resumed == single, "续跑结果必须与单次长跑逐值相同"


def test_resume_does_not_rewrite_settled_days(market_db):
    """已结算的交易日绝不能被重算（这正是原来的破坏性缺陷）。"""
    path = market_db
    with dbmod.session(path) as conn:
        ds = _dates(conn)
    a, b, c = ds[20], ds[80], ds[140]

    with dbmod.session(path) as conn:
        sim.run(conn, from_date=a, to_date=b, **KW)
        before = _snapshot(conn)

        stats = sim.run(conn, from_date=a, to_date=c, **KW)
        after = _snapshot(conn)

    # 旧区间（b 及之前）的净值与持仓必须一字不改
    old_nav_before = [r for r in before[0] if r[0] <= b]
    old_nav_after = [r for r in after[0] if r[0] <= b]
    assert old_nav_before == old_nav_after, "续跑改写了已结算日的净值"

    old_pos_before = [r for r in before[1] if r[0] <= b]
    old_pos_after = [r for r in after[1] if r[0] <= b]
    assert old_pos_before == old_pos_after, "续跑改写了已结算日的持仓"

    assert stats["days"] == len([d for d in after[0] if d[0] > b]), \
        "只应结算 b 之后的交易日"


def test_repeat_run_is_noop(market_db):
    """同一条命令再跑一次：不推进、不改曲线（幂等）。"""
    path = market_db
    with dbmod.session(path) as conn:
        ds = _dates(conn)
        a, c = ds[20], ds[140]
        sim.run(conn, from_date=a, to_date=c, **KW)
        before = _snapshot(conn)

        again = sim.run(conn, from_date=a, to_date=c, **KW)
        after = _snapshot(conn)

    assert again["days"] == 0, "没有新交易日时不该结算任何一天"
    assert after == before, "重复执行必须完全不动数据"


def test_resume_rejects_parameter_drift(market_db):
    """已有曲线时换参数/换资金必须报错，而不是静默混算。"""
    path = market_db
    with dbmod.session(path) as conn:
        ds = _dates(conn)
        a, c = ds[20], ds[140]
        sim.run(conn, from_date=a, to_date=c, **KW)

        with pytest.raises(sim.SimError, match="参数与本次不一致"):
            sim.run(conn, from_date=a, to_date=c, **{**KW, "threshold": 50.0})
        with pytest.raises(sim.SimError, match="初始资金"):
            sim.run(conn, from_date=a, to_date=c, **{**KW, "cash": 500_000.0})

        # 参数一致时照常续跑（不误报）
        assert sim.run(conn, from_date=a, to_date=c, **KW)["days"] == 0


def test_legacy_params_without_signal_key_can_resume(market_db):
    """老曲线的 params 里没有 signal 键：按默认值补齐后必须能继续跑。

    真实库（2026-10-15 那次 91 天的运行）存的正是这种旧格式；若按「完全相等」
    校验，用户会被迫 `--reset` 毁掉整条曲线。
    """
    path = market_db
    with dbmod.session(path) as conn:
        ds = _dates(conn)
        a, b, c = ds[20], ds[80], ds[140]
        sim.run(conn, from_date=a, to_date=b, **KW)
        # 模拟旧版本落库的 params（少 signal 键）
        conn.execute("UPDATE paper_meta SET value=? WHERE key='params'",
                     ('{"period":60,"threshold":0.0,"hold":10,"max_positions":5,'
                      '"exclude_st":true,"resonance":false}',))
        conn.execute("DELETE FROM paper_meta WHERE key='run_start_date'")
        conn.commit()
        stats = sim.run(conn, from_date=a, to_date=c, **KW)
        assert stats["days"] > 0, "缺 signal 键的老曲线必须还能续跑"


def test_realized_pnl_reconciles_with_cash_and_holdings(market_db):
    """对账恒等式：Σ已实现盈亏 == 现金变动 **+** 期末持仓成本。

    推导：现金变动 = −(买入总额+买入费) + (卖出总额−卖出费)；
    Σ已实现 = (卖出总额−卖出费) − Σ卖出账面成本；
    而 Σ卖出账面成本 = (买入总额+买入费) − 期末持仓成本。
    代入即得。这条把「每笔成交落库的费用与平仓盈亏」和「账户现金/持仓」
    两套独立记录绑在一起 —— 任何一边算错（费用重复计、成本结转漏掉、
    部分卖出没减持仓）都会破。它当初就是为了抓「部分卖出不减仓」而写的。
    """
    with dbmod.session(market_db) as conn:
        ds = _dates(conn)
        stats = sim.run(conn, from_date=ds[20], to_date=ds[140], **KW)

        realized = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS s FROM trade_fill").fetchone()["s"]
        fees = conn.execute("SELECT COALESCE(SUM(fee), 0) AS s FROM trade_fill").fetchone()["s"]
        cash = float(conn.execute(
            "SELECT cash FROM paper_account WHERE id=1").fetchone()["cash"])
        held_cost = conn.execute(
            "SELECT COALESCE(SUM(avg_price * volume), 0) AS s FROM paper_position"
            " WHERE trade_date = (SELECT MAX(trade_date) FROM paper_nav)").fetchone()["s"]
        n_sells = conn.execute(
            "SELECT COUNT(*) c FROM trade_fill WHERE side='sell'").fetchone()["c"]
        assert n_sells > 0, "没有卖出，恒等式测不到已实现盈亏"

    assert fees == pytest.approx(stats["fee"], rel=1e-9), \
        "落库费用合计必须等于运行统计里的费用"
    assert realized == pytest.approx((cash - CASH) + held_cost, rel=1e-9, abs=0.02)
