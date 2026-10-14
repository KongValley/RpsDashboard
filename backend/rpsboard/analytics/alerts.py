"""条件预警引擎：评估规则并把命中写进 `alert_hit`。

边界（与 README 的 F5 约定一致）：**只落库 + 命令行输出，不做任何消息推送**。
前端只负责展示命中列表；`cli alert run` 与 `cli update` 末尾都会调用这里。

规则类型（`kind` 决定 `params` 的 JSON 结构）：

| kind            | params                                | 命中条件 |
|-----------------|---------------------------------------|----------|
| `rps_threshold` | `{"period": 60, "min": 90, "max": 100}` | 当日 `rps{period}` 落在 [min, max] 内 |
| `sanxingdi`     | `{"window": 5}`                        | 近 window 个交易日内出现过三升底 |
| `limit_up`      | `{}`                                   | 当日按 limit_ratio 判为涨停（含容差） |
"""

from __future__ import annotations

import json
import math
import sqlite3

from .. import config

ALERT_KINDS = ("rps_threshold", "sanxingdi", "limit_up")

#: 三升底窗口：与 api/rps.py 的 SANXINGDI_WINDOWS 保持一致（0 = 不筛）
SANXINGDI_WINDOWS = (1, 5, 10, 20)


def _trade_dates_before(conn: sqlite3.Connection, date: str, n: int) -> list[str]:
    """date 及其之前 n 个有 RPS 数据的交易日（含 date），升序。"""
    rows = conn.execute(
        "SELECT DISTINCT date FROM rps_daily WHERE date <= ? "
        "ORDER BY date DESC LIMIT ?",
        (date, n),
    ).fetchall()
    return sorted(r["date"] for r in rows)


def _as_float(params: dict, key: str, default: float) -> float:
    """params[key] 转 float；None / 空串 / 非法 / 非有限值一律回落 default。

    库里可能存着老版本前端写进去的 `{"min": null}`（JSON null）——
    直接 float(None) 会让整条规则评估抛 TypeError，连带后面所有规则都不跑。
    """
    raw = params.get(key)
    if raw is None or raw == "":
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if math.isfinite(val) else default


def _as_int(params: dict, key: str, default: int) -> int:
    raw = params.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _hit_rps_threshold(conn, date: str, params: dict) -> list[tuple[str, str]]:
    period = _as_int(params, "period", config.DEFAULT_PERIOD)
    if period not in config.PERIODS:
        period = config.DEFAULT_PERIOD
    lo = _as_float(params, "min", 0.0)
    hi = _as_float(params, "max", 100.0)
    lo, hi = min(lo, hi), max(lo, hi)
    rows = conn.execute(
        f"SELECT symbol, rps{period} AS v FROM rps_daily "
        f"WHERE date = ? AND rps{period} IS NOT NULL AND rps{period} BETWEEN ? AND ?",
        (date, lo, hi),
    ).fetchall()
    return [(r["symbol"], f"RPS{period}={r['v']:.1f}") for r in rows]


def _hit_sanxingdi(conn, date: str, params: dict) -> list[tuple[str, str]]:
    if not (conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pattern_daily'"
    ).fetchone()):
        return []
    window = _as_int(params, "window", 5)
    if window not in SANXINGDI_WINDOWS:
        window = 5
    dates = _trade_dates_before(conn, date, window)
    if not dates:
        return []
    marks = ",".join("?" * len(dates))
    rows = conn.execute(
        f"SELECT symbol, MAX(date) AS d FROM pattern_daily "
        f"WHERE date IN ({marks}) GROUP BY symbol",
        tuple(dates),
    ).fetchall()
    return [(r["symbol"], f"三升底 {r['d']}") for r in rows]


def _hit_limit_up(conn, date: str, params: dict) -> list[tuple[str, str]]:
    from .limits import TOLERANCE_PCT

    rows = conn.execute(
        "SELECT p.symbol AS symbol, p.limit_ratio AS ratio, b.pct_chg AS pct "
        "FROM price_limit_daily p JOIN daily_bar b "
        "  ON b.symbol = p.symbol AND b.date = p.date "
        "WHERE p.date = ? AND p.limit_ratio IS NOT NULL AND b.pct_chg IS NOT NULL",
        (date,),
    ).fetchall()
    out = []
    for r in rows:
        pct = float(r["pct"]) * 100.0
        if pct >= float(r["ratio"]) - TOLERANCE_PCT:
            out.append((r["symbol"], f"涨停 {pct:.2f}%"))
    return out


_EVALUATORS = {
    "rps_threshold": _hit_rps_threshold,
    "sanxingdi": _hit_sanxingdi,
    "limit_up": _hit_limit_up,
}


def run_rules(conn: sqlite3.Connection, date: str | None = None) -> list[dict]:
    """评估全部 enabled 规则并写入 alert_hit，返回命中的记录列表。

    date 缺省 = 最新交易日（取自 rps_daily）；没有 RPS 数据时直接返回空。
    """
    if date is None:
        row = conn.execute("SELECT MAX(date) AS d FROM rps_daily").fetchone()
        date = row["d"] if row and row["d"] else None
    if not date:
        return []

    rules = conn.execute(
        "SELECT id, name, kind, params FROM alert_rule WHERE enabled = 1 ORDER BY id"
    ).fetchall()
    hits: list[dict] = []
    for rule in rules:
        kind = rule["kind"]
        if kind not in _EVALUATORS:
            print(f"[alert] 规则 {rule['id']} 类型 {kind} 不支持，跳过")
            continue
        try:
            params = json.loads(rule["params"] or "{}")
            if not isinstance(params, dict):
                raise ValueError("params 不是对象")
        except (ValueError, TypeError) as exc:
            print(f"[alert] 规则 {rule['id']} 参数非法（{exc}），跳过")
            continue
        try:
            matched = _EVALUATORS[kind](conn, date, params)
        except (sqlite3.Error, ValueError, TypeError) as exc:
            print(f"[alert] 规则 {rule['id']} 评估失败：{exc}")
            continue
        for symbol, detail in matched:
            conn.execute(
                "INSERT OR REPLACE INTO alert_hit(rule_id, date, symbol, detail) "
                "VALUES (?,?,?,?)",
                (rule["id"], date, symbol, detail),
            )
            hits.append({"rule_id": rule["id"], "rule_name": rule["name"],
                         "date": date, "symbol": symbol, "detail": detail})
        # 每条规则单独提交：只在最后 commit 的话，后面一条规则抛错会把前面
        # 已经算好并写入的命中一起回滚掉（并且异常还会中断整轮评估）。
        conn.commit()
    conn.commit()
    return hits
