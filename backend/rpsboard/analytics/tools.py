"""三个可直接使用的交易工具：仓位预算、往返成本、持仓压力测试。

它们都**复用同一套规则**（`analytics.costs.fee_decimal` 的费用公式与
`broker.trading_rules` 的申报数量），所以工具算出来的数量拿去下单不会被拒，
算出来的保本价与模拟盘记的账也对得上。

边界说明（都在页面上写明）：
- 仓位工具的「止损」只是**预算假设**，不是保证成交的止损单；
- 压力测试是**静态价格情景**，不是 VaR、也不是发生概率预测。
"""

from __future__ import annotations

import sqlite3
from datetime import date as _date
from decimal import Decimal

from ..broker import trading_rules as rules
from . import costs as costs_mod

#: 纯计算工具用**零滑点**费用模型：撮合价已经是明确的限价/开盘价，再记一次
#: 滑点等于把同一笔摩擦算两遍。
_MODEL = costs_mod.CostModel(enabled=True, slippage_rate=0.0)


class ToolError(ValueError):
    """输入越界（API 层转 422）。"""


class ToolStateError(ToolError):
    """输入没问题，但**资源状态**不满足（没有曲线/账户没有持仓）→ 409。"""


def _fee(notional: float, side: str, on: str) -> float:
    return float(costs_mod.fee_decimal(
        _MODEL, Decimal(repr(float(notional))), side=side,
        on=_date.fromisoformat(on)))


def _require_date(value: str) -> str:
    try:
        return _date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ToolError(f"trade_date 必须是 YYYY-MM-DD：{value!r}") from exc


