"""模拟账户 CLI 的行为（`cli trade account/run/paper/archive/risk`）。

这些命令是**用户实际会敲的入口**，也是「参数归位」的验收点：
资金/起点/策略参数只在**建账户**时给，`run` 只挑账户与终点；
旧的 `--from/--cash/--reset` 必须被 argparse 明确拒绝，而不是被静默忽略。
"""

from __future__ import annotations

import argparse

import pytest
from rpsboard import cli, config
from rpsboard import db as dbmod
from rpsboard.broker import accounts as accounts_mod

from .test_portfolio import seed_portfolio_smoke


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "cli_trade.db"
    info = seed_portfolio_smoke(str(path))
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    return path, info


def _ns(**over):
    base = {"name": None, "kind": "manual", "cash": 100_000.0, "from_date": None,
            "max_participation": None, "period": 60, "threshold": 80.0, "hold": 20,
            "max_positions": 20, "resonance": False, "no_exclude_st": False,
            "signal": "rps", "account": None, "to": None, "label": None,
            "list_history": False, "push": False, "dry_run": False,
            "max_single": None, "max_industry": None, "max_avg_corr": None,
            "max_drawdown": None}
    base.update(over)
    return argparse.Namespace(**base)


# ── account list / new ──────────────────────────────────────────────
def test_account_list_prints_seeded_accounts(env, capsys):
    _, info = env
    assert cli.cmd_trade_account_list(_ns()) == 0
    out = capsys.readouterr().out
    assert "冒烟手动" in out and "冒烟策略" in out
    assert "manual" in out and "strategy" in out


