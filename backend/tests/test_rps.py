"""RPS 数学正确性测试——全部离线，不触网。

最关键的是 `test_rps_no_lookahead`：把信号日之后的行情全部打乱，
信号日的 RPS 必须**逐值不变**。这从结构上锁死了未来函数。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from rpsboard import config
from rpsboard.analytics import rps
from rpsboard.analytics.matrix import (
    build_close_matrix,
    build_st_flags,
    build_tradable_matrix,
)

from .fixtures import make_long, perturb_after

# 周期真源在 config.PERIODS（本文件不再另抄一份清单）
PERIODS = config.PERIODS


def build_frames(df: pd.DataFrame, **kw):
    close = build_close_matrix(df)
    return close, rps.compute_rps_frames(
        close, build_tradable_matrix(df), build_st_flags(df), **kw
    )


# ── 口径 ────────────────────────────────────────────────────────────
def test_rps_top_is_exactly_100():
    """每个有效日期的榜首 RPS 必须是 100（rank(pct) 口径）。"""
    df = make_long(n_symbols=40, n_days=300)
    _, frames = build_frames(df)
    for n in PERIODS:
        rps_n = frames[f"rps{n}"].dropna(how="all")
        assert not rps_n.empty
        assert np.allclose(rps_n.max(axis=1).values, 100.0), f"period {n} 榜首不是 100"


def test_rps_value_range():
    df = make_long(n_symbols=40, n_days=300)
    _, frames = build_frames(df)
    for n in PERIODS:
        vals = frames[f"rps{n}"].stack().dropna()
        assert (vals > 0).all() and (vals <= 100).all(), f"period {n} 值域越界"


def test_rps_monotonic_with_return():
    """涨幅越高 → RPS 越高（横截面排序一致性）。

    用「秩的皮尔逊相关」代替 spearman —— 后者需要 scipy，而本项目刻意不引入它。
    """
    df = make_long(n_symbols=40, n_days=300)
    _, frames = build_frames(df)
    for n in PERIODS:
        ret, r = frames[f"ret{n}"], frames[f"rps{n}"]
        for date in ret.index[-5:]:
            pair = pd.DataFrame({"ret": ret.loc[date], "rps": r.loc[date]}).dropna()
            if len(pair) < 5:
                continue
            # 对秩做皮尔逊相关 == spearman
            corr = pair["ret"].rank().corr(pair["rps"].rank())
            assert corr > 0.99, f"period {n} @{date} 秩相关 {corr:.4f} < 0.99"


def test_rps_handcomputed_small_fixture():
    """小夹具逐值手算核对（5 只股票、4 天、period=2）。

    刻意让 000001 与 000003 的收益**并列**，用来锁定 pandas 的平均名次口径：
    并列两名各得 (1.5)/5 = 30% → RPS 90，而不是 80 或 100。
    """
    dates = pd.bdate_range("2024-01-01", periods=4)
    closes = {
        "000001": [10.0, 11.0, 12.0, 13.0],   # ret2 @idx2 = 12/10-1 =  0.20（并列最高）
        "000002": [20.0, 19.0, 21.0, 20.0],   # ret2 @idx2 = 21/20-1 =  0.05
        "000003": [5.0, 5.5, 6.0, 6.0],       # ret2 @idx2 =  6/5 -1  =  0.20（并列最高）
        "000004": [8.0, 8.0, 8.0, 8.0],       # ret2 @idx2 =  0.00
        "000005": [50.0, 40.0, 30.0, 30.0],   # ret2 @idx2 = 30/50-1 = -0.40
    }
    rows = [
        {"symbol": s, "date": d, "close": c,
         "pct_chg": 0.0, "tradestatus": 1, "is_st": 0}
        for s, arr in closes.items() for d, c in zip(dates, arr)
    ]
    df = pd.DataFrame(rows)

    close = build_close_matrix(df)
    ret = close / close.shift(2) - 1.0
    rk = (ret.rank(axis=1, pct=True) * 100.0).round(2)

    day3 = dates[2]   # build_close_matrix 会把 date 转成 Timestamp
    # 升序名次（并列取平均）：-0.40→1, 0.00→2, 0.05→3, 0.20→4.5, 0.20→4.5
    # rank(pct) = 名次 / 5
    assert rk.loc[day3, "000005"] == pytest.approx(1 / 5 * 100, abs=0.01)   # 20
    assert rk.loc[day3, "000004"] == pytest.approx(2 / 5 * 100, abs=0.01)   # 40
    assert rk.loc[day3, "000002"] == pytest.approx(3 / 5 * 100, abs=0.01)   # 60
    assert rk.loc[day3, "000001"] == pytest.approx(4.5 / 5 * 100, abs=0.01) # 90（并列）
    assert rk.loc[day3, "000003"] == pytest.approx(4.5 / 5 * 100, abs=0.01) # 90（并列）

    # 区间收益本身也要对上
    assert ret.loc[day3, "000001"] == pytest.approx(0.20)
    assert ret.loc[day3, "000005"] == pytest.approx(-0.40)


# ── 无未来函数（最重要的硬断言）────────────────────────────────────
def test_rps_no_lookahead():
    """扰动 cutoff 之后的行情，cutoff 当日的 RPS 必须逐值不变。"""
    base = make_long(n_symbols=50, n_days=300, seed=11)
    cutoff = sorted(base["date"].unique())[200]

    _, frames_a = build_frames(base)
    perturbed = perturb_after(base, cutoff)
    _, frames_b = build_frames(perturbed)

    for n in PERIODS:
        a = frames_a[f"rps{n}"].loc[cutoff]
        b = frames_b[f"rps{n}"].loc[cutoff]
        pd.testing.assert_series_equal(
            a, b, check_names=False,
            obj=f"period {n} 在 {cutoff} 的 RPS 因未来数据变化而改变",
        )


def test_ret_no_lookahead():
    """区间收益本身也不该受未来数据影响。"""
    base = make_long(n_symbols=30, n_days=200, seed=13)
    cutoff = sorted(base["date"].unique())[150]
    _, fa = build_frames(base)
    _, fb = build_frames(perturb_after(base, cutoff))
    for n in PERIODS:
        pd.testing.assert_series_equal(
            fa[f"ret{n}"].loc[cutoff], fb[f"ret{n}"].loc[cutoff], check_names=False
        )


# ── 新股 / 停牌 / ST ───────────────────────────────────────────────
def test_new_listing_excluded():
    """上市不足 N 日的股票，其 RPS 必须是 NaN（等价于「剔除新股」）。

    取值日期要用**该股票自己的最后一根 K 线**，而不是全市场最后一天——
    新股在上市之前根本不存在，那天它不该出现在排名里。
    """
    n_days = 120
    symbols = [f"{i:06d}" for i in range(10)]
    late = symbols[5]                     # 让它只有 30 根 K 线
    df = make_long(n_symbols=10, n_days=n_days, listed_bars={late: 30})
    _, frames = build_frames(df)

    own_last = df.loc[df["symbol"] == late, "date"].max()
    assert pd.isna(frames["rps60"].loc[own_last, late]), "上市不足 60 日不应有 RPS60"
    assert pd.notna(frames["rps20"].loc[own_last, late]), "上市 30 日应有 RPS20"

    # 在新股上市之前的那天，它不该出现在任何排名里
    market_last = df["date"].max()
    assert pd.isna(frames["rps20"].loc[market_last, late]), "上市前不应参与排名"


def test_resumption_day_participates_missing_bar_does_not():
    """复牌当天参与排名；真正没有 bar 的日子才被剔除。

    `tradestatus=0` 的语义是「这根 bar 之前缺了交易日（停牌复牌当天）」，
    复牌日本身是真实成交日（有量、有涨跌），把它剔出去会漏掉复牌补涨的龙头。
    停牌期间没有 bar，那一格在 pivot 后是 NaN → 不可交易，自然不参与。
    """
    n_days = 30
    df = make_long(n_symbols=5, n_days=n_days, seed=5)
    last = df["date"].max()
    df.loc[(df["date"] == last) & (df["symbol"] == "000003"), "tradestatus"] = 0

    _, frames = build_frames(df)
    assert pd.notna(frames["rps20"].loc[last, "000003"]), "复牌当天应参与排名"

    # 停牌期间（没有 bar）不参与：把 000004 最后一根 bar 删掉模拟停牌最后一日
    stopped = df[~((df["date"] == last) & (df["symbol"] == "000004"))]
    _, frames2 = build_frames(stopped)
    assert pd.isna(frames2["rps20"].loc[last, "000004"]), "没有 bar 的日子不应参与排名"


def test_exclude_st_flag():
    """exclude_st=True 时 ST 股不参与排名；False 时参与。"""
    df = make_long(n_symbols=10, n_days=80, seed=9)
    last = df["date"].max()
    df.loc[(df["date"] == last) & (df["symbol"] == "000002"), "is_st"] = 1

    close = build_close_matrix(df)
    tradable = build_tradable_matrix(df)
    st = build_st_flags(df)

    excl = rps.compute_rps_frames(close, tradable, st, exclude_st=True)
    incl = rps.compute_rps_frames(close, tradable, st, exclude_st=False)
    assert pd.isna(excl["rps20"].loc[last, "000002"])
    assert pd.notna(incl["rps20"].loc[last, "000002"])


# ── 幂等 ────────────────────────────────────────────────────────────
def test_rps_is_deterministic():
    """同一输入算两次必须完全一致（幂等）。"""
    df = make_long(n_symbols=25, n_days=200, seed=3)
    _, a = build_frames(df)
    _, b = build_frames(df)
    for n in PERIODS:
        pd.testing.assert_frame_equal(a[f"rps{n}"], b[f"rps{n}"])


def test_empty_input_returns_empty():
    empty = pd.DataFrame(columns=["symbol", "date", "close", "pct_chg",
                                  "tradestatus", "is_st"])
    close = build_close_matrix(empty)
    assert close.empty
    assert rps.compute_rps_frames(close) == {}


def test_suspect_gaps_finds_interior_hole():
    """中间空洞（daily_bar 有、rps_daily 没有）必须被报出来。

    区间前那批「前置 K 线不足」的日期不算缺口，所以判据要从 rps_daily 首日之后算。
    """
    import sqlite3

    from rpsboard import db as dbmod

    df = make_long(n_symbols=30, n_days=280, seed=7)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(dbmod.SCHEMA)
    df[["symbol", "date", "close", "pct_chg", "tradestatus", "is_st"]].to_sql(
        "daily_bar", conn, if_exists="append", index=False)

    dates = sorted(df["date"].unique())
    hole = dates[40]
    conn.executemany(
        "INSERT INTO rps_daily(date, symbol, rps60) VALUES (?,?,?)",
        [(d, "000001", 50.0) for d in dates[20:] if d != hole],
    )
    conn.commit()

    gaps = rps.suspect_gaps(conn)
    assert gaps == [hole], f"应只报中间那一天，实际 {gaps[:5]}"
    conn.close()


def test_short_history_stock_enters_board_via_rps15(tmp_path):
    """上市只有 16 个交易日的股票：以前四个周期全 NaN、被 dropna 丢弃，
    现在 rps15 有值 → 首次进榜（新增 15 日周期带来的可见副作用）。
    """
    import sqlite3

    from rpsboard import db as dbmod

    df = make_long(n_symbols=10, n_days=120, seed=9, listed_bars={"000009": 16})
    conn = sqlite3.connect(str(tmp_path / "mat.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(dbmod.SCHEMA)
    try:
        close = build_close_matrix(df)
        ind = pd.Series({s: "行业A" for s in sorted(df["symbol"].unique())})
        rps.materialize(
            conn, close, build_tradable_matrix(df), build_st_flags(df), ind, None
        )
        row = conn.execute(
            "SELECT rps15, rps20 FROM rps_daily WHERE symbol = '000009' "
            "ORDER BY date DESC LIMIT 1"
        ).fetchone()
        assert row is not None, "16 根 K 线的股票应当进榜"
        assert row["rps15"] is not None and not pd.isna(row["rps15"])
        assert row["rps20"] is None, "20 日周期的前置 K 线还不够，不该有值"
    finally:
        conn.close()


def test_non_positive_close_excluded_from_returns():
    """后复权价穿零（close<=0）不得进入收益率。

    负分母会让比值符号翻转、0 分母得 ±inf，而 rank() 会把 +inf 当最大值排到榜首
    （实测 600076 因此拿到全市场 rps60=100）。
    """
    df = make_long(n_symbols=6, n_days=80, seed=11)
    last = df["date"].max()
    df.loc[(df["date"] == last) & (df["symbol"] == "000002"), "close"] = -1.0
    df.loc[(df["date"] == last) & (df["symbol"] == "000003"), "close"] = 0.0

    _, frames = build_frames(df)
    for n in PERIODS:
        assert pd.isna(frames[f"ret{n}"].loc[last, "000002"]), f"负价不该有 ret{n}"
        assert pd.isna(frames[f"ret{n}"].loc[last, "000003"]), f"零价不该有 ret{n}"
    assert frames["rps20"].loc[last].dropna().max() == 100.0, "其余股票排名不受影响"


def test_thin_cross_section_dates_dropped(tmp_path):
    """横截面只有 1 只股票的日期不落库。

    单只股票时 rank(pct) 恒等于 100，那不是「强度」而是没有可比对象
    （实测 2023-03-06 起约 23 个交易日全市场只有 1 只股票有 RPS）。
    """
    import sqlite3

    from rpsboard import db as dbmod

    # 000001 只有前 30 根 K 线 → 后段横截面只剩 000000 一只
    df = make_long(n_symbols=2, n_days=60, seed=13, listed_bars={"000001": 30})
    conn = sqlite3.connect(str(tmp_path / "thin.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(dbmod.SCHEMA)
    try:
        ind = pd.Series({s: "行业A" for s in sorted(df["symbol"].unique())})
        rps.materialize(
            conn, build_close_matrix(df), build_tradable_matrix(df),
            build_st_flags(df), ind, None,
        )
        thin = conn.execute(
            "SELECT date FROM rps_daily GROUP BY date HAVING COUNT(*) < 2"
        ).fetchall()
        assert [r["date"] for r in thin] == [], "单成员日期不该落库"

        tail = sorted(df["date"].unique())[35:]
        n_tail = conn.execute(
            "SELECT COUNT(*) AS n FROM rps_daily WHERE date >= ?", (tail[0],)
        ).fetchone()["n"]
        assert n_tail == 0, "只剩一只股票有行情的那段不该有 RPS 行"
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM rps_daily"
        ).fetchone()["n"] > 0, "双股票那段仍应有行"
    finally:
        conn.close()


def test_tiny_price_endpoints_excluded(monkeypatch):
    """端点价 < 门槛（默认 1 元）时该区间收益不算 —— 这是两处失真的共同来源：

    - 分母极小 → 正常的原始价涨幅被放大成几百个百分点（600076 的 ret60 = +998%，
      当天原始价只涨了约 8%）；
    - 分子极小 → 比值接近 −100%，同样不是「区间涨幅」。

    区间收益只取决于**两个端点**，所以按端点剔除，而不是「跨过异常日整段作废」。
    """
    from rpsboard import config as cfg
    from rpsboard.analytics.backtest import _forward_return

    monkeypatch.setattr(cfg, "ANOMALY_MIN_PRICE", 1.0)
    dates = pd.bdate_range("2026-01-05", periods=8)
    prices = {
        "000001": [10.0] * 8,                                   # 正常
        "000002": [5.0, 5.0, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],     # 第 3 根起穿到 1 元以下
        "000003": [1.0, 1.2, 1.5, 1.1, 1.3, 1.4, 1.6, 1.8],     # 刚好 1 元 → 仍在门槛内
    }
    rows = [{"symbol": s, "date": d, "close": c, "pct_chg": 0.0,
             "tradestatus": 1, "is_st": 0}
            for s, arr in prices.items() for d, c in zip(dates, arr)]
    df = pd.DataFrame(rows)

    close = build_close_matrix(df)
    assert pd.isna(close.loc[dates[2], "000002"]), "极小价端点应被置空"
    assert pd.notna(close.loc[dates[0], "000003"]), "1 元整不该被剔除"

    frames = rps.compute_rps_frames(
        close, build_tradable_matrix(df), build_st_flags(df),
        exclude_st=False, periods=(2,),
    )
    ret2 = frames["ret2"]
    # 000002：第 3 根起分子 <1 元、第 7 根的分母也 <1 元 → 全部不可算
    for i in range(2, 8):
        assert pd.isna(ret2.loc[dates[i], "000002"]), f"{dates[i]} 端点异常却没置空"
    # 000003：两端都 ≥1 元 → 正常算得出来（证明没有整体误伤）
    assert ret2.loc[dates[2], "000003"] == pytest.approx(1.5 / 1.0 - 1.0)
    assert ret2.loc[dates[-1], "000003"] == pytest.approx(1.8 / 1.4 - 1.0)

    # 回测的前瞻收益用同一个 close 矩阵 → 同一套端点规则
    fwd = _forward_return(close, 2)
    assert pd.isna(fwd.loc[dates[2], "000002"]), "卖出端点也是极小价 → 不可算"
    assert pd.isna(fwd.loc[dates[0], "000002"]), "买入端点极小 → 不可算"
    assert fwd.loc[dates[0], "000003"] == pytest.approx(1.5 / 1.0 - 1.0)


def test_anomaly_floor_zero_keeps_low_prices(monkeypatch):
    """门槛设为 0 时只剔除非正价（旧行为），低价股照常参与。"""
    from rpsboard import config as cfg

    monkeypatch.setattr(cfg, "ANOMALY_MIN_PRICE", 0.0)
    dates = pd.bdate_range("2026-01-05", periods=4)
    rows = [{"symbol": "000002", "date": d, "close": c, "pct_chg": 0.0,
             "tradestatus": 1, "is_st": 0}
            for d, c in zip(dates, [0.5, -0.1, 0.6, 0.7])]
    close = build_close_matrix(pd.DataFrame(rows))
    assert pd.notna(close.loc[dates[0], "000002"]), "门槛 0 时 0.5 元应保留"
    assert pd.isna(close.loc[dates[1], "000002"]), "非正价永远剔除"


def test_negative_offset_restored_before_returns():
    """负偏移股票必须还原成原始价比例后再算区间收益。

    后复权价 = 原始价 × scale + offset，offset < 0 时原始价贴着变换零点，比值会被
    放大到失真 —— 实测 5 只（000793/600076/600136/600180/600734）每周期有
    900~1,270 行偏差 >20pp（最大 1,640pp），且它们的 RPS 能到 100，
    回测会把「原始价 +10%」当成「+85%」买进去。

    构造：零点 9 元（offset=-9），原始价 10.2 → 11.22 走 +10%，
    对应后复权 1.2 → 2.22（两个端点都 ≥1 元，端点剔除挡不住）。
    """
    n = 16
    raw = [10.2 * (1 + 0.10 / 15) ** i for i in range(n)]
    df = pd.DataFrame([
        {"symbol": "600734", "date": f"2026-01-{i + 1:02d}", "close": raw[i] - 9.0,
         "pct_chg": 0.0, "tradestatus": 1, "is_st": 0}
        for i in range(n)
    ])

    restored = rps.compute_rps_frames(
        build_close_matrix(df, {"600734": 9.0}), periods=(15,))
    distorted = rps.compute_rps_frames(build_close_matrix(df), periods=(15,))

    expect_true = raw[-1] / raw[0] - 1            # 原始价口径的真实涨幅
    expect_bad = (raw[-1] - 9.0) / (raw[0] - 9.0) - 1   # 后复权比值（贴着零点）

    assert restored["ret15"].iloc[-1, 0] == pytest.approx(expect_true, abs=1e-12), (
        "还原后 = 真实涨幅"
    )
    assert distorted["ret15"].iloc[-1, 0] == pytest.approx(expect_bad, abs=1e-12)
    assert expect_bad > 5 * expect_true, "这正是生产库里发生的放大"