def _require_price(value: float, field: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{field} 必须是数字") from exc
    if not (v > 0) or v != v or v in (float("inf"), float("-inf")):
        raise ToolError(f"{field} 必须是有限正数")
    if abs(v * 100 - round(v * 100)) > 1e-6:
        raise ToolError(f"{field} 必须对齐到 0.01 元")
    return round(v, 2)


def position_size(*, symbol: str, equity: float, cash: float, entry: float,
                  stop: float, risk_pct: float, max_weight: float,
                  trade_date: str) -> dict:
    """按「单笔最大亏损 = 权益 × risk_pct」反推可买数量。

    同时满足三条约束（**按分量化后**比较，与真实扣款一致）：
    1. 买入金额 + 买入费用 ≤ 可用现金；
    2. 买入金额 ≤ 权益 × 单票权重上限；
    3. 到止损价的亏损 + 买入费用 + 卖出费用 ≤ 权益 × risk_pct。

    数量必须落在该板块的合法申报网格上；连最小单位都买不起时返回 0 与原因。
    """
    try:
        rule = rules.quantity_rule(symbol)
    except rules.TradeRuleError as exc:
        raise ToolError(str(exc)) from exc
    day = _require_date(trade_date)
    entry = _require_price(entry, "entry")
    stop = _require_price(stop, "stop")
    if not (0 < stop < entry):
        raise ToolError("必须 0 < stop < entry（做多止损价低于买入价）")
    equity = float(equity)
    cash = float(cash)
    if equity <= 0 or equity != equity:
        raise ToolError("equity 必须是正数")
    if not (0 <= cash <= equity):
        raise ToolError("必须 0 ≤ cash ≤ equity")
    rp = float(risk_pct)
    mw = float(max_weight)
    if not (0 < rp <= 1):
        raise ToolError("risk_pct 必须在 (0, 1]")
    if not (0 < mw <= 1):
        raise ToolError("max_weight 必须在 (0, 1]")

    risk_budget = equity * rp
    step = rule["buy_step"]
    lo_qty = rule["buy_min"]

    def fits(q: int) -> bool:
        notional = q * entry
        buy_fee = _fee(notional, "buy", day)
        if round(notional + buy_fee, 2) > cash:
            return False
        if round(notional, 2) > round(equity * mw, 2):
            return False
        loss = q * (entry - stop)
        sell_fee = _fee(q * stop, "sell", day)
        return round(loss + buy_fee + sell_fee, 2) <= round(risk_budget, 2)

    # 上界：三个约束各自允许的最大数量取小（再向上取一格，二分区间要包含边界）
    cap = min(
        int(cash / entry) if entry else 0,
        int(equity * mw / entry) if entry else 0,
        int(risk_budget / (entry - stop)) if entry > stop else 0,
    )
    hi = max(lo_qty, cap + step)
    # 归到合法网格：q = lo_qty + k*step
    def qty_of(k: int) -> int:
        return lo_qty + k * step

    lo_k, hi_k = 0, max(0, (hi - lo_qty) // step)
    best_k = None
    while lo_k <= hi_k:
        mid = (lo_k + hi_k) // 2
        if fits(qty_of(mid)):
            best_k = mid
            lo_k = mid + 1
        else:
            hi_k = mid - 1

    if best_k is None:
        return {
            "volume": 0, "notional": 0.0, "buy_fee": 0.0, "stop_sell_fee": 0.0,
            "loss_at_stop": 0.0, "risk_budget": round(risk_budget, 2),
            "remaining_cash": round(cash, 2), "weight": 0.0,
            "reason": "below_minimum_quantity",
            "board": rule["board"], "board_label": rule["label"],
            "trade_date": day,
        }

    q = qty_of(best_k)
    notional = round(q * entry, 2)
    buy_fee = _fee(notional, "buy", day)
    sell_fee = _fee(q * stop, "sell", day)
    loss = round(q * (entry - stop) + buy_fee + sell_fee, 2)
    return {
        "volume": q,
        "notional": notional,
        "buy_fee": buy_fee,
        "stop_sell_fee": sell_fee,
        "loss_at_stop": loss,
        "risk_budget": round(risk_budget, 2),
        "remaining_cash": round(cash - notional - buy_fee, 2),
        "weight": round(notional / equity, 4),
        "reason": "ok",
        "board": rule["board"], "board_label": rule["label"],
        "trade_date": day,
    }


def round_trip(*, symbol: str, volume: int, buy_price: float, sell_price: float,
               trade_date: str) -> dict:
    """一笔往返的净盈亏与**保本价**。

    保本价在 0.01 的价格网格上二分查找：因为最低佣金（5 元）与印花税都不是
    线性的，用「差价 = 费用」的线性近似会算错一分钱。
    """
    try:
        rule = rules.quantity_rule(symbol)
    except rules.TradeRuleError as exc:
        raise ToolError(str(exc)) from exc
    day = _require_date(trade_date)
    buy_price = _require_price(buy_price, "buy_price")
    sell_price = _require_price(sell_price, "sell_price")
    if not isinstance(volume, int) or isinstance(volume, bool) or volume <= 0:
        raise ToolError("volume 必须是正整数")
    if volume < rule["buy_min"] or volume % rule["buy_step"] != 0:
        raise ToolError(
            f"{rule['label']}数量必须 ≥ {rule['buy_min']} 且为 {rule['buy_step']} 的整数倍")

    buy_notional = round(volume * buy_price, 2)
    sell_notional = round(volume * sell_price, 2)
    buy_fee = _fee(buy_notional, "buy", day)
    sell_fee = _fee(sell_notional, "sell", day)
    net = round(sell_notional - buy_notional - buy_fee - sell_fee, 2)
    cost_basis = buy_notional + buy_fee
    net_return = round(net / cost_basis, 6) if cost_basis else None

    def net_at(px: float) -> float:
        proceeds = round(volume * px, 2)
        return round(proceeds - buy_notional - buy_fee - _fee(proceeds, "sell", day), 2)

    # 上界：从买入价对应的分位开始倍增，直到净利非负（费率都远小于 1，必收敛）。
    lo_cents = int(round(buy_price * 100))
    hi_cents = max(lo_cents + 1, int(round(lo_cents * 1.5)) or lo_cents + 1)
    guard = 0
    while net_at(hi_cents / 100.0) < 0 and guard < 64:
        hi_cents = hi_cents * 2 - lo_cents
        guard += 1
    while lo_cents < hi_cents:
        mid = (lo_cents + hi_cents) // 2
        if net_at(mid / 100.0) >= 0:
            hi_cents = mid
        else:
            lo_cents = mid + 1
    breakeven = round(lo_cents / 100.0, 2)

    return {
        "volume": volume,
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "buy_fee": buy_fee,
        "sell_fee": sell_fee,
        "total_fees": round(buy_fee + sell_fee, 2),
        "net_pnl": net,
        "net_return": net_return,
        "breakeven_price": breakeven,
        "board": rule["board"], "board_label": rule["label"],
        "trade_date": day,
    }


def stress_test(conn: sqlite3.Connection, *, account_id: int,
                date: str | None = None,
                shocks: list[dict] | None = None) -> dict:
    """持仓的**静态价格压力**：给定各股涨跌幅，重算权益。

    只对价格做线性平移：`pnl = Σ 市值 × 冲击`，现金与已确认应收不变。
    缺口/未上市股份照样按其所属股票价格承压。
    """
    from ..broker import sim

    positions = sim.positions_at(conn, account_id=int(account_id),
                                 trade_date=date)
    nav = sim.nav_curve(conn, account_id=int(account_id), date=date, limit=1)
    if not nav:
        raise ToolStateError("该账户还没有净值曲线，无法做压力测试")
    base = nav[-1]
    held = {p["symbol"]: float(p["market_value"]) for p in positions}

    applied: dict[str, float] = {}
    for s in shocks or []:
        sym = str(s.get("symbol", "")).strip()
        if sym not in held:
            raise ToolError(f"{sym} 不是该账户在 {base['trade_date']} 的持仓")
        if sym in applied:
            raise ToolError(f"{sym} 重复给出冲击")
        try:
            r = float(s.get("return"))
        except (TypeError, ValueError) as exc:
            raise ToolError(f"{sym} 的 return 必须是数字") from exc
        if not (-1.0 <= r <= 10.0) or r != r:
            raise ToolError(f"{sym} 的 return 必须在 [-1, 10]")
        applied[sym] = r

    rows = []
    pnl = 0.0
    for sym, mv in sorted(held.items()):
        r = applied.get(sym, 0.0)
        delta = round(mv * r, 2)
        pnl += delta
        rows.append({"symbol": sym, "market_value": mv, "shock": r,
                     "pnl": delta})
    pnl = round(pnl, 2)
    before = round(float(base["total"]), 2)
    after = round(before + pnl, 2)
    return {
        "account_id": int(account_id),
        "date": base["trade_date"],
        "equity_before": before,
        "equity_after": after,
        "pnl": pnl,
        "return": round(pnl / before, 6) if before else None,
        "positions": rows,
    }
