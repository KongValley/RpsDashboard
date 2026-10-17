"""撮合规则的行为测试（新多账户引擎）。

老的单账户引擎测试（test_paper_sim.py / test_paper_resume.py /
test_paper_review.py / test_paper_archive.py）测的是**已经不存在的接口**
（`sim.init_account` / `sim.run` / `sim.reset_account` / 单账户 `paper_*`），
按计划随实现一起退休；其中真正有价值的**撮合规则**在这里用新接口重写，
其余行为（跨日追加、账户隔离、公司行为、指标口径）在 test_portfolio.py。

规则本身没变：限价判定、涨停买不进/跌停卖不掉、停牌顺延、参与率上限、
整手/步长、最低佣金、印花税按日分段。
"""

from __future__ import annotations

import sqlite3

import pytest
from rpsboard import config, db
from rpsboard.broker import accounts as accounts_mod
from rpsboard.broker import sim

D0, D1, D2, D3 = "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"


def _mk(tmp_path, monkeypatch, days=(D0, D1, D2, D3), *, cash=200_000.0):
    """最小引擎库：只放这些交易日与一只股票（默认 10 元、可成交 1000 股/天）。"""
    path = tmp_path / "rules.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(db.SCHEMA)
    db.ensure_paper_accounts(conn)
    for d in days:
        conn.execute("INSERT INTO benchmark_bar(symbol,date,close) VALUES ('000001',?,1)",
                     (d,))
        conn.execute("INSERT OR REPLACE INTO raw_coverage(symbol,date_from,date_to,"
                     "updated_at) VALUES ('000001',?,?,'x')", (days[0], d))
        conn.execute("INSERT OR REPLACE INTO corporate_sync(symbol,checked_through,"
                     "updated_at) VALUES ('000001',?,'x')", (d,))
    conn.execute("INSERT INTO security_master(symbol,name,exchange,is_st,updated_at)"
                 " VALUES ('000001','测试','sz',0,'x')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    with db.session(path) as c:
        acc = accounts_mod.create_account(c, name="规则", kind="manual", cash=cash,
                                          start_date=days[0], participation=0.1)
        c.execute("DELETE FROM paper_nav WHERE account_id=?", (acc["id"],))
        c.execute("DELETE FROM paper_position WHERE account_id=?", (acc["id"],))
        c.commit()
    return path, acc["id"]


def _bar(conn, day, o, h, lo, c, vol=100_000.0):
    conn.execute("INSERT OR REPLACE INTO raw_bar(symbol,date,open,high,low,close,"
                 "volume,amount,tradestatus) VALUES ('000001',?,?,?,?,?,?,?,1)",
                 (day, o, h, lo, c, vol, c * vol))


def _limit(conn, day, up, dn, ratio=10.0, status="normal"):
    conn.execute("INSERT OR REPLACE INTO price_limit_daily(date,symbol,limit_ratio,"
                 "limit_up,limit_down,rule,status) VALUES (?,?,?,?,?,'main',?)",
                 (day, "000001", ratio, up, dn, status))


def _order(conn, aid, day, side, volume, price, *, source="manual", rid=None):
    """直接落一笔委托（跳过下单校验，专注撮合规则）。"""
    conn.execute(
        "INSERT INTO paper_order(account_id, request_id, trade_date, symbol, side,"
        " volume, price, status, source, thesis, created_at, updated_at)"
        " VALUES (?,?,?,? ,?,?,?,'pending',?,'', 'x','x')",
        (aid, rid or f"{day}-{side}-{volume}", day, "000001", side, volume, price,
         source))
    conn.commit()


def _status(conn, aid):
    row = conn.execute("SELECT status FROM paper_order WHERE account_id=?",
                       (aid,)).fetchone()
    return row["status"]


def _seed_day(conn, day, o, h, lo, c, up, dn, vol=100_000.0, ratio=10.0):
    _bar(conn, day, o, h, lo, c, vol)
    _limit(conn, day, up, dn, ratio)


def test_buy_fills_at_open_when_open_below_limit(tmp_path, monkeypatch):
    path, aid = _mk(tmp_path, monkeypatch)
    with db.session(path) as conn:
        _seed_day(conn, D0, 10, 10, 10, 10, 11, 9)
        _seed_day(conn, D1, 10.0, 10.4, 9.9, 10.2, 11, 9)
        conn.commit()
        sim.advance(conn, account_id=aid, to_date=D0)
        _order(conn, aid, D0, "buy", 1000, 10.05)
        sim.advance(conn, account_id=aid, to_date=D1)
        f = conn.execute("SELECT * FROM paper_fill").fetchone()
        assert f["price"] == pytest.approx(10.00), "开盘价 ≤ 限价 → 按开盘成交"
        assert int(f["volume"]) == 1000
        assert _status(conn, aid) == "filled"


def test_buy_fills_at_limit_when_open_above(tmp_path, monkeypatch):
    path, aid = _mk(tmp_path, monkeypatch)
    with db.session(path) as conn:
        _seed_day(conn, D0, 10, 10, 10, 10, 11, 9)
        _seed_day(conn, D1, 10.3, 10.5, 9.9, 10.4, 11, 9)   # 开盘高于限价，最低价触及
        conn.commit()
        sim.advance(conn, account_id=aid, to_date=D0)
        _order(conn, aid, D0, "buy", 1000, 10.05)
        sim.advance(conn, account_id=aid, to_date=D1)
        f = conn.execute("SELECT * FROM paper_fill").fetchone()
        assert f["price"] == pytest.approx(10.05), "触及限价 → 按限价成交"


def test_buy_canceled_when_limit_never_reached(tmp_path, monkeypatch):
    path, aid = _mk(tmp_path, monkeypatch)
    with db.session(path) as conn:
        _seed_day(conn, D0, 10, 10, 10, 10, 11, 9)
        _seed_day(conn, D1, 10.3, 10.5, 10.2, 10.4, 11, 9)
        conn.commit()
        sim.advance(conn, account_id=aid, to_date=D0)
        _order(conn, aid, D0, "buy", 1000, 10.05)
        sim.advance(conn, account_id=aid, to_date=D1)
        assert _status(conn, aid) == "canceled"
        assert conn.execute("SELECT COUNT(*) c FROM paper_fill").fetchone()["c"] == 0


def test_sell_fills_only_when_high_reaches(tmp_path, monkeypatch):
    path, aid = _mk(tmp_path, monkeypatch)
    with db.session(path) as conn:
        _seed_day(conn, D0, 10, 10, 10, 10, 11, 9)
        _seed_day(conn, D1, 10.0, 10.2, 9.9, 10.1, 11, 9)
        conn.execute("INSERT INTO paper_position(account_id, trade_date, symbol,"
                     " volume, available_volume, avg_price, cost_basis, close,"
                     " price_date, market_value)"
                     " VALUES (?,?,'000001',1000,1000,10,10000,10,?,10000)",
                     (aid, D0, D0))
        conn.execute("INSERT INTO paper_nav(account_id, trade_date, cash, market_value,"
                     " receivable, total, nav, n_positions, filled_orders)"
                     " VALUES (?,?,1000,10000,0,11000,1,1,0)", (aid, D0))
        conn.commit()
        _order(conn, aid, D0, "sell", 1000, 9.95)
        sim.advance(conn, account_id=aid, to_date=D1)
        f = conn.execute("SELECT * FROM paper_fill").fetchone()
        # 开盘 10.00 高于限价 9.95 → 按更优的开盘价成交（卖方不会吃亏）
        assert f["side"] == "sell" and f["price"] == pytest.approx(10.00)
        state = sim.load_account(conn, account_id=aid)
        assert "000001" not in state.positions


def test_limit_up_day_blocks_buy_then_retries(tmp_path, monkeypatch):
    path, aid = _mk(tmp_path, monkeypatch)
    with db.session(path) as conn:
        _seed_day(conn, D0, 10, 10, 10, 10, 11, 9)
        _seed_day(conn, D1, 11, 11, 11, 11, 11, 9)      # 一字涨停
        _seed_day(conn, D2, 11, 11, 11, 11, 11, 9)
        _seed_day(conn, D3, 10.4, 10.6, 9.9, 10.5, 11, 9)  # 回落，能成交
        conn.commit()
        sim.advance(conn, account_id=aid, to_date=D0)
        _order(conn, aid, D0, "buy", 1000, 10.05)
        sim.advance(conn, account_id=aid, to_date=D2)
        assert "000001" not in sim.load_account(conn, account_id=aid).positions
        assert conn.execute("SELECT COUNT(*) c FROM paper_fill").fetchone()["c"] == 0
        assert _status(conn, aid) == "deferred", "涨停是顺延，不是撤单"
        sim.advance(conn, account_id=aid, to_date=D3)
        assert sim.load_account(conn, account_id=aid).positions["000001"] == 1000


def test_limit_down_day_blocks_sell(tmp_path, monkeypatch):
    path, aid = _mk(tmp_path, monkeypatch)
    with db.session(path) as conn:
        _seed_day(conn, D0, 10, 10, 10, 10, 11, 9)
        _seed_day(conn, D1, 9, 9, 9, 9, 11, 9)          # 一字跌停
        conn.execute("INSERT INTO paper_position(account_id, trade_date, symbol,"
                     " volume, available_volume, avg_price, cost_basis, close,"
                     " price_date, market_value)"
                     " VALUES (?,?,'000001',1000,1000,10,10000,10,?,10000)",
                     (aid, D0, D0))
        conn.execute("INSERT INTO paper_nav(account_id, trade_date, cash, market_value,"
                     " receivable, total, nav, n_positions, filled_orders)"
                     " VALUES (?,?,1000,10000,0,11000,1,1,0)", (aid, D0))
        conn.commit()
        _order(conn, aid, D0, "sell", 1000, 9.45)
        sim.advance(conn, account_id=aid, to_date=D1)
        assert sim.load_account(conn, account_id=aid).positions["000001"] == 1000
        assert _status(conn, aid) == "deferred"


def test_no_price_limit_status_is_not_blocked(tmp_path, monkeypatch):
    path, aid = _mk(tmp_path, monkeypatch)
    with db.session(path) as conn:
        _seed_day(conn, D0, 10, 10, 10, 10, 11, 9)
        _bar(conn, D1, 11, 11, 11, 11)     # 涨了一大截
        _limit(conn, D1, 11, 9, status="no_price_limit")
        conn.commit()
        sim.advance(conn, account_id=aid, to_date=D0)
        _order(conn, aid, D0, "buy", 1000, 10.5)
        sim.advance(conn, account_id=aid, to_date=D1)
        assert _status(conn, aid) == "canceled", "无涨跌幅限制日不该被当成封板"
        assert "000001" not in sim.load_account(conn, account_id=aid).positions


def test_suspended_day_defers_without_cancelling(tmp_path, monkeypatch):
    path, aid = _mk(tmp_path, monkeypatch)
    with db.session(path) as conn:
        _seed_day(conn, D0, 10, 10, 10, 10, 11, 9)
        conn.commit()
        sim.advance(conn, account_id=aid, to_date=D0)
        _order(conn, aid, D0, "buy", 1000, 10.05)
        # D1 没有行情：未确认停牌 → 阻断（不猜停牌）
        res = sim.advance(conn, account_id=aid, to_date=D1)
        assert res["ok"] is False and res["blocked_reason"] == "raw_bar_missing"
        conn.execute("INSERT INTO suspension_confirmation(symbol,date,source_note,"
                     "created_at) VALUES ('000001',?,'公告','x')", (D1,))
        conn.commit()
        res2 = sim.advance(conn, account_id=aid, to_date=D1)
        assert res2["ok"] is True
        assert _status(conn, aid) == "deferred", "停牌是顺延，不是撤单"


def test_participation_cap_splits_fill(tmp_path, monkeypatch):
    path, aid = _mk(tmp_path, monkeypatch)
    with db.session(path) as conn:
        _seed_day(conn, D0, 10, 10, 10, 10, 11, 9)
        _seed_day(conn, D1, 10, 10.6, 9.9, 10.5, 11, 9, vol=5000)   # 10% = 500 股
        _seed_day(conn, D2, 10.5, 10.8, 10.0, 10.6, 11, 9, vol=5000)
        conn.commit()
        sim.advance(conn, account_id=aid, to_date=D0)
        _order(conn, aid, D0, "buy", 1000, 10.05)
        sim.advance(conn, account_id=aid, to_date=D1)
        fills = [dict(r) for r in conn.execute("SELECT * FROM paper_fill")]
        assert len(fills) == 1 and int(fills[0]["volume"]) == 500
        assert _status(conn, aid) == "deferred"
        sim.advance(conn, account_id=aid, to_date=D2)
        total = sum(int(r["volume"]) for r in conn.execute(
            "SELECT volume FROM paper_fill"))
        assert total == 1000, "跨日合计正好是委托量，不超买"
        assert _status(conn, aid) == "filled"
        assert conn.execute("SELECT volume FROM paper_order WHERE account_id=?",
                            (aid,)).fetchone()["volume"] == 1000, "原委托量不变"


def test_participation_zero_means_unlimited(tmp_path, monkeypatch):
    path, aid = _mk(tmp_path, monkeypatch)
    with db.session(path) as conn:
        conn.execute("UPDATE paper_meta SET value=? WHERE account_id=? AND key='execution'",
                     ('{"participation": 0.0, "slippage": 0.0}', aid))
        _seed_day(conn, D0, 10, 10, 10, 10, 11, 9)
        _seed_day(conn, D1, 10, 10.6, 9.9, 10.5, 11, 9, vol=500)   # 量很小
        conn.commit()
        sim.advance(conn, account_id=aid, to_date=D0)
        _order(conn, aid, D0, "buy", 1000, 10.05)
        sim.advance(conn, account_id=aid, to_date=D1)
        f = conn.execute("SELECT volume FROM paper_fill").fetchone()
        assert int(f["volume"]) == 1000, "参与率 0 = 关闭比例限制"


def test_capacity_uses_integer_shares_not_fixed_lots(tmp_path, monkeypatch):
    """容量按整数股算（10% × 400 = 40 股），余量顺延 —— 不再按整手截断。

    旧实现 `int(volume * participation) // LOT * LOT` 会把 40 股砍成 0，
    小盘股的调仓于是在「明明能成交」的日子里一笔都不成交。
    """
    path, aid = _mk(tmp_path, monkeypatch)
    with db.session(path) as conn:
        _seed_day(conn, D0, 10, 10, 10, 10, 11, 9)
        _seed_day(conn, D1, 10, 10.6, 9.9, 10.5, 11, 9, vol=400)   # 10% = 40 股
        conn.commit()
        sim.advance(conn, account_id=aid, to_date=D0)
        _order(conn, aid, D0, "buy", 1000, 10.05)
        sim.advance(conn, account_id=aid, to_date=D1)
        f = conn.execute("SELECT volume FROM paper_fill").fetchone()
        assert int(f["volume"]) == 40
        assert _status(conn, aid) == "deferred", "余量顺延"


def test_zero_capacity_day_defers(tmp_path, monkeypatch):
    """容量向下取整为 0（9 股 × 10%）→ 当天一股都吃不下，顺延不撤单。"""
    path, aid = _mk(tmp_path, monkeypatch)
    with db.session(path) as conn:
        _seed_day(conn, D0, 10, 10, 10, 10, 11, 9)
        _seed_day(conn, D1, 10, 10.6, 9.9, 10.5, 11, 9, vol=9)
        conn.commit()
        sim.advance(conn, account_id=aid, to_date=D0)
        _order(conn, aid, D0, "buy", 1000, 10.05)
        sim.advance(conn, account_id=aid, to_date=D1)
        assert conn.execute("SELECT COUNT(*) c FROM paper_fill").fetchone()["c"] == 0
        assert _status(conn, aid) == "deferred"


def test_minimum_commission_is_charged(tmp_path, monkeypatch):
    path, aid = _mk(tmp_path, monkeypatch)
    with db.session(path) as conn:
        _seed_day(conn, D0, 10, 10, 10, 10, 11, 9)
        _seed_day(conn, D1, 10, 10.1, 9.9, 10, 11, 9)
        conn.commit()
        sim.advance(conn, account_id=aid, to_date=D0)
        _order(conn, aid, D0, "buy", 1000, 10.0)     # 1 万元 → 佣金按 5 元起收
        sim.advance(conn, account_id=aid, to_date=D1)
        f = conn.execute("SELECT * FROM paper_fill").fetchone()
        assert float(f["fee"]) == pytest.approx(5.10), "最低佣金 5 + 过户费 0.10"


def test_sell_fee_includes_stamp_tax(tmp_path, monkeypatch):
    path, aid = _mk(tmp_path, monkeypatch)
    with db.session(path) as conn:
        _seed_day(conn, D0, 10, 10, 10, 10, 11, 9)
        _seed_day(conn, D1, 10, 10.2, 9.9, 10.1, 11, 9)
        conn.execute("INSERT INTO paper_position(account_id, trade_date, symbol,"
                     " volume, available_volume, avg_price, cost_basis, close,"
                     " price_date, market_value)"
                     " VALUES (?,?,'000001',1000,1000,10,10000,10,?,10000)",
                     (aid, D0, D0))
        conn.execute("INSERT INTO paper_nav(account_id, trade_date, cash, market_value,"
                     " receivable, total, nav, n_positions, filled_orders)"
                     " VALUES (?,?,1000,10000,0,11000,1,1,0)", (aid, D0))
        conn.commit()
        _order(conn, aid, D0, "sell", 1000, 10.0)
        sim.advance(conn, account_id=aid, to_date=D1)
        f = conn.execute("SELECT * FROM paper_fill").fetchone()
        # 佣金 5 + 过户费 0.10 + 印花税 5.00（2026 年已是 0.05%）
        assert float(f["fee"]) == pytest.approx(10.10)


def test_suspended_symbol_bar_missing_uses_previous_close(tmp_path, monkeypatch):
    path, aid = _mk(tmp_path, monkeypatch)
    with db.session(path) as conn:
        _seed_day(conn, D0, 10, 10, 10, 10, 11, 9)
        conn.execute("INSERT INTO suspension_confirmation(symbol,date,source_note,"
                     "created_at) VALUES ('000001',?,'公告','x')", (D1,))
        conn.execute("INSERT INTO paper_position(account_id, trade_date, symbol,"
                     " volume, available_volume, avg_price, cost_basis, close,"
                     " price_date, market_value)"
                     " VALUES (?,?,'000001',1000,1000,10,10000,10,?,10000)",
                     (aid, D0, D0))
        conn.execute("INSERT INTO paper_nav(account_id, trade_date, cash, market_value,"
                     " receivable, total, nav, n_positions, filled_orders)"
                     " VALUES (?,?,0,10000,0,10000,1,1,0)", (aid, D0))
        conn.commit()
        res = sim.advance(conn, account_id=aid, to_date=D1)
        assert res["ok"] is True
        pos = sim.positions_at(conn, account_id=aid, trade_date=D1)
        assert pos[0]["price_date"] == D0, "停牌日沿用上一个有效原价的日期"
        assert float(pos[0]["market_value"]) == pytest.approx(10_000.0)


def test_gate_blocks_before_missing_day_and_resumes(tmp_path, monkeypatch):
    """缺口只在缺口当天阻断，前面的日子照常结算，补齐后可继续。"""
    path, aid = _mk(tmp_path, monkeypatch)
    with db.session(path) as conn:
        _seed_day(conn, D0, 10, 10, 10, 10, 11, 9)
        _seed_day(conn, D1, 10, 10, 10, 10, 11, 9)
        conn.execute("INSERT INTO paper_position(account_id, trade_date, symbol,"
                     " volume, available_volume, avg_price, cost_basis, close,"
                     " price_date, market_value)"
                     " VALUES (?,?,'000001',1000,1000,10,10000,10,?,10000)",
                     (aid, D0, D0))
        conn.execute("INSERT INTO paper_nav(account_id, trade_date, cash, market_value,"
                     " receivable, total, nav, n_positions, filled_orders)"
                     " VALUES (?,?,0,10000,0,10000,1,1,0)", (aid, D0))
        conn.commit()
        # D2 完全没有行情（也没确认停牌）→ 阻断在 D2 之前
        res = sim.advance(conn, account_id=aid, to_date=D2)
        assert res["ok"] is False and res["blocked_reason"] == "raw_bar_missing"
        assert res["settled_through"] == D1, "缺口之前的日子照常结算"
        _seed_day(conn, D2, 10, 10, 10, 10, 11, 9)
        conn.commit()
        res2 = sim.advance(conn, account_id=aid, to_date=D2)
        assert res2["ok"] is True
        assert sim.last_settled_date(conn, aid) == D2


# ── 申报数量与价格规则（单一真源）──────────────────────────────────
def test_board_of_table():
    from rpsboard.broker import trading_rules as rules

    assert rules.board_of("600519") == "main"
    assert rules.board_of("000001") == "main"
    assert rules.board_of("002415") == "main"
    assert rules.board_of("300750") == "chinext"
    assert rules.board_of("688981") == "star"
    assert rules.board_of("920001") == "bse"
    assert rules.board_of("830799") == "bse"
    assert not rules.is_valid_code("600519.SH")
    assert not rules.is_valid_code("６００５１９")
    with pytest.raises(rules.TradeRuleError):
        rules.board_of("999999")
    with pytest.raises(rules.TradeRuleError):
        rules.board_rule("nope")


def test_validate_sell_allows_odd_lot_cleanup():
    from rpsboard.broker import trading_rules as rules

    # 主板：整手卖出合法；零股必须一次卖完
    rules.validate_order_quantity("000001", "sell", 200, held=1000)
    rules.validate_order_quantity("000001", "sell", 50, held=50)
    with pytest.raises(rules.TradeRuleError):
        rules.validate_order_quantity("000001", "sell", 50, held=1000)
    # 科创板：最小 200，零股一次卖完也放行
    with pytest.raises(rules.TradeRuleError):
        rules.validate_order_quantity("688001", "sell", 150, held=1000)
    rules.validate_order_quantity("688001", "sell", 150, held=150)
    # 非法方向 / 非整数 / 非正
    with pytest.raises(rules.TradeRuleError):
        rules.validate_order_quantity("000001", "hold", 100)
    with pytest.raises(rules.TradeRuleError):
        rules.validate_order_quantity("000001", "buy", "100")
    with pytest.raises(rules.TradeRuleError):
        rules.validate_order_quantity("000001", "buy", 0)


def test_max_legal_buy_volume_uses_board_step():
    from rpsboard.broker import trading_rules as rules

    assert rules.max_legal_buy_volume("000001", 950) == 900      # 主板向下取整手
    assert rules.max_legal_buy_volume("000001", 99) == 0         # 连最小单位都不够
    assert rules.max_legal_buy_volume("688001", 201) == 201      # 科创板步长 1
    assert rules.max_legal_buy_volume("688001", 199) == 0
    assert rules.is_legal_buy_volume("920001", 101) is True
    assert rules.is_legal_buy_volume("000001", 150) is False
    assert rules.is_pure_sell_of_remainder("000001", 50, 50) is True
    assert rules.is_pure_sell_of_remainder("000001", 50, 150) is False
    assert rules.quantize_price(10.005) == 10.01
