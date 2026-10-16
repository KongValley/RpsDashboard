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
- **停牌**：当日无 bar 或 `tradestatus=0` → 订单**顺延**到下一个交易日，不撤单；
  顺延的订单会被后续每个交易日继续尝试，直到成交或撤销（不会静默卡死）。
- **费用**：逐笔走 `analytics/costs.py` 的 `CostModel`（佣金最低 5 元、印花税按
  日期分段、过户费、滑点），与回测同一套费率，不另造一套。

号段：本地委托号从 `ORDER_ID_BASE` 起自增，与券商号段刻意不重叠。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime

from .. import config, db
from ..analytics import costs as costs_mod
from ..analytics.limits import NO_LIMIT_STATUSES, TOLERANCE_PCT

#: 委托价相对参考价的偏移（买 +、卖 −）。与 plan.PRICE_OFFSET 同源。
PRICE_OFFSET = 0.005

#: 本地委托号段起点。刻意选一个大号段，避免与任何券商返回的号混淆。
ORDER_ID_BASE = 900_000_000

#: 单个交易日最多撮合几笔（防御性上限，正常远小于此）
MAX_ORDERS_PER_DAY = 200

#: `paper_meta['params']` 的补全基准：老曲线落库时可能缺键（如 signal），
#: 缺失项按这套默认值补齐后再与本次请求比较。
DEFAULT_PARAMS = {
    "period": config.DEFAULT_PERIOD, "threshold": 80.0, "hold": 20,
    "max_positions": 20, "exclude_st": True, "resonance": False, "signal": "rps",
}


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

    含**此前顺延下来**的挂单（停牌 / 涨跌停当日撮合不了，日期不变、状态仍是
    `pending`）：它们会一直重试到能成交或被撤销为止。

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
    # 顺延（停牌 / 涨跌停）会把订单留在**原 `trade_date`** 上，所以只捞「当天
    # 日期」的挂单是错的：第二天按新日期查询就再也看不到它，委托会永久卡在
    # `pending`，既不成交也不撤销（文档承诺的"顺延到下一个交易日"从未生效）。
    # 这里取「到本日为止仍未成交的全部挂单」，按原日期顺序补撮合。
    pending = conn.execute(
        "SELECT * FROM trade_order WHERE status IN ('pending','submitted','part')"
        " AND plan_id IS NOT NULL AND broker_order_id >= ? AND trade_date <= ?"
        " ORDER BY trade_date, id",
        (ORDER_ID_BASE, trade_date)).fetchall()
    if len(pending) > MAX_ORDERS_PER_DAY:
        # 防御性上限：正常调仓远小于此。多余的留到下一个交易日结算，
        # 不丢单、也不让异常数据把一天的处理时间拖爆。
        pending = pending[:MAX_ORDERS_PER_DAY]

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
        got, fee, realized, unit_cost = _apply(acc, sym, side, want, limit_px, model, on)
        if got <= 0:
            _set_status(conn, int(o["id"]), "rejected", err_msg="现金或持仓不足")
            stats["canceled"] += 1
            continue
        conn.execute(
            "INSERT OR REPLACE INTO trade_fill(trade_date, broker_order_id,"
            " broker_trade_id, symbol, side, volume, price, traded_at, fee,"
            " realized_pnl, avg_cost) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (trade_date, int(o["broker_order_id"]), 1, sym, side, got, limit_px,
             db.now(), fee, realized, unit_cost))
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
           model: costs_mod.CostModel, on) -> tuple[int, float, float | None, float | None]:
    """把一笔成交作用到账户上。现金/持仓不足时**按整手缩减**，全不够则 0。

    返回 ``(成交股数, 费用, 已实现盈亏, 成本单价)``：买入笔的已实现盈亏为
    ``None``、成本单价是含费成本；卖出笔的已实现盈亏 = 卖出净收入 − 账面成本。
    这些数字过去算完就丢，导致事后复盘只能靠重放（而重放会改写曲线）。
    """
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
        return 0, 0.0, None, None

    notional = got * price
    if side == "buy":
        fee = model.buy_cost(notional)
        total_cost = acc.avg_cost.get(sym, 0.0) * acc.positions.get(sym, 0) + notional + fee
        acc.cash -= notional + fee
        acc.positions[sym] = acc.positions.get(sym, 0) + got
        acc.avg_cost[sym] = total_cost / acc.positions[sym]
        unit_cost = (notional + fee) / got
        return got, fee, None, unit_cost

    have_before = acc.positions.get(sym, 0)
    fee = model.sell_cost(notional, on)
    unit_cost = acc.avg_cost.get(sym, 0.0)
    basis = unit_cost * got
    realized = notional - fee - basis
    acc.cash += notional - fee
    left = have_before - got
    if left > 0:
        # **必须把持仓改小**：只更新 avg_cost 而不改 volume 的话，账户还以为
        # 自己握着卖掉的股数 —— 下次调仓会照着这个虚高的数量再卖一次
        # （实测 7800 卖掉 100 之后又卖掉 7800，凭空多出 100 股的现金）。
        acc.positions[sym] = left
        acc.avg_cost[sym] = (unit_cost * have_before - basis) / left
    else:
        acc.positions.pop(sym, None)
        acc.avg_cost.pop(sym, None)
    return got, fee, realized, unit_cost


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


