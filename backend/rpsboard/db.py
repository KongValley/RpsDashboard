"""SQLite 存储层：建表、连接、写日志。

设计要点：
- 所有写入前先 DELETE 同日期再批量 INSERT，保证**幂等重跑**不产生重复行。
- daily_bar / rps_daily 用复合主键兜底。
- connect() 返回只读用途的普通连接；API 层绝不在请求链路内联网取数。
"""

from __future__ import annotations

import json
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

"""

#: 四张**需要重建**的模拟盘表：老库的主键/列清单与新结构不兼容，SQLite 又
#: 不支持改主键，只能「建临时表 → 复制 → 删旧表 → 改名」。
#: 它们是 SCHEMA 与 db.ensure_paper_accounts 的**唯一真源**（{name} 由两处各自
#: 填真实表名），避免同一段 DDL 抄两遍后悄悄漂移。
_PAPER_TABLE_DDL: dict[str, str] = {
    "paper_account": """CREATE TABLE IF NOT EXISTS {name} (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL,
  kind         TEXT NOT NULL CHECK (kind IN ('manual','strategy','legacy_hfq')),
  price_basis  TEXT NOT NULL CHECK (price_basis IN ('raw','hfq')),
  initial_cash REAL NOT NULL,
  cash         REAL NOT NULL,
  start_date   TEXT,
  status       TEXT NOT NULL CHECK (status IN ('active','archived')),
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
)""",
    "paper_position": """CREATE TABLE IF NOT EXISTS {name} (
  account_id   INTEGER NOT NULL,
  trade_date   TEXT NOT NULL,
  symbol       TEXT NOT NULL,
  volume       INTEGER NOT NULL,
  available_volume INTEGER NOT NULL DEFAULT 0,
  avg_price    REAL NOT NULL,
  cost_basis   REAL NOT NULL DEFAULT 0,
  close        REAL NOT NULL,
  price_date   TEXT,
  market_value REAL NOT NULL,
  PRIMARY KEY (account_id, trade_date, symbol)
)""",
    "paper_nav": """CREATE TABLE IF NOT EXISTS {name} (
  account_id    INTEGER NOT NULL,
  trade_date    TEXT NOT NULL,
  cash          REAL NOT NULL,
  market_value  REAL NOT NULL,
  receivable    REAL NOT NULL DEFAULT 0,
  total         REAL NOT NULL,
  nav           REAL NOT NULL,
  n_positions   INTEGER NOT NULL,
  filled_orders INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, trade_date)
)""",
    "paper_meta": """CREATE TABLE IF NOT EXISTS {name} (
  account_id INTEGER NOT NULL,
  key        TEXT NOT NULL,
  value      TEXT NOT NULL,
  PRIMARY KEY (account_id, key)
)""",
}

#: 重建顺序：先建谁、复制谁、删谁、改名谁，都按这个序列走。
PAPER_REBUILD_TABLES = ("paper_account", "paper_position", "paper_nav", "paper_meta")

SCHEMA = SCHEMA + "".join(
    _PAPER_TABLE_DDL[t].format(name=t) + ";\n" for t in PAPER_REBUILD_TABLES
) + """
-- 模拟盘本地委托：与券商 trade_order 彻底分开（原先共用一张表，
-- 结果本地 reset 会把 QMT/影子成交一起删掉）。
CREATE TABLE IF NOT EXISTS paper_order (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id    INTEGER NOT NULL,
  request_id    TEXT NOT NULL,
  trade_date    TEXT NOT NULL,      -- 收盘决策日；只在**以后**的市场日撮合
  symbol        TEXT NOT NULL,
  side          TEXT NOT NULL CHECK (side IN ('buy','sell')),
  volume        INTEGER NOT NULL CHECK (volume > 0),  -- 原始委托量，永不改写
  price         REAL NOT NULL CHECK (price > 0),
  status        TEXT NOT NULL,
  source        TEXT NOT NULL CHECK (source IN ('manual','strategy')),
  thesis        TEXT NOT NULL DEFAULT '',
  review_note   TEXT NOT NULL DEFAULT '',
  err_msg       TEXT,
  closed_trade_date TEXT,           -- 撤单/拒绝/成交结束发生在哪个账户业务日
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (account_id, request_id)
);

