"""`sources/tdxnet_src` 与 `sources/corporate_src` 的解析层（不联网）。

eltdx 客户端换成最小替身：这里守的是**我们自己写的那部分**——分页翻页、
单位换算（手 vs 股）、日期解析、去重排序、权息事件合并、以及各条失败分支
不能把「失败」伪装成「没有数据」。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pandas as pd
from rpsboard import config
from rpsboard import db as dbmod
from rpsboard.sources import corporate_src, tdxnet_src


class _Page:
    def __init__(self, bars):
        self.bars = tuple(bars)


class _Bar:
    def __init__(self, day, o, h, lo, c, lots, amount):
        self.time = datetime.strptime(day, "%Y-%m-%d")
        self.open = o
        self.high = h
        self.low = lo
        self.close = c
        self.volume_lots = lots                # 单位：手
        self.volume_wire_value = lots * 100    # 单位：股
        self.amount = amount


class _Bars:
    """按 (code, start, count) 切片返回，模拟通达信分页（单页上限由 MAX_COUNT 控制）。"""

    def __init__(self, bars_by_code):
        self.calls: list[tuple] = []
        self._data = bars_by_code

    def get(self, code, *, period="day", adjust=None, count=800, start=0, **kw):
        self.calls.append((code, adjust, start, count))
        bars = self._data.get(code, [])
        if adjust == "hfq":
            return _Page(bars[start:start + count])
        return _Page(bars[start:start + count])


class _Helpers:
    def __init__(self, rows=()):
        self._rows = rows

    def latest_stock_list(self):
        return list(self._rows)


class _FakeClient:
    def __init__(self, bars_by_code=None, rows=(), download=None):
        self.bars = _Bars(bars_by_code or {})
        self.helpers = _Helpers(rows)
        self.resources = SimpleNamespace(download_file=lambda name: (download or {}).get(name))

    def connect(self):
        return None

    def close(self):
        return None


def _install(monkeypatch, **kw) -> _FakeClient:
    client = _FakeClient(**kw)

    class _Factory:
        @staticmethod
        def from_hosts(*_a, **_k):
            return client

    import eltdx
    monkeypatch.setattr(eltdx, "TdxClient", _Factory, raising=False)
    return client


def _bars(n, base=10.0):
    out = []
    for i in range(n):
        day = f"2026-01-{i + 1:02d}"
        px = base + i * 0.1
        out.append(_Bar(day, px, px + 0.5, px - 0.5, px, 1000 + i, 1e6))
        if i == 27:
            break
    return out


# ── 代码规范化 ──────────────────────────────────────────────────────
def test_to_full_code_prefixes():
    assert tdxnet_src.to_full_code("600519") == "sh600519"
    assert tdxnet_src.to_full_code("000001") == "sz000001"
    assert tdxnet_src.to_full_code("300750") == "sz300750"
    assert tdxnet_src.to_full_code("830799") == "bj830799"
    assert tdxnet_src.to_full_code("920001") == "bj920001"
    assert tdxnet_src.to_full_code("1") == "sz000001", "短代码补零"


# ── 分页与单位 ──────────────────────────────────────────────────────
def test_bar_pages_paginates_until_short_page(monkeypatch):
    client = _install(monkeypatch, bars_by_code={"sh600519": _bars(5)})
    monkeypatch.setattr(tdxnet_src, "MAX_COUNT", 2)
    src = tdxnet_src.TdxSource()
    pages = src._bar_pages("sh600519", 4)
    assert len(pages) == 4, "按 MAX_COUNT 翻页凑够请求根数就停"
    # 每次请求都带上了递增的 start（凑够就停，不会多请求一页）
    assert [c[2] for c in client.bars.calls] == [0, 2]


def test_bar_pages_stops_at_first_short_page(monkeypatch):
    """分页取到的根数少于请求数 = 已到上市首日，必须停（否则死循环）。"""
    _install(monkeypatch, bars_by_code={"sh600519": _bars(3)})
    monkeypatch.setattr(tdxnet_src, "MAX_COUNT", 10)
    src = tdxnet_src.TdxSource()
    assert len(src._bar_pages("sh600519", 50)) == 3


def test_hfq_and_raw_use_different_volume_units(monkeypatch):
    """后复权存**手**、不复权存**股** —— 差 100 倍，混用会把参与率算错。"""
    _install(monkeypatch, bars_by_code={"sh600519": _bars(3)})
    src = tdxnet_src.TdxSource()
    hfq = src.hfq_bars("600519")
    raw = src.raw_bars("600519")
    assert list(hfq["volume"]) == [1000, 1001, 1002]
    assert list(raw["volume"]) == [100_000, 100_100, 100_200]


def test_bars_frame_sorts_and_dedups_and_skips_bad_rows(monkeypatch):
    """分页回来是「新→旧」，且可能重复：必须排序去重，缺 close 的行丢掉。"""
    bars = _bars(3)
    bars.append(_Bar("2026-01-03", 1, 1, 1, 1, 1, 1))          # 与上一根同日期 → 去重
    bad = _Bar("2026-01-04", 1, 1, 1, 1, 1, 1)
    bad.close = None                                            # 无收盘价 → 丢弃
    bars.append(bad)
    _install(monkeypatch, bars_by_code={"sh600519": list(reversed(bars))})
    src = tdxnet_src.TdxSource()
    df = src.hfq_bars("600519")
    assert list(df["date"]) == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert df["date"].is_monotonic_increasing


def test_index_bars(monkeypatch):
    _install(monkeypatch, bars_by_code={"sh000001": _bars(4, base=3000.0)})
    src = tdxnet_src.TdxSource()
    df = src.index_bars("sh000001")
    assert list(df.columns) == ["date", "close"] and len(df) == 4


def test_index_bars_empty(monkeypatch):
    _install(monkeypatch, bars_by_code={})
    assert tdxnet_src.TdxSource().index_bars("sh000001").empty


def test_stock_list_dedups_and_flags_st(monkeypatch):
    rows = [SimpleNamespace(code="1", name="平安银行", exchange="sz"),
            SimpleNamespace(code="000001", name="平安银行", exchange="sz"),
            SimpleNamespace(code="2", name="ST 某某", exchange="sz"),
            SimpleNamespace(code=None, name="无处安放", exchange="sz")]
    _install(monkeypatch, rows=rows)
    df = tdxnet_src.TdxSource().stock_list()
    assert list(df["symbol"]) == ["000001", "000002"]
    assert list(df["is_st"]) == [0, 1]


# ── 写入分支 ────────────────────────────────────────────────────────
def test_write_hfq_and_raw_with_no_frames(tmp_path, capsys):
    path = tmp_path / "w.db"
    dbmod.init_db(path)
    with dbmod.session(path) as conn:
        assert tdxnet_src._write_hfq(conn, [], {}) == 0
        assert tdxnet_src._write_raw(conn, []) == 0
        assert tdxnet_src._update_raw_coverage(conn, [], "2026-01-05") == 0
    out = capsys.readouterr().out
    assert "后复权未取到任何数据" in out and "不复权未取到任何数据" in out


def test_raw_tradestatus_flags_invalid_rows():
    df = pd.DataFrame({
        "open": [10.0, 0.0, 10.0, 10.5],
        "high": [10.5, 1.0, 10.5, 10.0],      # 最后一根 high < open → 不自洽
        "low": [9.5, 0.5, 9.5, 9.5],
        "close": [10.2, 0.0, 10.2, 9.8],
        "volume": [1000.0, 1000.0, 0.0, 1000.0],
    })
    assert list(tdxnet_src._raw_tradestatus(df)) == [1, 0, 0, 0]


# ── 公司行为 ────────────────────────────────────────────────────────
def _rec(day, cat, c1=0.0, c2=0.0, c3=0.0, c4=0.0):
    return SimpleNamespace(date=datetime.strptime(day, "%Y-%m-%d").date(),
                           category_raw=cat, category_name="除权除息",
                           c1_value=c1, c2_value=c2, c3_value=c3, c4_value=c4)


def test_fetch_capital_changes_distinguishes_empty_and_failure():
    """空列表 = 查过且没有事件；异常/结构不认识 = **未知**（不能当无事件）。"""
    src = SimpleNamespace(_client=SimpleNamespace(corporate=SimpleNamespace(
        capital_changes=lambda code: SimpleNamespace(records=()))))
    assert corporate_src.fetch_capital_changes(src, "600519") == []

    src_err = SimpleNamespace(_client=SimpleNamespace(corporate=SimpleNamespace(
        capital_changes=lambda code: (_ for _ in ()).throw(RuntimeError("网络炸")))))
    assert corporate_src.fetch_capital_changes(src_err, "600519") is None

    src_none = SimpleNamespace(_client=SimpleNamespace(corporate=SimpleNamespace(
        capital_changes=lambda code: None)))
    assert corporate_src.fetch_capital_changes(src_none, "600519") is None

    src_weird = SimpleNamespace(_client=SimpleNamespace(corporate=SimpleNamespace(
        capital_changes=lambda code: SimpleNamespace())))
    assert corporate_src.fetch_capital_changes(src_weird, "600519") is None


def test_fetch_capital_changes_serializes_dates():
    src = SimpleNamespace(_client=SimpleNamespace(corporate=SimpleNamespace(
        capital_changes=lambda code: SimpleNamespace(
            records=(_rec("2026-01-06", 1, c1=1.0, c3=0.5),)))))
    out = corporate_src.fetch_capital_changes(src, "600519")
    assert out == [{"date": "2026-01-06", "category_raw": 1,
                    "category_name": "除权除息", "c1_value": 1.0, "c2_value": 0.0,
                    "c3_value": 0.5, "c4_value": 0.0}]


def test_write_events_merges_same_day_and_category(tmp_path):
    """同日同类多条事件必须存成**数组**，不能被最后一条覆盖。"""
    path = tmp_path / "ev.db"
    dbmod.init_db(path)
    with dbmod.session(path) as conn:
        n = corporate_src._write_events(conn, "600519", [
            {"date": "2026-01-06", "category_raw": 1, "c1_value": 1.0},
            {"date": "2026-01-06", "category_raw": 1, "c1_value": 2.0},
            {"date": "2026-01-07", "category_raw": 5, "c1_value": 0.0},
            {"date": None, "category_raw": 1},                     # 无日期 → 丢
        ])
        assert n == 2
        import json
        rows = {r["event_date"]: json.loads(r["payload"]) for r in conn.execute(
            "SELECT event_date, payload FROM corporate_event")}
    assert len(rows["2026-01-06"]) == 2 and len(rows["2026-01-07"]) == 1
    assert corporate_src._write_events(conn, "600519", [{"date": None}]) == 0


def test_sync_corporate_uses_coverage_end(tmp_path, monkeypatch):
    """查全标记推进到**该股本轮的覆盖末日**，失败的不动。"""
    path = tmp_path / "cs.db"
    dbmod.init_db(path)
    with dbmod.session(path) as conn:
        conn.executemany("INSERT INTO raw_coverage(symbol, date_from, date_to,"
                         " updated_at) VALUES (?,?,?,'x')",
                         [("600519", "2026-01-01", "2026-01-05"),
                          ("000001", "2026-01-01", "2026-01-06")])
        conn.commit()
    monkeypatch.setattr(config, "DB_PATH", path)

    class _Src:
        def __init__(self, *a, **k):
            pass

        def close(self):
            pass

    monkeypatch.setattr(corporate_src, "TdxSource", _Src)
    monkeypatch.setattr(corporate_src, "fetch_capital_changes",
                        lambda src, sym: [] if sym == "600519" else None)
    with dbmod.session(path) as conn:
        res = corporate_src.sync_corporate(conn, workers=1)
        through = {r["symbol"]: r["checked_through"] for r in conn.execute(
            "SELECT symbol, checked_through FROM corporate_sync")}
    assert res["ok"] == 1 and res["failed"] == ["000001"]
    assert through == {"600519": "2026-01-05"}
