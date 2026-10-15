"""QMT 桥：项目 3.12 进程 ⇄ QMT 客户端的 xtquant（**必须在 3.11 下运行**）。

为什么单独一个进程：xtquant 的 `xttrader` / `xtdata` 依赖编译扩展
（`xtpythonclient.pyd` / `IPythonApiClient.pyd`），客户端只提供 cp36~cp311，
而项目 venv 是 3.12 —— 直接 import 会 ImportError。所以项目侧
（broker/qmt.py）用子进程 + 一行 JSON 调用这里，桥只用标准库 + 客户端
自带的 site-packages，**不装任何包**。

协议（stdin 一行 / stdout 一行）::

    {"op": "status", "payload": {...}}  →  {"ok": true, "data": {...}}
                                          →  {"ok": false, "error": "Type: msg"}

stdout 只有这一行：xtquant 自己的 print/log 一律重定向到 stderr，
否则调用方按「最后一行」解析也会被中间日志污染。
断线事件走 stderr 的 `{"event":"disconnected"}`，同样不混进 stdout。

账号在所有返回与诊断里都脱敏（只留后 4 位）。
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

#: 客户端自带的 site-packages（xtquant 在里面）
_SITE_REL = ("bin.x64", "Lib", "site-packages")

#: 委托状态（官方 xtconstant，取不到时的兜底见 _ORDER_STATUS）
_ORDER_STATUS = {
    48: "submitted", 49: "submitted", 50: "submitted",
    51: "canceling", 52: "canceling",
    53: "canceled", 54: "canceled",
    55: "part", 56: "filled", 57: "rejected", 255: "unknown",
}

_SIDES = {"buy": None, "sell": None}   # 绑定见 _bind_xt()


def mask_account(account_id: str) -> str:
    """账号脱敏：只留后 4 位。日志、接口、页面一律走这里。"""
    tail = "".join(ch for ch in str(account_id) if ch.isdigit())[-4:]
    return f"****{tail}" if tail else "****"


def _bare(stock_code: str) -> str:
    """`600000.SH` → `600000`（本项目的库里一律裸 6 位）。"""
    return str(stock_code).split(".")[0]


def _num(v):
    """xtquant 有时给 numpy 标量/字符串；统一成可 JSON 序列化的数字或 None。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


def _bind_xt(payload: dict) -> dict:
    """把客户端 site-packages 挂进 sys.path 并导入 xtquant。"""
    sp = payload.get("site_packages")
    if not sp:
        qmt_dir = Path(payload["qmt_dir"])
        sp = str(qmt_dir.joinpath(*_SITE_REL))
    sp = str(sp)
    if sp not in sys.path:
        sys.path.insert(0, sp)

    from xtquant import xtconstant, xtdata, xttrader  # noqa: PLC0415
    from xtquant.xttype import StockAccount  # noqa: PLC0415

    _SIDES["buy"] = xtconstant.STOCK_BUY
    _SIDES["sell"] = xtconstant.STOCK_SELL
    return {
        "xttrader": xttrader,
        "xtconstant": xtconstant,
        "xtdata": xtdata,
        "StockAccount": StockAccount,
    }


class _Callback:
    """把断线事件写到 stderr（stdout 留给唯一的响应行）。"""

    def on_disconnected(self):
        print(json.dumps({"event": "disconnected"}), file=sys.stderr, flush=True)

    def on_connected(self):
        pass

    def on_account_status(self, account):
        pass

    def on_order_error(self, account, order_id, error_id, error_msg):
        pass

    def on_cancel_error(self, account, order_id, error_id, error_msg):
        pass

    def on_stock_order(self, account, order):
        pass

    def on_stock_trade(self, account, trade):
        pass

    def on_stock_asset(self, account, asset):
        pass

    def on_stock_position(self, account, position):
        pass


def _connect(xt: dict, payload: dict):
    """建连接。connect() 返回非 0 就是失败 —— 官方接口**不会自动重连**，
    重连由调用方（项目侧）决定，故这里直接抛。"""
    path = payload.get("userdata_mini")
    session_id = int(payload.get("session_id") or 0)
    account_id = payload["account"]

    trader = xt["xttrader"].XtQuantTrader(str(path), session_id)
    trader.register_callback(_Callback())
    trader.start()
    code = trader.connect()
    if code != 0:
        raise RuntimeError(f"connect() 返回非 0（{code}）：QMT 客户端未启动或未登录")
    acc = xt["StockAccount"](account_id)
    trader.subscribe(acc)
    return trader, acc


def _asset(trader, acc) -> dict:
    a = trader.query_stock_asset(acc)
    if a is None:
        return {"cash": None, "frozen_cash": None,
                "market_value": None, "total_asset": None}
    return {"cash": _num(a.cash), "frozen_cash": _num(a.frozen_cash),
            "market_value": _num(a.market_value), "total_asset": _num(a.total_asset)}


def op_status(xt, trader, acc, payload) -> dict:
    return {
        "connected": True,
        "account_masked": mask_account(acc.account_id),
        "asset": _asset(trader, acc),
    }


