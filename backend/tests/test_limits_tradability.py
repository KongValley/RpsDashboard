"""回测中的可交易性测试。

守住两条容易被忽略的建模规则：
1. **买入日涨停买不进** —— 涨停的股票必须从当日候选池剔除。
2. **卖出日跌停/停牌卖不掉** —— 这部分持仓的收益不可实现，必须顺延，
   不能照单全收，否则会系统性高估策略的可执行性。
"""

from __future__ import annotations

import pandas as pd
import pytest

from rpsboard.analytics.backtest import _exit_blocked, _forward_return
from rpsboard.analytics.limits import limit_mask


def _close(closes: dict[str, list[float]]) -> pd.DataFrame:
    return pd.DataFrame(closes, index=pd.bdate_range("2024-01-02",
                                                    periods=len(next(iter(closes.values())))))


def test_exit_blocked_by_limit_down():
    """计划卖出日跌停 → 该持仓标记为顺延。

    跌停发生在 T+hold，掩码要标记在**买入日 T** 那一行，
    所以实现里必须 shift(-hold)；少了这一步就会把「买入当天跌停」
    错当成「卖出时卖不掉」。
    """
    idx = pd.bdate_range("2024-01-02", periods=5)
    dn = pd.DataFrame({
        "A": [False, False, False, False, False],
        "B": [False, False, False, True, True],   # B 在第 3、4 天跌停
    }, index=idx)
    tradable = pd.DataFrame(True, index=idx, columns=["A", "B"])
    blocked = _exit_blocked(dn, tradable, hold=3)
    # 第 r 行的卖出日是第 r+3 行：B 在第 3、4 天跌停
    #   → 第 0 行（卖出日=第3天）受阻、第 1 行（卖出日=第4天）受阻
    #   → 第 2 行之后没有未来卖出日，不受阻
    assert bool(blocked.iloc[0]["B"]) is True
    assert bool(blocked.iloc[1]["B"]) is True
    assert not bool(blocked.to_numpy().any(axis=1)[2:].any())
    assert not bool(blocked["A"].any())


def test_exit_blocked_by_suspension():
    """计划卖出日停牌 → 同样顺延。"""
    idx = pd.bdate_range("2024-01-02", periods=5)
    tradable = pd.DataFrame(True, index=idx, columns=["A"])
    tradable.iloc[3, 0] = False                  # A 在第 3 天停牌
    blocked = _exit_blocked(None, tradable, hold=3)
    assert bool(blocked.iloc[0]["A"]) is True
    assert bool(blocked.iloc[2]["A"]) is False


def test_exit_blocked_none_when_no_data():
    """没有涨跌停也没有停牌数据时返回 None，退化为旧行为而不是伪造。"""
    idx = pd.bdate_range("2024-01-02", periods=3)
    tradable = pd.DataFrame(True, index=idx, columns=["A"])
    assert _exit_blocked(None, tradable, hold=2) is not None   # 停牌数据可判定
    assert _exit_blocked(None, None, hold=2) is None            # 两者都没有


def test_forward_return_shift_alignment():
    fwd = _forward_return(_close({"A": [10.0, 11.0, 12.0, 13.0]}), hold=2)
    # T 日买入持有 2 天 → T+2 的 close/T - 1
    assert fwd["A"].iloc[0] == pytest.approx(12.0 / 10.0 - 1)
    assert pd.isna(fwd["A"].iloc[-1])          # 末尾没有未来价格


def test_buy_day_limit_up_excluded_from_candidates():
    """构造一只当日涨停的股票，确认它会被剔除出可买集合。"""
    idx = pd.bdate_range("2024-01-02", periods=3)
    close = pd.DataFrame({"A": [10.0, 11.0, 11.5], "B": [10.0, 10.5, 10.6]}, index=idx)
    ratios = pd.DataFrame({"A": [10.0, 10.0, 10.0], "B": [10.0, 10.0, 10.0]}, index=idx)
    up, _ = limit_mask(close, ratios)
    # 第 1 天 A 涨停（买不进），B 未涨停
    assert bool(up.iloc[1]["A"]) is True
    assert not bool(up.iloc[1]["B"])

    tradable = pd.DataFrame(True, index=idx, columns=["A", "B"])
    candidates = pd.DataFrame(True, index=idx, columns=["A", "B"])
    mask = candidates & ~up & tradable
    assert bool(mask.iloc[1]["A"]) is False     # A 被剔除
    assert bool(mask.iloc[1]["B"]) is True      # B 保留


def test_deferred_not_counted_twice():
    """顺延的持仓不能既算 0 又在某处被重复计入。

    这里验证接口约定：blocked 掩码只标记「不可卖出」，
    计算收益时应当从 names 中剔除，而不是既剔除又加回来。
    """
    idx = pd.bdate_range("2024-01-02", periods=3)
    fwd = pd.DataFrame({"A": [0.1, 0.2, 0.3], "B": [0.1, 0.2, 0.3]}, index=idx)
    blocked = pd.DataFrame({"A": [True, False, False],
                           "B": [False, False, False]}, index=idx)
    names = ["A", "B"]
    bad = blocked.iloc[0].reindex(names).fillna(False).astype(bool)
    remaining = pd.Index(names)[~bad.values]
    assert list(remaining) == ["B"]
    assert fwd.loc[idx[0], remaining].mean() == pytest.approx(0.1)
