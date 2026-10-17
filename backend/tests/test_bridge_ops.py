"""QMT 桥的操作层（`broker/bridge.py`）——**不启动子进程**的单元级覆盖。

桥本身是 3.11 子进程里跑的（xtquant 只提供到 cp311），所以进程内覆盖率永远是 0。
这里直接对 `dispatch` 的各个 op 打桩 `_bind_xt`/`_connect`，把每个 op 的
**数据转换与安全边界**钉住：账号脱敏、只接受限价单、取不到价就不猜价、
状态码映射、以及任何异常都必须变成 JSON（不能裸栈）。
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from rpsboard.broker import bridge


class _FakeTrader:
    def __init__(self, *, asset=None, positions=(), orders=(), trades=(),
                 order_id=42, cancel=0):
        self._asset = asset
        self._positions = list(positions)
        self._orders = list(orders)
        self._trades = list(trades)
        self._order_id = order_id
        self._cancel = cancel
        self.placed: list[dict] = []

    # 连接相关
    def register_callback(self, cb):
        self.callback = cb

    def start(self):
        pass

    def connect(self):
        return 0

    def subscribe(self, acc):
        pass

    def query_stock_asset(self, acc):
        return self._asset

    def query_stock_positions(self, acc):
        return self._positions

    def query_stock_orders(self, acc):
        return self._orders

    def query_stock_trades(self, acc):
        return self._trades

    def order_stock(self, acc, code, side, volume, price_type, price, strategy, remark):
        self.placed.append({"code": code, "side": side, "volume": volume,
                            "price": price, "strategy": strategy, "remark": remark})
        return self._order_id

    def cancel_order_stock(self, acc, order_id):
        self.last_cancel = order_id
        return self._cancel


def _patch(monkeypatch, trader, *, tick=None, connect_code=0):
    """装好一个「绑定完成」的桥环境。

    `_SIDES` 是模块级状态，由真正的 `_bind_xt()` 在导入 xtquant 时填好；
    这里既然把 `_bind_xt` 换成了桩，就要一并把它设成绑定后的样子
    （否则 `place` 会认为方向未绑定而拒绝 —— 那是子进程里的真实前置条件）。
    """
    monkeypatch.setattr(bridge, "_SIDES", {"buy": 23, "sell": 24})
    acc = SimpleNamespace(account_id="1234567890")
    if connect_code != 0:
        def bad_connect(xt, payload):
            raise RuntimeError(f"connect() 返回非 0（{connect_code}）：QMT 客户端未启动或未登录")
        monkeypatch.setattr(bridge, "_connect", bad_connect)
    else:
        monkeypatch.setattr(bridge, "_connect", lambda xt, payload: (trader, acc))
    xt = {
        "xttrader": SimpleNamespace(XtQuantTrader=lambda path, sid: trader),
        "StockAccount": lambda aid: acc,
        "xtconstant": SimpleNamespace(FIX_PRICE=11),
        # 真实接口只返回**请求过的**代码
        "xtdata": SimpleNamespace(get_full_tick=lambda codes:
                                  {c: tick[c] for c in codes if c in (tick or {})}),
    }
    monkeypatch.setattr(bridge, "_bind_xt", lambda payload: xt)
    return acc


def test_mask_account_only_keeps_last4():
    assert bridge.mask_account("1234567890") == "****7890"
    assert bridge.mask_account("acc-12-34") == "****1234"
    assert bridge.mask_account("") == "****"


def test_bare_strips_exchange_suffix():
    assert bridge._bare("600519.SH") == "600519"
    assert bridge._bare("000001") == "000001"


def test_dispatch_rejects_unknown_op():
    with pytest.raises(ValueError, match="未知 op"):
        bridge.dispatch("nope", {"account": "1"})


def test_status_and_asset(monkeypatch):
    asset = SimpleNamespace(cash=1000.0, frozen_cash=10.0, market_value=2000.0,
                            total_asset=3000.0)
    trader = _FakeTrader(asset=asset)
    _patch(monkeypatch, trader)
    st = bridge.dispatch("status", {"account": "1", "userdata_mini": "x"})
    assert st["connected"] is True
    assert st["account_masked"] == "****7890", "账号必须脱敏"
    assert st["asset"]["cash"] == 1000.0
    assert bridge.dispatch("asset", {"account": "1"})["total_asset"] == 3000.0


def test_asset_when_client_returns_none(monkeypatch):
    """客户端没登录时 query 返回 None：必须给空结构，不能崩。"""
    _patch(monkeypatch, _FakeTrader(asset=None))
    out = bridge.dispatch("asset", {"account": "1"})
    assert out == {"cash": None, "frozen_cash": None, "market_value": None,
                   "total_asset": None}


def test_positions_orders_trades_mapping(monkeypatch):
    pos = SimpleNamespace(stock_code="600519.SH", volume=100, can_use_volume=100,
                          frozen_volume=0, open_price=10.0, market_value=1000.0)
    order = SimpleNamespace(order_id=7, stock_code="000001.SZ", order_type=23,
                            order_status=56, order_volume=100, traded_volume=100,
                            price=10.0, order_time=1, status_msg="已成")
    trade = SimpleNamespace(order_id=7, traded_id=9, stock_code="000001.SZ",
                            order_type=23, traded_volume=100, traded_price=10.0,
                            traded_amount=1000.0, traded_time=2)
    _patch(monkeypatch, _FakeTrader(positions=[pos], orders=[order], trades=[trade]))

    p = bridge.dispatch("positions", {"account": "1"})
    assert p == [{"symbol": "600519", "volume": 100, "can_use_volume": 100,
                  "frozen_volume": 0, "avg_price": 10.0, "market_value": 1000.0}]

    o = bridge.dispatch("orders", {"account": "1"})[0]
    assert o["symbol"] == "000001" and o["status"] == "filled", "状态码 56 = 已成交"
    assert bridge.dispatch("trades", {"account": "1"})[0]["traded_id"] == 9


def test_orders_unknown_status_maps_to_unknown(monkeypatch):
    order = SimpleNamespace(order_id=1, stock_code="600519.SH", order_type=1,
                            order_status=9999, order_volume=1, traded_volume=0,
                            price=1.0, order_time=0, status_msg="")
    _patch(monkeypatch, _FakeTrader(orders=[order]))
    assert bridge.dispatch("orders", {"account": "1"})[0]["status"] == "unknown"


def test_tick_drops_invalid_prices(monkeypatch):
    """取不到有效价的代码**不出现**在结果里（不猜价）。"""
    tick = {"600519.SH": {"lastPrice": 20.5}, "000001.SZ": {"lastPrice": 0},
            "000002.SZ": {}, "000003.SZ": {"lastPrice": None}}
    _patch(monkeypatch, _FakeTrader(), tick=tick)
    assert bridge.dispatch("tick", {"account": "1", "codes": ["600519.SH"]}) == \
        {"600519": 20.5}
    assert bridge.dispatch("tick", {"account": "1",
                                    "codes": ["000001.SZ", "000002.SZ"]}) == {}
    assert bridge.dispatch("tick", {"account": "1", "codes": []}) == {}


def test_place_only_accepts_limit_orders(monkeypatch):
    trader = _FakeTrader(order_id=88)
    _patch(monkeypatch, trader)
    out = bridge.dispatch("place", {"account": "1", "code": "600519.SH",
                                    "side": "buy", "volume": 100, "price": 10.005,
                                    "strategy": "s", "remark": "r"})
    assert out == {"order_id": 88}
    sent = trader.placed[0]
    assert sent["price"] == 10.01, "价格四舍五入到分"
    assert sent["volume"] == 100 and sent["strategy"] == "s"


def test_place_validates_side_price_volume(monkeypatch):
    _patch(monkeypatch, _FakeTrader())
    with pytest.raises(ValueError, match="side"):
        bridge.dispatch("place", {"account": "1", "code": "x", "side": "hold",
                                  "volume": 100, "price": 10})
    with pytest.raises(ValueError, match="限价"):
        bridge.dispatch("place", {"account": "1", "code": "x", "side": "buy",
                                  "volume": 100, "price": 0})
    with pytest.raises(ValueError, match="数量"):
        bridge.dispatch("place", {"account": "1", "code": "x", "side": "buy",
                                  "volume": 0, "price": 10})


def test_cancel(monkeypatch):
    trader = _FakeTrader(cancel=0)
    _patch(monkeypatch, trader)
    assert bridge.dispatch("cancel", {"account": "1", "order_id": 7}) == \
        {"cancel_result": 0}
    assert trader.last_cancel == 7


def test_connect_failure_is_explicit(monkeypatch):
    """connect() 非 0 必须直接抛（官方接口不会自动重连）。"""
    _patch(monkeypatch, _FakeTrader(), connect_code=1)
    with pytest.raises(RuntimeError, match="connect\\(\\) 返回非 0"):
        bridge.dispatch("asset", {"account": "1"})


def test_main_wraps_everything_as_json(monkeypatch, capsys):
    """`main()` 必须把任何异常变成一行 JSON（stdout 只留响应）。"""
    monkeypatch.setattr(bridge.sys, "stdin",
                        io.StringIO(json.dumps({"op": "asset", "payload": {}}) + "\n"))
    assert bridge.main() == 1
    out = capsys.readouterr().out.strip().splitlines()[-1]
    body = json.loads(out)
    assert body["ok"] is False and "RPSBOARD_QMT_ACCOUNT" in body["error"]


def test_main_reports_unknown_op_as_json(monkeypatch, capsys):
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO(json.dumps(
        {"op": "nope", "payload": {"account": "1"}}) + "\n"))
    assert bridge.main() == 1
    body = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert body["ok"] is False and "未知 op" in body["error"]


def test_main_success_path(monkeypatch, capsys):
    asset = SimpleNamespace(cash=1.0, frozen_cash=0.0, market_value=2.0,
                            total_asset=3.0)
    _patch(monkeypatch, _FakeTrader(asset=asset))
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO(json.dumps(
        {"op": "asset", "payload": {"account": "1"}}) + "\n"))
    assert bridge.main() == 0
    body = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert body["ok"] is True and body["data"]["total_asset"] == 3.0


def test_callback_writes_disconnect_to_stderr(capsys):
    cb = bridge._Callback()
    cb.on_disconnected()
    assert "disconnected" in capsys.readouterr().err.lower()
