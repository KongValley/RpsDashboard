"""向量化改造的**等价性**测试。

改造把 `compute_industry_rps` / `compute_buckets` 从逐行 Python 循环换成向量实现。
这类改造最大的风险不是「跑不起来」，而是「跑得通但数字悄悄变了」，
所以这里把**旧实现原样抄一份**作为参照实现，在多组合成数据上逐值对比。

- `test_industry_rps_matches_legacy`：板块 RPS 逐行逐列相等（含空值与列序）
- `test_buckets_matches_legacy`：分档前瞻收益逐档相等
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from rpsboard.analytics import backtest, rps
from rpsboard.analytics.matrix import (
    build_close_matrix,
    build_st_flags,
    build_tradable_matrix,
)

from .fixtures import make_industry_map, make_industry_map_zh, make_long

PERIODS = (20, 60, 120, 250)


# ── 参照实现：改造前的原始代码，逐字保留 ──────────────────────────────
def legacy_compute_industry_rps(
    frames: dict[str, pd.DataFrame],
    industries: pd.Series,
    leader_rps: pd.DataFrame | None = None,
    names: pd.Series | None = None,
    periods: tuple[int, ...] = PERIODS,
) -> pd.DataFrame:
    if industries is None or industries.empty or not frames:
        return pd.DataFrame()

    ind_map = industries.to_dict()

    records: dict[tuple, dict] = {}
    for n in periods:
        ret = frames.get(f"ret{n}")
        if ret is None:
            continue
        for date in ret.index:
            row = ret.loc[date].dropna()
            if row.empty:
                continue
            for industry, vals in row.groupby(row.index.map(ind_map)):
                if industry is None or (isinstance(industry, float) and pd.isna(industry)):
                    continue
                key = (date, industry)
                rec = records.get(key)
                if rec is None:
                    rec = {"date": date, "industry": industry}
                    records[key] = rec
                rec[f"ret{n}"] = float(vals.mean())
                rec["member_count"] = int(vals.size)

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(list(records.values()))
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

    for n in periods:
        col = f"ret{n}"
        if col not in df.columns:
            continue
        df[f"rps{n}"] = (df.groupby("date")[col].rank(pct=True) * 100.0).round(2)

    if leader_rps is not None and not leader_rps.empty:
        lr = leader_rps.T
        lr = lr.dropna(how="all")
        if lr.empty:
            lead = pd.DataFrame(columns=["industry", "date", "leader_symbol"])
        else:
            date_cols = list(lr.columns)
            ind_series = pd.Series(lr.index.map(ind_map), index=lr.index)
            records2 = []
            for d in date_cols:
                col = lr[d].dropna()
                if col.empty:
                    continue
                tmp = pd.DataFrame(
                    {"industry": ind_series.reindex(col.index).values,
                     "value": col.values},
                    index=col.index,
                ).dropna(subset=["industry"])
                if tmp.empty:
                    continue
                best = tmp.groupby("industry")["value"].idxmax()
                for industry in best.index:
                    records2.append({
                        "industry": industry,
                        "date": d,
                        "leader_symbol": best[industry],
                    })
            lead = pd.DataFrame(
                records2, columns=["industry", "date", "leader_symbol"]
            )
            lead["date"] = pd.to_datetime(lead["date"]).dt.strftime("%Y-%m-%d")

        df = df.merge(lead, on=["date", "industry"], how="left")
        if names is not None and not names.empty:
            df["leader_name"] = df["leader_symbol"].map(names.to_dict())
    cols_order = ["date", "industry"]
    for n in periods:
        cols_order += [f"rps{n}", f"ret{n}"]
    cols_order += ["member_count", "leader_symbol", "leader_name"]
    df = df[[c for c in cols_order if c in df.columns]]
    return df.drop_duplicates(subset=["date", "industry"], keep="first")


def legacy_compute_buckets(rps_df: pd.DataFrame, fwd: pd.DataFrame) -> list[dict]:
    out: list[dict] = []
    if rps_df.empty or fwd.empty:
        return out

    common_cols = [c for c in rps_df.columns if c in fwd.columns]
    r = rps_df[common_cols]
    f = fwd[common_cols]

    counts = {lbl: 0 for lbl in backtest.BUCKET_LABELS}
    sums = {lbl: 0.0 for lbl in backtest.BUCKET_LABELS}
    wins = {lbl: 0 for lbl in backtest.BUCKET_LABELS}

    for date in r.index:
        rv = r.loc[date]
        fv = f.loc[date]
        for lbl, (lo, hi) in zip(backtest.BUCKET_LABELS, backtest.BUCKETS):
            sel = (rv >= lo) & (rv < hi) if hi < 100 else (rv >= lo)
            sel = sel & fv.notna()
            n = int(sel.sum())
            if n:
                vals = fv[sel]
                counts[lbl] += n
                sums[lbl] += float(vals.sum())
                wins[lbl] += int((vals > 0).sum())

    for lbl, (lo, hi) in zip(backtest.BUCKET_LABELS, backtest.BUCKETS):
        n = counts[lbl]
        out.append({
            "bucket": lbl,
            "range": f"{lo}-{hi}" if hi < 100 else f"{lo}-100",
            "samples": n,
            "avg_forward_return": round(sums[lbl] / n, 5) if n else None,
            "win_rate": round(wins[lbl] / n, 4) if n else None,
        })
    return out


# ── 断言工具 ─────────────────────────────────────────────────────────
def _assert_frames_equal(a: pd.DataFrame, b: pd.DataFrame, label: str) -> None:
    """两张表按 (date, industry) 对齐后逐值比较，含空值与列序。"""
    assert not a.empty, f"{label}: 新实现返回空"
    assert list(a.columns) == list(b.columns), (
        f"{label}: 列序不一致\n  新={list(a.columns)}\n  旧={list(b.columns)}"
    )
    key = ["date", "industry"]
    assert len(a) == len(b), f"{label}: 行数不一致 新={len(a)} 旧={len(b)}"
    a2 = a.sort_values(key).reset_index(drop=True)
    b2 = b.sort_values(key).reset_index(drop=True)
    assert a2[key].equals(b2[key]), f"{label}: 主键不一致"

    for col in a2.columns:
        if col in key:
            continue
        va, vb = a2[col], b2[col]
        if pd.api.types.is_numeric_dtype(va) and not pd.api.types.is_bool_dtype(va):
            na, nb = va.isna(), vb.isna()
            assert (na == nb).all(), f"{label}: 列 {col} 空值分布不一致"
            both = ~na
            np.testing.assert_allclose(
                va[both].to_numpy(dtype="float64"),
                vb[both].to_numpy(dtype="float64"),
                rtol=1e-12, atol=1e-12,
                err_msg=f"{label}: 列 {col} 数值不一致",
            )
        else:
            assert va.astype(object).equals(vb.astype(object)), (
                f"{label}: 列 {col} 不一致\n"
                f"  新(前5)={list(va.head(5))}\n  旧(前5)={list(vb.head(5))}"
            )


# ── 板块 RPS 等价性 ─────────────────────────────────────────────────
@pytest.mark.parametrize("seed", [42, 77, 123])
def test_industry_rps_matches_legacy(seed: int):
    df = make_long(n_symbols=60, n_days=300, seed=seed)
    close = build_close_matrix(df)
    frames = rps.compute_rps_frames(
        close, build_tradable_matrix(df), build_st_flags(df)
    )
    ind_map = make_industry_map_zh(sorted(df["symbol"].unique()), 5)
    names = pd.Series({s: f"名称{s}" for s in sorted(df["symbol"].unique())})

    new = rps.compute_industry_rps(frames, ind_map, leader_rps=frames["rps60"], names=names)
    old = legacy_compute_industry_rps(frames, ind_map, leader_rps=frames["rps60"], names=names)

    _assert_frames_equal(new, old, f"industry_rps(seed={seed})")


def test_industry_rps_matches_legacy_with_unmapped_symbols():
    """部分 symbol 没有行业映射时必须被同样地排除。"""
    df = make_long(n_symbols=50, n_days=280, seed=9)
    close = build_close_matrix(df)
    frames = rps.compute_rps_frames(close)
    syms = sorted(df["symbol"].unique())
    ind_map = make_industry_map(syms, 4).iloc[:-7]          # 故意丢掉 7 只
    new = rps.compute_industry_rps(frames, ind_map, leader_rps=frames["rps60"])
    old = legacy_compute_industry_rps(frames, ind_map, leader_rps=frames["rps60"])
    _assert_frames_equal(new, old, "industry_rps(部分无映射)")


def test_industry_rps_matches_legacy_with_new_listings():
    """新股导致各周期样本数不同 —— member_count 的覆盖语义必须保持一致。"""
    listed = {"000003": 60, "000011": 120, "000027": 200, "000041": 280}
    df = make_long(n_symbols=45, n_days=300, seed=15, listed_bars=listed)
    close = build_close_matrix(df)
    frames = rps.compute_rps_frames(close, build_tradable_matrix(df), build_st_flags(df))
    ind_map = make_industry_map_zh(sorted(df["symbol"].unique()), 5)
    new = rps.compute_industry_rps(frames, ind_map, leader_rps=frames["rps60"])
    old = legacy_compute_industry_rps(frames, ind_map, leader_rps=frames["rps60"])
    _assert_frames_equal(new, old, "industry_rps(新股)")


def test_industry_rps_matches_legacy_with_suspensions():
    """停牌会让某些 (date, industry) 在部分周期缺样本。"""
    dates = pd.bdate_range("2024-01-01", periods=260)
    symbols = [f"{i:06d}" for i in range(30)]
    suspend = pd.DataFrame(
        [[(i + j) % 23 == 0 for j in range(30)] for i in range(len(dates))],
        index=dates.strftime("%Y-%m-%d"), columns=symbols,
    )
    df = make_long(n_symbols=30, n_days=260, seed=33, tradestatus=suspend)
    close = build_close_matrix(df)
    frames = rps.compute_rps_frames(
        close, build_tradable_matrix(df), build_st_flags(df)
    )
    ind_map = make_industry_map_zh(symbols, 5)
    new = rps.compute_industry_rps(frames, ind_map, leader_rps=frames["rps60"])
    old = legacy_compute_industry_rps(frames, ind_map, leader_rps=frames["rps60"])
    _assert_frames_equal(new, old, "industry_rps(停牌)")


# ── 分档前瞻收益等价性 ──────────────────────────────────────────────
@pytest.mark.parametrize("seed", [5, 61])
def test_buckets_matches_legacy(seed: int):
    df = make_long(n_symbols=45, n_days=280, seed=seed)
    close = build_close_matrix(df)
    frames = rps.compute_rps_frames(
        close, build_tradable_matrix(df), build_st_flags(df)
    )
    fwd = backtest._forward_return(close, 20)
    new = backtest.compute_buckets(frames["rps60"], fwd)
    old = legacy_compute_buckets(frames["rps60"], fwd)

    assert len(new) == len(old)
    for a, b in zip(new, old):
        assert a["bucket"] == b["bucket"], "档位名不一致"
        assert a["range"] == b["range"], f"{a['bucket']} 区间不一致"
        assert a["samples"] == b["samples"], (
            f"{a['bucket']} 样本数不一致 新={a['samples']} 旧={b['samples']}"
        )
        if b["avg_forward_return"] is None:
            assert a["avg_forward_return"] is None
        else:
            assert a["avg_forward_return"] == pytest.approx(
                b["avg_forward_return"], abs=5e-5
            ), f"{a['bucket']} 平均前瞻收益不一致"
            assert a["win_rate"] == pytest.approx(b["win_rate"], abs=5e-4)
