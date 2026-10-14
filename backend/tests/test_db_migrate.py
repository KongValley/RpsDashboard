"""旧库迁移：周期列补齐必须幂等，且覆盖索引要跟着重建。

旧的四周期库不会因为 `CREATE TABLE IF NOT EXISTS` 而拿到 rps15/ret15 ——
`ensure_period_columns()` 是唯一入口（cli init 调用），这里守住它。
"""

from __future__ import annotations

import sqlite3

import pandas as pd
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
CREATE INDEX IF NOT EXISTS idx_rps_date ON rps_daily(date);
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
    assert dbmod.ensure_indexes(conn) == ["idx_rps_date_period"]   # init_db 里的第二步

    assert "rps_daily.rps15" in added and "rps_daily.ret15" in added
    for table in dbmod.PERIOD_TABLES:
        cols = _cols(conn, table)
        assert {"rps15", "ret15"} <= set(cols), f"{table} 缺周期列：{cols}"

    index_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_rps_date_period'"
    ).fetchone()[0]
    assert "rps15" in index_sql, "覆盖索引必须重建（IF NOT EXISTS 不会更新同名旧索引）"
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_rps_date'"
    ).fetchone()[0] == 0, "被覆盖索引前缀取代的 idx_rps_date 必须删掉"

    # 加列是元数据操作，既有数据不受影响
    assert conn.execute("SELECT rps60 FROM rps_daily").fetchone()[0] == 95.0

    # 幂等：列与索引都已就位时完全无副作用
    assert dbmod.ensure_period_columns(conn) == []
    assert dbmod.ensure_indexes(conn) == []
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


def test_full_materialize_drops_and_rebuilds_period_index(tmp_path):
    """全量重算先删覆盖索引、写完重建：跑完之后索引必须回来，且没有冗余索引。

    索引是 try/finally 重建的，进程被中途杀掉也不会永久丢失（init_db → ensure_indexes
    同样能自愈）—— 这里守住「跑完之后状态正确」这一半。
    """
    from rpsboard.analytics import matrix, rps

    from .fixtures import make_long

    path = tmp_path / "mat.db"
    dbmod.init_db(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        df = make_long(n_symbols=20, n_days=120, seed=5)
        ind = pd.Series({s: "行业A" for s in sorted(df["symbol"].unique())})
        n_stock, _ = rps.materialize(
            conn, matrix.build_close_matrix(df), matrix.build_tradable_matrix(df),
            matrix.build_st_flags(df), ind, None, mode="full",
        )
        assert n_stock > 0

        indexes = dict(conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index'"
        ).fetchall())
        assert "idx_rps_date" not in indexes, "冗余索引不该被重建"
        assert "rps15" in (indexes.get("idx_rps_date_period") or ""), \
            f"全量重算后覆盖索引必须重建：{indexes.get('idx_rps_date_period')}"
    finally:
        conn.close()
