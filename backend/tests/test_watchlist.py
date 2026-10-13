"""自选股池与条件预警：落库、幂等、参数校验。

全部离线；库里只放规则与命中需要的几张表（不跑完整回测）。
"""

from __future__ import annotations

import json
import sqlite3

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from rpsboard import config
from rpsboard import db as dbmod
from rpsboard.analytics import alerts as alerts_engine


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """临时库 + 一天 RPS 数据 + 一条涨停样本，够规则评估用。"""
    path = tmp_path / "alerts.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(dbmod.SCHEMA)

    pd.DataFrame({
        "symbol": ["000001", "000002"],
        "name": ["测试一", "测试二"],
        "exchange": ["sz", "sz"],
        "ipo_date": [None, None],
        "is_st": [0, 0],
        "updated_at": ["2026-01-01"] * 2,
    }).to_sql("security_master", conn, if_exists="append", index=False)

    # 000001 的 rps60=95（应命中 90~100 的阈值规则），000002 只有 40
    pd.DataFrame({
        "date": ["2026-01-05", "2026-01-05"],
        "symbol": ["000001", "000002"],
        "rps20": [90.0, 40.0], "rps60": [95.0, 40.0],
        "rps120": [90.0, 40.0], "rps250": [90.0, 40.0],
    }).to_sql("rps_daily", conn, if_exists="append", index=False)

    # 涨跌停：000001 涨停（10% 限价 + 实际涨幅 10%），000002 只涨 5%
    pd.DataFrame({
        "date": ["2026-01-05", "2026-01-05"],
        "symbol": ["000001", "000002"],
        "limit_ratio": [10.0, 10.0],
        "limit_up": [0.0, 0.0], "limit_down": [0.0, 0.0],
        "rule": ["main", "main"], "status": ["normal", "normal"],
    }).to_sql("price_limit_daily", conn, if_exists="append", index=False)
    pd.DataFrame({
        "symbol": ["000001", "000002"],
        "date": ["2026-01-05", "2026-01-05"],
        "close": [11.0, 11.0], "pct_chg": [0.10, 0.05],
        "tradestatus": [1, 1], "is_st": [0, 0],
    }).to_sql("daily_bar", conn, if_exists="append", index=False)

    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", path)
    import rpsboard.api as apimod
    monkeypatch.setattr(apimod, "_cache", {"conn": None, "opened": None})
    from rpsboard.api import create_app

    return TestClient(create_app()), path


# ── 自选股池 ────────────────────────────────────────────────────────
def test_watchlist_add_is_idempotent(client):
    c, path = client
    assert c.post("/api/watchlist", json={"symbol": "000001"}).json()["ok"] is True
    assert c.post("/api/watchlist", json={"symbol": "000001", "note": "改备注"}).json()["ok"] is True
    rows = c.get("/api/watchlist").json()
    assert len(rows) == 1 and rows[0]["note"] == "改备注"
    assert rows[0]["name"] == "测试一"
    assert rows[0]["rps60"] == 95.0

    conn = sqlite3.connect(str(path))
    assert conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 1
    conn.close()


def test_watchlist_remove_reports_removed_count(client):
    c, _ = client
    c.post("/api/watchlist", json={"symbol": "000002"})
    assert c.delete("/api/watchlist/000002").json()["removed"] == 1
    assert c.delete("/api/watchlist/000002").json()["removed"] == 0


def test_watchlist_rejects_bad_symbol(client):
    c, _ = client
    assert c.post("/api/watchlist", json={"symbol": "60051"}).status_code == 422
    assert c.post("/api/watchlist", json={"symbol": "abcdef"}).status_code == 422
    assert c.delete("/api/watchlist/abc").status_code == 422


# ── 规则 ────────────────────────────────────────────────────────────
def test_rule_kind_whitelist(client):
    c, _ = client
    body = c.post("/api/alerts/rules",
                  json={"name": "坏规则", "kind": "nope", "params": {}})
    assert body.status_code == 422
    ok = c.post("/api/alerts/rules", json={
        "name": "RPS90", "kind": "rps_threshold",
        "params": {"period": 60, "min": 90, "max": 100},
    }).json()
    assert ok["ok"] is True and isinstance(ok["id"], int)
    rules = c.get("/api/alerts/rules").json()
    assert rules[0]["params"] == {"period": 60, "min": 90, "max": 100}
    assert rules[0]["enabled"] is True and rules[0]["hits"] == 0


def test_run_rules_threshold_hits_once(client):
    """阈值规则命中 000001（rps60=95）；重复 run 不重复计数。"""
    c, path = client
    c.post("/api/alerts/rules", json={
        "name": "RPS90", "kind": "rps_threshold",
        "params": {"period": 60, "min": 90, "max": 100},
    })
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    first = alerts_engine.run_rules(conn)
    assert [h["symbol"] for h in first] == ["000001"]
    assert "RPS60=95.0" in first[0]["detail"]
    second = alerts_engine.run_rules(conn)
    assert len(second) == 1                      # 仍报命中（幂等写入）
    assert conn.execute("SELECT COUNT(*) FROM alert_hit").fetchone()[0] == 1
    conn.close()

    hits = c.get("/api/alerts/hits").json()
    assert len(hits) == 1
    assert hits[0]["rule_name"] == "RPS90" and hits[0]["name"] == "测试一"


