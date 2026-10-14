"""SQLite 存储层：建表、连接、写日志。

设计要点：
- 所有写入前先 DELETE 同日期再批量 INSERT，保证**幂等重跑**不产生重复行。
- daily_bar / rps_daily 用复合主键兜底。
- connect() 返回只读用途的普通连接；API 层绝不在请求链路内联网取数。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS security_master (
  symbol     TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  exchange   TEXT NOT NULL,
  ipo_date   TEXT,           -- 数据源不给上市日（恒 NULL），UI 显示的是首根 K 线日期
  is_st      INTEGER DEFAULT 0,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS industry_map (
  symbol        TEXT PRIMARY KEY,
  industry      TEXT NOT NULL,
  industry_cls  TEXT,
  updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS daily_bar (
  symbol      TEXT NOT NULL,
  date        TEXT NOT NULL,
  open        REAL,
  high        REAL,
  low         REAL,
  close       REAL,          -- 后复权收盘价
  volume      REAL,
  amount      REAL,
  pct_chg     REAL,
  is_st       INTEGER,
  tradestatus INTEGER,
  PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_bar_date ON daily_bar(date);

CREATE TABLE IF NOT EXISTS rps_daily (
  date   TEXT NOT NULL,
  symbol TEXT NOT NULL,
  rps15  REAL, rps20 REAL, rps60 REAL, rps120 REAL, rps250 REAL,
  ret15  REAL, ret20 REAL, ret60 REAL, ret120 REAL, ret250 REAL,
  PRIMARY KEY (symbol, date)
);
-- 覆盖索引：排行榜的核心查询是
--   WHERE date=? AND rps60 BETWEEN ? AND ? ORDER BY rps60 DESC
-- 单列 date 索引只能定位到日期，再用 TEMP B-TREE 临时排序 5000+ 行。
-- 把**全部**周期都放进索引后可以直接走有序区间扫描，排序与筛选都在索引里完成；
-- 它的前缀就是 date，所以历史上那个单列 idx_rps_date 是纯冗余（81 MB +
-- 每次写入多维护一棵 B 树），已从建表语句里删掉、旧库由 init_db → ensure_indexes DROP。
CREATE INDEX IF NOT EXISTS idx_rps_date_period
  ON rps_daily(date, rps15, rps20, rps60, rps120, rps250);

CREATE TABLE IF NOT EXISTS industry_rps_daily (
  date          TEXT NOT NULL,
  industry      TEXT NOT NULL,
  rps15  REAL, rps20 REAL, rps60 REAL, rps120 REAL, rps250 REAL,
  ret15  REAL, ret20 REAL, ret60 REAL, ret120 REAL, ret250 REAL,
  member_count  INTEGER,
  leader_symbol TEXT,
  leader_name   TEXT,
  PRIMARY KEY (industry, date)
);
CREATE INDEX IF NOT EXISTS idx_ind_rps_date ON industry_rps_daily(date);

CREATE TABLE IF NOT EXISTS benchmark_bar (
  symbol TEXT NOT NULL,
  date   TEXT NOT NULL,
  close  REAL,
  PRIMARY KEY (symbol, date)
);

-- 当日涨跌停信息（eltdx daily_price_limits）
-- limit_ratio 是**百分数**（10.0 / 20.0 / 30.0），不是小数；
-- limit_up/limit_down 是**不复权**价位，与 daily_bar 的后复权收盘价
-- 不在同一基准，**不可直接比较**——判定请用 analytics/limits.py 的收益率比法。
CREATE TABLE IF NOT EXISTS price_limit_daily (
  date        TEXT NOT NULL,
  symbol      TEXT NOT NULL,
  limit_ratio REAL,
  limit_up    REAL,
  limit_down  REAL,
  rule        TEXT,
  status      TEXT,
  PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_limit_date ON price_limit_daily(date);

-- 概念/题材成分股（一只股票可属于多个概念，成分高度重叠）
CREATE TABLE IF NOT EXISTS concept_map (
  symbol    TEXT NOT NULL,
  concept   TEXT NOT NULL,
  board_code TEXT,
  updated_at TEXT,
  PRIMARY KEY (symbol, concept)
);
CREATE INDEX IF NOT EXISTS idx_concept_name ON concept_map(concept);

-- 概念板块 RPS，结构与 industry_rps_daily 完全对称
CREATE TABLE IF NOT EXISTS concept_rps_daily (
  date          TEXT NOT NULL,
  concept       TEXT NOT NULL,
  rps15  REAL, rps20 REAL, rps60 REAL, rps120 REAL, rps250 REAL,
  ret15  REAL, ret20 REAL, ret60 REAL, ret120 REAL, ret250 REAL,
  member_count  INTEGER,
  leader_symbol TEXT,
  leader_name   TEXT,
  PRIMARY KEY (concept, date)
);
CREATE INDEX IF NOT EXISTS idx_concept_rps_date ON concept_rps_daily(date);

-- 技术形态命中表（三升底）。**稀疏表**：只存成立的形态，不存未成立的日子。
-- base_low 是第一根阳线的最低价，跌破即形态失效。
CREATE TABLE IF NOT EXISTS pattern_daily (
  symbol     TEXT NOT NULL,
  date       TEXT NOT NULL,      -- 第三根阳线（形态确认日）
  base_date  TEXT,               -- 第一根阳线日期
  base_low   REAL,               -- 第一根最低价：跌破即失效
  base_high  REAL,
  strength   REAL,               -- 三根合计涨幅
  body_ratio REAL,               -- 第一根实体 / 第三根实体
  PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_pattern_date ON pattern_daily(date);
CREATE INDEX IF NOT EXISTS idx_pattern_symbol ON pattern_daily(symbol, date);

-- ── 自选股池与条件预警（T6）──────────────────────────────────────
-- 看板的写入面就这三张表（其余 handler 一律只读 SQLite）
CREATE TABLE IF NOT EXISTS watchlist (
  symbol   TEXT PRIMARY KEY,
  note     TEXT,
  added_at TEXT NOT NULL
);

-- 预警规则：kind 决定 params 的 JSON 结构
--   rps_threshold: {"period": 60, "min": 90, "max": 100}
--   sanxingdi:     {"window": 5}
--   limit_up:      {}
CREATE TABLE IF NOT EXISTS alert_rule (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  kind       TEXT NOT NULL,
  params     TEXT NOT NULL,
  enabled    INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

-- 命中记录：按 (规则, 日期, 股票) 去重，反复 run 幂等
CREATE TABLE IF NOT EXISTS alert_hit (
  rule_id INTEGER NOT NULL,
  date    TEXT NOT NULL,
  symbol  TEXT NOT NULL,
  detail  TEXT,
  PRIMARY KEY (rule_id, date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_alert_hit_date ON alert_hit(date);

CREATE TABLE IF NOT EXISTS collect_log (
  run_id        TEXT PRIMARY KEY,
  task          TEXT NOT NULL,
  source        TEXT,
  status        TEXT NOT NULL,
  started_at    TEXT,
  finished_at   TEXT,
  rows_affected INTEGER,
  message       TEXT
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """建立 SQLite 连接（行工厂设为列名访问）。"""
    db_path = Path(path) if path else config.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def session(path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(path: Path | str | None = None) -> None:
    with session(path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()
        # 旧库不会因为 CREATE TABLE IF NOT EXISTS 而获得新增的周期列（如 rps15）
        added = ensure_period_columns(conn)
        if added:
            print(f"[db] 新增周期列 {', '.join(added)}（需 cli compute-rps --full 填值）")
        if ensure_indexes(conn):
            print("[db] 已重建 rps_daily 覆盖索引（并删除冗余的 idx_rps_date）")


#: 带周期列的三张表（列名由 config.PERIODS 决定，见 config.period_columns）
PERIOD_TABLES = ("rps_daily", "industry_rps_daily", "concept_rps_daily")


def ensure_period_columns(conn: sqlite3.Connection) -> list[str]:
    """给既有库补上 config.PERIODS 里的周期列。

    幂等：列已存在就跳过。返回本次新增的列名（形如 ``rps_daily.rps15``）。
    只做 ALTER TABLE ADD COLUMN —— 纯元数据操作，不重建表、不动数据。
    索引由 ``ensure_indexes()`` 负责。
    """
    added: list[str] = []
    wanted = config.period_columns() + config.period_columns("ret")
    for table in PERIOD_TABLES:
        if not table_exists(conn, table):
            continue
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col in wanted:
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL")
                added.append(f"{table}.{col}")
    conn.commit()
    return added


def drop_period_index(conn: sqlite3.Connection) -> None:
    """删掉 rps_daily 的覆盖索引（全量重写前调用：394 万行逐行维护 B 树要 90 s）。"""
    conn.execute("DROP INDEX IF EXISTS idx_rps_date_period")
    conn.commit()


def rebuild_period_index(conn: sqlite3.Connection) -> None:
    """按 config.PERIODS 重建覆盖索引：写完再建只扫一遍表，比逐行维护快得多。

    必须先 DROP：``CREATE INDEX IF NOT EXISTS`` 遇到同名旧索引什么都不做，
    留下的还是旧列清单（这正是 rps15 那次迁移踩过的坑）。
    """
    conn.execute("DROP INDEX IF EXISTS idx_rps_date_period")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rps_date_period"
        f" ON rps_daily(date, {', '.join(config.period_columns())})"
    )
    conn.commit()


def ensure_indexes(conn: sqlite3.Connection) -> list[str]:
    """确保覆盖索引存在且列清单与 config.PERIODS 一致，并删掉冗余的 idx_rps_date。

    幂等；无动作时只查一次 sqlite_master。返回值非空表示重建过索引。
    全量重算会先 DROP 覆盖索引（写完再建），进程被中途杀掉就会留下「索引没了」的
    状态 —— 靠这里自愈，否则排行榜会退化成临时排序。
    """
    conn.execute("DROP INDEX IF EXISTS idx_rps_date")
    # 不假设连接设了 row_factory：裸 sqlite3 连接的 fetchone() 返回 tuple
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_rps_date_period'"
    ).fetchone()
    index_sql = (row[0] or "") if row else ""
    if all(c in index_sql for c in config.period_columns()):
        conn.commit()
        return []
    rebuild_period_index(conn)
    return ["idx_rps_date_period"]


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    # 不假设连接设了 row_factory：裸 sqlite3 连接下 fetchone() 返回 tuple
    if not table_exists(conn, table):
        return 0
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    if not row:
        return 0
    return int(row[0])


def table_has_rows(conn: sqlite3.Connection, table: str) -> bool:
    """表是否至少有一行。

    **判断「有没有数据」时不要用 count_rows()**：COUNT(*) 对大表会退化成
    覆盖索引全扫描。本机实测（daily_bar 427 万行）：
        SELECT COUNT(*) FROM daily_bar     235.1 ms
        SELECT MAX(date)   FROM daily_bar     0.0 ms
        SELECT 1 FROM daily_bar LIMIT 1       0.0 ms
    API 的每个请求都要判断数据库是否就绪，用 COUNT 等于给每次请求硬加 235ms。
    """
    try:
        return conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


_count_cache: dict[tuple[str, str], tuple[float, int]] = {}


def _db_file(conn: sqlite3.Connection) -> str:
    """当前连接指向的库文件路径（用于给缓存分区，避免多个库互相污染）。"""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        return (row[2] or "") if row else ""
    except sqlite3.Error:
        return ""


def count_rows_cached(
    conn: sqlite3.Connection, table: str, ttl: float = 60.0
) -> int:
    """带 TTL 的行数统计，仅用于**展示**（如状态栏的「日K 427 万行」）。

    数据每天才更新一次，60 秒的延迟完全无感；而每次请求都 COUNT 一遍
    400 万行要 200ms+。需要精确值的地方（例如 CLI）请直接用 count_rows。

    缓存键包含库文件路径：测试与脚本会同时打开多个临时库，
    只按表名缓存会让 A 库读到 B 库的行数。
    """
    key = (_db_file(conn), table)
    now = time.monotonic()
    hit = _count_cache.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    n = count_rows(conn, table)
    _count_cache[key] = (now, n)
    return n


# ── 采集日志 ────────────────────────────────────────────────────────
def start_run(conn: sqlite3.Connection, task: str, source: str) -> str:
    run_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO collect_log (run_id, task, source, status, started_at)"
        " VALUES (?,?,?,?,?)",
        (run_id, task, source, "running", now()),
    )
    conn.commit()
    return run_id


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: str,
    rows: int = 0,
    message: str = "",
) -> None:
    conn.execute(
        "UPDATE collect_log SET status=?, finished_at=?, rows_affected=?, message=?"
        " WHERE run_id=?",
        (status, now(), rows, message[:2000], run_id),
    )
    conn.commit()


def last_log(conn: sqlite3.Connection) -> dict[str, Any] | None:
    if not table_exists(conn, "collect_log"):
        return None
    row = conn.execute(
        "SELECT * FROM collect_log ORDER BY started_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


# ── 写入辅助 ────────────────────────────────────────────────────────
def replace_date_partition(
    conn: sqlite3.Connection,
    table: str,
    dates: list[str],
    df: pd.DataFrame,
    date_col: str = "date",
) -> int:
    """先删除指定日期分区，再批量写入——保证重跑幂等。

    这是参考 Sequoia-X 的做法：全市场增量写同一批日期时，先清干净再写，
    否则复权因子修正/数据订正会留下新旧混杂的脏行。
    """
    if df.empty:
        return 0
    for d in dates:
        conn.execute(f"DELETE FROM {table} WHERE {date_col} = ?", (d,))
    conn.commit()
    df.to_sql(table, conn, if_exists="append", index=False, chunksize=2000)
    conn.commit()
    return len(df)


def upsert_master(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """证券主数据：按 symbol 覆盖写。"""
    if df.empty:
        return 0
    conn.execute("DELETE FROM security_master")
    conn.commit()
    df.to_sql("security_master", conn, if_exists="append", index=False, chunksize=1000)
    conn.commit()
    return len(df)
