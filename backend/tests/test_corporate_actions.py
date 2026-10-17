"""公司行为核对：接口层合同 + 校验规则。

结算算法的行为（分红/送转/配股/税/阻断）在 test_portfolio.py；这里守的是
**接口与校验**：缺核对必须停、方案不能追溯改写、停牌确认的准入条件、
以及「未知税额」与「确认无税」必须可区分。
"""

from __future__ import annotations

from rpsboard import config
from rpsboard import db as dbmod
from rpsboard.broker import accounts as accounts_mod
from rpsboard.broker import corporate_actions as corp_mod
from rpsboard.broker import sim

from .test_portfolio import _seed_corp


def _account(conn, days, cash=1000.0, volume=1000):
    acc = accounts_mod.create_account(conn, name="核对", kind="manual", cash=cash,
                                      start_date=days[0], participation=0.1)
    aid = acc["id"]
    conn.execute(
        "INSERT INTO paper_position(account_id, trade_date, symbol, volume,"
        " available_volume, avg_price, cost_basis, close, price_date, market_value)"
        " VALUES (?,?,'000001',?,?,10.0,?,10.0,?,?)",
        (aid, days[0], volume, volume, volume * 10.0, days[0], volume * 10.0))
    conn.execute("INSERT OR REPLACE INTO paper_nav(account_id, trade_date, cash, market_value,"
                 " receivable, total, nav, n_positions, filled_orders)"
                 " VALUES (?,?,?,?,0,?,1,1,0)",
                 (aid, days[0], cash, volume * 10.0, cash + volume * 10.0))
    conn.commit()
    return aid


def _resolution(days, **over):
    base = {"record_date": days[0], "effective_date": days[1],
            "cash_gross": 100.0, "cash_pay_date": days[2],
            "share_delta": 0, "share_unlocks": [],
            "subscription_cash": 0.0, "subscription_pay_date": None,
            "taxes": [], "taxes_checked_through": days[3],
            "source_note": "公告核对"}
    base.update(over)
    return base


def test_actions_endpoint_lists_and_registers(tmp_path, monkeypatch):
    """待核对列表 → 登记 → 列表里变成已核对。"""
    from fastapi.testclient import TestClient
    from rpsboard.api import create_app

    path, days = _seed_corp(tmp_path, monkeypatch)
    with dbmod.session(path) as conn:
        aid = _account(conn, days)
    with TestClient(create_app()) as client:
        listed = client.get(f"/api/portfolio/accounts/{aid}/actions").json()
        assert len(listed["rows"]) == 1 and listed["rows"][0]["resolved"] is False
        assert listed["unresolved"] and \
            listed["unresolved"][0]["reason"] == "unresolved"

        ok = client.post(f"/api/portfolio/accounts/{aid}/actions", json={
            "symbol": "000001", "event_date": days[1], "category": 1,
            "resolution": _resolution(days)})
        assert ok.status_code == 200, ok.text
        assert ok.json()["ok"] is True

        after = client.get(f"/api/portfolio/accounts/{aid}/actions").json()
        assert after["rows"][0]["resolved"] is True
        assert after["unresolved"] == []
        assert len(after["actions"]) == 1


def test_actions_endpoint_rejects_unknown_event(tmp_path, monkeypatch):
    """不能凭空登记一条不存在的事件。"""
    from fastapi.testclient import TestClient
    from rpsboard.api import create_app

    path, days = _seed_corp(tmp_path, monkeypatch)
    with dbmod.session(path) as conn:
        aid = _account(conn, days)
    with TestClient(create_app()) as client:
        r = client.post(f"/api/portfolio/accounts/{aid}/actions", json={
            "symbol": "000002", "event_date": days[1], "category": 1,
            "resolution": _resolution(days)})
        assert r.status_code == 409


