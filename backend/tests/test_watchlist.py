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

    with TestClient(create_app()) as client:
        yield client, path


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


def test_watchlist_supports_date(client):
    """自选可以按日回看（原先硬编码最新交易日）。"""
    c, path = client
    c.post("/api/watchlist", json={"symbol": "000001"})
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT INTO rps_daily(date, symbol, rps60) VALUES"
                 " ('2026-01-06', '000001', 42.0)")
    conn.commit()
    conn.close()

    latest = c.get("/api/watchlist").json()
    assert latest[0]["data_date"] == "2026-01-06" and latest[0]["rps60"] == 42.0

    past = c.get("/api/watchlist", params={"date": "2026-01-05"}).json()
    assert past[0]["data_date"] == "2026-01-05" and past[0]["rps60"] == 95.0

    # 非交易日就近回退（不是静默换成最新一天之前，而是落到不晚于该日的最近一天）
    fb = c.get("/api/watchlist", params={"date": "2030-01-01"}).json()
    assert fb[0]["data_date"] == "2026-01-06"


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


def test_run_rules_limit_up_ignores_unrestricted_days(client):
    """无涨跌幅限制的日期不能算涨停，且重算要清掉旧的误报。

    口径真源是 `limits.limit_mask`：`limit_ratio > 0` 且 status 不在
    `NO_LIMIT_STATUSES` 里，才算「当日有涨跌停约束」。预警此前不看这两条，
    ratio=0 时 `pct >= 0 - 0.2` 对任何上涨都为真 → 新股/复牌日全天误报。
    """
    c, path = client
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    day = "2026-01-05"
    extra = [
        # (symbol, ratio, status, pct_chg, 期望命中)
        ("000003", 10.0, None, 0.10, True),                 # NULL status = 正常
        ("000004", 0.0, "no_price_limit", 0.40, False),      # 无限制日，大涨也不算
        ("000005", 10.0, "missing_pre_close", 0.40, False),  # 缺前收盘
        ("000006", 0.0, "normal", 0.0, False),               # ratio=0 不算
        ("000007", -5.0, "normal", 0.0, False),              # 负比率不算
        ("000008", None, "normal", 0.10, False),             # ratio 缺失不算
    ]
    conn.executemany(
        "INSERT INTO price_limit_daily(date, symbol, limit_ratio, limit_up, limit_down,"
        " rule, status) VALUES (?,?,?,0,0,'main',?)",
        [(day, s, r, st) for s, r, st, _p, _hit in extra],
    )
    conn.executemany(
        "INSERT INTO daily_bar(symbol, date, close, pct_chg, tradestatus, is_st)"
        " VALUES (?,?,11,?,1,0)",
        [(s, day, p) for s, _r, _st, p, _hit in extra],
    )
    conn.execute(
        "INSERT INTO alert_rule(name, kind, params, enabled, created_at)"
        " VALUES ('涨停', 'limit_up', '{}', 1, '2026-01-05')")
    conn.commit()

    hits = alerts_engine.run_rules(conn, day)
    assert {h["symbol"] for h in hits} == {"000001", "000003"}, hits

    # 行情修正（000001 不再涨停）后重算：必须清掉旧命中，而不是只追加
    conn.execute("UPDATE daily_bar SET pct_chg = 0.05 WHERE symbol='000001' AND date=?",
                 (day,))
    conn.commit()
    again = alerts_engine.run_rules(conn, day)
    stored = {r["symbol"] for r in conn.execute(
        "SELECT symbol FROM alert_hit WHERE rule_id=1 AND date=?", (day,))}
    conn.close()
    assert "000001" not in {h["symbol"] for h in again}
    assert "000001" not in stored, "重算必须清掉不再成立的旧命中"

    # 命中端点同样不再返回它
    assert "000001" not in {h["symbol"] for h in c.get("/api/alerts/hits").json()}


