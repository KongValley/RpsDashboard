"""测试全局约定。

合成数据的股票数只有几十只，而生产门槛 `config.MIN_CROSS_SECTION`（默认 1000）
会把**所有**日期判成「横截面太薄」，于是每个用到 RPS 的用例都会看到空表 ——
那不是被测逻辑坏了，是门槛在起作用。这里统一调低；要验证门槛本身的用例
（tests/test_rps.py::test_cross_section_gate_is_configurable）显式传参。
"""

from __future__ import annotations

import sqlite3

import pytest
from rpsboard import config
from rpsboard import db as dbmod
from rpsboard.analytics import rps as rps_engine
from rpsboard.analytics.matrix import (
    build_close_matrix,
    build_st_flags,
    build_tradable_matrix,
)
from rpsboard.broker import qmt

from .fixtures import make_industry_map_zh, make_long

#: 合成账户的可用现金
TRADE_CASH = 1_000_000.0


@pytest.fixture(autouse=True)
def _small_cross_section(monkeypatch):
    """把横截面门槛降到 2（等价于「至少两只股票才比较」）。"""
    monkeypatch.setattr(config, "MIN_CROSS_SECTION", 2)


@pytest.fixture()
def trade_db(tmp_path, monkeypatch):
    """交易链路的合成库：40 只 × 320 天，含 rps_daily 与宽松的实测涨跌停价。

    涨跌停价放到 ±50%：**价格越界只由用例自己造**，不会被合成行情的随机
    价格误触发。QMT 侧（positions/tick/asset）默认打成「空仓 + 真实收盘价」，
    用例按需再覆盖。
    """
    path = tmp_path / "trade.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(dbmod.SCHEMA)

    df = make_long(n_symbols=40, n_days=320, seed=202)
    df.assign(volume=1_000_000.0, amount=1e8, open=1.0, high=1.0, low=1.0)[[
        "symbol", "date", "open", "high", "low", "close", "volume", "amount",
        "pct_chg", "is_st", "tradestatus"]].to_sql("daily_bar", conn, if_exists="append",
                                                    index=False)

    symbols = sorted(df["symbol"].unique())
    conn.executemany(
        "INSERT INTO security_master(symbol, name, exchange, ipo_date, is_st, updated_at)"
        " VALUES (?,?,?,?,0,'2026-01-01')",
        [(s, f"测试{i}", "sz", "20200101") for i, s in enumerate(symbols)],
    )
    ind = make_industry_map_zh(symbols, 5)
    ind.rename("industry").reset_index().rename(columns={"index": "symbol"}).assign(
        industry_cls="证监会", updated_at="2026-01-01"
    ).to_sql("industry_map", conn, if_exists="append", index=False)

    close = build_close_matrix(df)
    rps_engine.materialize(
        conn, close, build_tradable_matrix(df), build_st_flags(df), ind, None)

    plan_date = df["date"].max()
    last_close = close.iloc[-1].to_dict()
    conn.executemany(
        "INSERT OR REPLACE INTO price_limit_daily(date, symbol, limit_ratio, limit_up,"
        " limit_down, rule, status) VALUES (?,?,10.0,?,?,'main','normal')",
        [(plan_date, s, last_close[s] * 1.5, last_close[s] * 0.5) for s in symbols],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "QMT_ACCOUNT", "0000000123")
    monkeypatch.setattr(qmt, "positions", lambda: [])
    monkeypatch.setattr(qmt, "tick", lambda codes: {c: last_close[c] for c in codes})
    monkeypatch.setattr(qmt, "asset", lambda: {"cash": TRADE_CASH})
    return path, plan_date, last_close