def test_actions_endpoint_rejects_unknown_field(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from rpsboard.api import create_app

    path, days = _seed_corp(tmp_path, monkeypatch)
    with dbmod.session(path) as conn:
        aid = _account(conn, days)
    with TestClient(create_app()) as client:
        r = client.post(f"/api/portfolio/accounts/{aid}/actions", json={
            "symbol": "000001", "event_date": days[1], "category": 1,
            "resolution": _resolution(days), "extra": 1})
        assert r.status_code == 422


def test_suspension_endpoint_guards(tmp_path, monkeypatch):
    """停牌确认：必须是市场交易日、且当天确实没有正常成交行情。"""
    from fastapi.testclient import TestClient
    from rpsboard.api import create_app

    from .test_portfolio import seed_portfolio_smoke

    info = seed_portfolio_smoke(str(tmp_path / "susp.db"))
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "susp.db")
    import rpsboard.api as apimod
    monkeypatch.setattr(apimod, "_cache", {"conn": None, "opened": None})
    with TestClient(create_app()) as client:
        ok = client.post("/api/portfolio/suspensions", json={
            "symbol": "000001", "date": info["start_date"],
            "source_note": "公告：临时停牌一日"})
        assert ok.status_code == 409, "当天有正常行情，不能登记为停牌"
        bad = client.post("/api/portfolio/suspensions", json={
            "symbol": "000001", "date": "1999-01-01", "source_note": "x"})
        assert bad.status_code == 409, "非交易日不能登记"
        assert client.post("/api/portfolio/suspensions", json={
            "symbol": "000001", "date": info["start_date"],
            "source_note": ""}).status_code == 422, "核对依据必填"


def test_resolution_validation_units():
    """校验规则的单元级契约（不依赖库）。"""
    good = {"record_date": "2026-01-05", "effective_date": "2026-01-06",
            "cash_gross": 100.0, "cash_pay_date": "2026-01-07",
            "share_delta": 1000,
            "share_unlocks": [{"kind": "bonus", "volume": 600,
                               "available_date": "2026-01-08"},
                              {"kind": "subscription", "volume": 400,
                               "available_date": "2026-01-09"}],
            "subscription_cash": 800.0, "subscription_pay_date": "2026-01-07",
            "taxes": [{"date": "2026-01-07", "amount": 20.0}],
            "taxes_checked_through": "2026-01-09",
            "source_note": "公告核对：每 10 股送 6 转 4 配 2"}
    norm = corp_mod.normalize_resolution(good)
    assert norm["share_delta"] == 1000 and len(norm["share_unlocks"]) == 2

    import pytest as _pytest

    def rejects(**over):
        bad = dict(good)
        bad.update(over)
        with _pytest.raises(corp_mod.CorporateActionError):
            corp_mod.normalize_resolution(bad)

    # 解锁批次之和必须等于送转总量
    rejects(share_unlocks=[{"kind": "bonus", "volume": 999,
                            "available_date": "2026-01-08"}])
    # 解锁日不能早于生效日
    rejects(share_unlocks=[{"kind": "bonus", "volume": 1000,
                            "available_date": "2026-01-05"}])
    # 登记日不能晚于生效日
    rejects(record_date="2026-01-08")
    # 现金分红给了金额却不到账日 → 拒绝（不能默认不用到账）
    rejects(cash_pay_date=None)
    # 未知类别 / 负金额 / 超大 note
    rejects(unknown_field=1)
    rejects(taxes=[{"date": "2026-01-07", "amount": -1.0}])
    rejects(source_note="")
    # 缩股当前不支持，必须显式拒绝而不是当成 0
    rejects(share_delta=-100, share_unlocks=[])


def test_unsupported_categories_are_named():
    assert 13 in corp_mod.UNSUPPORTED_CATEGORIES
    assert 4 in corp_mod.UNSUPPORTED_CATEGORIES
    assert 1 in corp_mod.ACTION_CATEGORIES
    assert 5 in corp_mod.CAPITAL_CATEGORIES


def test_checked_through_advances_only_for_successful_symbols(tmp_path, monkeypatch):
    """公司行为采集的查全标记只跟着**成功**的股票走（失败保留旧值）。"""
    from .test_portfolio import seed_portfolio_smoke

    path = tmp_path / "corp_sync.db"
    info = seed_portfolio_smoke(str(path))
    days = info["tail"]
    monkeypatch.setattr(config, "DB_PATH", path)
    from rpsboard.sources import corporate_src

    with dbmod.session(path) as conn:
        conn.execute("UPDATE corporate_sync SET checked_through=?", (days[0],))
        conn.commit()

        class _Src:
            def __init__(self, *a, **k):
                pass

            def close(self):
                pass

        # 采一只成功、一只失败：成功的推进，失败的原样保留
        monkeypatch.setattr(corporate_src, "TdxSource", _Src)
        monkeypatch.setattr(corporate_src, "fetch_capital_changes",
                            lambda src, sym: [] if sym == "000001" else None)
        res = corporate_src.sync_corporate(conn, ["000001", "000002"], workers=1)
        assert res["ok"] == 1 and res["failed"] == ["000002"]
        rows = {r["symbol"]: r["checked_through"] for r in conn.execute(
            "SELECT symbol, checked_through FROM corporate_sync"
            " WHERE symbol IN ('000001','000002')")}
    assert rows["000001"] == days[-1], "成功的股票推进到覆盖末日"
    assert rows["000002"] == days[0], "失败的股票保留旧标记，不冒充查过"


