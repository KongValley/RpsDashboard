"""验证「增量重算」与「全量重算」对同一天产出完全相同的结果。

背景：RPS 是截面量，某日的横截面百分位只依赖该日及之前的收盘价，
所以每天只补新增交易日在数学上是成立的。但「成立」不能靠嘴说，
这个脚本在合成数据上把两条路径跑一遍逐值比对：

1. 用合成行情建一个临时库，跑 **全量**，把 rps_daily / industry_rps_daily 快照下来；
2. 删掉最后 5 个交易日的 RPS（模拟「这 5 天还没算」），再跑 **增量**；
3. 断言增量之后的两张表与全量快照**逐值相等**。

用法：python backend/tests/verify_incremental.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rpsboard import config, db                      # noqa: E402
from rpsboard.analytics import matrix                # noqa: E402
from rpsboard.analytics import rps as rps_engine     # noqa: E402

from .fixtures import make_industry_map_zh, make_long  # noqa: E402

N_SYMBOLS = 80
N_DAYS = 320
TAIL_DAYS = 5          # 模拟「最近几天还没算」


def _snapshot(conn) -> dict[str, pd.DataFrame]:
    return {
        t: pd.read_sql_query(f"SELECT * FROM {t} ORDER BY 1, 2", conn)
        for t in ("rps_daily", "industry_rps_daily")
    }


def _compute(conn, mode: str, targets: list[str] | None = None) -> tuple[int, int]:
    start = rps_engine.window_start_for(conn, targets[0]) if targets else None
    long = matrix.load_long(conn, start)
    close = matrix.build_close_matrix(long)
    tradable = matrix.build_tradable_matrix(long)
    st = matrix.build_st_flags(long)
    ind_map = matrix.load_industry_map(conn)
    names = pd.read_sql_query(
        "SELECT symbol, name FROM security_master", conn
    ).set_index("symbol")["name"]
    return rps_engine.materialize(
        conn, close, tradable, st, ind_map, names,
        mode=mode, target_dates=targets,
    )


def _assert_same(a: pd.DataFrame, b: pd.DataFrame, label: str) -> None:
    if list(a.columns) != list(b.columns):
        raise AssertionError(f"{label} 列不一致:\n  全量={list(a.columns)}\n  增量={list(b.columns)}")
    if len(a) != len(b):
        raise AssertionError(f"{label} 行数不一致 全量={len(a)} 增量={len(b)}")
    for col in a.columns:
        va, vb = a[col], b[col]
        if pd.api.types.is_numeric_dtype(va) and not pd.api.types.is_bool_dtype(va):
            if not (va.isna() == vb.isna()).all():
                raise AssertionError(f"{label} 列 {col} 空值分布不一致")
            both = ~va.isna()
            if not np.allclose(
                va[both].to_numpy(dtype="float64"),
                vb[both].to_numpy(dtype="float64"),
                rtol=1e-12, atol=1e-12,
            ):
                raise AssertionError(f"{label} 列 {col} 数值不一致")
        else:
            if not va.astype(object).equals(vb.astype(object)):
                bad = (va.astype(object) != vb.astype(object))
                raise AssertionError(
                    f"{label} 列 {col} 不一致，首个差异:\n"
                    f"{pd.DataFrame({'全量': va[bad].head(3), '增量': vb[bad].head(3)})}"
                )


def main() -> int:
    print("构造合成行情 ...")
    df = make_long(n_symbols=N_SYMBOLS, n_days=N_DAYS, seed=2026)
    listed = {"000010": 90, "000025": 180, "000050": 260}   # 制造样本不齐
    df = make_long(n_symbols=N_SYMBOLS, n_days=N_DAYS, seed=2026, listed_bars=listed)
    ind_map = make_industry_map_zh(sorted(df["symbol"].unique()), 5)
    ind_df = ind_map.rename("industry").rename_axis("symbol").reset_index()
    ind_df["industry_cls"] = "CSRC"
    ind_df["updated_at"] = db.now()

    tmpdir = tempfile.mkdtemp(prefix="rps_incr_")
    dbpath = Path(tmpdir) / "verify.db"
    config.DATA_DIR = Path(tmpdir)          # 让 ensure_dirs 不去碰真实目录
    db.init_db(dbpath)
    conn = db.connect(dbpath)
    try:
        conn.executemany(
            "INSERT INTO security_master (symbol,name,exchange,is_st,updated_at) VALUES (?,?,?,?,?)",
            [(s, f"股票{s}", "sz", 0, db.now()) for s in sorted(df["symbol"].unique())],
        )
        conn.execute("DELETE FROM industry_map")
        ind_df.to_sql("industry_map", conn, if_exists="append", index=False)
        df.assign(symbol=df["symbol"]).to_sql(
            "daily_bar", conn, if_exists="append", index=False
        )
        conn.commit()
        print(f"  daily_bar {len(df)} 行，{df['symbol'].nunique()} 只，"
              f"{df['date'].nunique()} 个交易日")

        # ── 1. 全量 ──────────────────────────────────────────────
        t0 = time.perf_counter()
        n1, n2 = _compute(conn, "full")
        t_full = time.perf_counter() - t0
        snap_full = _snapshot(conn)
        print(f"全量重算      {t_full:6.2f}s  个股 {n1} 行 / 板块 {n2} 行")

        # ── 2. 制造「尾部未计算」 ─────────────────────────────────
        all_dates = [r["date"] for r in conn.execute(
            "SELECT DISTINCT date FROM daily_bar ORDER BY date")]
        tail = all_dates[-TAIL_DAYS:]
        conn.execute(
            "DELETE FROM rps_daily WHERE date IN (%s)" % ",".join("?" * len(tail)), tail
        )
        conn.execute(
            "DELETE FROM industry_rps_daily WHERE date IN (%s)" % ",".join("?" * len(tail)), tail
        )
        conn.commit()
        pending = rps_engine.pending_dates(conn)
        if pending != tail:
            raise AssertionError(f"待算日期识别不对: {pending} != {tail}")

        # ── 3. 增量 ──────────────────────────────────────────────
        t0 = time.perf_counter()
        n3, n4 = _compute(conn, "incremental", pending)
        t_incr = time.perf_counter() - t0
        snap_incr = _snapshot(conn)
        print(f"增量重算      {t_incr:6.2f}s  个股 {n3} 行 / 板块 {n4} 行"
              f"（{len(tail)} 天）  提速 {t_full / max(t_incr, 1e-9):.1f}x")

        # ── 4. 断言 ──────────────────────────────────────────────
        for table in ("rps_daily", "industry_rps_daily"):
            _assert_same(snap_full[table], snap_incr[table], table)
            print(f"  [OK] {table}：{len(snap_incr[table])} 行与全量逐值一致")

        # ── 5. 重复跑增量必须幂等 ────────────────────────────────
        pending2 = rps_engine.pending_dates(conn)
        if pending2:
            raise AssertionError(f"重跑仍有待算日期: {pending2}")
        _compute(conn, "incremental", tail)          # 同日期再算一次
        _assert_same(snap_full["rps_daily"], _snapshot(conn)["rps_daily"],
                     "rps_daily(幂等)")
        print("  [OK] 同日期重复增量计算幂等")

        print(f"\n全部通过：增量与全量对同一天产出完全一致，增速 {t_full / max(t_incr, 1e-9):.1f}x")
        return 0
    finally:
        conn.close()
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
