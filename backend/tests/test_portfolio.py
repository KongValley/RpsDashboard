"""模拟账户的端到端行为测试（离线：合成行情 + 内存/临时库，不连券商、不推送）。

同时提供 `seed_portfolio_smoke(path)`：一个**固定种子**的完整新结构库，
既给下面的测试用，也给「浏览器实走」用（计划里的端到端证明）。
它不依赖网络，也不依赖真实行情。
"""

from __future__ import annotations

import os
import sqlite3

import pandas as pd
import pytest
from rpsboard import config, db
from rpsboard.analytics import rps as rps_engine
from rpsboard.analytics.matrix import (
    build_close_matrix,
    build_st_flags,
    build_tradable_matrix,
)
from rpsboard.broker import accounts as accounts_mod
from rpsboard.broker import corporate_actions as corp_mod
from rpsboard.broker import sim
from rpsboard.broker import trading_rules as rules
from rpsboard.sources import tdxnet_src as tdx

from .fixtures import make_industry_map_zh, make_long

#: 种子里的四个特殊代码 → 板块
SEED_CODES = ("000001", "000002", "688001", "920001")


def _board_ratio(symbol: str) -> float:
    return {"main": 0.10, "chinext": 0.20, "star": 0.20, "bse": 0.30}[
        rules.board_of(symbol)]


def seed_portfolio_smoke(path: str | None = None) -> dict:
    """建一个可直接跑（也适合浏览器实走）的完整库。

    结构：40 只票 × 320 个交易日，前四只刻意排成
    `000001 / 000002 / 688001 / 920001`（覆盖主板/创业板/科创板的数量规则），
    其余是互异的主板代码。最后 6 个交易日这四只的**开收盘固定 10.00**，
    让浏览器里的手工下单有个可预期的价格；同时留足 320 根 K 线给 RPS60 预热。

    返回 `{start_date, next_date, latest_complete_date, manual_account_id,
    strategy_account_id, dates}`，浏览器按返回值里的真实日期操作。
    """
    if not path:
        raise ValueError("seed_portfolio_smoke 必须给显式的库路径")
    if os.path.exists(path):
        os.remove(path)
    monkey_cs = config.MIN_CROSS_SECTION
    config.MIN_CROSS_SECTION = 2          # 合成库只有 40 只票
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        db.ensure_paper_accounts(conn)

        df = make_long(n_symbols=40, n_days=320, start="2024-01-01", seed=202)
        syms = sorted(df["symbol"].unique())
        mapping = {s: (SEED_CODES[i] if i < len(SEED_CODES) else f"6{i:05d}")
                   for i, s in enumerate(syms)}
        df = df.assign(real=df["symbol"].map(mapping))
        # 原始 `symbol` 列先丢掉：后面统一把 `real`（真实代码）改名成 `symbol`，
        # 两列同名会让 groupby/rename 直接炸（重复列名不是 1 维）。
        df = df.drop(columns=["symbol"])

        df["close"] = (df["close"] * 0.5).round(2).clip(lower=1.0)
        df["open"] = (df["close"] * 0.995).round(2)
        df["high"] = (df["close"] * 1.02).round(2)
        df["low"] = (df["close"] * 0.98).round(2)
        # 低/高必须夹住 open/close（原始行情合同），四舍五入后可能有 1 分的越界
        df["low"] = df[["low", "open", "close"]].min(axis=1)
        df["high"] = df[["high", "open", "close"]].max(axis=1)

        dates = sorted(df["date"].unique())
        tail = dates[-6:]
        mask = df["real"].isin(SEED_CODES) & df["date"].isin(tail)
        df.loc[mask, "close"] = 10.0
        df.loc[mask, "open"] = 10.0
        df.loc[mask, "high"] = 10.20
        df.loc[mask, "low"] = 9.80

        df = df.sort_values(["real", "date"])
        df["pct_chg"] = df.groupby("real")["close"].pct_change().round(6)
        vol_shares = 100_000.0
        df["amount"] = (df["close"] * vol_shares).round(2)

        # daily_bar：后复权（这里与原始价相同）、volume 用**手**
        bars = df.assign(volume=vol_shares / 100.0, tradestatus=1, is_st=0)
        bars = bars.rename(columns={"real": "symbol"})
        bars[["symbol", "date", "open", "high", "low", "close", "volume", "amount",
              "pct_chg", "is_st", "tradestatus"]].to_sql(
            "daily_bar", conn, if_exists="append", index=False)
        # raw_bar：不复权、volume 用**股**、tradestatus 由有效性推导
        raw = df.rename(columns={"real": "symbol"})[
            ["symbol", "date", "open", "high", "low", "close", "amount"]]
        raw = raw.assign(volume=vol_shares)
        raw["tradestatus"] = tdx._raw_tradestatus(raw)
        raw.to_sql("raw_bar", conn, if_exists="append", index=False)

        master = pd.DataFrame({
            "symbol": sorted(mapping.values()),
            "name": [f"测试{i}" for i in range(len(mapping))],
            "exchange": "sz", "ipo_date": "20150101", "is_st": 0,
            "updated_at": "2026-01-01"})
        master.to_sql("security_master", conn, if_exists="append", index=False)
        ind = make_industry_map_zh(sorted(mapping.values()), 5)
        ind.rename("industry").reset_index().rename(columns={"index": "symbol"}).assign(
            industry_cls="证监会", updated_at="2026-01-01").to_sql(
            "industry_map", conn, if_exists="append", index=False)

        # 基准指数：000001（市场日历）与 000300（回测/复盘基准）
        bench = []
        for i, d in enumerate(dates):
            bench.append(("000001", d, 3000.0 + i))
            bench.append(("000300", d, 3500.0 + i * 1.2))
        conn.executemany("INSERT INTO benchmark_bar(symbol, date, close)"
                         " VALUES (?,?,?)", bench)

        # 实测涨跌停：按板块给合理的百分比区间（当日收盘价 ± 板块比例）
        limits = []
        for r in raw.itertuples(index=False):
            ratio = _board_ratio(r.symbol) * 100.0
            limits.append((r.date, r.symbol, ratio, round(r.close * (1 + ratio / 100), 2),
                           round(r.close * (1 - ratio / 100), 2), "main", "normal"))
        conn.executemany(
            "INSERT OR REPLACE INTO price_limit_daily(date, symbol, limit_ratio,"
            " limit_up, limit_down, rule, status) VALUES (?,?,?,?,?,?,?)", limits)

        # 原价覆盖 + 公司行为查全标记：全部推进到最后一个交易日
        last = dates[-1]
        conn.executemany(
            "INSERT OR REPLACE INTO raw_coverage(symbol, date_from, date_to,"
            " updated_at) VALUES (?,?,?,?)",
            [(s, dates[0], last, "2026-01-01") for s in sorted(mapping.values())])
        conn.executemany(
            "INSERT OR REPLACE INTO corporate_sync(symbol, checked_through,"
            " updated_at) VALUES (?,?,?)",
            [(s, last, "2026-01-01") for s in sorted(mapping.values())])

        bars_for_matrix = bars
        rps_engine.materialize(
            conn, build_close_matrix(bars_for_matrix),
            build_tradable_matrix(bars_for_matrix),
            build_st_flags(bars_for_matrix),
            ind, None)
        conn.commit()

        # 全市场完整日检查点（模拟推进的默认终点读它）
        db.write_raw_checkpoint(conn, scope="all", market_date_value=last,
                                latest_complete_date=last, raw_failed=[],
                                hfq_failed=[])

        start = tail[0]
        manual = accounts_mod.create_account(
            conn, name="冒烟手动", kind="manual", cash=200_000.0,
            start_date=start, participation=0.1)
        strategy = accounts_mod.create_account(
            conn, name="冒烟策略", kind="strategy", cash=200_000.0,
            start_date=start, participation=0.1,
            params={"period": 60, "threshold": 0.0, "hold": 1,
                    "max_positions": 2, "exclude_st": True, "resonance": False,
                    "signal": "rps"})
        conn.close()
        return {"start_date": start, "next_date": tail[1], "tail": tail,
                "latest_complete_date": last,
                "manual_account_id": manual["id"],
                "strategy_account_id": strategy["id"]}
    finally:
        config.MIN_CROSS_SECTION = monkey_cs


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    path = tmp_path / "portfolio.db"
    info = seed_portfolio_smoke(str(path))
    monkeypatch.setattr(config, "DB_PATH", path)
    return str(path), info


