"""`broker/qmt.py` 的子进程 JSON 协议测试（不需要 QMT 客户端）。

被测的是**协议层**：8 个 op 的请求/响应、错误透传、超时、断线事件、
前置检查（缺账号 / 缺 3.11 解释器 / 非法 TRADE_MODE）。真正的 xtquant 调用
无法在 3.12 下测（编译扩展只到 cp311），那是唯一没有覆盖的部分。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest
from rpsboard import config
from rpsboard.broker import qmt

FAKE_SRC = Path(__file__).with_name("fake_qmt.py")


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    """把 fake_qmt.py 装成一个假的 rpsboard.broker.bridge，并把解释器指到当前 Python。"""
    root = tmp_path / "fakeqmt"
    pkg = root / "rpsboard" / "broker"
    pkg.mkdir(parents=True)
    (root / "rpsboard" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "bridge.py").write_text(FAKE_SRC.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(qmt, "BACKEND_DIR", root)
    monkeypatch.setattr(config, "QMT_PYTHON", Path(sys.executable))
    monkeypatch.setattr(config, "QMT_ACCOUNT", "0000000123")
    monkeypatch.setattr(qmt, "LAST_DISCONNECT_AT", None, raising=False)
    for var in ("FAKE_QMT_FAIL", "FAKE_QMT_DISCONNECT", "FAKE_QMT_HANG",
                "FAKE_QMT_NO_TICK", "FAKE_QMT_EMPTY_ASSET"):
        monkeypatch.delenv(var, raising=False)
    return root


def test_status_returns_masked_account_and_asset(bridge):
    st = qmt.status()
    assert st["connected"] is True
    assert st["account_masked"] == "****0123"
    assert st["asset"]["cash"] == 1_000_000.0
    # 账号原文绝不能出现在任何一层输出里
    assert "0000000123" not in str(st)


def test_positions_orders_trades_shapes(bridge):
    pos = qmt.positions()
    assert pos[0]["symbol"] == "000001" and pos[0]["volume"] == 1000
    assert qmt.orders()[0]["status"] == "filled"
    assert qmt.trades()[0]["traded_volume"] == 1000


def test_tick_omits_codes_without_price(bridge, monkeypatch):
    monkeypatch.setenv("FAKE_QMT_NO_TICK", "000002")
    ticks = qmt.tick(["000001", "000002"])
    assert "000001" in ticks and "000002" not in ticks


def test_place_rejects_non_positive_price(bridge):
    with pytest.raises(qmt.QmtError):
        qmt.place(code="000001", side="buy", volume=100, price=0)
    assert qmt.place(code="000001", side="buy", volume=100, price=12.6)


def test_error_from_bridge_is_raised_verbatim(bridge, monkeypatch):
    monkeypatch.setenv("FAKE_QMT_FAIL", "1")
    with pytest.raises(qmt.QmtError, match="connect"):
        qmt.status()


def test_timeout_becomes_actionable_error(bridge, monkeypatch):
    monkeypatch.setenv("FAKE_QMT_HANG", "1")
    with pytest.raises(qmt.QmtError, match="超时"):
        qmt.run("status", timeout=1.0)


def test_disconnect_event_sets_marker_and_fails_call(bridge, monkeypatch):
    monkeypatch.setenv("FAKE_QMT_DISCONNECT", "1")
    with pytest.raises(qmt.QmtError, match="断开"):
        qmt.status()
    assert isinstance(qmt.LAST_DISCONNECT_AT, datetime)


def test_missing_account_names_env_local(bridge, monkeypatch):
    monkeypatch.setattr(config, "QMT_ACCOUNT", "")
    with pytest.raises(qmt.QmtError, match=r"\.env\.local"):
        qmt.status()


def test_missing_python_points_at_uv_venv(bridge, monkeypatch):
    monkeypatch.setattr(config, "QMT_PYTHON", Path("D:/no/such/python.exe"))
    with pytest.raises(qmt.QmtError, match=r"uv venv --python 3.11"):
        qmt.status()


def test_unset_python_defaults_to_actionable_error(bridge, monkeypatch):
    """默认值刻意留空（不把本机路径写进公开仓库）→ 报错必须指路而不是 AttributeError。"""
    monkeypatch.setattr(config, "QMT_PYTHON", None)
    with pytest.raises(qmt.QmtError, match=r"RPSBOARD_QMT_PYTHON"):
        qmt.status()


def test_illegal_trade_mode_is_rejected(bridge, monkeypatch):
    monkeypatch.setattr(config, "TRADE_MODE", "live")
    with pytest.raises(qmt.QmtError, match="非法"):
        qmt.status()


def test_payload_carries_configured_paths_and_account(bridge, tmp_path, monkeypatch):
    """子进程收到的 payload 必须来自配置（否则改配置等于没改）。"""
    echo = tmp_path / "echo.json"
    monkeypatch.setenv("FAKE_QMT_ECHO", str(echo))
    monkeypatch.setattr(config, "QMT_USERDATA_MINI", Path("D:/cfg/userdata_mini"))
    qmt.status()
    sent = json.loads(echo.read_text(encoding="utf-8"))
    assert sent["op"] == "status"
    assert sent["payload"]["userdata_mini"] == str(Path("D:/cfg/userdata_mini"))
    assert sent["payload"]["account"] == "0000000123"
    assert isinstance(sent["payload"]["session_id"], int)
