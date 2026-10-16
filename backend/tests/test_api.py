"""API 契约测试：用临时 SQLite + 合成数据，不触网。"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from rpsboard import config
from rpsboard import db as dbmod
from rpsboard.analytics import rps as rps_engine
from rpsboard.analytics.matrix import (
    build_close_matrix,
    build_st_flags,
    build_tradable_matrix,
)

from .fixtures import make_industry_map_zh, make_long

PERIODS = config.PERIODS
PERIODS_DEFAULT = config.DEFAULT_PERIOD


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    """建一个装好合成数据的临时库，并把 config.DB_PATH 指过去。"""
    path = tmp_path / "api.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(dbmod.SCHEMA)

    df = make_long(n_symbols=40, n_days=320, seed=101)
    df[["symbol", "date", "close", "pct_chg", "tradestatus", "is_st"]].assign(
        volume=1_000_000.0, amount=1e8, open=1.0, high=1.0, low=1.0
    )[["symbol", "date", "open", "high", "low", "close", "volume", "amount",
       "pct_chg", "is_st", "tradestatus"]].to_sql(
        "daily_bar", conn, if_exists="append", index=False)

    symbols = sorted(df["symbol"].unique())
    pd.DataFrame({
        "symbol": symbols,
        "name": [f"测试{i}" for i in range(len(symbols))],
        "exchange": ["sz"] * len(symbols),
        "ipo_date": ["20200101"] * len(symbols),
        "is_st": [0] * len(symbols),
        "updated_at": ["2026-01-01"] * len(symbols),
    }).to_sql("security_master", conn, if_exists="append", index=False)

    ind = make_industry_map_zh(symbols, 5)
    ind.rename("industry").reset_index().rename(columns={"index": "symbol"}).assign(
        industry_cls="证监会", updated_at="2026-01-01"
    ).to_sql("industry_map", conn, if_exists="append", index=False)

    close = build_close_matrix(df)
    rps_engine.materialize(
        conn, close, build_tradable_matrix(df), build_st_flags(df), ind, None
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", path)
    import rpsboard.api as apimod
    monkeypatch.setattr(apimod, "_cache", {"conn": None, "opened": None})

    from rpsboard.api import create_app
    return TestClient(create_app()), df, ind


# ── meta ────────────────────────────────────────────────────────────
def test_status_ok(seeded):
    client, *_ = seeded
    r = client.get("/api/meta/status")
    assert r.status_code == 200
    body = r.json()
    assert body["db_ready"] is True
    assert body["bar_rows"] > 0
    assert body["rps_rows"] > 0
    assert body["last_bar_date"]
    assert body["periods"] == list(PERIODS)
    assert body["source"] == config.DATA_SOURCE
    # 快照相关字段已随 akshare 数据源一起移除（量比/市值在本机根本拿不到）
    assert "spot_stale" not in body
    assert "last_spot_date" not in body


def test_periods(seeded):
    client, *_ = seeded
    r = client.get("/api/meta/periods")
    assert r.status_code == 200 and r.json() == list(PERIODS)


def test_industries(seeded):
    client, *_ = seeded
    r = client.get("/api/meta/industries")
    assert r.status_code == 200
    assert len(r.json()) == 5


def test_dates_descending(seeded):
    client, *_ = seeded
    ds = client.get("/api/meta/dates").json()
    assert ds == sorted(ds, reverse=True)


# ── ranking ─────────────────────────────────────────────────────────
def test_ranking_basic(seeded):
    client, *_ = seeded
    r = client.get("/api/rps/ranking", params={"period": 60, "rps_min": 80})
    assert r.status_code == 200
    body = r.json()
    assert body["rows"], "应至少返回一些高 RPS 股票"
    assert body["period"] == 60
    row = body["rows"][0]
    for k in ("symbol", "name", "rps", "rank_overall"):
        assert k in row
    assert all(x["rps"] >= 80 for x in body["rows"])


def test_ranking_top_is_100(seeded):
    client, *_ = seeded
    rows = client.get("/api/rps/ranking", params={"period": 60, "limit": 5}).json()["rows"]
    assert rows[0]["rps"] == pytest.approx(100.0, abs=0.01)


def test_ranking_pagination(seeded):
    client, *_ = seeded
    p1 = client.get("/api/rps/ranking", params={"limit": 10, "page": 1}).json()
    p2 = client.get("/api/rps/ranking", params={"limit": 10, "page": 2}).json()
    assert p1["total"] == p2["total"]
    assert {x["symbol"] for x in p1["rows"]}.isdisjoint(
        {x["symbol"] for x in p2["rows"]})


def test_ranking_industry_filter(seeded):
    client, _, ind = seeded
    industry = sorted(ind.unique())[0]
    rows = client.get("/api/rps/ranking",
                      params={"industries": industry}).json()["rows"]
    assert rows
    assert {x["industry"] for x in rows} == {industry}


def test_ranking_idempotent(seeded):
    """同一请求两次结果必须完全一致。"""
    client, *_ = seeded
    a = client.get("/api/rps/ranking", params={"period": 60}).json()
    b = client.get("/api/rps/ranking", params={"period": 60}).json()
    assert a["rows"] == b["rows"]


def test_ranking_sort_by_amount(seeded):
    client, *_ = seeded
    rows = client.get("/api/rps/ranking",
                      params={"sort": "amount", "limit": 20}).json()["rows"]
    vals = [x["amount"] for x in rows if x["amount"] is not None]
    assert vals == sorted(vals, reverse=True)


def test_ranking_shortest_period_has_values(seeded):
    """新增的 15 日周期必须真的能用：接口接受 period=15 且名次有值。"""
    client, *_ = seeded
    body = client.get("/api/rps/ranking", params={"period": 15, "limit": 5}).json()
    assert body["period"] == 15
    assert body["total"] > 0
    assert body["rows"][0]["rps"] == pytest.approx(100.0, abs=0.01)


def test_ranking_invalid_period_falls_back(seeded):
    client, *_ = seeded
    r = client.get("/api/rps/ranking", params={"period": 999})
    assert r.status_code == 200
    assert r.json()["period"] == config.DEFAULT_PERIOD


# ── sector / heatmap ────────────────────────────────────────────────
def test_sector(seeded):
    client, *_ = seeded
    body = client.get("/api/rps/sector", params={"period": 60}).json()
    assert body["rows"]
    rps_vals = [x["rps"] for x in body["rows"] if x["rps"] is not None]
    assert rps_vals == sorted(rps_vals, reverse=True)


def test_heatmap_shape(seeded):
    client, *_ = seeded
    body = client.get("/api/rps/heatmap").json()
    assert len(body["industries"]) == 5
    assert body["periods"] == list(PERIODS)
    assert len(body["matrix"]) == 5


# ── stock ───────────────────────────────────────────────────────────
def test_stock_detail(seeded):
    client, df, _ = seeded
    sym = df["symbol"].iloc[0]
    body = client.get(f"/api/stock/{sym}").json()
    assert body["info"]["symbol"] == sym
    assert body["info"]["industry"], "应带出所属行业"
    assert set(body["market_percentile"]) == {f"rps{p}" for p in PERIODS}
    # 各周期在全市场都有排名分位
    assert any(v is not None for v in body["market_percentile"].values())


def test_stock_curve_aligned(seeded):
    client, df, _ = seeded
    sym = df["symbol"].iloc[0]
    body = client.get(f"/api/stock/{sym}/curve", params={"days": 100}).json()
    n = len(body["dates"])
    assert n > 0
    for key in ("close", "rps20", "rps60", "rps120", "rps250"):
        assert len(body[key]) == n, f"{key} 长度与日期轴不一致"


# ── 当日复盘 ────────────────────────────────────────────────────────
def test_review_day_breadth_limits_and_signals(seeded):
    """当日复盘：宽度自洽、涨跌停清单带连板、行业排序、信号名单。"""
    client, df, _ = seeded
    conn = dbmod.connect()
    try:
        dates = [r["date"] for r in conn.execute(
            "SELECT DISTINCT date FROM rps_daily ORDER BY date")]
        day, prev = dates[-1], dates[-2]
        hot, cold = sorted(df["symbol"].unique())[:2]
        for d in (prev, day):        # hot 连续两天涨停 → 连板 2
            conn.execute(
                "INSERT OR REPLACE INTO price_limit_daily(date, symbol, limit_ratio,"
                " rule, status) VALUES (?,?,10.0,'main','normal')", (d, hot))
            conn.execute("UPDATE daily_bar SET pct_chg = 0.10"
                         " WHERE symbol = ? AND date = ?", (hot, d))
        conn.execute(          # cold 当日跌停
            "INSERT OR REPLACE INTO price_limit_daily(date, symbol, limit_ratio,"
            " rule, status) VALUES (?,?,10.0,'main','normal')", (day, cold))
        conn.execute("UPDATE daily_bar SET pct_chg = -0.10"
                     " WHERE symbol = ? AND date = ?", (cold, day))
        conn.commit()
    finally:
        conn.close()

    body = client.get("/api/review/day",
                      params={"date": day, "threshold": 0}).json()
    assert body["date"] == day
    b = body["breadth"]
    assert b["traded"] == b["up"] + b["down"] + b["flat"] > 0
    assert b["limit_up"] == len(body["limit_up"]) == 1
    assert b["limit_down"] == len(body["limit_down"]) == 1

    up = body["limit_up"][0]
    assert up["symbol"] == hot and up["streak"] == 2, up
    assert up["limit_ratio"] == 10.0 and up["name"]
    assert body["limit_down"][0]["symbol"] == cold

    assert body["sectors"], "行业当日涨跌不该为空"
    avg = [s["avg_pct"] for s in body["sectors"]]
    assert avg == sorted(avg, reverse=True)
    assert all(s["up"] <= s["n"] for s in body["sectors"])

    assert body["signals"], "threshold=0 时应有信号名单"
    assert {"symbol", "rps", "period_return"} <= set(body["signals"][0])
    assert "共振" in body["signals_note"], "必须写明不含共振/择时"

    # 非交易日就近回退，并回显生效日
    fallback = client.get("/api/review/day", params={"date": "2030-01-01"}).json()
    assert fallback["date"] == day


def test_review_day_on_empty_db(monkeypatch):
    """空库不 500：返回空结构并给出提示。"""
    import rpsboard.api as apimod
    from rpsboard.api import create_app, perf

    def fresh():
        c = sqlite3.connect(":memory:", check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(dbmod, "connect", fresh)
    monkeypatch.setattr(perf, "_append_jsonl", lambda entry: None)
    monkeypatch.setattr(apimod, "_cache", {"conn": None, "opened": None})
    body = TestClient(create_app()).get("/api/review/day").json()
    assert body["date"] is None and body["breadth"] is None
    assert body["limit_up"] == [] and body["message"]


# ── 已保存的筛选条件（排行榜看板）──────────────────────────────────
def test_saved_screens_roundtrip_and_validation(seeded):
    """保存/同名覆盖/删除，以及**白名单 + 夹取**（存进去的东西会直接拼进查询）。"""
    client, *_ = seeded
    assert client.get("/api/screens").json()["rows"] == []

    created = client.post("/api/screens", json={
        "name": "强势涨幅榜",
        "params": {"period": 60, "rps_min": 90, "sort": "pct_chg", "direction": "ASC",
                   "industries": "银行,,软件", "exclude_st": True},
    }).json()
    assert created["ok"] is True and created["id"] >= 1

    row = client.get("/api/screens").json()["rows"][0]
    p = row["params"]
    assert p["rps_min"] == 90.0 and p["direction"] == "asc", "方向归一化成小写"
    assert p["industries"] == "银行,软件", "行业名要去掉空项"

    # 同名再存 = 覆盖（不攒同名条目）
    again = client.post("/api/screens", json={
        "name": "强势涨幅榜", "params": {"period": 20}}).json()
    assert again["id"] == created["id"]
    rows = client.get("/api/screens").json()["rows"]
    assert len(rows) == 1 and rows[0]["params"] == {"period": 20}

    # 未知键拒绝（422），非法值回落
    assert client.post("/api/screens", json={
        "name": "坏", "params": {"nope": 1}}).status_code == 422
    assert client.post("/api/screens", json={
        "name": "  ", "params": {}}).status_code == 422

    fallback = client.post("/api/screens", json={
        "name": "回落", "params": {"period": 999, "sort": "nope", "sanxingdi": 7,
                                   "rps_min": -5, "direction": "sideways"}}).json()
    got = {r["id"]: r["params"] for r in client.get("/api/screens").json()["rows"]}
    fp = got[fallback["id"]]
    assert fp["period"] == PERIODS_DEFAULT and fp["sort"] == "rps"
    assert fp["sanxingdi"] == 0 and fp["rps_min"] == 0.0 and fp["direction"] == "desc"
    # 分页参数刻意不保存：看板存的是筛选口径，不是"第几页"
    assert "page" not in fp and "limit" not in fp

    assert client.delete(f"/api/screens/{fallback['id']}").json()["ok"] is True
    assert client.delete("/api/screens/999").json()["ok"] is False


def test_backtest_save_and_reload(seeded):
    """回测存库：列表不带 payload、详情能载入完整结果、删除可用。"""
    client, *_ = seeded
    params = {"period": 20, "threshold": 80, "hold": 20}
    saved = client.post("/api/backtest/save", params=params).json()
    assert saved["ok"] is True and saved["id"] >= 1
    assert "RPS20" in saved["label"] and "计成本" in saved["label"]

    rows = client.get("/api/backtest/saved").json()["rows"]
    assert [r["id"] for r in rows] == [saved["id"]]
    assert "payload" not in rows[0], "列表不该带 payload（对比表只需参数与指标）"
    assert rows[0]["metrics"].get("total_return") is not None
    assert rows[0]["params"]["period"] == 20

    detail = client.get(f"/api/backtest/saved/{saved['id']}").json()
    assert detail["ok"] is True
    assert detail["result"]["saved"]["id"] == saved["id"]
    assert detail["result"]["equity_curve"] and detail["result"]["metrics"]

    # 与实时跑一次同参数的结果一致（同一口径，不是另算一套）
    live = client.get("/api/backtest/run", params=params).json()
    assert detail["result"]["metrics"]["total_return"] == live["metrics"]["total_return"]

    assert client.delete(f"/api/backtest/saved/{saved['id']}").json()["ok"] is True
    assert client.get("/api/backtest/saved").json()["rows"] == []
    assert client.delete("/api/backtest/saved/999").json()["ok"] is False
    assert client.get("/api/backtest/saved/999").json()["ok"] is False


def test_backtest_auto_label_covers_variants(seeded):
    """自动名要把关键参数写进去，否则列表里一堆「RPS20」分不清。"""
    client, *_ = seeded
    a = client.post("/api/backtest/save", params={
        "period": 20, "threshold": 0, "hold": 20, "select_top": 5}).json()
    b = client.post("/api/backtest/save", params={
        "period": 60, "threshold": 90, "hold": 10, "market_filter": "ma200",
        "use_cost": False}).json()
    rows = {r["id"]: r["label"] for r in client.get("/api/backtest/saved").json()["rows"]}
    assert "Top5" in rows[a["id"]]
    assert "RPS60" in rows[b["id"]] and "零成本" in rows[b["id"]]
    assert "200 日均线" in rows[b["id"]], rows[b["id"]]


def test_stock_detail_and_curve_follow_date(seeded):
    """`?date=` 让个股详情与曲线锚在历史某天（非交易日就近回退）。"""
    client, df, _ = seeded
    sym = df["symbol"].iloc[0]
    with dbmod.connect() as conn:
        dates = [r["date"] for r in conn.execute(
            "SELECT DISTINCT date FROM rps_daily WHERE symbol = ? ORDER BY date",
            (sym,))]
    assert len(dates) > 20
    mid = dates[len(dates) // 2]

    body = client.get(f"/api/stock/{sym}", params={"date": mid}).json()
    assert body["current"]["date"] == mid, "详情必须锚在指定日"
    # 分位与行业都按那一天算（不是最新一天）
    assert set(body["market_percentile"]) == {f"rps{p}" for p in PERIODS}

    curve = client.get(f"/api/stock/{sym}/curve",
                       params={"days": 50, "date": mid}).json()
    assert curve["dates"][-1] == mid, "曲线右端必须是指定日"
    assert len(curve["dates"]) <= 50 and all(d <= mid for d in curve["dates"])

    # 早于该股第一根 RPS → 就近回退到第一天（不是换成最新一天）
    early = client.get(f"/api/stock/{sym}", params={"date": "1990-01-01"}).json()
    assert early["current"]["date"] == dates[0]


def test_stock_patterns_do_not_look_ahead_when_reviewing(seeded):
    """回看历史形态时不许看到未来：`min_low_after` 必须只算到那一天为止。"""
    client, df, _ = seeded
    sym = df["symbol"].iloc[0]
    with dbmod.connect() as conn:
        dates = [r["date"] for r in conn.execute(
            "SELECT DISTINCT date FROM rps_daily WHERE symbol = ? ORDER BY date",
            (sym,))]
        d0, d_last = dates[5], dates[-1]
        conn.execute("DELETE FROM pattern_daily WHERE symbol = ?", (sym,))
        conn.execute(
            "INSERT INTO pattern_daily(symbol, date, base_date, base_low, base_high,"
            " strength, body_ratio) VALUES (?,?,?,?,?,?,?)",
            (sym, d0, d0, 5.0, 6.0, 0.1, 0.4))
        # 形态之后某天的最低价远低于 base_low：看今天已失效
        conn.execute("UPDATE daily_bar SET low = 1.0 WHERE symbol = ? AND date = ?",
                     (sym, d_last))
        conn.commit()

    now = client.get(f"/api/stock/{sym}/patterns").json()
    assert now["patterns"][0]["active"] is False, "按最新数据应判为失效"

    past = client.get(f"/api/stock/{sym}/patterns", params={"date": d0}).json()
    assert past["patterns"][0]["active"] is True, \
        "回看 d0 当天不许用未来的低点判失效"
    assert past["patterns"][0]["min_low_after"] is None, \
        "回看当天「此后最低」应为空，而不是未来低点"


def test_stock_404(seeded):
    client, *_ = seeded
    assert client.get("/api/stock/999999").status_code == 404


# ── backtest ────────────────────────────────────────────────────────
def test_backtest_run(seeded):
    client, *_ = seeded
    r = client.get("/api/backtest/run",
                   params={"period": 20, "threshold": 80, "hold": 20})
    assert r.status_code == 200
    body = r.json()
    assert "warnings" in body and body["warnings"], "必须携带幸存者偏差警示"
    assert len(body["buckets"]) == 5
    assert "metrics" in body


def test_backtest_param_clamping(seeded):
    client, *_ = seeded
    body = client.get("/api/backtest/run",
                      params={"threshold": 9999, "hold": 9999}).json()
    assert body["params"]["threshold"] == 100.0
    assert body["params"]["hold"] == 250


def test_backtest_warns_on_missing_benchmark_and_limits(seeded):
    """基准与涨跌停缺失都必须在 warnings 里说出来。

    合成库既没有 benchmark_bar 也没有 price_limit_daily：
    - 基准缺失原来会静默产出空基准线（用户只看到「基准没画出来」）
    - 涨跌停缺失原来只在一行为都没有时才提，部分覆盖则完全沉默
    """
    client, *_ = seeded
    body = client.get("/api/backtest/run",
                      params={"period": 20, "threshold": 80, "hold": 20}).json()
    text = " ".join(body["warnings"])
    assert "基准指数" in text
    assert "price_limit_daily" in text or "涨跌停数据仅覆盖" in text


def test_admin_update_rejects_unknown(seeded):
    """只允许触发 update 这一个白名单任务，不接受任意命令执行。"""
    client, *_ = seeded
    body = client.post("/api/admin/update", json={"task": "rm -rf"}).json()
    assert body["ok"] is False
    assert "不支持的任务" in body["error"]
    # 旧的同步接口必须已经不存在（现在只匹配 SPA 兜底路由 → 405/404），
    # 避免有人继续走那条会阻塞服务的路径
    assert client.post(
        "/api/admin/collect", json={"task": "compute-rps"}
    ).status_code in (404, 405)


def test_admin_update_status_shape(seeded):
    """轮询接口必须给出运行状态与阶段，即使当前没有任务在跑。"""
    client, *_ = seeded
    r = client.get("/api/admin/update/status")
    assert r.status_code == 200
    body = r.json()
    for k in ("running", "phase", "error", "started_at", "finished_at"):
        assert k in body
    assert isinstance(body["running"], bool)


# ── 降级：空库 ──────────────────────────────────────────────────────
def test_empty_db_graceful(tmp_path, monkeypatch):
    """数据库为空时接口应返回可解释的空结果，而不是 500。"""
    path = tmp_path / "empty.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(dbmod.SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    import rpsboard.api as apimod
    monkeypatch.setattr(apimod, "_cache", {"conn": None, "opened": None})
    from rpsboard.api import create_app

    client = TestClient(create_app())
    st = client.get("/api/meta/status").json()
    assert st["db_ready"] is False

    rank = client.get("/api/rps/ranking").json()
    assert rank["rows"] == [] and rank["total"] == 0

    assert client.get("/api/rps/sector").status_code == 200
    assert client.get("/api/rps/heatmap").status_code == 200
    assert client.get("/api/meta/industries").json() == []
    assert client.get("/api/meta/dates").json() == []


def test_ranking_core_columns_present(seeded):
    """排行榜必须返回核心列。"""
    client, *_ = seeded
    rows = client.get("/api/rps/ranking").json()["rows"]
    assert rows
    for k in ("symbol", "name", "industry", "rps", "period_return",
              "pct_chg", "amount", "close", "rank_overall"):
        assert k in rows[0], f"缺少核心列 {k}"
    assert all(x["amount"] is not None for x in rows), "成交额应来自 daily_bar"


def test_ranking_dropped_columns_absent(seeded):
    """换手率与快照四列必须**不存在**，而不是恒为 None。

    这几个字段在本机都没有可靠来源（实测 daily_bar.turn 非空行数为 0；
    akshare 快照被网络阻断）。留一列恒为 `—` 的假数据只会误导用户，
    所以直接从接口移除——这个测试防止它们被悄悄加回来。
    """
    client, *_ = seeded
    row = client.get("/api/rps/ranking").json()["rows"][0]
    for k in ("turnover_rate", "volume_ratio", "total_mv", "float_mv", "price"):
        assert k not in row, f"{k} 已确认无数据来源，不应再出现在响应里"


def test_ranking_limit_is_capped(seeded):
    """limit 必须被夹到上限，不能一次性把全市场拉出来。"""
    from rpsboard.api.rps import MAX_PAGE_LIMIT

    client, *_ = seeded
    body = client.get("/api/rps/ranking", params={"limit": 100000}).json()
    assert body["limit"] == MAX_PAGE_LIMIT
    assert len(body["rows"]) <= MAX_PAGE_LIMIT


def test_ranking_export_full_result(seeded):
    """导出必须覆盖**全量**筛选结果，而不是当前页。"""
    client, *_ = seeded
    r = client.get("/api/rps/ranking/export")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    body = r.content.decode("utf-8-sig")
    lines = [ln for ln in body.splitlines() if ln.strip()]
    total = client.get("/api/rps/ranking", params={"limit": 500}).json()["total"]
    # 表头 1 行 + 数据行；导出的是全量，行数应等于 total
    assert len(lines) - 1 == total, f"导出 {len(lines)-1} 行，应为全量 {total} 行"


# ── 排序分页选行（2026-10-14 修复）───────────────────────────────────
@pytest.mark.parametrize("sort,fkey,desc", [
    ("amount", "amount", True),
    ("close", "close", False),
    ("pct_chg", "pct_chg", False),
])
def test_ranking_join_sort_picks_global_top(seeded, sort, fkey, desc):
    """JOIN 列排序时选中的必须是**全市场**前 N 名（页面与 CSV 不再互相矛盾）。

    旧实现让内层子查询固定带 `LIMIT ? OFFSET ?`：sort=amount 时内层按索引
    扫描顺序切片，实测选中集合 MAX(rps15)=3.75（全市场倒数 3.75%），
    而 CSV 导出走的是另一条路径 —— 同一筛选条件下两边结果对不上。
    """
    client, *_ = seeded
    conn = dbmod.connect()
    try:
        last = conn.execute("SELECT MAX(date) AS d FROM rps_daily").fetchone()["d"]
        ref = pd.read_sql_query(
            f"SELECT b.symbol FROM daily_bar b "
            f"JOIN rps_daily r ON r.symbol = b.symbol AND r.date = b.date "
            f"WHERE b.date = ? AND r.rps60 IS NOT NULL AND b.{fkey} IS NOT NULL "
            f"ORDER BY b.{fkey} {'DESC' if desc else 'ASC'}, b.symbol ASC LIMIT 15",
            conn, params=(last,),
        )
    finally:
        conn.close()
    rows = client.get("/api/rps/ranking",
                      params={"sort": sort, "limit": 15}).json()["rows"]
    assert [x["symbol"] for x in rows] == ref["symbol"].tolist()


def test_ranking_join_sort_matches_export(seeded):
    """同一筛选条件下，页面第 1 页与 CSV 导出必须一致（修前两者矛盾）。"""
    client, *_ = seeded
    params = {"sort": "amount", "limit": 10, "period": 60}
    page = client.get("/api/rps/ranking", params=params).json()["rows"]
    text = client.get("/api/rps/ranking/export",
                      params={"sort": "amount", "period": 60}).content.decode("utf-8-sig")
    exported = [r[0] for r in list(csv.reader(io.StringIO(text)))[1:]]
    assert [f'="{x["symbol"]}"' for x in page] == exported[:10]


# ── 分位 / NULL / 空库 ──────────────────────────────────────────────
def test_stock_market_percentile_top_is_100(seeded):
    """全市场第一名「跑赢全市场」必须是 100%，不是 0%。"""
    client, *_ = seeded
    conn = dbmod.connect()
    try:
        row = conn.execute(
            "SELECT symbol FROM rps_daily "
            "WHERE date = (SELECT MAX(date) FROM rps_daily) "
            "ORDER BY rps60 DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    body = client.get(f"/api/stock/{row['symbol']}").json()
    assert body["market_percentile"]["rps60"] == pytest.approx(100.0)


def test_stock_null_period_returns_200(seeded):
    """某周期为 NULL（新股/缺口）时必须 200 且该字段 null。

    实测 157 行周期值是 NULL（688796 的 rps250），NaN 交给 Starlette 的
    allow_nan=False 会直接 500。
    """
    client, df, _ = seeded
    sym = df["symbol"].iloc[0]
    conn = dbmod.connect()
    try:
        conn.execute("UPDATE rps_daily SET rps250 = NULL WHERE symbol = ?", (sym,))
        conn.commit()
    finally:
        conn.close()

    r = client.get(f"/api/stock/{sym}")
    assert r.status_code == 200
    assert r.json()["current"]["rps250"] is None

    c = client.get(f"/api/stock/{sym}/curve", params={"days": 60})
    assert c.status_code == 200
    assert all(v is None for v in c.json()["rps250"])


def test_ranking_reports_empty_when_rps_missing(seeded):
    """有日K但没算 RPS 时必须明说「数据库为空」，不能静默返回空表。"""
    client, *_ = seeded
    conn = dbmod.connect()
    try:
        conn.execute("DELETE FROM rps_daily")
        conn.commit()
    finally:
        conn.close()
    body = client.get("/api/rps/ranking").json()
    assert body["data_date"] is None
    assert body["rows"] == [] and body["total"] == 0
    assert "compute-rps" in body.get("message", "")


def test_sanxingdi_active_ignores_future_bars(seeded):
    """截至查询日未跌破 base_low 的形态，不能因为之后的下跌被标成失效。

    实测 2026-06-24~30 窗口 364 条命中里 196 条（54%）因为「有效性」用到了
    查询日之后的 K 线而被标成失效。
    """
    client, df, _ = seeded
    sym = df["symbol"].iloc[0]
    conn = dbmod.connect()
    try:
        dates = [r["date"] for r in conn.execute(
            "SELECT DISTINCT date FROM daily_bar WHERE symbol = ? ORDER BY date",
            (sym,))]
        d = dates[-6]        # 查询日（历史）
        base = dates[-8]     # 形态确认日，落在「截至 d 的 5 个交易日」内
        conn.execute(
            "UPDATE daily_bar SET low = CASE WHEN date <= ? THEN 99.0 "
            "  WHEN date <= ? THEN 50.0 ELSE 1.0 END WHERE symbol = ?",
            (base, d, sym))
        conn.execute(
            "INSERT INTO pattern_daily(date, symbol, base_date, base_low, base_high, "
            "strength, body_ratio) VALUES (?,?,?,50.0,60.0,0.2,0.1)",
            (base, sym, base))
        conn.commit()
    finally:
        conn.close()

    rows = client.get("/api/rps/ranking", params={
        "date": d, "sanxingdi": 5, "rps_min": 0, "limit": 500}).json()["rows"]
    hit = [x for x in rows if x["symbol"] == sym]
    assert hit, f"形态窗口内应筛出 {sym}"
    assert hit[0]["sanxingdi_date"] == base
    assert hit[0]["sanxingdi_active"] is True, "不得用查询日之后的 K 线判失效"


# ── CSV 导出 ────────────────────────────────────────────────────────
def test_export_csv_parses_and_guards_formulas(seeded):
    """导出必须是可解析的 CSV、行数与 total 一致，且防公式注入。"""
    client, df, _ = seeded
    conn = dbmod.connect()
    try:
        conn.execute("UPDATE security_master SET name = '=1+1' WHERE symbol = ?",
                     (df["symbol"].iloc[0],))
        conn.commit()
    finally:
        conn.close()

    total = client.get("/api/rps/ranking", params={"period": 60}).json()["total"]
    r = client.get("/api/rps/ranking/export", params={"period": 60})
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0] == ["代码", "名称", "行业", "RPS", "区间涨幅%", "当日涨跌%",
                       "成交额", "全市场名次"]
    assert len(rows) == total + 1
    # 代码列包公式：Excel 打开时前导零不会丢
    assert all(c.startswith('="') and c.endswith('"') for c in
               (row[0] for row in rows[1:]))
    assert any(row[1] == "'=1+1" for row in rows[1:]), "= 开头的名称必须加前缀"


# ── 更新线程的收尾与互斥（修：start_run 在线程里 + 不收尾）────────────
def test_admin_update_is_mutually_exclusive_and_closes_log(seeded, monkeypatch):
    """后台更新：第二次 POST 必须被拒；线程结束后 collect_log 必须收尾。

    修前两个问题叠在一起：锁只在启动瞬间持有、启动前也不看 running，
    第二次 POST 会起**第二个**采集线程；线程里 start_run 的记录永远不写
    finished_at，页面「最近更新」于是恒显示「进行中」。
    """
    client, *_ = seeded
    from rpsboard.api import backtest as bt_api

    release = threading.Event()

    def fake_pipeline(progress=None, workers=None):
        if progress:
            progress("bars")
        release.wait(timeout=5)
        return {"ok": True, "price_limit_rows": 7, "alert_hits": 2}

    monkeypatch.setattr(bt_api, "cli_run_update_pipeline", fake_pipeline)

    first = client.post("/api/admin/update", json={"task": "update"}).json()
    assert first["ok"] is True and first["run_id"], "响应必须带真实 run_id"
    second = client.post("/api/admin/update", json={"task": "update"}).json()
    assert second["ok"] is False and "已有更新任务" in second["error"]

    release.set()
    for _ in range(250):          # 有界等待，不用固定 sleep
        if not bt_api._update_state["running"]:
            break
        time.sleep(0.02)
    assert bt_api._update_state["running"] is False

    conn = dbmod.connect()
    try:
        row = conn.execute(
            "SELECT status, rows_affected FROM collect_log WHERE run_id = ?",
            (first["run_id"],)).fetchone()
    finally:
        conn.close()
    assert row is not None, "请求线程里就该写号 collect_log"
    assert row["status"] == "success" and row["rows_affected"] == 7


def test_cache_token_tracks_non_rps_updates(seeded):
    """回测缓存指纹必须覆盖 rps 之外的更新（只补涨跌停也能让缓存失效）。

    只用 rps_daily.MAX(date) 会漏掉「同日补采涨跌停 / 补基准 / 形态重算」，
    同参数请求会继续返回旧结论。
    """
    from rpsboard.api.backtest import _cache_token

    conn = dbmod.connect()
    try:
        before = _cache_token(conn)
        conn.execute("INSERT INTO price_limit_daily(date, symbol, limit_ratio) "
                     "VALUES ('2099-01-01','000001',10.0)")
        conn.commit()
        after = _cache_token(conn)
    finally:
        conn.close()
    assert before and after and before != after


# ── 排序方向（2026-10-14 新增控件）────────────────────────────────────
@pytest.mark.parametrize("sort,fkey", [
    ("rps", "rps"), ("ret", "period_return"), ("amount", "amount"),
    ("pct_chg", "pct_chg"), ("close", "close"),
])
def test_ranking_direction_is_honored(seeded, sort, fkey):
    """direction=asc/desc 必须真的生效，且分页与排序键同向。

    `sort=pct_chg` 的默认方向是**升序**（跌幅榜），所以控件必须能显式指定方向 ——
    否则用户点「涨跌幅」看到的会是跌得最多的那一批。
    """
    client, *_ = seeded
    desc = client.get("/api/rps/ranking",
                      params={"sort": sort, "direction": "desc", "limit": 500}).json()
    asc = client.get("/api/rps/ranking",
                     params={"sort": sort, "direction": "asc", "limit": 500}).json()
    assert desc["direction"] == "desc" and asc["direction"] == "asc"
    assert desc["total"] == asc["total"]
    assert {r["symbol"] for r in desc["rows"]} == {r["symbol"] for r in asc["rows"]}

    d_vals = [r[fkey] for r in desc["rows"] if r[fkey] is not None]
    a_vals = [r[fkey] for r in asc["rows"] if r[fkey] is not None]
    assert d_vals == sorted(d_vals, reverse=True), f"{sort} 降序不单调"
    assert a_vals == sorted(a_vals), f"{sort} 升序不单调"


def test_ranking_direction_edges_match_database(seeded):
    """升序首行/降序首行必须分别是全市场的最小/最大值（用取值唯一的 close 校验）。"""
    client, *_ = seeded
    conn = dbmod.connect()
    try:
        last = conn.execute("SELECT MAX(date) AS d FROM rps_daily").fetchone()["d"]
        row = conn.execute(
            "SELECT MIN(b.close) AS lo, MAX(b.close) AS hi FROM daily_bar b "
            "JOIN rps_daily r ON r.symbol = b.symbol AND r.date = b.date "
            "WHERE b.date = ? AND r.rps60 IS NOT NULL", (last,)).fetchone()
    finally:
        conn.close()
    asc = client.get("/api/rps/ranking",
                     params={"sort": "close", "direction": "asc", "limit": 5}).json()
    desc = client.get("/api/rps/ranking",
                      params={"sort": "close", "direction": "desc", "limit": 5}).json()
    assert asc["rows"][0]["close"] == pytest.approx(row["lo"])
    assert desc["rows"][0]["close"] == pytest.approx(row["hi"])


def test_ranking_direction_with_inner_paging(seeded):
    """rps/ret 走内层 LIMIT 快路径：方向必须一路传进子查询，翻页也不重不漏。"""
    client, *_ = seeded
    p1 = client.get("/api/rps/ranking",
                    params={"sort": "rps", "direction": "asc", "limit": 10, "page": 1}).json()
    p2 = client.get("/api/rps/ranking",
                    params={"sort": "rps", "direction": "asc", "limit": 10, "page": 2}).json()
    assert {r["symbol"] for r in p1["rows"]}.isdisjoint({r["symbol"] for r in p2["rows"]})
    joined = [r["rps"] for r in p1["rows"]] + [r["rps"] for r in p2["rows"]]
    assert joined == sorted(joined), "内层分页没有跟着方向走"


# ── QMT 交易接口（只读库，不连客户端）──────────────────────────────
@pytest.fixture()
def trade_client(seeded, tmp_path, monkeypatch):
    """交易端点专用：halt 哨兵指到临时目录，summary 缓存每个用例清一次。"""
    from rpsboard.api import trade as trade_api

    monkeypatch.setattr(config, "TRADE_HALT_FILE", tmp_path / "trade.halt")
    monkeypatch.setattr(config, "TRADE_MODE", "paper")
    monkeypatch.setattr(trade_api, "_summary_cache", {})
    return seeded[0]


def _seed_trade_rows(conn, plan_date="2026-01-01"):
    cur = conn.execute(
        "INSERT INTO trade_plan(plan_date, mode, params, cash_budget, status, note,"
        " created_at) VALUES (?,?,?,?,?,?,?)",
        (plan_date, "paper", '{"max_positions": 1}', 100000.0, "planned",
         "口径说明", dbmod.now()))
    pid = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO trade_plan_item(plan_id, symbol, target_volume, ref_price, plan_price,"
        " side, reason, held) VALUES (?,?,?,?,?,?,?,0)",
        (pid, "000001", 1000, 12.0, 12.1, "buy", "ok"))
    conn.execute(
        "INSERT INTO trade_order(plan_id, trade_date, symbol, side, volume, price, status,"
        " broker_order_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (pid, plan_date, "000001", "buy", 1000, 12.1, "submitted", 900001,
         dbmod.now(), dbmod.now()))
    conn.execute(
        "INSERT INTO trade_fill(trade_date, broker_order_id, broker_trade_id, symbol, side,"
        " volume, price, traded_at) VALUES (?,?,?,?,?,?,?,?)",
        (plan_date, 900001, 1, "000001", "buy", 1000, 12.1, dbmod.now()))
    conn.execute(
        "INSERT INTO position_snapshot(snap_date, taken_at, symbol, volume, can_use_volume,"
        " avg_price, market_value) VALUES (?,?,?,?,?,?,?)",
        (plan_date, dbmod.now(), "000001", 500, 500, 12.0, 6000.0))
    conn.commit()
    return pid


def test_trade_summary_shape_and_no_qmt_call(trade_client, monkeypatch):
    """summary 必须**只读库**：给 QMT 打桩，一旦被调用就让用例失败。"""
    import rpsboard.broker.qmt as qmt_mod

    def boom(*_a, **_k):
        raise AssertionError("summary 不该连 QMT")

    monkeypatch.setattr(qmt_mod, "status", boom)
    monkeypatch.setattr(qmt_mod, "positions", boom)
    client = trade_client
    with dbmod.connect() as conn:
        _seed_trade_rows(conn)
    s = client.get("/api/trade/summary").json()
    assert s["mode"] == "paper" and s["halt"] is False
    assert s["connected"] is False          # 从没跑过 cli trade status
    assert s["account_masked"] is None
    assert s["plan"]["id"] >= 1 and s["plan"]["params"]["max_positions"] == 1
    assert s["plan"]["items"][0]["symbol"] == "000001"
    assert s["positions"][0]["volume"] == 500
    assert s["orders"][0]["status"] == "submitted"
    assert s["fills"][0]["volume"] == 1000
    # 现持 500 ≠ 目标 1000 → 报一条差额 500
    assert s["reconcile"] == [
        {"symbol": "000001", "side": "buy", "have": 500, "target": 1000, "delta": 500}]


def test_trade_summary_reads_last_status_run(trade_client):
    """账号/资金来自最近一次**成功的账户状态**（`cli trade status`）。

    同一 task='trade' 下还写着页面快照（source=api）与发单（source=qmt）的
    普通文字记录；它们不能把已知账号/资金清空。
    """
    import json

    client = trade_client
    with dbmod.connect() as conn:
        conn.execute(
            "INSERT INTO collect_log(run_id, task, source, status, started_at, finished_at,"
            " message) VALUES ('r1','trade','qmt','success',?,?,?)",
            (dbmod.now(), dbmod.now(), json.dumps(
                {"account_masked": "****0123", "asset": {"cash": 5000.0}}, ensure_ascii=False)))
        conn.commit()

    def summary():
        from rpsboard.api import trade as trade_api
        trade_api._summary_cache.clear()
        return client.get("/api/trade/summary").json()

    s = summary()
    assert s["connected"] is True
    assert s["account_masked"] == "****0123"
    assert s["asset"]["cash"] == 5000.0
    assert s["last_run"]["run_id"] == "r1", s["last_run"]

    # 更晚的非账户记录：页面快照 / 发单成功 / 发单失败 / running —— 都不能顶掉账户状态
    later = [
        ("snap", "api", "success", "持仓快照（页面触发）"),
        ("exec", "qmt", "success", "提交 2 笔"),
        ("execfail", "qmt", "failed", "对账差异"),
        ("running", "api", "running", "进行中"),
        ("badjson", "qmt", "success", "not-json"),
        ("nulljson", "qmt", "success", "null"),
    ]
    with dbmod.connect() as conn:
        for run_id, source, status, msg in later:
            conn.execute(
                "INSERT INTO collect_log(run_id, task, source, status, started_at, finished_at,"
                " message) VALUES (?, 'trade', ?, ?, '2099-01-01T00:00:00',"
                " '2099-01-01T00:00:01', ?)",
                (run_id, source, status, msg))
        conn.commit()

    s = summary()
    assert s["connected"] is True
    assert s["account_masked"] == "****0123", s
    assert s["asset"]["cash"] == 5000.0
    assert s["last_run"]["run_id"] == "r1"

    # 新的有效账户状态必须接管
    with dbmod.connect() as conn:
        conn.execute(
            "INSERT INTO collect_log(run_id, task, source, status, started_at, finished_at,"
            " message) VALUES ('r2','trade','qmt','success','2099-01-02T00:00:00',"
            " '2099-01-02T00:00:01', ?)",
            (json.dumps({"account_masked": "****9999", "asset": {"cash": 6000.0}}),))
        conn.commit()
    s = summary()
    assert s["last_run"]["run_id"] == "r2"
    assert s["account_masked"] == "****9999"
    assert s["asset"]["cash"] == 6000.0


def test_trade_summary_without_account_state(trade_client):
    """没有任何合格账户状态（或缺表）时返回空结构，不 500。"""
    client = trade_client
    from rpsboard.api import trade as trade_api

    with dbmod.connect() as conn:
        conn.execute(
            "INSERT INTO collect_log(run_id, task, source, status, started_at, finished_at,"
            " message) VALUES ('r1','trade','qmt','failed',?,?,?)",
            (dbmod.now(), dbmod.now(), "连接失败"))
        conn.commit()
    trade_api._summary_cache.clear()
    s = client.get("/api/trade/summary").json()
    assert s["connected"] is False
    assert s["account_masked"] is None and s["asset"] is None


def test_trade_list_endpoints(trade_client):
    client = trade_client
    with dbmod.connect() as conn:
        _seed_trade_rows(conn, "2026-01-02")
    assert client.get("/api/trade/positions").json()["rows"][0]["symbol"] == "000001"
    assert client.get("/api/trade/orders").json()["rows"][0]["broker_order_id"] == 900001
    assert client.get("/api/trade/fills").json()["rows"][0]["volume"] == 1000
    assert client.get("/api/trade/plans?limit=5").json()["rows"][0]["mode"] == "paper"
    # 按日期过滤：换一个没有数据的日期必须返回空，而不是全量
    assert client.get("/api/trade/orders",
                      params={"date": "2020-01-01"}).json()["rows"] == []
    assert client.get("/api/trade/fills",
                      params={"date": "2026-01-02"}).json()["rows"][0]["symbol"] == "000001"


def test_trade_halt_toggles_sentinel_file(trade_client):
    client = trade_client
    halt_file = config.TRADE_HALT_FILE
    assert not halt_file.exists()
    on = client.post("/api/trade/halt", json={"on": True}).json()
    assert on == {"ok": True, "halt": True} and halt_file.exists()
    assert client.get("/api/trade/summary").json()["halt"] is True
    off = client.post("/api/trade/halt", json={"on": False}).json()
    assert off == {"ok": True, "halt": False} and not halt_file.exists()
    with dbmod.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM collect_log WHERE task='trade-halt'"
        ).fetchone()["c"]
    assert n == 2, "开关动作要留审计记录"


def test_trade_endpoints_tolerate_empty_db(trade_client):
    """一张交易数据都没有时必须返回空结构而不是 500。"""
    client = trade_client
    for ep in ("/api/trade/summary", "/api/trade/positions", "/api/trade/orders",
               "/api/trade/fills", "/api/trade/plans"):
        body = client.get(ep).json()
        assert isinstance(body, dict)
    s = client.get("/api/trade/summary").json()
    assert s["plan"] is None and s["positions"] == [] and s["reconcile"] == []


def test_trade_paper_endpoint_shape(seeded, tmp_path, monkeypatch):
    """自建模拟盘端点：空库返回空结构（days=0），不 500。"""
    client, *_ = seeded
    body = client.get("/api/trade/paper").json()
    assert body["days"] == 0 and body["curve"] == [] and body["positions"] == []


def test_trade_paper_endpoint_returns_nav_and_positions(seeded, tmp_path, monkeypatch):
    client, *_ = seeded
    with dbmod.connect() as conn:
        conn.execute(
            "INSERT INTO paper_account(id, cash, created_at, updated_at)"
            " VALUES (1, 500000, '2026-01-01', '2026-01-02')")
        conn.executemany(
            "INSERT INTO paper_nav(trade_date, cash, market_value, total, nav, n_positions)"
            " VALUES (?,?,?,?,?,?)",
            [("2026-01-01", 900000, 100000, 1000000, 1.0, 1),
             ("2026-01-02", 920000, 80000, 1000000, 1.0, 1)])
        conn.execute(
            "INSERT INTO paper_position(trade_date, symbol, volume, avg_price, close,"
            " market_value) VALUES ('2026-01-02','000001',1000,10.0,10.5,10500)")
        conn.commit()
    body = client.get("/api/trade/paper").json()
    assert body["days"] == 2 and body["from"] == "2026-01-01"
    assert [p["symbol"] for p in body["positions"]] == ["000001"]
    assert len(body["curve"]) == 2


# ── 自建模拟盘的归档（策略对照）─────────────────────────────────
def test_paper_archive_endpoints(trade_client):
    """归档三个入口：空账户明确报错、归档成功、列表、删除。"""
    client = trade_client
    empty = client.post("/api/trade/paper/archive", json={"label": "x"}).json()
    assert empty["ok"] is False and "无可归档" in empty["error"]

    with dbmod.connect() as conn:
        conn.execute(
            "INSERT INTO paper_account(id, cash, created_at, updated_at)"
            " VALUES (1, 900000, '2026-01-01', '2026-01-02')")
        conn.executemany(
            "INSERT INTO paper_nav(trade_date, cash, market_value, total, nav, n_positions)"
            " VALUES (?,?,?,?,?,?)",
            [("2026-01-01", 900000, 100000, 1000000, 1.0, 1),
             ("2026-01-02", 920000, 80000, 1000000, 1.0, 1)])
        conn.commit()

    created = client.post("/api/trade/paper/archive", json={"label": "RPS60"}).json()
    assert created["ok"] is True and created["label"] == "RPS60"
    rows = client.get("/api/trade/paper/archives").json()["rows"]
    assert [r["label"] for r in rows] == ["RPS60"]
    assert rows[0]["days"] == 2 and len(rows[0]["curve"]) == 2

    assert client.delete(f"/api/trade/paper/archives/{created['id']}").json()["ok"] is True
    assert client.get("/api/trade/paper/archives").json()["rows"] == []


def test_paper_export_csv(trade_client):
    """模拟盘导出：空库给带表头的空文件；有数据时转义正确、公式注入被前缀。"""
    import io

    client = trade_client
    empty = client.get("/api/trade/paper/export.csv")
    assert empty.status_code == 200
    assert empty.content.decode("utf-8-sig").splitlines()[0].startswith("日期")

    with dbmod.connect() as conn:
        conn.execute(
            "INSERT INTO paper_account(id, cash, created_at, updated_at)"
            " VALUES (1, 900000, '2026-01-05', '2026-01-05')")
        conn.executemany(
            "INSERT INTO paper_nav(trade_date, cash, market_value, total, nav, n_positions)"
            " VALUES (?,?,?,?,?,?)",
            [("2026-01-05", 900000, 100000, 1000000, 1.0, 1),
             ("2026-01-06", 920000, 80000, 1000000, 1.0, 1)])
        conn.execute(
            "INSERT INTO trade_order(plan_id, trade_date, symbol, side, volume, price,"
            " status, broker_order_id, err_msg, created_at, updated_at)"
            " VALUES (1,'2026-01-05','000001','buy',1000,12.1,'pending',900000001,"
            " '=1+1',?,?)", (dbmod.now(), dbmod.now()))
        conn.execute(
            "INSERT INTO trade_fill(trade_date, broker_order_id, broker_trade_id, symbol,"
            " side, volume, price, traded_at, fee, realized_pnl)"
            " VALUES ('2026-01-06',900000001,1,'000001','sell',1000,12.5,?,3.2,380.0)",
            (dbmod.now(),))
        conn.commit()

    nav = client.get("/api/trade/paper/export.csv", params={"kind": "nav"})
    rows = list(csv.reader(io.StringIO(nav.content.decode("utf-8-sig"))))
    assert rows[0][:2] == ["日期", "现金"]
    assert len(rows) == 3, "两行净值 + 表头"
    assert "paper_nav_2026-01-06.csv" in nav.headers["content-disposition"]

    orders = client.get("/api/trade/paper/export.csv",
                        params={"kind": "orders"}).content.decode("utf-8-sig")
    assert "'=1+1" in orders, "以 = 开头的文本必须加前缀（CSV 公式注入）"

    fills = client.get("/api/trade/paper/export.csv",
                       params={"kind": "fills"}).content.decode("utf-8-sig")
    assert "3.2000" in fills and "380.0000" in fills, "费用与已实现盈亏要导出"


def test_trade_paper_endpoint_supports_date_and_review(seeded):
    """`/api/trade/paper?date=` 回看历史某日（就近回退），并带复盘字段。"""
    client, *_ = seeded
    with dbmod.connect() as conn:
        conn.execute(
            "INSERT INTO paper_account(id, cash, created_at, updated_at)"
            " VALUES (1, 900000, '2026-01-01', '2026-01-02')")
        conn.executemany(
            "INSERT INTO paper_nav(trade_date, cash, market_value, total, nav, n_positions)"
            " VALUES (?,?,?,?,?,?)",
            [("2026-01-01", 900000, 100000, 1000000, 1.0, 1),
             ("2026-01-02", 920000, 80000, 1000000, 1.0, 1)])
        conn.execute(
            "INSERT INTO paper_position(trade_date, symbol, volume, avg_price, close,"
            " market_value) VALUES ('2026-01-01','000001',1000,10.0,10.5,10500)")
        conn.execute(
            "INSERT INTO paper_position(trade_date, symbol, volume, avg_price, close,"
            " market_value) VALUES ('2026-01-02','000002',500,20.0,21.0,10500)")
        conn.commit()

    d1 = client.get("/api/trade/paper", params={"date": "2026-01-01"}).json()
    assert d1["date"] == "2026-01-01"
    assert [p["symbol"] for p in d1["positions"]] == ["000001"], "回看日应给那天的持仓"
    assert d1["nav_row"]["trade_date"] == "2026-01-01"
    assert "review" in d1 and d1["review"]["summary"]["round_trips"] == 0

    # 早于曲线第一天 → 就近落到第一天（不是静默换成最新一天）
    early = client.get("/api/trade/paper", params={"date": "2025-12-28"}).json()
    assert early["date"] == "2026-01-01"

    latest = client.get("/api/trade/paper").json()
    assert latest["date"] == "2026-01-02"
    assert [p["symbol"] for p in latest["positions"]] == ["000002"]


# ── 因子评估 ────────────────────────────────────────────────────────
def test_factors_empty_returns_full_structure(seeded):
    """还没跑过 `cli factors` 时返回**同构**的空结构，不是 404/500。"""
    client, *_ = seeded
    r = client.get("/api/factors")
    assert r.status_code == 200
    body = r.json()
    assert body["run_at"] is None and body["params"] == {}
    assert body["factors"] == [] and body["corr"] == {"names": [], "matrix": []}
    assert body["notes"], "口径说明必须始终带上"


def test_factors_returns_latest_payload(seeded):
    client, *_ = seeded
    payload = {
        "factors": [{"name": "mom", "label": "动量", "ic_mean": 0.02, "samples": 900}],
        "corr": {"names": ["mom"], "matrix": [[1.0]]},
        "params": {"period": 60, "hold": 20, "train_days": 250},
        "notes": ["IC 为全样本口径"],
    }
    with dbmod.connect() as conn:
        conn.executemany(
            "INSERT INTO factor_eval(run_at, params, payload) VALUES (?,?,?)",
            [("2026-01-01T00:00:00", "{}", json.dumps({"factors": []})),
             ("2026-02-01T00:00:00", "{}", json.dumps(payload))])
        conn.commit()
    body = client.get("/api/factors").json()
    assert body["run_at"] == "2026-02-01T00:00:00", "必须取最新一次评估"
    assert body["factors"] == payload["factors"]
    assert body["corr"]["matrix"] == [[1.0]]


def test_backtest_signal_bogus_falls_back_to_rps(seeded):
    """白名单外的信号源回落成 rps（与 market_filter 同款兜底，不 500）。"""
    client, *_ = seeded
    body = client.get("/api/backtest/run", params={"signal": "bogus"}).json()
    assert body["params"]["signal"] == "rps"
    assert body["params"]["select_top"] is None


def test_backtest_composite_select_top(seeded, monkeypatch):
    from rpsboard.analytics import factors as fac

    monkeypatch.setattr(fac, "MIN_CROSS_IC", 3)     # 合成库只有 40 只票
    client, *_ = seeded
    r = client.get("/api/backtest/run", params={
        "period": 20, "hold": 20, "threshold": 0,
        "signal": "composite", "select_top": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["params"]["signal"] == "composite"
    assert body["params"]["select_top"] == 5
    assert all(c.get("holdings", 0) <= 5 for c in body["equity_curve"])


def test_backtest_cache_key_includes_signal(seeded, monkeypatch):
    """锁住「缓存串味」：先 rps 后 composite，两次必须各拿各的结果。

    缓存键漏掉 signal 时，第二次请求会直接返回第一次的 params（signal='rps'）。
    """
    from rpsboard.analytics import factors as fac
    from rpsboard.api import backtest as bt_api

    monkeypatch.setattr(fac, "MIN_CROSS_IC", 3)
    client, *_ = seeded
    bt_api.clear_backtest_cache()
    first = client.get("/api/backtest/run",
                       params={"period": 20, "threshold": 80, "signal": "rps"}).json()
    second = client.get("/api/backtest/run", params={
        "period": 20, "threshold": 0, "signal": "composite", "select_top": 3}).json()
    assert first["params"]["signal"] == "rps"
    assert second["params"]["signal"] == "composite"
    assert second["params"]["select_top"] == 3
    assert first.get("cached") is False, "第一次不该命中缓存"
    assert second.get("cached") is False, "参数不同就不该命中同一条缓存"


# ── 空库容错：未 init 时列表接口返回空，而不是 500 ───────────────────
def test_read_sql_missing_table_and_real_errors():
    """缺表 → 空表；真正的 SQL 错误（错误列/语法）必须继续抛。

    `pandas.read_sql_query` 把底层的 `sqlite3.OperationalError` 包成
    `pd.errors.DatabaseError`，所以 `except sqlite3.OperationalError`
    永远接不到 —— 未 init 的库上 `/api/meta/dates` 会直接 500。
    """
    from rpsboard.api import read_sql

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        assert read_sql(conn, "SELECT * FROM absent_table").empty
        assert read_sql(conn, "SELECT 42 AS value")["value"].iloc[0] == 42
        for sql in ("SELECT nonexistent_col FROM sqlite_master", "SELEC 1"):
            with pytest.raises(pd.errors.DatabaseError):
                read_sql(conn, sql)
    finally:
        conn.close()


def test_meta_lists_before_init(monkeypatch):
    """全新未建表的库：日期/行业/概念清单都是空列表，不是 500。"""
    import rpsboard.api as apimod
    from rpsboard.api import create_app, perf

    def fresh_conn():
        c = sqlite3.connect(":memory:", check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(dbmod, "connect", fresh_conn)
    monkeypatch.setattr(perf, "_append_jsonl", lambda entry: None)   # 不写真实日志
    monkeypatch.setattr(apimod, "_cache", {"conn": None, "opened": None})
    client = TestClient(create_app())
    for ep in ("/api/meta/dates", "/api/meta/industries", "/api/rps/concepts"):
        r = client.get(ep)
        assert r.status_code == 200, f"{ep} → {r.status_code}"
        assert r.json() == [], ep
