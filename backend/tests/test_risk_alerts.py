"""模拟账户的组合风险告警：阈值触发、默认不推送。

守的是两件事：**判据本身**（权重/相关性/回撤怎么算、超没超）与
**推送边界**（默认只打印，`--push` 才发；未配置推送地址时给明确错误码）。

账户口径现在是显式的 `account_id`：没有「默认账户」这回事。
"""

from __future__ import annotations

import pytest
from rpsboard import config
from rpsboard import db as dbmod
from rpsboard.broker import accounts as accounts_mod
from rpsboard.broker import risk_alerts, sim


def _seed(conn, *, navs, positions, industries=None, kind="manual"):
    """造一个最小账户：净值若干天 + 指定日持仓。"""
    conn.execute(
        "INSERT INTO paper_account(name, kind, price_basis, initial_cash, cash,"
        " start_date, status, created_at, updated_at)"
        " VALUES ('风险测试',?, 'raw', 1000000, 0, ?, 'active', '2026-01-01',"
        " '2026-01-01')", (kind, navs[0][0]))
    aid = int(conn.execute("SELECT MAX(id) FROM paper_account").fetchone()[0])
    conn.executemany(
        "INSERT INTO paper_nav(account_id, trade_date, cash, market_value, receivable,"
        " total, nav, n_positions, filled_orders) VALUES (?,?,0,?,0,?,?,?,0)",
        [(aid, d, t, t, t / 1_000_000.0, len(positions)) for d, t in navs])
    conn.executemany(
        "INSERT INTO paper_position(account_id, trade_date, symbol, volume,"
        " available_volume, avg_price, cost_basis, close, price_date, market_value)"
        " VALUES (?,?,?,100,100,10.0,1000.0,10.0,?,?)",
        [(aid, navs[-1][0], s, navs[-1][0], mv) for s, mv in positions])
    for sym, ind in (industries or {}).items():
        conn.execute("INSERT OR REPLACE INTO industry_map(symbol, industry,"
                     " industry_cls, updated_at) VALUES (?,?,'证监会','2026-01-01')",
                     (sym, ind))
    conn.commit()
    return aid


def test_no_alerts_when_below_thresholds(market_db):
    with dbmod.session(market_db) as conn:
        _seed(conn, navs=[("2026-01-01", 1_000_000.0), ("2026-01-02", 1_000_000.0)],
              positions=[("000001", 10_000.0)])
        aid = int(conn.execute("SELECT MAX(id) FROM paper_account").fetchone()[0])
        alerts = risk_alerts.evaluate(
            conn, account_id=aid, max_single=0.99, max_industry=0.99,
            max_avg_corr=0.99, max_drawdown=0.99)
    assert alerts == []


def test_single_weight_alert(market_db):
    with dbmod.session(market_db) as conn:
        aid = _seed(conn, navs=[("2026-01-01", 1_000_000.0),
                                ("2026-01-02", 1_000_000.0)],
                    positions=[("000001", 400_000.0), ("000002", 100_000.0)],
                    industries={"000001": "银行", "000002": "软件"})
        alerts = risk_alerts.evaluate(conn, account_id=aid, max_single=0.25,
                                      max_industry=0.99, max_avg_corr=0.99,
                                      max_drawdown=0.99)
    kinds = {a["kind"] for a in alerts}
    assert kinds == {"single_weight"}, alerts
    a = alerts[0]
    assert a["value"] == pytest.approx(0.4) and a["limit"] == 0.25
    assert "000001" in a["detail"] and "40.00%" in a["detail"]


def test_industry_weight_alert(market_db):
    with dbmod.session(market_db) as conn:
        aid = _seed(conn, navs=[("2026-01-01", 1_000_000.0),
                                ("2026-01-02", 1_000_000.0)],
                    positions=[("000001", 200_000.0), ("000002", 200_000.0)],
                    industries={"000001": "银行", "000002": "银行"})
        alerts = risk_alerts.evaluate(conn, account_id=aid, max_single=0.9,
                                      max_industry=0.3, max_avg_corr=0.99,
                                      max_drawdown=0.99)
    assert [a["kind"] for a in alerts] == ["industry_weight"]
    assert "银行" in alerts[0]["detail"] and alerts[0]["value"] == pytest.approx(0.4)


def test_current_drawdown_alert(market_db):
    """回撤看的是**当前**距峰值的跌幅，不是历史最大回撤。"""
    with dbmod.session(market_db) as conn:
        aid = _seed(conn, navs=[("2026-01-01", 1_000_000.0),
                                ("2026-01-02", 1_200_000.0),
                                ("2026-01-03", 900_000.0)],
                    positions=[("000001", 100_000.0)])
        assert risk_alerts.current_drawdown(conn, account_id=aid) == \
            pytest.approx(0.25, abs=1e-9)
        alerts = risk_alerts.evaluate(conn, account_id=aid, max_single=0.99,
                                      max_industry=0.99, max_avg_corr=0.99,
                                      max_drawdown=0.2)
    assert [a["kind"] for a in alerts] == ["drawdown"]
    assert alerts[0]["value"] == pytest.approx(0.25, abs=1e-9)
    assert "25.00%" in alerts[0]["detail"]