def test_corporate_sync_skips_when_no_coverage(tmp_path, monkeypatch):
    """没有原价覆盖时不猜任何东西，直接返回空。"""
    from rpsboard.sources import corporate_src

    path = tmp_path / "nocoverage.db"
    dbmod.init_db(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    with dbmod.session(path) as conn:
        res = corporate_src.sync_corporate(conn)
    assert res == {"ok": 0, "failed": [], "events": 0}


def test_share_change_invalidates_stale_orders(tmp_path, monkeypatch):
    """送转/配股当天：该股**旧余单作废**并留下可追溯原因。

    除权后股价基准变了，按老限价挂着的单子要么白等一天，要么以与新股数不匹配的
    价格成交 —— 都不能留。
    """
    path, days = _seed_corp(tmp_path, monkeypatch)
    with dbmod.session(path) as conn:
        aid = _account(conn, days)
        conn.execute(
            "INSERT INTO paper_order(account_id, request_id, trade_date, symbol, side,"
            " volume, price, status, source, created_at, updated_at)"
            " VALUES (?,?,?,?,'sell',1000,12.0,'pending','manual','x','x')",
            (aid, "stale", days[0], "000001"))
        conn.commit()
        corp_mod.resolve_action(
            conn, account_id=aid, symbol="000001", event_date=days[1], category=1,
            last_settled=days[0],
            resolution={"record_date": days[0], "effective_date": days[1],
                        "cash_gross": 0.0, "cash_pay_date": None,
                        "share_delta": 1000,
                        "share_unlocks": [{"kind": "bonus", "volume": 1000,
                                           "available_date": days[2]}],
                        "subscription_cash": 0.0, "subscription_pay_date": None,
                        "taxes": [], "taxes_checked_through": days[3],
                        "source_note": "公告：每 10 股送 10 股"})
        sim.advance(conn, account_id=aid, to_date=days[1])
        row = conn.execute("SELECT status, closed_trade_date, err_msg FROM paper_order"
                           " WHERE account_id=?", (aid,)).fetchone()
    assert row["status"] == "canceled"
    assert row["closed_trade_date"] == days[1], "终止日记的是**账户业务日**"
    assert "corporate_action_order_invalidated" in (row["err_msg"] or "")


def test_corporate_cash_deduction_revalidates_buy_freeze(tmp_path, monkeypatch):
    """公司行为扣现金后，买单冻结按 id 先到先得重验；不够的整张余单撤掉。

    不这么做的话，冻结会一直大于现金，之后每次撮合都在算一个假的可用现金。
    """
    path, days = _seed_corp(tmp_path, monkeypatch)
    with dbmod.session(path) as conn:
        # 现金刚好够一笔买单的冻结（1000 + 5.01 = 1005.01）
        aid = _account(conn, days, cash=1005.01, volume=1000)
        conn.execute(
            "INSERT INTO paper_order(account_id, request_id, trade_date, symbol, side,"
            " volume, price, status, source, created_at, updated_at)"
            " VALUES (?,?,?,?,'buy',100,10.0,'pending','manual','x','x')",
            (aid, "buy1", days[0], "000001"))
        conn.commit()
        # 纯现金流出（缴款/代扣税款一类）：生效日当天扣掉 1000 元现金 ——
        # 正好把那笔买单的冻结（1005.01）挤掉。
        corp_mod.resolve_action(
            conn, account_id=aid, symbol="000001", event_date=days[1], category=1,
            last_settled=days[0],
            resolution={"record_date": days[0], "effective_date": days[1],
                        "cash_gross": 0.0, "cash_pay_date": None,
                        "share_delta": 0, "share_unlocks": [],
                        "subscription_cash": 1000.0, "subscription_pay_date": days[1],
                        "taxes": [], "taxes_checked_through": days[3],
                        "source_note": "公告：配股缴款（股份尚未到账）"})
        res = sim.advance(conn, account_id=aid, to_date=days[1])
        assert res["ok"] is True
        row = conn.execute("SELECT status, err_msg FROM paper_order WHERE account_id=?",
                           (aid,)).fetchone()
        assert row["status"] == "canceled", row
        assert "corporate_action_reservation_conflict" in (row["err_msg"] or ""), row
        state = sim.load_account(conn, account_id=aid)
        assert state.cash >= 0, "扣款后现金不能为负"
