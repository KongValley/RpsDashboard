"""模拟盘复盘（`sim.review`）：逐笔回合、绩效汇总、基准对照、行业分布。

复盘的价值全在"数字对不对"，所以这里断言的是**与成交记录的一致性**
（落库的 realized_pnl / fee 合计、持仓权重），而不是"结构里有这些键"。
"""

from __future__ import annotations

import pytest
from rpsboard import db as dbmod
from rpsboard.broker import sim

from .test_paper_resume import KW


def _run(conn) -> list[str]:
    ds = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM rps_daily ORDER BY date")]
    sim.run(conn, from_date=ds[20], to_date=ds[140], **KW)
    return ds


def test_review_empty_account_returns_shape(market_db):
    """没跑过模拟盘时返回同构空结构，不是 500。"""
    with dbmod.session(market_db) as conn:
        rev = sim.review(conn)
    assert rev["days"] == 0 and rev["round_trips"] == []
    assert rev["summary"] == {} and rev["positions"] == []
    assert rev["benchmark"] is None and rev["industry"] == []


def test_review_matches_stored_fills(market_db):
    """逐笔回合的盈亏/费用合计必须与 trade_fill 里落库的完全一致。"""
    with dbmod.session(market_db) as conn:
        _run(conn)
        rev = sim.review(conn)
        stored_pnl = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) s FROM trade_fill").fetchone()["s"]
        stored_fee = conn.execute(
            "SELECT COALESCE(SUM(fee), 0) s FROM trade_fill").fetchone()["s"]
        n_sell = conn.execute(
            "SELECT COUNT(*) c FROM trade_fill WHERE side='sell'").fetchone()["c"]

    s = rev["summary"]
    assert rev["days"] > 0
    assert s["round_trips"] == len(rev["round_trips"]) == n_sell, \
        "每个卖出成交对应一个回合"
    assert s["realized_total"] == pytest.approx(round(stored_pnl, 2), abs=0.02)
    assert s["fees_total"] == pytest.approx(round(stored_fee, 2), abs=0.02)
    assert s["wins"] + s["losses"] <= s["round_trips"]
    assert s["return"] == pytest.approx(rev["summary"]["return"])
    assert all(r["holding_days"] is None or r["holding_days"] >= 0
               for r in rev["round_trips"])
    assert all(r["unmatched_volume"] == 0 for r in rev["round_trips"]), \
        "正常成交不该出现配不上买入成本的卖出"


def test_review_reports_annualized_metrics(market_db):
    """年化/波动/夏普：口径必须与区间收益同源，且不许编造。"""
    with dbmod.session(market_db) as conn:
        _run(conn)
        rev = sim.review(conn)

    s = rev["summary"]
    assert s["years"] > 0, "合成库跨了几个月，年化年限不该是 0"
    assert s["volatility"] >= 0
    assert (s["annualized"] > 0) == (s["return"] > 0), \
        "年化方向必须与区间收益一致（都由 nav 推出，不该打架）"
    if s["volatility"] > 0:
        assert s["sharpe"] == pytest.approx(s["annualized"] / s["volatility"], abs=0.01)
    # 口径注意：模拟盘侧的 max_drawdown 是**正数幅度**（与 sim.summary 一致），
    # 而回测侧（compute_metrics）给的是负数 —— 两边各自渲染，别互相套用
    assert s["max_drawdown"] > 0, "模拟盘的口径是正数幅度"
    assert s["calmar"] == pytest.approx(s["annualized"] / s["max_drawdown"], abs=0.05)


def test_review_metrics_are_not_fabricated_on_tiny_curve(market_db):
    """只有 1 个净值点时不该给出夏普（样本不足就返回 None）。"""
    with dbmod.session(market_db) as conn:
        conn.execute(
            "INSERT INTO paper_account(id, cash, created_at, updated_at)"
            " VALUES (1, 1000000, '2026-01-05', '2026-01-05')")
        conn.execute(
            "INSERT INTO paper_nav(trade_date, cash, market_value, total, nav, n_positions)"
            " VALUES ('2026-01-05', 1000000, 0, 1000000, 1.0, 0)")
        conn.commit()
        rev = sim.review(conn)
    s = rev["summary"]
    assert s["round_trips"] == 0
    assert s["sharpe"] is None and s["calmar"] is None