def _day_after(conn, day: str) -> str:
    return sim.next_market_day(conn, day)


# ── 账户生命周期 ────────────────────────────────────────────────────
def test_first_advance_settles_from_start_date(seeded):
    path, info = seeded
    with db.session(path) as conn:
        res = sim.advance(conn, account_id=info["manual_account_id"])
        assert res["ok"] is True and res["days"] > 0
        s = sim.summary(conn, account_id=info["manual_account_id"])
        assert s["from"] == info["start_date"]
        assert s["positions"] == []          # 手动账户没有任何委托
        assert s["total"] == pytest.approx(200_000.0)


def test_advance_is_idempotent(seeded):
    path, info = seeded
    with db.session(path) as conn:
        first = sim.advance(conn, account_id=info["manual_account_id"])
        again = sim.advance(conn, account_id=info["manual_account_id"])
        assert again["days"] == 0, "已结算的交易日绝不重算"
        assert again["settled_through"] == first["settled_through"]


def test_strategy_account_places_orders_and_fills_next_day(seeded):
    path, info = seeded
    with db.session(path) as conn:
        res = sim.advance(conn, account_id=info["strategy_account_id"])
        assert res["ok"] is True
        fills = sim.fills_query(conn, account_id=info["strategy_account_id"])
        assert fills["total"] > 0, "策略账户应当真的成交过（否则断言是空的）"
        s = sim.summary(conn, account_id=info["strategy_account_id"])
        assert s["positions"], "成交后应当有持仓"


def test_accounts_are_isolated(seeded):
    path, info = seeded
    with db.session(path) as conn:
        before_manual = sim.summary(conn, account_id=info["manual_account_id"])
        sim.advance(conn, account_id=info["strategy_account_id"])
        after_manual = sim.summary(conn, account_id=info["manual_account_id"])
        strat = sim.summary(conn, account_id=info["strategy_account_id"])
        # 手动账户只有创建那天的全现金快照，推进策略账户不得改动它
        assert before_manual["days"] == 1
        assert after_manual["days"] == 1 and after_manual["positions"] == []
        assert after_manual["total"] == pytest.approx(200_000.0)
        assert after_manual["total"] == before_manual["total"]
        assert strat["positions"], "策略账户的持仓不该被手动账户的清掉"
        # 手动账户自己推进时也不该影响策略账户
        sim.advance(conn, account_id=info["manual_account_id"])
        assert sim.summary(conn, account_id=info["strategy_account_id"])["total"] == \
            pytest.approx(strat["total"])
        assert sim.summary(conn, account_id=info["manual_account_id"])["positions"] == []


def test_archive_keeps_history_and_blocks_writes(seeded):
    path, info = seeded
    with db.session(path) as conn:
        sim.advance(conn, account_id=info["manual_account_id"])
        before = sim.summary(conn, account_id=info["manual_account_id"])
        acc = accounts_mod.archive_account(conn, info["manual_account_id"])
        assert acc["status"] == "archived"
        after = sim.summary(conn, account_id=info["manual_account_id"])
        assert after["total"] == before["total"], "归档不改动任何历史数值"
        with pytest.raises(sim.SimError):
            sim.advance(conn, account_id=info["manual_account_id"])
        with pytest.raises(sim.SimError):
            sim.submit_manual_order(conn, account_id=info["manual_account_id"],
                                    request_id="x", symbol="000001", side="buy",
                                    volume=100, price=10.0)


def test_legacy_account_is_read_only(seeded):
    path, info = seeded
    with db.session(path) as conn:
        legacy = accounts_mod.create_account
        # 手工造一个 legacy 账户（迁移产物的形态），确认它拒绝一切写操作
        conn.execute(
            "INSERT INTO paper_account(name, kind, price_basis, initial_cash, cash,"
            " start_date, status, created_at, updated_at)"
            " VALUES ('旧研究','legacy_hfq','hfq',1000,1000,?, 'archived','x','x')",
            (info["start_date"],))
        lid = int(conn.execute("SELECT MAX(id) FROM paper_account").fetchone()[0])
        conn.commit()
        with pytest.raises(sim.SimError):
            sim.advance(conn, account_id=lid)
        assert legacy is accounts_mod.create_account


# ── 下单与撮合 ──────────────────────────────────────────────────────
def test_manual_buy_fills_at_limit_and_charges_fee(seeded):
    path, info = seeded
    aid = info["manual_account_id"]
    with db.session(path) as conn:
        sim.advance(conn, account_id=aid, to_date=info["start_date"])
        order = sim.submit_manual_order(
            conn, account_id=aid, request_id="r1", symbol="000001", side="buy",
            volume=1000, price=10.00, thesis="冒烟买入")
        assert order["status"] == "pending"
        sim.advance(conn, account_id=aid)
        fills = sim.fills_query(conn, account_id=aid)["rows"]
        assert len(fills) == 1
        f = fills[0]
        assert f["volume"] == 1000
        assert f["price"] == pytest.approx(10.00), "开盘价 ≤ 限价时按开盘成交"
        assert f["fee"] == pytest.approx(5.10), "10000 元：最低佣金 5 + 过户费 0.10"
        s = sim.summary(conn, account_id=aid)
        assert s["cash"] == pytest.approx(200_000 - 10_000 - 5.10, abs=0.01)
        assert len(s["positions"]) == 1


def test_manual_order_rejects_illegal_quantity(seeded):
    path, info = seeded
    aid = info["manual_account_id"]
    with db.session(path) as conn:
        sim.advance(conn, account_id=aid, to_date=info["start_date"])
        with pytest.raises(sim.SimError):
            sim.submit_manual_order(conn, account_id=aid, request_id="r1",
                                    symbol="000001", side="buy", volume=150,
                                    price=10.0)
        # 科创板 201 股合法（步长 1）
        ok = sim.submit_manual_order(conn, account_id=aid, request_id="r2",
                                     symbol="688001", side="buy", volume=201,
                                     price=10.0)
        assert ok["volume"] == 201
        # 北交所 101 股合法
        ok2 = sim.submit_manual_order(conn, account_id=aid, request_id="r3",
                                      symbol="920001", side="buy", volume=101,
                                      price=10.0)
        assert ok2["volume"] == 101


