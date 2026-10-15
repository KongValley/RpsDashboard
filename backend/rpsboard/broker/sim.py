"""自建模拟盘（mode=`local`）：本项目自己的撮合引擎，**不连任何券商**。

**为什么不用 QMT**：QMT 的模拟账户是券商提供的（期货模拟在客户端里有，股票模拟
没有），拿不到就等于没有交易对手。而 RPS 策略要验的东西 —— 限价能不能挂进去、
整手取整吃掉多少、停牌顺延多少次、最低佣金对小资金的惩罚 —— 全都可以用
**库里已有的后复权日 K** 回答，日 K 已经是回测的同一份数据，结论可直接对照。

撮合规则（刻意保守，宁可少成交也不要高估）：

- **下单**：T 日收盘后按 close(T) ∓0.5% 生成限价单 → **T+1 日内**撮合。
- **成交**：买单 `low(T+1) <= 限价`、卖单 `high(T+1) >= 限价` 才算挂进去，
  成交价取**限价本身**（不假设挂到了更好的价）。
- **涨跌停**：T+1 涨停日买不进、跌停日卖不掉（与 `analytics/limits.py` 的收益率比
  口径完全一致 —— 回测里实测过滤掉 15,333 格的规则，模拟盘不能比回测更宽松）。
- **停牌**：当日无 bar 或 `tradestatus=0` → 订单**顺延**到下一个交易日，不撤单。
- **费用**：逐笔走 `analytics/costs.py` 的 `CostModel`（佣金最低 5 元、印花税按
  日期分段、过户费、滑点），与回测同一套费率，不另造一套。

号段：本地委托号从 `ORDER_ID_BASE` 起自增，与券商号段刻意不重叠。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date as _date

from .. import db
from ..analytics import costs as costs_mod
from ..analytics.limits import NO_LIMIT_STATUSES, TOLERANCE_PCT

#: 委托价相对参考价的偏移（买 +、卖 −）。与 plan.PRICE_OFFSET 同源。
PRICE_OFFSET = 0.005

#: 本地委托号段起点。刻意选一个大号段，避免与任何券商返回的号混淆。
ORDER_ID_BASE = 900_000_000

#: 单个交易日最多撮合几笔（防御性上限，正常远小于此）
MAX_ORDERS_PER_DAY = 200


class SimError(RuntimeError):
    """模拟盘状态非法（账户不存在 / 参数越界等）。"""


@dataclass
class Bar:
    """T+1 的日 K（全后复权，与 daily_bar 一致）。"""

    symbol: str
    open: float
    high: float
    low: float
    close: float
    tradable: bool = True


@dataclass
class FillResult:
    symbol: str
    side: str
    volume: int
    price: float
    fee: float


@dataclass
class Account:
    """模拟账户的内存态：现金 + 持仓。持仓是**绝对股数**。"""

    cash: float
    positions: dict[str, int] = field(default_factory=dict)
    #: symbol → 累计成本 / 股（含费用），卖出时按比例结转
    avg_cost: dict[str, float] = field(default_factory=dict)


# ── 账户存取 ────────────────────────────────────────────────────────
def init_account(conn: sqlite3.Connection, cash: float) -> Account:
    """建（或复用）模拟账户。**已存在时不改余额** —— 重跑整个区间从哪一天
    开始，取决于库里已经跑到哪儿，而不是重新注资。"""
    row = conn.execute("SELECT cash FROM paper_account WHERE id = 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO paper_account(id, cash, created_at, updated_at) VALUES (1,?,?,?)",
            (float(cash), db.now(), db.now()))
        conn.commit()
    return load_account(conn)


def load_account(conn: sqlite3.Connection, date: str | None = None) -> Account:
    """读账户。`date` 给定时还原**该日收盘后**的持仓（默认取净值表最后一天）。"""
    row = conn.execute("SELECT cash FROM paper_account WHERE id = 1").fetchone()
    if row is None:
        raise SimError("模拟账户不存在：cli trade run --cash 1000000 --from <日期>")
    if date is None:
        last = conn.execute("SELECT MAX(trade_date) AS d FROM paper_nav").fetchone()
        date = last["d"] if last and last["d"] else None
    acc = Account(cash=float(row["cash"]))
    if date:
        for r in conn.execute(
                "SELECT symbol, volume, avg_price FROM paper_position WHERE trade_date=?",
                (date,)):
            if int(r["volume"] or 0):
                acc.positions[r["symbol"]] = int(r["volume"])
                acc.avg_cost[r["symbol"]] = float(r["avg_price"])
    return acc


def reset_account(conn: sqlite3.Connection) -> None:
    """清空模拟盘（换策略重来用）。**破坏性**，只在显式调用时执行。

    连带删掉 `mode='local'` 的计划与条目：它们是这条曲线的产物，留着会让
    页面上的「最新计划」指向一次已经被清掉的运行。
    """
    for t in ("paper_nav", "paper_position", "trade_order", "trade_fill"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM trade_plan_item WHERE plan_id IN"
                 " (SELECT id FROM trade_plan WHERE mode = 'local')")
    conn.execute("DELETE FROM trade_plan WHERE mode = 'local'")
    conn.execute("DELETE FROM paper_account")
    conn.execute("DELETE FROM paper_meta WHERE key = 'last_order_id'")
    conn.commit()


def _initial_cash(conn: sqlite3.Connection) -> float:
    v = conn.execute("SELECT value FROM paper_meta WHERE key='initial_cash'").fetchone()
    return float(v["value"]) if v else 0.0


# ── 撮合 ────────────────────────────────────────────────────────────
def _day_bars(conn: sqlite3.Connection, trade_date: str) -> dict[str, Bar]:
    """某交易日的全市场日 K（一次索引 seek，约 5500 行）。"""
    rows = conn.execute(
        "SELECT symbol, open, high, low, close, tradestatus FROM daily_bar WHERE date = ?",
        (trade_date,)).fetchall()
    out: dict[str, Bar] = {}
    for r in rows:
        if r["open"] is None or r["close"] is None:
            continue
        out[r["symbol"]] = Bar(
            symbol=r["symbol"], open=float(r["open"]), high=float(r["high"]),
            low=float(r["low"]), close=float(r["close"]),
            tradable=int(r["tradestatus"] or 0) == 1)
    return out


def _limit_ratios(conn: sqlite3.Connection, trade_date: str
                   ) -> dict[str, tuple[float | None, str]]:
    """当日涨跌停比例（实测）与状态。缺失时返回空 → 不做涨跌停过滤。"""
    out: dict[str, tuple[float | None, str]] = {}
    for r in conn.execute(
            "SELECT symbol, limit_ratio, status FROM price_limit_daily WHERE date = ?",
            (trade_date,)):
        out[r["symbol"]] = (r["limit_ratio"], r["status"] or "normal")
    return out


def settle_day(
    conn: sqlite3.Connection,
    acc: Account,
    trade_date: str,
    prev_date: str,
    *,
    cost_model: costs_mod.CostModel | None = None,
) -> dict:
    """撮合 `trade_date` 当天到期、且来自自建模拟盘的全部挂单。

    返回当天的统计：``{"filled", "pending", "canceled", "deferred", "fee"}``。
    """
    model = cost_model or costs_mod.default_cost_model(enabled=True)
    bars = _day_bars(conn, trade_date)
    ratios = _limit_ratios(conn, trade_date)
    prev_close = {s: b.close for s, b in _day_bars(conn, prev_date).items()} \
        if prev_date else {}
    on = _as_date(trade_date)

    stats = {"filled": 0, "pending": 0, "canceled": 0, "deferred": 0,
             "fee": 0.0, "trade_date": trade_date}
    pending = conn.execute(
        "SELECT * FROM trade_order WHERE trade_date = ? AND status IN"
        " ('pending','submitted','part') AND plan_id IS NOT NULL"
        " AND broker_order_id >= ? ORDER BY id",
        (trade_date, ORDER_ID_BASE)).fetchall()

    for o in pending:
        sym, side = o["symbol"], o["side"]
        bar = bars.get(sym)
        if bar is None or not bar.tradable:
            # 停牌 / 无 bar：顺延到下一交易日，**不撤单**
            stats["deferred"] += 1
            continue
        if _limit_blocked(bar, ratios.get(sym), prev_close.get(sym), side):
            stats["deferred"] += 1
            continue

        limit_px = float(o["price"])
        hit = bar.low <= limit_px if side == "buy" else bar.high >= limit_px
        if not hit:
            _set_status(conn, int(o["id"]), "canceled", err_msg="限价未触及")
            stats["canceled"] += 1
            continue

        want = int(o["volume"])
        got, fee = _apply(acc, sym, side, want, limit_px, model, on)
        if got <= 0:
            _set_status(conn, int(o["id"]), "rejected", err_msg="现金或持仓不足")
            stats["canceled"] += 1
            continue
        conn.execute(
            "INSERT OR REPLACE INTO trade_fill(trade_date, broker_order_id,"
            " broker_trade_id, symbol, side, volume, price, traded_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (trade_date, int(o["broker_order_id"]), 1, sym, side, got, limit_px, db.now()))
        _set_status(conn, int(o["id"]),
                    "filled" if got == want else "part", err_msg=None)
        stats["filled"] += 1
        stats["fee"] += fee

    conn.commit()
    return stats


def _limit_blocked(bar: Bar, ratio, prev_close: float | None, side: str) -> bool:
    """涨停买不进 / 跌停卖不掉（收益率比法，与回测同一口径）。"""
    if not ratio or ratio[0] is None or float(ratio[0]) <= 0:
        return False
    if (ratio[1] or "normal") in NO_LIMIT_STATUSES:
        return False
    if not prev_close or prev_close <= 0 or bar.open <= 0:
        return False
    pct = (bar.open / prev_close - 1) * 100.0
    limit = float(ratio[0])
    return pct >= limit - TOLERANCE_PCT if side == "buy" else pct <= -limit + TOLERANCE_PCT


def _apply(acc: Account, sym: str, side: str, want: int, price: float,
           model: costs_mod.CostModel, on) -> tuple[int, float]:
    """把一笔成交作用到账户上。现金/持仓不足时**按整手缩减**，全不够则 0。"""
    got = want
    while got > 0:
        notional = got * price
        fee = model.buy_cost(notional) if side == "buy" else model.sell_cost(notional, on)
        held = acc.positions.get(sym, 0)
        if side == "buy":
            if acc.cash >= notional + fee:
                break
        elif held >= got:
            break
        got -= 100
    if got <= 0:
        return 0, 0.0

    notional = got * price
    if side == "buy":
        fee = model.buy_cost(notional)
        total_cost = acc.avg_cost.get(sym, 0.0) * acc.positions.get(sym, 0) + notional + fee
        acc.cash -= notional + fee
        acc.positions[sym] = acc.positions.get(sym, 0) + got
        acc.avg_cost[sym] = total_cost / acc.positions[sym]
    else:
        fee = model.sell_cost(notional, on)
        basis = acc.avg_cost.get(sym, 0.0) * got
        acc.cash += notional - fee
        left = acc.positions.get(sym, 0) - got
        if left > 0:
            acc.avg_cost[sym] = (acc.avg_cost[sym] * acc.positions[sym] - basis) / left
        else:
            acc.positions.pop(sym, None)
            acc.avg_cost.pop(sym, None)
    return got, fee


# ── 记账 ────────────────────────────────────────────────────────────
def next_order_id(conn: sqlite3.Connection) -> int:
    """单调递增的本地委托号（跨进程持久化，重跑不会撞号）。

    号段起点从 `paper_meta` 读；表被清空过（`reset_account`）时退回查
    `trade_order` 的最大值，两条路都拿不到才从基址开始。
    """
    row = conn.execute("SELECT value FROM paper_meta WHERE key='last_order_id'").fetchone()
    if row:
        nxt = int(row["value"])
    else:
        mx = conn.execute("SELECT MAX(broker_order_id) AS m FROM trade_order"
                          " WHERE broker_order_id >= ?", (ORDER_ID_BASE,)).fetchone()
        nxt = (int(mx["m"]) + 1) if mx and mx["m"] else ORDER_ID_BASE
    conn.execute("INSERT OR REPLACE INTO paper_meta(key, value) VALUES ('last_order_id', ?)",
                 (str(nxt + 1),))
    conn.commit()
    return nxt


def submit(conn: sqlite3.Connection, plan_id: int, trade_date: str,
           items: list[dict], acc: Account) -> int:
    """把计划里要动的票写成 pending 委托，返回笔数（幂等：已存在的不重发）。

    **下单量由账户现算**，不直接用 `trade_plan_item.target_volume`：
    目标是绝对股数，委托量是 `abs(目标 − 现持)`。这样同一个计划无论什么时候
    执行，买卖方向与数量都不会错（目标 0 的孤儿持仓 = 全部卖出）。
    """
    n = 0
    for it in items:
        if it.get("reason") not in ("ok", "orphan") or it["side"] not in ("buy", "sell"):
            continue
        have = int(acc.positions.get(it["symbol"], 0))
        volume = abs(int(it["target_volume"]) - have)
        if volume <= 0:
            continue
        exists = conn.execute(
            "SELECT 1 FROM trade_order WHERE trade_date=? AND symbol=? AND side=?"
            " AND plan_id=?", (trade_date, it["symbol"], it["side"], plan_id)).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO trade_order(plan_id, trade_date, symbol, side, volume, price,"
            " status, broker_order_id, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,'pending',?,?,?)",
            (plan_id, trade_date, it["symbol"], it["side"], volume,
             float(it["plan_price"]), next_order_id(conn), db.now(), db.now()))
        n += 1
    conn.commit()
    return n


def _set_status(conn: sqlite3.Connection, order_id: int, status: str,
                err_msg: str | None = None) -> None:
    conn.execute("UPDATE trade_order SET status=?, err_msg=?, updated_at=? WHERE id=?",
                 (status, err_msg, db.now(), order_id))


def mark_to_market(conn: sqlite3.Connection, acc: Account, trade_date: str) -> dict:
    """按当日收盘价盯市，写 paper_position / paper_nav。同一交易日重跑覆盖。"""
    bars = _day_bars(conn, trade_date)
    mv = 0.0
    conn.execute("DELETE FROM paper_position WHERE trade_date=?", (trade_date,))
    for sym, vol in sorted(acc.positions.items()):
        if vol <= 0:
            continue
        close = bars[sym].close if sym in bars else acc.avg_cost.get(sym, 0.0)
        value = vol * close
        mv += value
        conn.execute(
            "INSERT INTO paper_position(trade_date, symbol, volume, avg_price, close,"
            " market_value) VALUES (?,?,?,?,?,?)",
            (trade_date, sym, vol, acc.avg_cost.get(sym, 0.0), close, value))
    total = acc.cash + mv
    initial = _initial_cash(conn) or total
    conn.execute(
        "INSERT OR REPLACE INTO paper_nav(trade_date, cash, market_value, total, nav,"
        " n_positions) VALUES (?,?,?,?,?,?)",
        (trade_date, acc.cash, mv, total, total / initial if initial else 1.0,
         len(acc.positions)))
    conn.execute("UPDATE paper_account SET cash=?, updated_at=? WHERE id=1",
                 (acc.cash, db.now()))
    conn.commit()
    return {"trade_date": trade_date, "cash": acc.cash, "market_value": mv,
            "total": total, "n_positions": len(acc.positions)}


def nav_curve(conn: sqlite3.Connection, limit: int = 2000) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_nav ORDER BY trade_date LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def positions_at(conn: sqlite3.Connection, trade_date: str | None = None) -> list[dict]:
    if trade_date is None:
        last = conn.execute("SELECT MAX(trade_date) AS d FROM paper_nav").fetchone()
        trade_date = last["d"] if last and last["d"] else None
    if not trade_date:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM paper_position WHERE trade_date=? ORDER BY symbol", (trade_date,))]


def summary(conn: sqlite3.Connection) -> dict:
    """一屏摘要：初始资金、当前净值、区间收益、最大回撤、持仓数。"""
    curve = nav_curve(conn)
    if not curve:
        return {"initial_cash": 0.0, "total": 0.0, "nav": 1.0, "return": 0.0,
                "max_drawdown": 0.0, "days": 0, "from": None, "to": None,
                "positions": [], "curve": []}
    first, last = curve[0], curve[-1]
    peak, mdd = 0.0, 0.0
    for r in curve:
        peak = max(peak, r["total"])
        if peak > 0:
            mdd = max(mdd, 1 - r["total"] / peak)
    return {
        "initial_cash": round(first["total"] / (first["nav"] or 1.0), 2),
        "total": round(last["total"], 2),
        "nav": round(last["nav"], 4),
        "return": round(last["nav"] - 1, 4),
        "max_drawdown": round(mdd, 4),
        "days": len(curve),
        "from": first["trade_date"], "to": last["trade_date"],
        "positions": positions_at(conn),
        "curve": curve,
    }


def _as_date(x: str) -> _date:
    return _date(*(int(v) for v in x.split("-")))


def meta_set(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO paper_meta(key, value) VALUES (?,?)",
                 (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()


# ── 逐日循环 ────────────────────────────────────────────────────────
def _trading_days(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    return [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM daily_bar WHERE date >= ? AND date <= ?"
        " ORDER BY date", (start, end))]


def run(
    conn: sqlite3.Connection,
    *,
    from_date: str,
    to_date: str | None = None,
    cash: float = 1_000_000.0,
    period: int = 60,
    threshold: float = 80.0,
    hold: int = 20,
    max_positions: int = 20,
    exclude_st: bool = True,
    resonance: bool = False,
    signal: str = "rps",
    reset: bool = False,
    progress=None,
) -> dict:
    """把「出计划 → 次日撮合 → 盯市」在 `[from_date, to_date]` 上跑一遍。

    与回测同一份选股口径（`entry_mask`），差别只在**成交是逐笔撮合出来的**，
    而不是把前瞻收益直接乘进净值 —— 这正是要看的东西。

    `signal="composite"` 时与回测同样把阈值与排序键都换成合成分
    （阈值的语义仍是 `--threshold`，只是比较对象从 RPS 变成合成分百分位）。
    """
    from ..analytics import backtest as bt
    from . import plan as plan_mod

    if signal not in bt.SIGNALS:
        raise SimError(f"signal={signal!r} 非法，只允许 {list(bt.SIGNALS)}")
    if reset:
        reset_account(conn)
    acc = init_account(conn, cash)
    meta_set(conn, "initial_cash", float(cash))
    meta_set(conn, "params", {"period": period, "threshold": threshold, "hold": hold,
                              "max_positions": max_positions, "exclude_st": exclude_st,
                              "resonance": resonance, "signal": signal})

    ctx = bt.load_context(conn, None, with_ohlcv=(signal == "composite"))
    score = None
    if signal == "composite":
        from ..analytics import factors as fac

        score = fac.composite_score(ctx, conn, period=period, hold=hold,
                                    exclude_st=exclude_st)
    # entry 只算一次：逐日循环里重算一次要几十秒
    entry = bt.entry_mask(ctx, conn, period, threshold,
                          exclude_st=exclude_st, resonance=resonance, score=score)
    sig = entry.sig_dates or set()
    dates = [d for d in _trading_days(conn, from_date, to_date or "9999-12-31")
             if d in sig]
    if not dates:
        raise SimError(f"{from_date}~{to_date or '今天'} 区间内没有信号日（rps_daily 无值）")

    step = max(1, int(hold))
    totals = {"orders": 0, "filled": 0, "canceled": 0, "deferred": 0, "fee": 0.0,
              "plans": 0}
    prev: str | None = None
    for i, day in enumerate(dates):
        if prev is not None:
            st = settle_day(conn, acc, day, prev)
            totals["filled"] += st["filled"]
            totals["canceled"] += st["canceled"]
            totals["deferred"] += st["deferred"]
            totals["fee"] += st["fee"]
        mark_to_market(conn, acc, day)
        prev = day
        # 到 day 的收盘价已知 → 为下一个交易日生成计划
        if i % step == 0 and i + 1 < len(dates):
            plan_id, _ = plan_mod.build_plan(
                conn, plan_date=day, period=period, threshold=threshold, hold=hold,
                exclude_st=exclude_st, resonance=resonance,
                max_positions=max_positions, mode="local", ctx=ctx, entry=entry,
                signal=signal, score=score,
                liquidate_orphans=True)
            _, items = plan_mod.load_plan(conn, plan_id)
            totals["orders"] += submit(conn, plan_id, dates[i + 1], items, acc)
            totals["plans"] += 1
        if progress:
            progress(i + 1, len(dates), day)

    end_nav = conn.execute(
        "SELECT total FROM paper_nav ORDER BY trade_date DESC LIMIT 1").fetchone()
    totals["final_total"] = round(float(end_nav["total"]), 2) if end_nav else 0.0
    totals["return"] = round(totals["final_total"] / cash - 1, 4) if cash else 0.0
    return totals
