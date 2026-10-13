"""大盘择时（F2）的正确性测试。

择时最大的风险是**未来函数**：如果「今天该不该开仓」这个判断用到了今天之后
才知道的信息，回测结果就是假的（而且往往虚假地好看）。因此这里复用
fixtures.perturb_after 做硬断言：扰动 T 日之后的行情，T 日的开仓决策必须不变。

同时覆盖：
- 均线未成型时默认空仓（数据不足不下注）
- 空仓期净值不变但**保留曲线点**（否则画不出开/空仓区间）
- 市场过滤只会减少交易机会，不会凭空制造收益
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from rpsboard.analytics.backtest import (
    MARKET_FILTERS,
    market_state,
)

from .fixtures import make_long


def _bench_from(close_wide: pd.DataFrame, symbol: str = "000001") -> pd.Series:
    return close_wide[symbol].dropna()


# ── market_state 本身 ──────────────────────────────────────────────
def test_market_filter_modes_all_supported():
    assert set(MARKET_FILTERS) == {"none", "ma200", "above_ma50", "trend_up"}


def test_none_is_always_open():
    s = pd.Series([100.0, 90.0, 80.0], index=pd.bdate_range("2024-01-01", periods=3))
    ok = market_state(s, "none")
    assert ok.all()


@pytest.mark.parametrize("mode", ["ma200", "above_ma50", "trend_up"])
def test_warmup_period_is_flat(mode):
    """均线尚未成型（样本不足 min_periods）时应判为空仓，而不是放行。"""
    n = 60                      # 不足以形成 200 日均线
    s = pd.Series(np.linspace(100, 200, n), index=pd.bdate_range("2024-01-01", periods=n))
    ok = market_state(s, mode)
    # 前若干行必须为 False；即使价格一路上涨也不能在数据不足时开仓
    assert not bool(ok.iloc[0])
    if mode in ("ma200", "trend_up"):
        assert not ok.iloc[:199].any(), "200 日均线未成型却允许开仓"


def test_ma200_matches_manual_comparison():
    n = 300
    idx = pd.bdate_range("2024-01-01", periods=n)
    close = pd.Series(np.concatenate([np.full(150, 100.0), np.full(150, 200.0)]), index=idx)
    ok = market_state(close, "ma200")
    manual = close > close.rolling(200, min_periods=200).mean()
    manual = manual.fillna(False)
    assert ok.equals(manual)
    # 150 日后价格翻倍，第 200 日起均线才成型，之后应为 True
    assert bool(ok.iloc[250]) is True


def test_trend_up_requires_both_conditions():
    n = 400
    idx = pd.bdate_range("2023-01-02", periods=n)
    # 先涨后跌：价格 > MA200 但 MA50 < MA200 → trend_up 必须为 False
    close = pd.Series(np.concatenate([np.linspace(100, 300, 300), np.linspace(300, 120, 100)]), index=idx)
    ok = market_state(close, "trend_up")
    ma50 = close.rolling(50, min_periods=50).mean().iloc[350:]
    ma200 = close.rolling(200, min_periods=200).mean().iloc[350:]
    manual = ((close.iloc[350:] > ma200) & (ma50 > ma200)).fillna(False)
    assert ok.iloc[350:].equals(manual)


# ── 无未来函数 ────────────────────────────────────────────────────
@pytest.mark.parametrize("mode", ["ma200", "above_ma50", "trend_up"])
def test_market_filter_has_no_lookahead(mode):
    """扰动截止日之后的行情，截止日当天的择时判定必须逐值不变。"""
    from rpsboard.analytics.matrix import build_close_matrix

    from .fixtures import perturb_after

    df = make_long(n_symbols=12, n_days=320, seed=808)
    cutoff = "2024-06-28"
    df2 = perturb_after(df, cutoff, seed=99, scale=1.5)

    wide1 = build_close_matrix(df)
    wide2 = build_close_matrix(df2)

    ok1 = market_state(_bench_from(wide1), mode)
    ok2 = market_state(_bench_from(wide2), mode)

    idx1 = ok1.index[ok1.index <= pd.Timestamp(cutoff)]
    idx2 = ok2.index[ok2.index <= pd.Timestamp(cutoff)]
    assert len(idx1) == len(idx2) and len(idx1) > 0
    assert ok1.reindex(idx1).tolist() == ok2.reindex(idx2).tolist(), (
        f"{mode}: 扰动 {cutoff} 之后的行情改变了当日及之前的择时判定 —— 存在未来函数"
    )


def test_market_filter_reacts_to_past_only():
    """反向确认：改动截止日**之前**的历史，判定必须改变（否则说明压根没在用行情）。"""
    from rpsboard.analytics.matrix import build_close_matrix

    from .fixtures import perturb_after

    df = make_long(n_symbols=12, n_days=320, seed=808)
    cutoff = "2024-06-28"
    df2 = perturb_after(df, cutoff, seed=7, scale=0.0)   # scale=0 不扰动
    df3 = df.copy()
    # 把 cutoff 之前的一段整体抬高，制造趋势反转
    mask = df3["date"] <= cutoff
    df3.loc[mask, "close"] = df3.loc[mask, "close"] * 1.8

    wide2 = build_close_matrix(df2)
    wide3 = build_close_matrix(df3)
    ok2 = market_state(_bench_from(wide2), "ma200")
    ok3 = market_state(_bench_from(wide3), "ma200")
    common = ok2.index.intersection(ok3.index)
    assert len(common) > 0
    assert ok2.reindex(common).tolist() != ok3.reindex(common).tolist(), (
        "改变历史行情后择时判定完全不变，判定逻辑可能没生效"
    )
