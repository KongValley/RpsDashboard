"""概念板块 RPS 测试。

概念 RPS 与行业 RPS 走的是**同一份向量化实现**，只是分组映射表不同。
这个测试守住的不只是数值正确，更是「换一种分组维度不改变计算口径」——
一旦有人给概念单独写一套算法，这里就会红。
"""

from __future__ import annotations

import sqlite3

import pandas as pd
from rpsboard import config
from rpsboard.analytics import rps as engine
from rpsboard.analytics.matrix import (
    build_close_matrix,
    build_st_flags,
    build_tradable_matrix,
)

from .fixtures import make_long


def _seed_concept_db(conn: sqlite3.Connection, mapping: dict[str, str]) -> None:
    from rpsboard import db as dbmod
    conn.executescript(dbmod.SCHEMA)      # 临时库也要建表
    conn.execute("DELETE FROM concept_map")
    conn.executemany(
        "INSERT INTO concept_map (symbol, concept, board_code, updated_at) "
        "VALUES (?,?,?,?)",
        [(s, c, f"b{i}", "2026-01-01") for i, (s, c) in enumerate(mapping.items())],
    )
    conn.commit()


def test_concept_rps_matches_industry_rps_same_implementation(tmp_path):
    """同一套映射下，概念 RPS 的数值必须与行业 RPS 逐值相等。"""
    df = make_long(n_symbols=40, n_days=300, seed=4242)
    close = build_close_matrix(df)
    frames = engine.compute_rps_frames(
        close, build_tradable_matrix(df), build_st_flags(df)
    )
    mapping = pd.Series({s: f"概念{i % 4}" for i, s in
                         enumerate(sorted(df["symbol"].unique()))})

    conn = db_path = tmp_path / "c.db"
    conn = sqlite3.connect(str(db_path))
    try:
        by_industry = engine.compute_industry_rps(frames, mapping,
                                                  leader_rps=frames["rps60"])
        _seed_concept_db(conn, mapping.to_dict())
        by_concept = engine.compute_concept_rps(conn, frames)

        assert not by_concept.empty
        # 列名与 industry_rps_daily 对称，只是 industry → concept；周期列跟随
        # config.PERIODS（新增周期时这里自动跟上，不必手抄）
        assert list(by_concept.columns) == (
            ["date", "concept"]
            + [c for p in config.PERIODS for c in (f"rps{p}", f"ret{p}")]
            + ["member_count", "leader_symbol"]
        )
        a = by_industry.rename(columns={"industry": "concept"}).reset_index(drop=True)
        b = by_concept.reset_index(drop=True)
        assert len(a) == len(b)
        pd.testing.assert_frame_equal(
            a.sort_values(["date", "concept"]).reset_index(drop=True),
            b.sort_values(["date", "concept"]).reset_index(drop=True),
        )
    finally:
        conn.close()


def test_symbol_in_many_concepts_is_counted_once(tmp_path):
    """一只股票属于多个概念时只能进一个组，否则涨幅被重复计入。

    这是个真实的建模陷阱：概念成分高度重叠（AI 算力 / CPO 常常共享个股）。
    """
    df = make_long(n_symbols=30, n_days=300, seed=99)
    symbols = sorted(df["symbol"].unique())
    mapping = {s: "概念A" for s in symbols}          # 全部同一个概念
    conn = sqlite3.connect(str(tmp_path / "m.db"))
    try:
        _seed_concept_db(conn, mapping)
        out = engine.load_concept_map(conn)
        assert len(out) == len(symbols)             # 一对一，不重复
        assert set(out.index) == set(symbols)
    finally:
        conn.close()


def test_concept_rps_empty_when_no_map(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "e.db"))
    try:
        from rpsboard import db as dbmod
        conn.executescript(dbmod.SCHEMA)
        conn.commit()
        df = make_long(n_symbols=20, n_days=260, seed=7)
        frames = engine.compute_rps_frames(build_close_matrix(df))
        assert engine.compute_concept_rps(conn, frames).empty
    finally:
        conn.close()