-- 本地成交：一日一单一行（UNIQUE(order_id, trade_date)），跨日**追加**，
-- 绝不 INSERT OR REPLACE —— 那会把前一天的成交盖掉（费用与已实现盈亏随之丢失）。
CREATE TABLE IF NOT EXISTS paper_fill (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id  INTEGER NOT NULL,
  order_id    INTEGER NOT NULL,
  trade_date  TEXT NOT NULL,
  symbol      TEXT NOT NULL,
  side        TEXT NOT NULL,
  volume      INTEGER NOT NULL CHECK (volume > 0),
  price       REAL NOT NULL CHECK (price > 0),
  fee         REAL NOT NULL,
  realized_pnl REAL,
  avg_cost    REAL,
  traded_at   TEXT NOT NULL,
  UNIQUE (order_id, trade_date)
);

-- 每次调仓的候选与跳过原因（复盘「为什么没买这只」的依据）
CREATE TABLE IF NOT EXISTS paper_rebalance (
  account_id  INTEGER NOT NULL,
  signal_date TEXT NOT NULL,
  params      TEXT NOT NULL,
  selected    TEXT NOT NULL,
  skipped     TEXT NOT NULL,
  PRIMARY KEY (account_id, signal_date)
);

-- 模拟盘曲线归档（策略对照）：跨账户保留一份不可变的曲线证据。
-- price_basis 标记口径；老归档一律 'hfq'，其指标不重算。
CREATE TABLE IF NOT EXISTS paper_archive (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id   INTEGER,
  label        TEXT NOT NULL,      -- 展示名（缺省按信号源 + 时间生成）
  price_basis  TEXT NOT NULL DEFAULT 'hfq',
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

-- ── 原价（不复权）行情与采集覆盖（模拟账户的成交/估值基准）──────────
-- volume 单位统一是**股**（取 SDK volume_wire_value）；daily_bar.volume 仍是手。
CREATE TABLE IF NOT EXISTS raw_bar (
  symbol      TEXT NOT NULL,
  date        TEXT NOT NULL,
  open        REAL,
  high        REAL,
  low         REAL,
  close       REAL,
  volume      REAL,
  amount      REAL,
  tradestatus INTEGER,
  PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_raw_bar_date ON raw_bar(date);

-- 每只股票「已完整取回」的市场日期区间。账户结算只看相关股票是否覆盖，
-- 不被无关股票的一次失败冻住；全市场完整日另由 collect_log 检查点决定。
CREATE TABLE IF NOT EXISTS raw_coverage (
  symbol     TEXT PRIMARY KEY,
  date_from  TEXT NOT NULL,
  date_to    TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- 缺 bar 的停牌确认（用户按公告核对）：没有确认的缺 bar 一律 raw_bar_missing，
-- 不能用 tradestatus 的复牌间隔诊断去猜停牌。
CREATE TABLE IF NOT EXISTS suspension_confirmation (
  symbol      TEXT NOT NULL,
  date        TEXT NOT NULL,
  source_note TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  PRIMARY KEY (symbol, date)
);

-- ── 公司行为（权息）────────────────────────────────────────────────
-- 供应商事件（capital_changes）：日期/类别/四业务字段，**只够发现事件**，
-- 推不出登记日、到账日、可卖日与个人分红税。
CREATE TABLE IF NOT EXISTS corporate_event (
  symbol     TEXT NOT NULL,
  event_date TEXT NOT NULL,
  category   INTEGER NOT NULL,
  payload    TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (symbol, event_date, category)
);
CREATE TABLE IF NOT EXISTS corporate_sync (
  symbol          TEXT PRIMARY KEY,
  checked_through TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

-- 用户核对后的账户权益方案（resolution 是固定结构的 JSON，见
-- broker/corporate_actions.py）。金额是**账户总额**，不是每股数。
CREATE TABLE IF NOT EXISTS paper_action (
  account_id INTEGER NOT NULL,
  symbol     TEXT NOT NULL,
  event_date TEXT NOT NULL,
  category   INTEGER NOT NULL,
  resolution TEXT NOT NULL,
  PRIMARY KEY (account_id, symbol, event_date, category)
);

-- ── 复盘笔记（市场级 + 个股级）──────────────────────────────────────
-- symbol='' 是市场笔记，6 位代码是个股笔记。日期一律存 resolve_date 后的生效日。
CREATE TABLE IF NOT EXISTS review_note (
  date       TEXT NOT NULL,
  symbol     TEXT NOT NULL DEFAULT '',
  note       TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (date, symbol)
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

#: 需要 account_id 列的索引：**只能**在 ensure_paper_accounts 确认表结构
#: 之后执行。放进 SCHEMA 会在老库上引用不存在的列而整个 execute 失败。
PAPER_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_paper_position_date
  ON paper_position(account_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_paper_nav_date
  ON paper_nav(account_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_paper_order_date
  ON paper_order(account_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_paper_fill_date
  ON paper_fill(account_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_paper_action_symbol
  ON paper_action(account_id, symbol, event_date);
"""

#: SCHEMA 的版本：写进 `PRAGMA user_version`。0 = 迁移前的老库。
#: 1 = 多账户 paper 结构（本文件当前形态）。大于 1 说明库比程序新，拒绝启动。
SCHEMA_VERSION = 1


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
        _guard_schema_version(conn)
        conn.executescript(SCHEMA)
        conn.commit()
        # 旧库不会因为 CREATE TABLE IF NOT EXISTS 而获得新增的周期列（如 rps15）
        added = ensure_period_columns(conn)
        if added:
            print(f"[db] 新增周期列 {', '.join(added)}（需 cli compute-rps --full 填值）")
        # 顺序要紧：先补 trade_fill 的费用列，才能据此判断老成交的费用是否可信；
        # 补列之前的库，其历史成交**没有**费用记录，legacy 账户要标「未知」而不是 0。
        fill_added = ensure_fill_columns(conn)
        if fill_added:
            print("[db] trade_fill 已补 fee/realized_pnl/avg_cost 列"
                  "（历史行无费用记录，显示为 0/未知）")
        actions = ensure_paper_accounts(
            conn, legacy_costs_complete=not fill_added)
        for a in actions:
            print(f"[db] 模拟盘迁移 {a}")
        if ensure_indexes(conn):
            print("[db] 已重建 rps_daily 覆盖索引（并删除冗余的 idx_rps_date）")


def _guard_schema_version(conn: sqlite3.Connection) -> None:
    """库比程序新就拒绝启动：降级覆写会静默丢字段。

    `PRAGMA user_version` 只在库文件里，内存库一律 0。
    """
    try:
        v = int(conn.execute("PRAGMA user_version").fetchone()[0])
    except (sqlite3.Error, TypeError):
        return
    if v > SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库 schema 版本 {v} 比本程序支持的 {SCHEMA_VERSION} 新，"
            "拒绝启动（降级运行会丢字段）。请更新程序或改用正确的库文件。")


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


# ── 模拟盘多账户迁移 ────────────────────────────────────────────────
def _paper_schema_is_current(conn: sqlite3.Connection) -> bool:
    """既有库是否已是多账户结构。

    按**列清单**判断，不能只看 `PRAGMA user_version`：版本号是给「库比程序新」
    的拒绝逻辑用的，老库的版本号一律是 0，重建过的库才是 1。
    """
    want = {
        "paper_account": {"kind", "price_basis", "status", "initial_cash"},
        "paper_position": {"account_id", "available_volume", "cost_basis", "price_date"},
        "paper_nav": {"account_id", "receivable"},
        "paper_meta": {"account_id"},
    }
    for table, cols in want.items():
        if not table_exists(conn, table):
            return False
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not cols <= have:
            return False
    return True


def _backup_db(conn: sqlite3.Connection, dst_dir: Path | None = None) -> Path | None:
    """迁移前用 `Connection.backup()` 落一份完整副本（含 WAL 已提交内容）。

    源连接**只读**且独立于当前写连接；目标文件名带时间与随机后缀，绝不覆盖
    已有备份。返回备份文件；没有库文件（内存库）时返回 None。
    """
    src_path = _db_file(conn)
    if not src_path or not Path(src_path).exists():
        return None
    src = Path(src_path)
    base = Path(dst_dir) if dst_dir else src.parent
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = base / f"{src.stem}.pre-workbench-{stamp}-{uuid.uuid4().hex[:6]}.db"
    ro = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(target))
        try:
            ro.backup(dst)
        finally:
            dst.close()
    finally:
        ro.close()
    return target


def _ensure_archive_columns(conn: sqlite3.Connection) -> list[str]:
    """老 `paper_archive` 补 account_id/price_basis（纯 ADD COLUMN，不动数据）。

    老归档一律是 HFQ 研究口径，`price_basis` 默认值就写 'hfq'；
    其 metrics/curve **不重算**（重算等于改写已公布的历史结论）。
    """
    added: list[str] = []
    if not table_exists(conn, "paper_archive"):
        return added
    have = {r[1] for r in conn.execute("PRAGMA table_info(paper_archive)")}
    if "account_id" not in have:
        conn.execute("ALTER TABLE paper_archive ADD COLUMN account_id INTEGER")
        added.append("paper_archive.account_id")
    if "price_basis" not in have:
        conn.execute(
            "ALTER TABLE paper_archive ADD COLUMN price_basis TEXT NOT NULL DEFAULT 'hfq'")
        added.append("paper_archive.price_basis")
    conn.commit()
    return added


def _set_user_version(conn: sqlite3.Connection) -> None:
    # PRAGMA 不能参数化；SCHEMA_VERSION 是模块内 int 常量，没有注入面。
    conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")


def _legacy_initial_cash(account_row: dict | None, nav_rows: list[dict]) -> float:
    """legacy 账户的初始资金：老 meta 的 initial_cash → 首日 total/nav → 现金。"""
    if account_row is None:
        return 0.0
    cash = float(account_row.get("cash") or 0.0)
    if nav_rows:
        first = nav_rows[0]
        nav = float(first.get("nav") or 0.0)
        if nav > 0:
            return round(float(first.get("total") or 0.0) / nav, 2)
    return cash


def ensure_paper_accounts(
    conn: sqlite3.Connection,
    *,
    backup_dir: Path | None = None,
    legacy_costs_complete: bool = True,
) -> list[str]:
    """把模拟盘迁到多账户结构（幂等）。返回本次动作描述（空 = 无需迁移）。

    老库是**单账户**且 `paper_*` 没有 account_id 列，而 SQLite 改不了主键，
    只能重建四张表。顺序固定：检测 → 备份 → 单事务重建 → 建索引 → 记版本。
    事务内逐条 `execute`（不用 `executescript`，它会隐式提交把事务切碎），
    也不调用自行 commit 的 `ensure_*`；唯一提交点是这里的 `COMMIT`。

    迁移**只搬数值，不重算任何历史结论**：旧曲线/持仓/成交原样进 legacy_hfq
    只读账户（id=1）；旧本地成交留在共用 `trade_order/trade_fill` 里由号段过滤
    读取，不复制进新的 `paper_order/paper_fill`。
    """
    if _paper_schema_is_current(conn):
        conn.executescript(PAPER_INDEXES)
        _ensure_archive_columns(conn)
        _set_user_version(conn)
        conn.commit()
        return []

    backup = _backup_db(conn, backup_dir)
    actions = [f"backup:{backup.name}" if backup else "backup:skipped(no-file)"]

    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")

        have_account = table_exists(conn, "paper_account")
        row = (conn.execute("SELECT * FROM paper_account LIMIT 1").fetchone()
               if have_account else None)
        account_row = dict(row) if row is not None else None
        pos_rows = ([dict(r) for r in conn.execute("SELECT * FROM paper_position")]
                    if table_exists(conn, "paper_position") else [])
        nav_rows = ([dict(r) for r in conn.execute(
            "SELECT * FROM paper_nav ORDER BY trade_date")]
            if table_exists(conn, "paper_nav") else [])
        meta_rows = ([dict(r) for r in conn.execute("SELECT * FROM paper_meta")]
                     if table_exists(conn, "paper_meta") else [])

        for t in PAPER_REBUILD_TABLES:
            conn.execute(_PAPER_TABLE_DDL[t].format(name=f"{t}_new"))

        has_legacy = account_row is not None or bool(pos_rows) or bool(nav_rows)
        if has_legacy:
            init_cash = _legacy_initial_cash(account_row, nav_rows)
            if not init_cash:
                for m in meta_rows:
                    if m.get("key") == "initial_cash":
                        try:
                            init_cash = float(json.loads(m["value"]))
                        except (TypeError, ValueError):
                            init_cash = 0.0
            conn.execute(
                "INSERT INTO paper_account_new(id, name, kind, price_basis, initial_cash,"
                " cash, start_date, status, created_at, updated_at)"
                " VALUES (1,?,?,?,?,?,?,?,?,?)",
                ("旧后复权研究账户", "legacy_hfq", "hfq", init_cash,
                 float((account_row or {}).get("cash") or 0.0),
                 nav_rows[0]["trade_date"] if nav_rows else None,
                 "archived",
                 (account_row or {}).get("created_at") or now(),
                 (account_row or {}).get("updated_at") or now()))
            conn.executemany(
                "INSERT INTO paper_position_new(account_id, trade_date, symbol, volume,"
                " available_volume, avg_price, cost_basis, close, price_date, market_value)"
                " VALUES (1,?,?,?,?,?,?,?,?,?)",
                [(r["trade_date"], r["symbol"], int(r["volume"]), int(r["volume"]),
                  float(r["avg_price"]), float(r["avg_price"]) * int(r["volume"]),
                  float(r["close"]), r["trade_date"], float(r["market_value"]))
                 for r in pos_rows])
            conn.executemany(
                "INSERT INTO paper_nav_new(account_id, trade_date, cash, market_value,"
                " receivable, total, nav, n_positions, filled_orders)"
                " VALUES (1,?,?,?,0,?,?,?,?)",
                [(r["trade_date"], float(r["cash"]), float(r["market_value"]),
                  float(r["total"]), float(r["nav"]), int(r["n_positions"]),
                  int(r.get("filled_orders") or 0)) for r in nav_rows])
            legacy_meta = {m["key"]: m["value"] for m in meta_rows}
            legacy_meta["costs_complete"] = json.dumps(bool(legacy_costs_complete))
            conn.executemany(
                "INSERT INTO paper_meta_new(account_id, key, value) VALUES (1,?,?)",
                list(legacy_meta.items()))
            actions.append(f"legacy-account:1(nav={len(nav_rows)},pos={len(pos_rows)})")
        else:
            actions.append("legacy-account:none")

        for t in PAPER_REBUILD_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
            conn.execute(f"ALTER TABLE {t}_new RENAME TO {t}")

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = ""

    conn.executescript(PAPER_INDEXES)
    _ensure_archive_columns(conn)
    _set_user_version(conn)
    conn.commit()
    return actions


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
        # **不截断**：检查点 JSON（raw-bar-sync 的完整日与失败名单）要靠它读回，
        # 截到 2000 字符会把 JSON 切碎到无法解析。展示层自己截。
        (status, now(), rows, message, run_id),
    )
    conn.commit()


def last_log(conn: sqlite3.Connection) -> dict[str, Any] | None:
    if not table_exists(conn, "collect_log"):
        return None
    row = conn.execute(
        "SELECT * FROM collect_log ORDER BY started_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


# ── 原价采集检查点 / 市场交易日 ─────────────────────────────────────
#: 原价采集写入 collect_log 的任务名与固定 JSON 结构。
#: `latest_complete_date` 是「全市场原价都取齐了」的那一天；账户推进的默认终点
#: 取 `market_date`，再逐股票查各自的覆盖，不被无关股票的失败冻住。
RAW_SYNC_TASK = "raw-bar-sync"
RAW_CHECKPOINT_FIELDS = ("scope", "market_date", "latest_complete_date",
                         "raw_failed_symbols", "hfq_failed_symbols")


def market_date(conn: sqlite3.Connection) -> str | None:
    """市场交易日 = 上证指数（benchmark_bar 000001）的最新日期。

    刻意不用 `MAX(daily_bar.date)`：个股停牌会让某些票的行情比市场晚，
    而 `MAX(rps_daily.date)` 又受横截面门槛影响；指数日期是唯一干净的日历。
    """
    if not table_exists(conn, "benchmark_bar"):
        return None
    row = conn.execute(
        "SELECT MAX(date) AS d FROM benchmark_bar WHERE symbol='000001'").fetchone()
    return row["d"] if row and row["d"] else None


def write_raw_checkpoint(
    conn: sqlite3.Connection,
    *,
    scope: str,
    market_date_value: str | None,
    latest_complete_date: str | None,
    raw_failed: list[str],
    hfq_failed: list[str],
) -> str:
    """落一条原价采集检查点（collect_log）。返回 run_id。

    message 是固定 JSON —— 读取方（`read_raw_checkpoint`）按结构解析，
    所以 `finish_run` 里那个 2000 字符截断必须已经去掉。
    """
    payload = json.dumps({
        "scope": scope,
        "market_date": market_date_value,
        "latest_complete_date": latest_complete_date,
        "raw_failed_symbols": sorted(raw_failed),
        "hfq_failed_symbols": sorted(hfq_failed),
    }, ensure_ascii=False)
    run_id = start_run(conn, task=RAW_SYNC_TASK, source=config.DATA_SOURCE)
    status = "success" if not raw_failed else "partial"
    finish_run(conn, run_id, status, len(raw_failed), payload)
    return run_id


def read_raw_checkpoint(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """最近一次已结束的原价采集检查点；从未跑过返回 None。

    最近一条存在但**结构非法就报错**：静默退回 None（或改用
    `MAX(raw_bar.date)`）会让「推进到哪一天」在数据损坏时悄悄取到错值，
    比直接失败危险得多。
    """
    if not table_exists(conn, "collect_log"):
        return None
    row = conn.execute(
        "SELECT message FROM collect_log WHERE task=? AND status IN"
        " ('success','partial','failed') ORDER BY finished_at DESC, rowid DESC LIMIT 1",
        (RAW_SYNC_TASK,)).fetchone()
    if row is None:
        return None
    # 不假设连接设了 row_factory（裸 sqlite3 连接下 fetchone() 返回 tuple）
    raw = row[0] if not isinstance(row, sqlite3.Row) else row["message"]
    try:
        data = json.loads(raw or "")
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"raw-bar-sync 检查点不是合法 JSON：{raw!r}") from exc
    if not isinstance(data, dict) or not set(RAW_CHECKPOINT_FIELDS) <= set(data):
        raise RuntimeError(f"raw-bar-sync 检查点结构非法：{data!r}")
    return data


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
