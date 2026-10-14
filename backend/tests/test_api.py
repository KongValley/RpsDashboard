"""API 契约测试：用临时 SQLite + 合成数据，不触网。"""

from __future__ import annotations

import csv
import io
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
