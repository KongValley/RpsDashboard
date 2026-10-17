"""backfill 的写入语义：单事务 upsert，不允许「先删后写」的空窗。

2026-10-14 实测事故：旧实现先对 4.47M 个 (symbol, date) 对逐个 DELETE 并 commit，
再 to_sql 插入并 commit。两次提交之间被中断（进程被杀）→ 库停在「已删未写」，
435 万行日 K 只剩 1 万行，且删除已提交无法回滚。这里用 sqlite3 的 trace 回调把
写入路径执行过哪些 SQL 固定下来：不得对 daily_bar 发 DELETE，且必须以
INSERT OR REPLACE 收尾。
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest
from rpsboard import cli
from rpsboard import db as dbmod
from rpsboard.sources import tdxnet_src


class _FakeSource:
    """最小数据源替身：两只股票各 3 根 K 线，基准为空。

    `raw_fails` 可以指定「哪些股票的不复权抓取失败」，用来验证**单侧失败**
    的统计与返回（两口径必须分开记账，不能把一侧的失败糊在一起）。
    """

    def __init__(self, raw_fails: tuple[str, ...] = ()):
        self.closed = False
        self.raw_fails = raw_fails

    def stock_list(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "symbol": ["000001", "600000"],
                "name": ["平安银行", "浦发银行"],
                "exchange": ["sz", "sh"],
                "is_st": [0, 0],
            }
        )

    def hfq_bars(self, symbol: str, count: int | None = None) -> pd.DataFrame:
        base = 10.0 if symbol == "000001" else 20.0
        return pd.DataFrame(
            {
                "date": ["2026-01-05", "2026-01-06", "2026-01-07"],
                "open": [base, base + 0.1, base + 0.2],
                "high": [base + 0.5] * 3,
                "low": [base - 0.5] * 3,
                "close": [base, base + 0.1, base + 0.2],
                "volume": [1000, 1100, 1200],
                "amount": [1e6, 1.1e6, 1.2e6],
            }
        )

    def raw_bars(self, symbol: str, count: int | None = None) -> pd.DataFrame:
        if symbol in self.raw_fails:
            raise RuntimeError("raw 抓取失败（测试用）")
        base = 10.0 if symbol == "000001" else 20.0
        return pd.DataFrame(
            {
                "date": ["2026-01-05", "2026-01-06", "2026-01-07"],
                "open": [base, base + 0.1, base + 0.2],
                "high": [base + 0.5] * 3,
                "low": [base - 0.5] * 3,
                "close": [base, base + 0.1, base + 0.2],
                # 不复权口径：成交量单位是**股**（不是手）
                "volume": [100_000, 110_000, 120_000],
                "amount": [1e6, 1.1e6, 1.2e6],
            }
        )

    def index_bars(self, code: str, count: int | None = None) -> pd.DataFrame:
        return pd.DataFrame()

    def close(self) -> None:
        self.closed = True


def _run(conn: sqlite3.Connection, *, raw_fails: tuple[str, ...] = ()):
    seen: list[str] = []
    conn.set_trace_callback(lambda sql: seen.append(sql.strip()))
    try:
        result = tdxnet_src.backfill_bars(conn, workers=1)
    finally:
        conn.set_trace_callback(None)
    return result, seen


@pytest.fixture()
def bar_db(tmp_path, monkeypatch):
    path = tmp_path / "bf.db"
    dbmod.init_db(path)
    conn = sqlite3.connect(str(path))
    monkeypatch.setattr(dbmod, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(tdxnet_src, "TdxSource", _FakeSource)
    try:
        yield conn
    finally:
        conn.close()


def test_backfill_upserts_without_delete(bar_db):
    """写入只走 upsert：不删 daily_bar，也不留临时表。"""
    conn = bar_db
    conn.executemany(
        "INSERT INTO daily_bar(symbol, date, close, pct_chg) VALUES (?,?,?,?)",
        [
            ("000001", "2026-01-07", 1.0, 0.5),   # 会被新值覆盖
            ("999999", "2026-01-07", 7.0, None),  # 同日期、别的股票：不许被碰
        ],
    )
    conn.commit()

    (n_sym, n_rows, failed), seen = _run(conn)
    # 两口径都成功：daily_bar 6 行 + raw_bar 6 行
    assert (n_sym, n_rows, failed) == (2, 12, [])

    assert not [s for s in seen if s.upper().startswith("DELETE FROM DAILY_BAR")], (
        "先删后写会留下已删未写的空窗（2026-10-14 事故根因）："
        f"{[s for s in seen if 'daily_bar' in s][:3]}"
    )
    assert any(
        s.upper().startswith("INSERT OR REPLACE INTO DAILY_BAR") for s in seen
    ), f"写入必须走 upsert，实际执行: {[s for s in seen if 'daily_bar' in s][:3]}"

    same_day = dict(
        conn.execute("SELECT symbol, close FROM daily_bar WHERE date='2026-01-07'")
    )
    assert same_day["999999"] == 7.0, "同一天别的股票的行被误删了"
    assert same_day["000001"] == pytest.approx(10.2), "同股票同日期的行没有被新值覆盖"
    assert conn.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0] == 7

    stage = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='_bars_stage'"
    ).fetchone()[0]
    assert stage == 0, "临时表必须收尾清理"


def test_backfill_is_idempotent(bar_db):
    """重跑同一批数据：不产生重复行，行数不变。"""
    conn = bar_db
    first, _ = _run(conn)
    second, _ = _run(conn)
    assert first == second == (2, 12, [])
    assert conn.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM raw_bar").fetchone()[0] == 6


def test_upsert_master_rolls_back_delete_on_failure(tmp_path):
    """整表替换必须原子：插入失败时旧行要还在（DELETE 不能先提交）。

    旧写法 `DELETE ...; commit(); INSERT` 在中断/失败后会把证券清单清空，
    页面上所有股票的名字都会消失。
    """
    path = tmp_path / "master.db"
    dbmod.init_db(path)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO security_master(symbol, name, exchange) VALUES ('000001', '平安银行', 'sz')"
    )
    conn.commit()

    bad = pd.DataFrame({"symbol": ["600000"], "not_a_column": [1.0]})
    with pytest.raises(pd.errors.DatabaseError):
        dbmod.upsert_master(conn, bad)

    conn.rollback()  # 模拟进程被杀：未提交的事务一起丢
    assert conn.execute("SELECT COUNT(*) FROM security_master").fetchone()[0] == 1
    conn.close()


def test_write_all_rolls_back_delete_on_failure(tmp_path):
    """同样的原子性要求：_write_all 中断后旧行仍在。"""
    path = tmp_path / "cw.db"
    dbmod.init_db(path)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO concept_rps_daily(date, concept, rps60) VALUES ('2026-01-05','车联网',88.0)"
    )
    conn.commit()

    bad = pd.DataFrame({"date": ["2026-01-06"], "concept": ["车联网"], "not_a_column": [1.0]})
    with pytest.raises(pd.errors.DatabaseError):
        cli._write_all(conn, "concept_rps_daily", bad)

    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM concept_rps_daily").fetchone()[0] == 1
    conn.close()


def test_raw_only_failure_is_reported_per_side(bar_db, monkeypatch):
    """一只票的不复权抓取失败：仍写后复权、仍报失败，且两侧分开记账。"""
    conn = bar_db
    monkeypatch.setattr(tdxnet_src, "TdxSource",
                        lambda: _FakeSource(raw_fails=("600000",)))
    (n_sym, n_rows, failed), _seen = _run(conn)
    assert failed == ["600000"], "失败的股票必须报出来"
    assert n_sym == 1, "两口径都成功的只有 1 只"
    assert n_rows == 6 + 3, "后复权 6 行 + 不复权 3 行"
    # 后复权一侧的数据照样写入（单侧失败不该丢掉另一侧）
    assert conn.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM raw_bar").fetchone()[0] == 3
    cp = dbmod.read_raw_checkpoint(conn)
    assert cp["raw_failed_symbols"] == ["600000"]
    assert cp["scope"] == "all"
    # 有失败 → 全市场完整日不推进（首轮就是 None）
    assert cp["latest_complete_date"] is None


def test_raw_volume_is_in_shares(bar_db):
    """raw_bar.volume 必须是**股**（取 SDK 的 volume_wire_value）。"""
    conn = bar_db
    _run(conn)
    rows = dict(conn.execute(
        "SELECT symbol, volume FROM raw_bar WHERE date='2026-01-05'"))
    assert rows["000001"] == pytest.approx(100_000.0), "100000 股，不是 1000 手"