# ── 曲线归档（策略对照）─────────────────────────────────────────────
def archive(conn: sqlite3.Connection, label: str | None = None) -> dict:
    """把**当前**模拟盘曲线归档一份。返回归档行的摘要。

    模拟盘只有一个活动账户（`paper_account.id CHECK (id=1)`），跑第二种策略
    必须先 `--reset` 清掉第一种。归档就是把清掉之前的证据留下来 ——
    此前这份「RPS vs 合成分」的对照只能手写进 README。
    """
    s = summary(conn)
    if not s["days"]:
        raise SimError("模拟盘还没有曲线，无可归档")
    rev = review(conn)
    params = meta_get(conn, "params") or {}
    name = (label or "").strip() or (
        f"{params.get('signal', 'rps')}-{datetime.now():%Y%m%d-%H%M%S}")
    metrics = {**(rev.get("summary") or {}), "benchmark": rev.get("benchmark")}
    cur = conn.execute(
        "INSERT INTO paper_archive(label, params, initial_cash, from_date, to_date,"
        " total, nav, total_return, max_drawdown, days, metrics, curve, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, json.dumps(params, ensure_ascii=False), float(s["initial_cash"]),
         s["from"], s["to"], float(s["total"]), float(s["nav"]),
         float(s["return"]), float(s["max_drawdown"]), int(s["days"]),
         json.dumps(metrics, ensure_ascii=False),
         json.dumps(s["curve"], ensure_ascii=False), db.now()))
    conn.commit()
    return {"id": int(cur.lastrowid), "label": name, "days": s["days"],
            "total": s["total"], "return": s["return"]}