def test_review_positions_and_industry_exposure(market_db):
    """当前持仓的浮动盈亏、权重、行业分布必须与 paper_position 对得上。"""
    with dbmod.session(market_db) as conn:
        _run(conn)
        rev = sim.review(conn)
        last = conn.execute(
            "SELECT trade_date, total FROM paper_nav ORDER BY trade_date DESC LIMIT 1"
        ).fetchone()
        held = [dict(r) for r in conn.execute(
            "SELECT * FROM paper_position WHERE trade_date = ?", (last["trade_date"],))]

    assert {p["symbol"] for p in rev["positions"]} == {h["symbol"] for h in held}
    assert sum(p["market_value"] for p in rev["positions"]) == pytest.approx(
        sum(float(h["market_value"]) for h in held), rel=1e-9)
    assert sum(p["weight"] for p in rev["positions"]) <= 1.0 + 1e-9
    assert rev["summary"]["unrealized_total"] == pytest.approx(
        sum(p["unrealized_pnl"] for p in rev["positions"]), abs=0.02)
    assert rev["industry"], "合成库有 industry_map，行业分布不该为空"
    assert rev["industry"][0]["n"] >= rev["industry"][-1]["n"]
    assert sum(i["market_value"] for i in rev["industry"]) == pytest.approx(
        sum(p["market_value"] for p in rev["positions"]), rel=1e-9)


def test_review_benchmark_excess(market_db):
    """基准对照：读 benchmark_bar，给出同期基准涨幅与超额。"""
    with dbmod.session(market_db) as conn:
        _run(conn)
        rows = list(conn.execute(
            "SELECT DISTINCT date FROM rps_daily ORDER BY date"))
        dates = [r["date"] for r in rows]
        lo, hi = dates[20], dates[140]
        conn.executemany(
            "INSERT INTO benchmark_bar(symbol, date, close) VALUES ('000300',?,?)",
            [(lo, 1000.0), (dates[100], 1100.0), (hi, 900.0)])
        conn.commit()
        rev = sim.review(conn)

    b = rev["benchmark"]
    assert b is not None and b["symbol"] == "000300"
    assert b["return"] == pytest.approx(900.0 / 1000.0 - 1, abs=1e-6)
    assert b["excess"] == pytest.approx(rev["summary"]["return"] - b["return"], abs=1e-6)


def test_review_without_fee_columns_is_marked_in_notes(market_db):
    """迁移前的历史成交（fee=0 / realized NULL）必须明说"不含费用"，不能装精确。"""
    with dbmod.session(market_db) as conn:
        _run(conn)
        conn.execute("UPDATE trade_fill SET fee = 0, realized_pnl = NULL,"
                     " avg_cost = NULL")
        conn.commit()
        rev = sim.review(conn)

    assert any("不含费用" in n for n in rev["notes"]), rev["notes"]
    # 退化路径仍要给出可用的回合与持有天数
    assert rev["summary"]["round_trips"] == len(rev["round_trips"]) > 0
    assert rev["summary"]["fees_total"] == 0.0
    assert all(r["buy_price"] is not None for r in rev["round_trips"]), \
        "没有落库成本时也要按买入成交价配出买入均价"


def test_nav_rows_carry_daily_fill_count(market_db):
    """`paper_nav.filled_orders` 必须真的写入（这列此前永远 0）。"""
    with dbmod.session(market_db) as conn:
        _run(conn)
        rows = [dict(r) for r in conn.execute(
            "SELECT trade_date, filled_orders FROM paper_nav ORDER BY trade_date")]
        fills = conn.execute("SELECT COUNT(*) c FROM trade_fill").fetchone()["c"]

    assert fills > 0, "合成行情下应有成交"
    assert sum(r["filled_orders"] for r in rows) == fills, \
        "逐日成交笔数之和必须等于成交总数"


def test_truncated_curve_is_flagged(market_db, monkeypatch):
    """曲线点数被上限截断时必须显式标记（原先静默截断，区间收益/回撤会被误读）。"""
    with dbmod.session(market_db) as conn:
        _run(conn)
        full = sim.summary(conn)
        monkeypatch.setattr(sim, "NAV_CURVE_LIMIT", 3)
        cut = sim.summary(conn)

    assert full["curve_truncated"] is False and len(full["curve"]) > 3
    assert cut["curve_truncated"] is True
    assert len(cut["curve"]) == 3 and cut["curve_limit"] == 3
