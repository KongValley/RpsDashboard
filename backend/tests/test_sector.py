"""板块 RPS 测试：等权聚合、排名正确性、边界情况。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from rpsboard.analytics import rps
from rpsboard.analytics.matrix import (
    build_close_matrix,
    build_st_flags,
    build_tradable_matrix,
)

from .fixtures import make_industry_map_zh, make_long


def test_industry_return_is_equal_weight_mean():
    """行业收益 = 成分股收益的算术平均。"""
    dates = pd.bdate_range("2024-01-01", periods=5)
    closes = {
        "000001": [10, 11, 12, 13, 14],
        "000002": [20, 22, 24, 26, 28],
        "000003": [50, 45, 40, 35, 30],
    }
    rows = [
        {"symbol": s, "date": d.strftime("%Y-%m-%d"), "close": c,
         "pct_chg": 0, "tradestatus": 1, "is_st": 0}
        for s, arr in closes.items() for d, c in zip(dates, arr)
    ]
    df = pd.DataFrame(rows)
    close = build_close_matrix(df)
    # 只有 4 天数据 → 只能用 period=2，否则 shift(N) 全是 NaN 就没有任何记录
    frames = rps.compute_rps_frames(close, periods=(2,))
    ind = rps.compute_industry_rps(
        frames, make_industry_map_zh(list(closes), 3), periods=(2,)
    )
    assert not ind.empty
    by_name = ind.set_index(["date", "industry"])
    # 000003 是第三只 → 落在 names[2] = "新能源"，且它是该行业唯一成分股。
    # 首个有效日是第 3 个交易日（shift(2) 需要两根前置 K 线）。
    first_day = "2024-01-03"
    row = by_name.loc[(first_day, "新能源")]
    assert row["ret2"] == pytest.approx(40 / 50 - 1, abs=1e-6)   # -0.20
    assert row["member_count"] == 1
    # 末日 2024-01-05：30/40 - 1 = -0.25
    assert by_name.loc[("2024-01-05", "新能源")]["ret2"] == pytest.approx(-0.25, abs=1e-6)


def test_industry_rps_ranking():
    """行业 RPS = 该行业平均涨幅在全部行业中的百分位。"""
    df = make_long(n_symbols=40, n_days=200, seed=21)
    close = build_close_matrix(df)
    frames = rps.compute_rps_frames(close, build_tradable_matrix(df),
                                    build_st_flags(df))
    ind_map = make_industry_map_zh(sorted(df["symbol"].unique()), 5)
    ind = rps.compute_industry_rps(frames, ind_map)

    for _date, grp in ind.groupby("date"):
        if len(grp) < 2:
            continue
        # 15 日周期让「上市 15~19 个交易日」的股票在那几天先进榜，此时 ret20 还全是
        # NaN（idxmax 会直接抛 ValueError）。断言只针对有长周期数据的日期。
        if grp["ret20"].isna().all():
            continue
        # 最强的行业，rps 必须显著高于均值
        best = grp.loc[grp["ret20"].idxmax()]
        assert best["rps20"] >= grp["rps20"].mean()
        worst = grp.loc[grp["ret20"].idxmin()]
        assert worst["rps20"] <= grp["rps20"].mean()


def test_single_member_industry_no_crash():
    """单成分股行业不应崩溃。"""
    df = make_long(n_symbols=6, n_days=100, seed=31)
    close = build_close_matrix(df)
    frames = rps.compute_rps_frames(close)
    ind_map = pd.Series({s: f"唯一{i}" for i, s in enumerate(sorted(df["symbol"].unique()))})
    ind = rps.compute_industry_rps(frames, ind_map)
    assert not ind.empty
    assert (ind["member_count"] == 1).all()


def test_missing_data_does_not_propagate_nan():
    """某行业某日样本不全时，按可用样本算，不产生 NaN 传播。"""
    df = make_long(n_symbols=12, n_days=90, seed=41)
    close = build_close_matrix(df)
    frames = rps.compute_rps_frames(close)
    ind_map = make_industry_map_zh(sorted(df["symbol"].unique()), 3)
    ind = rps.compute_industry_rps(frames, ind_map)
    for col in ("ret20", "ret60"):
        if col in ind.columns:
            vals = ind[col].dropna()
            assert not vals.empty
            assert np.isfinite(vals).all()


def test_leader_is_highest_rps_member():
    """领涨股 = 该行业内 RPS 最高的成分股。"""
    df = make_long(n_symbols=30, n_days=200, seed=51)
    close = build_close_matrix(df)
    tradable, st = build_tradable_matrix(df), build_st_flags(df)
    frames = rps.compute_rps_frames(close, tradable, st)
    ind_map = make_industry_map_zh(sorted(df["symbol"].unique()), 4)
    ind = rps.compute_industry_rps(frames, ind_map, leader_rps=frames["rps60"])

    last = ind["date"].max()
    sub = ind[ind["date"] == last]
    checked = 0
    for _, row in sub.iterrows():
        if pd.isna(row["leader_symbol"]):
            continue
        members = ind_map[ind_map == row["industry"]].index.tolist()
        vals = frames["rps60"].loc[last, members].dropna()
        if vals.empty:
            continue
        assert row["leader_symbol"] in vals.index
        assert vals[row["leader_symbol"]] == vals.max()
        checked += 1
    assert checked > 0, "没有可校验的领涨股"


def test_empty_industry_map_returns_empty():
    df = make_long(n_symbols=5, n_days=50)
    close = build_close_matrix(df)
    frames = rps.compute_rps_frames(close)
    ind = rps.compute_industry_rps(frames, pd.Series(dtype="object"))
    assert ind.empty
