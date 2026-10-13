"""旧库迁移：周期列补齐必须幂等，且覆盖索引要跟着重建。

旧的四周期库不会因为 `CREATE TABLE IF NOT EXISTS` 而拿到 rps15/ret15 ——
`ensure_period_columns()` 是唯一入口（cli init 调用），这里守住它。
"""

from __future__ import annotations

import sqlite3

from rpsboard import config
from rpsboard import db as dbmod

#: 改造前的四周期 DDL（含旧的四列覆盖索引），用来模拟线上已有的库
OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS rps_daily (
  date   TEXT NOT NULL,
  symbol TEXT NOT NULL,
  rps20  REAL, rps60 REAL, rps120 REAL, rps250 REAL,
  ret20  REAL, ret60 REAL, ret120 REAL, ret250 REAL,
  PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_rps_date_period
  ON rps_daily(date, rps20, rps60, rps120, rps250);
CREATE TABLE IF NOT EXISTS industry_rps_daily (
  date          TEXT NOT NULL,
  industry      TEXT NOT NULL,
  rps20  REAL, rps60 REAL, rps120 REAL, rps250 REAL,
  ret20  REAL, ret60 REAL, ret120 REAL, ret250 REAL,
  member_count  INTEGER,
  PRIMARY KEY (industry, date)
);
CREATE TABLE IF NOT EXISTS concept_rps_daily (
  date          TEXT NOT NULL,
  concept       TEXT NOT NULL,
  rps20  REAL, rps60 REAL, rps120 REAL, rps250 REAL,
  ret20  REAL, ret60 REAL, ret120 REAL, ret250 REAL,
  member_count  INTEGER,
  PRIMARY KEY (concept, date)
);
"""


def _cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def test_ensure_period_columns_backfills_old_db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "old.db"))
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO rps_daily(date, symbol, rps60) VALUES ('2026-01-05','600519',95.0)"
    )
    conn.commit()

    added = dbmod.ensure_period_columns(conn)

    assert "rps_daily.rps15" in added and "rps_daily.ret15" in added
    for table in dbmod.PERIOD_TABLES:
        cols = _cols(conn, table)
        assert {"rps15", "ret15"} <= set(cols), f"{table} 缺周期列：{cols}"

    index_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_rps_date_period'"
    ).fetchone()[0]
    assert "rps15" in index_sql, "覆盖索引必须重建（IF NOT EXISTS 不会更新同名旧索引）"

    # 加列是元数据操作，既有数据不受影响
    assert conn.execute("SELECT rps60 FROM rps_daily").fetchone()[0] == 95.0

    # 幂等：列已存在时完全无副作用
    assert dbmod.ensure_period_columns(conn) == []
    conn.close()


def test_ensure_period_columns_is_noop_on_new_db(tmp_path):
    """新库的 SCHEMA 已含全部周期列 → 迁移函数什么都不做。"""
    path = tmp_path / "new.db"
    dbmod.init_db(path)
    conn = sqlite3.connect(str(path))
    try:
        assert dbmod.ensure_period_columns(conn) == []
        for table in dbmod.PERIOD_TABLES:
            assert config.period_columns() == [
                c for c in _cols(conn, table) if c.startswith("rps")
            ], table
    finally:
        conn.close()
