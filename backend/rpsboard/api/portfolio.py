"""模拟持仓接口：多账户、原价撮合、公司行为核对、风险与导出。

全部按 `account_id` 隔离。写操作先取**每账户文件锁**（跨进程）再 `BEGIN IMMEDIATE`，
把「读余额 → 校验 → 写入」整段串起来；业务性受阻返回 200 + `ok=false`
（它是**状态**不是错误），格式错 422、状态冲突 409、程序异常照常 500。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import config, db
from ..broker import accounts as accounts_mod
from ..broker import corporate_actions as corp_mod
from ..broker import risk_alerts, sim
from . import csv_response

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])

#: 写操作的 SQLite busy 等待（秒）。比默认 30 秒略短，让「库被别的进程占着」
#: 变成一个明确的 409 而不是把请求挂到前端超时。
_WRITE_TIMEOUT = 25.0


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AccountIn(_Strict):
    name: str = Field(min_length=1, max_length=60)
    kind: str = Field(pattern="^(manual|strategy)$")
    cash: float = Field(gt=0)
    start_date: str
    params: dict | None = None
    participation: float | None = Field(default=None, ge=0, le=1)


class OrderIn(_Strict):
    request_id: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=6, max_length=6)
    side: str = Field(pattern="^(buy|sell)$")
    volume: int = Field(gt=0)
    price: float = Field(gt=0)
    thesis: str = Field(default="", max_length=2000)


class AdvanceIn(_Strict):
    to_date: str | None = None


class ReviewNoteIn(_Strict):
    review_note: str = Field(default="", max_length=10000)


class SuspensionIn(_Strict):
    symbol: str = Field(min_length=6, max_length=6)
    date: str
    source_note: str = Field(min_length=1, max_length=1000)


class ActionIn(_Strict):
    symbol: str = Field(min_length=6, max_length=6)
    event_date: str
    category: int
    resolution: dict


def _http_status(exc: Exception) -> int:
    if isinstance(exc, accounts_mod.AccountBusy):
        return 409
    if isinstance(exc, (accounts_mod.AccountError, corp_mod.CorporateActionError,
                        sim.SimError)):
        return 409
    return 500


def _raise(exc: Exception) -> None:
    status = _http_status(exc)
    if status == 500:
        raise exc
    raise HTTPException(status_code=status, detail=str(exc)) from exc


def _write_session():
    """写操作用的连接：busy 等待短一点，冲突转成 409 而不是长时间挂起。"""
    conn = db.connect()
    conn.execute(f"PRAGMA busy_timeout = {int(_WRITE_TIMEOUT * 1000)}")
    return conn


# ── 账户 ────────────────────────────────────────────────────────────
@router.get("/accounts")
def list_accounts() -> dict:
    conn = db.connect()
    try:
        return {"rows": accounts_mod.list_accounts(conn)}
    finally:
        conn.close()


@router.post("/accounts")
def create_account(body: AccountIn) -> dict:
    conn = _write_session()
    try:
        acc = accounts_mod.create_account(
            conn, name=body.name, kind=body.kind, cash=body.cash,
            start_date=body.start_date, params=body.params,
            participation=body.participation)
        return {"ok": True, "account": acc}
    except Exception as exc:  # noqa: BLE001 - 统一转 HTTP
        _raise(exc)
    finally:
        conn.close()


@router.post("/accounts/{account_id}/archive")
def archive_account(account_id: int) -> dict:
    conn = _write_session()
    try:
        with accounts_mod.account_write_lock(conn, account_id):
            acc = accounts_mod.archive_account(conn, account_id)
        return {"ok": True, "account": acc}
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    finally:
        conn.close()


@router.get("/accounts/{account_id}")
def account_detail(account_id: int, date: str | None = None) -> dict:
    conn = db.connect()
    try:
        acc = accounts_mod.get_account(conn, account_id)
        summary = sim.summary(conn, account_id=account_id, date=date)
        out = dict(summary)
        # 冻结/可用现金要算出来给页面（工具带入资金时用的是**可用**现金，
        # 不是含冻结的现金余额 —— 否则会算出一个花不出去的数量）。
        frozen = sim._frozen_cash(conn, account_id)
        out["frozen_cash"] = round(frozen, 2)
        out["available_cash"] = round(float(summary.get("cash") or 0.0) - frozen, 2)
        out["review"] = sim.review(conn, account_id=account_id, date=date)
        out["account"] = acc
        out["date"] = summary.get("to") if date else None
        out["requested_date"] = date
        out["latest_date"] = acc.get("latest_date")
        out["corporate"] = corp_mod.health(conn, account_id)
        if date and not summary.get("days"):
            # 查得比账户起点还早：**明确说**「那一天账户还不存在」，
            # 而不是悄悄前跳到首日（那会让人以为那天有数据）。
            out["message"] = "该日期账户尚未建立"
        return out
    except accounts_mod.AccountError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()


@router.get("/accounts/{account_id}/dates")
def account_dates(account_id: int) -> list[str]:
    conn = db.connect()
    try:
        return [r["trade_date"] for r in conn.execute(
            "SELECT trade_date FROM paper_nav WHERE account_id=?"
            " ORDER BY trade_date DESC", (int(account_id),))]
    finally:
        conn.close()


# ── 委托 / 成交 ─────────────────────────────────────────────────────
@router.get("/accounts/{account_id}/orders")
def orders(account_id: int, date: str | None = None,
           limit: int = Query(default=100, ge=1, le=1000),
           offset: int = Query(default=0, ge=0)) -> dict:
    conn = db.connect()
    try:
        return sim.orders_query(conn, account_id=account_id, date=date,
                                limit=limit, offset=offset)
    finally:
        conn.close()


@router.get("/accounts/{account_id}/fills")
def fills(account_id: int, date: str | None = None,
          limit: int = Query(default=100, ge=1, le=1000),
          offset: int = Query(default=0, ge=0)) -> dict:
    conn = db.connect()
    try:
        return sim.fills_query(conn, account_id=account_id, date=date,
                               limit=limit, offset=offset)
    finally:
        conn.close()


@router.post("/accounts/{account_id}/orders")
def submit_order(account_id: int, body: OrderIn) -> dict:
    conn = _write_session()
    try:
        with accounts_mod.account_write_lock(conn, account_id):
            order = sim.submit_manual_order(
                conn, account_id=account_id, request_id=body.request_id,
                symbol=body.symbol, side=body.side, volume=body.volume,
                price=body.price, thesis=body.thesis)
        return {"ok": True, "order": order}
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    finally:
        conn.close()


@router.post("/accounts/{account_id}/orders/{order_id}/cancel")
def cancel_order(account_id: int, order_id: int) -> dict:
    conn = _write_session()
    try:
        with accounts_mod.account_write_lock(conn, account_id):
            order = sim.cancel_order(conn, account_id=account_id, order_id=order_id)
        return {"ok": True, "order": order}
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    finally:
        conn.close()


@router.post("/accounts/{account_id}/orders/{order_id}/review")
def order_review(account_id: int, order_id: int, body: ReviewNoteIn) -> dict:
    """只改这笔委托的**事后复盘文本**（不能改成交数量/价格）。"""
    conn = _write_session()
    try:
        with accounts_mod.account_write_lock(conn, account_id):
            row = conn.execute(
                "SELECT id FROM paper_order WHERE id=? AND account_id=?",
                (int(order_id), int(account_id))).fetchone()
            if row is None:
                raise accounts_mod.AccountError(f"委托 {order_id} 不存在")
            conn.execute(
                "UPDATE paper_order SET review_note=?, updated_at=? WHERE id=?",
                (body.review_note, db.now(), int(order_id)))
            conn.commit()
            out = conn.execute("SELECT * FROM paper_order WHERE id=?",
                               (int(order_id),)).fetchone()
        return {"ok": True, "order": sim._order_view(conn, dict(out))}
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    finally:
        conn.close()


# ── 推进 ────────────────────────────────────────────────────────────
@router.post("/accounts/{account_id}/advance")
def advance(account_id: int, body: AdvanceIn = Body(default=AdvanceIn())) -> dict:
    conn = _write_session()
    try:
        with accounts_mod.account_write_lock(conn, account_id):
            res = sim.advance(conn, account_id=account_id, to_date=body.to_date)
        return res
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    finally:
        conn.close()


# ── 公司行为核对 ────────────────────────────────────────────────────
@router.get("/accounts/{account_id}/actions")
def actions(account_id: int) -> dict:
    conn = db.connect()
    try:
        acc = accounts_mod.get_account(conn, account_id)
        # 列表要能看到「已发现但还没核对」的事件（包括尚未到生效日的），
        # 所以窗口取到最新市场日，而不是账户已经结算到的那一天。
        nav = db.market_date(conn) or acc.get("latest_date") or acc.get("start_date")
        # 只列该账户**可能涉及**的股票：当前持仓 + 历史持仓 + 已有方案。
        held = {r["symbol"] for r in conn.execute(
            "SELECT DISTINCT symbol FROM paper_position WHERE account_id=?",
            (int(account_id),))}
        held |= {a["symbol"] for a in corp_mod.list_actions(conn, account_id)}
        events = []
        if held:
            marks = ",".join("?" * len(held))
            for r in conn.execute(
                    f"SELECT * FROM corporate_event WHERE symbol IN ({marks})"
                    " ORDER BY event_date", tuple(sorted(held))):
                d = dict(r)
                act = conn.execute(
                    "SELECT 1 FROM paper_action WHERE account_id=? AND symbol=?"
                    " AND event_date=? AND category=?",
                    (int(account_id), d["symbol"], d["event_date"],
                     d["category"])).fetchone()
                d["resolved"] = act is not None
                events.append(d)
        unresolved = corp_mod.unresolved_events(
            conn, account_id=account_id, symbols=held,
            through=nav or "9999-12-31")
        return {"rows": events, "actions": corp_mod.list_actions(conn, account_id),
                "unresolved": unresolved}
    except accounts_mod.AccountError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/accounts/{account_id}/actions")
def register_action(account_id: int, body: ActionIn) -> dict:
    conn = _write_session()
    try:
        with accounts_mod.account_write_lock(conn, account_id):
            last = sim.last_settled_date(conn, account_id)
            res = corp_mod.resolve_action(
                conn, account_id=account_id, symbol=body.symbol,
                event_date=body.event_date, category=body.category,
                resolution=body.resolution, last_settled=last)
        return res
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    finally:
        conn.close()


@router.post("/suspensions")
def confirm_suspension(body: SuspensionIn) -> dict:
    """登记一条「已按公告核对」的停牌（缺 bar 的日子）。

    没有这条确认的缺 bar 一律 `raw_bar_missing`：不能从复牌间隔去猜停牌。
    """
    conn = _write_session()
    try:
        day = db.market_date(conn)
        if not day:
            raise accounts_mod.AccountError("还没有市场日历，无法登记停牌")
        row = conn.execute(
            "SELECT 1 FROM benchmark_bar WHERE symbol='000001' AND date=?",
            (body.date,)).fetchone()
        if row is None:
            raise accounts_mod.AccountError(f"{body.date} 不是市场交易日")
        normal = conn.execute(
            "SELECT 1 FROM raw_bar WHERE symbol=? AND date=? AND tradestatus=1",
            (body.symbol, body.date)).fetchone()
        if normal is not None:
            raise accounts_mod.AccountError(
                f"{body.symbol} 在 {body.date} 有正常成交行情，不能登记为停牌")
        conn.execute(
            "INSERT OR REPLACE INTO suspension_confirmation(symbol, date,"
            " source_note, created_at) VALUES (?,?,?,?)",
            (body.symbol, body.date, body.source_note, db.now()))
        conn.commit()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
    finally:
        conn.close()


# ── 风险 / 导出 / 归档 ──────────────────────────────────────────────
@router.get("/accounts/{account_id}/risk")
def risk(account_id: int, date: str | None = None,
         max_single: float | None = Query(default=None, gt=0, le=1),
         max_industry: float | None = Query(default=None, gt=0, le=1),
         max_avg_corr: float | None = Query(default=None, gt=0, le=1),
         max_drawdown: float | None = Query(default=None, gt=0, le=1)) -> dict:
    conn = db.connect()
    try:
        over = {k: v for k, v in (
            ("max_single", max_single), ("max_industry", max_industry),
            ("max_avg_corr", max_avg_corr), ("max_drawdown", max_drawdown),
        ) if v is not None}
        return {
            "account_id": int(account_id), "date": date,
            "thresholds": risk_alerts.thresholds(**over),
            "alerts": risk_alerts.evaluate(conn, account_id=account_id,
                                          date=date, **over),
            "current_drawdown": risk_alerts.current_drawdown(
                conn, account_id=account_id, date=date),
        }
    finally:
        conn.close()


_EXPORTS = {
    "nav": (["日期", "现金", "持仓市值", "应收", "总权益", "净值", "持仓数"],
            lambda r: [r["trade_date"], r["cash"], r["market_value"],
                       r.get("receivable", 0), r["total"], r["nav"],
                       r["n_positions"]]),
    "positions": (["日期", "代码", "股数", "可卖", "成本价", "成本余额", "收盘价",
                   "价格日期", "市值"],
                  lambda r: [r["trade_date"], r["symbol"], r["volume"],
                             r["available_volume"], r["avg_price"],
                             r["cost_basis"], r["close"], r["price_date"],
                             r["market_value"]]),
    "orders": (["决策日", "代码", "方向", "委托量", "委托价", "状态", "来源",
                "终止日", "说明"],
               lambda r: [r["trade_date"], r["symbol"], r["side"], r["volume"],
                          r["price"], r["status"], r["source"],
                          r.get("closed_trade_date"), r.get("err_msg")]),
    "fills": (["日期", "代码", "方向", "数量", "价格", "费用", "已实现盈亏"],
              lambda r: [r["trade_date"], r["symbol"], r["side"], r["volume"],
                         r["price"], r["fee"], r["realized_pnl"]]),
}


@router.get("/accounts/{account_id}/export.csv")
def export_csv(account_id: int,
               kind: str = Query(default="nav",
                                 pattern="^(nav|positions|orders|fills)$"),
               date: str | None = None,
               limit: int = Query(default=5000, ge=1, le=100_000),
               ) -> StreamingResponse:
    """按账户与截止日导出（复盘留档）。SQL 故障**不**返回空文件。"""
    conn = db.connect()
    try:
        if kind == "nav":
            rows = sim.nav_curve(conn, account_id=account_id, date=date,
                                 limit=limit)
        elif kind == "positions":
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM paper_position WHERE account_id=?"
                + (" AND trade_date <= ?" if date else "")
                + " ORDER BY trade_date DESC, symbol LIMIT ?",
                ((account_id, date, limit) if date else (account_id, limit)))]
        elif kind == "orders":
            rows = sim.orders_query(conn, account_id=account_id, date=date,
                                    limit=limit)["rows"]
        else:
            rows = sim.fills_query(conn, account_id=account_id, date=date,
                                   limit=limit)["rows"]
    finally:
        conn.close()
    headers, pick = _EXPORTS[kind]
    dates = [r["trade_date"] for r in rows if r.get("trade_date")]
    stamp = max(dates) if dates else "empty"
    return csv_response(headers, [pick(r) for r in rows],
                        f"paper_{kind}_{account_id}_{stamp}.csv")


@router.get("/archives")
def archives(limit: int = Query(default=10, ge=1, le=50)) -> dict:
    """历史曲线归档（只读，多为迁移前的 HFQ 研究口径）。"""
    conn = db.connect()
    try:
        return {"rows": sim.list_archives(conn, limit=int(limit))}
    finally:
        conn.close()


@router.get("/config")
def portfolio_config() -> dict:
    """给页面的默认值（参与率、账户类型枚举），避免前端各写一份。"""
    return {"participation_default": float(config.PAPER_MAX_PARTICIPATION),
            "kinds": list(accounts_mod.KINDS),
            "statuses": list(accounts_mod.STATUSES)}
