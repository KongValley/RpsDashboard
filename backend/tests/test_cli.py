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
