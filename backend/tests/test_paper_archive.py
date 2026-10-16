"""模拟盘曲线归档：跑第二种策略前把第一种存下来（策略对照）。

守的是这个场景：`paper_account.id CHECK (id=1)` 决定了一个库只能有一条活动曲线，
换策略必须 `--reset` 清掉旧的 —— 没有归档就只能靠截图，README 里那份
「RPS vs 合成分」的对照正是手写进去的。
"""

from __future__ import annotations

import pytest
from rpsboard import db as dbmod
from rpsboard.broker import sim

from .test_paper_resume import KW


def _run(conn) -> None:
    ds = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM rps_daily ORDER BY date")]
    sim.run(conn, from_date=ds[20], to_date=ds[140], **KW)


def test_archive_requires_a_curve(market_db):
    """还没跑过模拟盘时不能归档（否则会存一条空曲线，看起来像"跑出来是 0%"）。"""
    with dbmod.session(market_db) as conn:
        with pytest.raises(sim.SimError, match="无可归档"):
            sim.archive(conn)


def test_archive_round_trip(market_db):
    """归档 → 列表 → 删除；归档的指标必须与当时的复盘一致。"""
    with dbmod.session(market_db) as conn:
        _run(conn)
        before = sim.review(conn)["summary"]
        cur = sim.summary(conn)

        res = sim.archive(conn, "RPS60 基准")
        rows = sim.list_archives(conn)

    assert res["id"] >= 1 and res["days"] == cur["days"]
    assert [r["label"] for r in rows] == ["RPS60 基准"]
    row = rows[0]
    assert row["days"] == cur["days"]
    assert row["total_return"] == pytest.approx(cur["return"], abs=1e-6)
    assert row["max_drawdown"] == pytest.approx(cur["max_drawdown"], abs=1e-6)
    assert len(row["curve"]) == cur["days"], "曲线要原样存下来（对照页要画）"
    assert row["params"].get("signal") == "rps", "参数要跟着曲线一起存"
    assert row["metrics"]["round_trips"] == before["round_trips"]
    assert row["metrics"]["fees_total"] == pytest.approx(before["fees_total"], abs=0.01)

    with dbmod.session(market_db) as conn:
        assert sim.delete_archive(conn, row["id"]) is True
        assert sim.delete_archive(conn, row["id"]) is False, "重复删除返回 False"
        assert sim.list_archives(conn) == []


def test_archive_survives_reset(market_db):
    """**核心价值**：reset 换策略之后，旧曲线仍能从归档里查到。"""
    with dbmod.session(market_db) as conn:
        _run(conn)
        a = sim.archive(conn, "策略A")
        assert sim.list_archives(conn) and len(sim.list_archives(conn)) == 1

        sim.reset_account(conn)
        assert sim.last_settled_date(conn) is None, "reset 会清掉活动曲线"

        rows = sim.list_archives(conn)
        assert len(rows) == 1 and rows[0]["id"] == a["id"], "归档必须不受 reset 影响"
        assert rows[0]["curve"], "归档里的曲线仍可用"
        assert rows[0]["params"].get("signal") == "rps"


def test_archive_label_falls_back_to_signal_and_time(market_db):
    """不给名字时按信号源 + 时间生成，避免一堆同名归档分不清。"""
    with dbmod.session(market_db) as conn:
        _run(conn)
        res = sim.archive(conn)
        rows = sim.list_archives(conn)
    assert res["label"].startswith("rps-")
    assert rows[0]["label"] == res["label"]


def test_default_archive_label_uses_custom_signal(market_db):
    """换了信号源时，自动名要能区分（对照正是为了比较不同信号）。"""
    with dbmod.session(market_db) as conn:
        ds = [r["date"] for r in conn.execute(
            "SELECT DISTINCT date FROM rps_daily ORDER BY date")]
        sim.run(conn, from_date=ds[20], to_date=ds[140], **{**KW, "signal": "composite"})
        res = sim.archive(conn)
    assert res["label"].startswith("composite-"), res["label"]