def test_run_rules_refresh_is_atomic_on_insert_failure(client):
    """重算的「清旧 + 写新」必须同一事务：写入失败不得留下已删未写的空结果。"""
    c, path = client
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    day = "2026-01-05"
    conn.execute(
        "INSERT INTO alert_rule(name, kind, params, enabled, created_at)"
        " VALUES ('涨停', 'limit_up', '{}', 1, '2026-01-05')")
    # 先落一条旧命中（模拟上一轮结果），再加一个必然失败的触发器
    conn.execute("INSERT INTO alert_hit(rule_id, date, symbol, detail)"
                 " VALUES (1, ?, '000001', '涨停 10.00%')", (day,))
    conn.execute(
        "CREATE TRIGGER fail_hit BEFORE INSERT ON alert_hit"
        " BEGIN SELECT RAISE(ABORT, 'test insert failure'); END")
    conn.commit()

    with pytest.raises(sqlite3.Error):
        alerts_engine.run_rules(conn, day)

    kept = conn.execute("SELECT symbol FROM alert_hit WHERE rule_id=1 AND date=?",
                        (day,)).fetchall()
    conn.close()
    assert [r["symbol"] for r in kept] == ["000001"], "写入失败时必须回滚删除"


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


def test_review_limit_list_matches_alert_rule(client):
    """复盘页的当日涨停清单与 `limit_up` 预警规则必须给同一批股票。

    两者共用 `limits.daily_limit_lists` —— 这条用例就是防止哪天有人在其中
    一处又抄一遍判据（涨跌停口径分叉过一次，代价是整批误报）。
    """
    c, path = client
    c.post("/api/alerts/rules", json={"name": "涨停", "kind": "limit_up",
                                      "params": {}})
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    hits = alerts_engine.run_rules(conn, "2026-01-05")
    conn.close()

    body = c.get("/api/review/day",
                 params={"date": "2026-01-05", "threshold": 0}).json()
    assert {h["symbol"] for h in hits} == {x["symbol"] for x in body["limit_up"]}
    assert {x["symbol"] for x in body["limit_up"]} == {"000001"}, body["limit_up"]
    assert all(x["streak"] >= 1 for x in body["limit_up"])


def test_update_pipeline_paper_phase_is_opt_in(tmp_path, monkeypatch):
    """`paper=True` 才推进模拟账户；且**只推进 active 的策略账户**。

    验证的是**接线**：默认关闭（推进要加载全市场矩阵，耗时以分钟计）；打开时
    对每个 active/strategy 账户调用 `sim.advance`，手动账户与 legacy 一律不动。
    """
    from rpsboard import cli
    from rpsboard import config as cfg
    from rpsboard import db as dbmod
    from rpsboard.broker import accounts as accounts_mod
    from rpsboard.broker import sim

    path = tmp_path / "paper_pipe.db"
    dbmod.init_db(path)
    monkeypatch.setattr(cfg, "DB_PATH", path)
    monkeypatch.setattr(cfg, "ensure_dirs", lambda: None)
    monkeypatch.setattr(cli, "cmd_backfill", lambda _ns: 0)
    monkeypatch.setattr(cli, "cmd_compute_rps", lambda _ns: 0)
    monkeypatch.setattr(cli, "cmd_patterns", lambda _ns: 0)
    monkeypatch.setattr(cli, "cmd_industry_map", lambda _ns: 0)
    monkeypatch.setattr(cli, "cmd_corporate", lambda _ns: 0)
    from rpsboard.sources import limit_src

    monkeypatch.setattr(limit_src, "sync_price_limits", lambda *_a, **_k: 0)
    calls: list[int] = []
    monkeypatch.setattr(sim, "advance", lambda conn, **kw: (
        calls.append(kw["account_id"]), {"days": 3, "settled_through": "2026-01-07",
                                         "blocked_reason": None})[1])

    phases: list[str] = []
    res = cli.run_update_pipeline(progress=phases.append)
    assert res["paper"] is None and not calls, "默认不该推进模拟账户"
    assert "paper" not in phases

    # 建两个账户：一个 active 策略（要推进）、一个归档的（不该推进）
    with dbmod.session(path) as conn:
        conn.execute("INSERT INTO benchmark_bar(symbol,date,close)"
                     " VALUES ('000001','2026-01-05',3000)")
        conn.commit()
        live = accounts_mod.create_account(
            conn, name="要推进", kind="strategy", cash=100000.0,
            start_date="2026-01-05", params={"period": 60, "threshold": 0.0,
                                             "hold": 1, "max_positions": 2,
                                             "exclude_st": True, "resonance": False,
                                             "signal": "rps"})
        dead = accounts_mod.create_account(
            conn, name="已归档", kind="strategy", cash=100000.0,
            start_date="2026-01-05")
        accounts_mod.archive_account(conn, dead["id"])

    phases2: list[str] = []
    res2 = cli.run_update_pipeline(progress=phases2.append, paper=True)
    assert "paper" in phases2 and phases2[-1] == "done"
    assert calls == [live["id"]], f"只推进 active 策略账户，实际 {calls}"
    assert res2["paper"]["accounts"][0]["id"] == live["id"]


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