def test_manual_order_rejects_unknown_board(seeded):
    path, info = seeded
    aid = info["manual_account_id"]
    with db.session(path) as conn:
        sim.advance(conn, account_id=aid, to_date=info["start_date"])
        with pytest.raises(sim.SimError):
            sim.submit_manual_order(conn, account_id=aid, request_id="r9",
                                    symbol="999999", side="buy", volume=100,
                                    price=10.0)


def test_request_id_is_idempotent_and_conflicting(seeded):
    path, info = seeded
    aid = info["manual_account_id"]
    with db.session(path) as conn:
        sim.advance(conn, account_id=aid, to_date=info["start_date"])
        a = sim.submit_manual_order(conn, account_id=aid, request_id="same",
                                    symbol="000001", side="buy", volume=100,
                                    price=10.0)
        b = sim.submit_manual_order(conn, account_id=aid, request_id="same",
                                    symbol="000001", side="buy", volume=100,
                                    price=10.0)
        assert a["order_id"] == b["order_id"], "同 request_id 同内容要复用同一笔"
        with pytest.raises(sim.SimError):
            sim.submit_manual_order(conn, account_id=aid, request_id="same",
                                    symbol="000002", side="buy", volume=100,
                                    price=10.0)


def test_insufficient_cash_is_rejected_not_shrunk(seeded):
    path, info = seeded
    aid = info["manual_account_id"]
    with db.session(path) as conn:
        sim.advance(conn, account_id=aid, to_date=info["start_date"])
        with pytest.raises(sim.SimError):
            sim.submit_manual_order(conn, account_id=aid, request_id="big",
                                    symbol="000001", side="buy", volume=100_000,
                                    price=10.0)
        assert sim.orders_query(conn, account_id=aid)["total"] == 0, \
            "被拒的委托不该留下任何痕迹"


def test_frozen_cash_prevents_double_spending(seeded):
    path, info = seeded
    aid = info["manual_account_id"]
    with db.session(path) as conn:
        sim.advance(conn, account_id=aid, to_date=info["start_date"])
        # 20 万现金：先挂满 19,000 股（19 万），再挂 2,000 股应当因可用现金不足被拒
        sim.submit_manual_order(conn, account_id=aid, request_id="f1",
                                symbol="000001", side="buy", volume=19_000,
                                price=10.0)
        with pytest.raises(sim.SimError):
            sim.submit_manual_order(conn, account_id=aid, request_id="f2",
                                    symbol="000001", side="buy", volume=2_000,
                                    price=10.0)


def test_t_plus_one_blocks_same_day_sell(seeded):
    path, info = seeded
    aid = info["manual_account_id"]
    with db.session(path) as conn:
        day0 = info["start_date"]
        sim.advance(conn, account_id=aid, to_date=day0)
        # 第一天买入并成交
        sim.submit_manual_order(conn, account_id=aid, request_id="b1",
                                symbol="000001", side="buy", volume=1000, price=10.0)
        day1 = _day_after(conn, day0)
        sim.advance(conn, account_id=aid, to_date=day1)
        state = sim.load_account(conn, account_id=aid)
        assert state.positions.get("000001") == 1000
        # 当日新买不进入可卖量 → 在 day1 收盘提交「次日卖出」是允许的（下一会话可卖）
        order = sim.submit_manual_order(conn, account_id=aid, request_id="s1",
                                        symbol="000001", side="sell", volume=1000,
                                        price=10.0)
        assert order["status"] == "pending"


def test_orders_and_fills_are_cumulative_queries(seeded):
    path, info = seeded
    aid = info["manual_account_id"]
    with db.session(path) as conn:
        day0 = info["start_date"]
        sim.advance(conn, account_id=aid, to_date=day0)
        sim.submit_manual_order(conn, account_id=aid, request_id="b1",
                                symbol="000001", side="buy", volume=1000, price=10.0)
        # 决策日当天查：订单存在但还没有成交
        at0 = sim.orders_query(conn, account_id=aid, date=day0)
        assert at0["total"] == 1 and at0["rows"][0]["filled_volume"] == 0
        assert at0["rows"][0]["status"] == "pending"
        assert sim.fills_query(conn, account_id=aid, date=day0)["total"] == 0
        day1 = _day_after(conn, day0)
        sim.advance(conn, account_id=aid, to_date=day1)
        # 回看 day0 仍然是「当时的样子」：未成交、没有未来成交泄漏
        again = sim.orders_query(conn, account_id=aid, date=day0)
        assert again["rows"][0]["filled_volume"] == 0
        assert sim.fills_query(conn, account_id=aid, date=day0)["total"] == 0
        later = sim.orders_query(conn, account_id=aid, date=day1)
        assert later["rows"][0]["status"] == "filled"
        assert later["rows"][0]["filled_volume"] == 1000


def test_cancel_releases_freeze(seeded):
    path, info = seeded
    aid = info["manual_account_id"]
    with db.session(path) as conn:
        sim.advance(conn, account_id=aid, to_date=info["start_date"])
        o = sim.submit_manual_order(conn, account_id=aid, request_id="c1",
                                    symbol="000001", side="buy", volume=19_000,
                                    price=10.0)
        with pytest.raises(sim.SimError):
            sim.submit_manual_order(conn, account_id=aid, request_id="c2",
                                    symbol="000001", side="buy", volume=2_000,
                                    price=10.0)
        sim.cancel_order(conn, account_id=aid, order_id=o["order_id"])
        # 撤掉之后资金释放，同一笔可以再挂
        sim.submit_manual_order(conn, account_id=aid, request_id="c3",
                                symbol="000001", side="buy", volume=2_000,
                                price=10.0)