def test_account_list_on_empty_db_explains_how_to_start(tmp_path, monkeypatch, capsys):
    path = tmp_path / "empty_cli.db"
    dbmod.init_db(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    assert cli.cmd_trade_account_list(_ns()) == 0
    assert "trade account new" in capsys.readouterr().out


def test_account_new_manual_and_strategy(env, capsys):
    _, info = env
    code = cli.cmd_trade_account_new(_ns(name="CLI 手动", kind="manual",
                                        cash=123456.0,
                                        from_date=info["start_date"]))
    assert code == 0
    out = capsys.readouterr().out
    assert "CLI 手动" in out and "123,456.00" in out

    code = cli.cmd_trade_account_new(_ns(name="CLI 策略", kind="strategy", cash=50000.0,
                                        from_date=info["start_date"], hold=5,
                                        signal="rps", max_positions=3))
    assert code == 0
    assert "策略参数" in capsys.readouterr().out

    with dbmod.session(config.DB_PATH) as conn:
        rows = {r["name"]: r for r in accounts_mod.list_accounts(conn)}
    assert rows["CLI 策略"]["params"]["hold"] == 5
    assert rows["CLI 策略"]["params"]["max_positions"] == 3
    assert rows["CLI 手动"]["params"] is None


def test_account_new_rejects_bad_input(env, capsys):
    _, info = env
    # 起点不是交易日
    assert cli.cmd_trade_account_new(_ns(name="x", from_date="1999-01-01")) == 2
    assert "交易日" in capsys.readouterr().err
    # 缺名字（argparse 层挡住；这里直接给空名验证业务校验）
    assert cli.cmd_trade_account_new(_ns(name="", from_date=info["start_date"])) == 2


def test_legacy_run_options_are_rejected_by_argparse():
    """`--from/--cash/--reset` 这些旧选项必须**明确报错**，不能接受后忽略。"""
    parser = cli.build_parser()
    for argv in (["trade", "run", "--from", "2026-01-01"],
                 ["trade", "run", "--cash", "1000000"],
                 ["trade", "run", "--reset", "--yes-reset"],
                 ["trade", "archive", "--rm", "1"],
                 ["trade", "archive", "--save"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_run_and_paper_and_risk_and_archive(env, capsys):
    path, info = env
    aid = info["manual_account_id"]
    # 推进
    assert cli.cmd_trade_paper_run(_ns(account=aid)) == 0
    assert "已结算到" in capsys.readouterr().out
    # 打印账户
    assert cli.cmd_trade_paper(_ns(account=aid)) == 0
    assert "初始资金" in capsys.readouterr().out
    # 风险体检（无告警 → 0）
    assert cli.cmd_trade_risk(_ns(account=aid)) == 0
    assert "没有触发任何告警" in capsys.readouterr().out
    # 归档
    assert cli.cmd_trade_archive(_ns(account=aid, label="CLI 归档")) == 0
    assert "已归档" in capsys.readouterr().out
    # 列出历史
    assert cli.cmd_trade_archive(_ns(list_history=True)) == 0
    assert "CLI 归档" in capsys.readouterr().out


def test_paper_on_account_without_curve(tmp_path, monkeypatch, capsys):
    path = tmp_path / "nocurve.db"
    dbmod.init_db(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    with dbmod.session(path) as conn:
        conn.execute("INSERT INTO benchmark_bar(symbol,date,close)"
                     " VALUES ('000001','2025-03-14',1)")
        conn.commit()
        acc = accounts_mod.create_account(conn, name="无曲线", kind="manual",
                                          cash=1000.0, start_date="2025-03-14")
        # 手工抹掉创建那只净值行，模拟「还没推进过」的老账户
        conn.execute("DELETE FROM paper_nav WHERE account_id=?", (acc["id"],))
        conn.commit()
    assert cli.cmd_trade_paper(_ns(account=acc["id"])) == 0
    assert "trade run --account" in capsys.readouterr().out


def test_run_reports_data_gap_with_exit_code_3(env, tmp_path, capsys):
    """数据缺口必须**报出来**并且退出码非 0（脚本/CI 靠它发现问题）。"""
    path, info = env
    aid = info["manual_account_id"]
    with dbmod.session(path) as conn:
        conn.execute("DELETE FROM raw_bar WHERE symbol='000001'")
        conn.execute("DELETE FROM price_limit_daily WHERE symbol='000001'")
        conn.execute(
            "INSERT INTO paper_order(account_id, request_id, trade_date, symbol, side,"
            " volume, price, status, source, created_at, updated_at)"
            " VALUES (?, 'x', ?, '000001', 'buy', 100, 10.0, 'pending',"
            " 'manual', 'x', 'x')", (aid, info["start_date"]))
        conn.commit()
    code = cli.cmd_trade_paper_run(_ns(account=aid))
    out = capsys.readouterr().out
    assert code == 3
    assert "raw_bar_missing" in out and "补齐数据" in out


def test_run_on_archived_account_fails(env, capsys):
    _, info = env
    aid = info["manual_account_id"]
    with dbmod.session(config.DB_PATH) as conn:
        accounts_mod.archive_account(conn, aid)
    assert cli.cmd_trade_paper_run(_ns(account=aid)) == 2
    assert "已归档" in capsys.readouterr().err


def test_risk_push_is_gated_by_flag(env, monkeypatch, capsys):
    """未配置推送地址时 `--push` 要给出明确错误码（默认只打印）。"""
    _, info = env
    aid = info["manual_account_id"]
    monkeypatch.setattr(config, "SHOWDOC_PUSH_URL", "")
    # 先制造一个必然触发的告警：单票权重上限设成 0 附近
    with dbmod.session(config.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO paper_position(account_id, trade_date, symbol, volume,"
            " available_volume, avg_price, cost_basis, close, price_date, market_value)"
            " VALUES (?,?, '000001', 1000, 1000, 10, 10000, 10, ?, 10000)",
            (aid, info["start_date"], info["start_date"]))
        conn.commit()
    code = cli.cmd_trade_risk(_ns(account=aid, max_single=0.0001))
    assert code == 3
    assert "只是打印" in capsys.readouterr().out
    # --dry-run 也只打印
    assert cli.cmd_trade_risk(_ns(account=aid, max_single=0.0001, dry_run=True)) == 3


def test_trade_dispatch_unknown_action(capsys):
    assert cli.cmd_trade(_ns(action="nope")) == 2
    assert "用法" in capsys.readouterr().err
    assert cli.cmd_trade(_ns(action="account", account_action="nope")) == 2


def test_archive_without_account_or_history_is_rejected_by_argparse():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["trade", "archive"])
