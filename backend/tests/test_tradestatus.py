"""tradestatus 口径测试。

守住的是一条真实事故：**按自然日间隔判停牌会把长假当成停牌**。
国庆 7 天长假后第一个交易日，全市场 5000+ 只票的「与上一根 bar 的间隔」都是 8 天，
旧实现（`gap.days <= 3`）把它们整段标成 `tradestatus=0`，再被
`matrix.build_tradable_matrix` 掩掉 → 21 个交易日整天没有 RPS（112,379 行）。
"""

from __future__ import annotations

import pandas as pd
import pytest
from rpsboard.sources import base

#: 2026 年国庆：09-30 之后到 10-08 之间没有交易日
HOLIDAY_DATES = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09"]


def test_holiday_gap_is_tradable():
    """长假后的第一个交易日必须算正常交易（旧口径会把它标成 0）。"""
    df = pd.DataFrame({
        "symbol": ["000001"] * len(HOLIDAY_DATES) + ["600519"] * len(HOLIDAY_DATES),
        "date": HOLIDAY_DATES * 2,
    })
    assert base.tradestatus_from_calendar(df).tolist() == [1] * 10


def test_missing_trading_day_flags_resumption():
    """某只票缺了一个交易日（停牌一天）→ 它的复牌 bar 标 0，别的票不受影响。

    交易日历来自**全市场**：只喂一只股票的 df 是测不出来的（它自己的日期就是日历）。
    """
    df = pd.DataFrame({
        "symbol": ["000001"] * 3 + ["600519"] * 4,
        "date": ["2024-01-02", "2024-01-03", "2024-01-05"]        # 000001 缺 01-04
                + ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
    })
    assert base.tradestatus_from_calendar(df).tolist() == [1, 1, 0, 1, 1, 1, 1]


def test_first_bar_is_tradable():
    """新股的首根 bar 没有前值可比较，必须算正常交易。"""
    df = pd.DataFrame({
        "symbol": ["000001", "000001", "000002"],
        "date": ["2024-01-02", "2024-01-03", "2024-01-03"],
    })
    assert base.tradestatus_from_calendar(df).tolist() == [1, 1, 1]


def test_calendar_must_cover_dates():
    """显式传入的日历必须覆盖所有日期，否则会算出假的间隔。"""
    df = pd.DataFrame({"symbol": ["000001"], "date": ["2024-01-02"]})
    with pytest.raises(ValueError):
        base.tradestatus_from_calendar(df, pd.Index(["2024-01-03"]))