def list_archives(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """最近的归档（新的在前）。**含曲线**，供对照页叠加画图。"""
    rows = conn.execute(
        "SELECT * FROM paper_archive ORDER BY id DESC LIMIT ?",
        (max(1, int(limit)),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for key in ("params", "metrics", "curve"):
            try:
                d[key] = json.loads(d[key])
            except (TypeError, ValueError):
                d[key] = {} if key != "curve" else []
        out.append(d)
    return out


def delete_archive(conn: sqlite3.Connection, arch_id: int) -> bool:
    """删掉一份归档；返回是否真的删到了。"""
    cur = conn.execute("DELETE FROM paper_archive WHERE id = ?", (int(arch_id),))
    conn.commit()
    return bool(cur.rowcount)


def _trading_calendar(conn: sqlite3.Connection) -> dict[str, int]:
    """交易日序号表（用 `rps_daily` 的日期，与全项目同一套交易日口径）。"""
    return {r["date"]: i for i, r in enumerate(conn.execute(
        "SELECT DISTINCT date FROM rps_daily ORDER BY date"))}


def review(conn: sqlite3.Connection, *, benchmark_symbol: str = "000300") -> dict:
    """模拟盘复盘：逐笔回合、绩效汇总、当前持仓、基准对照、行业分布。

    **不重放撮合**：费用与已实现盈亏在成交时就落进了 `trade_fill`
    （`fee` / `realized_pnl` / `avg_cost`），这里只做汇总与 FIFO 配对。
    老库的历史成交没有这些列（迁移后是 0/NULL），此时退化成"按价格重算、
    不含费用"，并在 `notes` 里说明 —— 假装有精确费用比不给更糟。
    """
    curve = nav_curve(conn)
    if not curve:
        return {"days": 0, "round_trips": [], "summary": {}, "positions": [],
                "industry": [], "benchmark": None, "notes": []}

    cal = _trading_calendar(conn)
    fills = [dict(r) for r in conn.execute(
        "SELECT trade_date, symbol, side, volume, price, fee, realized_pnl, avg_cost"
        " FROM trade_fill ORDER BY trade_date, broker_order_id, broker_trade_id")]
    has_costs = any(f.get("fee") for f in fills) or any(
        f.get("realized_pnl") is not None for f in fills)

    # FIFO 配对：只用来算「持有交易日数」与买入均价，盈亏以落库值为准
    lots: dict[str, list[list]] = {}
    round_trips: list[dict] = []
    for f in fills:
        sym, vol = f["symbol"], int(f["volume"])
        if f["side"] == "buy":
            unit = (f["price"] * vol + (f["fee"] or 0.0)) / vol if vol else f["price"]
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
        # 卖出的股数多于账上（老库的"部分卖出没减仓"缺陷留下的印记）：
        # 多出来的部分没有买入成本可配，单列出来而不是硬凑一个 0 成本。
        unmatched = remaining
        basis = matched_cost
        proceeds = f["price"] * matched_qty
        if f.get("realized_pnl") is not None:
            realized = float(f["realized_pnl"])
        else:
            realized = proceeds - basis          # 老库：不含费用
        buy_date = first_date
        round_trips.append({
            "symbol": sym,
            "buy_date": buy_date,
            "sell_date": f["trade_date"],
            "volume": matched_qty,
            "unmatched_volume": unmatched,
            "buy_price": round(basis / matched_qty, 4) if matched_qty else None,
            "sell_price": round(float(f["price"]), 4),
            "realized_pnl": round(realized, 2),
            "fee": round(float(f["fee"] or 0.0), 2),
            "holding_days": (cal.get(f["trade_date"], 0) - cal.get(buy_date, 0))
                            if buy_date in cal else None,
        })

    wins = [r for r in round_trips if r["realized_pnl"] > 0]
    losses = [r for r in round_trips if r["realized_pnl"] < 0]
    gross_win = sum(r["realized_pnl"] for r in wins)
    gross_loss = -sum(r["realized_pnl"] for r in losses)
    holds = [r["holding_days"] for r in round_trips if r["holding_days"] is not None]

    last = curve[-1]
    positions = positions_at(conn)
    total = float(last["total"]) or 1.0
    pos_rows = []
    unrealized = 0.0
    for p in positions:
        pnl = (float(p["close"]) - float(p["avg_price"])) * int(p["volume"])
        unrealized += pnl
        pos_rows.append({
            "symbol": p["symbol"], "volume": int(p["volume"]),
            "avg_price": round(float(p["avg_price"]), 4),
            "close": round(float(p["close"]), 4),
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
            agg = industry.setdefault(key, {"industry": key, "market_value": 0.0,
                                            "n": 0})
            agg["market_value"] += p["market_value"]
            agg["n"] += 1
        for agg in industry.values():
            agg["market_value"] = round(agg["market_value"], 2)
            agg["weight"] = round(agg["market_value"] / total, 4)
    industry_rows = sorted(industry.values(), key=lambda x: -x["market_value"])

    # 基准对照：与回测读的是同一张 benchmark_bar（000300）
    bench = None
    b_rows = conn.execute(
        "SELECT date, close FROM benchmark_bar WHERE symbol = ?"
        " AND date >= ? AND date <= ? ORDER BY date",
        (benchmark_symbol, curve[0]["trade_date"], last["trade_date"])).fetchall()
    if len(b_rows) >= 2:
        base = float(b_rows[0]["close"]) or 1.0
        b_ret = float(b_rows[-1]["close"]) / base - 1
        s_ret = float(last["nav"]) - 1
        bench = {
            "symbol": benchmark_symbol,
            "from": b_rows[0]["date"], "to": b_rows[-1]["date"],
            "return": round(b_ret, 4),
            "excess": round(s_ret - b_ret, 4),
        }

    peak, mdd = 0.0, 0.0
    for r in curve:
        peak = max(peak, float(r["total"]))
        if peak > 0:
            mdd = max(mdd, 1 - float(r["total"]) / peak)

    notes = ["逐笔盈亏与费用取自成交记录（trade_fill.fee / realized_pnl）。",
             "持有天数按交易日计（口径同全项目的 rps_daily 交易日）。"]
    if not has_costs:
        notes.append("本库的历史成交没有费用/已实现盈亏列（迁移前落库），"
                     "这里的盈亏按成交价重算、**不含费用**，只作参考。")

    return {
        "days": len(curve),
        "from": curve[0]["trade_date"], "to": last["trade_date"],
        "round_trips": round_trips,
        "summary": {
            "round_trips": len(round_trips),
            "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(round_trips), 4) if round_trips else None,
            "gross_win": round(gross_win, 2), "gross_loss": round(gross_loss, 2),
            "profit_factor": round(gross_win / gross_loss, 4) if gross_loss else None,
            "realized_total": round(sum(r["realized_pnl"] for r in round_trips), 2),
            "unrealized_total": round(unrealized, 2),
            # 累计费用取**全部**成交（买+卖）：与 `cli trade run` 打印的那个数同口径。
            # 回合里带的 fee 只是卖出那一笔，买入费已经摊进 basis/realized 里了。
            "fees_total": round(sum(float(f.get("fee") or 0.0) for f in fills), 2),
            "avg_holding_days": round(sum(holds) / len(holds), 1) if holds else None,
            "best": max((r["realized_pnl"] for r in round_trips), default=None),
            "worst": min((r["realized_pnl"] for r in round_trips), default=None),
            "max_drawdown": round(mdd, 4),
            "return": round(float(last["nav"]) - 1, 4),
        },
        "positions": pos_rows,
        "industry": industry_rows,
        "benchmark": bench,
        "notes": notes,
    }


def _as_date(x: str) -> _date:
    return _date(*(int(v) for v in x.split("-")))


def meta_set(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO paper_meta(key, value) VALUES (?,?)",
                 (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()


def meta_get(conn: sqlite3.Connection, key: str):
    """读 `paper_meta` 里的一条；不存在或不是合法 JSON 时返回 None。"""
    try:
        row = conn.execute("SELECT value FROM paper_meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except ValueError:
        return None


def last_settled_date(conn: sqlite3.Connection) -> str | None:
    """净值曲线最后一个已结算日；曲线为空时 None。"""
    row = conn.execute("SELECT MAX(trade_date) AS d FROM paper_nav").fetchone()
    return row["d"] if row and row["d"] else None


def _first_nav_date(conn: sqlite3.Connection) -> str | None:
    """净值曲线第一天。老库没有 `run_start_date` 时用它当曲线起点。

    曲线的第一天 == 原始 `from_date` 之后的**第一个信号日**（循环第一天就盯市），
    所以用它重算全局信号日序列，调仓节奏与原来完全一致。
    """
    row = conn.execute("SELECT MIN(trade_date) AS d FROM paper_nav").fetchone()
    return row["d"] if row and row["d"] else None


def resume_params(conn: sqlite3.Connection) -> dict:
    """续跑一条已有曲线该用的参数（`paper_meta.params` 按默认值补齐）。

    供「一键更新」里的模拟盘阶段使用：它不该自己写一套默认值，否则会和
    曲线创建时的参数不一致而被 `run()` 拒绝。
    """
    return {**DEFAULT_PARAMS, **(meta_get(conn, "params") or {})}


def _plan_exists(conn: sqlite3.Connection, plan_date: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM trade_plan WHERE mode = 'local' AND plan_date = ? LIMIT 1",
        (plan_date,)).fetchone()
    return row is not None



# ── 逐日循环 ────────────────────────────────────────────────────────
def _trading_days(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    return [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM daily_bar WHERE date >= ? AND date <= ?"
        " ORDER BY date", (start, end))]


def run(
    conn: sqlite3.Connection,
    *,
    from_date: str | None = None,
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
    """把「出计划 → 次日撮合 → 盯市」推进到 `to_date`（缺省至今）。

    与回测同一份选股口径（`entry_mask`），差别只在**成交是逐笔撮合出来的**，
    而不是把前瞻收益直接乘进净值 —— 这正是要看的东西。

    `signal="composite"` 时与回测同样把阈值与排序键都换成合成分
    （阈值的语义仍是 `--threshold`，只是比较对象从 RPS 变成合成分百分位）。

    **可重复调用（续跑）**：已经结算过的交易日一律跳过，绝不重算。
    早先的实现每次都从 `from_date` 重走，而账户是从**当前**状态（`paper_nav`
    最后一天的现金与持仓）载入的 —— 于是重跑一次就把整条历史净值改成
    「用今天的持仓去估那天的价」，实测 41/41 个已结算日、475 行持仓全部被改写。
    要「推进到今天」只需再跑一次同样的命令；换参数或不续跑请显式 `--reset`。

    `from_date` 只在**首次建曲线**时决定起点；已有曲线时它被忽略（曲线起点
    存在 `paper_meta.run_start_date`，改起点要 `--reset`）。因此日常推进可以直接
    调用 `run(conn)` 不带起点。
    """
    from ..analytics import backtest as bt
    from . import plan as plan_mod

    if signal not in bt.SIGNALS:
        raise SimError(f"signal={signal!r} 非法，只允许 {list(bt.SIGNALS)}")
    params = {"period": period, "threshold": threshold, "hold": hold,
              "max_positions": max_positions, "exclude_st": exclude_st,
              "resonance": resonance, "signal": signal}
    if reset:
        reset_account(conn)
    acc = init_account(conn, cash)
    last = last_settled_date(conn)
    if last:
        # 续跑：曲线已经存在，起点、资金与参数都必须与它一致，否则这条曲线
        # 就不是同一个策略的净值了（静默混进去比报错难查得多）。
        stored = meta_get(conn, "params") or {}
        if stored:
            # 老曲线（本模板新增 signal 之前）落库的 params 缺 signal 键；
            # 缺失项按**当时的默认值**补齐再比，避免把一条正常的 91 天曲线
            # 判成"参数不一致"而迫使 --reset 把它毁掉。
            stored_full = {**DEFAULT_PARAMS, **stored}
            if stored_full != params:
                raise SimError(
                    f"模拟盘已有曲线（结算到 {last}），参数与本次不一致："
                    f"{stored_full} ≠ {params}。换参数请加 --reset 重建。")
        stored_cash = _initial_cash(conn)
        if stored_cash and abs(float(stored_cash) - float(cash)) > 0.01:
            raise SimError(
                f"模拟盘初始资金是 {stored_cash:.2f}，与本次 --cash {cash:.2f} 不一致。"
                "改初始资金请加 --reset 重建。")
        # 曲线起点固定：`run_start_date` 缺失（老库）时退回**净值第一天** ——
        # 那正是原始 from_date 落在的第一个信号日，调仓节奏照样对得上。
        run_start = meta_get(conn, "run_start_date") or _first_nav_date(conn) or from_date
        if run_start is None:
            raise SimError("模拟盘曲线缺少起始日，无法续跑；请用 --reset 重建。")
    else:
        run_start = from_date
        if not run_start:
            raise SimError("首次运行必须指定起始信号日（cli trade run --from <日期>）")
    meta_set(conn, "initial_cash", float(cash))
    meta_set(conn, "params", params)
    meta_set(conn, "run_start_date", run_start)

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
    # 全局信号日序列：调仓节奏（i % step）建立在**整条曲线**上，续跑才接得上。
    # 只截取「本次之后」的话，续跑会从 0 重新数节奏，调仓点整体错位。
    dates = [d for d in _trading_days(conn, run_start, to_date or "9999-12-31")
             if d in sig]
    if not dates:
        raise SimError(f"{run_start}~{to_date or '今天'} 区间内没有信号日（rps_daily 无值）")

    step = max(1, int(hold))
    totals = {"orders": 0, "filled": 0, "canceled": 0, "deferred": 0, "fee": 0.0,
              "plans": 0, "days": 0}

    def _plan_and_submit(k: int) -> None:
        """在第 k 个信号日收盘后为下一个交易日挂单。"""
        if k % step or k + 1 >= len(dates) or _plan_exists(conn, dates[k]):
            return
        plan_id, _ = plan_mod.build_plan(
            conn, plan_date=dates[k], period=period, threshold=threshold, hold=hold,
            exclude_st=exclude_st, resonance=resonance,
            max_positions=max_positions, mode="local", ctx=ctx, entry=entry,
            signal=signal, score=score,
            liquidate_orphans=True)
        _, items = plan_mod.load_plan(conn, plan_id)
        totals["orders"] += submit(conn, plan_id, dates[k + 1], items, acc)
        totals["plans"] += 1

    if last is not None and last in dates:
        # 上一次运行停在这一天：它的收盘计划还没生成（循环里 `i + 1 < len` 不成立），
        # 续跑必须先补上，否则第一个新交易日没有任何挂单可撮合。
        _plan_and_submit(dates.index(last))

    for k, day in enumerate(dates):
        if last is not None and day <= last:
            continue                      # 已结算，绝不重算
        prev = dates[k - 1] if k > 0 else None
        if prev is not None:
            st = settle_day(conn, acc, day, prev)
            totals["filled"] += st["filled"]
            totals["canceled"] += st["canceled"]
            totals["deferred"] += st["deferred"]
            totals["fee"] += st["fee"]
        mark_to_market(conn, acc, day)
        totals["days"] += 1
        _plan_and_submit(k)
        if progress:
            progress(totals["days"], len(dates), day)

    end_nav = conn.execute(
        "SELECT total FROM paper_nav ORDER BY trade_date DESC LIMIT 1").fetchone()
    totals["final_total"] = round(float(end_nav["total"]), 2) if end_nav else 0.0
    totals["return"] = round(totals["final_total"] / cash - 1, 4) if cash else 0.0
    totals["resumed_from"] = last
    return totals
