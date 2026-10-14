"""CLI 入口的行为测试（此前 cli.py 零覆盖，一个连接用错的 bug 就这么溜过去了）。"""

from __future__ import annotations

import sqlite3

from rpsboard import cli, config
from rpsboard import db as dbmod


def test_cmd_backfill_returns_zero_and_cleans_pct_chg(tmp_path, monkeypatch):
    """backfill 结束后清理 pct_chg 必须能跑通。

    原实现把 `clean_bad_pct_chg(conn)` 写在 `with db.session() as conn` 之外，
    连接已关闭 → sqlite3.ProgrammingError：日K 拉完后整个 update 管道中断
    （涨跌停 / RPS / 形态 / 预警全没跑），而 collect_log 里那条记录却是 success。
    """
    path = tmp_path / "backfill.db"
    dbmod.init_db(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(cli.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(cli.tdxnet_src, "backfill_bars", lambda *a, **k: (3, 300))

    with dbmod.session() as conn:
        conn.execute(
            "INSERT INTO daily_bar(symbol, date, close, pct_chg) VALUES ('000001','2026-01-05',1.0,9.9)"
        )
        conn.commit()

    rc = cli.cmd_backfill(cli._ns(workers=1))
    assert rc == 0

    conn = sqlite3.connect(str(path))
    assert conn.execute(
        "SELECT COUNT(*) FROM daily_bar WHERE pct_chg IS NOT NULL"
    ).fetchone()[0] == 0, "异常幅度（|pct_chg|>1.2）必须被清掉"
    # 采集日志也应当写着成功
    assert conn.execute(
        "SELECT status FROM collect_log ORDER BY started_at DESC LIMIT 1"
    ).fetchone()[0] == "success"
    conn.close()


def _flags(path, symbol="000001"):
    conn = sqlite3.connect(str(path))
    rows = conn.execute(
        "SELECT tradestatus FROM daily_bar WHERE symbol=? ORDER BY date", (symbol,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def test_cmd_tradestatus_writes_only_changes_and_is_idempotent(
    tmp_path, monkeypatch, capsys
):
    """长假后的第一个交易日必须从「被误判停牌」改成正常交易，且重复跑不重复写。"""
    path = tmp_path / "ts.db"
    dbmod.init_db(path)
    monkeypatch.setattr(config, "DB_PATH", path)

    with dbmod.session() as conn:
        conn.executemany(
            "INSERT INTO daily_bar(symbol, date, close, tradestatus) VALUES (?,?,?,?)",
            [
                ("000001", "2026-09-30", 1.0, 1),
                ("000001", "2026-10-08", 1.0, 0),   # 旧口径：长假后被误判停牌
                ("000001", "2026-10-09", 1.0, 0),
            ],
        )
        conn.commit()

    assert cli.cmd_tradestatus(cli._ns(dry_run=True)) == 0
    assert "0→1 2 行，1→0 0 行" in capsys.readouterr().out
    assert _flags(path) == [1, 0, 0], "--dry-run 不能写库"

    assert cli.cmd_tradestatus(cli._ns(dry_run=False)) == 0
    assert "已写回 2 行" in capsys.readouterr().out
    assert _flags(path) == [1, 1, 1]

    # 幂等：再跑一次什么都不改
    assert cli.cmd_tradestatus(cli._ns(dry_run=False)) == 0
    assert "0→1 0 行，1→0 0 行" in capsys.readouterr().out


# ── price-limits --fill ─────────────────────────────────────────────
DATES = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]


def _seed_bars_with_limits(path, covered: str = "2026-01-08"):
    with dbmod.session() as conn:
        conn.executemany(
            "INSERT INTO daily_bar(symbol, date, close) VALUES ('000001',?,1.0)",
            [(d,) for d in DATES],
        )
        conn.execute(
            "INSERT INTO price_limit_daily(date, symbol, limit_ratio) VALUES (?,?,?)",
            (covered, "000001", 10.0),
        )
        conn.commit()


def test_missing_limit_dates_descending(tmp_path, monkeypatch):
    """只返回未覆盖的交易日，倒序（先补最近的，覆盖区间连续）。"""
    path = tmp_path / "limits.db"
    dbmod.init_db(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    _seed_bars_with_limits(path)

    with dbmod.session() as conn:
        assert cli.missing_limit_dates(conn) == ["2026-01-07", "2026-01-06", "2026-01-05"]
        assert cli.missing_limit_dates(conn, limit=2) == ["2026-01-07", "2026-01-06"]


def test_cmd_price_limits_fill_calls_sync_per_date(tmp_path, monkeypatch, capsys):
    path = tmp_path / "limits2.db"
    dbmod.init_db(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    _seed_bars_with_limits(path)

    called: list[str] = []

    def fake_sync(date=None):
        called.append(date)
        return 100

    monkeypatch.setattr(cli.limit_src, "sync_price_limits", fake_sync)
    assert cli.cmd_price_limits(cli._ns(fill=True)) == 0
    assert called == ["2026-01-07", "2026-01-06", "2026-01-05"], "必须倒序且跳过已覆盖日期"
    out = capsys.readouterr().out
    assert "本轮补 3 天 / 300 行" in out and "仍未覆盖 3 天" in out


def test_cmd_price_limits_fill_respects_time_budget(tmp_path, monkeypatch, capsys):
    """时间预算到点就停，剩下的留给下一次续采（否则一轮 8 小时没法中断）。

    用假时钟推进：Windows 的 monotonic() 只有 ~15ms 分辨率，靠真实耗时测不出来。
    """
    path = tmp_path / "limits3.db"
    dbmod.init_db(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    _seed_bars_with_limits(path)

    called: list[str] = []
    monkeypatch.setattr(
        cli.limit_src, "sync_price_limits",
        lambda date=None: (called.append(date), 100)[1],
    )
    clock = {"t": 0.0}

    def tick():
        clock["t"] += 60.0          # 每次读表前进 1 分钟
        return clock["t"]

    monkeypatch.setattr(cli.time, "monotonic", tick)
    assert cli.cmd_price_limits(cli._ns(fill=True, max_minutes=0.5)) == 0
    assert called == ["2026-01-07"], "预算已用尽，第二轮不该再采"
    assert "可再跑一次续采" in capsys.readouterr().out


def test_cmd_price_limits_fill_conflicts_with_days(tmp_path, monkeypatch, capsys):
    path = tmp_path / "limits4.db"
    dbmod.init_db(path)
    monkeypatch.setattr(config, "DB_PATH", path)

    called: list[str] = []
    monkeypatch.setattr(
        cli.limit_src, "sync_price_limits",
        lambda date=None: (called.append(date), 100)[1],
    )
    assert cli.cmd_price_limits(cli._ns(fill=True, days=5)) == 2
    assert called == [], "参数冲突时不能联网采集"
    assert "--fill 与 --days 不能同时使用" in capsys.readouterr().err
