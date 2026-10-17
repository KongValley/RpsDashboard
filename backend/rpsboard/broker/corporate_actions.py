"""公司行为（权息）的**账户入账方案**：核对、校验、按日结算。

分工很清楚：
- `sources/corporate_src.py` 只负责**发现**事件（接口只给日期/类别/四个业务
  字段，既没有登记日、也没有到账日/可卖日，更算不出个人分红税）；
- 本模块把它变成**可执行的入账时间表** —— 用户按公告核对后填 `resolution`，
  系统只按填好的日期加减现金、股数、解锁、成本与应收/应付。

所以这里的原则是：**宁可停下来问，也不猜**。缺核对就返回阻断原因，账户停在
那一天之前；绝不把「未知税额」当 0，也绝不把复权系数当代替真实现金流水。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date as _date

#: 会改变**持有人权益**、必须逐条核对的类别（见 7709-股本变迁GBBQ.md 类别表）。
#: 1=除权除息（分红/送转/配股）、11=扩缩股、12=非流通股缩股、15=重整调整。
ACTION_CATEGORIES = (1, 11, 12, 15)
#: 描述整体股本/上市信息的类别（不按每条事件给持有人加股；解锁走核对的
#: shares_available_date）。2/3/5/6/7/8/9/10。
CAPITAL_CATEGORIES = (2, 3, 5, 6, 7, 8, 9, 10)
#: 当前现金股票模型**无法表达**的类别：4=未知股本变动、13/14=权证。
UNSUPPORTED_CATEGORIES = (4, 13, 14)

RESOLUTION_FIELDS = (
    "record_date", "effective_date", "cash_gross", "cash_pay_date",
    "share_delta", "share_unlocks", "subscription_cash", "subscription_pay_date",
    "taxes", "taxes_checked_through", "source_note",
)


class CorporateActionError(RuntimeError):
    """核对方案非法（调用方转 422/409）。"""


def _iso_or_none(v, field: str) -> str | None:
    if v in (None, ""):
        return None
    try:
        return _date.fromisoformat(str(v)).isoformat()
    except ValueError as exc:
        raise CorporateActionError(f"{field} 必须是 YYYY-MM-DD") from exc


def _money(v, field: str) -> float:
    if v in (None, ""):
        return 0.0
    try:
        f = float(v)
    except (TypeError, ValueError) as exc:
        raise CorporateActionError(f"{field} 必须是数字") from exc
    if f != f or f in (float("inf"), float("-inf")):
        raise CorporateActionError(f"{field} 必须是有限数字")
    return round(f, 2)


def _shares(v, field: str) -> int:
    if v in (None, ""):
        return 0
    if isinstance(v, bool) or not isinstance(v, int):
        try:
            f = float(v)
        except (TypeError, ValueError) as exc:
            raise CorporateActionError(f"{field} 必须是整数股数") from exc
        if f != int(f):
            raise CorporateActionError(f"{field} 必须是整数股数")
        v = int(f)
    return int(v)


def normalize_resolution(resolution: dict) -> dict:
    """把用户填的方案规范化成固定结构（缺项补 0/None，非法直接拒绝）。"""
    if not isinstance(resolution, dict):
        raise CorporateActionError("resolution 必须是对象")
    unknown = set(resolution) - set(RESOLUTION_FIELDS)
    if unknown:
        raise CorporateActionError(f"未知字段：{sorted(unknown)}")

    record_date = _iso_or_none(resolution.get("record_date"), "record_date")
    effective_date = _iso_or_none(resolution.get("effective_date"), "effective_date")
    if not record_date or not effective_date:
        raise CorporateActionError("record_date 与 effective_date 必填")
    if record_date > effective_date:
        raise CorporateActionError("record_date 不能晚于 effective_date")

    cash_gross = _money(resolution.get("cash_gross"), "cash_gross")
    subscription_cash = _money(resolution.get("subscription_cash"),
                               "subscription_cash")
    if cash_gross < 0 or subscription_cash < 0:
        raise CorporateActionError("现金金额不能为负（方向由字段语义决定）")

    share_delta = _shares(resolution.get("share_delta"), "share_delta")

    unlocks_raw = resolution.get("share_unlocks") or []
    if not isinstance(unlocks_raw, list):
        raise CorporateActionError("share_unlocks 必须是数组")
    unlocks = []
    for item in unlocks_raw:
        if not isinstance(item, dict):
            raise CorporateActionError("share_unlocks 的每一项必须是对象")
        kind = item.get("kind")
        if kind not in ("bonus", "subscription"):
            raise CorporateActionError("share_unlocks.kind 只能是 bonus/subscription")
        vol = _shares(item.get("volume"), "share_unlocks.volume")
        if vol <= 0:
            raise CorporateActionError("share_unlocks.volume 必须为正")
        avail = _iso_or_none(item.get("available_date"), "share_unlocks.available_date")
        if not avail:
            raise CorporateActionError("share_unlocks.available_date 必填")
        if avail < effective_date:
            raise CorporateActionError("股份解锁日不能早于生效日")
        unlocks.append({"kind": kind, "volume": vol, "available_date": avail})

    if share_delta > 0:
        if sum(u["volume"] for u in unlocks) != share_delta:
            raise CorporateActionError(
                "share_delta 为正时，各解锁批次数量之和必须等于 share_delta")
    elif unlocks:
        raise CorporateActionError("share_delta ≤ 0 时不应有解锁批次")
    if share_delta < 0:
        raise CorporateActionError("负变动（缩股）当前不支持，请用 11/12 类别的核对表")

    taxes_raw = resolution.get("taxes") or []
    if not isinstance(taxes_raw, list):
        raise CorporateActionError("taxes 必须是数组")
    taxes = []
    for item in taxes_raw:
        if not isinstance(item, dict):
            raise CorporateActionError("taxes 的每一项必须是对象")
        d = _iso_or_none(item.get("date"), "taxes.date")
        amt = _money(item.get("amount"), "taxes.amount")
        if not d:
            raise CorporateActionError("taxes.date 必填")
        if amt < 0:
            raise CorporateActionError("taxes.amount 不能为负")
        taxes.append({"date": d, "amount": amt})

    note = str(resolution.get("source_note") or "").strip()
    if not 1 <= len(note) <= 1000:
        raise CorporateActionError("source_note 必须是 1–1000 个字符")

    cash_pay = _iso_or_none(resolution.get("cash_pay_date"), "cash_pay_date")
    sub_pay = _iso_or_none(resolution.get("subscription_pay_date"),
                           "subscription_pay_date")
    for label, d in (("cash_pay_date", cash_pay), ("subscription_pay_date", sub_pay)):
        if d and d < effective_date:
            # 认购缴款可以早于生效日（预付款），但分红到账不能早于除权。
            if label == "cash_pay_date":
                raise CorporateActionError("分红到账日不能早于生效日")
    if cash_gross > 0 and not cash_pay:
        raise CorporateActionError("有现金分红时必须填到账日（不能默认不用到账）")

    taxes_checked = _iso_or_none(resolution.get("taxes_checked_through"),
                                 "taxes_checked_through")

    return {
        "record_date": record_date,
        "effective_date": effective_date,
        "cash_gross": cash_gross,
        "cash_pay_date": cash_pay,
        "share_delta": share_delta,
        "share_unlocks": unlocks,
        "subscription_cash": subscription_cash,
        "subscription_pay_date": sub_pay,
        "taxes": taxes,
        "taxes_checked_through": taxes_checked,
        "source_note": note,
    }


# ── 事件发现 / 待核对 ───────────────────────────────────────────────
def _event_category(conn: sqlite3.Connection, symbol: str, event_date: str,
                    category: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM corporate_event WHERE symbol=? AND event_date=? AND category=?",
        (symbol, event_date, int(category))).fetchone()
    return dict(row) if row else None


def _action_row(conn: sqlite3.Connection, account_id: int, symbol: str,
                event_date: str, category: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_action WHERE account_id=? AND symbol=? AND event_date=?"
        " AND category=?",
        (int(account_id), symbol, event_date, int(category))).fetchone()
    return dict(row) if row else None


def resolve_action(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    symbol: str,
    event_date: str,
    category: int,
    resolution: dict,
    last_settled: str | None = None,
) -> dict:
    """登记一条**已核对**的账户权益方案。

    校验要点：
    - 必须对得上一条已采集的事件（不能凭空造事件）；
    - 生效日/到账日/解锁日/扣税日等**记账日期**都必须晚于账户最后结算日 ——
      已经结算过的日子不能被追溯改写（那会让已公布的净值与流水对不上）；
      登记日只作取证，不受这条限制（先结算到登记日收盘是调用方的责任）；
    - 重复提交同一内容返回原记录；已生效的方案不可改。
    """
    if category in UNSUPPORTED_CATEGORIES or category not in ACTION_CATEGORIES:
        raise CorporateActionError(
            f"类别 {category} 无法用现金股票模型表达，请另建账户或人工记录")
    ev = _event_category(conn, symbol, event_date, category)
    if ev is None:
        raise CorporateActionError(
            f"没有找到 {symbol} {event_date} 类别 {category} 的事件，不能凭空登记")
    norm = normalize_resolution(resolution)

    existing = _action_row(conn, account_id, symbol, event_date, category)
    payload = json.dumps(norm, ensure_ascii=False)
    if existing is not None:
        if existing["resolution"] == payload:
            return {"ok": True, "unchanged": True,
                    "action": {**existing, "resolution": norm}}
        raise CorporateActionError("该事件的权益方案已登记，已生效内容不可修改")

    # 记账日期必须晚于最后结算日（历史不可追溯改写）。
    if last_settled:
        for label, d in (("effective_date", norm["effective_date"]),
                         ("cash_pay_date", norm["cash_pay_date"]),
                         ("subscription_pay_date", norm["subscription_pay_date"]),
                         ("taxes_checked_through", norm["taxes_checked_through"])):
            if d and d <= last_settled:
                raise CorporateActionError(
                    f"{label}={d} 不晚于账户最后结算日 {last_settled}，"
                    "历史不能追溯改写；请从最早生效日前新建账户重跑")
        for u in norm["share_unlocks"]:
            if u["available_date"] <= last_settled:
                raise CorporateActionError(
                    f"解锁日 {u['available_date']} 不晚于最后结算日 {last_settled}")
        for t in norm["taxes"]:
            if t["date"] <= last_settled:
                raise CorporateActionError(
                    f"扣税日 {t['date']} 不晚于最后结算日 {last_settled}")

    conn.execute(
        "INSERT INTO paper_action(account_id, symbol, event_date, category, resolution)"
        " VALUES (?,?,?,?,?)",
        (int(account_id), symbol, event_date, int(category), payload))
    conn.commit()
    return {"ok": True, "unchanged": False,
            "action": {"account_id": int(account_id), "symbol": symbol,
                       "event_date": event_date, "category": int(category),
                       "resolution": norm}}


def unresolved_events(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    symbols: set[str],
    through: str,
) -> list[dict]:
    """在 `through`（含）之前**已发生但未核对**的、影响 `symbols` 的事件。

    只看这些股票：没持有过、也没有待交易标的的历史事件不该阻断账户。
    已登记方案的股票还要检查「税款核对截止日」是否落后于权益生效日。
    """
    if not symbols:
        return []
    marks = ",".join("?" * len(symbols))
    rows = conn.execute(
        f"SELECT symbol, event_date, category, payload FROM corporate_event"
        f" WHERE symbol IN ({marks}) AND event_date <= ? ORDER BY event_date",
        (*sorted(symbols), through)).fetchall()
    out: list[dict] = []
    for r in rows:
        cat = int(r["category"])
        if cat in CAPITAL_CATEGORIES:
            continue                      # 股本/上市信息，不给持有人加股
        if cat not in ACTION_CATEGORIES:
            out.append({"symbol": r["symbol"], "event_date": r["event_date"],
                        "category": cat, "reason": "unsupported",
                        "payload": r["payload"]})
            continue
        act = _action_row(conn, account_id, r["symbol"], r["event_date"], cat)
        if act is None:
            out.append({"symbol": r["symbol"], "event_date": r["event_date"],
                        "category": cat, "reason": "unresolved",
                        "payload": r["payload"]})
            continue
        res = json.loads(act["resolution"])
        if res.get("cash_gross", 0) > 0 or res.get("taxes"):
            checked = res.get("taxes_checked_through")
            if not checked or checked < res["effective_date"]:
                out.append({"symbol": r["symbol"], "event_date": r["event_date"],
                            "category": cat, "reason": "tax_unresolved",
                            "payload": r["payload"]})
    return out


# ── 按日结算效果 ────────────────────────────────────────────────────
def effects_for_day(conn: sqlite3.Connection, *, account_id: int,
                    day: str) -> dict:
    """该账户在 `day` 这一天到期的公司行为效果（每天最多应用一次）。

    返回值里的每个字段都直接由 resolution 的日期推导，**没有隐藏状态**：
    只要日期不重复处理，就不会重复入账。
    """
    out: dict = {
        "cash_delta": 0.0,          # 现金变动（到账 + / 缴款、扣税 -）
        "receivable_delta": 0.0,    # 应收/应付净额变动
        "income_delta": 0.0,        # 已确认分红收入（统计口径，不直接进现金）
        "shares_delta": {},         # symbol → 总持股变动
        "lock_delta": {},           # symbol → 新增锁定股数（生效日锁住）
        "unlock_delta": {},         # symbol → 可卖量增量
        "cost_delta": {},           # symbol → 总成本余额增量（认购款）
        "notes": [],
    }
    rows = conn.execute(
        "SELECT * FROM paper_action WHERE account_id = ?", (int(account_id),)
    ).fetchall()
    for r in rows:
        res = json.loads(r["resolution"])
        sym = r["symbol"]
        eff = res["effective_date"]

        if day == eff:
            if res["share_delta"]:
                out["shares_delta"][sym] = out["shares_delta"].get(sym, 0) \
                    + int(res["share_delta"])
                # 新增股在生效日**全部先锁住**，由各自的解锁日分批放行 ——
                # 只记解锁而不记锁定，会让送转股当天就变成可卖。
                out["lock_delta"][sym] = out["lock_delta"].get(sym, 0) \
                    + res["share_delta"]
            if res["cash_gross"] > 0:
                # 除权日先确认应收，到账日才转现金 —— 否则除息日会凭空「亏」一笔。
                out["receivable_delta"] += res["cash_gross"]
                out["income_delta"] += res["cash_gross"]
            if res["subscription_cash"] > 0:
                out["cost_delta"][sym] = out["cost_delta"].get(sym, 0.0) \
                    + res["subscription_cash"]
                if not res["subscription_pay_date"] or \
                        res["subscription_pay_date"] > eff:
                    out["receivable_delta"] -= res["subscription_cash"]
            out["notes"].append(f"{sym} 权益生效（类别 {r['category']}）")

        for u in res["share_unlocks"]:
            if day == u["available_date"]:
                out["unlock_delta"][sym] = out["unlock_delta"].get(sym, 0) \
                    + int(u["volume"])

        if res["cash_gross"] > 0 and res["cash_pay_date"] == day:
            out["cash_delta"] += res["cash_gross"]
            out["receivable_delta"] -= res["cash_gross"]
            out["notes"].append(f"{sym} 分红到账")

        if res["subscription_cash"] > 0 and res["subscription_pay_date"] == day:
            out["cash_delta"] -= res["subscription_cash"]
            if res["subscription_pay_date"] < eff:
                out["receivable_delta"] += res["subscription_cash"]  # 预付款
            else:
                out["receivable_delta"] += res["subscription_cash"]  # 冲销应付
            out["notes"].append(f"{sym} 认购缴款")

        for t in res["taxes"]:
            if t["date"] == day:
                out["cash_delta"] -= t["amount"]
                out["income_delta"] -= t["amount"]
                out["notes"].append(f"{sym} 扣分红税 {t['amount']:.2f}")
    return out


def list_actions(conn: sqlite3.Connection, account_id: int,
                 symbols: set[str] | None = None) -> list[dict]:
    """该账户已登记的公司行为方案（含解析后的 resolution）。"""
    rows = conn.execute(
        "SELECT * FROM paper_action WHERE account_id = ? ORDER BY event_date",
        (int(account_id),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["resolution"] = json.loads(d["resolution"])
        if symbols and d["symbol"] not in symbols:
            continue
        out.append(d)
    return out


def health(conn: sqlite3.Connection, account_id: int) -> dict:
    """账户的公司行为数据状态（页面用）：发现多少、核对多少、还缺什么。"""
    nav = conn.execute(
        "SELECT MAX(trade_date) AS d FROM paper_nav WHERE account_id=?",
        (int(account_id),)).fetchone()
    latest = nav["d"] if nav and nav["d"] else None
    sync = conn.execute("SELECT MIN(checked_through) AS lo FROM corporate_sync"
                        ).fetchone()
    return {
        "checked_through": sync["lo"] if sync and sync["lo"] else None,
        "latest_date": latest,
        "actions": len(list_actions(conn, account_id)),
    }