def test_run_rules_limit_up_uses_tolerance(client):
    """涨停判定按 limit_ratio + 容差：+10% 命中，+5% 不命中。"""
    c, path = client
    c.post("/api/alerts/rules", json={"name": "涨停", "kind": "limit_up", "params": {}})
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    hits = alerts_engine.run_rules(conn)
    assert [(h["symbol"], h["detail"]) for h in hits] == [("000001", "涨停 10.00%")]
    conn.close()


def test_run_rules_skips_bad_params(client):
    """params 不是 JSON 对象时跳过该规则，不影响其它规则与调用方。"""
    c, path = client
    conn = sqlite3.connect(str(path))
    conn.executescript(dbmod.SCHEMA)
    conn.execute(
        "INSERT INTO alert_rule(name, kind, params, enabled, created_at) VALUES (?,?,?,?,?)",
        ("坏的", "rps_threshold", "not-json", 1, "2026-01-01"),
    )
    conn.execute(
        "INSERT INTO alert_rule(name, kind, params, enabled, created_at) VALUES (?,?,?,?,?)",
        ("好的", "rps_threshold", json.dumps({"period": 60, "min": 90}), 1, "2026-01-01"),
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    hits = alerts_engine.run_rules(conn)
    assert [h["rule_name"] for h in hits] == ["好的"]
    conn.close()


def test_hits_endpoint_without_data(client):
    """没有命中时返回空数组（不是 500）。"""
    c, _ = client
    assert c.get("/api/alerts/hits").json() == []
    assert "rps_threshold" in c.get("/api/alerts/kinds").json()


def test_update_pipeline_runs_industry_and_alerts(tmp_path, monkeypatch):
    """update 管道接线：空表时补行业分类、末尾跑预警，并返回 alert_hits。

    这里把各采集步骤替换成空实现——验证的是**接线**（谁在什么条件下被调用、
    阶段名、返回值），不是采集逻辑。
    """
    from rpsboard import cli
    from rpsboard import config as cfg
    from rpsboard import db as dbmod

    path = tmp_path / "pipeline.db"
    dbmod.init_db(path)
    monkeypatch.setattr(cfg, "DB_PATH", path)
    monkeypatch.setattr(cli, "cmd_backfill", lambda _ns: 0)
    monkeypatch.setattr(cli, "cmd_compute_rps", lambda _ns: 0)
    monkeypatch.setattr(cli, "cmd_patterns", lambda _ns: 0)
    monkeypatch.setattr(cli, "cmd_industry_map", lambda _ns: 0)
    from rpsboard.sources import limit_src

    monkeypatch.setattr(limit_src, "sync_price_limits", lambda *_a, **_k: 0)
    monkeypatch.setattr(cli.config, "ensure_dirs", lambda: None)

    phases: list[str] = []
    res = cli.run_update_pipeline(progress=phases.append)
    assert phases[0] == "bars" and phases[-1] == "done"
    assert "industry" in phases, "industry_map 为空时必须补行业分类"
    assert "alerts" in phases, "末尾必须跑一次预警"
    assert res["alert_hits"] == 0                    # 没有规则 → 0 命中
    assert res["ok"] is True

    with dbmod.session() as conn:
        conn.execute(
            "INSERT INTO industry_map(symbol, industry, industry_cls, updated_at) "
            "VALUES ('000001', 'X', '测试口径', '2026-01-01')"
        )
        conn.commit()

    phases2: list[str] = []
    cli.run_update_pipeline(progress=phases2.append)
    assert "industry" not in phases2, "已有行业数据时必须跳过，不能悄悄换口径"


def test_cli_alert_rm_removes_hits(tmp_path, monkeypatch):
    """CLI 删规则要连带删命中记录（与 API 的 DELETE 行为一致）。

    否则 alert_hit 里留下 rule_name 为空的孤儿行，页面与 /api/alerts/hits 都会显示它们。
    """
    from rpsboard import cli
    from rpsboard import config as cfg
    from rpsboard import db as dbmod

    path = tmp_path / "alerts_cli.db"
    dbmod.init_db(path)
    monkeypatch.setattr(cfg, "DB_PATH", path)

    class _Ns:
        action, name, kind, params, id = "add", "RPS90", "rps_threshold", "{}", None

    assert cli.cmd_alert(_Ns()) == 0
    with dbmod.session() as conn:
        conn.execute("INSERT INTO alert_hit(rule_id, date, symbol, detail) "
                     "VALUES (1, '2026-01-05', '600519', 'RPS60=95.0')")
        conn.commit()

    _Ns.action, _Ns.id = "rm", "1"
    assert cli.cmd_alert(_Ns()) == 0
    with dbmod.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_rule").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM alert_hit").fetchone()[0] == 0