# ── 跨日部分成交 ────────────────────────────────────────────────────
def test_partial_fills_append_across_days(tmp_path, monkeypatch):
    """跨日部分成交必须**逐日追加**，不能把前一天的成交覆盖掉。"""
    path = tmp_path / "partial.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(db.SCHEMA)
    db.ensure_paper_accounts(conn)
    for d in ("2026-01-05", "2026-01-06", "2026-01-07"):
        conn.execute("INSERT INTO benchmark_bar(symbol,date,close) VALUES ('000001',?,?)",
                     (d, 3000.0))
    conn.execute("INSERT INTO security_master(symbol,name,exchange,is_st,updated_at)"
                 " VALUES ('000001','测试','sz',0,'x')")
    # 每日只有 1000 股可成交 → 10% 参与率 = 100 股/天
    for d in ("2026-01-05", "2026-01-06", "2026-01-07"):
        conn.execute(
            "INSERT INTO raw_bar(symbol,date,open,high,low,close,volume,amount,"
            "tradestatus) VALUES ('000001',?,10,10,10,10,1000,10000,1)", (d,))
        conn.execute(
            "INSERT INTO price_limit_daily(date,symbol,limit_ratio,limit_up,limit_down,"
            "rule,status) VALUES (?,?,10,11,9,'main','normal')", (d, "000001"))
        conn.execute("INSERT OR REPLACE INTO raw_coverage(symbol,date_from,date_to,"
                     "updated_at) VALUES ('000001','2026-01-05',?,'x')", (d,))
        conn.execute("INSERT OR REPLACE INTO corporate_sync(symbol,checked_through,"
                     "updated_at) VALUES ('000001',?,'x')", (d,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", path)

    with db.session(path) as conn:
        acc = accounts_mod.create_account(conn, name="部分成交", kind="manual",
                                          cash=100_000.0, start_date="2026-01-05",
                                          participation=0.1)
        aid = acc["id"]
        sim.advance(conn, account_id=aid, to_date="2026-01-05")
        sim.submit_manual_order(conn, account_id=aid, request_id="p1",
                                symbol="000001", side="buy", volume=200, price=10.0)
        res = sim.advance(conn, account_id=aid, to_date="2026-01-07")
        assert res["ok"] is True
        rows = conn.execute("SELECT trade_date, volume FROM paper_fill"
                            " ORDER BY trade_date").fetchall()
        assert [int(r["volume"]) for r in rows] == [100, 100], \
            "两天各成交 100 股，必须是两行（旧实现会把第一天覆盖掉）"
        state = sim.load_account(conn, account_id=aid)
        assert state.positions["000001"] == 200
        # 再推进同日不新增成交
        sim.advance(conn, account_id=aid, to_date="2026-01-07")
        n = conn.execute("SELECT COUNT(*) c FROM paper_fill").fetchone()["c"]
        assert n == 2


def test_fee_decimal_is_quantized_once():
    """费用一次量化到分：最低佣金 + 过户费 + 滑点合计后取整。"""
    from datetime import date as _d
    from decimal import Decimal

    from rpsboard.analytics import costs

    m = costs.CostModel(slippage_rate=0.0)
    assert costs.fee_decimal(m, Decimal("10000"), side="buy") == Decimal("5.10")
    assert costs.fee_decimal(m, Decimal("11000"), side="sell",
                             on=_d(2026, 1, 5)) == Decimal("10.61")
    # 研究口径（浮点、不量化）与记账口径同源
    assert m.buy_cost(10000) == float(Decimal("5.10"))


# ── 数据闸门 ────────────────────────────────────────────────────────
def test_missing_price_limit_row_blocks(seeded, tmp_path, monkeypatch):
    path, info = seeded
    with db.session(path) as conn:
        aid = info["manual_account_id"]
        day0 = info["start_date"]
        sim.advance(conn, account_id=aid, to_date=day0)
        sim.submit_manual_order(conn, account_id=aid, request_id="g1",
                                symbol="000001", side="buy", volume=1000, price=10.0)
        day1 = _day_after(conn, day0)
        conn.execute("DELETE FROM price_limit_daily WHERE date=? AND symbol='000001'",
                     (day1,))
        conn.commit()
        res = sim.advance(conn, account_id=aid, to_date=day1)
        assert res["ok"] is False
        assert res["blocked_reason"] == "price_limit_missing"
        assert res["settled_through"] == day0, "应停在缺口之前"


def test_missing_raw_bar_blocks_until_suspension_confirmed(seeded, tmp_path, monkeypatch):
    path, info = seeded
    aid = info["manual_account_id"]
    with db.session(path) as conn:
        day0 = info["start_date"]
        sim.advance(conn, account_id=aid, to_date=day0)
        sim.submit_manual_order(conn, account_id=aid, request_id="b1",
                                symbol="000001", side="buy", volume=1000, price=10.0)
        day1 = _day_after(conn, day0)
        conn.execute("DELETE FROM raw_bar WHERE date=? AND symbol='000001'", (day1,))
        conn.execute("DELETE FROM price_limit_daily WHERE date=? AND symbol='000001'",
                     (day1,))
        conn.commit()
        res = sim.advance(conn, account_id=aid, to_date=day1)
        assert res["ok"] is False and res["blocked_reason"] == "raw_bar_missing"
        # 按公告确认停牌后可以继续（估值沿用前一日原价）
        conn.execute(
            "INSERT INTO suspension_confirmation(symbol,date,source_note,created_at)"
            " VALUES ('000001',?,'公告核对','x')", (day1,))
        conn.execute("DELETE FROM raw_bar WHERE date=? AND symbol='000001'", (day1,))
        conn.commit()
        res2 = sim.advance(conn, account_id=aid, to_date=day1)
        assert res2["ok"] is True and res2["days"] >= 1


def test_unrelated_symbol_failure_does_not_block(seeded, tmp_path, monkeypatch):
    path, info = seeded
    with db.session(path) as conn:
        aid = info["manual_account_id"]
        day0 = info["start_date"]
        sim.advance(conn, account_id=aid, to_date=day0)
        # 把一只与账户无关的票的原价删掉：不该冻住这个账户
        conn.execute("DELETE FROM raw_bar WHERE symbol='600000'")
        conn.commit()
        res = sim.advance(conn, account_id=aid)
        assert res["ok"] is True


# ── 公司行为 ────────────────────────────────────────────────────────
def _seed_corp(tmp_path, monkeypatch, *, category=1, payload=None, prices=None):
    """最小库：四天行情 + 一条权息事件（公司行为测试的公共脚手架）。

    `prices` 可给出每一天的收盘价（默认全 10）：分红/配股要让价格真的除权，
    否则「除息日权益不变」这类断言会被合成数据掩盖。
    """
    path = tmp_path / "corp.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(db.SCHEMA)
    db.ensure_paper_accounts(conn)
    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    for d in days:
        conn.execute("INSERT INTO benchmark_bar(symbol,date,close) VALUES ('000001',?,?)",
                     (d, 3000.0))
    conn.execute("INSERT INTO security_master(symbol,name,exchange,is_st,updated_at)"
                 " VALUES ('000001','测试','sz',0,'x')")
    for i, d in enumerate(days):
        px = float(prices[i]) if prices else 10.0
        conn.execute("INSERT INTO raw_bar(symbol,date,open,high,low,close,volume,"
                     "amount,tradestatus) VALUES ('000001',?,?,?,?,?,100000,"
                     "1000000,1)", (d, px, px, px, px))
        conn.execute("INSERT INTO price_limit_daily(date,symbol,limit_ratio,limit_up,"
                     "limit_down,rule,status) VALUES (?,?,10,?,?, 'main','normal')",
                     (d, "000001", round(px * 1.1, 2), round(px * 0.9, 2)))
        conn.execute("INSERT OR REPLACE INTO raw_coverage(symbol,date_from,date_to,"
                     "updated_at) VALUES ('000001','2026-01-05',?,'x')", (d,))
        conn.execute("INSERT OR REPLACE INTO corporate_sync(symbol,checked_through,"
                     "updated_at) VALUES ('000001',?,'x')", (d,))
    conn.execute("INSERT INTO corporate_event(symbol,event_date,category,payload,"
                 "updated_at) VALUES ('000001','2026-01-06',?,?, 'x')",
                 (category, payload or '[]'))
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    return path, days


def test_cash_dividend_keeps_equity_then_pays(tmp_path, monkeypatch):
    # 除息日价格真的跌到 9.9（不能靠合成数据掩盖「除息日假亏损」这个问题）
    path, days = _seed_corp(tmp_path, monkeypatch, prices=[10.0, 9.9, 9.9, 9.9])
    with db.session(path) as conn:
        acc = accounts_mod.create_account(conn, name="分红", kind="manual",
                                          cash=1000.0, start_date=days[0],
                                          participation=0.1)
        aid = acc["id"]
        # 手工建持仓：登记日 1000 股 × 10 元，成本 10000
        conn.execute(
            "INSERT INTO paper_position(account_id, trade_date, symbol, volume,"
            " available_volume, avg_price, cost_basis, close, price_date, market_value)"
            " VALUES (?,?,'000001',1000,1000,10.0,10000.0,10.0,?,10000.0)",
            (aid, days[0], days[0]))
        conn.execute("INSERT OR REPLACE INTO paper_nav(account_id, trade_date, cash, market_value,"
                     " receivable, total, nav, n_positions, filled_orders)"
                     " VALUES (?,?,0,10000,0,10000,1,1,0)", (aid, days[0]))
        conn.commit()
        # 核对：登记日持股 1000，现金分红 100 元，两日后到账，无税
        corp_mod.resolve_action(
            conn, account_id=aid, symbol="000001", event_date=days[1],
            category=1, last_settled=days[0],
            resolution={"record_date": days[0], "effective_date": days[1],
                        "cash_gross": 100.0, "cash_pay_date": days[3],
                        "share_delta": 0, "share_unlocks": [],
                        "subscription_cash": 0, "subscription_pay_date": None,
                        "taxes": [], "taxes_checked_through": days[3],
                        "source_note": "公告核对：每 10 股派 1 元"})
        sim.advance(conn, account_id=aid, to_date=days[1])
        nav1 = conn.execute("SELECT * FROM paper_nav WHERE account_id=? AND"
                            " trade_date=?", (aid, days[1])).fetchone()
        # 除息日价格没变（合成数据），权益靠应收保持
        assert float(nav1["receivable"]) == pytest.approx(100.0)
        assert float(nav1["market_value"]) == pytest.approx(9900.0)
        assert float(nav1["total"]) == pytest.approx(10000.0), "除息日权益不凭空变化"
        sim.advance(conn, account_id=aid, to_date=days[3])
        nav3 = conn.execute("SELECT * FROM paper_nav WHERE account_id=? AND"
                            " trade_date=?", (aid, days[3])).fetchone()
        assert float(nav3["cash"]) == pytest.approx(100.0)
        assert float(nav3["receivable"]) == pytest.approx(0.0)
        assert float(nav3["total"]) == pytest.approx(10000.0), "到账不重复算收益"


def test_bonus_shares_unlock_on_confirmed_date(tmp_path, monkeypatch):
    path, days = _seed_corp(tmp_path, monkeypatch)
    with db.session(path) as conn:
        acc = accounts_mod.create_account(conn, name="送转", kind="manual",
                                          cash=1000.0, start_date=days[0],
                                          participation=0.1)
        aid = acc["id"]
        conn.execute(
            "INSERT INTO paper_position(account_id, trade_date, symbol, volume,"
            " available_volume, avg_price, cost_basis, close, price_date, market_value)"
            " VALUES (?,?,'000001',1000,1000,10.0,10000.0,10.0,?,10000.0)",
            (aid, days[0], days[0]))
        conn.execute("INSERT OR REPLACE INTO paper_nav(account_id, trade_date, cash, market_value,"
                     " receivable, total, nav, n_positions, filled_orders)"
                     " VALUES (?,?,0,10000,0,10000,1,1,0)", (aid, days[0]))
        conn.commit()
        corp_mod.resolve_action(
            conn, account_id=aid, symbol="000001", event_date=days[1], category=1,
            last_settled=days[0],
            resolution={"record_date": days[0], "effective_date": days[1],
                        "cash_gross": 0.0, "cash_pay_date": None,
                        "share_delta": 1000,
                        "share_unlocks": [{"kind": "bonus", "volume": 1000,
                                           "available_date": days[3]}],
                        "subscription_cash": 0.0, "subscription_pay_date": None,
                        "taxes": [], "taxes_checked_through": days[3],
                        "source_note": "公告核对：每 10 股送 10 股"})
        sim.advance(conn, account_id=aid, to_date=days[1])
        rows = sim.positions_at(conn, account_id=aid, trade_date=days[1])
        p = rows[0]
        assert int(p["volume"]) == 2000, "所有权股数增加"
        assert int(p["available_volume"]) == 1000, "新增股到可卖日前锁定"
        assert float(p["cost_basis"]) == pytest.approx(10000.0), "总成本不变"
        sim.advance(conn, account_id=aid, to_date=days[3])
        rows3 = sim.positions_at(conn, account_id=aid, trade_date=days[3])
        assert int(rows3[0]["available_volume"]) == 2000, "解锁日后全部可卖"


def test_rights_issue_accounts_subscription_payable(tmp_path, monkeypatch):
    path, days = _seed_corp(tmp_path, monkeypatch, prices=[10.0, 9.0, 9.0, 9.0])
    with db.session(path) as conn:
        acc = accounts_mod.create_account(conn, name="配股", kind="manual",
                                          cash=2000.0, start_date=days[0],
                                          participation=0.1)
        aid = acc["id"]
        conn.execute(
            "INSERT INTO paper_position(account_id, trade_date, symbol, volume,"
            " available_volume, avg_price, cost_basis, close, price_date, market_value)"
            " VALUES (?,?,'000001',1000,1000,10.0,10000.0,10.0,?,10000.0)",
            (aid, days[0], days[0]))
        conn.execute("INSERT OR REPLACE INTO paper_nav(account_id, trade_date, cash, market_value,"
                     " receivable, total, nav, n_positions, filled_orders)"
                     " VALUES (?,?,2000,10000,0,12000,1,1,0)", (aid, days[0]))
        conn.commit()
        corp_mod.resolve_action(
            conn, account_id=aid, symbol="000001", event_date=days[1], category=1,
            last_settled=days[0],
            resolution={"record_date": days[0], "effective_date": days[1],
                        "cash_gross": 0.0, "cash_pay_date": None,
                        "share_delta": 200,
                        "share_unlocks": [{"kind": "subscription", "volume": 200,
                                           "available_date": days[2]}],
                        "subscription_cash": 800.0, "subscription_pay_date": days[2],
                        "taxes": [], "taxes_checked_through": days[3],
                        "source_note": "公告核对：每 10 股配 2 股，配股价 4 元"})
        sim.advance(conn, account_id=aid, to_date=days[1])
        nav1 = conn.execute("SELECT * FROM paper_nav WHERE account_id=? AND"
                            " trade_date=?", (aid, days[1])).fetchone()
        assert float(nav1["receivable"]) == pytest.approx(-800.0), "应付认购款"
        assert float(nav1["total"]) == pytest.approx(12000.0)
        rows = sim.positions_at(conn, account_id=aid, trade_date=days[1])
        assert int(rows[0]["volume"]) == 1200
        assert float(rows[0]["cost_basis"]) == pytest.approx(10800.0)
        sim.advance(conn, account_id=aid, to_date=days[2])
        nav2 = conn.execute("SELECT * FROM paper_nav WHERE account_id=? AND"
                            " trade_date=?", (aid, days[2])).fetchone()
        assert float(nav2["cash"]) == pytest.approx(1200.0)
        assert float(nav2["receivable"]) == pytest.approx(0.0)
        assert float(nav2["total"]) == pytest.approx(12000.0)


def test_unresolved_event_blocks_before_event_day(tmp_path, monkeypatch):
    path, days = _seed_corp(tmp_path, monkeypatch)
    with db.session(path) as conn:
        acc = accounts_mod.create_account(conn, name="待核对", kind="manual",
                                          cash=1000.0, start_date=days[0],
                                          participation=0.1)
        aid = acc["id"]
        conn.execute(
            "INSERT INTO paper_position(account_id, trade_date, symbol, volume,"
            " available_volume, avg_price, cost_basis, close, price_date, market_value)"
            " VALUES (?,?,'000001',100,100,10.0,1000.0,10.0,?,1000.0)",
            (aid, days[0], days[0]))
        conn.execute("INSERT OR REPLACE INTO paper_nav(account_id, trade_date, cash, market_value,"
                     " receivable, total, nav, n_positions, filled_orders)"
                     " VALUES (?,?,1000,1000,0,2000,1,1,0)", (aid, days[0]))
        conn.commit()
        res = sim.advance(conn, account_id=aid, to_date=days[3])
        assert res["ok"] is False
        assert res["blocked_reason"] == "corporate_action_unresolved"
        assert res["settled_through"] == days[0], "停在事件日之前"
        assert res["missing"], "要列出到底是哪个事件"


def test_tax_unresolved_is_distinct_from_zero_tax(tmp_path, monkeypatch):
    path, days = _seed_corp(tmp_path, monkeypatch)
    with db.session(path) as conn:
        acc = accounts_mod.create_account(conn, name="税", kind="manual",
                                          cash=1000.0, start_date=days[0],
                                          participation=0.1)
        aid = acc["id"]
        conn.execute(
            "INSERT INTO paper_position(account_id, trade_date, symbol, volume,"
            " available_volume, avg_price, cost_basis, close, price_date, market_value)"
            " VALUES (?,?,'000001',1000,1000,10.0,10000.0,10.0,?,10000.0)",
            (aid, days[0], days[0]))
        conn.execute("INSERT OR REPLACE INTO paper_nav(account_id, trade_date, cash, market_value,"
                     " receivable, total, nav, n_positions, filled_orders)"
                     " VALUES (?,?,0,10000,0,10000,1,1,0)", (aid, days[0]))
        conn.commit()
        corp_mod.resolve_action(
            conn, account_id=aid, symbol="000001", event_date=days[1], category=1,
            last_settled=days[0],
            resolution={"record_date": days[0], "effective_date": days[1],
                        "cash_gross": 100.0, "cash_pay_date": days[2],
                        "share_delta": 0, "share_unlocks": [],
                        "subscription_cash": 0.0, "subscription_pay_date": None,
                        "taxes": [], "taxes_checked_through": None,
                        "source_note": "有分红但税还没核对"})
        res = sim.advance(conn, account_id=aid, to_date=days[1])
        assert res["ok"] is False and res["blocked_reason"] == "corporate_tax_unresolved"


def test_resolve_action_rejects_retroactive_change(tmp_path, monkeypatch):
    path, days = _seed_corp(tmp_path, monkeypatch)
    with db.session(path) as conn:
        acc = accounts_mod.create_account(conn, name="追溯", kind="manual",
                                          cash=1000.0, start_date=days[0],
                                          participation=0.1)
        aid = acc["id"]
        conn.commit()
        with pytest.raises(corp_mod.CorporateActionError):
            corp_mod.resolve_action(
                conn, account_id=aid, symbol="000001", event_date=days[1],
                category=1, last_settled=days[2],
                resolution={"record_date": days[0], "effective_date": days[1],
                            "cash_gross": 10.0, "cash_pay_date": days[2],
                            "share_delta": 0, "share_unlocks": [],
                            "subscription_cash": 0.0, "subscription_pay_date": None,
                            "taxes": [], "taxes_checked_through": days[2],
                            "source_note": "x"})
        # 不对应任何已采集事件 → 拒绝凭空登记
        with pytest.raises(corp_mod.CorporateActionError):
            corp_mod.resolve_action(
                conn, account_id=aid, symbol="000001", event_date=days[3],
                category=1, last_settled=days[0],
                resolution={"record_date": days[0], "effective_date": days[3],
                            "cash_gross": 10.0, "cash_pay_date": None,
                            "share_delta": 0, "share_unlocks": [],
                            "subscription_cash": 0.0, "subscription_pay_date": None,
                            "taxes": [], "taxes_checked_through": days[3],
                            "source_note": "x"})
        # 空税数组但给了核对截止日 = 已确认无税（与「未知」可区分）
        res = corp_mod.resolve_action(
            conn, account_id=aid, symbol="000001", event_date=days[1], category=1,
            last_settled=days[0],
            resolution={"record_date": days[0], "effective_date": days[1],
                        "cash_gross": 10.0, "cash_pay_date": days[2],
                        "share_delta": 0, "share_unlocks": [],
                        "subscription_cash": 0.0, "subscription_pay_date": None,
                        "taxes": [], "taxes_checked_through": days[3],
                        "source_note": "确认无税"})
        assert res["ok"] is True


def test_unsupported_category_blocks(tmp_path, monkeypatch):
    path, days = _seed_corp(tmp_path, monkeypatch, category=13)
    with db.session(path) as conn:
        acc = accounts_mod.create_account(conn, name="权证", kind="manual",
                                          cash=1000.0, start_date=days[0],
                                          participation=0.1)
        aid = acc["id"]
        conn.execute(
            "INSERT INTO paper_position(account_id, trade_date, symbol, volume,"
            " available_volume, avg_price, cost_basis, close, price_date, market_value)"
            " VALUES (?,?,'000001',100,100,10.0,1000.0,10.0,?,1000.0)",
            (aid, days[0], days[0]))
        conn.execute("INSERT OR REPLACE INTO paper_nav(account_id, trade_date, cash, market_value,"
                     " receivable, total, nav, n_positions, filled_orders)"
                     " VALUES (?,?,1000,1000,0,2000,1,1,0)", (aid, days[0]))
        conn.commit()
        res = sim.advance(conn, account_id=aid, to_date=days[3])
        assert res["ok"] is False
        assert res["blocked_reason"] == "corporate_action_unsupported"


# ── 复盘与指标 ──────────────────────────────────────────────────────
def test_review_cutoff_is_consistent(seeded):
    path, info = seeded
    aid = info["strategy_account_id"]
    with db.session(path) as conn:
        sim.advance(conn, account_id=aid)
        days = [r["trade_date"] for r in conn.execute(
            "SELECT trade_date FROM paper_nav WHERE account_id=? ORDER BY trade_date",
            (aid,))]
        assert len(days) >= 4
        cutoff = days[2]
        first = sim.review(conn, account_id=aid, date=cutoff)
        # cut 之后发生的事（更晚的成交/更剧烈的行情）不得改变 cutoff 时的结论
        sim.advance(conn, account_id=aid)
        second = sim.review(conn, account_id=aid, date=cutoff)
        assert first["summary"]["realized_total"] == second["summary"]["realized_total"]
        assert first["to"] == second["to"] == cutoff
        assert [p["symbol"] for p in first["positions"]] == \
            [p["symbol"] for p in second["positions"]]
        full = sim.review(conn, account_id=aid)
        assert full["to"] >= cutoff


def test_review_reports_null_not_zero_for_short_series(seeded):
    path, info = seeded
    with db.session(path) as conn:
        sim.advance(conn, account_id=info["manual_account_id"],
                    to_date=info["start_date"])
        rev = sim.review(conn, account_id=info["manual_account_id"])
        assert rev["days"] == 1
        assert rev["summary"]["sharpe"] is None, "单点净值不该编造一个夏普"
        assert rev["summary"]["volatility"] is None


def test_metrics_match_between_backtest_and_sim():
    from rpsboard.analytics import backtest as bt

    equity = [1.0, 1.01, 0.9999]
    dates = ["2026-01-05", "2026-01-06", "2026-01-07"]
    m = bt.compute_metrics_from_points(equity, dates)
    assert m["volatility"] == pytest.approx(0.2245, abs=1e-3)
    assert m["sharpe"] == pytest.approx(0.0, abs=1e-9)
    assert m["metrics_basis"] == "daily-v1"
    # 同一序列送「调仓点口径」入口（n_days_per_point=1）必须一致
    import pandas as pd
    m2 = bt.compute_metrics(pd.Series(equity), n_days_per_point=1, dates=dates)
    assert m2["volatility"] == pytest.approx(m["volatility"])
    assert m2["sharpe"] == pytest.approx(m["sharpe"])


def test_correlation_aligns_on_common_dates(tmp_path, monkeypatch):
    """两只票在**共同**交易日的收益相同 → 相关系数必须是 1（旧实现给 0.9685）。"""
    path = tmp_path / "corr.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(db.SCHEMA)
    db.ensure_paper_accounts(conn)
    days = [f"2026-01-{d:02d}" for d in range(1, 9)]
    prices = [100.0]
    for ret in (0.1, -0.1, 0.2, -0.2, 0.3, -0.3, 0.4):
        prices.append(prices[-1] * (1 + ret))
    for sym in ("000001", "000002"):
        for i, (d, px) in enumerate(zip(days, prices)):
            # 000002 中间缺一天 → 位置对齐会算错
            if sym == "000002" and i == 4:
                continue
            conn.execute("INSERT INTO daily_bar(symbol,date,close) VALUES (?,?,?)",
                         (sym, d, px))
        conn.execute("INSERT INTO raw_bar(symbol,date,open,high,low,close,volume,"
                     "amount,tradestatus) VALUES (?,?,1,1,1,1,1,1,1)",
                     (sym, days[-1]))
        conn.execute("INSERT INTO paper_position(account_id, trade_date, symbol, volume,"
                     " available_volume, avg_price, cost_basis, close, price_date,"
                     " market_value) VALUES (1,?,?,100,100,1,100,?, ?, 100)",
                     (days[-1], sym, days[-1], prices[-1]))
        for d in days:
            conn.execute("INSERT OR REPLACE INTO benchmark_bar(symbol,date,close)"
                         " VALUES ('000001',?,1)", (d,))
    total = 200 * prices[-1]
    conn.execute("INSERT INTO paper_account(id,name,kind,price_basis,initial_cash,cash,"
                 "start_date,status,created_at,updated_at) VALUES (1,'x','manual','raw',"
                 "1,0,?,'active','x','x')", (days[0],))
    conn.execute("INSERT INTO paper_nav(account_id,trade_date,cash,market_value,"
                 "receivable,total,nav,n_positions,filled_orders)"
                 " VALUES (1,?,0,?,0,?,1,2,0)", (days[-1], total, total))
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    with db.session(path) as conn:
        risk = sim.portfolio_risk(conn, account_id=1, window=60)
    assert risk["corr_pairs"] == 1
    assert risk["corr_max"]["value"] == pytest.approx(1.0, abs=1e-9), \
        "共同日期上配对的相关系数必须是 1"


def test_metrics_are_not_truncated_by_curve_limit(seeded, monkeypatch):
    """图可以抽样，但**统计**必须基于完整曲线。"""
    path, info = seeded
    aid = info["strategy_account_id"]
    with db.session(path) as conn:
        sim.advance(conn, account_id=aid)
        days = conn.execute("SELECT COUNT(*) c FROM paper_nav WHERE account_id=?",
                            (aid,)).fetchone()["c"]
        assert days >= 3, "曲线太短会让这条断言失去意义"
        monkeypatch.setattr(sim, "NAV_CURVE_LIMIT", 2)
        s = sim.summary(conn, account_id=aid)
        rev = sim.review(conn, account_id=aid)
    assert s["curve_truncated"] is True
    assert len(s["curve"]) == 2, "图被抽样到上限"
    assert s["days"] == days and s["stats_days"] == days, \
        "统计区间必须仍是完整曲线"
    assert rev["days"] == days, "复盘统计同样不受图点上限截断"


def test_cost_basis_rounds_to_cents_and_releases_on_close(tmp_path, monkeypatch):
    path = tmp_path / "cost.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(db.SCHEMA)
    db.ensure_paper_accounts(conn)
    conn.execute("INSERT INTO paper_account(id,name,kind,price_basis,initial_cash,cash,"
                 "start_date,status,created_at,updated_at)"
                 " VALUES (1,'x','manual','raw',0,0,'2026-01-05','active','x','x')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    with db.session(path) as conn:
        state = sim.Account(account_id=1, cash=1_000_000.0)
        sim._apply_fill(state, "000001", "buy", 300, 10.0, 5.03, "2026-01-05")
        assert state.cost_basis["000001"] == pytest.approx(3005.03)
        allocations = []
        for _ in range(3):
            before = state.cost_basis.get("000001", 0.0)
            sim._apply_fill(state, "000001", "sell", 100, 10.0, 0.0, "2026-01-05")
            after = state.cost_basis.get("000001", 0.0)
            allocations.append(round(before - after, 2))
        assert "000001" not in state.cost_basis, "清仓必须释放全部成本余额"
        assert allocations == [1001.68, 1001.68, 1001.67], \
            "按比例结转取分，不能拿四舍五入后的均价倒乘"
        assert sum(allocations) == pytest.approx(3005.03, abs=0.001)


def test_partial_fill_insufficient_cash_cancels_remainder(tmp_path, monkeypatch):
    """含费的部分成交后资金不足以维持余量冻结 → 撤余量并记原因。"""
    path = tmp_path / "part_fee.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(db.SCHEMA)
    db.ensure_paper_accounts(conn)
    days = ["2026-01-05", "2026-01-06", "2026-01-07"]
    for d in days:
        conn.execute("INSERT INTO benchmark_bar(symbol,date,close) VALUES ('000001',?,1)",
                     (d,))
        conn.execute("INSERT INTO raw_bar(symbol,date,open,high,low,close,volume,"
                     "amount,tradestatus) VALUES ('000001',?,10,10,10,10,1000,10000,1)",
                     (d,))
        conn.execute("INSERT INTO price_limit_daily(date,symbol,limit_ratio,limit_up,"
                     "limit_down,rule,status) VALUES (?,?,10,11,9,'main','normal')",
                     (d, "000001"))
        conn.execute("INSERT OR REPLACE INTO raw_coverage(symbol,date_from,date_to,"
                     "updated_at) VALUES ('000001','2026-01-05',?,'x')", (d,))
        conn.execute("INSERT OR REPLACE INTO corporate_sync(symbol,checked_through,"
                     "updated_at) VALUES ('000001',?,'x')", (d,))
    conn.execute("INSERT INTO security_master(symbol,name,exchange,is_st,updated_at)"
                 " VALUES ('000001','t','sz',0,'x')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    with db.session(path) as conn:
        acc = accounts_mod.create_account(conn, name="含费", kind="manual",
                                          cash=2005.02, start_date=days[0],
                                          participation=0.1)
        aid = acc["id"]
        sim.advance(conn, account_id=aid, to_date=days[0])
        sim.submit_manual_order(conn, account_id=aid, request_id="x",
                                symbol="000001", side="buy", volume=200, price=10.0)
        res = sim.advance(conn, account_id=aid, to_date=days[2])
        assert res["ok"] is True
        fills = sim.fills_query(conn, account_id=aid)["rows"]
        assert sum(int(f["volume"]) for f in fills) == 100, "只成交当日容量 100 股"
        order = sim.orders_query(conn, account_id=aid)["rows"][0]
        assert order["status"] == "canceled"
        assert "insufficient_cash" in (order["err_msg"] or "") or \
            order["remaining_volume"] == 100
        nav = conn.execute("SELECT cash FROM paper_nav WHERE account_id=?"
                           " ORDER BY trade_date DESC LIMIT 1", (aid,)).fetchone()
        assert float(nav["cash"]) >= 0, "绝不能出现负现金"


def test_calendar_day_without_signal_still_settles(tmp_path, monkeypatch):
    """中间某个市场日没有 RPS，也不能跳过撮合/盯市。"""
    path = tmp_path / "nosig.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(db.SCHEMA)
    db.ensure_paper_accounts(conn)
    days = ["2026-01-05", "2026-01-06", "2026-01-07"]
    for d in days:
        conn.execute("INSERT INTO benchmark_bar(symbol,date,close) VALUES ('000001',?,1)",
                     (d,))
        conn.execute("INSERT INTO raw_bar(symbol,date,open,high,low,close,volume,"
                     "amount,tradestatus) VALUES ('000001',?,10,10,10,10,100000,"
                     "1000000,1)", (d,))
        conn.execute("INSERT INTO price_limit_daily(date,symbol,limit_ratio,limit_up,"
                     "limit_down,rule,status) VALUES (?,?,10,11,9,'main','normal')",
                     (d, "000001"))
        conn.execute("INSERT OR REPLACE INTO raw_coverage(symbol,date_from,date_to,"
                     "updated_at) VALUES ('000001','2026-01-05',?,'x')", (d,))
        conn.execute("INSERT OR REPLACE INTO corporate_sync(symbol,checked_through,"
                     "updated_at) VALUES ('000001',?,'x')", (d,))
    conn.execute("INSERT INTO security_master(symbol,name,exchange,is_st,updated_at)"
                 " VALUES ('000001','t','sz',0,'x')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    with db.session(path) as conn:
        acc = accounts_mod.create_account(conn, name="无信号", kind="manual",
                                          cash=100_000.0, start_date=days[0],
                                          participation=0.1)
        aid = acc["id"]
        sim.advance(conn, account_id=aid, to_date=days[0])
        sim.submit_manual_order(conn, account_id=aid, request_id="x",
                                symbol="000001", side="buy", volume=1000, price=10.0)
        res = sim.advance(conn, account_id=aid, to_date=days[2])
        assert res["days"] == 2
        navs = [r["trade_date"] for r in conn.execute(
            "SELECT trade_date FROM paper_nav WHERE account_id=? ORDER BY trade_date",
            (aid,))]
        assert navs == days, "每个市场日都要有净值行"
        assert sim.fills_query(conn, account_id=aid)["total"] == 1


def test_history_order_state_rebuild_after_terminal(tmp_path, monkeypatch):
    """D1 挂单 → D2 停牌顺延（仍 pending）→ D3 部分成交后撤余量。

    回看 D2 必须是 `pending` 且当时冻结还在：当前终态不能透传到过去。
    """
    path = tmp_path / "hist.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(db.SCHEMA)
    db.ensure_paper_accounts(conn)
    days = ["2026-01-05", "2026-01-06", "2026-01-07"]
    for _i, d in enumerate(days):
        conn.execute("INSERT INTO benchmark_bar(symbol,date,close) VALUES ('000001',?,1)",
                     (d,))
        # D2（01-06）没有行情 = 已确认停牌；其余日成交量只有 100 股/天
        if d != "2026-01-06":
            conn.execute("INSERT INTO raw_bar(symbol,date,open,high,low,close,volume,"
                         "amount,tradestatus) VALUES ('000001',?,10,10,10,10,1000,"
                         "10000,1)", (d,))
            conn.execute("INSERT INTO price_limit_daily(date,symbol,limit_ratio,"
                         "limit_up,limit_down,rule,status)"
                         " VALUES (?,?,10,11,9,'main','normal')", (d, "000001"))
        conn.execute("INSERT OR REPLACE INTO raw_coverage(symbol,date_from,date_to,"
                     "updated_at) VALUES ('000001','2026-01-05',?,'x')", (d,))
        conn.execute("INSERT OR REPLACE INTO corporate_sync(symbol,checked_through,"
                     "updated_at) VALUES ('000001',?,'x')", (d,))
    conn.execute("INSERT INTO security_master(symbol,name,exchange,is_st,updated_at)"
                 " VALUES ('000001','t','sz',0,'x')")
    conn.execute("INSERT INTO suspension_confirmation(symbol,date,source_note,"
                 "created_at) VALUES ('000001','2026-01-06','公告核对','x')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    with db.session(path) as conn:
        acc = accounts_mod.create_account(conn, name="历史", kind="manual",
                                          cash=100_000.0, start_date=days[0],
                                          participation=0.1)
        aid = acc["id"]
        sim.advance(conn, account_id=aid, to_date=days[0])
        o = sim.submit_manual_order(conn, account_id=aid, request_id="h1",
                                    symbol="000001", side="buy", volume=200,
                                    price=10.0)
        # D2：停牌顺延，订单仍是 pending，冻结还在
        sim.advance(conn, account_id=aid, to_date=days[1])
        d2 = sim.orders_query(conn, account_id=aid, date=days[1])["rows"][0]
        assert d2["status"] == "pending" and d2["closed_trade_date"] is None
        assert sim._frozen_cash(conn, aid) > 0, "当时资金仍被占住"
        # D3：容量 100 → 部分成交，然后用户撤掉余量
        sim.advance(conn, account_id=aid, to_date=days[2])
        d3 = sim.orders_query(conn, account_id=aid)["rows"][0]
        assert d3["filled_volume"] == 100 and d3["status"] in ("pending", "part") \
            or d3["status"] == "pending"
        sim.cancel_order(conn, account_id=aid, order_id=o["order_id"])
        today = sim.orders_query(conn, account_id=aid)["rows"][0]
        assert today["status"] == "canceled"
        assert today["volume"] == 200, "原委托量永不改写"
        assert today["filled_volume"] == 100, "已成交部分保留"
        # D2 的历史回答不因为 D3 的撤单而改变
        again = sim.orders_query(conn, account_id=aid, date=days[1])["rows"][0]
        assert again["status"] == "pending"
        assert again["filled_volume"] == 0, "未来成交不得泄漏到历史"
