"""假 QMT 桥：让 `broker/qmt.py` 在没有 QMT 客户端的机器上也能测。

`broker/bridge.py` 依赖 cp36~cp311 的编译扩展，**3.12 下 import 就炸**，
所以真正可测的是这一层「子进程 + 一行 JSON」协议。测试把本文件复制成
`rpsboard/broker/bridge.py` 放进临时包，再用 `RPSBOARD_QMT_PYTHON=sys.executable`
跑它 —— 与真实调用形状完全一致（子进程、独立解释器、stdout 只有一行）。

环境变量：
  FAKE_QMT_FAIL=1        返回 ok=false（错误透传）
  FAKE_QMT_DISCONNECT=1  往 stderr 写断线事件
  FAKE_QMT_HANG=1        永不返回（超时）
  FAKE_QMT_NO_TICK=000002 取不到价的代码（逗号分隔）
  FAKE_QMT_EMPTY_ASSET=1 资金查询返回 None（可用现金为 0）
  FAKE_QMT_ECHO=out.json    把收到的 payload 原样写进该文件（断言传参用）
"""

from __future__ import annotations

import json
import os
import sys
import time

ACCOUNT = "0000000123"
_next_order_id = [900001]

POSITIONS = [{
    "symbol": "000001", "volume": 1000, "can_use_volume": 1000, "frozen_volume": 0,
    "avg_price": 12.5, "market_value": 12500.0,
}]
ORDERS = [{
    "order_id": 900001, "symbol": "000001", "order_type": 23, "order_status": 56,
    "status": "filled", "order_volume": 1000, "traded_volume": 1000,
    "price": 12.6, "order_time": 93000, "status_msg": "",
}]
TRADES = [{
    "order_id": 900001, "traded_id": 1, "symbol": "000001", "order_type": 23,
    "traded_volume": 1000, "traded_price": 12.6, "traded_amount": 12600.0,
    "traded_time": 93001,
}]


def _asset() -> dict:
    if os.environ.get("FAKE_QMT_EMPTY_ASSET"):
        return {}
    return {"cash": 1_000_000.0, "frozen_cash": 0.0,
            "market_value": 12500.0, "total_asset": 1_012_500.0}


def _tick(codes: list[str]) -> dict:
    skip = {c.strip() for c in os.environ.get("FAKE_QMT_NO_TICK", "").split(",") if c.strip()}
    out = {}
    for c in codes:
        if c in skip:
            continue
        out[c] = round(10.0 + (int(c) % 90) / 10.0, 2)
    return out


def dispatch(op: str, payload: dict) -> dict:
    if op == "status":
        return {"connected": True, "account_masked": "****" + ACCOUNT[-4:],
                "asset": _asset()}
    if op == "asset":
        return _asset()
    if op == "positions":
        return POSITIONS
    if op == "orders":
        return ORDERS
    if op == "trades":
        return TRADES
    if op == "tick":
        return _tick([str(c) for c in payload.get("codes") or []])
    if op == "place":
        if float(payload.get("price") or 0) <= 0:
            raise ValueError("限价必须为正")
        if int(payload.get("volume") or 0) <= 0:
            raise ValueError("数量必须为正")
        _next_order_id[0] += 1
        return {"order_id": _next_order_id[0]}
    if op == "cancel":
        return {"cancel_result": 0}
    raise ValueError(f"未知 op: {op}")


def main() -> int:
    req = json.loads(sys.stdin.readline() or "{}")
    payload = req.get("payload") or {}
    echo = os.environ.get("FAKE_QMT_ECHO")
    if echo:
        with open(echo, "w", encoding="utf-8") as fp:
            json.dump({"op": req.get("op"), "payload": payload}, fp)
    if os.environ.get("FAKE_QMT_HANG"):
        time.sleep(300)
    if os.environ.get("FAKE_QMT_DISCONNECT"):
        print(json.dumps({"event": "disconnected"}), file=sys.stderr, flush=True)
    if os.environ.get("FAKE_QMT_FAIL"):
        print(json.dumps({"ok": False, "error": "RuntimeError: connect() 返回非 0"}))
        return 1
    if not payload.get("account"):
        print(json.dumps({"ok": False,
                          "error": "ValueError: 未设置 RPSBOARD_QMT_ACCOUNT"}))
        return 1
    try:
        resp = {"ok": True, "data": dispatch(req.get("op"), payload)}
    except Exception as exc:  # noqa: BLE001
        resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(resp, ensure_ascii=False))
    return 0 if resp["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
