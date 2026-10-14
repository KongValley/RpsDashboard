"""复权因子异常检测测试（全部离线，不触网）。

守住三件事：
1. 库内单日跳变要**分类正确**：上市前 5 日、复牌日是「正常无限制」，
   中段的巨幅跳变才是可疑（因子跳变/数据错误）。
2. 价格水平异常要区分「非正价」（RPS 已置空）与「极小正价」（仍在参与计算，
   收益被放大 —— 600076 的 +998% 就来自这里）。
3. 官方复权因子的解析：因子跳变、负偏移档位，以及**只对库内价格被压低的股票**
   记录负偏移（不加这个过滤会命中几百只，检测就没意义了）。
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest
from rpsboard.analytics import quality
from rpsboard.analytics.limits import board_ratio


def _db(tmp_path, bars):
    from rpsboard import db as dbmod

    conn = sqlite3.connect(str(tmp_path / "q.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(dbmod.SCHEMA)
    conn.executemany(
        "INSERT INTO daily_bar(symbol, date, close, tradestatus, is_st) VALUES (?,?,?,?,0)",
        bars,
    )
    conn.commit()
    return conn


def test_scan_hfq_jumps_classifies_kinds(tmp_path):
    """首日/复牌日的巨幅波动算正常，中段的才算可疑。"""
    dates = [f"2026-01-{d:02d}" for d in range(5, 15)]
    bars = []
    # A：上市首日 +200%（无涨跌幅限制），其后都是 ±1%
    bars += [("000001", dates[0], 10.0, 1), ("000001", dates[1], 30.0, 1)]
    bars += [("000001", d, 30.3, 1) for d in dates[2:]]
    # B：中段（第 6 根）出现 +300% 跳变 → 可疑
    bars += [("000002", d, 10.0, 1) for d in dates[:5]]
    bars += [("000002", dates[5], 40.0, 1)]
    bars += [("000002", d, 40.0, 1) for d in dates[6:]]
    # C：复牌日 +150%（tradestatus=0）→ 正常
    bars += [("000003", d, 10.0, 1) for d in dates[:5]]
    bars += [("000003", dates[5], 25.0, 0)]
    bars += [("000003", d, 25.0, 1) for d in dates[6:]]

    conn = _db(tmp_path, bars)
    df = quality.scan_hfq_jumps(conn)
    got = {(r["symbol"], r["date"]): r["kind"] for _, r in df.iterrows()}
    assert got[("000001", dates[1])] == "no_limit_day", "上市首日无涨跌幅限制"
    assert got[("000002", dates[5])] == "hfq_jump", "中段巨幅跳变应判可疑"
    assert got[("000003", dates[5])] == "no_limit_day", "复牌日无涨跌幅限制"

    suspects = df[df["kind"] == "hfq_jump"]
    assert len(suspects) == 1 and suspects.iloc[0]["symbol"] == "000002"
    assert suspects.iloc[0]["ratio"] == pytest.approx(3.0, abs=1e-6)
    conn.close()


def test_scan_price_levels_splits_kinds(tmp_path):
    """非正价与极小正价必须分开：前者已置空，后者仍在参与计算。"""
    bars = [
        ("600076", "2026-01-05", 0.5, 1),
        ("600076", "2026-01-06", 0.18, 1),
        ("600136", "2026-01-05", -0.04, 1),
        ("600519", "2026-01-05", 1500.0, 1),
    ]
    conn = _db(tmp_path, bars)
    df = quality.scan_price_levels(conn)
    kinds = {(r["symbol"], r["date"]): r["kind"] for _, r in df.iterrows()}
    assert kinds[("600076", "2026-01-05")] == "tiny_price"
    assert kinds[("600076", "2026-01-06")] == "tiny_price"
    assert kinds[("600136", "2026-01-05")] == "nonpositive_price"
    assert ("600519", "2026-01-05") not in kinds
    assert quality.tiny_price_symbols(conn) == ["600076", "600136"]
    conn.close()


# ── 官方因子解析（用假客户端，不联网）────────────────────────────────
class _Factor:
    def __init__(self, date, scale, offset):
        self.date = pd.Timestamp(date).date()
        self.hfq_scale = scale
        self.hfq_offset = offset


class _Resp:
    def __init__(self, code, items):
        self.code = code
        self.items = tuple(items)


class _Batch:
    def __init__(self, responses):
        self.responses = tuple(responses)


class _Corporate:
    def __init__(self, batch):
        self._batch = batch

    def adjustment_factors(self, codes, batch_size=75):
        return self._batch


class _Client:
    def __init__(self, batch):
        self.corporate = _Corporate(batch)


class _Source:
    batch: _Batch

    def __init__(self, *a, **k):
        self._client = _Client(self.batch)

    def close(self):
        pass


def _patch_source(monkeypatch, batch):
    import rpsboard.sources.tdxnet_src as tdx

    src = type("Stub", (_Source,), {"batch": batch})
    monkeypatch.setattr(tdx, "TdxSource", src)


def test_full_code_maps_markets():
    assert quality.full_code("600076") == "sh600076"
    assert quality.full_code("688981") == "sh688981"
    assert quality.full_code("000001") == "sz000001"
    assert quality.full_code("301551") == "sz301551"
    assert quality.full_code("920002") == "bj920002"
    assert quality.full_code("830799") == "bj830799"


def test_fetch_factor_events_parses_jumps_and_focus(monkeypatch):
    """因子跳变全部记录；负偏移只对 focus 里（库内价格被压低）的股票记录。"""
    batch = _Batch([
        _Resp("sh600076", [
            _Factor("2019-08-05", 3.952, -5.0304),      # 负偏移（且生效至今）
            _Factor("2024-06-28", 7.904, -5.0304),      # scale 翻倍 → 因子跳变
        ]),
        _Resp("sh601398", [
            _Factor("2010-01-01", 1.0, 0.0),
            _Factor("2011-01-01", 2.0, 0.0),            # 因子跳变，但无负偏移
        ]),
        _Resp("sz000007", [
            _Factor("2017-05-22", 12.6978, -4.4891),    # 负偏移但不在 focus 里
        ]),
    ])
    _patch_source(monkeypatch, batch)

    df = quality.fetch_factor_events(
        ["600076", "601398", "000007"], since="2023-02-13",
        focus_symbols={"600076"},
    )
    kinds = {(r["symbol"], r["kind"]): r for _, r in df.iterrows()}
    assert ("600076", "factor_jump") in kinds
    assert kinds[("600076", "factor_jump")]["ratio"] == pytest.approx(1.0)
    assert ("601398", "factor_jump") in kinds
    # 负偏移只在 focus 里记录，且 detail 里带出「变换零点≈原始价」；
    # 600076 的两档都是负偏移且都生效，两行都要有；000007 不在 focus → 不记
    neg = df[df["kind"] == "negative_offset"]
    assert set(neg["symbol"]) == {"600076"}
    assert len(neg) == 2
    assert all("零点" in d for d in neg["detail"])

    # 历史档位（早已被后续档位覆盖）不算：负偏移若在 since 之前结束就跳过
    batch2 = _Batch([_Resp("sh600000", [_Factor("2001-01-01", 1.0, -3.0),
                                        _Factor("2015-01-01", 2.0, 1.0)])])
    _patch_source(monkeypatch, batch2)
    df2 = quality.fetch_factor_events(["600000"], since="2023-02-13",
                                      focus_symbols={"600000"})
    assert not (df2["kind"] == "negative_offset").any(), \
        "负偏移档位在 since 之前结束（被后续档位覆盖）→ 不该报"


def test_materialize_is_idempotent(tmp_path):
    """同一来源重复写入必须覆盖而不是叠加（检测可反复运行）。"""
    conn = _db(tmp_path, [("600076", "2026-01-05", 0.5, 1)])
    events = quality.scan_price_levels(conn)
    assert quality.materialize(conn, events, "internal") == 1
    assert quality.materialize(conn, events, "internal") == 1
    n = conn.execute("SELECT COUNT(*) AS n FROM adjust_event").fetchone()["n"]
    assert n == 1, "重复运行不该叠加旧行"
    # 空结果也要清掉旧行（异常消失后不该留着）
    assert quality.materialize(conn, pd.DataFrame(columns=quality.EVENT_COLS),
                               "internal") == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM adjust_event").fetchone()["n"] == 0
    with pytest.raises(ValueError):
        quality.materialize(conn, events, "bogus")
    conn.close()


def test_board_ratio_matches_limits_module():
    """quality 与 limits 用的是同一套板块规则（避免两处口径漂移）。"""
    assert board_ratio("920002") == 30.0
    assert quality.TINY_PRICE == 1.0
