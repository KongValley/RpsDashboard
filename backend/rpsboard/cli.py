"""命令行入口：init / backfill / sync / spot / compute-rps / status。

Windows 注意：调用 `python -m rpsboard.cli` 会走 __main__ 保护；
本模块同时提供 `main()` 供 `rpsboard` 控制台脚本使用。
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import sys
import time
from datetime import date

import pandas as pd

from . import config, db
from .analytics import matrix, rps as rps_engine
from .sources import limit_src, tdxnet_src
from .sources.base import SourceError

BENCHMARK = "sh.000300"


def _today() -> str:
    return date.today().isoformat()


# ── init ────────────────────────────────────────────────────────────
def cmd_init(_: argparse.Namespace) -> int:
    config.ensure_dirs()
    db.init_db()
    print(f"[init] 数据库就绪: {config.DB_PATH}")
    print("[init] 下一步: python -m rpsboard.cli backfill")
    return 0


# ── backfill / sync ─────────────────────────────────────────────────
def clean_bad_pct_chg(conn) -> int:
    """把 ±inf / 荒谬幅度的 pct_chg 置空。

    pct_chg 由 `groupby("symbol")["close"].pct_change()` 得出，
    遇到前收盘为 0 或缺失时会产出 ±inf。实测库里曾有 3 行这种值，
    它们会污染任何阈值比较与图表绘制。数据源侧已经加了防护，
    这里再兜一次底，保证历史遗留行也被清掉。
    """
    cur = conn.execute(
        "UPDATE daily_bar SET pct_chg = NULL "
        "WHERE pct_chg IS NOT NULL AND (pct_chg > 1.2 OR pct_chg < -1.2)"
    )
    conn.commit()
    return cur.rowcount or 0


def cmd_backfill(args: argparse.Namespace) -> int:
    """回填 / 增量更新日K。eltdx 本身返回最近 N 根，多跑一次即为增量。"""
    config.ensure_dirs()
    db.init_db()
    with db.session() as conn:
        run_id = db.start_run(conn, task="backfill", source=config.DATA_SOURCE)
        try:
            n_sym, n_rows = tdxnet_src.backfill_bars(
                conn, workers=args.workers
            )
            status = "success" if n_rows else "partial"
            db.finish_run(conn, run_id, status, n_rows, f"{n_sym} 只股票")
        except SourceError as exc:
            db.finish_run(conn, run_id, "failed", 0, str(exc))
            print(f"[backfill] 数据源不可用: {exc}", file=sys.stderr)
            return 2

    n_dirty = clean_bad_pct_chg(conn)
    if n_dirty:
        print(f"[clean] 清理异常 pct_chg {n_dirty} 行")

    print(f"[backfill] 完成，{n_rows} 行。请执行 compute-rps")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """增量更新（等价 backfill）。"""
    return cmd_backfill(args)


# ── compute-rps ─────────────────────────────────────────────────────
#: 增量模式下待算日期超过该数就提醒用 --full（通常是换数据源或历史被订正）
INCREMENTAL_PENDING_HINT = 40


def cmd_compute_rps(args: argparse.Namespace) -> int:
    db.init_db()
    with db.session() as conn:
        run_id = db.start_run(conn, task="compute-rps-cli", source="internal")
        try:
            # ── 决定模式与窗口 ────────────────────────────────────
            # 默认增量：只补 daily_bar 有、rps_daily 没有的交易日。
            # 正确性依据是 RPS 的截面性质——某日的百分位排名只依赖该日及之前的
            # 收盘价，新数据不会改动历史名次。历史被订正则增量察觉不到，
            # 必须显式 --full 重算。
            pending = rps_engine.pending_dates(conn)
            force_full = bool(getattr(args, "full", False))
            if force_full or len(pending) > 200:      # 200 天以上基本等于没算过
                mode, targets, start = "full", None, None
                if pending and not force_full:
                    print(f"[rps] 待算 {len(pending)} 天，按全量处理")
                else:
                    print("[rps] 全量重算")
            elif not pending:
                print(f"[rps] 已是最新（{db.count_rows(conn, 'rps_daily')} 行），无需计算")
                db.finish_run(conn, run_id, "success", 0, "mode=skip 无待算日期")
                return 0
            else:
                mode = "incremental"
                targets = pending
                start = rps_engine.window_start_for(conn, pending[0])
                print(f"[rps] 增量计算 {len(targets)} 天：{targets[0]} ~ {targets[-1]}")
                print(f"[rps] 计算窗口从 {start} 起（需要 {max(config.PERIODS)} 根前置 K 线）")
                if len(targets) > INCREMENTAL_PENDING_HINT:
                    print(f"[rps] 提示：待算日期偏多，若刚订正过历史行情请加 --full 重算")

            long = matrix.load_long(conn, start)
            if long.empty:
                db.finish_run(conn, run_id, "failed", 0, "daily_bar 为空")
                print("[rps] daily_bar 为空，请先 backfill", file=sys.stderr)
                return 1
            close = matrix.build_close_matrix(long)
            tradable = matrix.build_tradable_matrix(long)
            st = matrix.build_st_flags(long)
            ind_map = matrix.load_industry_map(conn)
            try:
                names = pd.read_sql_query(
                    "SELECT symbol, name FROM security_master", conn
                ).set_index("symbol")["name"]
            except Exception:  # noqa: BLE001
                names = None

            n_stock, n_ind = rps_engine.materialize(
                conn, close, tradable, st, ind_map, names,
                exclude_st=not args.include_st,
                mode=mode, target_dates=targets,
            )
        except Exception as exc:  # noqa: BLE001
            db.finish_run(conn, run_id, "failed", 0, str(exc))
            raise
    print(f"[rps] 个股 {n_stock} 行，板块 {n_ind} 行（{mode}）")
    return 0


# ── price-limits ────────────────────────────────────────────────────
def cmd_price_limits(args: argparse.Namespace) -> int:
    """采集涨跌停。

    `daily_price_limits` 每次调用约 34 秒且**服务端不缓存**
    （同一 trade_date 连调两次分别 35.3s / 31.4s），
    因此全历史 887 个交易日需要 8 小时以上。默认只补最近一天，
    历史回补用 --days N 分批进行。
    """
    conn = db.connect()
    try:
        days = int(getattr(args, "days", 0) or 0)
        if days > 0:
            rows = conn.execute(
                "SELECT DISTINCT date FROM daily_bar ORDER BY date DESC LIMIT ?",
                (days,),
            ).fetchall()
        else:
            row = conn.execute(
                "SELECT MAX(date) AS d FROM daily_bar").fetchone()
            rows = [{"date": row["d"]}] if row and row["d"] else []
    finally:
        conn.close()
    if not rows:
        print("[limits] daily_bar 为空")
        return 1

    dates = [r["date"] for r in rows]
    total = 0
    t_all = time.time()
    for i, d in enumerate(dates, 1):
        total += limit_src.sync_price_limits(d)
        done = time.time() - t_all
        eta = done / i * (len(dates) - i)
        print(f"[limits] {i}/{len(dates)} 累计 {total} 行"
              f" | 已用 {done/60:.1f} 分钟 | 预计剩余 {eta/60:.1f} 分钟",
              flush=True)
    return 0


# ── concept（概念/题材板块）─────────────────────────────────────────
def cmd_concept(args: argparse.Namespace) -> int:
    """采集概念成分股；--compute 只算 RPS 不重采。"""
    from .sources import board_src

    db.init_db()
    if not getattr(args, "compute_only", False):
        with db.session() as conn:
            run_id = db.start_run(conn, task="concept", source=config.DATA_SOURCE)
            try:
                n_sym = board_src.sync_concept_map()
                db.finish_run(conn, run_id, "success", n_sym, f"覆盖 {n_sym} 只股票")
            except Exception as exc:  # noqa: BLE001
                db.finish_run(conn, run_id, "failed", 0, str(exc))
                print(f"[concept] 采集失败: {exc}", file=sys.stderr)
                return 2

    with db.session() as conn:
        if not db.table_has_rows(conn, "concept_map"):
            print("[concept] concept_map 为空，先跑 `cli concept` 采集", file=sys.stderr)
            return 1
        mode = "full" if getattr(args, "full", False) else None
        if mode is None:
            mode = "incremental"
            targets = rps_engine.pending_dates(conn, table="concept_rps_daily")
            if not targets:
                print("[concept] RPS 已是最新")
                return 0
            start = rps_engine.window_start_for(conn, targets[0])
        else:
            targets, start = None, None

        long = matrix.load_long(conn, start)
        close = matrix.build_close_matrix(long)
        frames = rps_engine.compute_rps_frames(
            close, matrix.build_tradable_matrix(long), matrix.build_st_flags(long),
            exclude_st=not args.include_st, periods=config.PERIODS,
        )
        try:
            names = pd.read_sql_query(
                "SELECT symbol, name FROM security_master", conn
            ).set_index("symbol")["name"]
        except Exception:  # noqa: BLE001
            names = None
        cdf = rps_engine.compute_concept_rps(conn, frames, names=names)
        if cdf.empty:
            print("[concept] 未算出概念 RPS（concept_map 为空？）")
            return 1
        if targets is not None:
            cdf = cdf[cdf["date"].isin(set(targets))]
        conn.execute("DELETE FROM concept_rps_daily") if targets is None else None
        conn.commit()
        n = db.replace_date_partition(
            conn, "concept_rps_daily", sorted(set(cdf["date"])), cdf
        ) if targets is not None else _write_all(conn, "concept_rps_daily", cdf)
        print(f"[concept] 概念 RPS {n} 行（{mode}），覆盖 {cdf['concept'].nunique()} 个概念")
    return 0


def _write_all(conn, table: str, df) -> int:
    conn.execute(f"DELETE FROM {table}")
    conn.commit()
    df.to_sql(table, conn, if_exists="append", index=False)
    conn.commit()
    return len(df)


# ── patterns（技术形态）─────────────────────────────────────────────
def cmd_patterns(_: argparse.Namespace) -> int:
    """计算三升底等形态。

    只读 daily_bar 的 OHLC，不需要重新算 RPS，因此可以随时单独跑。
    """
    from .analytics import patterns

    db.init_db()
    conn = db.connect()
    try:
        ohlc = pd.read_sql_query(
            "SELECT symbol, date, open, high, low, close FROM daily_bar "
            "ORDER BY symbol, date", conn
        )
        if ohlc.empty:
            print("[pattern] daily_bar 为空", file=sys.stderr)
            return 1
        run_id = db.start_run(conn, task="patterns", source="internal")
        try:
            n = patterns.materialize(conn, ohlc)
        except Exception as exc:  # noqa: BLE001
            db.finish_run(conn, run_id, "failed", 0, str(exc))
            print(f"[pattern] 计算失败: {exc}", file=sys.stderr)
            return 2
        latest = conn.execute(
            "SELECT MAX(date) d FROM pattern_daily").fetchone()["d"]
        today_n = conn.execute(
            "SELECT COUNT(*) n FROM pattern_daily WHERE date = ?", (latest,)
        ).fetchone()["n"]
        db.finish_run(conn, run_id, "success", n, f"{n} 个形态")
    finally:
        conn.close()
    print(f"[pattern] 三升底 {n} 个（最新 {latest} 当日 {today_n} 个）")
    return 0


# ── status ──────────────────────────────────────────────────────────
def cmd_status(_: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        bar = db.count_rows(conn, "daily_bar")
        rps_n = db.count_rows(conn, "rps_daily")
        ind = db.count_rows(conn, "industry_rps_daily")
        lim = db.count_rows(conn, "price_limit_daily")
        last = conn.execute("SELECT MAX(date) AS d FROM daily_bar").fetchone()["d"]
        n_sym = conn.execute("SELECT COUNT(*) AS c FROM security_master").fetchone()["c"]
        print(f"数据库      {config.DB_PATH}")
        print(f"证券        {n_sym} 只")
        print(f"日K         {bar} 行，最后交易日 {last}")
        print(f"RPS         {rps_n} 行")
        print(f"板块RPS     {ind} 行")
        print(f"涨跌停      {lim} 行")
    finally:
        conn.close()
    return 0


# ── backtest ────────────────────────────────────────────────────────
def cmd_backtest(args: argparse.Namespace) -> int:
    from .analytics import backtest as engine

    conn = db.connect()
    try:
        res = engine.run_backtest(
            conn, period=args.period, threshold=args.threshold,
            hold=args.hold, rebalance=args.rebalance,
            resonance=args.resonance, exclude_st=not args.include_st,
        )
    finally:
        conn.close()

    print(f"参数: {res.params}")
    print(f"指标: {res.metrics}")
    for w in res.warnings:
        print(f"⚠ {w}")
    print("分档前瞻收益:")
    for b in res.buckets:
        print(f"  {b['bucket']:>8} 样本 {b['samples']:>8}  "
              f"均值 {b['avg_forward_return']}  胜率 {b['win_rate']}")
    return 0


# ── 供 /api/admin/update 派发 ───────────────────────────────────────
def _ns(**over):
    base = dict(
        workers=config.TDX_WORKERS, limit=None, include_st=False,
        period=config.DEFAULT_PERIOD, threshold=80.0, hold=20,
        rebalance=None, resonance=False, full=False, market_filter="none",
    )
    base.update(over)
    return argparse.Namespace(**base)


def run_update_pipeline(progress=None) -> dict:
    """完整更新流程：拉日K → 采涨跌停 → 重算 RPS。

    供 API 后台线程与 update.bat 共用。任一步失败即中止并把错误抛出，
    调用方负责写 collect_log。`progress(phase)` 用于向前端播报当前阶段。
    """
    say = progress or (lambda _phase: None)
    say("bars")
    rc = cmd_backfill(_ns())
    if rc != 0:
        raise RuntimeError(f"日K 更新失败（退出码 {rc}）")

    say("limits")
    from .sources import limit_src
    n_lim = limit_src.sync_price_limits()
    say("rps")
    rc = cmd_compute_rps(_ns())
    if rc != 0:
        raise RuntimeError(f"RPS 计算失败（退出码 {rc}）")

    say("patterns")
    rc = cmd_patterns(_ns())
    if rc != 0:
        raise RuntimeError(f"形态计算失败（退出码 {rc}）")

    say("done")
    return {"ok": True, "price_limit_rows": n_lim}


def run_task(task: str) -> dict:
    """执行单个采集任务（同步）。"""
    handlers = {
        "backfill": cmd_backfill,
        "sync": cmd_sync,
        "compute-rps": cmd_compute_rps,
        "backtest": cmd_backtest,
        "price-limits": lambda _ns: 0,
    }
    try:
        rc = handlers[task](_ns())
        return {"ok": rc == 0, "task": task, "returncode": rc}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "task": task, "error": str(exc)}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rpsboard", description="A股 RPS 看板")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化数据库").set_defaults(func=cmd_init)
    sub.add_parser("status", help="查看数据状态").set_defaults(func=cmd_status)

    for name, fn, helptext in (
        ("backfill", cmd_backfill, "回填/刷新历史日K（全市场约 209 秒）"),
        ("sync", cmd_sync, "增量更新日K（等价 backfill）"),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--workers", type=int, default=config.TDX_WORKERS,
                        help="采集并发线程数（默认 8）")
        sp.set_defaults(func=fn)

    sub.add_parser("update", help="完整更新：日K + 涨跌停 + RPS").set_defaults(
        func=lambda _ns: (run_update_pipeline(), 0)[1]
    )

    sp = sub.add_parser("price-limits", help="采集涨跌停价")
    sp.add_argument("--days", type=int, default=0,
                    help="回补最近 N 个交易日（每次调用约 34 秒且服务端不缓存，"
                         "全历史 887 天需 8 小时以上；默认只补最近一天）")
    sp.set_defaults(func=lambda ns: (cmd_price_limits(ns), 0)[1])

    sp = sub.add_parser("concept", help="采集概念板块并计算概念 RPS")
    sp.add_argument("--compute-only", action="store_true",
                    help="只重算 RPS，不重新采集成分股（采集约 31 分钟）")
    sp.add_argument("--full", action="store_true", help="概念 RPS 全量重算")
    sp.add_argument("--include-st", action="store_true", help="把 ST 也纳入排名")
    sp.set_defaults(func=cmd_concept)

    sub.add_parser("patterns", help="计算三升底形态").set_defaults(func=cmd_patterns)

    sp = sub.add_parser("compute-rps", help="计算个股/板块 RPS")
    sp.add_argument("--include-st", action="store_true", help="把 ST 也纳入排名")
    sp.add_argument("--full", action="store_true",
                    help="全量重算（首次计算、或历史行情被订正后使用；默认只补新增交易日）")
    sp.set_defaults(func=cmd_compute_rps)

    sp = sub.add_parser("backtest", help="命令行回测")
    sp.add_argument("--period", type=int, default=config.DEFAULT_PERIOD)
    sp.add_argument("--threshold", type=float, default=80.0)
    sp.add_argument("--hold", type=int, default=20)
    sp.add_argument("--rebalance", type=int, default=None)
    sp.add_argument("--resonance", action="store_true")
    sp.add_argument("--include-st", action="store_true")
    sp.set_defaults(func=cmd_backtest)

    return p


def main(argv: list[str] | None = None) -> int:
    mp.freeze_support()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
