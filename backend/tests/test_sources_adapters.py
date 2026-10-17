"""数据源适配器的行为（不联网：把 eltdx 客户端换成最小替身）。

守的是几处**已经踩过的坑**：
- `board_quotes` 空选择时 rows 是空 tuple，`rows or table` 会把它变成
  `list(BoardQuoteTable)` → TypeError，把「这个分类没数据」伪装成崩溃；
- `board_member_quotes` 必须传完整代码（传 6 位会被拼成 `bj880515` → topic not found）；
- 行业采集：整表重建只在 **--replace 且本轮零失败** 时发生，部分失败必须保留旧行；
- 概念采集：失败板块保留旧成分，且清掉 `board_code IS NULL` 的历史孤儿。
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest
from rpsboard import config
from rpsboard import db as dbmod
from rpsboard.sources import board_src, industry_src, limit_src


class _Rows:
    def __init__(self, rows):
        self.rows = rows


class _Board:
    def __init__(self, code, name, full=None):
        self.board_code = code
        self.board_name = name
        self.full_code = full if full is not None else f"sh{code}"


def _fake_src(*, boards=None, members=None, files=None, limits=None):
    """最小 eltdx 替身：只实现被适配器用到的几个入口。"""
    helpers = SimpleNamespace(
        board_quotes=lambda category="概念": _Rows(boards if boards is not None else ()),
        board_member_quotes=lambda full_code: SimpleNamespace(
            raw_members=members.get(full_code, []) if members else []),
        daily_price_limits=lambda trade_date=None: _Rows(
            limits if limits is not None else ()),
    )
    resources = SimpleNamespace(download_file=lambda name: (files or {}).get(name))
    client = SimpleNamespace(helpers=helpers, resources=resources)
    return SimpleNamespace(_client=client, close=lambda: None)


# ── board_src ───────────────────────────────────────────────────────
def test_fetch_boards_handles_empty_selection():
    """空选择不能崩（旧写法 `rows or table` 会 TypeError）。"""
    src = _fake_src(boards=())
    df = board_src.fetch_boards(src=src, category="一级行业")
    assert df.empty


def test_fetch_boards_normalizes_and_flags_concepts():
    src = _fake_src(boards=[
        _Board("880515", "车联网", "sh880515"),
        _Board("880001", "通达信88", "sh880001"),        # 宽基指数，不算概念
        _Board("", "只给了 full_code", "sz399001"),
        _Board("", ""),                                   # 两列都空 → 丢弃
    ])
    df = board_src.fetch_boards(src=src)
    got = {r["board_code"]: (r["board_name"], r["is_concept"]) for _, r in df.iterrows()}
    assert got["880515"] == ("车联网", 1)
    assert got["880001"][1] == 0, "通达信宽基指数不是概念题材"
    assert "399001" in got, "只有 full_code 时也要能补出 board_code"
    assert len(df) == 3


def test_fetch_members_zero_pads_and_dedups():
    src = _fake_src(members={"sh880515": [{"code": "1"}, {"code": "600519"},
                                          SimpleNamespace(code=2), {"nope": 1}]})
    assert board_src.fetch_members("sh880515", src) == ["000001", "600519", "000002"]


def test_sync_concept_map_keeps_old_members_on_failure(tmp_path, monkeypatch):
    """失败的板块保留旧成分；成功板块按 board_code 增量替换。"""
    path = tmp_path / "concept.db"
    dbmod.init_db(path)
    with dbmod.session(path) as conn:
        conn.execute("INSERT INTO concept_map(symbol, concept, board_code, updated_at)"
                     " VALUES ('000001','老概念','880001','x')")
        # 历史孤儿：没有 board_code 的行（整表替换时代留下的）
        conn.execute("INSERT INTO concept_map(symbol, concept, board_code, updated_at)"
                     " VALUES ('000002','孤儿',NULL,'x')")
        conn.commit()
    monkeypatch.setattr(config, "DB_PATH", path)

    def fake_fetch_members(full, src):
        if full == "sh880002":
            raise RuntimeError("这个板块挂了")
        return ["600519"]

    monkeypatch.setattr(board_src, "fetch_members", fake_fetch_members)
    monkeypatch.setattr(board_src, "TdxSource", lambda: _fake_src())
    monkeypatch.setattr(board_src, "fetch_boards", lambda *a, **k: pd.DataFrame([
        {"board_code": "880001", "full_code": "sh880001", "board_name": "概念A",
         "is_concept": 1},
        {"board_code": "880002", "full_code": "sh880002", "board_name": "概念B",
         "is_concept": 1},
        {"board_code": "880003", "full_code": "sh880003", "board_name": "通达信88",
         "is_concept": 0},
    ]))
    n_sym, failed = board_src.sync_concept_map()
    assert failed == ["概念B"]
    assert n_sym >= 1
    with dbmod.session(path) as conn:
        rows = {r["symbol"]: (r["concept"], r["board_code"]) for r in conn.execute(
            "SELECT symbol, concept, board_code FROM concept_map")}
    assert rows["600519"] == ("概念A", "880001"), "成功板块按 board_code 增量替换"
    assert "000002" not in rows, "board_code 为 NULL 的历史孤儿要被清掉"
    # 失败板块（880002）没有旧行，但它的票不该被别的板块顺手清掉；
    # 880001 是**按 board_code 整块替换**，所以旧成员 000001 随之消失。
    assert "000001" not in rows


def test_sync_concept_map_raises_when_no_boards(monkeypatch):
    monkeypatch.setattr(board_src, "fetch_boards", lambda *a, **k: pd.DataFrame())
    with pytest.raises(RuntimeError, match="board_quotes 返回空"):
        board_src.sync_concept_map()


def test_sync_concept_map_raises_when_all_members_fail(tmp_path, monkeypatch):
    path = tmp_path / "concept2.db"
    dbmod.init_db(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(board_src, "fetch_boards", lambda *a, **k: pd.DataFrame([
        {"board_code": "880001", "full_code": "sh880001", "board_name": "概念A",
         "is_concept": 1}]))
    monkeypatch.setattr(board_src, "fetch_members",
                        lambda full, src: (_ for _ in ()).throw(RuntimeError("挂")))
    monkeypatch.setattr(board_src, "TdxSource", lambda: _fake_src())
    with pytest.raises(RuntimeError, match="未取到任何概念成分股"):
        board_src.sync_concept_map()


# ── industry_src ────────────────────────────────────────────────────
def test_pick_one_industry_prefers_smallest_board():
    """一只票同时属于多个行业时，取**成员最少**的那个（避免重复计入）。"""
    df = industry_src._pick_one_industry([
        {"symbol": "000001", "industry": "大行业"},
        {"symbol": "000001", "industry": "小行业"},
        {"symbol": "000002", "industry": "大行业"},
        {"symbol": "000003", "industry": "大行业"},
        {"symbol": "000004", "industry": "小行业"},
        {"symbol": "000005", "industry": "小行业"},
    ])
    got = dict(zip(df["symbol"], df["industry"]))
    assert got["000001"] == "大行业"
    assert industry_src._pick_one_industry([]).empty


def test_from_csv_reads_and_validates(tmp_path, capsys):
    csv = tmp_path / "map.csv"
    csv.write_text("symbol,industry,industry_cls\n"
                   "600519,白酒,外部\n"
                   "abc,坏行,外部\n"
                   "000001,,空行业\n",
                   encoding="utf-8")
    df, cls, failed = industry_src._from_csv(csv)
    assert failed == 0 and cls == "外部"
    assert list(df["symbol"]) == ["600519"]

    bad = tmp_path / "bad.csv"
    bad.write_text("code,name\n1,2\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="symbol 与 industry"):
        industry_src._from_csv(bad)
    empty = tmp_path / "empty.csv"
    empty.write_text("symbol,industry\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="没有有效"):
        industry_src._from_csv(empty)


def test_from_tdxhy_parses_level_keys(tmp_path, monkeypatch):
    cfg = tmp_path / "tdxhy.cfg"
    cfg.write_text(
        "1|600519|1|1|1|X3201|extra\n"    # 正常行（键长于前缀，取前 3 位）
        "|short|fields\n"                  # 字段不足
        "1|abc|||1|X3201|extra\n"          # 非法代码
        "1|000001|||1|X32|extra\n"         # 层级键只剩前缀 → 无层级信息，丢掉
        "1|000002|||1|X9902|extra\n",      # 正常行
        encoding="gbk")
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path)
    df, cls, failed = industry_src._from_tdxhy()
    assert cls == industry_src.CLS_CODE and failed == 0
    assert dict(zip(df["symbol"], df["industry"])) == {"600519": "X32", "000002": "X99"}


def test_ensure_tdxhy_downloads_when_missing(tmp_path):
    target = tmp_path / "sub" / "tdxhy.cfg"
    src = _fake_src(files={"tdxhy.cfg": b"1|600519||||X32|"})
    import rpsboard.sources.industry_src as mod

    orig = mod.TdxSource
    mod.TdxSource = lambda: src
    try:
        out = mod._ensure_tdxhy(target)
    finally:
        mod.TdxSource = orig
    assert out == target and target.read_bytes().startswith(b"1|600519")
    # 已存在时直接返回，不再下载
    assert mod._ensure_tdxhy(target) == target


def test_ensure_tdxhy_raises_with_actionable_hint(tmp_path):
    target = tmp_path / "missing.cfg"
    src = _fake_src(files={})
    import rpsboard.sources.industry_src as mod

    orig = mod.TdxSource
    mod.TdxSource = lambda: src
    try:
        with pytest.raises(RuntimeError, match="--from-csv"):
            mod._ensure_tdxhy(target)
    finally:
        mod.TdxSource = orig


def test_from_named_boards_reports_failed_boards(monkeypatch):
    monkeypatch.setattr(industry_src, "fetch_boards", lambda category="概念": (
        pd.DataFrame([{"board_name": "行业A", "full_code": "sh880001"}])
        if category == "一级行业" else pd.DataFrame()))
    monkeypatch.setattr(industry_src, "TdxSource", lambda: _fake_src())

    def fake_members(full, src):
        raise RuntimeError("板块挂了")

    monkeypatch.setattr(industry_src, "fetch_members", fake_members)
    df, cls, failed = industry_src._from_named_boards()
    assert df.empty and failed == 0 and cls == "", "全部失败 → 返回空，交给下一条数据源"


def test_sync_industry_map_paths(tmp_path, monkeypatch):
    path = tmp_path / "ind.db"
    dbmod.init_db(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    csv = tmp_path / "map.csv"
    csv.write_text("symbol,industry\n600519,白酒\n000001,银行\n", encoding="utf-8")

    n_sym, failed = industry_src.sync_industry_map(from_csv=str(csv))
    assert n_sym == 2 and failed == 0
    with dbmod.session(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM industry_map").fetchone()[0] == 2

    # 已有数据且未指定 replace/fill_missing → 跳过（保护已有口径）
    with dbmod.session(path) as conn:
        before = sorted((r["symbol"], r["industry"]) for r in conn.execute(
            "SELECT symbol, industry FROM industry_map"))
    assert industry_src.sync_industry_map() == (0, 0)
    with dbmod.session(path) as conn:
        after = sorted((r["symbol"], r["industry"]) for r in conn.execute(
            "SELECT symbol, industry FROM industry_map"))
    assert before == after

    # 部分失败 → **不做**整表重建（旧行留着），只覆盖本轮成功的票
    monkeypatch.setattr(industry_src, "fetch_boards", lambda category="概念": pd.DataFrame(
        [{"board_name": "行业A", "full_code": "sh880001"}]
        if category == "一级行业" else []))
    monkeypatch.setattr(industry_src, "TdxSource", lambda: _fake_src())
    monkeypatch.setattr(industry_src, "fetch_members",
                        lambda full, src: ["600000"] if full == "sh880001" else [])
    # 让其中一个板块失败：再加一个板块
    monkeypatch.setattr(industry_src, "fetch_boards", lambda category="概念": pd.DataFrame(
        [{"board_name": "行业A", "full_code": "sh880001"},
         {"board_name": "行业B", "full_code": "sh880002"}]
        if category == "一级行业" else []))

    def members(full, src):
        if full == "sh880002":
            raise RuntimeError("炸")
        return ["600000"]

    monkeypatch.setattr(industry_src, "fetch_members", members)
    n_sym, failed = industry_src.sync_industry_map(replace=True)
    assert failed == 1
    with dbmod.session(path) as conn:
        rows = dict(conn.execute("SELECT symbol, industry FROM industry_map"))
    assert "600519" in rows, "有板块失败时不能整表重建（旧行必须还在）"
    assert rows["600000"] == "行业A"


def test_sync_industry_map_replace_only_when_zero_failures(tmp_path, monkeypatch):
    path = tmp_path / "ind2.db"
    dbmod.init_db(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    with dbmod.session(path) as conn:
        conn.execute("INSERT INTO industry_map(symbol, industry, updated_at)"
                     " VALUES ('999999','旧口径','x')")
        conn.commit()
    monkeypatch.setattr(industry_src, "fetch_boards", lambda category="概念": pd.DataFrame(
        [{"board_name": "行业A", "full_code": "sh880001"}]
        if category == "一级行业" else []))
    monkeypatch.setattr(industry_src, "TdxSource", lambda: _fake_src())
    monkeypatch.setattr(industry_src, "fetch_members", lambda full, src: ["600000"])
    assert industry_src.sync_industry_map(replace=True) == (1, 0)
    with dbmod.session(path) as conn:
        rows = dict(conn.execute("SELECT symbol, industry FROM industry_map"))
    assert "999999" not in rows, "零失败 + --replace 才是真正的整表重建"
    assert rows["600000"] == "行业A"


def test_sync_industry_map_fill_missing(tmp_path, monkeypatch):
    path = tmp_path / "ind3.db"
    dbmod.init_db(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    with dbmod.session(path) as conn:
        conn.execute("INSERT INTO industry_map(symbol, industry, industry_cls,"
                     " updated_at) VALUES ('600519','白酒','证监会','x')")
        conn.commit()
    cfg = tmp_path / "tdxhy.cfg"
    cfg.write_text("1|600519|||1|X3201|x\n1|000001|||1|X3201|x\n", encoding="gbk")
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path)
    n_sym, failed = industry_src.sync_industry_map(fill_missing=True)
    assert failed == 0 and n_sym == 1, "只补缺行业的那一只"
    with dbmod.session(path) as conn:
        rows = dict(conn.execute("SELECT symbol, industry FROM industry_map"))
    assert rows["600519"] == "白酒", "已有口径保留"
    assert rows["000001"] == "X32", "缺的补上层级代码"
    # 没有需要补的时候返回 (0, 0)
    assert industry_src.sync_industry_map(fill_missing=True) == (0, 0)


def test_sync_industry_map_raises_when_nothing_found(tmp_path, monkeypatch):
    path = tmp_path / "ind4.db"
    dbmod.init_db(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(industry_src, "fetch_boards", lambda category="概念": pd.DataFrame())
    monkeypatch.setattr(industry_src, "TdxSource", lambda: _fake_src())
    monkeypatch.setattr(industry_src, "fetch_members", lambda full, src: [])
    # 下载目录指到空目录：否则会读到仓库里真实的 downloads/tdxhy.cfg
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "nodl")
    with pytest.raises(RuntimeError, match="--from-csv"):
        industry_src.sync_industry_map(replace=True)


# ── limit_src ───────────────────────────────────────────────────────
def test_fetch_limit_rows_normalizes(monkeypatch):
    limits = [SimpleNamespace(full_code="sh600519", limit_ratio_pct=10.0,
                              limit_up_price=11.0, limit_down_price=9.0,
                              limit_rule="main_10pct", limit_status="normal"),
              SimpleNamespace(full_code="", limit_ratio_pct=1, limit_up_price=1,
                              limit_down_price=1, limit_rule="x", limit_status="y")]
    monkeypatch.setattr(limit_src, "TdxSource", lambda: _fake_src(limits=limits))
    df = limit_src.fetch_limit_rows("20260105")
    assert list(df.columns) == ["date", "symbol", "limit_ratio", "limit_up",
                                "limit_down", "rule", "status"]
    assert len(df) == 1 and df.iloc[0]["symbol"] == "600519"
    assert df.iloc[0]["date"] == "2026-01-05"


def test_fetch_limit_rows_empty(monkeypatch):
    monkeypatch.setattr(limit_src, "TdxSource", lambda: _fake_src(limits=()))
    df = limit_src.fetch_limit_rows("20260105")
    assert df.empty and "symbol" in df.columns


def test_sync_price_limits_writes_rows(tmp_path, monkeypatch, capsys):
    path = tmp_path / "lim.db"
    dbmod.init_db(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    limits = [SimpleNamespace(full_code="sh600519", limit_ratio_pct=10.0,
                              limit_up_price=11.0, limit_down_price=9.0,
                              limit_rule="main_10pct", limit_status="normal"),
              SimpleNamespace(full_code="sz000001", limit_ratio_pct=10.0,
                              limit_up_price=11.0, limit_down_price=9.0,
                              limit_rule="main_10pct", limit_status="no_price_limit")]
    monkeypatch.setattr(limit_src, "TdxSource", lambda: _fake_src(limits=limits))
    n = limit_src.sync_price_limits("2026-01-05")
    assert n == 2
    out = capsys.readouterr().out
    assert "写入 2 行" in out and "无涨跌幅限制 1" in out


def test_sync_price_limits_empty_returns_zero(tmp_path, monkeypatch, capsys):
    path = tmp_path / "lim2.db"
    dbmod.init_db(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(limit_src, "TdxSource", lambda: _fake_src(limits=()))
    assert limit_src.sync_price_limits("2026-01-05") == 0
    assert "未取到涨跌停数据" in capsys.readouterr().out


def test_sync_price_limits_chunk_collects_failures(tmp_path, monkeypatch, capsys):
    path = tmp_path / "lim3.db"
    dbmod.init_db(path)
    monkeypatch.setattr(config, "DB_PATH", path)

    def maybe_fail(trade_date, src=None):
        if trade_date == "2026-01-06":
            raise RuntimeError("下游挂了")
        return pd.DataFrame([{"date": trade_date, "symbol": "600519",
                              "limit_ratio": 10.0, "limit_up": 11.0,
                              "limit_down": 9.0, "rule": "main",
                              "status": "normal"}])

    monkeypatch.setattr(limit_src, "fetch_limit_rows", maybe_fail)
    total, failed, empty = limit_src.sync_price_limits_chunk(
        ["2026-01-05", "2026-01-06", "2026-01-07"], workers=2)
    assert "2026-01-06" in failed
    assert total >= 1
    # 空列表直接返回 0，不启动线程池
    assert limit_src.sync_price_limits_chunk([]) == (0, [], [])


def test_missing_limit_dates_requires_half_coverage(tmp_path, monkeypatch):
    """「有行」不等于「覆盖齐」：只采到不足一半的日期要重采（倒序，先补最近的）。

    判据是 `m < max(1, n * 0.5)`（n = 当日 daily_bar 行数）——
    残缺分区（部分失败）不会因为「有几十行」就被当成采过了。
    """
    from rpsboard import cli

    path = tmp_path / "lim4.db"
    dbmod.init_db(path)
    with dbmod.session(path) as conn:
        # 2026-01-05：10 只票有行情；2026-01-06：2 只
        for i in range(10):
            conn.execute("INSERT INTO daily_bar(symbol,date,close) VALUES"
                         " (?, '2026-01-05', 1)", (f"{600000 + i:06d}",))
        conn.execute("INSERT INTO daily_bar(symbol,date,close)"
                     " VALUES ('600000','2026-01-06',1)")
        conn.execute("INSERT INTO daily_bar(symbol,date,close)"
                     " VALUES ('600001','2026-01-06',1)")
        conn.commit()
    monkeypatch.setattr(config, "DB_PATH", path)
    conn = dbmod.connect(path)
    try:
        # 两个日期一行都没有 → 都要补（倒序）
        assert cli.missing_limit_dates(conn) == ["2026-01-06", "2026-01-05"]
        with dbmod.session(path) as c:
            # 2026-01-05 只采到 2 行（阈值 5）→ 仍算未覆盖
            c.executemany(
                "INSERT INTO price_limit_daily(date, symbol, limit_ratio, rule, status)"
                " VALUES ('2026-01-05', ?, 10, 'main', 'normal')",
                [(f"{600000 + i:06d}",) for i in range(2)])
            # 2026-01-06 采满 2 行（阈值 max(1, 1.0) = 1）→ 算覆盖齐
            c.executemany(
                "INSERT INTO price_limit_daily(date, symbol, limit_ratio, rule, status)"
                " VALUES ('2026-01-06', ?, 10, 'main', 'normal')",
                [("600000",), ("600001",)])
            c.commit()
        assert cli.missing_limit_dates(conn) == ["2026-01-05"]
        # limit>0 只取最近的 N 天
        with dbmod.session(path) as c:
            c.execute("INSERT INTO daily_bar(symbol,date,close)"
                      " VALUES ('600002','2026-01-07',1)")
            c.commit()
        assert cli.missing_limit_dates(conn, limit=1) == ["2026-01-07"]
    finally:
        conn.close()
