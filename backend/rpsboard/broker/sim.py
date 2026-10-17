"""自建模拟盘：**多账户、原价（不复权）日线、逐笔费用**的撮合与记账引擎。

与旧实现的区别（都是实测出来的坑）：

1. **不再共用 `trade_order/trade_fill`**。旧代码的 `reset_account` 会
   `DELETE FROM trade_order/trade_fill`（没有 WHERE），把 QMT 与影子的成交
   一起删掉。本地流水现在在 `paper_order/paper_fill` 里，物理隔离。
2. **成交按日追加**。旧代码写 `trade_fill` 时把 `broker_trade_id` 恒设为 1，
   主键又是 `(broker_order_id, broker_trade_id)` —— 跨日部分成交时第二天的
   `INSERT OR REPLACE` 直接把第一天的行盖掉（费用与已实现盈亏随之丢失）。
   现在一天一行、`UNIQUE(order_id, trade_date)`，只追加不覆盖。
3. **原价记账**。旧代码用后复权收盘价当钱（实测同一只票 hfq/raw 平均差 8.8 倍），
   股数、市值、5 元最低佣金的临界点全在错的价位上。现在成交与估值都用
   `raw_bar`。
4. **参与率按股**。旧代码拿 SDK 的 `volume_lots`（手）当股用，误差 100 倍。

撮合规则（日线近似，**不承诺复原盘中路径与涨跌停排队**）：
先卖后买；买单价 ≥ 当日开盘则按开盘成交，否则当日最低价触及限价则按限价成交；
卖单对称。封在涨停板上买不进、封在跌停板上卖不掉（顺延），限价一整天没被
触及就撤掉剩余量。
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date as _date
from decimal import ROUND_HALF_UP, Decimal

from .. import db
from ..analytics import costs as costs_mod
from . import accounts as accounts_mod
from . import corporate_actions as corporate_mod
from . import trading_rules as rules

#: 委托价相对参考价的偏移（买 +、卖 −）。与 plan.PRICE_OFFSET 同源。
PRICE_OFFSET = 0.005

#: 一手 = 100 股（A 股统一口径，数量规则的兜底值）。
LOT = 100

#: 单个交易日最多撮合几笔（防御性上限，正常远小于此）
MAX_ORDERS_PER_DAY = 200

#: `summary()`/`nav_curve()` 一次最多返回多少个净值点（约 8 年交易日）。
NAV_CURVE_LIMIT = 2000

#: 撮合/查询用到的委托状态。`deferred` 是**内部**状态（当天被停牌/涨停挡住，
#: 顺延重试），对外一律按 `pending` 呈现 —— 见 `_public_status`。
OPEN_STATUSES = ("pending", "deferred", "part")
TERMINAL_STATUSES = ("filled", "canceled", "rejected")
#: 供 SQL 直接内联的状态清单（常量，无注入面）。
_OPEN_SQL = "('pending','deferred','part')"

#: 销售价格保留 2 位、金额按分取整的量化器。
_CENT = Decimal("0.01")

#: 记账用的唯一费用模型：**零滑点**。raw 撮合的成交价已经是明确的限价/开盘价，
#: 再记一次滑点等于把同一笔摩擦算两遍。冻结、下单校验、撮合三处必须同源，
#: 否则会出现「校验说够、结算说不够」这类自相矛盾。
_MODEL = costs_mod.CostModel(enabled=True, slippage_rate=0.0)


class SimError(RuntimeError):
    """模拟盘状态非法（账户不存在/参数越界/数据缺口等）。"""


class DataBlocked(SimError):
    """数据缺口挡住推进：账户停在安全位置，`reason` 是给用户的精确原因。"""

    def __init__(self, reason: str, missing: list[dict] | None = None,
                 day: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.missing = missing or []
        self.day = day


def _q(value) -> float:
    """金额按分四舍五入（记账口径统一走这里）。"""
    return float(Decimal(repr(float(value))).quantize(_CENT, rounding=ROUND_HALF_UP))


@dataclass
class Bar:
    """某交易日的不复权日 K。"""
    symbol: str
    open: float
    high: float
    low: float
    close: float
    tradable: bool = True
    volume: float = 0.0


@dataclass
class Account:
    """模拟账户的内存态（以某个结算日收盘为准）。"""
    account_id: int
    cash: float
    positions: dict[str, int] = field(default_factory=dict)
    #: 锁定股数（公司行为新增、尚未到可卖日）
    locked: dict[str, int] = field(default_factory=dict)
    #: symbol → 总成本余额（元，含费用）
    cost_basis: dict[str, float] = field(default_factory=dict)
    #: 已确认但未到账的净权益（分红为正、应付认购款为负）
    receivable: float = 0.0
    #: 当日的可卖量（仅结算过程中有意义）
    available: dict[str, int] = field(default_factory=dict)

    def refresh_available(self, extra: dict[str, int] | None = None) -> None:
        """重算可卖量 = 总持股 − 锁定（+ 本日解锁量）。"""
        avail = {}
        for sym, vol in self.positions.items():
            avail[sym] = max(0, vol - self.locked.get(sym, 0))
        for sym, add in (extra or {}).items():
            avail[sym] = avail.get(sym, 0) + int(add)
        self.available = avail

    def snapshot_positions(self) -> dict[str, int]:
        return dict(self.positions)


# ── 交易日历 ────────────────────────────────────────────────────────
def trading_days(conn: sqlite3.Connection, start: str | None = None,
                 end: str | None = None) -> list[str]:
    """市场交易日：上证指数（benchmark_bar 000001）的日期序列，升序。

    刻意不用 `daily_bar` 的日期：个股停牌会让某些票的行情比市场晚一天，
    而「下一个市场交易日」必须按市场走。
    """
    sql = "SELECT date FROM benchmark_bar WHERE symbol='000001'"
    params: list = []
    if start:
        sql += " AND date >= ?"
        params.append(start)
    if end:
        sql += " AND date <= ?"
        params.append(end)
    sql += " ORDER BY date"
    return [r["date"] for r in conn.execute(sql, tuple(params))]


def next_market_day(conn: sqlite3.Connection, after: str) -> str | None:
    row = conn.execute(
        "SELECT date FROM benchmark_bar WHERE symbol='000001' AND date > ?"
        " ORDER BY date LIMIT 1", (after,)).fetchone()
    return row["date"] if row else None


def _prev_calendar_day(days: list[str], day: str) -> str | None:
    idx = days.index(day)
    return days[idx - 1] if idx > 0 else None


# ── 行情取值 ────────────────────────────────────────────────────────
def _day_bars(conn: sqlite3.Connection, day: str) -> dict[str, Bar]:
    """当天**可成交**的不复权日 K（tradestatus=1 才进得来）。"""
    out: dict[str, Bar] = {}
    for r in conn.execute(
            "SELECT symbol, open, high, low, close, volume, tradestatus"
            " FROM raw_bar WHERE date = ?", (day,)):
        if int(r["tradestatus"] or 0) != 1:
            continue
        out[r["symbol"]] = Bar(
            symbol=r["symbol"], open=float(r["open"]), high=float(r["high"]),
            low=float(r["low"]), close=float(r["close"]), tradable=True,
            volume=float(r["volume"] or 0.0))
    return out


def raw_close(conn: sqlite3.Connection, symbol: str, day: str) -> float | None:
    """当日可用的不复权收盘价（不可用返回 None，**不回退**到历史价）。"""
    row = conn.execute(
        "SELECT close, tradestatus FROM raw_bar WHERE symbol=? AND date=?",
        (symbol, day)).fetchone()
    if row and int(row["tradestatus"] or 0) == 1 and row["close"]:
        return float(row["close"])
    return None


def last_valid_close(conn: sqlite3.Connection, symbol: str,
                     day: str) -> tuple[float, str] | None:
    """`day`（含）之前最近一次可用的不复权收盘价与它的日期。"""
    row = conn.execute(
        "SELECT close, date FROM raw_bar WHERE symbol=? AND date<=?"
        " AND tradestatus=1 AND close>0 ORDER BY date DESC LIMIT 1",
        (symbol, day)).fetchone()
    return (float(row["close"]), row["date"]) if row else None


def _valuation(conn: sqlite3.Connection, day: str,
               symbols: list[str]) -> dict[str, tuple[float, str] | None]:
    """估值价：优先当日可用原价，否则沿用最近有效原价（停牌），都没有返回 None。"""
    out: dict[str, tuple[float, str] | None] = {}
    for sym in symbols:
        close = raw_close(conn, sym, day)
        if close is not None:
            out[sym] = (close, day)
        else:
            out[sym] = last_valid_close(conn, sym, day)
    return out


def _limits(conn: sqlite3.Connection, day: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT symbol, limit_ratio, limit_up, limit_down, status, rule"
            " FROM price_limit_daily WHERE date = ?", (day,)):
        out[r["symbol"]] = dict(r)
    return out


def _suspended_confirmed(conn: sqlite3.Connection, symbol: str, day: str) -> bool:
    if not db.table_exists(conn, "suspension_confirmation"):
        return False
    return conn.execute(
        "SELECT 1 FROM suspension_confirmation WHERE symbol=? AND date=?",
        (symbol, day)).fetchone() is not None


# ── 账户读写 ────────────────────────────────────────────────────────
def _nav_row(conn: sqlite3.Connection, account_id: int, date: str | None) -> dict | None:
    if date is None:
        row = conn.execute(
            "SELECT * FROM paper_nav WHERE account_id=? ORDER BY trade_date DESC LIMIT 1",
            (account_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM paper_nav WHERE account_id=? AND trade_date<=?"
            " ORDER BY trade_date DESC LIMIT 1", (account_id, date)).fetchone()
    return dict(row) if row else None


def load_account(conn: sqlite3.Connection, *, account_id: int,
                 date: str | None = None) -> Account:
    """还原某日**收盘后**的账户（默认最新结算日）。

    现金取自那一天的净值行（不是 `paper_account.cash` —— 那是**当前**余额，
    拿它去配历史持仓会算出一个从未存在过的账户）。
    """
    acc_row = conn.execute("SELECT * FROM paper_account WHERE id=?",
                           (int(account_id),)).fetchone()
    if acc_row is None:
        raise SimError(f"模拟账户 {account_id} 不存在")
    nav = _nav_row(conn, int(account_id), date)
    state = Account(account_id=int(account_id),
                    cash=float(nav["cash"]) if nav else float(acc_row["cash"]),
                    receivable=float(nav["receivable"]) if nav else 0.0)
    if nav:
        for r in conn.execute(
                "SELECT symbol, volume, available_volume, cost_basis"
                " FROM paper_position WHERE account_id=? AND trade_date=?",
                (int(account_id), nav["trade_date"])):
            vol = int(r["volume"] or 0)
            if vol <= 0:
                continue
            state.positions[r["symbol"]] = vol
            avail = int(r["available_volume"] or 0)
            state.locked[r["symbol"]] = max(0, vol - avail)
            state.cost_basis[r["symbol"]] = float(r["cost_basis"] or 0.0)
    state.refresh_available()
    return state


def positions_at(conn: sqlite3.Connection, *, account_id: int,
                 trade_date: str | None = None) -> list[dict]:
    """某日收盘持仓（默认最新）。"""
    if trade_date is None:
        row = conn.execute(
            "SELECT MAX(trade_date) AS d FROM paper_nav WHERE account_id=?",
            (int(account_id),)).fetchone()
        trade_date = row["d"] if row and row["d"] else None
    if not trade_date:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM paper_position WHERE account_id=? AND trade_date=?"
        " ORDER BY symbol", (int(account_id), trade_date))]


def nav_curve(conn: sqlite3.Connection, *, account_id: int,
              date: str | None = None, limit: int | None = None) -> list[dict]:
    """净值曲线（升序）。`date` 给定则截到那一天。"""
    sql = "SELECT * FROM paper_nav WHERE account_id=?"
    params: list = [int(account_id)]
    if date:
        sql += " AND trade_date <= ?"
        params.append(date)
    sql += " ORDER BY trade_date"
    if limit:
        # 取**最近** N 行（曲线越长越要看尾部），但仍按升序返回。
        sql = f"SELECT * FROM ({sql} DESC LIMIT ?) ORDER BY trade_date"
        params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, tuple(params))]


def last_settled_date(conn: sqlite3.Connection, account_id: int) -> str | None:
    row = conn.execute(
        "SELECT MAX(trade_date) AS d FROM paper_nav WHERE account_id=?",
        (int(account_id),)).fetchone()
    return row["d"] if row and row["d"] else None


def _initial_cash(conn: sqlite3.Connection, account_id: int) -> float:
    acc = conn.execute("SELECT initial_cash FROM paper_account WHERE id=?",
                       (int(account_id),)).fetchone()
    return float(acc["initial_cash"]) if acc else 0.0


def summary(conn: sqlite3.Connection, *, account_id: int,
            date: str | None = None) -> dict:
    """一屏摘要：初始资金、当前净值、区间收益、最大回撤、持仓数。

    **统计用完整曲线**（`curve` 字段只是给图用的抽样窗口）：把区间收益/回撤
    算在前 2000 个点上，会得出一个「不是这个账户」的数字。图截断时用
    `curve_truncated` + 统计区间说明。
    """
    full = nav_curve(conn, account_id=account_id, date=date)
    base = {"initial_cash": 0.0, "total": 0.0, "nav": 1.0, "return": 0.0,
            "max_drawdown": 0.0, "days": 0, "from": None, "to": None,
            "positions": [], "curve": [], "curve_limit": NAV_CURVE_LIMIT,
            "curve_truncated": False, "receivable": 0.0,
            "cash": 0.0, "market_value": 0.0,
            "stats_from": None, "stats_to": None, "stats_days": 0}
    if not full:
        return base
    first, last = full[0], full[-1]
    peak, mdd = 0.0, 0.0
    for r in full:
        peak = max(peak, float(r["total"]))
        if peak > 0:
            mdd = max(mdd, 1 - float(r["total"]) / peak)
    truncated = len(full) > NAV_CURVE_LIMIT
    chart = full[-NAV_CURVE_LIMIT:] if truncated else full
    return {
        "initial_cash": round(float(last["total"]) / (float(last["nav"]) or 1.0), 2),
        "total": round(float(last["total"]), 2),
        "nav": round(float(last["nav"]), 4),
        "return": round(float(last["nav"]) - 1, 4),
        "max_drawdown": round(mdd, 4),
        "days": len(full),
        "from": first["trade_date"], "to": last["trade_date"],
        "cash": round(float(last["cash"]), 2),
        "market_value": round(float(last["market_value"]), 2),
        "receivable": round(float(last["receivable"] or 0.0), 2),
        "positions": positions_at(conn, account_id=account_id,
                                  trade_date=last["trade_date"]),
        "curve": chart,
        "curve_limit": NAV_CURVE_LIMIT,
        "curve_truncated": truncated,
        # 统计口径始终是完整区间，与图上的窗口区分开
        "stats_from": first["trade_date"], "stats_to": last["trade_date"],
        "stats_days": len(full),
    }


# ── 费用与成交 ──────────────────────────────────────────────────────
def _fee(notional: float, side: str, on: str, model: costs_mod.CostModel) -> float:
    return float(costs_mod.fee_decimal(
        model, Decimal(repr(float(notional))), side=side,
        on=_date.fromisoformat(on)))


def _capacity(bar: Bar, participation: float) -> int | None:
    """当日最多能成交多少股（成交量 × 参与率，向下取整到股）。

    `participation <= 0` 表示关闭比例限制；成交量不可用（<=0）时返回 0 ——
    **没有成交量不等于可以无限量成交**（旧实现这里返回 None 等于放行，
    与 README 承诺的「最多吃掉成交量 10%」完全不符）。
    """
    if participation <= 0:
        return None
    if bar.volume <= 0:
        return 0
    return int(bar.volume * participation)


def _blocked_by_limit(bar: Bar, lim: dict | None, prev_close: float | None,
                      side: str) -> bool:
    """封在涨停板上买不进、封在跌停板上卖不掉。

    优先用**实测涨跌停价**（不复权口径，与 raw_bar 同一基准）；没有价位行时
    退回收益率比（与历史行为一致，也覆盖合成数据只有 ratio 的情形）。
    """
    if not lim:
        return False
    status = (lim.get("status") or "normal")
    if status in ("no_price_limit",):
        return False
    up, dn = lim.get("limit_up"), lim.get("limit_down")
    if up and dn:
        up, dn = float(up), float(dn)
        # 全天最低价都没低于涨停价 → 封板，买不进
        if side == "buy":
            return bar.low >= up - 1e-9
        return bar.high <= dn + 1e-9
    ratio = lim.get("limit_ratio")
    if not ratio or float(ratio) <= 0 or not prev_close or prev_close <= 0 \
            or bar.open <= 0:
        return False
    pct = (bar.open / prev_close - 1) * 100.0
    limit = float(ratio)
    return pct >= limit - 0.2 if side == "buy" else pct <= -limit + 0.2


def _public_status(status: str, filled: int, total: int,
                   closed: str | None) -> str:
    """内部状态 → 对外状态（`deferred` 对用户就是 `pending`）。"""
    if status == "deferred":
        return "pending"
    if status == "part" and filled >= total:
        return "filled"
    return status


# ── 下单（手工）─────────────────────────────────────────────────────
def _frozen_cash(conn: sqlite3.Connection, account_id: int) -> float:
    """未成交买单占用的资金（限价金额 + 费用），实时派生、不落库。"""
    total = 0.0
    model = _MODEL
    for r in conn.execute(
            "SELECT id, price, trade_date, volume FROM paper_order"
            f" WHERE account_id=? AND side='buy' AND status IN {_OPEN_SQL}",
            (int(account_id),)):
        remaining = int(r["volume"]) - _filled_volume(conn, int(r["id"]))
        if remaining <= 0:
            continue
        notional = remaining * float(r["price"])
        total += notional + _fee(notional, "buy", r["trade_date"], model)
    return _q(total)


def _frozen_shares(conn: sqlite3.Connection, account_id: int,
                   symbol: str) -> int:
    """该股票上未成交卖单占用的股数（按剩余量计）。"""
    total = 0
    for r in conn.execute(
            "SELECT id, volume FROM paper_order WHERE account_id=? AND symbol=?"
            f" AND side='sell' AND status IN {_OPEN_SQL}",
            (int(account_id), symbol)):
        total += max(0, int(r["volume"]) - _filled_volume(conn, int(r["id"])))
    return total


def _filled_volume(conn: sqlite3.Connection, order_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(volume),0) AS v FROM paper_fill WHERE order_id=?",
        (int(order_id),)).fetchone()
    return int(row["v"] or 0)


def _open_orders(conn: sqlite3.Connection, account_id: int,
                 before: str | None = None) -> list[dict]:
    sql = ("SELECT * FROM paper_order WHERE account_id=?"
           f" AND status IN {_OPEN_SQL}")
    params: list = [int(account_id)]
    if before:
        sql += " AND trade_date < ?"
        params.append(before)
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, tuple(params))]


def submit_manual_order(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    request_id: str,
    symbol: str,
    side: str,
    volume: int,
    price: float,
    thesis: str = "",
) -> dict:
    """登记一笔手动限价单（**不**立即成交，等下一个市场交易日撮合）。

    校验：账户可写、代码/数量合法（按板块）、价格有限正数、现金或可卖量足够。
    现金与可卖量按现有未成交委托**实时派生**冻结，避免重复占用。
    """
    acc = accounts_mod.get_account(conn, account_id)
    if acc["status"] != "active":
        raise SimError("账户已归档，不能下单")
    if acc["kind"] != "manual":
        raise SimError("策略账户不接受手工下单")

    rid = str(request_id or "").strip()
    if not 1 <= len(rid) <= 64:
        raise SimError("request_id 必须是 1–64 个字符")

    existing = conn.execute(
        "SELECT * FROM paper_order WHERE account_id=? AND request_id=?",
        (int(account_id), rid)).fetchone()
    if existing is not None:
        same = (existing["symbol"] == symbol and existing["side"] == side
                and int(existing["volume"]) == int(volume)
                and abs(float(existing["price"]) - float(price)) < 1e-9)
        if not same:
            raise SimError("request_id 已被另一笔委托使用")
        return _order_view(conn, dict(existing))

    day = last_settled_date(conn, account_id) or acc["start_date"]
    if not day:
        raise SimError("账户尚未结算任何一天，无法下单")
    if next_market_day(conn, day) is None:
        raise SimError("还没有下一个交易日的行情，无法挂单")

    if side not in ("buy", "sell"):
        raise SimError("方向只能是 buy/sell")
    try:
        price = rules.quantize_price(float(price))
    except (TypeError, ValueError) as exc:
        raise SimError("价格必须是数字") from exc
    if not (price > 0) or price != price or math.isinf(price):
        raise SimError("价格必须是有限正数")
    if abs(price * 100 - round(price * 100)) > 1e-6:
        raise SimError("价格必须对齐到 0.01 元")

    state = load_account(conn, account_id=int(account_id))
    if side == "buy":
        try:
            rules.validate_order_quantity(symbol, "buy", int(volume))
        except rules.TradeRuleError as exc:
            raise SimError(str(exc)) from exc
        notional = int(volume) * price
        need = _q(notional + _fee(notional, "buy", day, _MODEL))
        if _q(state.cash - _frozen_cash(conn, int(account_id))) < need:
            raise SimError(
                f"可用现金不足：需要 {need:.2f}（含费用），"
                f"可用 {_q(state.cash - _frozen_cash(conn, int(account_id))):.2f}")
    else:
        held = state.positions.get(symbol, 0)
        sellable = state.available.get(symbol, 0) - _frozen_shares(
            conn, int(account_id), symbol)
        try:
            rules.validate_order_quantity(symbol, "sell", int(volume), held=held + int(volume))
        except rules.TradeRuleError as exc:
            raise SimError(str(exc)) from exc
        if int(volume) > sellable:
            raise SimError(f"可卖数量不足：可卖 {sellable}，委托 {int(volume)}")

    now = db.now()
    cur = conn.execute(
        "INSERT INTO paper_order(account_id, request_id, trade_date, symbol, side,"
        " volume, price, status, source, thesis, review_note, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,'pending','manual',?,'',?,?)",
        (int(account_id), rid, day, symbol, side, int(volume), price,
         str(thesis or "")[:2000], now, now))
    conn.commit()
    row = conn.execute("SELECT * FROM paper_order WHERE id=?",
                       (int(cur.lastrowid),)).fetchone()
    return _order_view(conn, dict(row))


def cancel_order(conn: sqlite3.Connection, *, account_id: int,
                 order_id: int) -> dict:
    """撤销某笔委托的**未成交余额**（已成交部分原样保留）。"""
    row = conn.execute("SELECT * FROM paper_order WHERE id=? AND account_id=?",
                       (int(order_id), int(account_id))).fetchone()
    if row is None:
        raise SimError(f"委托 {order_id} 不存在")
    if row["status"] not in OPEN_STATUSES:
        raise SimError(f"委托已是终态（{row['status']}），不能再撤")
    day = last_settled_date(conn, int(account_id))
    conn.execute(
        "UPDATE paper_order SET status='canceled', closed_trade_date=?,"
        " err_msg=?, updated_at=? WHERE id=?",
        (day, "用户撤单", db.now(), int(order_id)))
    conn.commit()
    out = conn.execute("SELECT * FROM paper_order WHERE id=?",
                       (int(order_id),)).fetchone()
    return _order_view(conn, dict(out))


def _order_view(conn: sqlite3.Connection, order: dict,
                cutoff: str | None = None) -> dict:
    """委托的对外视图：`filled/remaining` 按**截止日**重算，历史可还原。"""
    total = int(order["volume"])
    filled = _filled_volume_before(conn, int(order["id"]), cutoff)
    status = order["status"]
    closed = order.get("closed_trade_date")
    if filled >= total:
        status = "filled"
    elif status in TERMINAL_STATUSES:
        # 终态发生在截止日之后 → 当时还没终止
        if cutoff and closed and closed > cutoff:
            status = "part" if filled > 0 else "pending"
    elif status == "deferred":
        status = "pending"
    elif status == "part" and filled >= total:
        status = "filled"
    if cutoff and closed and closed > cutoff:
        err = None
    else:
        err = order.get("err_msg")
    return {
        "order_id": int(order["id"]),
        "account_id": int(order["account_id"]),
        "trade_date": order["trade_date"],
        "symbol": order["symbol"],
        "side": order["side"],
        "volume": total,
        "filled_volume": filled,
        "remaining_volume": max(0, total - filled),
        "price": float(order["price"]),
        "status": status,
        "source": order["source"],
        "thesis": order.get("thesis") or "",
        "review_note": order.get("review_note") or "",
        "err_msg": err,
        "closed_trade_date": closed,
    }


def _filled_volume_before(conn: sqlite3.Connection, order_id: int,
                          cutoff: str | None) -> int:
    sql = "SELECT COALESCE(SUM(volume),0) AS v FROM paper_fill WHERE order_id=?"
    params: list = [int(order_id)]
    if cutoff:
        sql += " AND trade_date <= ?"
        params.append(cutoff)
    return int(conn.execute(sql, tuple(params)).fetchone()["v"] or 0)


def orders_query(conn: sqlite3.Connection, *, account_id: int,
                 date: str | None = None, limit: int = 100,
                 offset: int = 0) -> dict:
    """截至 `date` 的委托（含历史状态重建）。返回 `{rows, total}`。"""
    where = ["account_id=?"]
    params: list = [int(account_id)]
    if date:
        where.append("trade_date <= ?")
        params.append(date)
    clause = " AND ".join(where)
    total = int(conn.execute(
        f"SELECT COUNT(*) AS c FROM paper_order WHERE {clause}",
        tuple(params)).fetchone()["c"] or 0)
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM paper_order WHERE {clause} ORDER BY trade_date DESC, id DESC"
        f" LIMIT ? OFFSET ?", (*params, int(limit), int(offset)))]
    return {"rows": [_order_view(conn, r, cutoff=date) for r in rows],
            "total": total}


def fills_query(conn: sqlite3.Connection, *, account_id: int,
                date: str | None = None, limit: int = 100,
                offset: int = 0) -> dict:
    where = ["account_id=?"]
    params: list = [int(account_id)]
    if date:
        where.append("trade_date <= ?")
        params.append(date)
    clause = " AND ".join(where)
    total = int(conn.execute(
        f"SELECT COUNT(*) AS c FROM paper_fill WHERE {clause}",
        tuple(params)).fetchone()["c"] or 0)
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM paper_fill WHERE {clause}"
        f" ORDER BY trade_date DESC, id DESC LIMIT ? OFFSET ?",
        (*params, int(limit), int(offset)))]
    return {"rows": rows, "total": total}


# ── 日结 ────────────────────────────────────────────────────────────
def _apply_fill(state: Account, sym: str, side: str, qty: int, price: float,
                fee: float, on: str) -> tuple[float, float | None]:
    """把一笔成交作用到账户上，返回 (费用, 已实现盈亏)。

    买入：现金减少（名义额 + 费），总成本余额增加，股数增加。
    卖出：现金增加（名义额 − 费），总成本余额按**比例结转**（分位取整），
    剩余成本留在仓位上 —— 这样部分卖出不会把成本一次性抹平。
    """
    notional = _q(qty * price)
    if side == "buy":
        state.cash = _q(state.cash - notional - fee)
        state.positions[sym] = state.positions.get(sym, 0) + qty
        state.cost_basis[sym] = _q(state.cost_basis.get(sym, 0.0) + notional + fee)
        return fee, None

    held = state.positions.get(sym, 0)
    basis_total = state.cost_basis.get(sym, 0.0)
    if qty >= held:
        basis = basis_total                      # 清仓：把全部余额释放掉
        left = 0
        state.positions.pop(sym, None)
        state.cost_basis.pop(sym, None)
        state.locked.pop(sym, None)
    else:
        # 按比例结转总成本并取到分；余下的仍留在仓位上（含四舍五入的尾差）。
        basis = _q(basis_total * qty / held)
        left = held - qty
        state.positions[sym] = left
        state.cost_basis[sym] = _q(basis_total - basis)
        state.locked[sym] = min(state.locked.get(sym, 0), left)
    realized = _q(notional - fee - basis)
    state.cash = _q(state.cash + notional - fee)
    return fee, realized


def _settle_orders(conn: sqlite3.Connection, state: Account, day: str,
                   prev_day: str | None, model: costs_mod.CostModel,
                   participation: float) -> dict:
    """撮合当日到期的全部挂单。返回当日统计。"""
    bars = _day_bars(conn, day)
    limits = _limits(conn, day)
    prev_close: dict[str, float] = {}
    if prev_day:
        prev_close = {s: b.close for s, b in _day_bars(conn, prev_day).items()}

    stats = {"filled": 0, "pending": 0, "canceled": 0, "deferred": 0,
             "fee": 0.0, "trade_date": day, "capped": 0, "illiquid": 0}
    pending = _open_orders(conn, state.account_id, before=day)[:MAX_ORDERS_PER_DAY]
    # 当日容量按「账户 × 股票」共享：买卖合计不能超过上限。
    remaining_cap: dict[str, int | None] = {}

    for o in pending:
        oid = int(o["id"])
        sym, side = o["symbol"], o["side"]
        remaining_total = int(o["volume"]) - _filled_volume(conn, oid)
        if remaining_total <= 0:
            _close_order(conn, oid, "filled", day)
            continue
        want = remaining_total
        bar = bars.get(sym)
        if bar is None:
            # 停牌（已确认）或无行情：顺延，不撤单。
            stats["deferred"] += 1
            _set_status(conn, oid, "deferred", err_msg="当日无成交行情（停牌/停牌确认）")
            continue
        if _blocked_by_limit(bar, limits.get(sym), prev_close.get(sym), side):
            stats["deferred"] += 1
            _set_status(conn, oid, "deferred", err_msg="涨停买不进/跌停卖不掉")
            continue

        limit_px = float(o["price"])
        lim = limits.get(sym)
        if lim and lim.get("limit_up") and lim.get("limit_down") and \
                (lim.get("status") or "normal") not in ("no_price_limit",):
            up, dn = float(lim["limit_up"]), float(lim["limit_down"])
            if not dn - 1e-9 <= limit_px <= up + 1e-9:
                stats["canceled"] += 1
                _close_order(conn, oid, "rejected", day, "委托价超出当日涨跌停")
                continue

        if side == "buy":
            if bar.open <= limit_px:
                px = bar.open
            elif bar.low <= limit_px:
                px = limit_px
            else:
                stats["canceled"] += 1
                _close_order(conn, oid, "canceled", day, "限价未触及")
                continue
        else:
            if bar.open >= limit_px:
                px = bar.open
            elif bar.high >= limit_px:
                px = limit_px
            else:
                stats["canceled"] += 1
                _close_order(conn, oid, "canceled", day, "限价未触及")
                continue

        if sym not in remaining_cap:
            remaining_cap[sym] = _capacity(bar, participation)
        cap = remaining_cap[sym]
        if cap is not None:
            if cap <= 0:
                stats["deferred"] += 1
                stats["illiquid"] += 1
                _set_status(conn, oid, "deferred", err_msg="当日成交量不足")
                continue
            if cap < want:
                want = cap
                stats["capped"] += 1

        # 现金/持仓不足时按该板块的申报步长缩减（手工单下单时已校验，这是兜底）。
        step = rules.BOARDS[rules.board_of(sym)]["buy_step"]
        got = want
        while got > 0:
            notional = got * px
            fee = _fee(notional, side, day, model)
            if side == "buy":
                if state.cash >= notional + fee:
                    break
            elif state.positions.get(sym, 0) >= got:
                break
            got -= step
        if got <= 0:
            stats["canceled"] += 1
            if side == "buy":
                # 买方的资金不足是**策略/资金约束**，撤掉余量并留下可追溯的原因，
                # 而不是当成「委托非法」拒绝（那是 rejected 的语义）。
                _close_order(conn, oid, "canceled", day,
                             "现金不足维持余量（insufficient_cash_after_partial）")
            else:
                _close_order(conn, oid, "rejected", day, "持仓不足")
            continue

        fee, realized = _apply_fill(state, sym, side, got, px,
                                    _fee(got * px, side, day, model), day)
        conn.execute(
            "INSERT INTO paper_fill(account_id, order_id, trade_date, symbol, side,"
            " volume, price, fee, realized_pnl, avg_cost, traded_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (state.account_id, oid, day, sym, side, got, px, fee, realized,
             (state.cost_basis.get(sym, 0.0) / state.positions[sym])
             if state.positions.get(sym) else None, db.now()))
        if cap is not None:
            remaining_cap[sym] = cap - got
        stats["filled"] += 1
        stats["fee"] = _q(stats["fee"] + fee)

        # 余量按**原始剩余量**算：`want` 可能已被参与率/资金截断，
        # 拿它当余量的话「部分成交」会被误判成「全部成交」而直接封单。
        left = remaining_total - got
        if left > 0:
            stats["deferred"] += 1
            _set_status(conn, oid, "deferred", err_msg="部分成交（当日容量/资金）")
        else:
            _close_order(conn, oid, "filled", day)
    return stats


def _set_status(conn, order_id: int, status: str, *, err_msg: str | None) -> None:
    conn.execute("UPDATE paper_order SET status=?, err_msg=?, updated_at=? WHERE id=?",
                 (status, err_msg, db.now(), int(order_id)))


def _close_order(conn, order_id: int, status: str, day: str,
                 err_msg: str | None = None) -> None:
    """把委托置为终态并记下**账户业务日**（历史还原要靠它，不能用墙钟）。"""
    conn.execute(
        "UPDATE paper_order SET status=?, err_msg=?, closed_trade_date=?,"
        " updated_at=? WHERE id=?", (status, err_msg, day, db.now(), int(order_id)))


def _revalidate_buy_freeze(conn: sqlite3.Connection, state: Account,
                           day: str) -> int:
    """按订单 id 先到先得重验买单冻结，资金不够的整张余单撤掉。

    只用于**公司行为扣款之后**：普通撮合时资金不足由撮合循环自己处理
    （部分成交 + 撤余量），这里是「现金被外部事件扣掉」的那条路径。
    """
    reserved = 0.0
    canceled = 0
    for r in conn.execute(
            "SELECT id, price, trade_date, volume FROM paper_order"
            f" WHERE account_id=? AND side='buy' AND status IN {_OPEN_SQL}"
            " ORDER BY id", (state.account_id,)):
        remaining = int(r["volume"]) - _filled_volume(conn, int(r["id"]))
        if remaining <= 0:
            continue
        notional = remaining * float(r["price"])
        need = _q(notional + _fee(notional, "buy", r["trade_date"], _MODEL))
        if _q(reserved + need) <= _q(state.cash):
            reserved = _q(reserved + need)
            continue
        _close_order(conn, int(r["id"]), "canceled", day,
                     "公司行为扣款后资金不足（corporate_action_reservation_conflict）")
        canceled += 1
    return canceled


def _mark_to_market(conn: sqlite3.Connection, state: Account, day: str,
                    filled_orders: int) -> dict:
    """按当日收盘价盯市，写 paper_position / paper_nav（同日重跑覆盖）。"""
    symbols = sorted(state.positions)
    values = _valuation(conn, day, symbols)
    mv = 0.0
    conn.execute("DELETE FROM paper_position WHERE account_id=? AND trade_date=?",
                 (state.account_id, day))
    for sym in symbols:
        vol = state.positions[sym]
        if vol <= 0:
            continue
        hit = values.get(sym)
        if hit is None:
            raise DataBlocked("raw_bar_missing", [{"symbol": sym, "date": day}], day)
        close, price_date = hit
        value = _q(vol * close)
        mv = _q(mv + value)
        conn.execute(
            "INSERT INTO paper_position(account_id, trade_date, symbol, volume,"
            " available_volume, avg_price, cost_basis, close, price_date, market_value)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (state.account_id, day, sym, vol, state.available.get(sym, 0),
             (state.cost_basis.get(sym, 0.0) / vol) if vol else 0.0,
             _q(state.cost_basis.get(sym, 0.0)), close, price_date, value))
    total = _q(state.cash + mv + state.receivable)
    initial = _initial_cash(conn, state.account_id) or total
    conn.execute(
        "INSERT OR REPLACE INTO paper_nav(account_id, trade_date, cash, market_value,"
        " receivable, total, nav, n_positions, filled_orders)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (state.account_id, day, _q(state.cash), mv, _q(state.receivable), total,
         total / initial if initial else 1.0, len(symbols), int(filled_orders)))
    conn.execute("UPDATE paper_account SET cash=?, updated_at=? WHERE id=?",
                 (_q(state.cash), db.now(), state.account_id))
    return {"trade_date": day, "cash": _q(state.cash), "market_value": mv,
            "receivable": _q(state.receivable), "total": total,
            "n_positions": len(symbols)}


# ── 推进 ────────────────────────────────────────────────────────────
def _symbols_of_interest(conn: sqlite3.Connection, account_id: int,
                         state: Account, day: str) -> set[str]:
    syms = set(state.positions)
    for o in _open_orders(conn, account_id, before=day):
        syms.add(o["symbol"])
    return syms


def _precheck(conn: sqlite3.Connection, account_id: int, state: Account,
              day: str) -> None:
    """逐日闸门：任何一项不满足都停在这天之前，并给出精确原因。"""
    syms = _symbols_of_interest(conn, account_id, state, day)
    if not syms:
        return
    unresolved = corporate_mod.unresolved_events(
        conn, account_id=account_id, symbols=syms, through=day)
    if unresolved:
        reasons = {u["reason"] for u in unresolved}
        reason = ("corporate_tax_unresolved" if reasons == {"tax_unresolved"}
                  else "corporate_action_unsupported"
                  if reasons <= {"unsupported"}
                  else "corporate_action_unresolved")
        raise DataBlocked(reason, unresolved, day)

    # 持仓必须有可估值价：当天没有可用 bar 时，必须是**已确认停牌**且此前有
    # 有效原价（沿用旧价估值）。缺 bar 又没确认 → 阻断，绝不猜停牌。
    for sym in sorted(state.positions):
        if raw_close(conn, sym, day) is not None:
            continue
        if not _suspended_confirmed(conn, sym, day) or \
                last_valid_close(conn, sym, day) is None:
            raise DataBlocked("raw_bar_missing", [{"symbol": sym, "date": day}], day)

    # 有挂单可撮合的股票：必须有当日行情（或已确认停牌）与**当日实测涨跌停行**，
    # 否则无法判定可交易性。
    limit_rows = _limits(conn, day)
    for o in _open_orders(conn, account_id, before=day):
        sym = o["symbol"]
        if raw_close(conn, sym, day) is None:
            if not _suspended_confirmed(conn, sym, day):
                raise DataBlocked("raw_bar_missing", [{"symbol": sym, "date": day}], day)
            # 已确认停牌：当天没有任何成交，自然也没有（也不需要）涨跌停行
            continue
        if sym not in limit_rows:
            raise DataBlocked("price_limit_missing", [{"symbol": sym, "date": day}], day)


def _advance_one_day(conn: sqlite3.Connection, account_id: int, day: str,
                     state: Account, model: costs_mod.CostModel,
                     participation: float, rebalance=None) -> dict:
    """结算**一天**（调用方负责事务与锁）。"""
    days = trading_days(conn, end=day)
    prev_day = _prev_calendar_day(days, day) if days and days[-1] == day else None

    effects = corporate_mod.effects_for_day(conn, account_id=account_id, day=day)
    unlock_extra: dict[str, int] = {}
    for sym, delta in effects["shares_delta"].items():
        if delta:
            state.positions[sym] = state.positions.get(sym, 0) + int(delta)
    # 股份数量或价格基准被公司行为改过之后，**旧的余单不能再撮合**：
    # 送转/配股当天股价除权，按老限价挂着的单子要么「一整天没触及」（白等），
    # 要么以与新股数不匹配的价格成交。作废并留下可追溯的原因，让用户按新价重挂。
    for sym in effects["shares_delta"]:
        conn.execute(
            "UPDATE paper_order SET status='canceled', closed_trade_date=?,"
            " err_msg=?, updated_at=? WHERE account_id=? AND symbol=?"
            f" AND status IN {_OPEN_SQL}",
            (day, "公司行为改变股份数量/价格基准，原委托作废"
                  "（corporate_action_order_invalidated）",
             db.now(), state.account_id, sym))
    for sym, delta in effects["lock_delta"].items():
        if delta:
            state.locked[sym] = state.locked.get(sym, 0) + int(delta)
    for sym, delta in effects["unlock_delta"].items():
        if delta:
            unlock_extra[sym] = unlock_extra.get(sym, 0) + int(delta)
            state.locked[sym] = max(0, state.locked.get(sym, 0) - int(delta))
    for sym, delta in effects["cost_delta"].items():
        if delta:
            state.cost_basis[sym] = _q(state.cost_basis.get(sym, 0.0) + delta)
    if effects["cash_delta"]:
        state.cash = _q(state.cash + effects["cash_delta"])
    if effects["receivable_delta"]:
        state.receivable = _q(state.receivable + effects["receivable_delta"])
    if state.cash < 0:
        raise DataBlocked("insufficient_cash_for_corporate_action",
                          [{"date": day, "cash": state.cash}], day)
    if effects["cash_delta"] < 0:
        # 公司行为扣了现金（认购款/红利税）之后必须**重验买单冻结**：
        # 按订单 id 先到先得，整张余单的资金不够就撤掉它，而不是让它继续占着
        # 一份已经不存在的冻结（否则后面每次撮合都在算一个假的可用现金）。
        _revalidate_buy_freeze(conn, state, day)
    state.refresh_available(unlock_extra)

    stats = _settle_orders(conn, state, day, prev_day, model, participation)
    state.refresh_available()
    nav = _mark_to_market(conn, state, day, stats["filled"])

    if rebalance is not None:
        rebalance(conn, state, day)

    stats["nav"] = nav
    stats["corporate_notes"] = effects["notes"]
    return stats


def advance(conn: sqlite3.Connection, *, account_id: int,
            to_date: str | None = None) -> dict:
    """把账户推进到 `to_date`（缺省 = 最新市场交易日）。

    - 已结算的交易日**绝不重算**（`paper_nav` 的存在就是完成标志）；
    - 每个完整日一个事务：公司行为、撮合、订单状态、现金、持仓、净值、当日
      调仓记录要么全成功要么全回滚；
    - 数据有缺口就停在缺口之前并返回精确原因（`blocked_reason`/`missing`）。
    """
    acc = accounts_mod.get_account(conn, account_id)
    if acc["status"] != "active":
        raise SimError(f"账户已归档（{acc['status']}），不能推进")

    mkt = db.market_date(conn)
    if not mkt:
        return {"ok": False, "account_id": int(account_id), "settled_through": None,
                "days": 0, "blocked_reason": "raw_not_ready", "missing": []}
    end = mkt if to_date is None else min(to_date, mkt)

    execu = accounts_mod.execution_of(conn, int(account_id))
    model = _MODEL
    last = last_settled_date(conn, int(account_id))
    start = (next_market_day(conn, last) if last else acc["start_date"])
    if start is None:
        return {"ok": False, "account_id": int(account_id), "settled_through": last,
                "days": 0, "blocked_reason": "calendar_missing", "missing": []}
    days = trading_days(conn, start=start, end=end)
    days = [d for d in days if not last or d > last]
    if not days:
        return {"ok": True, "account_id": int(account_id),
                "settled_through": last, "days": 0,
                "blocked_reason": None, "missing": []}

    state = load_account(conn, account_id=int(account_id))
    desired = _desired_params(conn, int(account_id))
    ctx = entry = score = None
    if desired is not None:
        ctx, entry, score = _strategy_context(conn, desired)
        # 创建当天就该留下这一轮的决策（否则策略要白等一个交易日才挂首批单）。
        # 只在**从来没有过**调仓记录时补一次，不会重复触发。
        if last and not conn.execute(
                "SELECT 1 FROM paper_rebalance WHERE account_id=? LIMIT 1",
                (int(account_id),)).fetchone():
            conn.isolation_level = None
            try:
                conn.execute("BEGIN IMMEDIATE")
                _emit_strategy_orders(conn, state, last, int(account_id),
                                      desired, ctx, entry, score)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.isolation_level = ""

    settled = 0
    blocked = None
    missing: list[dict] = []
    conn.isolation_level = None
    try:
        for day in days:
            try:
                _precheck(conn, int(account_id), state, day)
            except DataBlocked as exc:
                blocked, missing = exc.reason, exc.missing
                break
            conn.execute("BEGIN IMMEDIATE")
            try:
                def _rebalance(c, st, d, _desired=desired, _ctx=ctx,
                               _entry=entry, _score=score):
                    if _desired is not None:
                        _emit_strategy_orders(c, st, d, int(account_id), _desired,
                                              _ctx, _entry, _score)
                _advance_one_day(conn, int(account_id), day, state, model,
                                 execu["participation"], rebalance=_rebalance)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            settled += 1
    finally:
        conn.isolation_level = ""

    return {"ok": blocked is None, "account_id": int(account_id),
            "settled_through": last_settled_date(conn, int(account_id)),
            "days": settled, "blocked_reason": blocked, "missing": missing}


# ── 策略调仓 ────────────────────────────────────────────────────────
def _desired_params(conn: sqlite3.Connection, account_id: int) -> dict | None:
    acc = conn.execute("SELECT kind FROM paper_account WHERE id=?",
                       (int(account_id),)).fetchone()
    if acc is None or acc["kind"] != "strategy":
        return None
    return accounts_mod.meta_get(conn, int(account_id), "params")


def _strategy_context(conn: sqlite3.Connection, params: dict):
    """算一次 entry_mask（逐日循环里重算要几十秒）。"""
    from ..analytics import backtest as bt
    ctx = bt.load_context(conn, None, with_ohlcv=(params["signal"] == "composite"))
    score = None
    if params["signal"] == "composite":
        from ..analytics import factors as fac
        score = fac.composite_score(ctx, conn, period=params["period"],
                                    hold=params["hold"],
                                    exclude_st=params["exclude_st"])
    entry = bt.entry_mask(ctx, conn, params["period"], params["threshold"],
                          exclude_st=params["exclude_st"],
                          resonance=params["resonance"], score=score)
    return ctx, entry, score


def _rebalance_due(conn: sqlite3.Connection, account_id: int, day: str,
                   params: dict) -> bool:
    """按指数日历相对账户起点的**零基序号**判断是否调仓日（k % hold == 0）。

    与旧实现「按过滤后的信号日计数」不同：市场日历是稳定序列，不会因为某天
    没有 RPS 就把节奏整体挪位。
    """
    acc = conn.execute("SELECT start_date FROM paper_account WHERE id=?",
                       (int(account_id),)).fetchone()
    start = acc["start_date"] if acc else None
    if not start:
        return False
    cal = trading_days(conn, start=start)
    if day not in cal:
        return False
    k = cal.index(day)
    return k % max(1, int(params["hold"])) == 0


def _emit_strategy_orders(conn: sqlite3.Connection, state: Account, day: str,
                          account_id: int, params: dict, ctx, entry, score) -> int:
    """收盘后为**下一个交易日**挂策略单（候选/原因留档）。"""
    from . import plan as plan_mod

    if not _rebalance_due(conn, account_id, day, params):
        return 0
    if entry is None or day not in entry.mask.index:
        # 当天没有信号：只跳过「出新信号」，撮合/权益/估值照常（已经做完了）。
        conn.execute(
            "INSERT OR REPLACE INTO paper_rebalance(account_id, signal_date, params,"
            " selected, skipped) VALUES (?,?,?,?,?)",
            (int(account_id), day, "{}", "[]",
             '{"no_signal_day": 1}'))
        return 0

    candidates = plan_mod.select_candidates(
        ctx, entry, date=day, period=int(params["period"]),
        max_positions=int(params["max_positions"]),
        exclude_st=bool(params["exclude_st"]), signal=params["signal"], score=score)

    nxt = next_market_day(conn, day)
    if nxt is None:
        return 0

    equity = _q(state.cash + state.receivable + sum(
        _q(v * (raw_close(conn, s, day) or 0.0))
        for s, v in state.positions.items()))
    per_name = equity * 0.95 / max(1, int(params["max_positions"]))
    held = dict(state.positions)
    selected, skipped = [], {}

    # 先撤本策略上一轮的余单，避免和新目标叠加。
    conn.execute(
        "UPDATE paper_order SET status='canceled', closed_trade_date=?, err_msg=?,"
        " updated_at=? WHERE account_id=? AND source='strategy' AND status IN"
        " ('pending','deferred','part')",
        (day, "新一轮调仓，撤销上轮余量", db.now(), int(account_id)))

    targets: dict[str, int] = {}
    for sym in candidates:
        px = raw_close(conn, sym, day)
        if not px or px <= 0:
            skipped["no_price"] = skipped.get("no_price", 0) + 1
            continue
        cap = rules.max_legal_buy_volume(sym, int(per_name / px))
        targets[sym] = cap
        if cap <= 0:
            skipped["too_small"] = skipped.get("too_small", 0) + 1

    now = db.now()
    n = 0
    # 卖单先：回款要供后面的买单用。
    for sym, have in sorted(held.items()):
        want = targets.get(sym, 0)
        if have <= want:
            continue
        qty = have - want
        px = raw_close(conn, sym, day)
        if not px or px <= 0:
            skipped["orphan_no_price"] = skipped.get("orphan_no_price", 0) + 1
            continue
        try:
            rules.validate_order_quantity(sym, "sell", qty, held=qty)
        except rules.TradeRuleError:
            qty = rules.max_legal_buy_volume(sym, qty) or qty
        conn.execute(
            "INSERT OR IGNORE INTO paper_order(account_id, request_id, trade_date,"
            " symbol, side, volume, price, status, source, thesis, created_at,"
            " updated_at) VALUES (?,?,?,?,'sell',?,?,'pending','strategy',?,?,?)",
            (int(account_id), f"strategy:{day}:{sym}:sell", day, sym, int(qty),
             rules.quantize_price(px * (1 - PRICE_OFFSET)),
             f"调仓卖出：跌出候选/降目标（信号 {day}）", now, now))
        n += 1
    for sym in sorted(targets):
        want = targets[sym]
        have = held.get(sym, 0)
        if want <= have:
            continue
        qty = want - have
        px = raw_close(conn, sym, day)
        if not px or px <= 0:
            skipped["no_price"] = skipped.get("no_price", 0) + 1
            continue
        conn.execute(
            "INSERT OR IGNORE INTO paper_order(account_id, request_id, trade_date,"
            " symbol, side, volume, price, status, source, thesis, created_at,"
            " updated_at) VALUES (?,?,?,?,'buy',?,?,'pending','strategy',?,?,?)",
            (int(account_id), f"strategy:{day}:{sym}:buy", day, sym, int(qty),
             rules.quantize_price(px * (1 + PRICE_OFFSET)),
             f"调仓买入：信号 {day}（{params['signal']} 前 {params['max_positions']}）",
             now, now))
        n += 1
        selected.append(sym)

    conn.execute(
        "INSERT OR REPLACE INTO paper_rebalance(account_id, signal_date, params,"
        " selected, skipped) VALUES (?,?,?,?,?)",
        (int(account_id), day,
         json.dumps(params, ensure_ascii=False),
         json.dumps(selected, ensure_ascii=False),
         json.dumps(skipped, ensure_ascii=False)))
    return n


# ── 归档 ────────────────────────────────────────────────────────────
def archive(conn: sqlite3.Connection, label: str | None = None, *,
            account_id: int) -> dict:
    """把当前曲线归档一份（跨账户留证据；不改动账户本身）。"""
    s = summary(conn, account_id=int(account_id))
    if not s["days"]:
        raise SimError("模拟盘还没有曲线，无可归档")
    acc = accounts_mod.get_account(conn, int(account_id))
    params = acc.get("params") or {}
    name = (label or "").strip() or (
        f"{params.get('signal', 'rps')}-{account_id}-{db.now().replace(':', '')}")
    rev = review(conn, account_id=int(account_id))
    metrics = {**(rev.get("summary") or {}), "benchmark": rev.get("benchmark")}
    cur = conn.execute(
        "INSERT INTO paper_archive(account_id, label, price_basis, params,"
        " initial_cash, from_date, to_date, total, nav, total_return, max_drawdown,"
        " days, metrics, curve, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(account_id), name, acc["price_basis"],
         json.dumps(params, ensure_ascii=False),
         float(s["initial_cash"]), s["from"], s["to"], float(s["total"]),
         float(s["nav"]), float(s["return"]), float(s["max_drawdown"]),
         int(s["days"]), json.dumps(metrics, ensure_ascii=False),
         json.dumps(s["curve"], ensure_ascii=False), db.now()))
    conn.commit()
    return {"id": int(cur.lastrowid), "label": name, "days": s["days"],
            "total": s["total"], "return": s["return"]}


def list_archives(conn: sqlite3.Connection, limit: int = 10, *,
                  account_id: int | None = None) -> list[dict]:
    """归档列表（含曲线，供策略对照叠加画图）。"""
    sql = "SELECT * FROM paper_archive"
    params: list = []
    if account_id is not None:
        sql += " WHERE account_id = ?"
        params.append(int(account_id))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    out = []
    for r in conn.execute(sql, tuple(params)):
        d = dict(r)
        for key in ("params", "metrics", "curve"):
            try:
                d[key] = json.loads(d[key])
            except (TypeError, ValueError):
                d[key] = {} if key != "curve" else []
        out.append(d)
    return out


# ── 风险与复盘 ──────────────────────────────────────────────────────
def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """皮尔逊相关（纯标准库）。样本 <3 或零方差返回 None。"""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx > 0 and dy > 0 else None


def portfolio_risk(conn: sqlite3.Connection, *, account_id: int,
                   date: str | None = None, window: int = 60) -> dict:
    """当前持仓体检：集中度 + 两两相关性（"是不是一堆同一个赌注"）。

    相关性用**后复权**日收益（研究口径，除权日不会出现假跳变），但严格限制
    在截止日之前，并且**按共同交易日配对** —— 旧实现把各股缺失的日子删掉后
    按数组尾部对齐，等于拿不同日期的收益去算相关（实测把完全同步的两只票
    算成 0.9685 而不是 1）。
    """
    pos = positions_at(conn, account_id=int(account_id), trade_date=date)
    if not pos:
        return {}
    nav = _nav_row(conn, int(account_id), date)
    equity = float(nav["total"]) if nav else 0.0
    total = equity or (sum(float(p["market_value"]) for p in pos) or 1.0)
    weights = sorted(((p["symbol"], float(p["market_value"]) / total) for p in pos),
                     key=lambda x: -x[1])

    industry: dict[str, float] = {}
    syms = [p["symbol"] for p in pos]
    marks = ",".join("?" * len(syms))
    for r in conn.execute(
            f"SELECT symbol, industry FROM industry_map WHERE symbol IN ({marks})",
            syms):
        w = next((w for s, w in weights if s == r["symbol"]), 0.0)
        key = r["industry"] or "未分类"
        industry[key] = industry.get(key, 0.0) + w
    top_industry = max(industry.items(), key=lambda kv: kv[1]) if industry else None

    # 终点：截止日（含）之前最近的净值日；没有就最新。
    end_date = (nav["trade_date"] if nav else None) or date
    sql = ("SELECT DISTINCT date FROM daily_bar WHERE symbol='000001'"
           if False else "SELECT DISTINCT date FROM benchmark_bar WHERE symbol='000001'")
    if end_date:
        sql += " AND date <= ?"
    sql += " ORDER BY date DESC LIMIT ?"
    params: tuple = ((end_date, window + 1) if end_date else (window + 1,))
    days = [r["date"] for r in conn.execute(sql, params)]
    if days:
        days = sorted(days)

    rets: dict[str, dict[str, float]] = {}
    if days:
        lo = days[0]
        for r in conn.execute(
                f"SELECT symbol, date, close FROM daily_bar WHERE symbol IN ({marks})"
                f" AND date >= ? AND date <= ? ORDER BY date",
                (*syms, lo, days[-1])):
            rets.setdefault(r["symbol"], {})[r["date"]] = float(r["close"] or 0.0)

    # 收益率必须在**共同的市场日期网格**上算：先按网格对齐（缺 bar 记为 None），
    # 再算相邻两日的收益（任一端点缺失则该日无收益）。旧实现把各股缺失的日子
    # 删掉后按数组尾部对齐，等于拿不同日期段的收益去算相关 —— 实测把两只完全
    # 同步的票算成 0.9685 而不是 1。
    rmap: dict[str, dict[str, float]] = {}
    if days:
        for sym in syms:
            series = rets.get(sym, {})
            seq = [series.get(d) for d in days]
            out: dict[str, float] = {}
            for i in range(1, len(days)):
                prev, cur = seq[i - 1], seq[i]
                if prev and prev > 0 and cur and cur > 0:
                    out[days[i]] = cur / prev - 1.0
            rmap[sym] = out

    corrs: list[tuple[str, str, float]] = []
    corr_samples: list[dict] = []
    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            ra, rb = rmap.get(a, {}), rmap.get(b, {})
            common = sorted(set(ra) & set(rb))
            corr_samples.append({"a": a, "b": b, "n": len(common)})
            if len(common) < 3:
                continue
            c = _pearson([ra[d] for d in common], [rb[d] for d in common])
            if c is not None:
                corrs.append((a, b, c))
    best = max(corrs, key=lambda x: x[2]) if corrs else None

    return {
        "n_positions": len(pos),
        "top_weight": {"symbol": weights[0][0], "weight": round(weights[0][1], 4)},
        "top5_weight": round(sum(w for _, w in weights[:5]), 4),
        "top_industry": ({"industry": top_industry[0], "weight": round(top_industry[1], 4)}
                         if top_industry else None),
        "corr_window": int(window),
        "corr_pairs": len(corrs),
        "corr_samples": corr_samples,
        "corr_avg": round(sum(c for *_, c in corrs) / len(corrs), 4) if corrs else None,
        "corr_max": ({"a": best[0], "b": best[1], "value": round(best[2], 4)}
                     if best else None),
        "notes": [f"相关性按近 {int(window)} 个交易日的日收益算，"
                  "且只在**两只票都有连续收益**的共同日期上配对；样本不足 3 天不计入。",
                  "权重按 **持仓市值 / 总权益** 算（含现金），加总不等于 1 的部分是现金。"],
    }


def _metrics(equity: list[float], dates: list[str]) -> dict:
    """逐日净值指标（与回测共用同一套公式，口径一致）。"""
    from ..analytics import backtest as bt
    return bt.compute_metrics_from_points(equity, dates)


def _build_round_trips(conn: sqlite3.Connection, account_id: int,
                       fills: list[dict]) -> list[dict]:
    """FIFO 回合：把公司行为的股份变动也按时间线织进去。

    送转出来的股如果不在批次里，卖出就会被误报成「没有对应买入」。
    所以时间线是「成交 + 权益变动」两类事件按日期排序后的重放：
    - 送转/配股生效：按比例放大该股各批次股数（总成本不变），配股另加一个新批次。
    """
    events: list[tuple[str, int, dict]] = []
    for f in fills:
        events.append((f["trade_date"], 0, {"kind": "fill", "fill": f}))
    for a in corporate_mod.list_actions(conn, int(account_id)):
        res = a["resolution"]
        events.append((res["effective_date"], 1,
                       {"kind": "shares", "symbol": a["symbol"], "res": res}))
    events.sort(key=lambda x: (x[0], x[1]))

    lots: dict[str, list[list]] = {}
    round_trips: list[dict] = []
    for _, _, ev in events:
        if ev["kind"] == "shares":
            sym, res = ev["symbol"], ev["res"]
            delta = int(res["share_delta"])
            if delta <= 0:
                continue
            queue = lots.get(sym) or []
            have = sum(lot[1] for lot in queue)
            if res["subscription_cash"] > 0 and have > 0:
                # 配股：按批次数均摊新增股，成本按认购款加到对应批次
                unit = res["subscription_cash"] / delta
                queue.append([res["effective_date"], delta, unit])
            elif have > 0:
                # 送转：等比例放大各批次股数，单位成本按比例缩小
                scale = (have + delta) / have
                for lot in queue:
                    lot[1] = int(round(lot[1] * scale))
                    lot[2] = lot[2] / scale
            else:
                queue.append([res["effective_date"], delta, 0.0])
            lots[sym] = queue
            continue

        f = ev["fill"]
        sym, vol = f["symbol"], int(f["volume"])
        if f["side"] == "buy":
            unit = ((f["price"] * vol + (f["fee"] or 0.0)) / vol) if vol else f["price"]
            lots.setdefault(sym, []).append([f["trade_date"], vol, unit])
            continue
        remaining = vol
        matched_qty, matched_cost, first_date = 0, 0.0, None
        queue = lots.get(sym, [])
        while remaining > 0 and queue:
            lot = queue[0]
            take = min(remaining, lot[1])
            if first_date is None:
                first_date = lot[0]
            matched_qty += take
            matched_cost += take * lot[2]
            lot[1] -= take
            remaining -= take
            if lot[1] <= 0:
                queue.pop(0)
        unmatched = remaining
        basis = matched_cost
        proceeds = f["price"] * matched_qty
        realized = (float(f["realized_pnl"]) if f.get("realized_pnl") is not None
                    else proceeds - basis)
        round_trips.append({
            "symbol": sym,
            "buy_date": first_date,
            "sell_date": f["trade_date"],
            "volume": matched_qty,
            "unmatched_volume": unmatched,
            "buy_price": round(basis / matched_qty, 4) if matched_qty else None,
            "sell_price": round(float(f["price"]), 4),
            "realized_pnl": round(realized, 2),
            "fee": round(float(f["fee"] or 0.0), 2),
            "holding_days": _holding_days(conn, first_date, f["trade_date"]),
        })
    return round_trips


def _holding_days(conn: sqlite3.Connection, buy: str | None, sell: str) -> int | None:
    if not buy:
        return None
    cal = trading_days(conn)
    if buy not in cal or sell not in cal:
        return None
    return cal.index(sell) - cal.index(buy)


def review(conn: sqlite3.Connection, *, account_id: int,
           date: str | None = None, benchmark_symbol: str = "000300") -> dict:
    """复盘：逐笔回合、绩效汇总、当前持仓、基准对照、行业分布、组合风险。

    **不重放撮合**：费用与已实现盈亏在成交时就落进了 `paper_fill`，这里只做
    汇总与 FIFO 配对；所有口径都限制在 `date`（含）之前。
    """
    # 统计必须用**完整**曲线（图可以抽样），否则超过 2000 个点之后
    # 区间收益/回撤/夏普全都只反映前一段，与用户看到的总权益对不上。
    curve = nav_curve(conn, account_id=int(account_id), date=date)
    if not curve:
        return {"days": 0, "round_trips": [], "summary": {}, "positions": [],
                "industry": [], "benchmark": None, "risk": {}, "notes": []}

    where = "account_id=?"
    params: list = [int(account_id)]
    if date:
        where += " AND trade_date <= ?"
        params.append(date)
    fills = [dict(r) for r in conn.execute(
        f"SELECT * FROM paper_fill WHERE {where} ORDER BY trade_date, id",
        tuple(params))]
    has_costs = any(f.get("fee") for f in fills) or any(
        f.get("realized_pnl") is not None for f in fills)
    acc_row = conn.execute("SELECT kind FROM paper_account WHERE id=?",
                           (int(account_id),)).fetchone()
    legacy = acc_row is not None and acc_row["kind"] == "legacy_hfq"
    if legacy:
        costs_complete = accounts_mod.meta_get(conn, int(account_id),
                                               "costs_complete")
        has_costs = bool(costs_complete)

    round_trips = _build_round_trips(conn, int(account_id), fills)
    wins = [r for r in round_trips if r["realized_pnl"] > 0]
    losses = [r for r in round_trips if r["realized_pnl"] < 0]
    gross_win = sum(r["realized_pnl"] for r in wins)
    gross_loss = -sum(r["realized_pnl"] for r in losses)
    holds = [r["holding_days"] for r in round_trips if r["holding_days"] is not None]

    last = curve[-1]
    positions = positions_at(conn, account_id=int(account_id),
                             trade_date=last["trade_date"])
    total = float(last["total"]) or 1.0
    pos_rows = []
    unrealized = 0.0
    for p in positions:
        cost = float(p["cost_basis"] or 0.0)
        pnl = float(p["market_value"]) - cost
        unrealized += pnl
        pos_rows.append({
            "symbol": p["symbol"], "volume": int(p["volume"]),
            "available_volume": int(p["available_volume"] or 0),
            "avg_price": round(cost / int(p["volume"]), 4) if int(p["volume"]) else 0.0,
            "cost_basis": round(cost, 2),
            "close": round(float(p["close"]), 4),
            "price_date": p.get("price_date"),
            "market_value": round(float(p["market_value"]), 2),
            "unrealized_pnl": round(pnl, 2),
            "weight": round(float(p["market_value"]) / total, 4),
        })

    industry: dict[str, dict] = {}
    if pos_rows:
        marks = ",".join("?" * len(pos_rows))
        rows = conn.execute(
            f"SELECT symbol, industry FROM industry_map WHERE symbol IN ({marks})",
            [p["symbol"] for p in pos_rows]).fetchall()
        ind_of = {r["symbol"]: r["industry"] for r in rows}
        for p in pos_rows:
            key = ind_of.get(p["symbol"]) or "未分类"
            agg = industry.setdefault(key, {"industry": key, "market_value": 0.0, "n": 0})
            agg["market_value"] += p["market_value"]
            agg["n"] += 1
        for agg in industry.values():
            agg["market_value"] = round(agg["market_value"], 2)
            agg["weight"] = round(agg["market_value"] / total, 4)
    industry_rows = sorted(industry.values(), key=lambda x: -x["market_value"])

    bench = None
    b_rows = conn.execute(
        "SELECT date, close FROM benchmark_bar WHERE symbol=? AND date>=? AND date<=?"
        " ORDER BY date",
        (benchmark_symbol, curve[0]["trade_date"], last["trade_date"])).fetchall()
    if len(b_rows) >= 2:
        base = float(b_rows[0]["close"]) or 1.0
        b_ret = float(b_rows[-1]["close"]) / base - 1
        s_ret = float(last["nav"]) - 1
        bench = {"symbol": benchmark_symbol, "from": b_rows[0]["date"],
                 "to": b_rows[-1]["date"], "return": round(b_ret, 4),
                 "excess": round(s_ret - b_ret, 4)}

    # 统一风险指标（与回测同一公式、同一逐日口径）
    metrics = _metrics([float(r["total"]) for r in curve],
                       [r["trade_date"] for r in curve])
    peak, mdd = 0.0, 0.0
    for r in curve:
        peak = max(peak, float(r["total"]))
        if peak > 0:
            mdd = max(mdd, 1 - float(r["total"]) / peak)

    notes = ["逐笔盈亏与费用取自成交记录（paper_fill.fee / realized_pnl）。",
             "持有天数按交易日计（口径同全项目的市场交易日）。",
             "交易盈亏口径 = 移动平均含费成本；分红与扣税单列为 dividend_income。"]
    if fills and not has_costs:
        notes.append("本账户的历史成交没有费用/已实现盈亏记录（迁移前落库或未记录），"
                     "这里的盈亏按成交价重算、**不含费用**，只作参考。")

    return {
        "days": len(curve),
        "from": curve[0]["trade_date"], "to": last["trade_date"],
        "round_trips": round_trips,
        "summary": {
            # 统一指标先铺底（逐日口径、与回测同一公式），下面的字段再覆盖：
            # 模拟页的回撤是**正幅度**，而 helper 给的是带符号的回撤。
            **{**metrics, "max_drawdown": round(mdd, 4),
               "days": len(curve), "return": round(float(last["nav"]) - 1, 4)},
            "round_trips": len(round_trips),
            "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(round_trips), 4) if round_trips else None,
            "gross_win": round(gross_win, 2), "gross_loss": round(gross_loss, 2),
            "profit_factor": round(gross_win / gross_loss, 4) if gross_loss else None,
            "realized_total": round(sum(r["realized_pnl"] for r in round_trips), 2),
            "unrealized_total": round(unrealized, 2),
            "fees_total": round(sum(float(f.get("fee") or 0.0) for f in fills), 2),
            "dividend_income": round(_dividend_income(conn, int(account_id), date), 2),
            "avg_holding_days": round(sum(holds) / len(holds), 1) if holds else None,
            "best": max((r["realized_pnl"] for r in round_trips), default=None),
            "worst": min((r["realized_pnl"] for r in round_trips), default=None),
            "max_drawdown": round(mdd, 4),
            "return": round(float(last["nav"]) - 1, 4),
            **metrics,
        },
        "positions": pos_rows,
        "industry": industry_rows,
        "risk": portfolio_risk(conn, account_id=int(account_id), date=date),
        "benchmark": bench,
        "notes": notes,
    }


def _dividend_income(conn: sqlite3.Connection, account_id: int,
                     date: str | None) -> float:
    """已确认的分红收入（含扣税）：只统计截至 `date` 已生效的权益。"""
    total = 0.0
    for a in corporate_mod.list_actions(conn, account_id):
        res = a["resolution"]
        if date and res["effective_date"] > date:
            continue
        total += float(res.get("cash_gross") or 0.0)
        for t in res.get("taxes") or []:
            if date and t["date"] > date:
                continue
            total -= float(t["amount"])
    return total
