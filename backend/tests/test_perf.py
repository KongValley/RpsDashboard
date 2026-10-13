"""性能监测点：耗时中间件、环形缓冲、上报接口与 jsonl 落盘。

离线、不触网；只用空 schema 的临时库——被监测的是中间件本身，不是查询逻辑。
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from rpsboard import config, db as dbmod

from rpsboard.api import perf


@pytest.fixture()
def perf_env(tmp_path, monkeypatch):
    """空库 + 临时 jsonl + 复位后的环形缓冲。"""
    path = tmp_path / "perf.db"
    sqlite3.connect(str(path)).executescript(dbmod.SCHEMA)

    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "PERF_LOG", tmp_path / "perf.jsonl")

    import rpsboard.api as apimod

    monkeypatch.setattr(apimod, "_cache", {"conn": None, "opened": None})

    from rpsboard.api import create_app

    perf.reset()
    return TestClient(create_app()), tmp_path


def _paths(body: dict) -> dict[str, dict]:
    return {p["path"]: p for p in body["server"]["paths"]}


def test_header_and_aggregation(perf_env):
    client, _ = perf_env

    r = client.get("/api/meta/status")
    assert r.status_code == 200
    assert float(r.headers["x-perf-ms"]) >= 0

    client.get("/api/meta/status")
    client.get("/api/meta/status")
    client.get("/api/meta/dates")
    # 404 也必须看得见（接口挂掉时不能静默）
    assert client.get("/api/stock/ZZZZZZ").status_code == 404

    body = client.get("/api/meta/perf").json()
    assert body["slow_ms"] == config.PERF_SLOW_MS
    assert body["uptime_s"] >= 0
    # perf 查询自身不入统计
    assert body["server"]["n"] == 5

    paths = _paths(body)
    assert paths["/api/meta/status"]["n"] == 3
    assert paths["/api/meta/dates"]["n"] == 1
    status = paths["/api/meta/status"]
    assert status["p50_ms"] >= 0 and status["p95_ms"] >= status["p50_ms"]
    assert status["last_bytes"] > 0
    assert status["error"] == 0
    # 404 不是异常（handler 正常返回），但状态码要如实记录
    assert paths["/api/stock/ZZZZZZ"]["n"] == 1
    assert paths["/api/stock/ZZZZZZ"]["last_bytes"] > 0


def test_client_report_merges(perf_env):
    client, _ = perf_env
    client.get("/api/meta/status")
    before = client.get("/api/meta/perf").json()["server"]["n"]

    r = client.post(
        "/api/meta/perf/client",
        json={
            "entries": [
                {
                    "path": "/api/rps/ranking?period=60",
                    "ms": 123.4,
                    "server_ms": 100.0,
                    "status": 200,
                    "ok": True,
                }
            ]
        },
    )
    assert r.json() == {"ok": True, "accepted": 1}

    body = client.get("/api/meta/perf").json()
    assert body["client"]["n"] == 1
    assert body["client"]["slow"] == 0
    assert body["client"]["recent"][0]["net_ms"] == 23.4
    # 浏览器上报不混进服务端统计
    assert body["server"]["n"] == before

    # 缺 server_ms 时 net_ms 为 None，而不是编造一个 0
    client.post("/api/meta/perf/client", json={"entries": [{"path": "/api/x", "ms": 5}]})
    recent = client.get("/api/meta/perf").json()["client"]["recent"]
    assert recent[0]["server_ms"] is None and recent[0]["net_ms"] is None


def test_client_payload_bounds(perf_env):
    client, _ = perf_env
    ok = {"path": "/api/x", "ms": 1.0}
    assert client.post("/api/meta/perf/client", json={"entries": []}).json()["accepted"] == 0
    assert client.post("/api/meta/perf/client", json={"entries": [{}]}).status_code == 422
    assert client.post(
        "/api/meta/perf/client", json={"entries": [ok] * 201}
    ).status_code == 422
    assert client.post("/api/meta/perf/client", json={"entries": [ok] * 200}).status_code == 200


def test_jsonl_appended(perf_env):
    client, tmp_path = perf_env
    client.get("/api/meta/status")
    client.get("/api/stock/ZZZZZZ")
    client.post("/api/meta/perf/client", json={"entries": [{"path": "/api/x", "ms": 5}]})

    lines = (tmp_path / "perf.jsonl").read_text(encoding="utf-8").strip().splitlines()
    rows = [json.loads(l) for l in lines]
    assert len(rows) == 3
    assert [r["src"] for r in rows] == ["server", "server", "client"]
    assert rows[1]["path"] == "/api/stock/ZZZZZZ" and rows[1]["status"] == 404
    assert all(isinstance(r["ms"], float) and isinstance(r["slow"], bool) for r in rows)


def test_pct_nearest_rank():
    assert perf._pct([7.0], 0.5) == 7.0 == perf._pct([7.0], 0.95)
    xs = [1.0, 2.0, 3.0, 100.0]
    assert perf._pct(xs, 0.5) == 2.0      # nearest-rank，不插值
    assert perf._pct(xs, 0.95) == 100.0
    assert perf._pct(xs, 1.0) == 100.0


def test_log_write_failure_does_not_break_requests(perf_env, monkeypatch):
    """logs/perf.jsonl 不可写时：请求照常，只是不再落盘。"""
    client, tmp_path = perf_env
    monkeypatch.setattr(config, "PERF_LOG", tmp_path)  # 指向目录 → open 必定失败
    assert client.get("/api/meta/status").status_code == 200
    assert perf._log_broken is True
    assert client.get("/api/meta/perf").json()["server"]["n"] >= 1
