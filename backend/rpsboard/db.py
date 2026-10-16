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

-- 复权因子异常事件（analytics/quality.py 检测）。**只报告，不改 RPS 口径**：
-- 自动剔除会静默改变已公布的回测结论，必须由使用者判断。
-- kind: negative_offset（变换含负偏移，历史价被整体下移）
--       factor_jump（除权因子跳变，跨该日的区间收益含送转成分）
--       hfq_jump（库内单日跳变超涨跌停上限，非上市初期/复牌日）
--       no_limit_day（上市前 5 日或复牌日的正常无限制跳变）
-- source: official（eltdx 官方复权因子）/ internal（daily_bar 离线扫描）
CREATE TABLE IF NOT EXISTS adjust_event (
  symbol     TEXT NOT NULL,
  date       TEXT NOT NULL,
  kind       TEXT NOT NULL,
  ratio      REAL,
  detail     TEXT,
  source     TEXT NOT NULL,
  updated_at TEXT,
  PRIMARY KEY (symbol, date, kind, source)
);
CREATE INDEX IF NOT EXISTS idx_adjust_kind ON adjust_event(kind);

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

-- ── QMT 交易（T 只读 / P2 影子 / P3 券商模拟盘；**没有实盘**）─────────
-- 计划：口径来自回测的 entry_mask，等权目标仓位；mode 只有 paper / sim
CREATE TABLE IF NOT EXISTS trade_plan (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_date   TEXT NOT NULL,
  mode        TEXT NOT NULL,
  params      TEXT NOT NULL,
  cash_budget REAL NOT NULL,
  status      TEXT NOT NULL,          -- planned / executed / halted / partial
  note        TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trade_plan_date ON trade_plan(plan_date);

CREATE TABLE IF NOT EXISTS trade_plan_item (
  plan_id       INTEGER NOT NULL,
  symbol        TEXT NOT NULL,
  target_volume INTEGER NOT NULL,          -- 绝对目标股数（对账直接比它）
  ref_price     REAL NOT NULL,
  plan_price    REAL,
  side          TEXT NOT NULL,        -- buy / sell / hold（相对建计划时的现状）
  reason        TEXT NOT NULL,        -- ok / suspended / price_out_of_limit / no_tick …
  -- 建计划那一刻的持股数：用来判断「计划建好之后账户有没有被别人动过」，
  -- 否则执行前无从区分「按计划补足差额」与「计划已过期」。
  held          INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (plan_id, symbol)
);

-- 委托：幂等键 (trade_date, symbol, side, plan_id) —— 重跑不会重复发单
CREATE TABLE IF NOT EXISTS trade_order (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id         INTEGER,
  trade_date      TEXT NOT NULL,
  symbol          TEXT NOT NULL,
  side            TEXT NOT NULL,
  volume          INTEGER NOT NULL,
  price           REAL NOT NULL,
  status          TEXT NOT NULL,      -- pending/submitted/canceling/part/filled/canceled/rejected/unknown
  broker_order_id INTEGER,
  err_id          INTEGER,
  err_msg         TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  UNIQUE (trade_date, symbol, side, plan_id)
);
CREATE INDEX IF NOT EXISTS idx_trade_order_date ON trade_order(trade_date);

-- 成交：主键用 (broker_order_id, broker_trade_id)；影子成交用**负的**
-- broker_order_id（-plan_id），因此影子行与真实成交天然不冲突。
CREATE TABLE IF NOT EXISTS trade_fill (
  trade_date      TEXT NOT NULL,
  broker_order_id INTEGER NOT NULL,
  broker_trade_id INTEGER NOT NULL,
  symbol          TEXT NOT NULL,
  side            TEXT NOT NULL,
  volume          INTEGER NOT NULL,
  price           REAL NOT NULL,
  traded_at       TEXT NOT NULL,
  -- 该笔成交产生的费用（佣金/印花税/过户费/滑点）。老库经
  -- db.ensure_fill_columns 幂等补列，默认 0。
  fee             REAL NOT NULL DEFAULT 0,
  -- 仅卖出笔有意义：卖出净收入 − 卖出部分的账面成本（账面成本含累计费用）。
  -- 买入笔为 NULL。没有它，事后复盘只能靠重放，而重放会污染曲线。
  realized_pnl    REAL,
  -- 对账用成本单价：买入 = (成交额 + 费用) / 股数；卖出 = 卖出部分的账面成本 / 股数
  avg_cost        REAL,
  PRIMARY KEY (broker_order_id, broker_trade_id)
);
CREATE INDEX IF NOT EXISTS idx_trade_fill_date ON trade_fill(trade_date);

-- 持仓快照：只由 `cli trade status/snapshot` 写入，页面与对账都只读它
CREATE TABLE IF NOT EXISTS position_snapshot (
  snap_date      TEXT NOT NULL,
  taken_at       TEXT NOT NULL,
  symbol         TEXT NOT NULL,
  volume         INTEGER NOT NULL,
  can_use_volume INTEGER,
  avg_price      REAL,
  market_value   REAL,
  PRIMARY KEY (snap_date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_position_snapshot_taken ON position_snapshot(taken_at);

-- ── 自建模拟盘（mode='local'；**不连任何券商**）────────────────────
-- 撮合用库内后复权日 K：T 日收盘后下单 → T+1 日内按限价判定成交。
-- 现金是账户的**当前**余额（单行），持仓与净值按日留档，便于事后审计。
CREATE TABLE IF NOT EXISTS paper_account (
  id          INTEGER PRIMARY KEY CHECK (id = 1),   -- 只有一个模拟账户
  cash        REAL NOT NULL,                        -- 可用现金（元）
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_position (
  trade_date  TEXT NOT NULL,       -- 该日收盘时的持仓快照
  symbol      TEXT NOT NULL,
  volume      INTEGER NOT NULL,
  avg_price   REAL NOT NULL,       -- 累计成本（含费用）/ 股
  close       REAL NOT NULL,       -- 该日后复权收盘价
  market_value REAL NOT NULL,
  PRIMARY KEY (trade_date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_paper_position_date ON paper_position(trade_date);

-- 净值曲线：initial_cash 固定一次，之后用 total / nav（倍数）看
CREATE TABLE IF NOT EXISTS paper_nav (
  trade_date    TEXT NOT NULL,
  cash          REAL NOT NULL,
  market_value  REAL NOT NULL,
  total         REAL NOT NULL,
  nav           REAL NOT NULL,     -- total / initial_cash
  n_positions   INTEGER NOT NULL,
  filled_orders INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (trade_date)
);
CREATE TABLE IF NOT EXISTS paper_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 模拟盘曲线归档：模拟盘只有**一个**活动账户（paper_account.id CHECK (id=1)），
-- 跑第二种策略必须先 --reset 清掉第一种；归档就是把清掉之前的证据留下来，
-- 供「策略对照」页叠加比较（原先这份对照只能手写进 README）。
-- 纯新增表，不动任何既有表结构。
CREATE TABLE IF NOT EXISTS paper_archive (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  label        TEXT NOT NULL,      -- 展示名（缺省按信号源 + 时间生成）
  params       TEXT NOT NULL,      -- JSON：创建曲线时的 paper_meta.params
  initial_cash REAL NOT NULL,
  from_date    TEXT,
  to_date      TEXT,
  total        REAL NOT NULL,      -- 期末总权益
  nav          REAL NOT NULL,
  total_return REAL NOT NULL,
  max_drawdown REAL NOT NULL,
  days         INTEGER NOT NULL,
  metrics      TEXT NOT NULL,      -- JSON：review().summary + 基准对照
  curve        TEXT NOT NULL,      -- JSON：paper_nav 各行（点数受 nav_curve 上限约束）
  created_at   TEXT NOT NULL
);

-- 已保存的横截面筛选条件（排行榜的"我的看板"）。**纯新增表**。
-- params 是排行榜请求参数的子集（白名单，见 api/screens.py）；
-- name 唯一：同名保存视为覆盖，避免攒出一堆同名条目。
CREATE TABLE IF NOT EXISTS saved_screen (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL UNIQUE,
  params     TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- 已保存的回测（跨会话对比用）。**纯新增表**：既有库由
-- `CREATE TABLE IF NOT EXISTS` 直接获得，不动任何老表。
-- payload 是完整结果 JSON（逐日曲线约 890 点 + 分档 + 警告，约 50 KB），
-- 存下来才能"载入"回页面重看；对比表只读 params/metrics 两列。
CREATE TABLE IF NOT EXISTS backtest_run (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  label      TEXT NOT NULL,
  params     TEXT NOT NULL,   -- JSON：归一化后的请求参数
  metrics    TEXT NOT NULL,   -- JSON：指标（列表接口只回这两列）
  payload    TEXT NOT NULL,   -- JSON：完整结果（曲线/分档/警告）
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_backtest_run_created ON backtest_run(id DESC);

-- 因子评估结果：每次 `cli factors` 一行，payload 是完整 JSON（约 30 KB）。
-- 页面只读最新一行；保留历史是为了能对比「改过因子之后 IC 变了多少」。
-- 因子矩阵本身**不落库**（13 × 1285 × 5574 × 8B ≈ 750 MB），落库的只有这份结果。
CREATE TABLE IF NOT EXISTS factor_eval (
  run_at  TEXT PRIMARY KEY,
  params  TEXT NOT NULL,
  payload TEXT NOT NULL
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
    """借一个连接（用完关掉）。

    **不自动提交**：调用方写完必须自己 `conn.commit()`（只读查询不用）。
    漏提交不会报错，只是改动在 close 时被回滚 —— 接口却照常返回成功，
    排查起来很难（2026-10-17 回测存档就踩过一次）。
    """
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
        if ensure_fill_columns(conn):
            print("[db] trade_fill 已补 fee/realized_pnl/avg_cost 列"
                  "（历史行无费用记录，显示为 0/未知）")
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


def ensure_fill_columns(conn: sqlite3.Connection) -> list[str]:
    """给既有库的 `trade_fill` 补上费用/已实现盈亏列（幂等）。

    新库由 SCHEMA 直接带上这些列；老库的 `CREATE TABLE IF NOT EXISTS`
    不会补列，所以要走一次 ALTER TABLE ADD COLUMN（纯元数据操作，不动数据）。
    补出来的历史行 `fee=0, realized_pnl=NULL` —— 那些成交确实**没有**记录过
    费用，宁可让它们显示为"未知"也不要假装是 0 成本。
    """
    added: list[str] = []
    if not table_exists(conn, "trade_fill"):
        return added
    have = {row[1] for row in conn.execute("PRAGMA table_info(trade_fill)")}
    for col, ddl in (("fee", "REAL NOT NULL DEFAULT 0"),
                     ("realized_pnl", "REAL"),
                     ("avg_cost", "REAL")):
        if col not in have:
            conn.execute(f"ALTER TABLE trade_fill ADD COLUMN {col} {ddl}")
            added.append(f"trade_fill.{col}")
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
    # 删除与写入必须同一个事务：中间 commit 一次的话，插入失败（磁盘满、主键冲突）
    # 会留下一个**已删未写**的空分区，而且是静默的 —— 调用方只看到异常，
    # 数据却已经没了。
    for d in dates:
        conn.execute(f"DELETE FROM {table} WHERE {date_col} = ?", (d,))
    df.to_sql(table, conn, if_exists="append", index=False, chunksize=2000)
    conn.commit()
    return len(df)


def upsert_master(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """证券主数据：按 symbol 覆盖写。"""
    if df.empty:
        return 0
    # DELETE 与写入同一事务：证券清单全市场重写，中间中断留下空表的话，
    # 页面上所有股票的名字都会消失（且 backfill 一开头就调它）
    conn.execute("DELETE FROM security_master")
    df.to_sql("security_master", conn, if_exists="append", index=False, chunksize=1000)
    conn.commit()
    return len(df)
