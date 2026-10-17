"""复盘笔记：按**生效交易日**存取，市场级与个股级分开。

核心风险是「写进一行永远读不到的孤儿记录」：用户选了周末，就地把笔记存到
周末那一行，之后按交易日永远查不到。所以写入必须落在生效日上，落不了就 422
并给出建议日。
"""

from __future__ import annotations

from rpsboard import db as dbmod


def _last_day(client) -> str:
    days = client.get("/api/meta/dates").json()
    assert days, "夹具库应当有 RPS 交易日"
    return days[0]


def test_note_roundtrip_and_effective_date(client):
    _ = client
    day = _last_day(client)

    # 首次读：空结构（不是 404/500）
    assert client.get("/api/review/note", params={"date": day}).json() == {
        "date": day, "symbol": "", "note": "", "updated_at": None}

    saved = client.post("/api/review/note",
                        json={"date": day, "note": "退潮第 2 天，缩量"})
    assert saved.status_code == 200 and saved.json()["ok"] is True
    got = client.get("/api/review/note", params={"date": day}).json()
    assert got["note"] == "退潮第 2 天，缩量" and got["updated_at"]

    # 覆盖
    client.post("/api/review/note", json={"date": day, "note": "改成反弹尝试"})
    assert client.get("/api/review/note", params={"date": day}).json()["note"] == \
        "改成反弹尝试"

    # 空串 = 删除
    cleared = client.post("/api/review/note", json={"date": day, "note": ""})
    assert cleared.json()["note"] == ""
    assert client.get("/api/review/note", params={"date": day}).json()[
        "note"] == "" and client.get(
        "/api/review/note", params={"date": day}).json()["updated_at"] is None


def test_note_rejects_non_trading_day(client):
    """非交易日直接 422 并给出建议生效日 —— 不能静默改写到别的一天。"""
    _ = client
    day = _last_day(client)
    r = client.post("/api/review/note",
                    json={"date": "2030-01-01", "note": "x"})
    assert r.status_code == 422
    assert day in r.json()["detail"], "要告诉用户最近的生效日是哪天"


def test_notes_are_scoped_by_symbol(client):
    """市场笔记与个股笔记互不覆盖。"""
    _ = client
    day = _last_day(client)
    client.post("/api/review/note", json={"date": day, "note": "市场：普涨"})
    client.post("/api/review/note",
                json={"date": day, "symbol": "000001", "note": "个股：放量突破"})
    assert client.get("/api/review/note", params={"date": day}).json()["note"] == \
        "市场：普涨"
    assert client.get("/api/review/note",
                      params={"date": day, "symbol": "000001"}).json()["note"] == \
        "个股：放量突破"


def test_note_rejects_bad_symbol_and_oversize(client):
    _ = client
    day = _last_day(client)
    assert client.get("/api/review/note",
                      params={"date": day, "symbol": "abc"}).status_code == 422
    assert client.post("/api/review/note", json={
        "date": day, "symbol": "600000.SH", "note": "x"}).status_code == 422
    assert client.post("/api/review/note", json={
        "date": day, "note": "x" * 10001}).status_code == 422


def test_note_on_empty_db_is_graceful(tmp_path, monkeypatch):
    """还没初始化数据时：GET 给同构空结构，POST 明说无法锚定日期。"""
    from fastapi.testclient import TestClient
    from rpsboard import config
    from rpsboard.api import create_app

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "notes_empty.db")
    import rpsboard.api as apimod
    monkeypatch.setattr(apimod, "_cache", {"conn": None, "opened": None})
    with TestClient(create_app()) as client:
        got = client.get("/api/review/note")
        assert got.status_code == 200
        assert got.json() == {"date": None, "symbol": "", "note": "",
                              "updated_at": None}
        posted = client.post("/api/review/note", json={"note": "x"})
        assert posted.status_code == 409


def test_note_table_is_migrated(client):
    """新表由迁移建出来（老库也该有）。"""
    _ = client
    with dbmod.session() as conn:
        assert dbmod.table_exists(conn, "review_note")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(review_note)")}
    assert {"date", "symbol", "note", "updated_at"} <= cols
