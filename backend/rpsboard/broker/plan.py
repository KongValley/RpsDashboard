"""交易计划（P2 影子 / P3 模拟盘的共同输入）。

**口径来源只有一个**：`analytics.backtest.entry_mask`。回测能买的票，计划就
允许买 —— 两边各写一套时，最先坏掉的是那种没法排查的偏差（回测显示买得进、
计划却空手）。

计划本身**不下单**：它落库成 `trade_plan` / `trade_plan_item`，由
`cli trade shadow` 做影子成交、或 `cli trade execute` 送进券商模拟盘。

委托价 = QMT 最新价 ∓0.5% 的**限价**，与 `costs.CostModel.slippage_rate`
（0.05%）对称：成本模型按滑点已成交估算，限价单再留半个百分点余量，
宁可成交不上也不成交在更差的价格上。
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime

import pandas as pd

from .. import config, db
from ..analytics import backtest as bt
from . import qmt

#: 委托价相对最新价的偏移（买 +、卖 −）。保守滑点。
PRICE_OFFSET = 0.005

#: A 股最小交易单位
LOT = 100


class PlanError(RuntimeError):
    """计划不可生成 / 不可执行。"""


def _cash_budget_from_account(cash_budget: float, conn: sqlite3.Connection,
                              *, local: bool = False) -> float:
    """cash_budget=0 → 用账户权益的 95%（留 5% 给佣金与价格波动）。

    **权益 = 现金 + 持仓市值**，不是只有现金：这是一个等权再平衡的组合，
    每期要重新分配的是全部权益。只按现金算的话，买入越多现金越少、
    每只分到的钱越少，最后再也买不进任何新票（实测 91 天后持仓涨到 42 只）。
    """
    if cash_budget and cash_budget > 0:
        return float(cash_budget)
    if local:
        row = conn.execute("SELECT cash FROM paper_account WHERE id = 1").fetchone()
        cash = float(row["cash"]) if row else 0.0
        mv = conn.execute(
            "SELECT COALESCE(SUM(market_value), 0) AS mv FROM paper_position"
            " WHERE trade_date = (SELECT MAX(trade_date) FROM paper_nav)").fetchone()
        equity = cash + float(mv["mv"] or 0.0)
        if equity <= 0:
            raise PlanError("no_account：自建模拟盘尚未初始化（cli trade run --cash N）")
        return equity * 0.95
    try:
        asset = qmt.asset() or {}
    except qmt.QmtError as exc:
        raise PlanError(f"no_account：cash_budget=0 且取不到 QMT 可用现金（{exc}）") from exc
    cash = float(asset.get("cash") or 0.0)
    mv = float(asset.get("market_value") or 0.0)
    equity = cash + mv
    if equity <= 0:
        raise PlanError("no_account：QMT 账户权益为 0，无法推导 cash_budget")
    return equity * 0.95


def _held_map(conn: sqlite3.Connection, *, local: bool) -> dict[str, int]:
    """现持股数。local 读 `paper_position`，否则读券商回报。"""
    if not local:
        return {p["symbol"]: int(p.get("volume") or 0) for p in (qmt.positions() or [])}
    from . import sim

    return {p["symbol"]: int(p["volume"]) for p in sim.positions_at(conn)}


def _price_map(conn: sqlite3.Connection, plan_date: str, symbols: list[str],
               *, local: bool) -> dict[str, float]:
    """参考价。local 取库内收盘价（后复权），否则取 QMT 最新价（不复权）。"""
    if not symbols:
        return {}
    if not local:
        return qmt.tick(symbols)
    marks = ",".join("?" * len(symbols))
    rows = conn.execute(
        f"SELECT symbol, close FROM daily_bar WHERE date = ? AND symbol IN ({marks})",
        [plan_date, *symbols]).fetchall()
    return {r["symbol"]: float(r["close"]) for r in rows if r["close"]}


def _in_clause(symbols: list[str]) -> str:
    return "(" + ",".join("?" * len(symbols)) + ")" if symbols else "(NULL)"


def _limits(conn: sqlite3.Connection, plan_date: str, symbols: list[str]) -> dict[str, dict]:
    rows = conn.execute(
        f"SELECT symbol, limit_up, limit_down, status FROM price_limit_daily"
        f" WHERE date = ? AND symbol IN {_in_clause(symbols)}",
        [plan_date, *symbols],
    ).fetchall()
    return {r["symbol"]: dict(r) for r in rows}


def _bar_status(conn: sqlite3.Connection, plan_date: str, symbols: list[str]) -> dict[str, int]:
    rows = conn.execute(
        f"SELECT symbol, tradestatus FROM daily_bar"
        f" WHERE date = ? AND symbol IN {_in_clause(symbols)}",
        [plan_date, *symbols],
    ).fetchall()
    return {r["symbol"]: r["tradestatus"] for r in rows}


def _lot_volume(per_name: float, price: float) -> int:
    """按整手取整：不足一手就是 0（记 too_small），不向上凑整。"""
    if price <= 0 or per_name <= 0:
        return 0
    return int(math.floor(per_name / price / LOT) * LOT)


def _sort_score(scores: pd.Series, sym: str) -> float:
    """排序用的打分：取不到（NaN）当作 -inf，排在最后。"""
    v = scores.get(sym)
    return -float("inf") if v is None or v != v else float(v)


def build_plan(
    conn: sqlite3.Connection,
    *,
    plan_date: str,
    period: int = config.DEFAULT_PERIOD,
    threshold: float = 80.0,
    hold: int = 20,
    exclude_st: bool = True,
    resonance: bool = False,
    max_positions: int = 20,
    cash_budget: float = 0.0,
    mode: str = "paper",
    ctx: dict | None = None,
    entry: bt.EntryMask | None = None,
    liquidate_orphans: bool = False,
    signal: str = "rps",
    score: pd.DataFrame | None = None,
) -> tuple[int, int]:
    """生成并落库一份交易计划，返回 `(plan_id, item_count)`。

    `trade_plan_item.target_volume` 存的是**目标总股数（绝对目标）**，
    不是「要下多少股」—— 对账（`risk.reconcile_positions`）要拿它直接和
    券商回报的现持比对，只有绝对目标才比得出来。实际下单量由执行器按
    `abs(target_volume − 现持)` 现算。

    每一只候选都会落成一行，包括被跳过的那些（reason 写明原因）——
    「为什么这只没买」必须看得见，不能靠猜。

    `mode="local"`（自建模拟盘）时**不连 QMT**：参考价取库内 `daily_bar.close`，
    现持取 `paper_position`。`ctx` / `entry` / `score` 可由调用方预先算好传入
    （逐日循环里算一次就够，每次重算要几十秒到几分钟）。

    `signal="composite"` 时**取前 M 只的排序键换成合成分**（`score`）；
    `signal="rps"` 时排序键保持 `ret{period}` 不变（README 已写明该口径，
    改了会让已记录在案的模拟盘数字悄悄对不上）。
    """
    SIGNALS = bt.SIGNALS
    if mode not in config.TRADE_MODES:
        raise PlanError(
            f"mode={mode!r} 非法，只允许 {list(config.TRADE_MODES)}（本项目没有实盘模式）"
        )
    if signal not in SIGNALS:
        raise PlanError(f"signal={signal!r} 非法，只允许 {list(SIGNALS)}")
    if max_positions < 1:
        raise PlanError(f"max_positions 必须 ≥ 1，收到 {max_positions}")
    local = mode == "local"
    held = _held_map(conn, local=local)
    budget = _cash_budget_from_account(cash_budget, conn, local=local)
    per_name = budget / max_positions

    if signal == "composite" and (ctx is None or "high" not in ctx):
        # 合成因子要 OHLCV；调用方给的 ctx 可能是不带 OHLCV 的那份
        ctx = bt.load_context(conn, None, with_ohlcv=True)
    ctx = ctx if ctx is not None else bt.load_context(conn, None)
    if entry is None:
        entry = bt.entry_mask(ctx, conn, period, threshold,
                              exclude_st=exclude_st, resonance=resonance,
                              score=score)
    if plan_date not in entry.mask.index:
        raise PlanError(f"{plan_date} 不是信号日（rps_daily 无值或不在回测矩阵内）")
    sel = entry.mask.loc[plan_date]
    names = sorted(sel.index[sel.fillna(False).astype(bool)])

    # 取前 max_positions：无论用哪个排序键都是**静态**的（只用信号日之前的行情），
    # 不是前瞻收益 —— 用 fwd 排序等于按未来挑股票。
    if signal == "composite":
        from ..analytics import factors as fac

        if score is None:
            score = fac.composite_score(ctx, conn, period=period, hold=hold,
                                       exclude_st=exclude_st)
        if plan_date not in score.index:
            raise PlanError(f"{plan_date} 不是信号日（rps_daily 无值或不在回测矩阵内）")
        s = score.loc[plan_date]
        # 取不到合成分（NaN）的票排最后：排不出名次就不该占名额
        names.sort(key=lambda sym: (-_sort_score(s, sym), sym))
        key_desc = "合成分"
    else:
        ret = bt.get_frames(ctx, exclude_st)[f"ret{period}"].loc[plan_date]
        names.sort(key=lambda s: (-(ret.get(s) or float("-inf")), s))
        key_desc = f"ret{period}"
    candidates = names[:max_positions]
    orphans = sorted(s for s, v in held.items() if v > 0 and s not in set(candidates))
    # 孤儿持仓也要价：清仓同样要挂限价单，取不到价就卖不掉
    ticks = _price_map(conn, plan_date, sorted(set(candidates) | set(orphans)), local=local)
    limits = _limits(conn, plan_date, candidates)
    statuses = _bar_status(conn, plan_date, candidates)

    items: list[dict] = []
    skipped: dict[str, int] = {}
    # 越界校验只在**同一价格基准**下成立：`price_limit_daily.limit_up/down` 是不复权
    # 价，QMT 最新价也是不复权，两者可比；而 `daily_bar.close` 是**后复权**价，
    # 直接比会凭空判出一大批「越界」（实测 20 只候选里 12 只被误杀）。
    # 后复权口径的涨跌停由撮合引擎按收益率比处理（broker/sim.py::_limit_blocked），
    # 与回测用的是同一条规则。
    check_limits = not local
    for sym in candidates:
        reason, ref, price, target, side = "ok", 0.0, None, 0, "hold"
        if statuses.get(sym) is None or not int(statuses.get(sym) or 0):
            reason = "suspended"
        elif sym not in limits:
            # 没有实测涨跌停行就不推算：宁可不出计划，也不拿猜出来的价位发单
            reason = "no_limit_row"
        else:
            last = ticks.get(sym)
            if not last or float(last) <= 0:
                reason = "no_tick"
            else:
                ref = round(float(last), 4)
                lim = limits[sym]
                has_limit = (lim.get("status") or "normal") != "no_price_limit"
                buy_px = round(ref * (1 + PRICE_OFFSET), 2)
                sell_px = round(ref * (1 - PRICE_OFFSET), 2)
                want = _lot_volume(per_name, buy_px)
                in_band = (not check_limits) or not has_limit or (
                    float(lim["limit_down"] or 0) <= buy_px <= float(lim["limit_up"] or 0))
                if want <= 0:
                    reason = "too_small"
                elif not in_band:
                    reason = "price_out_of_limit"
                else:
                    target = want            # 绝对目标股数
                    have = int(held.get(sym) or 0)
                    if have > want:
                        side, price = "sell", sell_px
                        if check_limits and has_limit and not (
                                float(lim["limit_down"] or 0) <= sell_px
                                <= float(lim["limit_up"] or 0)):
                            side, price, reason = "hold", None, "price_out_of_limit"
                    elif have < want:
                        side, price = "buy", buy_px
                    else:
                        side, price = "hold", None

        if reason != "ok":
            skipped[reason] = skipped.get(reason, 0) + 1
        items.append({
            "symbol": sym, "side": side, "reason": reason,
            "target_volume": int(target), "ref_price": float(ref), "plan_price": price,
            "held": int(held.get(sym) or 0),
        })

    # 掉出候选的旧持仓：目标 0 → 全部卖出。不这么做的话组合只会越滚越大
    # （实测跑 91 天后持仓 27 只 > max_positions 20），策略已经不是原来那个。
    if liquidate_orphans:
        for sym in orphans:
            ref = round(float(ticks.get(sym) or 0.0), 4)
            if ref <= 0:
                items.append({
                    "symbol": sym, "side": "hold", "reason": "orphan_no_price",
                    "target_volume": 0, "ref_price": 0.0, "plan_price": None,
                    "held": int(held[sym]),
                })
                skipped["orphan_no_price"] = skipped.get("orphan_no_price", 0) + 1
                continue
            items.append({
                "symbol": sym, "side": "sell", "reason": "orphan",
                "target_volume": 0, "ref_price": ref,
                "plan_price": round(ref * (1 - PRICE_OFFSET), 2),
                "held": int(held[sym]),
            })

    # 卖单排在前面：卖出的回款要用于后面的买入
    items.sort(key=lambda it: {"sell": 0, "buy": 1, "hold": 2}[it["side"]])

    params = {
        "period": period, "threshold": threshold, "hold": hold,
        "exclude_st": exclude_st, "resonance": resonance,
        "max_positions": max_positions, "per_name": round(per_name, 2),
        "candidates": len(candidates), "selected": len(names),
        "skipped": skipped, "limit_warnings": entry.warnings,
        "orphans": orphans, "liquidate_orphans": bool(liquidate_orphans),
        "price_source": "daily_bar.close" if local else "qmt.tick",
        "signal": signal,
    }
    note = (
        f"目标 = 回测口径的 {len(names)} 只候选里按 {key_desc} 取前 {len(candidates)} 只"
        f"（回测为全市场等权，共 {len(names)} 只）；"
        f"委托价 = {'库内收盘价' if local else 'QMT 最新价'} ∓{PRICE_OFFSET:.1%} 的限价；"
        + (f"掉出候选的 {len(orphans)} 只持仓本次清仓；"
           if liquidate_orphans else f"不在候选内的持仓 {len(orphans)} 只，本次不动；")
        + "未建模：分红送转对持股数的影响以券商回报为准。"
    )

    cur = conn.execute(
        "INSERT INTO trade_plan(plan_date, mode, params, cash_budget, status, note, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (plan_date, mode, json.dumps(params, ensure_ascii=False), budget,
         "planned", note, db.now()),
    )
    plan_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO trade_plan_item(plan_id, symbol, target_volume, ref_price,"
        " plan_price, side, reason, held) VALUES (?,?,?,?,?,?,?,?)",
        [(plan_id, it["symbol"], it["target_volume"], it["ref_price"],
          it["plan_price"], it["side"], it["reason"], it["held"]) for it in items],
    )
    conn.commit()
    return plan_id, len(items)


def load_plan(conn: sqlite3.Connection, plan_id: int) -> tuple[dict, list[dict]]:
    """读计划及其条目（按落库顺序：卖 → 买 → 不动）。"""
    row = conn.execute("SELECT * FROM trade_plan WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        raise PlanError(f"计划 #{plan_id} 不存在")
    plan = dict(row)
    plan["params"] = json.loads(plan["params"] or "{}")
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM trade_plan_item WHERE plan_id = ? ORDER BY rowid", (plan_id,)
    )]
    return plan, items


def latest_signal_date(conn: sqlite3.Connection) -> str | None:
    """`rps_daily` 的最大日期（默认的计划日）。"""
    row = conn.execute("SELECT MAX(date) AS d FROM rps_daily").fetchone()
    return row["d"] if row and row["d"] else None


def plan_rows(conn: sqlite3.Connection, plan_id: int) -> list[dict]:
    """条目 + 最新快照现持 + 差额（CLI 打印与页面对账共用同一份计算）。"""
    plan, items = load_plan(conn, plan_id)
    snapshot = _snapshot_volume(conn)
    for it in items:
        have = int(snapshot.get(it["symbol"], 0))
        it["held"] = have
        it["delta"] = int(it["target_volume"] or 0) - have
    return [plan, items]


def _snapshot_volume(conn: sqlite3.Connection) -> dict[str, int]:
    """最近一次持仓快照的股数（没有快照时按空仓）。"""
    rows = conn.execute(
        "SELECT symbol, volume FROM position_snapshot WHERE snap_date ="
        " (SELECT MAX(snap_date) FROM position_snapshot)"
    ).fetchall()
    return {r["symbol"]: int(r["volume"] or 0) for r in rows}


def shadow_fill(conn: sqlite3.Connection, plan_id: int) -> dict:
    """影子成交：只对 `reason='ok'` 的**买单**按限价全额成交。

    影子从**最近一次持仓快照**出发，按绝对目标补足差额（空仓起步时就是
    全部目标股数）。限价买单挂出去大概率能在附近成交，因此成交价直接取
    `plan_price` —— 它衡量的正是「按计划价成交时，往返摩擦相对回测模型
    的 1.14% 是高估还是低估」。
    """
    plan, items = load_plan(conn, plan_id)
    held = _snapshot_volume(conn)
    fills = []
    for i, it in enumerate(items):
        if it["reason"] != "ok" or not it["plan_price"]:
            continue
        volume = int(it["target_volume"] or 0) - int(held.get(it["symbol"], 0))
        if volume <= 0:
            continue
        fills.append({
            "trade_date": plan["plan_date"],
            "broker_order_id": -plan_id,
            "broker_trade_id": -(i + 1),
            "symbol": it["symbol"],
            "side": "buy",
            "volume": volume,
            "price": float(it["plan_price"]),
            "traded_at": db.now(),
        })
    if not fills:
        raise PlanError(f"计划 #{plan_id} 没有可影子成交的买单")
    conn.executemany(
        "INSERT OR REPLACE INTO trade_fill(trade_date, broker_order_id, broker_trade_id,"
        " symbol, side, volume, price, traded_at) VALUES (:trade_date,:broker_order_id,"
        ":broker_trade_id,:symbol,:side,:volume,:price,:traded_at)",
        fills,
    )
    conn.commit()
    cost = shadow_round_trip(conn, plan_id)
    note = (f"{plan['note']}\n影子成交：{len(fills)} 笔，"
            f"影子价格摩擦 {cost:.4%}（往返，含买卖两侧 ∓{PRICE_OFFSET:.1%}）")
    conn.execute("UPDATE trade_plan SET note = ? WHERE id = ?", (note, plan_id))
    conn.commit()
    return {"plan_id": plan_id, "fills": len(fills), "round_trip": cost}


def shadow_round_trip(conn: sqlite3.Connection, plan_id: int) -> float:
    """影子成交口径的往返摩擦（**价格部分**，不含佣金/印花税）。

    从**实际落库的成交价**反推：影子买价 = 最新价 ×(1+0.5%)，卖价按同一
    最新价 ×(1−0.5%) 反推，因此 (买−卖)/买 就是计划价差造成的往返摩擦。
    """
    row = conn.execute(
        "SELECT AVG(price) AS avg_price, COUNT(*) AS n FROM trade_fill"
        " WHERE broker_order_id = ? AND side = 'buy'",
        (-plan_id,),
    ).fetchone()
    if not row or not row["n"] or not row["avg_price"]:
        return 0.0
    buy = float(row["avg_price"])
    ref = buy / (1 + PRICE_OFFSET)
    return (buy - ref * (1 - PRICE_OFFSET)) / buy


def calibrate(conn: sqlite3.Connection, days: int = 20, n_positions: int = 20) -> list[dict]:
    """影子成交的往返摩擦 vs 成本模型的理论值。**只打印，不写库。**

    三栏对照是有意为之：影子只能观测**价格**摩擦（委托价里预埋的 ±0.5%），
    模型的 1.14% 里还含佣金/印花税/过户费。把三者并排才看得出
    「回测的滑点假设（0.05%/边）比计划实际预埋的（0.5%/边）乐观 10 倍」。
    """
    from ..analytics.costs import default_cost_model

    model = default_cost_model(enabled=True)
    rows = conn.execute(
        "SELECT trade_date, broker_order_id, COUNT(*) AS n,"
        " SUM(volume * price) AS amount FROM trade_fill"
        " WHERE broker_order_id < 0 AND trade_date >= date('now', ?)"
        " GROUP BY trade_date, broker_order_id ORDER BY trade_date DESC LIMIT ?",
        (f"-{int(days)} days", int(days)),
    ).fetchall()
    out = []
    for r in rows:
        plan_id = -int(r["broker_order_id"])
        plan = conn.execute("SELECT plan_date FROM trade_plan WHERE id = ?",
                            (plan_id,)).fetchone()
        on = datetime.strptime(plan["plan_date"], "%Y-%m-%d").date() if plan else None
        out.append({
            "date": r["trade_date"],
            "plan_id": plan_id,
            "names": int(r["n"]),
            "amount": round(float(r["amount"] or 0.0), 2),
            "shadow_price": shadow_round_trip(conn, plan_id),
            "model_price": model.slippage_rate * 2,
            "model_round_trip": model.round_trip_cost(on, n_positions=n_positions),
        })
    return out
