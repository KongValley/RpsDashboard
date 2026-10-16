"""测试全局约定。

合成数据的股票数只有几十只，而生产门槛 `config.MIN_CROSS_SECTION`（默认 1000）
会把**所有**日期判成「横截面太薄」，于是每个用到 RPS 的用例都会看到空表 ——
那不是被测逻辑坏了，是门槛在起作用。这里统一调低；要验证门槛本身的用例
（tests/test_rps.py::test_cross_section_gate_is_configurable）显式传参。
"""

from __future__ import annotations

import sqlite3

import pandas as pd
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


@pytest.fixture(autouse=True)
def _no_real_push(monkeypatch):
    """测试期间一律不发真推送（同进程与子进程都挡）。

    `config` 在导入时就把仓库根 `.env.local` 的真实专属地址读进了 `os.environ`
    与 `config.SHOWDOC_PUSH_URL`。而 `broker.exec._notify_disconnect()` 只看
    `notify.configured()`，所以断线用例一跑就会**真的**把测试消息推到开发者手机上
    （2026-10-15 实测：一次全量 pytest 收到两条）。

    要测真实 HTTP 路径的用例（`test_notify_http.py`）在自己的 fixture 里重新设成
    本地临时服务地址，覆盖这里的空串。
    """
    monkeypatch.setattr(config, "SHOWDOC_PUSH_URL", "")
    monkeypatch.setenv("SHOWDOC_PUSH_URL", "")


@pytest.fixture()
def market_db(tmp_path):
    """40 只 × 320 交易日的合成全市场（含 rps_daily 与实测涨跌停行）。

    刻意用**贴近真实的 OHLC**（high/low 围绕 close，而不是恒定 1.0）：
    `trade_db` 那套把 high/low 压成 1.0 是为了让涨跌停断言可控，但用它跑模拟盘
    会让卖单永远挂不进去，「续跑等价性」「逐笔盈亏」都会退化成空断言。
    """
    path = tmp_path / "market.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(dbmod.SCHEMA)
    monkey_cs = config.MIN_CROSS_SECTION
    config.MIN_CROSS_SECTION = 2          # 合成库只有 40 只票
    try:
        df = make_long(n_symbols=40, n_days=320, seed=202)
        bars = df.assign(
            open=df["close"] * 0.995,
            high=df["close"] * 1.02,
            low=df["close"] * 0.98,
            volume=1_000_000.0, amount=1e8,
        )
        bars[["symbol", "date", "open", "high", "low", "close", "volume", "amount",
              "pct_chg", "is_st", "tradestatus"]].to_sql(
            "daily_bar", conn, if_exists="append", index=False)
        symbols = sorted(df["symbol"].unique())
        pd.DataFrame({
            "symbol": symbols, "name": [f"测试{i}" for i in range(len(symbols))],
            "exchange": "sz", "ipo_date": "20150101", "is_st": 0,
            "updated_at": "2026-01-01",
        }).to_sql("security_master", conn, if_exists="append", index=False)
        ind = make_industry_map_zh(symbols, 5)
        ind.rename("industry").reset_index().rename(
            columns={"index": "symbol"}).assign(
            industry_cls="证监会", updated_at="2026-01-01").to_sql(
            "industry_map", conn, if_exists="append", index=False)
        rps_engine.materialize(
            conn, build_close_matrix(bars), build_tradable_matrix(bars),
            build_st_flags(bars), ind, None)
        # 每个交易日的每只票都给实测涨跌停行（宽区间），免得行情本身把撮合拦住
        lim = pd.DataFrame(
            [(d, s, 50.0, 1e9, 0.0, "main", "normal") for s in symbols
             for d in df["date"].unique()],
            columns=["date", "symbol", "limit_ratio", "limit_up", "limit_down",
                     "rule", "status"])
        lim.to_sql("price_limit_daily", conn, if_exists="append", index=False)
        conn.commit()
    finally:
        config.MIN_CROSS_SECTION = monkey_cs
        conn.close()
    return path


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
