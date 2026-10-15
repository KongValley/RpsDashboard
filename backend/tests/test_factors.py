"""因子库测试：公式、去极值、缺失归一、训练窗、无未来函数、IC 口径。

全部离线（合成行情 + 临时库），不触网。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from rpsboard import db as dbmod
from rpsboard.analytics import backtest as bt
from rpsboard.analytics import factors as fac

from .fixtures import make_long, perturb_after

_BAR_COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount",
             "pct_chg", "is_st", "tradestatus"]


@pytest.fixture(autouse=True)
def _small_ic(monkeypatch):
    """合成数据只有几十只票，而 MIN_CROSS_IC=100 会把**所有**日期的 IC 判成不可信。

    这里只降阈值，不改判定逻辑本身（门槛语义由生产常量决定）。
    """
    monkeypatch.setattr(fac, "MIN_CROSS_IC", 3)


def _store(conn, df) -> None:
    df[_BAR_COLS].to_sql("daily_bar", conn, if_exists="append", index=False)


def _db(tmp_path, name: str, df):
    """把一段长表灌进一个带完整 SCHEMA 的临时库，返回库路径。"""
    path = tmp_path / name
    with dbmod.session(path) as conn:
        conn.executescript(dbmod.SCHEMA)
        _store(conn, df)
    return path


def _bars(n_symbols: int = 20, n_days: int = 300, seed: int = 5) -> pd.DataFrame:
    """带 OHLCV 的合成长表（`make_long` 只有 close，因子库要 high/low/amount）。"""
    df = make_long(n_symbols=n_symbols, n_days=n_days, seed=seed)
    rng = np.random.default_rng(seed)
    close = df["close"].to_numpy()
    return df.assign(
        open=close * (1 + rng.normal(0, 0.005, len(close))),
        high=close * (1 + np.abs(rng.normal(0, 0.01, len(close)))),
        low=close * (1 - np.abs(rng.normal(0, 0.01, len(close)))),
        volume=rng.uniform(1e5, 5e6, len(close)),
        amount=rng.uniform(1e7, 1e9, len(close)),
    )


def _linear_bars(n_symbols: int = 3, n_days: int = 260) -> pd.DataFrame:
    """价格写死的线性行情：公式断言要能手工算出期望值。

    第 3 个交易日的 `pct_chg` 刻意整行缺失（实测库里有两个这样的日期）：
    `pivot_table` 会**丢掉整行全 NaN 的日期**，所以 pct 派生的因子必须自己
    对齐回 close 的 shape，否则行列错位（下游 composite / compute_buckets 会炸）。
    """
    dates = pd.bdate_range("2023-01-02", periods=n_days).strftime("%Y-%m-%d")
    frames = []
    for i in range(n_symbols):
        close = 10.0 + 0.1 * (i + 1) * np.arange(n_days)
        pct = np.zeros(n_days)
        pct[1:] = np.diff(close) / close[:-1]
        pct[2] = np.nan
        frames.append(pd.DataFrame({
            "symbol": f"{i:06d}", "date": dates, "open": close, "high": close * 1.02,
            "low": close * 0.98, "close": close, "volume": 1e6, "amount": 1e6 + i,
            "pct_chg": pct, "is_st": 0, "tradestatus": 1,
        }))
    return pd.concat(frames, ignore_index=True)


def _ctx(path):
    with dbmod.session(path) as conn:
        return bt.load_context(conn, None, with_ohlcv=True)


# ── 公式 ────────────────────────────────────────────────────────────
def test_factor_formulas(tmp_path):
    """价格写死时逐条核对公式（不是只断言「非空」）。"""
    path = _db(tmp_path, "f.db", _linear_bars())
    ctx = _ctx(path)
    close = ctx["close"]
    with dbmod.session(path) as conn:
        got = dict(fac.compute_factors(ctx, conn, period=60))

    assert len(got) == 13, "因子清单丢了或多了"
    d, s = close.index[-1], "000000"
    col = close[s]
    # 每个因子都必须是 close 的 shape：pct 有一天整行缺失（pivot_table 会丢行），
    # 不对齐就是静默错位，下游 composite/compute_buckets 直接炸
    for name, frame in got.items():
        assert frame.shape == close.shape, f"{name} 与 close 行列数不一致"
    assert got["vol20"].loc[d, s] == pytest.approx(
        ctx["pct"][s].iloc[-20:].std(), rel=1e-9), "波动率算错日期"
    assert got["rev5"].loc[d, s] == pytest.approx(col.iloc[-1] / col.iloc[-6] - 1)
    assert got["rev20"].loc[d, s] == pytest.approx(col.iloc[-1] / col.iloc[-21] - 1)
    assert got["mom"].loc[d, s] == pytest.approx(col.iloc[-1] / col.iloc[-61] - 1)
    assert got["bias20"].loc[d, s] == pytest.approx(col.iloc[-1] / col.iloc[-20:].mean() - 1)
    assert got["near_high250"].loc[d, s] == pytest.approx(
        col.iloc[-1] / ctx["high"][s].iloc[-250:].max())
    assert got["age"].loc[d, s] == 260, "上市交易日数应等于矩阵长度"


# ── 标准化 ──────────────────────────────────────────────────────────
def test_mad_winsor_caps_outlier():
    """MAD 去极值：不经截断时跑出 3σ 之外的极端值必须被压回来。"""
    n, days = 40, 6
    dates = pd.bdate_range("2024-01-01", periods=days)
    cols = [f"{i:06d}" for i in range(n)]
    raw = pd.DataFrame(
        np.tile(np.arange(1.0, n + 1), (days, 1)), index=dates, columns=cols)
    raw.iloc[2:, 0] = 1e6

    row = raw.iloc[2]
    naive = (row - row.mean()) / row.std()
    assert abs(naive.iloc[0]) > fac.WINSOR_SIGMA, "构造的数据没有真的越界，用例失效"

    z = fac.standardize_factors(iter([("x", raw)]))["x"].loc[dates[2]]
    assert z.notna().all(), "截断后不该整行失效"
    assert abs(z.iloc[0]) <= fac.WINSOR_SIGMA + 1.0


def test_zero_mad_row_becomes_nan():
    """超过半数同值的行没有区分度 → 整行 NaN（不参与合成），不是压成 0。"""
    dates = pd.bdate_range("2024-01-01", periods=2)
    cols = [f"{i:06d}" for i in range(5)]
    raw = pd.DataFrame(0.0, index=dates, columns=cols)
    raw.loc[dates[1], cols[0]] = 1.0            # 仍是「多数同值」→ mad == 0
    z = fac.standardize_factors(iter([("t", raw)]))["t"]
    assert z.loc[dates[0]].isna().all()


# ── 合成 ────────────────────────────────────────────────────────────
def test_missing_factor_renormalizes():
    """只有 5/13 个因子有值时，合成分 = 那 5 个的等权平均（不是除以 13）。"""
    dates = pd.bdate_range("2024-01-01", periods=3)
    names = [f"f{i}" for i in range(13)]
    z, ic = {}, {}
    for i, nm in enumerate(names):
        df = pd.DataFrame(1.0, index=dates, columns=["A", "B"])
        df["B"] = -1.0
        if i >= 5:
            df["A"] = np.nan                 # A 只剩 5 个因子
        z[nm] = df
        ic[nm] = pd.Series(1.0, index=dates)  # 符号全为正 → 方向不翻转
    score = fac.composite(z, ic)
    assert score.loc[dates[0], "A"] == pytest.approx(1.0)
    assert score.loc[dates[0], "B"] == pytest.approx(-1.0)
    assert abs(score.loc[dates[0], "A"] - 5 / 13) > 0.5, "缺失被当成 0 了"


def test_insufficient_train_window_drops_factor():
    """训练窗内有效 IC 少于 MIN_TRAIN_IC 时符号为 NaN → 该因子不进入分母。"""
    dates = pd.bdate_range("2024-01-01", periods=60)
    good_ic = pd.Series(1.0, index=dates)
    bad_ic = pd.Series(np.nan, index=dates)
    bad_ic.iloc[-5:] = 1.0                   # 只有 5 个有效 IC < MIN_TRAIN_IC=20
    ic_roll = {
        "good": fac.rolling_ic(good_ic, hold=1),
        "bad": fac.rolling_ic(bad_ic, hold=1),
    }
    z = {
        "good": pd.DataFrame(1.0, index=dates, columns=["A"]),
        "bad": pd.DataFrame(10.0, index=dates, columns=["A"]),
    }
    score = fac.composite(z, ic_roll)
    d = dates[40]
    assert ic_roll["good"].loc[d] == pytest.approx(1.0)
    assert pd.isna(ic_roll["bad"].loc[d]), "训练窗不足时应给不出符号"
    # 只有 good 参与：(1.0 × 1) / 1，而不是 (1 + 10) / 2
    assert score.loc[d, "A"] == pytest.approx(1.0)


def test_rolling_ic_window_ends_before_hold():
    """训练窗右端 = t - hold：窗口内不得出现「需要 t 之后才知道」的 IC。"""
    dates = pd.bdate_range("2024-01-01", periods=10)
    ic = pd.Series(np.arange(10.0), index=dates)      # IC 值 = 日期序号，便于反推窗口
    got = fac.rolling_ic(ic, hold=2, train_days=3, min_periods=3)
    # t = dates[6] → 窗口 = dates[6-2-3+1 .. 6-2] = dates[2..4] → IC 2,3,4 → 均值 3
    assert got.loc[dates[6]] == pytest.approx(3.0)
    assert pd.isna(got.loc[dates[3]]), "窗口还没攒够就不该给出符号"


# ── 无未来函数（核心）────────────────────────────────────────────────
def _score_and_ic(ctx, conn, period=60, hold=20):
    """算出 (合成分, 各因子滚动 IC)——两条都要能单独断言。"""
    z = fac.standardize_factors(fac.compute_factors(ctx, conn, period=period), st_flags=None)
    fwd = fac._forward_return(ctx["close"], hold)
    ic_roll = {k: fac.rolling_ic(fac.ic_series(zk, fwd), hold=hold) for k, zk in z.items()}
    return fac.to_percentile(fac.composite(z, ic_roll)), ic_roll


def test_no_lookahead_in_composite(tmp_path):
    """**核心断言**：扰动 cutoff 之后的行情，cutoff 当日的合成分必须逐票相同。

    同时覆盖两处：因子值本身不能用到未来价；滚动训练窗不能把 `t-hold` 之后
    的数据算进来（少了 `rolling_ic` 里的 `shift(hold)`，这条必然失败）。
    """
    df = _bars(n_symbols=20, n_days=300, seed=77)
    cutoff = sorted(df["date"].unique())[200]
    # 两个库分别装原始与扰动后的行情：ctx 有进程内缓存，同一路径会拿到旧矩阵
    a = _db(tmp_path, "a.db", df)
    b = _db(tmp_path, "b.db", perturb_after(df, cutoff))

    with dbmod.session(a) as ca, dbmod.session(b) as cb:
        ctxa = bt.load_context(ca, None, with_ohlcv=True)
        ctxb = bt.load_context(cb, None, with_ohlcv=True)
        score_a, ic_a = _score_and_ic(ctxa, ca)
        score_b, ic_b = _score_and_ic(ctxb, cb)

    pd.testing.assert_series_equal(score_a.loc[cutoff], score_b.loc[cutoff])
    for k in ic_a:
        pd.testing.assert_series_equal(ic_a[k].loc[:cutoff], ic_b[k].loc[:cutoff])
    assert score_a.loc[cutoff].notna().any(), "合成分整行全空，断言等于没测"


def test_ic_series_is_spearman():
    """IC 是秩相关：单调但非线性的关系给出 1，而 Pearson 明显小于 1。"""
    dates = pd.bdate_range("2024-01-01", periods=1)
    cols = [f"{i:06d}" for i in range(30)]
    x = pd.DataFrame([np.arange(30.0)], index=dates, columns=cols)
    fwd = pd.DataFrame([np.arange(30.0) ** 3], index=dates, columns=cols)
    ic = fac.ic_series(x, fwd)
    assert ic.iloc[0] == pytest.approx(1.0)
    pear = x.loc[dates[0]].corr(fwd.loc[dates[0]])
    assert pear < 0.97, f"这组数据用 Pearson 也够接近 1（{pear:.3f}），证明不了秩相关"


def test_factor_corr_matrix_shape_and_diagonal():
    dates = pd.bdate_range("2024-01-01", periods=4)
    cols = [f"{i:06d}" for i in range(10)]
    z = {
        f"f{i}": pd.DataFrame(
            np.random.default_rng(i).normal(size=(len(dates), len(cols))),
            index=dates, columns=cols,
        )
        for i in range(13)
    }
    corr = fac.factor_corr(z)
    names, m = corr["names"], corr["matrix"]
    assert len(names) == 13
    assert len(m) == 13 and all(len(row) == 13 for row in m)
    assert all(m[i][i] == 1.0 for i in range(13))
    for i in range(13):
        for j in range(13):
            assert m[i][j] == m[j][i], "相关矩阵必须对称"
