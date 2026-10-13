"""三升底形态检测测试。

守住四件事：
1. **四条判定规则各自生效** —— 三根阳线、收盘递增、第一根小实体、突破第一根最高价。
2. **不能跨股票串行** —— 早期版本用 Series.shift() 在非唯一索引上整表位移，
   结果全市场命中 0 只；必须用 groupby(symbol).shift()。
3. **失效判定** —— 跌破第一根最低价即形态破坏。
4. **筛选窗口按交易日回退**，不是日历日。
"""

from __future__ import annotations

import pandas as pd
import pytest

from rpsboard.analytics import patterns


def _frame(rows):
    """rows: [(symbol, date, open, high, low, close)]"""
    df = pd.DataFrame(rows, columns=["symbol", "date", "open", "high", "low", "close"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def _three_bull(b1_body=1.0, b3_body=4.0, b1_shadow=0.2, breakout=True,
                mid_bull=True):
    """构造一组三根阳线（第一天 / 第二天 / 第三天）。

    b1_body    第一根实体
    b3_body    **第三根**实体（中间那根取 0.3 倍，只要求为正）
    b1_shadow  第一根的上影线长度 —— 实盘里第一根常带长上影，
               这也是「第三根收盘突破第一根最高价」成为独立条件的原因：
               没有足够长的上影时，ratio<=0.5 + 三根递增已经隐含了突破。

    ⚠ 第三根实体必须独立于第一根，否则「把第一根做大」这个反例会被
    等比放大的第三根抵消，测不出真正的拒绝逻辑。
    """
    base = 10.0
    b1_close = base + b1_body
    b1_high = b1_close + b1_shadow
    b2_open = b1_close
    b2_close = b1_close + b3_body * 0.3
    b2_high = max(b2_open, b2_close) + 0.2
    b3_open = b2_close
    b3_close = b2_close + b3_body * 0.7
    b3_high = b3_close + 0.2
    if not breakout:
        # 把第三根收盘压到第一根最高价之下，但仍保持阳线与收盘递增
        b3_close = min(b3_close, b1_high - 0.05)
        b3_open = b3_close - b3_body * 0.7
        b3_high = b3_close + 0.1
    if not mid_bull:
        # 第二根改成阴线：开盘抬高、收盘压低
        b2_open = b1_close + b3_body
        b2_close = b1_close + 0.01
        b2_high = b2_open + 0.2
    lo = lambda c: c - 0.05  # noqa: E731
    return [
        ("A", "2024-01-02", base, b1_high, lo(base), b1_close),
        ("A", "2024-01-03", b2_open, b2_high, lo(min(b2_open, b2_close)), b2_close),
        ("A", "2024-01-04", b3_open, b3_high, lo(min(b3_open, b3_close)), b3_close),
    ]


def test_detects_standard_pattern():
    out = patterns.compute_sanxingdi(_frame(_three_bull()))
    assert len(out) == 1
    r = out.iloc[0]
    assert r["symbol"] == "A"
    assert str(r["date"].date()) == "2024-01-04"     # 第三根阳线=确认日
    assert str(r["base_date"].date()) == "2024-01-02"  # 第一根阳线
    assert r["base_low"] == pytest.approx(9.95)
    assert r["base_high"] == pytest.approx(11.2)      # 10 + 1.0 实体 + 0.2 上影
    assert r["strength"] > 0
    # 第三根实体是 b3_body 的 0.7 倍（余下 0.3 给了中间那根）
    assert r["body_ratio"] == pytest.approx(1.0 / (4.0 * 0.7), abs=1e-6)


def test_requires_three_bullish_candles():
    out = patterns.compute_sanxingdi(_frame(_three_bull(mid_bull=False)))
    assert out.empty, "中间一根是阴线时不应成立"


def test_requires_ascending_closes():
    rows = _three_bull()
    # 让第二根收盘低于第一根收盘
    _, d2, o2, h2, l2, _ = rows[1]
    rows[1] = ("A", d2, o2, h2, l2, rows[0][5] - 0.01)
    out = patterns.compute_sanxingdi(_frame(rows))
    assert out.empty, "收盘未逐根抬高时不应成立"


def test_first_candle_body_must_be_small():
    """第一根实体超过第三根一半 → 不成立；放宽阈值后同一组数据应成立。"""
    rows = _frame(_three_bull(b1_body=3.0, b3_body=4.0))
    assert patterns.compute_sanxingdi(rows).empty
    lax = patterns.SanxingdiRules(first_body_ratio=1.2)
    assert len(patterns.compute_sanxingdi(rows, lax)) == 1


def test_breakout_of_first_high_required():
    """第一根上影线很长时，第三根收盘够不到第一根最高价 → 不成立。"""
    out = patterns.compute_sanxingdi(
        _frame(_three_bull(b1_shadow=8.0, breakout=False)))
    assert out.empty, "第三根收盘未突破第一根最高价时不应成立"


def test_breakout_can_be_disabled():
    rules = patterns.SanxingdiRules(require_breakout=False)
    out = patterns.compute_sanxingdi(
        _frame(_three_bull(b1_shadow=8.0, breakout=False)), rules)
    assert len(out) == 1


def test_patterns_do_not_leak_across_symbols():
    """关键回归：两只股票交错排列时，不能把 A 的形态算到 B 头上。

    早期用 Series.shift() 会在 (symbol, date) 非唯一索引上整表位移，
    取到「上一只股票的上一根 K 线」，全市场命中 0 只。
    """
    a = _three_bull()
    b = _three_bull()
    rows = []
    for i in range(3):
        rows.append(a[i])
        rows.append(("B", b[i][1], *b[i][2:]))
    out = patterns.compute_sanxingdi(_frame(rows))
    assert set(out["symbol"]) == {"A", "B"}
    assert len(out) == 2


def test_interleaved_fake_pattern_rejected():
    """A 的第一、三根 + B 的一根交错，A 缺自己的第二根，不该成立。"""
    a = _three_bull()
    rows = [a[0], ("B", a[1][1], *a[1][2:]), a[2]]
    out = patterns.compute_sanxingdi(_frame(rows))
    assert out.empty


def test_insufficient_history_returns_empty():
    rows = _three_bull()[:2]
    out = patterns.compute_sanxingdi(_frame(rows))
    assert out.empty


def test_empty_input():
    out = patterns.compute_sanxingdi(
        pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close"])
    )
    assert out.empty


def test_missing_columns_raise():
    with pytest.raises(ValueError, match="缺少列"):
        patterns.compute_sanxingdi(pd.DataFrame({"symbol": ["A"]}))


def test_invalidation_by_base_low():
    """跌破第一根最低价 → 形态失效；不破 → 仍有效。"""
    base_low = _three_bull()[0][4]

    def active(after_low):
        return after_low is None or after_low >= base_low

    assert active(base_low - 0.1) is False
    assert active(base_low) is True
    assert active(base_low + 1.0) is True
    assert active(None) is True      # 没有后续数据 → 视为仍有效


def test_nth_trading_date_back_uses_rps_dates():
    """窗口起点必须是「含当天在内」的最近 n 个交易日。

    早期用 `WHERE date < ?`，n=1 会退化成「前一个交易日」，
    于是「近1日」实际覆盖 2 天，命中数对不上。
    """
    import sqlite3
    from rpsboard import db as dbmod

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row       # 与 db.connect() 一致
    conn.executescript(dbmod.SCHEMA)
    dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    conn.executemany("INSERT INTO rps_daily (date, symbol, rps60) VALUES (?,?,?)",
                     [(d, "A", 90.0) for d in dates])
    conn.commit()
    w = patterns.window_start
    assert w(conn, "2024-01-05", 1) == "2024-01-05"   # 含当天
    assert w(conn, "2024-01-05", 2) == "2024-01-04"
    assert w(conn, "2024-01-05", 4) == "2024-01-02"
    assert w(conn, "2024-01-05", 5) is None            # 超出已有交易日
    assert w(conn, "2024-01-05", 0) == "2024-01-05"
    conn.close()
