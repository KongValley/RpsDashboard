"""回测测试：前瞻对齐、涨跌停过滤、空池、指标、无未来函数。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from rpsboard import config
from rpsboard.analytics import backtest as bt
from rpsboard.analytics.matrix import build_close_matrix

from .fixtures import make_long, perturb_after


def make_close(n_symbols=40, n_days=300, seed=61):
    df = make_long(n_symbols=n_symbols, n_days=n_days, seed=seed)
    return df, build_close_matrix(df)


# ── 前瞻收益对齐 ────────────────────────────────────────────────────
def test_forward_return_alignment():
    """T 日的 fwd_ret(hold=2) = close[T+2]/close[T] - 1，不存在 off-by-one。"""
    dates = pd.bdate_range("2024-01-01", periods=5)
    close = pd.DataFrame(
        [[10.0], [11.0], [12.0], [13.0], [14.0]],
        index=dates, columns=["000001"],
    )
    fwd = bt._forward_return(close, hold=2)
    assert fwd.loc[dates[0], "000001"] == pytest.approx(12.0 / 10.0 - 1)
    assert fwd.loc[dates[1], "000001"] == pytest.approx(13.0 / 11.0 - 1)
    assert pd.isna(fwd.loc[dates[3], "000001"])   # 末尾没有足够未来数据
    assert pd.isna(fwd.loc[dates[4], "000001"])


def test_signal_stability_under_future_perturbation():
    """**回测的无未来函数核心断言**。

    前瞻收益本来就依赖未来价格（这是回测的意义所在），所以正确的断言对象
    不是「收益不变」，而是「**信号不变**」：

        扰动 cutoff 之后的行情 → cutoff 当日选出的股票集合必须逐只相同。

    如果信号用到了未来数据（例如错写成 close.shift(+N) 或用了未来复权因子），
    这个断言必然失败。
    """
    from rpsboard.analytics import rps as rps_mod

    df = make_long(n_symbols=30, n_days=260, seed=64)
    cutoff = sorted(df["date"].unique())[200]

    close_a = build_close_matrix(df)
    frames_a = rps_mod.compute_rps_frames(close_a)
    mask_a = (frames_a["rps60"] >= 80).loc[cutoff].dropna()
    sel_a = set(mask_a[mask_a].index)

    close_b = build_close_matrix(perturb_after(df, cutoff))
    frames_b = rps_mod.compute_rps_frames(close_b)
    mask_b = (frames_b["rps60"] >= 80).loc[cutoff].dropna()
    sel_b = set(mask_b[mask_b].index)

    assert sel_a == sel_b, "扰动未来行情改变了信号日的选股结果 —— 存在未来函数"


def test_rps_at_signal_date_unchanged():
    """信号日的 RPS 数值本身也不受未来数据影响。"""
    from rpsboard.analytics import rps as rps_mod

    df = make_long(n_symbols=20, n_days=150, seed=65)
    cutoff = sorted(df["date"].unique())[100]
    a = rps_mod.compute_rps_frames(build_close_matrix(df))["rps60"].loc[cutoff]
    b = rps_mod.compute_rps_frames(
        build_close_matrix(perturb_after(df, cutoff))
    )["rps60"].loc[cutoff]
    pd.testing.assert_series_equal(a, b, check_names=False)


def test_exchange_detection_bj():
    """920xxx 必须判为北交所，不能像某些参考实现那样归到上海。"""
    from rpsboard import config
    assert config.exchange_of("600000") == "sh"
    assert config.exchange_of("000001") == "sz"
    assert config.exchange_of("300750") == "sz"
    assert config.exchange_of("688981") == "sh"
    assert config.exchange_of("920001") == "bj"   # 北交所新代码段
    assert config.exchange_of("830799") == "bj"
    assert config.exchange_of("873122") == "bj"


def test_limit_up_filter_excludes():
    """涨停判定（收益率比法）。

    早期实现是 `pct >= config.limit_threshold(...)`，而 limit_threshold 是
    拍出来的**百分数** 9.8 / 19.8，pct_chg 却是**小数** —— 量纲错了两个数量级，
    这个过滤实测从未命中过任何一只股票。现在改走 analytics.limits，
    按每只票当日真实的 limit_ratio 判定。
    """
    from rpsboard.analytics.limits import limit_mask

    # 收盘价构造：600000 主板 10%、000001 也按 10%（当日比例给 5%）、
    # 300750 创业板 20%
    idx = pd.bdate_range("2024-01-02", periods=3)
    close = pd.DataFrame({
        "600000": [10.0, 11.0, 11.1],    # +10%, +0.9%
        "000001": [10.0, 10.5, 10.4],    # +5%,  -1%
        "300750": [10.0, 12.0, 11.8],    # +20%, -1.7%
    }, index=idx)
    ratios = pd.DataFrame({
        "600000": [10.0, 10.0, 10.0],
        "000001": [10.0, 10.0, 10.0],
        "300750": [20.0, 20.0, 20.0],
    }, index=idx)
    up, dn = limit_mask(close, ratios)

    assert bool(up.iloc[1]["600000"])     # 主板 +10% → 涨停
    assert not bool(up.iloc[1]["000001"])  # +5% → 未涨停
    assert bool(up.iloc[1]["300750"])     # 创业板 +20% → 涨停
    assert not bool(up.iloc[0].any())     # 首日无前收盘，全部不算
    assert not bool(up.iloc[2].any())     # 次日普跌
    assert not bool(dn.iloc[2].any())


# ── 指标 ────────────────────────────────────────────────────────────
def test_metrics_handcomputed():
    eq = pd.Series([1.0, 1.1, 1.05, 1.2, 1.0], dtype="float64")
    m = bt.compute_metrics(eq, n_days_per_point=20)
    assert m["total_return"] == pytest.approx(0.0, abs=1e-6)     # 1.0 → 1.0
    assert m["max_drawdown"] == pytest.approx(-0.1666, abs=1e-3) # 1.2 → 1.0
    assert m["days"] == 5
    # 4 个间隔 × 20 交易日 / 252 ≈ 0.317 年
    assert m["years"] == pytest.approx(80 / 252, abs=0.01)


def test_metrics_annualization_is_sane():
    """年化不能爆炸：净值 8 倍、跨 1 年，年化应约 700% 而不是几万。"""
    eq = pd.Series([1.0 + i * 0.5 for i in range(14)])   # 13 个点
    m = bt.compute_metrics(eq, n_days_per_point=20)      # 13*20/252 ≈ 1.03 年
    assert 0.5 < m["annualized"] < 100.0, f"年化异常: {m['annualized']}"
    assert m["years"] == pytest.approx(260 / 252, abs=0.01)


def test_metrics_short_series():
    m = bt.compute_metrics(pd.Series([1.0]))
    assert m["days"] == 1 and m["max_drawdown"] == 0.0
    assert bt.compute_metrics(pd.Series([], dtype="float64"))["days"] == 0


def test_metrics_monotonic_equity():
    eq = pd.Series(np.linspace(1.0, 2.0, 100))
    m = bt.compute_metrics(eq)
    assert m["max_drawdown"] == pytest.approx(0.0, abs=1e-9)
    assert m["annualized"] > 0


# ── 分档 ────────────────────────────────────────────────────────────
def test_buckets_partition_and_boundaries():
    rps = pd.DataFrame(
        [[95.0, 85.0, 75.0, 60.0, 20.0, 100.0]],
        index=pd.date_range("2024-01-01", periods=1),
        columns=[f"{i:06d}" for i in range(6)],
    )
    fwd = pd.DataFrame(
        [[0.10, 0.05, 0.02, 0.00, -0.05, 0.20]],
        index=rps.index, columns=rps.columns,
    )
    out = bt.compute_buckets(rps, fwd)
    by_bucket = {b["bucket"]: b for b in out}

    # 100 应归入 90-100 档（不是 80-90）
    assert by_bucket["90-100"]["samples"] == 2      # 95 和 100
    assert by_bucket["80-90"]["samples"] == 1      # 85
    assert by_bucket["70-80"]["samples"] == 1      # 75
    assert by_bucket["50-70"]["samples"] == 1      # 60
    assert by_bucket["<50"]["samples"] == 1        # 20
    assert by_bucket["90-100"]["avg_forward_return"] == pytest.approx(0.15)
    assert by_bucket["<50"]["win_rate"] == pytest.approx(0.0)


def test_buckets_higher_rps_more_return():
    """构造强动量数据：RPS 高的一档前瞻收益应显著更高。"""
    rng = np.random.default_rng(99)
    n_sym, n_days = 300, 300
    # 前 30 只给强上行趋势，后 270 只走平
    drift = np.concatenate([np.full(30, 0.004), np.full(270, 0.0)])
    paths = np.array([
        10.0 * np.exp(np.cumsum(drift[s] + rng.normal(0, 0.012, n_days)))
        for s in range(n_sym)
    ])
    dates = pd.bdate_range("2024-01-01", periods=n_days)
    cols = [f"{i:06d}" for i in range(n_sym)]
    close = pd.DataFrame(paths.T, index=dates, columns=cols)
    from rpsboard.analytics import rps as rps_mod
    frames = rps_mod.compute_rps_frames(close)
    fwd = bt._forward_return(close, hold=10)
    out = {b["bucket"]: b for b in bt.compute_buckets(frames["rps20"], fwd)}

    assert out["90-100"]["avg_forward_return"] > out["<50"]["avg_forward_return"]


def test_buckets_empty():
    empty = pd.DataFrame()
    assert bt.compute_buckets(empty, empty) == []


# ── 回测主流程（用临时库）──────────────────────────────────────────
def _seed_db(tmp_path, name, n_symbols, n_days, seed):
    """建一个用真实 SCHEMA 的临时库，灌入合成行情**并算出 RPS**。"""
    from rpsboard import db as dbmod
    from rpsboard.analytics import rps as rps_engine
    from rpsboard.analytics.matrix import (
        build_st_flags,
        build_tradable_matrix,
    )

    from .fixtures import make_industry_map_zh

    path = tmp_path / name
    dbmod.init_db(path)
    conn = dbmod.connect(path)
    df = make_long(n_symbols=n_symbols, n_days=n_days, seed=seed)
    df[["symbol", "date", "close", "pct_chg", "tradestatus", "is_st"]].assign(
        volume=1_000_000.0, amount=1e8, open=1.0, high=1.0, low=1.0
    )[["symbol", "date", "open", "high", "low", "close", "volume", "amount",
       "pct_chg", "is_st", "tradestatus"]].to_sql(
        "daily_bar", conn, if_exists="append", index=False)

    close = build_close_matrix(df)
    tradable = build_tradable_matrix(df)
    st = build_st_flags(df)
    ind = make_industry_map_zh(sorted(df["symbol"].unique()), 5)
    rps_engine.materialize(conn, close, tradable, st, ind, None)
    conn.commit()
    return conn


def test_run_backtest_end_to_end(tmp_path):
    conn = _seed_db(tmp_path, "bt.db", n_symbols=60, n_days=320, seed=71)
    res = bt.run_backtest(conn, period=20, threshold=80, hold=20)
    conn.close()

    assert res.warnings, "必须始终携带幸存者偏差警示"
    assert res.equity_curve, "净值曲线不应为空"
    assert len(res.buckets) == 5
    assert "total_return" in res.metrics


def test_daily_curve_connects_rebalance_points(tmp_path):
    """逐日净值必须与调仓点净值对得上，且**回撤不小于**点式口径。

    口径注意：`equity_curve` 的点记在**建仓日**上，值是**该期期末**（t+hold）
    的净值。所以「某点的值」应当等于逐日曲线在**下一个调仓日**（即该期期末）
    的值 —— 这条断言把这个约定钉住，免得后人以为两条线互相矛盾。

    调仓点口径只在每期结束时取一次值，期内的下跌完全看不见，回撤因此被低估；
    逐日口径是把同一段持仓按收盘价逐日盯市。
    """
    conn = _seed_db(tmp_path, "bt_daily.db", n_symbols=60, n_days=320, seed=73)
    res = bt.run_backtest(conn, period=20, threshold=80, hold=20)
    conn.close()

    assert res.daily_curve, "逐日曲线不应为空"
    assert len(res.daily_curve) >= len(res.equity_curve), \
        "逐日点数不该少于调仓点数"
    dates = [p["date"] for p in res.daily_curve]
    assert dates == sorted(dates) and len(set(dates)) == len(dates), "日期必须递增且不重复"

    daily_eq = {p["date"]: p["equity"] for p in res.daily_curve}
    pts = res.equity_curve
    for pt in pts:
        assert pt["date"] in daily_eq, f"建仓日 {pt['date']} 在逐日曲线上缺失"
    for i, pt in enumerate(pts[:-1]):
        end = pts[i + 1]["date"]           # 该期期末 = 下一个调仓日
        assert daily_eq[end] == pytest.approx(pt["equity"], rel=1e-6), \
            f"{pt['date']} 建仓那期的期末净值与逐日曲线不一致"
    tail = pts[-1]
    if tail["date"] != dates[-1]:
        assert daily_eq[dates[-1]] == pytest.approx(tail["equity"], rel=1e-6), \
            "最后一个调仓点到窗口末的净值与逐日曲线不一致"

    mdd_pt = abs(res.metrics["max_drawdown"])
    mdd_daily = abs(res.metrics["max_drawdown_daily"])
    assert mdd_daily >= mdd_pt - 1e-9, \
        f"逐日回撤 {mdd_daily} 不该小于点式回撤 {mdd_pt}"


def test_curve_carries_holdings_list(tmp_path):
    """每个调仓点要带上买入名单（复盘要能回答"那天买了哪几只"）。"""
    conn = _seed_db(tmp_path, "bt_hold.db", n_symbols=60, n_days=320, seed=74)
    res = bt.run_backtest(conn, period=20, threshold=0.0, hold=20, select_top=5)
    conn.close()

    pts = [p for p in res.equity_curve if p.get("holdings")]
    assert pts, "应至少有一个实际持仓的调仓点"
    for pt in pts:
        assert pt["holdings"] == 5
        top = pt["holdings_top"]
        assert len(top) == min(5, bt.HOLDINGS_TOP) == 5
        assert len(set(top)) == len(top), "名单不该有重复"


def test_holdings_list_is_capped(tmp_path):
    """名单上限：等权全市场模式下真实持仓有几千只，全量塞进响应会撑爆 payload。"""
    conn = _seed_db(tmp_path, "bt_cap.db", n_symbols=60, n_days=320, seed=75)
    res = bt.run_backtest(conn, period=20, threshold=0.0, hold=20)
    conn.close()

    for pt in res.equity_curve:
        if pt.get("holdings_top"):
            assert len(pt["holdings_top"]) <= bt.HOLDINGS_TOP
            assert pt["holdings"] >= len(pt["holdings_top"])


def test_equity_curve_does_not_double_count(tmp_path):
    """回归测试：净值曲线不得重复计算持有期收益。

    早期 bug 把 hold+1 行的「未来 N 天累计收益」全部累乘，等于同一段收益
    算了 20 次，出现单期 +667%、年化 61875% 的荒谬数字。
    这里构造一段收益率恒定、可手算的行情，断言总收益量级正确。
    """
    from rpsboard import db as dbmod
    from rpsboard.analytics import rps as rps_engine
    from rpsboard.analytics.matrix import build_st_flags, build_tradable_matrix

    from .fixtures import make_industry_map_zh

    # 40 只股票，每天稳定涨 1%（后复权），持有 20 天 → 单期约 +21.9%
    n_sym, n_days = 40, 200
    dates = pd.bdate_range("2024-01-01", periods=n_days)
    rows = []
    for i in range(n_sym):
        sym = f"{i:06d}"
        closes = [10.0 * (1.01 ** day) for day in range(n_days)]
        for d, c in zip(dates, closes):
            rows.append({"symbol": sym, "date": d.strftime("%Y-%m-%d"),
                         "close": c, "pct_chg": 0.01, "tradestatus": 1, "is_st": 0})
    df = pd.DataFrame(rows)

    path = tmp_path / "steady.db"
    dbmod.init_db(path)
    conn = dbmod.connect(path)
    df[["symbol", "date", "close", "pct_chg", "tradestatus", "is_st"]].assign(
        volume=1e6, amount=1e8, open=1.0, high=1.0, low=1.0
    )[["symbol", "date", "open", "high", "low", "close", "volume", "amount",
       "pct_chg", "is_st", "tradestatus"]].to_sql(
        "daily_bar", conn, if_exists="append", index=False)
    close = build_close_matrix(df)
    rps_engine.materialize(conn, close, build_tradable_matrix(df),
                           build_st_flags(df),
                           make_industry_map_zh(sorted(df["symbol"].unique()), 4), None)
    conn.commit()

    res = bt.run_backtest(conn, period=20, threshold=50, hold=20,
                          exclude_limit_up=False)
    conn.close()

    m = res.metrics
    assert m["total_return"] > 0, "稳定上涨行情应录得正收益"
    # 199 天约 9 个 20 天周期，每期约 +21.9% → 总收益 ~5.6 倍。
    # 若存在重复累乘 bug，总收益会是天文数字；这里给它一个宽松上限。
    assert m["total_return"] < 50, f"总收益异常偏大（疑似重复计算）: {m['total_return']}"
    # 每期净值不能暴涨
    eq = [c["equity"] for c in res.equity_curve]
    jumps = [eq[i] / eq[i - 1] for i in range(1, len(eq))]
    assert max(jumps) < 2.0, f"单期净值跳涨超过 2 倍，疑似重复计算: {max(jumps):.2f}"


def test_run_backtest_empty_pool_not_crash(tmp_path):
    """阈值极高时入池为空，应优雅返回空组合而非崩溃。"""
    conn = _seed_db(tmp_path, "empty.db", n_symbols=20, n_days=120, seed=73)
    res = bt.run_backtest(conn, period=20, threshold=99.99, hold=20)
    conn.close()
    assert res.metrics["days"] >= 0


# ── 多周期共振 ──────────────────────────────────────────────────────
def test_resonance_mask_covers_every_other_period():
    """共振口径 = 除所选周期外的**全部**周期同时达标（15 日也进共振）。"""
    dates = pd.bdate_range("2024-01-01", periods=3)
    syms = ["000001", "000002", "000003"]

    def frame(values):
        return pd.DataFrame([values] * len(dates), index=dates, columns=syms)

    high = 90.0
    frames = {f"rps{p}": frame([high, high, high]) for p in config.PERIODS}
    frames["rps15"] = frame([high, high, 10.0])          # 000003 的 15 日不达标
    frames["rps250"] = frame([10.0, high, high])          # 000001 的 250 日不达标

    by_60 = bt.resonance_mask(frames, period=60, threshold=80)
    assert bool(by_60.loc[dates[0], "000002"]) is True    # 其余周期（含 15、250）全达标
    assert bool(by_60.loc[dates[0], "000003"]) is False, "15 日不达标必须排除该股"
    assert bool(by_60.loc[dates[0], "000001"]) is False, "250 日不达标必须排除该股"

    by_15 = bt.resonance_mask(frames, period=15, threshold=80)
    # 20/60/120/250 必须全达标；起始项固定是最短周期列，所选周期恰为 15 时它也被要求
    assert bool(by_15.loc[dates[0], "000002"]) is True
    assert bool(by_15.loc[dates[0], "000001"]) is False, "250 日不达标必须排除"
    assert bool(by_15.loc[dates[0], "000003"]) is False, "所选周期恰为最短周期时一并要求"


# ── Top-N 选股 ──────────────────────────────────────────────────────
def _one_day(symbols, mask_values, score_values):
    dates = pd.bdate_range("2024-01-01", periods=1)
    mask = pd.DataFrame([mask_values], index=dates, columns=symbols)
    score = pd.DataFrame([score_values], index=dates, columns=symbols, dtype="float64")
    return dates[0], mask, score


def test_select_top_takes_best_n():
    syms = [f"{i:06d}" for i in range(10)]
    t, mask, score = _one_day(syms, [True] * 10, list(range(10)))
    assert list(bt._select_names(mask, score, t, 3)) == ["000009", "000008", "000007"]


def test_select_top_tie_break_is_symbol_order():
    """并列时按 symbol 升序：否则同一份数据两次回测会抽出不同的票，结论不可复现。"""
    syms = [f"{i:06d}" for i in range(10)]
    t, mask, score = _one_day(syms, [True] * 10, [5.0] * 10)
    assert list(bt._select_names(mask, score, t, 3)) == syms[:3]
    # 列顺序被打乱时仍是 symbol 升序（不是「按列顺序取前 N」）
    order = [9, 0, 4, 2, 7, 1, 8, 3, 6, 5]
    shuffled = [syms[i] for i in order]
    assert list(bt._select_names(mask[shuffled], score[shuffled], t, 3)) == syms[:3]


def test_select_top_excludes_nan_score():
    """打分为 NaN 的票排不出名次，不占名额（会被次高者顶替）。"""
    syms = [f"{i:06d}" for i in range(4)]
    t, mask, score = _one_day(syms, [True] * 4, [1.0, float("nan"), 3.0, 2.0])
    assert list(bt._select_names(mask, score, t, 2)) == ["000002", "000003"]


def test_select_top_none_keeps_all_masked():
    syms = [f"{i:06d}" for i in range(6)]
    t, mask, score = _one_day(syms, [True, False, True, True, False, True], list(range(6)))
    assert list(bt._select_names(mask, score, t, None)) == [
        "000000", "000002", "000003", "000005"]
    # 掩码内不足 N 只时不截断：与不传 select_top 的结果逐只相同
    assert list(bt._select_names(mask, score, t, 99)) == list(
        bt._select_names(mask, score, t, None))


def test_signal_composite_runs_and_records_params(tmp_path, monkeypatch):
    """合成分跑通并把口径记进 params（方向取自滚动训练窗，不是全样本拟合）。"""
    from rpsboard.analytics import factors as fac

    monkeypatch.setattr(fac, "MIN_CROSS_IC", 3)   # 合成库只有 40 只票
    conn = _seed_db(tmp_path, "composite.db", n_symbols=40, n_days=320, seed=75)
    res = bt.run_backtest(conn, period=20, threshold=0.0, hold=20,
                          signal="composite", select_top=5)
    conn.close()
    assert res.params["signal"] == "composite"
    assert res.params["select_top"] == 5
    assert res.params["train_days"] == fac.DEFAULT_TRAIN_DAYS
    assert res.equity_curve, "合成分应有净值点（不是整段平线）"
    assert all(c.get("holdings", 0) <= 5 for c in res.equity_curve), "Top-N 不得超出 N"
    assert sum(b["samples"] for b in res.buckets) > 0
    assert "合成分" in res.buckets_note


def test_entry_mask_hard_constraints_still_apply_with_score(tmp_path):
    """给了 score 之后，ST / 涨停 / 停牌三种硬约束仍然剔除对应格子。"""
    conn = _seed_db(tmp_path, "hard.db", n_symbols=40, n_days=200, seed=87)
    dates = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM daily_bar ORDER BY date")]
    syms = [r["symbol"] for r in conn.execute(
        "SELECT DISTINCT symbol FROM daily_bar ORDER BY symbol")]
    last, prev = dates[-1], dates[-2]
    st_sym, up_sym, halt_sym, ok_sym = syms[0], syms[1], syms[2], syms[3]
    prev_close = conn.execute(
        "SELECT close FROM daily_bar WHERE symbol = ? AND date = ?",
        (up_sym, prev)).fetchone()["close"]
    conn.execute("UPDATE daily_bar SET is_st = 1 WHERE symbol = ? AND date = ?",
                 (st_sym, last))
    conn.execute("UPDATE daily_bar SET close = ? WHERE symbol = ? AND date = ?",
                 (prev_close * 1.102, up_sym, last))      # +10.2% → 涨停
    conn.execute("UPDATE daily_bar SET close = 0 WHERE symbol = ? AND date = ?",
                 (halt_sym, last))                        # 无有效收盘 → 不可交易
    conn.executemany(
        "INSERT OR REPLACE INTO price_limit_daily(date, symbol, limit_ratio, rule, status)"
        " VALUES (?,?,10.0,'main','normal')", [(last, s) for s in syms])
    conn.commit()
    bt.clear_context_cache()

    ctx = bt.load_context(conn, None)
    everything = pd.DataFrame(100.0, index=ctx["close"].index, columns=ctx["close"].columns)
    entry = bt.entry_mask(ctx, conn, 20, 0.0, score=everything)
    conn.close()

    assert not bool(entry.mask.loc[last, st_sym]), "ST 仍然被剔除"
    assert not bool(entry.mask.loc[last, up_sym]), "涨停仍然被剔除"
    assert not bool(entry.mask.loc[last, halt_sym]), "停牌/无价仍然被剔除"
    assert bool(entry.mask.loc[last, ok_sym]), "普通格子必须还在，否则上面的断言等于没测"


def test_ctx_key_distinguishes_ohlcv(tmp_path):
    """OHLCV 必须进 ctx 缓存键，否则两次调用会互相拿到对方那份矩阵。"""
    conn = _seed_db(tmp_path, "ctx.db", n_symbols=20, n_days=120, seed=79)
    bt.clear_context_cache()
    with_ohlcv = bt.load_context(conn, None, with_ohlcv=True)
    without = bt.load_context(conn, None)
    again = bt.load_context(conn, None, with_ohlcv=True)
    conn.close()
    assert "high" in with_ohlcv and "amount" in with_ohlcv
    assert "high" not in without and "amount" not in without
    assert "high" in again, "反向顺序（先无后有）也不得串味"


def test_select_top_does_not_change_equity_without_limit(tmp_path):
    """`select_top=None`（默认）必须与改造前的口径逐值相同：净值曲线不动。"""
    conn = _seed_db(tmp_path, "same.db", n_symbols=40, n_days=320, seed=91)
    a = bt.run_backtest(conn, period=20, threshold=80, hold=20)
    b = bt.run_backtest(conn, period=20, threshold=80, hold=20, select_top=None)
    conn.close()
    assert a.params["select_top"] is None and b.params["select_top"] is None
    assert a.metrics["total_return"] == b.metrics["total_return"]
    assert a.equity_curve == b.equity_curve