def op_asset(xt, trader, acc, payload) -> dict:
    return _asset(trader, acc)


def op_positions(xt, trader, acc, payload) -> list[dict]:
    out = []
    for p in trader.query_stock_positions(acc) or ():
        out.append({
            "symbol": _bare(p.stock_code),
            "volume": _num(p.volume),
            "can_use_volume": _num(p.can_use_volume),
            "frozen_volume": _num(p.frozen_volume),
            "avg_price": _num(p.open_price),
            "market_value": _num(p.market_value),
        })
    return out


def op_orders(xt, trader, acc, payload) -> list[dict]:
    out = []
    for o in trader.query_stock_orders(acc) or ():
        out.append({
            "order_id": _num(o.order_id),
            "symbol": _bare(o.stock_code),
            "order_type": _num(o.order_type),
            "order_status": _num(o.order_status),
            "status": _ORDER_STATUS.get(o.order_status, "unknown"),
            "order_volume": _num(o.order_volume),
            "traded_volume": _num(o.traded_volume),
            "price": _num(o.price),
            "order_time": _num(o.order_time),
            "status_msg": o.status_msg or "",
        })
    return out


def op_trades(xt, trader, acc, payload) -> list[dict]:
    out = []
    for t in trader.query_stock_trades(acc) or ():
        out.append({
            "order_id": _num(t.order_id),
            "traded_id": _num(t.traded_id),
            "symbol": _bare(t.stock_code),
            "order_type": _num(t.order_type),
            "traded_volume": _num(t.traded_volume),
            "traded_price": _num(t.traded_price),
            "traded_amount": _num(t.traded_amount),
            "traded_time": _num(t.traded_time),
        })
    return out


def op_tick(xt, trader, acc, payload) -> dict:
    """最新价。取不到有效价的代码**不出现**在结果里（调用方按缺失处理，不猜价）。"""
    codes = [str(c) for c in (payload.get("codes") or [])]
    if not codes:
        return {}
    raw = xt["xtdata"].get_full_tick(codes) or {}
    out: dict = {}
    for code, tick in raw.items():
        last = (tick or {}).get("lastPrice")
        price = _num(last)
        if price is not None and price > 0:
            out[_bare(code)] = price
    return out


def op_place(xt, trader, acc, payload) -> dict:
    """报单。**只接受限价单**（FIX_PRICE）—— 模拟环境不支持市价，
    而市价只在实盘生效；放开会让「以为在模拟、实际发出实盘单」成为可能。"""
    side = str(payload["side"])
    if side not in _SIDES or _SIDES[side] is None:
        raise ValueError(f"side 只能是 buy/sell，收到 {side!r}")
    price = _num(payload.get("price"))
    volume = int(payload.get("volume") or 0)
    if not price or price <= 0:
        raise ValueError(f"限价必须为正，收到 {payload.get('price')!r}")
    if volume <= 0:
        raise ValueError(f"数量必须为正，收到 {payload.get('volume')!r}")
    order_id = trader.order_stock(
        acc,
        str(payload["code"]),
        _SIDES[side],
        volume,
        xt["xtconstant"].FIX_PRICE,
        round(float(price), 2),
        str(payload.get("strategy") or "rpsboard"),
        str(payload.get("remark") or "")[:64],
    )
    return {"order_id": _num(order_id)}


def op_cancel(xt, trader, acc, payload) -> dict:
    return {"cancel_result": _num(trader.cancel_order_stock(acc, int(payload["order_id"])))}


_OPS = {
    "status": op_status,
    "asset": op_asset,
    "positions": op_positions,
    "orders": op_orders,
    "trades": op_trades,
    "tick": op_tick,
    "place": op_place,
    "cancel": op_cancel,
}


def dispatch(op: str, payload: dict) -> dict:
    handler = _OPS.get(op)
    if handler is None:
        raise ValueError(f"未知 op: {op}（可用 {sorted(_OPS)}）")
    xt = _bind_xt(payload)
    trader, acc = _connect(xt, payload)
    try:
        return handler(xt, trader, acc, payload)
    finally:
        # 官方接口没有 disconnect()；进程马上退出，socket 随进程释放。
        # 留着是为了让 xttrader 的后台线程在退出前把回调队列冲干净。
        del trader


def main() -> int:
    raw = sys.stdin.readline()
    try:
        req = json.loads(raw or "{}")
        op = req.get("op")
        payload = req.get("payload") or {}
        if not payload.get("account"):
            raise ValueError("未设置 RPSBOARD_QMT_ACCOUNT")
        # xtquant 会往 stdout 打日志/进度；全部赶到 stderr，stdout 只留响应行
        with contextlib.redirect_stdout(sys.stderr):
            data = dispatch(op, payload)
        resp = {"ok": True, "data": data}
    except Exception as exc:  # noqa: BLE001 —— 任何异常都要以 JSON 报告，不能裸栈
        resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(exc, (RuntimeError, ValueError, KeyError)):
            import traceback
            traceback.print_exc(file=sys.stderr)
    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0 if resp["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
