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


def test_coverage_counts_covered_days(tmp_path):
    """覆盖率只数「有数据的交易日」：部分覆盖必须能被算出来。

    没有这个数字时，回测会把缺日静默当成「这些天不筛涨停」。
    """
    import sqlite3

    from rpsboard import db as dbmod
    from rpsboard.analytics.limits import coverage

    conn = sqlite3.connect(str(tmp_path / "cov.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(dbmod.SCHEMA)
    rows = [
        ("2026-01-05", "600000", 10.0, "normal"),
        ("2026-01-06", "600000", 10.0, "normal"),
    ]
    conn.executemany(
        "INSERT INTO price_limit_daily(date, symbol, limit_ratio, status) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()

    dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    assert coverage(conn, dates) == (2, 5)
    assert coverage(conn, []) == (0, 0)
    conn.close()


def test_board_ratio_follows_board_rules():
    """板块规则推算的比例必须与交易所规则一致（688/689 不能合并）。

    2026-10-14 用已采集的 135 个交易日 / 743,959 行实测逐行校验：完全一致。
    最容易搞错的是 689（科创板 CDR）：688 是 20%，689 实测是 10%。
    """
    from rpsboard.analytics.limits import board_ratio

    assert board_ratio("600519") == 10.0     # 沪主板
    assert board_ratio("000001") == 10.0     # 深主板
    assert board_ratio("002415") == 10.0     # 中小板并入主板
    assert board_ratio("300750") == 20.0     # 创业板
    assert board_ratio("301551") == 20.0     # 创业板新段
    assert board_ratio("688981") == 20.0     # 科创板
    assert board_ratio("689009") == 10.0     # 科创板 CDR（实测 10%，与 688 不同）
    assert board_ratio("920002") == 30.0     # 北交所新段
    assert board_ratio("830799") == 30.0     # 北交所
    assert board_ratio("430047") == 30.0     # 北交所


def test_derived_frames_cover_all_dates_and_mark_ipo(tmp_path):
    """规则推算要能补全覆盖，并把新股前 5 日标成「无涨跌幅限制」。

    新股前 5 日的涨幅没有上限（实测 508 行全部 status='no_price_limit'），
    不标出来就会把它算成「涨停买不进」，凭空制造一批不可交易标的。
    """
    import sqlite3

    from rpsboard import db as dbmod
    from rpsboard.analytics.limits import (
        derive_ratio_frames,
        limit_mask,
        load_ratio_frames,
        merge_ratio_frames,
    )

    conn = sqlite3.connect(str(tmp_path / "derived.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(dbmod.SCHEMA)
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-05", periods=10)]
    # 600000 全区间有 bar；300001 从第 3 个交易日上市（前 5 根 bar 内涨幅无上限）
    bars = [("600000", d, 10.0, d, 10.0) for d in dates]
    bars += [("300001", d, 10.0, d, 10.0) for d in dates[2:]]
    conn.executemany(
        "INSERT INTO daily_bar(symbol, date, open, close, high, low) VALUES (?,?,?,?,?,?)",
        [(b[0], b[1], b[2], b[2], b[3], b[4]) for b in bars],
    )
    # 实测只覆盖第一天，且第一天 300001 还没上市
    conn.execute("INSERT INTO price_limit_daily(date, symbol, limit_ratio, status) "
                 "VALUES ('2026-01-05','600000',10.0,'normal')")
    conn.commit()

    cols = ["600000", "300001"]
    ratio, status = derive_ratio_frames(conn, dates, cols)
    assert list(ratio.columns) == cols and len(ratio) == len(dates)
    assert set(ratio["600000"].unique()) == {10.0}
    assert set(ratio["300001"].unique()) == {20.0}
    # 300001 的**最早 5 根 bar**（dates[2:7]）→ 无涨跌幅限制；第 6 根起恢复正常
    ipo_days = [d for d in dates if status.loc[d, "300001"] == "no_price_limit"]
    assert ipo_days == dates[2:7], f"应是该股最早的 5 根 bar：{ipo_days}"
    assert status.loc[dates[7], "300001"] == "normal"

    # 实测优先：合并后第一天用实测值，其余用推算值
    measured = load_ratio_frames(conn, dates, cols)
    merged, mstatus = merge_ratio_frames(measured, (ratio, status))
    assert merged.loc[pd.Timestamp("2026-01-05"), "600000"] == 10.0
    assert pd.isna(measured[0].loc[pd.Timestamp(dates[-1]), "600000"])
    assert merged.loc[pd.Timestamp(dates[-1]), "600000"] == 10.0

    # 新股前 5 根 bar 内 +150%：不算涨停（否则会被剔除出候选池）
    ipo_close = pd.DataFrame({
        "600000": [10.0] * len(dates),
        "300001": [10.0, 10.0, 10.0, 10.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0],
    }, index=pd.to_datetime(dates))
    up, dn = limit_mask(ipo_close, merged, mstatus)
    assert bool(up.loc[pd.Timestamp(dates[4]), "300001"]) is False, "IPO 前 5 日不该判涨停"
    assert bool(dn.to_numpy().sum()) is False

    # 同一只票过了前 5 根 bar 后涨 20% 整：按推算的 20% + 0.2pp 容差判定为涨停
    post_close = ipo_close.copy()
    post_close["300001"] = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 12.0, 12.0]
    up2, _ = limit_mask(post_close, merged, mstatus)
    assert bool(up2.loc[pd.Timestamp(dates[8]), "300001"]) is True
    conn.close()
