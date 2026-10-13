"""涨跌停判定测试（收益率比法）。

关键约束：**必须比收益率，不能比价格**。
`daily_bar.close` 是后复权价，`limit_up_price` 基于不复权前收盘价，
直接比会算出 4303 家假涨停（实测占全市场 77%）。收益比率与复权基准无关。
"""

from __future__ import annotations

import pandas as pd
import pytest

from rpsboard.analytics.limits import NO_LIMIT_STATUSES, limit_mask

RULES = ["main_10pct", "chinext_20pct", "star_20pct", "bse_30pct",
         "st_main_10pct", "ipo_first_5_days"]


def _frames(closes: dict[str, list[float]], ratios: dict[str, float],
            n_days: int):
    idx = pd.bdate_range("2024-01-02", periods=n_days)
    return pd.DataFrame(closes, index=idx), pd.DataFrame(
        {s: [ratios[s]] * n_days for s in closes}, index=idx
    )


def test_limit_rules_cover_all_board_types():
    """六种规则都要能被判定逻辑接受，不应有任何一种被硬编码忽略。"""
    assert len(set(RULES)) == 6
    ratios = {"main_10pct": 10.0, "chinext_20pct": 20.0, "star_20pct": 20.0,
              "bse_30pct": 30.0, "st_main_10pct": 5.0, "ipo_first_5_days": 0.0}
    close, rat = _frames({r: [10.0, 10.0] for r in ratios}, ratios, 2)
    up, _ = limit_mask(close, rat)
    assert up.shape == close.shape
    # 平盘时任何一种规则都不该判成涨停
    assert not up.iloc[1].any()


@pytest.mark.parametrize("ratio,move,expect_up", [
    (10.0, 10.0, True),      # 主板正好 +10%
    (10.0, 9.9, True),       # 容差内
    (10.0, 9.5, False),      # 容差外
    (20.0, 19.9, True),      # 创业板，容差内
    (20.0, 19.5, False),     # 创业板，容差外
    (20.0, 10.5, False),     # 创业板只涨 10% 不算涨停
    (30.0, 29.9, True),      # 北交所，容差内
    (5.0, 5.0, True),        # ST 正好 +5%
    (5.0, 3.0, False),       # ST 涨 3% 未到
])
def test_limit_up_threshold_by_ratio(ratio, move, expect_up):
    close, rat = _frames({"A": [10.0, 10.0 * (1 + move / 100)]},
                         {"A": ratio}, 2)
    up, _ = limit_mask(close, rat)
    assert bool(up.iloc[1]["A"]) is expect_up


@pytest.mark.parametrize("ratio,move,expect_dn", [
    (10.0, -10.0, True),
    (10.0, -9.9, True),      # 容差内
    (10.0, -9.5, False),     # 容差外
    (10.0, -5.0, False),
    (20.0, -20.0, True),
])
def test_limit_down_threshold_by_ratio(ratio, move, expect_dn):
    close, rat = _frames({"A": [10.0, 10.0 * (1 + move / 100)]},
                         {"A": ratio}, 2)
    _, dn = limit_mask(close, rat)
    assert bool(dn.iloc[1]["A"]) is expect_dn


def test_no_price_limit_is_not_treated_as_limit():
    """新股前五日无涨跌幅限制：涨 40% 也不能算涨停。"""
    close, rat = _frames({"A": [10.0, 14.0]}, {"A": 0.0}, 2)
    status = pd.DataFrame({"A": ["no_price_limit"] * 2},
                          index=close.index)
    up, dn = limit_mask(close, rat, status)
    assert not up.to_numpy().any()
    assert not dn.to_numpy().any()


def test_missing_pre_close_is_not_treated_as_limit():
    close, rat = _frames({"A": [10.0, 10.0]}, {"A": 10.0}, 2)
    status = pd.DataFrame({"A": ["missing_pre_close"] * 2},
                          index=close.index)
    up, _ = limit_mask(close, rat, status)
    assert not up.to_numpy().any()
    for s in NO_LIMIT_STATUSES:
        assert s in ("no_price_limit", "missing_pre_close")


def test_first_day_has_no_previous_close():
    """首行没有前收盘，一律不算涨停（否则会把所有股票第一天都算成涨停）。"""
    close, rat = _frames({"A": [11.0, 11.0]}, {"A": 10.0}, 2)
    up, _ = limit_mask(close, rat)
    assert not bool(up.iloc[0]["A"])


def test_ratio_method_not_price_method():
    """构造一个后复权价与不复权涨停价基准不同、但收益率达标的例子。

    close 全部乘以 1.0（这里不缩放），而 limit_up 若按不复权前收盘价给出，
    直接比价格会得到「远低于涨停价」的结论；收益率比则能正确判定。
    """
    idx = pd.bdate_range("2024-01-02", periods=2)
    close = pd.DataFrame({"A": [100.0, 110.0]}, index=idx)   # +10%
    ratios = pd.DataFrame({"A": [10.0, 10.0]}, index=idx)
    up, _ = limit_mask(close, ratios)
    assert bool(up.iloc[1]["A"]) is True


def test_empty_inputs_return_empty_masks():
    empty = pd.DataFrame()
    up, dn = limit_mask(empty, empty)
    assert up.empty and dn.empty


def test_ratio_mismatch_columns_are_reindexed():
    """涨跌停表缺某只票时，该票应判为「无约束」而不是崩溃。"""
    idx = pd.bdate_range("2024-01-02", periods=2)
    close = pd.DataFrame({"A": [10.0, 11.0], "B": [10.0, 10.5]}, index=idx)
    rat = pd.DataFrame({"A": [10.0, 10.0]}, index=idx)   # 只有 A 有比例
    up, _ = limit_mask(close, rat)
    assert bool(up.iloc[1]["A"]) is True
    assert not bool(up.iloc[1]["B"])      # B 没有 ratio → 不判定
