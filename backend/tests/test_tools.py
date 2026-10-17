"""三个交易工具的验收：数值必须与计划里手算的结果逐项一致。

这些数字不是「跑出来的」，是**先手算、再断言** —— 费用模型（最低佣金 5 元、
印花税按日分段、过户费）与数量规则（板块步长）都在断言里被钉住。
"""

from __future__ import annotations

import pytest
from rpsboard.analytics import tools as tools_mod

BASE = {"symbol": "000001", "equity": 100_000.0, "cash": 20_000.0,
        "entry": 10.0, "stop": 9.0, "risk_pct": 0.01, "max_weight": 0.25,
        "trade_date": "2026-01-05"}


def test_position_size_matches_hand_calculation():
    r = tools_mod.position_size(**BASE)
    assert r["volume"] == 900
    assert r["notional"] == 9_000.0
    assert r["buy_fee"] == pytest.approx(5.09)
    assert r["stop_sell_fee"] == pytest.approx(9.13)
    assert r["loss_at_stop"] == pytest.approx(914.22)
    assert r["risk_budget"] == pytest.approx(1_000.0)
    assert r["remaining_cash"] == pytest.approx(10_994.91)
    assert r["weight"] == pytest.approx(0.09)
    assert r["reason"] == "ok"
    # 1000 股会超出 1000 元风险预算（1014.69），不能返回 1000
    assert r["volume"] != 1000


def test_position_size_zero_when_below_minimum():
    r = tools_mod.position_size(**{**BASE, "equity": 500.0, "cash": 500.0})
    assert r["volume"] == 0
    assert r["reason"] == "below_minimum_quantity"
    assert r["notional"] == 0.0 and r["loss_at_stop"] == 0.0
    assert r["remaining_cash"] == 500.0


def test_position_size_rejects_bad_stop():
    with pytest.raises(tools_mod.ToolError):
        tools_mod.position_size(**{**BASE, "stop": 11.0})


def test_position_size_uses_board_step():
    # 科创板买入必须是 200 的整数倍之外的任意整数（步长 1）→ 数量不强制整百
    r = tools_mod.position_size(**{**BASE, "symbol": "688001"})
    assert r["volume"] >= 200
    assert r["board"] == "star"


def test_round_trip_matches_hand_calculation():
    r = tools_mod.round_trip(symbol="000001", volume=1000, buy_price=10.0,
                             sell_price=11.0, trade_date="2026-01-05")
    assert r["buy_fee"] == pytest.approx(5.10)
    assert r["sell_fee"] == pytest.approx(10.61)
    assert r["total_fees"] == pytest.approx(15.71)
    assert r["net_pnl"] == pytest.approx(984.29)
    assert r["breakeven_price"] == pytest.approx(10.02)


def test_breakeven_price_is_exact():
    below = tools_mod.round_trip(symbol="000001", volume=1000, buy_price=10.0,
                                 sell_price=10.01, trade_date="2026-01-05")
    at = tools_mod.round_trip(symbol="000001", volume=1000, buy_price=10.0,
                              sell_price=10.02, trade_date="2026-01-05")
    assert below["net_pnl"] == pytest.approx(-5.21), "还没保本"
    assert at["net_pnl"] == pytest.approx(4.79), "刚好保本"


def test_round_trip_stamp_tax_history():
    """2023-08-28 起印花税减半：同价同量，早卖税更重。"""
    before = tools_mod.round_trip(symbol="000001", volume=1000, buy_price=10.0,
                                  sell_price=11.0, trade_date="2023-06-01")
    after = tools_mod.round_trip(symbol="000001", volume=1000, buy_price=10.0,
                                 sell_price=11.0, trade_date="2024-06-01")
    assert before["sell_fee"] > after["sell_fee"]


def test_round_trip_rejects_illegal_volume():
    with pytest.raises(tools_mod.ToolError):
        tools_mod.round_trip(symbol="000001", volume=150, buy_price=10.0,
                             sell_price=11.0, trade_date="2026-01-05")
    with pytest.raises(tools_mod.ToolError):
        tools_mod.round_trip(symbol="000001", volume=1000, buy_price=10.004,
                             sell_price=11.0, trade_date="2026-01-05")


def test_tools_api_validates_unknown_fields(client):
    r = client.post("/api/tools/position-size", json={**BASE, "nope": 1})
    assert r.status_code == 422


def test_tools_api_ok(client):
    r = client.post("/api/tools/round-trip",
                    json={"symbol": "000001", "volume": 1000, "buy_price": 10.0,
                          "sell_price": 11.0, "trade_date": "2026-01-05"})
    assert r.status_code == 200
    assert r.json()["net_pnl"] == pytest.approx(984.29)


def test_stress_test_matches_hand_calculation(portfolio_client):
    """现金 20000 + A 50000 + B 30000；A=-10%、B=-20% → -11000。"""
    client, info = portfolio_client
    aid = info["smoke_account_id"]
    r = client.post("/api/tools/stress", json={
        "account_id": aid,
        "shocks": [{"symbol": info["a"], "return": -0.10},
                   {"symbol": info["b"], "return": -0.20}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["equity_before"] == pytest.approx(100_000.0)
    assert body["pnl"] == pytest.approx(-11_000.0)
    assert body["equity_after"] == pytest.approx(89_000.0)
    assert body["return"] == pytest.approx(-0.11)


def test_stress_rejects_unknown_symbol(portfolio_client):
    client, info = portfolio_client
    r = client.post("/api/tools/stress", json={
        "account_id": info["smoke_account_id"],
        "shocks": [{"symbol": "600009", "return": -0.1}]})
    assert r.status_code == 422


def test_stress_requires_curve(client):
    r = client.post("/api/tools/stress", json={"account_id": 999, "shocks": []})
    assert r.status_code == 409
