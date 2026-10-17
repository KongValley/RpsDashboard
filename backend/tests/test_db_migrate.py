"""旧库迁移：周期列补齐必须幂等，且覆盖索引要跟着重建。

旧的四周期库不会因为 `CREATE TABLE IF NOT EXISTS` 而拿到 rps15/ret15 ——
`ensure_period_columns()` 是唯一入口（cli init 调用），这里守住它。
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest
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


OLD_FILL_DDL = """
CREATE TABLE IF NOT EXISTS trade_fill (
  trade_date      TEXT NOT NULL,
  broker_order_id INTEGER NOT NULL,
  broker_trade_id INTEGER NOT NULL,
  symbol          TEXT NOT NULL,
  side            TEXT NOT NULL,
  volume          INTEGER NOT NULL,
  price           REAL NOT NULL,
  traded_at       TEXT NOT NULL,
  PRIMARY KEY (broker_order_id, broker_trade_id)
);
"""


def test_ensure_fill_columns_backfills_old_db(tmp_path):
    """老库的 `trade_fill` 没有 fee/realized_pnl/avg_cost：必须幂等补列且不动数据。"""
    conn = sqlite3.connect(str(tmp_path / "old_fill.db"))
    conn.executescript(OLD_FILL_DDL)
    conn.execute(
        "INSERT INTO trade_fill(trade_date, broker_order_id, broker_trade_id, symbol,"
        " side, volume, price, traded_at) VALUES ('2026-01-05', 900000001, 1,"
        " '000001', 'buy', 1000, 10.05, '2026-01-05T15:00:00')")
    conn.commit()

    added = dbmod.ensure_fill_columns(conn)
    assert set(added) == {"trade_fill.fee", "trade_fill.realized_pnl",
                          "trade_fill.avg_cost"}

    row = conn.execute("SELECT * FROM trade_fill").fetchone()
    cols = [d[0] for d in conn.execute("SELECT * FROM trade_fill").description]
    got = dict(zip(cols, row))
    assert got["volume"] == 1000 and got["price"] == 10.05
    assert got["fee"] == 0, "老成交没有费用记录，宁可显示 0/未知也不猜"
    assert got["realized_pnl"] is None
    assert got["avg_cost"] is None

    # 幂等：列都在时完全无副作用
    assert dbmod.ensure_fill_columns(conn) == []
    conn.close()


def test_init_db_adds_fill_columns_to_existing_db(tmp_path):
    """`cli init` 必须把老库升级到位（迁移的唯一入口）。"""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(OLD_FILL_DDL)
    conn.commit()
    conn.close()

    dbmod.init_db(path)
    conn = sqlite3.connect(str(path))
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(trade_fill)")}
        assert {"fee", "realized_pnl", "avg_cost"} <= cols
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


# ── 模拟盘多账户迁移 ────────────────────────────────────────────────
#: 改造前的单账户 DDL（paper_* 没有 account_id；trade_fill 没有费用列）
OLD_PAPER_DDL = """
CREATE TABLE paper_account (
  id INTEGER PRIMARY KEY CHECK (id = 1), cash REAL NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE paper_position (
  trade_date TEXT NOT NULL, symbol TEXT NOT NULL, volume INTEGER NOT NULL,
  avg_price REAL NOT NULL, close REAL NOT NULL, market_value REAL NOT NULL,
  PRIMARY KEY (trade_date, symbol));
CREATE INDEX idx_paper_position_date ON paper_position(trade_date);
CREATE TABLE paper_nav (
  trade_date TEXT NOT NULL, cash REAL NOT NULL, market_value REAL NOT NULL,
  total REAL NOT NULL, nav REAL NOT NULL, n_positions INTEGER NOT NULL,
  filled_orders INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (trade_date));
CREATE TABLE paper_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _make_old_paper_db(path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(OLD_PAPER_DDL)
    conn.executescript(OLD_FILL_DDL)
    conn.execute("INSERT INTO paper_account VALUES (1, 5000.0, '2026-01-01', '2026-01-01')")
    conn.execute("INSERT INTO paper_nav VALUES ('2026-01-01',4000,1100,5100,1.02,1,1)")
    conn.execute("INSERT INTO paper_position VALUES ('2026-01-01','000001',100,10.0,11.0,1100.0)")
    conn.execute("INSERT INTO paper_meta VALUES ('initial_cash','5000.0')")
    conn.execute("INSERT INTO trade_fill VALUES ('2026-01-01',123,1,'000001','buy',100,10.0,'x')")
    conn.commit()
    conn.close()


def test_paper_migration_preserves_values_and_makes_backup(tmp_path):
    """迁移：数值原样进 legacy 只读账户，QMT 成交不动，且有迁移前备份。"""
    path = tmp_path / "old_paper.db"
    _make_old_paper_db(path)
    dbmod.init_db(path)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    assert conn.execute("PRAGMA user_version").fetchone()[0] == dbmod.SCHEMA_VERSION
    acc = dict(conn.execute("SELECT * FROM paper_account").fetchone())
    assert acc["id"] == 1 and acc["kind"] == "legacy_hfq"
    assert acc["price_basis"] == "hfq" and acc["status"] == "archived"
    assert acc["initial_cash"] == 5000.0 and acc["cash"] == 5000.0
    # 曲线与持仓逐值保留（receivable 是新列，默认 0）
    nav = dict(conn.execute("SELECT * FROM paper_nav WHERE trade_date='2026-01-01'").fetchone())
    assert (nav["account_id"], nav["cash"], nav["market_value"], nav["total"], nav["nav"],
            nav["filled_orders"]) == (1, 4000.0, 1100.0, 5100.0, 1.02, 1)
    pos = dict(conn.execute("SELECT * FROM paper_position").fetchone())
    assert (pos["account_id"], pos["symbol"], pos["volume"], pos["cost_basis"]) == \
        (1, "000001", 100, 1000.0)
    # QMT 成交原样（只多了费用列，值不变）
    fill = dict(conn.execute("SELECT * FROM trade_fill").fetchone())
    assert fill["broker_order_id"] == 123 and fill["volume"] == 100
    # 费用列是「补出来的」→ legacy 标记 costs_complete=false
    meta = {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM paper_meta WHERE account_id=1")}
    assert meta["costs_complete"] == "false"
    conn.close()
    backups = [p for p in tmp_path.iterdir() if "pre-workbench" in p.name]
    assert len(backups) == 1, "迁移前必须有且只有一份备份"


def test_paper_migration_is_idempotent_and_does_not_rebackup(tmp_path):
    path = tmp_path / "old_paper2.db"
    _make_old_paper_db(path)
    dbmod.init_db(path)
    after_first = sorted(p.name for p in tmp_path.iterdir() if "pre-workbench" in p.name)
    dbmod.init_db(path)
    after_second = sorted(p.name for p in tmp_path.iterdir() if "pre-workbench" in p.name)
    assert after_first == after_second, "第二次 init 不该再备份"
    conn = sqlite3.connect(str(path))
    n = conn.execute("SELECT COUNT(*) FROM paper_account").fetchone()[0]
    assert n == 1, "重复迁移不该产生重复账户"
    conn.close()


def test_paper_migration_on_empty_db_creates_no_legacy_account(tmp_path):
    """空库（没有旧账户/净值/持仓）不该凭空造出一个 legacy 账户。"""
    path = tmp_path / "empty.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(OLD_PAPER_DDL)
    conn.commit()
    conn.close()
    dbmod.init_db(path)
    conn = sqlite3.connect(str(path))
    assert conn.execute("SELECT COUNT(*) FROM paper_account").fetchone()[0] == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == dbmod.SCHEMA_VERSION
    conn.close()
    # 旧结构被**重建**过 → 仍然要给一份备份（备份是按「是否做了破坏性重建」判断的，
    # 不是按「有没有数据」）；但不该凭空造出 legacy 账户。
    assert [p for p in tmp_path.iterdir() if "pre-workbench" in p.name], \
        "做过破坏性重建就必须留备份"


def test_schema_version_guard_refuses_newer_db(tmp_path):
    path = tmp_path / "future.db"
    dbmod.init_db(path)
    conn = sqlite3.connect(str(path))
    conn.execute(f"PRAGMA user_version = {dbmod.SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="比本程序支持的"):
        dbmod.init_db(path)


def test_finish_run_does_not_truncate_long_message(tmp_path):
    """检查点 JSON 不能被 2000 字符截断（否则读回来就解析不了）。"""
    path = tmp_path / "log.db"
    dbmod.init_db(path)
    with dbmod.session(path) as conn:
        payload = "x" * 5000
        run_id = dbmod.start_run(conn, task="raw-bar-sync", source="eltdx")
        dbmod.finish_run(conn, run_id, "success", 0, payload)
        row = conn.execute("SELECT message FROM collect_log WHERE run_id=?",
                           (run_id,)).fetchone()
    assert len(row["message"]) == 5000


def test_read_raw_checkpoint_rejects_malformed(tmp_path):
    path = tmp_path / "cp.db"
    dbmod.init_db(path)
    with dbmod.session(path) as conn:
        assert dbmod.read_raw_checkpoint(conn) is None
        run_id = dbmod.start_run(conn, task="raw-bar-sync", source="eltdx")
        dbmod.finish_run(conn, run_id, "success", 0, "{not json")
    with dbmod.session(path) as conn:
        with pytest.raises(RuntimeError, match="合法 JSON"):
            dbmod.read_raw_checkpoint(conn)
