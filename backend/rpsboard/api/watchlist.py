"""自选股池与条件预警的读写接口。

这是**全 API 唯一的写入口**（`db.py` 里 watchlist / alert_rule / alert_hit 三张表）；
其余 handler 保持只读 SQLite 的既有约束。写操作都在本文件内 commit + close，
不做任何外部采集（与 README 的「只落库 + CLI 输出、不引入消息推送」边界一致）。
"""

from __future__ import annotations

import json
import math

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from .. import config, db
from ..analytics.alerts import ALERT_KINDS, SANXINGDI_WINDOWS
from . import resolve_date

router = APIRouter(prefix="/api", tags=["watchlist"])

#: 代码必须是 6 位数字（与 eltdx 的 6 位代码口径一致）
SYMBOL_PATTERN = r"^\d{6}$"


class WatchItem(BaseModel):
    symbol: str = Field(pattern=SYMBOL_PATTERN, description="6 位股票代码")
    note: str | None = Field(default=None, max_length=120)


def _to_float(raw, default: float | None) -> float | None:
    """转 float；None/空串回落 default，非法或非有限返回 None（交给校验拒绝）。"""
    if raw is None or raw == "":
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None


def _to_int(raw, default: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1          # 值域白名单会拒掉它


class AlertRuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    kind: str
    params: dict = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        if v not in ALERT_KINDS:
            raise ValueError(f"kind 必须是 {ALERT_KINDS} 之一")
        return v

    @model_validator(mode="after")
    def _normalize_params(self) -> AlertRuleIn:
        """按 kind 校验并归一化 params（非法即 422）。

        参数直接决定 SQL 的 BETWEEN 边界与窗口大小：period 不在周期真源里会
        静默筛错列，`min: null` 会让 float(None) 抛 TypeError —— 以前这些值
        会原样存进 alert_rule，直到 cli alert run 才炸。
        """
        p = dict(self.params or {})
        if self.kind == "rps_threshold":
            period = _to_int(p.get("period"), config.DEFAULT_PERIOD)
            if period not in config.PERIODS:
                raise ValueError(f"period 必须是 {list(config.PERIODS)} 之一")
            # 键缺省 → 用默认区间；显式给 null/非法值 → 报错（不静默替换用户输入）
            lo = 0.0 if "min" not in p else _to_float(p.get("min"), None)
            hi = 100.0 if "max" not in p else _to_float(p.get("max"), None)
            if lo is None or hi is None:
                raise ValueError("min / max 必须是数字（0~100）")
            if not 0 <= lo <= hi <= 100:
                raise ValueError("min / max 必须满足 0 ≤ min ≤ max ≤ 100")
            p = {"period": period, "min": lo, "max": hi}
        elif self.kind == "sanxingdi":
            window = _to_int(p.get("window"), 5)
            if window not in SANXINGDI_WINDOWS:
                raise ValueError(
                    f"window 必须是 {list(SANXINGDI_WINDOWS)} 之一")
            p = {"window": window}
        else:                                   # limit_up 不接参数
            p = {}
        self.params = p
        return self


@router.get("/watchlist")
def list_watchlist(date: str | None = None) -> list[dict]:
    """自选列表，附带**指定日**（缺省最新）的各周期 RPS。

    原先硬编码 `MAX(date)`：复盘时看不到自选在历史某天的状态。
    `date` 走全项目统一的 `resolve_date`（非交易日就近回退），
    响应里每条都带 `data_date`（实际生效日）。
    """
    conn = db.connect()
    try:
        d = resolve_date(conn, date) or "9999-12-31"
        rows = conn.execute(
            "SELECT w.symbol, w.note, w.added_at, s.name, s.is_st, i.industry, "
            "       r.date AS data_date, "
            + ", ".join(f"r.{c}" for c in config.period_columns())
            + " FROM watchlist w "
            "LEFT JOIN security_master s ON s.symbol = w.symbol "
            "LEFT JOIN industry_map i ON i.symbol = w.symbol "
            "LEFT JOIN rps_daily r ON r.symbol = w.symbol "
            "  AND r.date = (SELECT MAX(date) FROM rps_daily WHERE date <= ?) "
            "ORDER BY w.added_at DESC",
            (d,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/watchlist")
def add_watchlist(item: WatchItem) -> dict:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO watchlist(symbol, note, added_at) VALUES (?,?,?)",
            (item.symbol, item.note, db.now()),
        )
        conn.commit()
        return {"ok": True, "symbol": item.symbol}
    finally:
        conn.close()


@router.delete("/watchlist/{symbol}")
def remove_watchlist(symbol: str) -> dict:
    if not symbol.isdigit() or len(symbol) != 6:
        raise HTTPException(status_code=422, detail="代码必须是 6 位数字")
    conn = db.connect()
    try:
        cur = conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))
        conn.commit()
        return {"ok": True, "removed": cur.rowcount or 0}
    finally:
        conn.close()


@router.get("/alerts/rules")
def list_rules() -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, name, kind, params, enabled, created_at FROM alert_rule ORDER BY id"
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["params"] = json.loads(item["params"] or "{}")
            except (ValueError, TypeError):
                item["params"] = {}
            item["enabled"] = bool(item["enabled"])
            item["hits"] = conn.execute(
                "SELECT COUNT(*) AS c FROM alert_hit WHERE rule_id = ?", (r["id"],)
            ).fetchone()["c"]
            out.append(item)
        return out
    finally:
        conn.close()


@router.post("/alerts/rules")
def add_rule(rule: AlertRuleIn) -> dict:
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO alert_rule(name, kind, params, enabled, created_at) VALUES (?,?,?,?,?)",
            (rule.name, rule.kind,
             json.dumps(rule.params, ensure_ascii=False),
             1 if rule.enabled else 0, db.now()),
        )
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}
    finally:
        conn.close()


@router.delete("/alerts/rules/{rule_id}")
def remove_rule(rule_id: int) -> dict:
    conn = db.connect()
    try:
        cur = conn.execute("DELETE FROM alert_rule WHERE id = ?", (rule_id,))
        conn.execute("DELETE FROM alert_hit WHERE rule_id = ?", (rule_id,))
        conn.commit()
        return {"ok": True, "removed": cur.rowcount or 0}
    finally:
        conn.close()


@router.get("/alerts/hits")
def list_hits(
    date: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
) -> list[dict]:
    """命中记录（默认最新交易日，倒序）。"""
    conn = db.connect()
    try:
        if date is None:
            row = conn.execute("SELECT MAX(date) AS d FROM alert_hit").fetchone()
            date = row["d"] if row and row["d"] else None
        if not date:
            return []
        rows = conn.execute(
            "SELECT h.date, h.symbol, h.detail, h.rule_id, r.name AS rule_name, "
            "       s.name AS name "
            "FROM alert_hit h "
            "LEFT JOIN alert_rule r ON r.id = h.rule_id "
            "LEFT JOIN security_master s ON s.symbol = h.symbol "
            "WHERE h.date = ? ORDER BY h.rule_id, h.symbol LIMIT ?",
            (date, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


#: 供前端展示的 kind 选项（与引擎保持同一真源）
@router.get("/alerts/kinds")
def list_kinds() -> list[str]:
    return list(ALERT_KINDS)
