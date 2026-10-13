"""交易成本模型测试。

要守住两件事：
1. **分段印花税**——2023-08-28 由 0.1% 降到 0.05%，回测样本横跨这次调整，
   必须按日期算，不能用一个常数糊弄。
2. **最低佣金会主导成本**——等权分散持有上千只时，单笔 5 元起收才是真正
   生效的那一项。这也是零成本回测最容易掩盖的摩擦。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from rpsboard.analytics.costs import (
    DEFAULT_PORTFOLIO_NOTIONAL,
    STAMP_CUTOFF,
    CostModel,
    default_cost_model,
)


def test_disabled_model_is_free():
    m = CostModel(enabled=False)
    assert m.round_trip_cost(date(2024, 1, 1), n_positions=1000) == 0.0
    assert m.buy_rate(1000, 1e6) == 0.0
    assert m.sell_rate(1000, 1e6, date(2024, 1, 1)) == 0.0


def test_stamp_duty_changes_on_cutoff():
    """印花税在 2023-08-28 由 0.1% 降到 0.05%。"""
    m = default_cost_model()
    before = m.sell_rate(100, 10_000_000, date(2023, 1, 1))
    after = m.sell_rate(100, 10_000_000, date(2024, 1, 1))
    # 单笔足够大时不触及 5 元起收，两次差额应恰好等于 0.05%
    assert before - after == pytest.approx(m.stamp_before - m.stamp_after)
    assert STAMP_CUTOFF == date(2023, 8, 28)


def test_stamp_is_sell_side_only():
    m = default_cost_model()
    n, p = 100, 10_000_000.0
    buy = m.buy_rate(n, p)
    sell = m.sell_rate(n, p, date(2024, 1, 1))
    # 卖出比买入多出来的，恰好就是印花税（此时最低佣金不生效）
    assert sell - buy == pytest.approx(m.stamp_after)


def test_minimum_commission_binds_for_diversified_portfolio():
    """100 万本金摊到 1000 只，每笔 1000 元，5 元起收才是主导项。"""
    m = default_cost_model()
    portfolio = DEFAULT_PORTFOLIO_NOTIONAL
    per_order = portfolio / 1000                     # = 1000 元
    prop_commission = per_order * m.commission_rate  # = 0.25 元
    assert prop_commission < m.commission_min        # 比例费率低于起收
    effective = m.buy_rate(1000, portfolio)
    expected_comm = 1000 * m.commission_min / portfolio
    assert effective == pytest.approx(
        expected_comm + m.transfer_fee_rate + m.slippage_rate
    )
    # 实际生效的佣金远高于比例费率——这正是零成本回测掩盖掉的摩擦
    assert effective > m.commission_rate * 10


def test_minimum_commission_not_binding_for_concentrated():
    """集中持仓时比例费率生效，起收不产生影响。"""
    m = default_cost_model()
    portfolio = DEFAULT_PORTFOLIO_NOTIONAL
    per_order = portfolio / 10                 # 10 万，比例佣金 25 元 > 5 元起收
    assert per_order * m.commission_rate > m.commission_min
    effective = m.buy_rate(10, portfolio)
    # 10 笔 × 每笔比例佣金 / 组合 = 恰好等于费率本身
    expected = m.commission_rate
    assert effective == pytest.approx(
        expected + m.transfer_fee_rate + m.slippage_rate
    )


def test_round_trip_is_sum_of_both_sides():
    m = default_cost_model()
    n, p, d = 200, DEFAULT_PORTFOLIO_NOTIONAL, date(2024, 6, 1)
    assert m.round_trip_cost(d, n, p) == pytest.approx(
        m.buy_rate(n, p) + m.sell_rate(n, p, d)
    )


def test_round_trip_reasonable_magnitude():
    """往返成本必须是「百分之几」量级。

    这条是防呆闸：早期版本把 5 元最低佣金直接作用在 notional=1.0 上，
    单期成本变成 500%，净值直接炸到 -402 万。
    """
    m = default_cost_model()
    for n in (20, 100, 1000):
        c = m.round_trip_cost(date(2024, 1, 1), n)
        assert 0.001 < c < 0.02, f"n={n} 的往返成本 {c:.4%} 不在合理区间"


def test_zero_or_negative_positions_are_free():
    m = default_cost_model()
    assert m.round_trip_cost(date(2024, 1, 1), 0) == 0.0
    assert m.round_trip_cost(date(2024, 1, 1), -5) == 0.0
    assert m.buy_rate(10, 0) == 0.0


def test_accepts_timestamp_and_string():
    m = default_cost_model()
    a = m.round_trip_cost(pd.Timestamp("2024-03-01"), 100)
    b = m.round_trip_cost("2024-03-01", 100)
    c = m.round_trip_cost(date(2024, 3, 1), 100)
    assert a == b == c


def test_describe_exposes_assumptions():
    d = default_cost_model().describe(100)
    assert d["enabled"] is True
    assert d["stamp_cutoff"] == "2023-08-28"
    # 印花税下调之后成本更低
    assert d["round_trip_after"] < d["round_trip_before"]
    assert 0 < d["round_trip_after"] < 0.02