def test_correlation_alert_uses_average(market_db, monkeypatch):
    """相关性告警用**平均**两两相关（并在详情里点出最像的一对）。"""
    with dbmod.session(market_db) as conn:
        aid = _seed(conn, navs=[("2026-01-01", 1_000_000.0),
                                ("2026-01-02", 1_000_000.0)],
                    positions=[("000001", 100_000.0), ("000002", 100_000.0)],
                    industries={"000001": "银行", "000002": "软件"})
        monkeypatch.setattr(risk_alerts.sim, "review", lambda conn, **kw: {
            "risk": {"n_positions": 2,
                     "top_weight": {"symbol": "000001", "weight": 0.05},
                     "top_industry": {"industry": "银行", "weight": 0.05},
                     "corr_avg": 0.83,
                     "corr_max": {"a": "000001", "b": "000002", "value": 0.83}}})
        alerts = risk_alerts.evaluate(conn, account_id=aid, max_single=0.99,
                                      max_industry=0.99, max_avg_corr=0.7,
                                      max_drawdown=0.99)
    assert [a["kind"] for a in alerts] == ["correlation"]
    assert "0.83" in alerts[0]["detail"] and "000001·000002" in alerts[0]["detail"]


def test_empty_account_has_no_alerts(market_db):
    """没有曲线/没有持仓时不报（空账户不是"风险为零"，是"无从谈起"）。"""
    with dbmod.session(market_db) as conn:
        assert risk_alerts.evaluate(conn) == []
        assert risk_alerts.current_drawdown(conn) is None
        # 显式给了一个不存在的账户：同样不能报，也不能崩
        assert risk_alerts.evaluate(conn, account_id=999) == []


def test_thresholds_override_and_format():
    """命令行覆盖只影响本次；推送文案包含数值与阈值。"""
    t = risk_alerts.thresholds(max_single=0.1)
    assert t["max_single_weight"] == 0.1
    assert t["max_industry_weight"] == config.RISK_MAX_INDUSTRY_WEIGHT

    alerts = [{"kind": "single_weight", "name": "单票集中度", "value": 0.4,
               "limit": 0.25, "detail": "单票 000001 占 40.00% > 上限 25%"}]
    title, content = risk_alerts.format_alerts(alerts, t)
    assert "风险告警 1 条" in title
    assert "000001" in content and "40.00%" in content and "≤ 10.00%" in content

    empty_title, empty_body = risk_alerts.format_alerts([], t)
    assert "无告警" in empty_title and "阈值内" in empty_body


def test_risk_review_is_scoped_to_account(market_db):
    """风险体检必须只看**这一个**账户（旧实现读「当前唯一账户」）。"""
    with dbmod.session(market_db) as conn:
        a = _seed(conn, navs=[("2026-01-01", 1_000_000.0),
                              ("2026-01-02", 1_000_000.0)],
                  positions=[("000001", 400_000.0)])
        b = _seed(conn, navs=[("2026-01-01", 1_000_000.0),
                              ("2026-01-02", 1_000_000.0)],
                  positions=[("000002", 10_000.0)])
        assert a != b
        alerts_a = risk_alerts.evaluate(conn, account_id=a, max_single=0.25,
                                        max_industry=0.99, max_avg_corr=0.99,
                                        max_drawdown=0.99)
        alerts_b = risk_alerts.evaluate(conn, account_id=b, max_single=0.25,
                                        max_industry=0.99, max_avg_corr=0.99,
                                        max_drawdown=0.99)
    assert [x["kind"] for x in alerts_a] == ["single_weight"]
    assert alerts_b == []


def test_account_list_helpers(market_db):
    """账户列表/取单个账户的基础行为（页面与研究脚本都依赖）。"""
    with dbmod.session(market_db) as conn:
        aid = _seed(conn, navs=[("2026-01-01", 1_000_000.0)], positions=[])
        acc = accounts_mod.get_account(conn, aid)
        assert acc["kind"] == "manual" and acc["price_basis"] == "raw"
        assert acc["latest_date"] == "2026-01-01"
        rows = accounts_mod.list_accounts(conn)
        assert [r["id"] for r in rows] == [aid]
        with pytest.raises(accounts_mod.AccountError):
            accounts_mod.get_account(conn, aid + 100)
        archived = accounts_mod.archive_account(conn, aid)
        assert archived["status"] == "archived"
        assert sim.summary(conn, account_id=aid)["days"] == 1, "归档不改历史"
