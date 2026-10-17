"""模拟账户：多账户生命周期、跨进程写锁、参数固化。

设计要点（都是踩过坑才定下来的）：

- **账户是隔离边界**：手动、策略、旧后复权研究账户各自一行，订单/成交/净值/
  持仓都带 `account_id`。旧的单账户实现里 `sim.reset_account` 会 `DELETE FROM
  trade_order/trade_fill`（不留 WHERE），把 QMT 与影子成交一起删掉 —— 现在
  本地流水在独立的 `paper_order/paper_fill`，物理上碰不到券商数据。
- **参数创建时固化**：策略参数与参与率写进账户自己的 meta，推进时只读。
  改参数要**新建账户**（旧曲线不重置、不混算）。
- **写锁按账户**：API 与 CLI 是两个进程，靠 `BEGIN IMMEDIATE` + 每账户一个
  文件锁把「读余额 → 校验 → 写入」整段串起来。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .. import config, db

#: 旧本地模拟盘用的委托号段起点（历史过滤用；新本地订单在 paper_order 里）。
LEGACY_ORDER_ID_BASE = 900_000_000

#: 策略账户可漂移前的完整默认参数（与旧 sim.DEFAULT_PARAMS 一致）。
DEFAULT_PARAMS: dict[str, Any] = {
    "period": config.DEFAULT_PERIOD, "threshold": 80.0, "hold": 20,
    "max_positions": 20, "exclude_st": True, "resonance": False, "signal": "rps",
}

#: 账户类型与价格口径的合法值（与 db.py 的 CHECK 约束一致）。
KINDS = ("manual", "strategy", "legacy_hfq")
PRICE_BASES = ("raw", "hfq")
STATUSES = ("active", "archived")


class AccountError(RuntimeError):
    """账户状态非法（不存在/已归档/参数越界等），调用方转 404/409/422。"""


class AccountBusy(AccountError):
    """同一账户已有写操作在进行（另一个进程/请求）。→ 409。"""


# ── 跨进程写锁 ──────────────────────────────────────────────────────
def _lock_path(conn: sqlite3.Connection, account_id: int) -> Path:
    """锁文件放在**真实数据库文件**旁边，按账户分文件。

    用 `PRAGMA database_list` 取真实路径（`config.DB_PATH` 在测试里会被
    monkeypatch 到临时库，必须跟着走）。库文件不存在（内存库）时退回配置路径。
    """
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        path = Path(row[2]) if row and row[2] else Path(config.DB_PATH)
    except Exception:  # noqa: BLE001
        path = Path(config.DB_PATH)
    return path.with_name(f"{path.name}.paper-{int(account_id)}.lock")


def _acquire_exclusive(fh) -> None:
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_exclusive(fh) -> None:
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def account_write_lock(conn: sqlite3.Connection, account_id: int):
    """独占该账户的写操作（跨进程）。占用中立即抛 `AccountBusy`。

    刻意**不删锁文件**：删掉会让「另一个进程正持有它」的判断失去依据；
    判断占用只看能否拿到 OS 锁，从不看文件是否存在。
    """
    path = _lock_path(conn, account_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+b")
    except OSError as exc:
        raise AccountError(f"无法创建账户锁文件 {path}：{exc}") from exc
    try:
        if fh.seek(0, os.SEEK_END) == 0:
            fh.write(b"\0")
            fh.flush()
        fh.seek(0)
        try:
            _acquire_exclusive(fh)
        except OSError as exc:
            raise AccountBusy(
                f"账户 {account_id} 正在被另一个进程操作，请稍后重试") from exc
        try:
            yield
        finally:
            try:
                _release_exclusive(fh)
            except Exception:  # noqa: BLE001
                pass
    finally:
        fh.close()


# ── 账户读写 ────────────────────────────────────────────────────────
def _row(conn: sqlite3.Connection, account_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM paper_account WHERE id = ?",
                       (int(account_id),)).fetchone()
    return dict(row) if row else None


def get_account(conn: sqlite3.Connection, account_id: int) -> dict:
    """账户 + 固化参数 + 最新结算日。找不到抛 `AccountError`。"""
    acc = _row(conn, account_id)
    if acc is None:
        raise AccountError(f"账户 {account_id} 不存在")
    acc["params"] = meta_get(conn, account_id, "params")
    acc["execution"] = meta_get(conn, account_id, "execution") or {}
    last = conn.execute(
        "SELECT MAX(trade_date) AS d FROM paper_nav WHERE account_id = ?",
        (int(account_id),)).fetchone()
    acc["latest_date"] = last["d"] if last and last["d"] else None
    return acc


def list_accounts(conn: sqlite3.Connection) -> list[dict]:
    """全部账户（新的在前），带最新结算日与参数（列表页要显示，不发 N 次请求）。"""
    out: list[dict] = []
    for r in conn.execute("SELECT * FROM paper_account ORDER BY id"):
        acc = dict(r)
        acc["params"] = meta_get(conn, acc["id"], "params")
        acc["execution"] = meta_get(conn, acc["id"], "execution") or {}
        last = conn.execute(
            "SELECT MAX(trade_date) AS d FROM paper_nav WHERE account_id = ?",
            (acc["id"],)).fetchone()
        acc["latest_date"] = last["d"] if last and last["d"] else None
        out.append(acc)
    return out


def normalize_params(kind: str, params: dict | None) -> dict | None:
    """策略参数补齐 + 校验；手动账户不接受参数。"""
    if kind == "manual":
        if params:
            raise AccountError("手动账户不接受策略参数")
        return None
    raw = {**DEFAULT_PARAMS, **(params or {})}
    unknown = set(raw) - set(DEFAULT_PARAMS)
    if unknown:
        raise AccountError(f"未知策略参数：{sorted(unknown)}")
    if raw["period"] not in config.PERIODS:
        raise AccountError(f"period 必须是 {list(config.PERIODS)} 之一")
    thr = float(raw["threshold"])
    if not 0.0 <= thr <= 100.0:
        raise AccountError("threshold 必须在 0..100")
    hold = int(raw["hold"])
    if hold < 1:
        raise AccountError("hold 必须 ≥ 1")
    mx = int(raw["max_positions"])
    if not 1 <= mx <= 200:
        raise AccountError("max_positions 必须在 1..200")
    if raw["signal"] not in ("rps", "composite"):
        raise AccountError("signal 只能是 rps / composite")
    return {"period": int(raw["period"]), "threshold": thr, "hold": hold,
            "max_positions": mx, "exclude_st": bool(raw["exclude_st"]),
            "resonance": bool(raw["resonance"]), "signal": raw["signal"]}


def _start_date_ok(conn: sqlite3.Connection, start_date: str) -> bool:
    """起点必须是**有原价数据**的指数交易日。"""
    if not start_date:
        return False
    row = conn.execute(
        "SELECT 1 FROM benchmark_bar WHERE symbol='000001' AND date = ? LIMIT 1",
        (start_date,)).fetchone()
    return row is not None


def create_account(
    conn: sqlite3.Connection,
    *,
    name: str,
    kind: str,
    cash: float,
    start_date: str,
    params: dict | None = None,
    participation: float | None = None,
) -> dict:
    """新建模拟账户。**只允许 manual / strategy + raw**。"""
    clean = (name or "").strip()
    if not 1 <= len(clean) <= 60:
        raise AccountError("账户名必须是 1–60 个字符")
    if kind not in ("manual", "strategy"):
        raise AccountError("只能新建 manual 或 strategy 账户")
    try:
        cash = float(cash)
    except (TypeError, ValueError) as exc:
        raise AccountError("初始资金必须是数字") from exc
    if not (cash > 0) or cash != cash or cash in (float("inf"), float("-inf")):
        raise AccountError("初始资金必须是有限正数")
    if not _start_date_ok(conn, start_date):
        raise AccountError(f"起点 {start_date} 不是有原价数据的交易日")

    norm_params = normalize_params(kind, params)
    if participation is None:
        p = float(config.PAPER_MAX_PARTICIPATION)
    else:
        try:
            p = float(participation)
        except (TypeError, ValueError) as exc:
            raise AccountError("参与率必须是数字") from exc
    if not (0.0 <= p <= 1.0):
        raise AccountError("参与率必须在 0..1")

    now = db.now()
    cur = conn.execute(
        "INSERT INTO paper_account(name, kind, price_basis, initial_cash, cash,"
        " start_date, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?, 'active', ?, ?)",
        (clean, kind, "raw", cash, cash, start_date, now, now))
    account_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO paper_meta(account_id, key, value) VALUES (?,?,?)",
        (account_id, "initial_cash", json.dumps(cash)))
    if norm_params is not None:
        conn.execute(
            "INSERT INTO paper_meta(account_id, key, value) VALUES (?,?,?)",
            (account_id, "params", json.dumps(norm_params, ensure_ascii=False)))
    conn.execute(
        "INSERT INTO paper_meta(account_id, key, value) VALUES (?,?,?)",
        (account_id, "execution", json.dumps({"participation": p,
                                              "slippage": 0.0})))
    # 创建当天就留一条「全现金」净值：不这么做的话新账户在页面上显示权益 0，
    # 而它明明是刚注入 20 万现金 —— 看起来像建失败了。
    conn.execute(
        "INSERT OR REPLACE INTO paper_nav(account_id, trade_date, cash, market_value,"
        " receivable, total, nav, n_positions, filled_orders)"
        " VALUES (?,?,?,0,0,?,1.0,0,0)",
        (account_id, start_date, cash, cash))
    conn.commit()
    return get_account(conn, account_id)


def archive_account(conn: sqlite3.Connection, account_id: int) -> dict:
    """归档：只切状态 + 撤销剩余委托（保留全部证据，**不删任何行**）。

    归档不等于删除：曲线、成交、笔记、公司行为核对都还在，只是不能再推进/下单。
    """
    acc = get_account(conn, account_id)
    if acc["status"] == "archived":
        return acc
    day = acc["latest_date"] or acc["start_date"]
    conn.execute(
        "UPDATE paper_order SET status='canceled', closed_trade_date=?, err_msg=?,"
        " updated_at=? WHERE account_id=? AND status IN ('pending','deferred','part')",
        (day, "账户归档，撤销余量", db.now(), int(account_id)))
    conn.execute("UPDATE paper_account SET status='archived', updated_at=? WHERE id=?",
                 (db.now(), int(account_id)))
    conn.commit()
    return get_account(conn, account_id)


# ── 账户 meta ───────────────────────────────────────────────────────
def meta_set(conn: sqlite3.Connection, account_id: int, key: str, value) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO paper_meta(account_id, key, value) VALUES (?,?,?)",
        (int(account_id), key, json.dumps(value, ensure_ascii=False)))


def meta_get(conn: sqlite3.Connection, account_id: int, key: str):
    """读一条账户 meta；不存在或不是合法 JSON 时返回 None。"""
    row = conn.execute(
        "SELECT value FROM paper_meta WHERE account_id = ? AND key = ?",
        (int(account_id), key)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return None


def execution_of(conn: sqlite3.Connection, account_id: int) -> dict:
    """该账户固化的执行参数（参与率、滑点）；缺省用当前配置补一次。"""
    ex = meta_get(conn, account_id, "execution") or {}
    return {"participation": float(ex.get("participation",
                                          config.PAPER_MAX_PARTICIPATION)),
            "slippage": float(ex.get("slippage", 0.0))}
