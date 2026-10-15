"""发单前的硬性风控（P3 券商模拟盘）。

这里全是**阻断型**规则：任何一条命中就整批不发单。理由是模拟盘的意义在于
「验证执行链路」，而不是「尽快把单子塞进去」—— 放宽任何一条，测出来的都
是不受控的中间态。

规则里**刻意没有绕过开关**（没有 `--force`、没有 `--now`）：
测试时段靠 pytest 注入时间，用户侧想验证就改系统时间或等真实时段。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, time

from .. import config
from . import qmt

#: 开盘后 5 分钟才允许发单：集合竞价刚结束时的价格没有参考价值，
#: 且这 5 分钟正好够 xtquant 完成登录与回报同步。
TRADING_SESSIONS = ((time(9, 35), time(11, 30)), (time(13, 0), time(14, 57)))

#: 单笔金额下限：低于它，最小佣金（5 元）占成交额的比例会失控，
#: 这类单子直接剔除而不是发出去。
MIN_ORDER_AMOUNT = 5_000.0

#: 现金占用上限（可用现金的 95%，留 5% 给佣金）
CASH_USABLE = 0.95

#: 单票金额上限（计划总预算的 20%）
MAX_NAME_WEIGHT = 0.2

#: 单批最多 60 笔；当日换手金额不超过计划总预算的 4 倍
MAX_ORDERS = 60
MAX_TURNOVER = 4.0


def in_trading_hours(now: datetime) -> bool:
    t = now.time()
    return any(lo <= t <= hi for lo, hi in TRADING_SESSIONS)


def halted() -> bool:
    return config.TRADE_HALT_FILE.exists()


def order_amount(item: dict) -> float:
    return float(item.get("plan_price") or 0.0) * int(item.get("target_volume") or 0)


def actionable(items: list[dict]) -> list[dict]:
    """真正要发的那批：剔除金额不足的小单（其余票照发，不整批阻断）。"""
    return [it for it in items
            if it.get("reason") == "ok" and it.get("side") in ("buy", "sell")
            and order_amount(it) >= MIN_ORDER_AMOUNT]


def check(ctx: dict, plan: dict, items: list[dict], *, now: datetime) -> list[str]:
    """返回阻断原因列表；**非空即拒绝整批发单**。"""
    reasons: list[str] = []
    todo = actionable(items)
    asset = ctx.get("asset") or {}

    if halted():
        reasons.append("halted")
    if plan.get("mode") != "sim":
        reasons.append("mode_not_sim")
    if not in_trading_hours(now):
        reasons.append("outside_trading_hours")

    buys = [it for it in todo if it["side"] == "buy"]
    buy_amount = sum(order_amount(it) for it in buys)
    cash = float(asset.get("cash") or 0.0)
    if buy_amount > cash * CASH_USABLE:
        reasons.append("insufficient_cash")

    budget = float(plan.get("cash_budget") or 0.0)
    if budget > 0 and any(order_amount(it) > budget * MAX_NAME_WEIGHT for it in todo):
        reasons.append("per_name_too_large")
    if len(todo) > MAX_ORDERS:
        reasons.append("too_many_orders")
    if budget > 0 and sum(order_amount(it) for it in todo) > budget * MAX_TURNOVER:
        reasons.append("turnover_too_high")
    return reasons


def reconcile_positions(
    conn: sqlite3.Connection, plan_id: int, live: list[dict] | None = None,
    *, against: str = "target",
) -> list[dict]:
    """对账：计划涉及的票，现持与某个基准差多少。

    `against="held"` → 与**建计划那一刻**的持股数比。差额非 0 说明账户在
    计划生成之后被外部动过（手工买卖、其它脚本），此时发单会把两个互相
    不知情的意图叠在一起，因此 `execute` 拿它当**阻断条件**。

    `against="target"` → 与**绝对目标**比。差额是「还差多少没调到位」，
    用于 `cli trade reconcile` 与页面展示，不阻断。

    两种口径都只比对计划里的票：不在计划里的持仓差异常年存在（用户自己
    买的），算进来会让对账永远红着，真信号被淹没。
    """
    from .plan import load_plan

    _plan, items = load_plan(conn, plan_id)
    if live is None:
        live = qmt.positions() or []
    held = {p["symbol"]: int(p.get("volume") or 0) for p in live}
    diffs = []
    for it in items:
        if it["reason"] != "ok" or not it["plan_price"]:
            continue
        have = held.get(it["symbol"], 0)
        ref = int(it["held"] or 0) if against == "held" else int(it["target_volume"] or 0)
        if have != ref:
            diffs.append({
                "symbol": it["symbol"], "side": it["side"], "have": have,
                "reference": ref, "delta": ref - have,
            })
    return diffs