def test_alert_rule_params_validated_by_kind(client):
    """params 按 kind 校验并归一化：非法 422，合法值落库前被规范化。

    非法的 params 以前会**原样存进 alert_rule**，直到 cli alert run 才出问题：
    `{"min": null}` 让 float(None) 抛 TypeError，period 不在周期真源里会静默
    筛错列，`window: 7` 会被引擎悄悄换回 5。
    """
    c, _ = client
    bad = [
        {"name": "空min", "kind": "rps_threshold",
         "params": {"period": 60, "min": None}},
        {"name": "错period", "kind": "rps_threshold", "params": {"period": 30}},
        {"name": "区间倒序", "kind": "rps_threshold",
         "params": {"min": 90, "max": 10}},
        {"name": "非数字", "kind": "rps_threshold", "params": {"min": "abc"}},
        {"name": "超范围", "kind": "rps_threshold", "params": {"max": 101}},
        {"name": "错窗口", "kind": "sanxingdi", "params": {"window": 7}},
    ]
    for body in bad:
        assert c.post("/api/alerts/rules", json=body).status_code == 422, body

    ok = c.post("/api/alerts/rules", json={
        "name": "涨停", "kind": "limit_up", "params": {"period": 60}}).json()
    assert ok["ok"] is True
    rules = {r["id"]: r for r in c.get("/api/alerts/rules").json()}
    assert rules[ok["id"]]["params"] == {}, "limit_up 必须丢掉多余参数"

    norm = c.post("/api/alerts/rules", json={
        "name": "默认区间", "kind": "rps_threshold",
        "params": {"period": "60"}}).json()
    assert norm["ok"] is True
    rules = {r["id"]: r for r in c.get("/api/alerts/rules").json()}
    assert rules[norm["id"]]["params"] == {"period": 60, "min": 0.0, "max": 100.0}


def test_run_rules_survives_bad_row_and_keeps_other_hits(client):
    """库里已有坏规则行时，run_rules 不崩、后续规则照常命中并落库。

    坏行直接写进库（跳过接口校验），模拟历史遗留数据：
    - id=1 `params` 是 JSON 数组 → 跳过
    - id=2 `min` 是 null → 容错为 0（旧实现 float(None) 抛 TypeError，
      一条规则抛错还会把前面已写入的命中一起回滚）
    - id=3 合法规则 → 命中必须在同一轮里持久化
    """
    c, path = client
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executemany(
        "INSERT INTO alert_rule(name, kind, params, enabled, created_at) "
        "VALUES (?,?,?,1,?)",
        [
            ("坏格式", "rps_threshold", "[1, 2]", "2026-01-01"),
            ("空min", "rps_threshold", json.dumps({"period": 60, "min": None}),
             "2026-01-01"),
            ("RPS90", "rps_threshold",
             json.dumps({"period": 60, "min": 90, "max": 100}), "2026-01-01"),
        ],
    )
    conn.commit()
    hits = alerts_engine.run_rules(conn)
    stored = conn.execute(
        "SELECT rule_id, symbol FROM alert_hit ORDER BY rule_id"
    ).fetchall()
    conn.close()
    assert [h["symbol"] for h in hits if h["rule_id"] == 3] == ["000001"]
    by_rule = {(r["rule_id"], r["symbol"]) for r in stored}
    assert (3, "000001") in by_rule, \
        "合法规则的命中必须在同一轮里落库（不被前面坏行回滚）"
    assert all(rid != 1 for rid, _ in by_rule), "params 不是对象的规则必须被跳过"
    assert (2, "000001") in by_rule and (2, "000002") in by_rule, \
        "min=null 必须容错成 0~100（旧实现 float(None) 会抛 TypeError）"
