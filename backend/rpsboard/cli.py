"""命令行入口：init / backfill / sync / spot / compute-rps / status。

Windows 注意：调用 `python -m rpsboard.cli` 会走 __main__ 保护；
本模块同时提供 `main()` 供 `rpsboard` 控制台脚本使用。
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import sys
from datetime import date, datetime, timedelta

import pandas as pd

from . import config, db
from .analytics import matrix, rps as rps_engine
from .sources import (
    akshare_src,
    baostock_src,
    tdx_local_src,
    tdxnet_src,
    tencent_src,
)
from .sources.base import SourceError

BENCHMARK = "sh.000300"


def _today() -> str:
    return date.today().isoformat()


def _default_start() -> str:
    return (date.today() - timedelta(days=config.BACKFILL_DAYS)).isoformat()


# ── init ────────────────────────────────────────────────────────────
def cmd_init(_: argparse.Namespace) -> int:
    config.ensure_dirs()
    db.init_db()
    print(f"[init] 数据库就绪: {config.DB_PATH}")
    print("[init] 下一步: python -m rpsboard.cli backfill")
    return 0


# ── 元数据同步 ──────────────────────────────────────────────────────
def _sync_metadata(conn) -> None:
    run_id = db.start_run(conn, task="metadata", source="baostock")
    try:
        master = baostock_src.fetch_security_master()
        n1 = db.upsert_master(conn, master)
        print(f"[meta] 证券主数据 {n1} 条")

        ind = baostock_src.fetch_industry_map()
        conn.execute("DELETE FROM industry_map")
        ind.assign(updated_at=db.now()).to_sql(
            "industry_map", conn, if_exists="append", index=False
        )
        conn.commit()
        print(f"[meta] 行业分类 {len(ind)} 条")

        cal = baostock_src.fetch_trade_calendar(
            _default_start(), date.today().isoformat()
        )
        conn.execute("DELETE FROM trade_calendar")
        cal.to_sql("trade_calendar", conn, if_exists="append", index=False)
        conn.commit()
        print(f"[meta] 交易日历 {len(cal)} 天")

        bench = baostock_src.fetch_benchmark(BENCHMARK, _default_start(), _today())
        if not bench.empty:
            bench = bench[["symbol", "date", "close"]]
            conn.execute("DELETE FROM benchmark_bar WHERE symbol = ?", (BENCHMARK.split(".")[-1],))
            bench.to_sql("benchmark_bar", conn, if_exists="append", index=False)
            conn.commit()
        print(f"[meta] 基准 {BENCHMARK} {len(bench)} 条")

        db.finish_run(conn, run_id, "success", n1 + len(ind) + len(cal))
    except SourceError as exc:
        db.finish_run(conn, run_id, "failed", 0, str(exc))
        raise


# ── backfill / sync ─────────────────────────────────────────────────
def _symbols(conn) -> list[str]:
    try:
        df = pd.read_sql_query("SELECT symbol FROM security_master", conn)
    except Exception:  # noqa: BLE001
        return []
    return df["symbol"].tolist() if not df.empty else []


def _latest_open_date(conn) -> str:
    """交易日历里最近的**交易日**。

    跳过逻辑必须对比它而不是 date.today()：今天若是周末/节假日，
    数据已经是最新的，但用 today 去比会让每只股票都被判定为「需要更新」，
    每天把全市场重拉一遍（实测踩过）。
    """
    try:
        row = conn.execute(
            "SELECT MAX(date) AS d FROM trade_calendar WHERE is_open = 1"
        ).fetchone()
        if row and row["d"]:
            return row["d"]
    except Exception:  # noqa: BLE001
        pass
    return date.today().isoformat()


def cmd_backfill(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    db.init_db()
    source = config.DATA_SOURCE
    with db.session() as conn:
        run_id = db.start_run(conn, task="backfill", source=source)
        try:
            if source == "tdxnet":
                # eltdx 自带证券清单与复权因子，一步到位
                n_sym, n_rows = tdxnet_src.backfill_bars(
                    conn, workers=args.workers
                )
            else:
                _sync_metadata(conn)
                symbols = _symbols(conn)
                if not symbols:
                    db.finish_run(conn, run_id, "failed", 0, "无证券列表")
                    print("[backfill] 无证券列表，请检查数据源连接")
                    return 1
                if args.limit:
                    symbols = symbols[: args.limit]
                latest_open = _latest_open_date(conn)
                done_syms = {
                    s for s, d in conn.execute(
                        "SELECT symbol, MAX(date) AS d FROM daily_bar GROUP BY symbol"
                    ) if d and d >= latest_open
                }
                todo = [s for s in symbols if s not in done_syms]
                print(f"[backfill] 最近交易日 {latest_open}；已最新 {len(done_syms)} 只，"
                      f"待抓 {len(todo)} 只，共 {len(symbols)} 只")
                if not todo:
                    db.finish_run(conn, run_id, "success", 0, "所有股票均为最新")
                    print("[backfill] 无需更新")
                    return 0
                n_sym, n_rows = baostock_src.backfill_bars(
                    conn, todo, _default_start(), _today(),
                    workers=config.BACKFILL_WORKERS,
                    stagger=config.LOGIN_STAGGER_SEC,
                )
            status = "success" if n_rows else "partial"
            db.finish_run(conn, run_id, status, n_rows, f"{n_sym} 只股票")
        except SourceError as exc:
            db.finish_run(conn, run_id, "failed", 0, str(exc))
            print(f"[backfill] 数据源不可用: {exc}", file=sys.stderr)
            return 2

    # 腾讯源不提供成交额，用本地通达信补
    if source != "tdxnet":
        try:
            tdx_local_src.fill_amount(conn, config.TDX_ROOT)
        except Exception as exc:  # noqa: BLE001
            print(f"[tdx] 成交额补全跳过: {exc}")

    print(f"[backfill] 完成，{n_rows} 行。请执行 compute-rps")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """增量更新（等价 backfill，会自动跳过已最新股票）。"""
    return cmd_backfill(args)


# ── spot（akshare）──────────────────────────────────────────────────
def cmd_spot(_: argparse.Namespace) -> int:
    db.init_db()
    with db.session() as conn:
        res = akshare_src.fetch_spot(conn)
    print(f"[spot] ok={res['ok']} rows={res['rows']} date={res.get('date')}")
    return 0 if res["ok"] else 0  # 失败不阻断主流程


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


# ── status ──────────────────────────────────────────────────────────
def cmd_status(_: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        bar = db.count_rows(conn, "daily_bar")
        rps_n = db.count_rows(conn, "rps_daily")
        ind = db.count_rows(conn, "industry_rps_daily")
        last = conn.execute("SELECT MAX(date) AS d FROM daily_bar").fetchone()["d"]
        n_sym = len(_symbols(conn))
        spot = akshare_src.latest_spot_date(conn)
        print(f"数据库      {config.DB_PATH}")
        print(f"证券        {n_sym} 只")
        print(f"日K         {bar} 行，最后交易日 {last}")
        print(f"RPS         {rps_n} 行")
        print(f"板块RPS     {ind} 行")
        print(f"快照日期    {spot}")
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


# ── 供 /api/admin/collect 派发 ─────────────────────────────────────
def run_task(task: str) -> dict:
    """执行一个采集任务（同步）。供 API 手动触发。"""
    ns = argparse.Namespace(
        workers=config.BACKFILL_WORKERS, limit=None,
        include_st=False, period=config.DEFAULT_PERIOD, threshold=80.0,
        hold=20, rebalance=None, resonance=False,
    )
    handlers = {
        "backfill": cmd_backfill,
        "sync": cmd_sync,
        "spot": cmd_spot,
        "compute-rps": cmd_compute_rps,
    }
    try:
        rc = handlers[task](ns)
        return {"ok": rc == 0, "task": task, "returncode": rc}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "task": task, "error": str(exc)}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rpsboard", description="A股 RPS 看板")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化数据库").set_defaults(func=cmd_init)
    sub.add_parser("status", help="查看数据状态").set_defaults(func=cmd_status)

    for name, fn, helptext in (
        ("backfill", cmd_backfill, "回填历史日K（首次运行，约 12~20 分钟）"),
        ("sync", cmd_sync, "增量更新日K（自动跳过已最新）"),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--workers", type=int, default=config.TENCENT_WORKERS,
                        help="腾讯源并发线程数（默认 12）")
        sp.add_argument("--limit", type=int, default=None,
                        help="只处理前 N 只（调试用）")
        sp.set_defaults(func=fn)

    sub.add_parser("spot", help="采集 akshare 当日快照").set_defaults(func=cmd_spot)

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
