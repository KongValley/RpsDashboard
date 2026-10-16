"""命令行入口：init / backfill / sync / spot / compute-rps / status。

Windows 注意：调用 `python -m rpsboard.cli` 会走 __main__ 保护；
本模块同时提供 `main()` 供 `rpsboard` 控制台脚本使用。
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import sqlite3
import sys
import time

import pandas as pd

from . import config, db
from .analytics import matrix
from .analytics import rps as rps_engine
from .sources import limit_src, tdxnet_src
from .sources.base import tradestatus_from_calendar


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
    raw_workers = getattr(args, "workers", None)
    # 不能用 `raw or default`：0 会被 or 当成「缺省」悄悄换成 8，
    # 下面这道防线就永远拦不住 --workers 0
    workers = int(config.TDX_WORKERS if raw_workers is None else raw_workers)
    if workers < 1:
        # ThreadPoolExecutor(max_workers=0) 会抛 ValueError 直接穿透整个命令
        print("[backfill] --workers 必须 ≥ 1", file=sys.stderr)
        return 2
    with db.session() as conn:
        run_id = db.start_run(conn, task="backfill", source=config.DATA_SOURCE)
        try:
            n_sym, n_rows, failed = tdxnet_src.backfill_bars(conn, workers=workers)
        except Exception as exc:  # noqa: BLE001
            # 只捕 SourceError 的话，别的异常（sqlite/线程池/解析）会让
            # collect_log 留下永久 running 行，页面永远显示「进行中」
            db.finish_run(conn, run_id, "failed", 0, f"{type(exc).__name__}: {exc}")
            print(f"[backfill] 采集失败: {exc}", file=sys.stderr)
            return 2

        status = "success" if not failed else ("failed" if n_rows == 0 else "partial")
        msg = f"{n_sym} 只股票 / {n_rows} 行"
        if failed:
            sample = "、".join(failed[:5])
            msg += (f"；失败 {len(failed)} 只：{sample}"
                    f"{' 等' if len(failed) > 5 else ''}")
        db.finish_run(conn, run_id, status, n_rows, msg)

        # 清理必须用**同一个仍然打开的**连接：原来这行写在 with 之外，
        # 连接此时已经关闭 → sqlite3.ProgrammingError，整个 update 管道
        # 在「日K 拉完之后」中断（涨跌停 / RPS / 形态 / 预警全没跑）。
        n_dirty = clean_bad_pct_chg(conn)

    if n_dirty:
        print(f"[clean] 清理异常 pct_chg {n_dirty} 行")

    if failed:
        # 少量个股失败不中止 update 流水线（数据可用），但必须显式可见：
        # collect_log 记 partial + 失败代码，终端也打印一份。
        print(f"[backfill] ⚠ {len(failed)} 只失败（示例："
              f"{', '.join(failed[:10])}）", file=sys.stderr)

    print(f"[backfill] 完成，{n_rows} 行。请执行 compute-rps")
    attempted = n_sym + len(failed)
    if n_rows == 0:
        return 2
    if failed and len(failed) > 0.1 * max(1, attempted):
        print(f"[backfill] 失败率 {len(failed)}/{attempted} 超过 10%，按失败处理",
              file=sys.stderr)
        return 2
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
            # 中间空洞（daily_bar 有、rps_daily 没有）增量永远补不上，先报出来
            gaps = rps_engine.suspect_gaps(conn)
            if gaps:
                print(f"[rps] 疑似缺口 {len(gaps)} 天（最早 {gaps[0]}）：增量补不上，"
                      f"若确认数据已补齐请加 --full 重算")
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
                    print("[rps] 提示：待算日期偏多，若刚订正过历史行情请加 --full 重算")

            long = matrix.load_long(conn, start, seed_before_start=(mode == "incremental"))
            if long.empty:
                db.finish_run(conn, run_id, "failed", 0, "daily_bar 为空")
                print("[rps] daily_bar 为空，请先 backfill", file=sys.stderr)
                return 1
            close = matrix.build_close_matrix(long, matrix.negative_offsets(conn))
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
        else:
            # 成功路径必须收尾：以前这里直接掉出 try，collect_log 留下永久的
            # running 行（实测 4 条），页面「最近更新」永远显示「进行中」。
            db.finish_run(
                conn, run_id, "success", n_stock + n_ind,
                f"mode={mode} 个股 {n_stock} 行 / 板块 {n_ind} 行",
            )
    print(f"[rps] 个股 {n_stock} 行，板块 {n_ind} 行（{mode}）")
    # 复权异常端点已自动剔除（收益为 NULL）：这里报出被剔除的行数，其余异常见 cli quality
    with db.session() as conn:
        floor = config.ANOMALY_MIN_PRICE
        # 必须按**矩阵口径**统计：负偏移股票先加回 |offset| 再比门槛
        # （matrix.negative_offsets）。否则报出来的是后复权价的行数（414），
        # 与实际被置空的端点（49）对不上，等于给用户一个假的核对基线。
        offs = matrix.negative_offsets(conn)
        if floor > 0:
            off_syms = list(offs)
            if off_syms:
                marks = ",".join("?" * len(off_syms))
                n_excl = conn.execute(
                    f"SELECT COUNT(*) AS n FROM daily_bar WHERE close < ? "
                    f"AND symbol NOT IN ({marks})", (floor, *off_syms)).fetchone()["n"]
                for sym, off in offs.items():
                    n_excl += conn.execute(
                        "SELECT COUNT(*) AS n FROM daily_bar WHERE symbol = ? AND close < ?",
                        (sym, floor - float(off))).fetchone()["n"]
            else:
                n_excl = conn.execute(
                    "SELECT COUNT(*) AS n FROM daily_bar WHERE close < ?",
                    (floor,)).fetchone()["n"]
        else:                       # 门槛为 0 时只剔除非正价（旧行为）
            n_excl = conn.execute(
                "SELECT COUNT(*) AS n FROM daily_bar WHERE close <= 0").fetchone()["n"]
        if n_excl:
            print(f"[rps] 复权异常：{n_excl} 行端点价 < {floor:g} 元（含穿零）已置空，"
                  f"跨这些点的区间收益不计：cli quality")
    # RPS 变了 → 回测的进程内矩阵/帧缓存必须失效（否则最长 300s 用旧值）
    from .analytics import backtest as bt_engine
    bt_engine.clear_context_cache()
    return 0


# ── tradestatus（数据修复，不联网）──────────────────────────────────
def cmd_tradestatus(args: argparse.Namespace) -> int:
    """按交易日口径重算 daily_bar.tradestatus，只写回真正变化的行（不联网、幂等）。

    旧口径按**自然日**间隔判停牌，长假后第一个交易日被整日误判成停牌，
    实测 21 个交易日 / 112k 行因此没有 RPS（见 sources/base.py 的说明）。
    """
    with db.session() as conn:
        df = pd.read_sql_query(
            "SELECT symbol, date, tradestatus FROM daily_bar ORDER BY symbol, date", conn
        )
        if df.empty:
            print("[tradestatus] daily_bar 为空")
            return 1
        new = tradestatus_from_calendar(df)
        old = df["tradestatus"].fillna(1).astype(int)
        n_up = int(((old == 0) & (new == 1)).sum())
        n_dn = int(((old == 1) & (new == 0)).sum())
        print(f"[tradestatus] 0→1 {n_up} 行，1→0 {n_dn} 行（共 {len(df)} 行）")
        if getattr(args, "dry_run", False):
            print("[tradestatus] --dry-run：未写回")
            return 0
        changed = (old != new).to_numpy()
        # 必须显式 int()：numpy 标量走缓冲区协议，sqlite3 会把它当成 BLOB 写进去
        rows = list(zip(
            (int(v) for v in new.to_numpy()[changed]),
            df["symbol"].to_numpy()[changed],
            df["date"].to_numpy()[changed],
        ))
        conn.executemany(
            "UPDATE daily_bar SET tradestatus=? WHERE symbol=? AND date=?", rows
        )
        conn.commit()
        print(f"[tradestatus] 已写回 {len(rows)} 行")
    return 0


# ── price-limits ────────────────────────────────────────────────────
def missing_limit_dates(conn: sqlite3.Connection, limit: int = 0) -> list[str]:
    """daily_bar 有、price_limit_daily **没有覆盖齐**的交易日，**倒序**（先补最近的）。

    「已覆盖」的判据不只是「有行」：某个日期只采到几十行（部分失败）同样要重采，
    否则残缺分区永远不会被补上。行数少于当日 daily_bar 行数的一半才算未覆盖。

    limit > 0 时只取前 limit 个（配合时间预算分批跑）。
    """
    sql = (
        "SELECT d.date AS date FROM "
        "  (SELECT date, COUNT(*) AS n FROM daily_bar GROUP BY date) d "
        "LEFT JOIN "
        "  (SELECT date, COUNT(*) AS m FROM price_limit_daily GROUP BY date) p "
        "  ON p.date = d.date "
        "WHERE COALESCE(p.m, 0) < MAX(1, d.n * 0.5) "
        "ORDER BY d.date DESC"
    )
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    return [r["date"] for r in conn.execute(sql)]


def cmd_price_limits(args: argparse.Namespace) -> int:
    """采集涨跌停。

    `daily_price_limits` 每次调用约 34 秒且**服务端不缓存**
    （同一 trade_date 连调两次分别 35.3s / 31.4s），
    因此全历史 887 个交易日需要 8 小时以上。默认只补最近一天；
    历史回补用 --days N 分批，或 `--fill` 断点续采（跳过已覆盖的日期，可反复运行）。
    """
    days = int(getattr(args, "days", 0) or 0)
    fill = bool(getattr(args, "fill", False))
    if fill and days > 0:
        print("[limits] --fill 与 --days 不能同时使用", file=sys.stderr)
        return 2

    conn = db.connect()
    try:
        if fill:
            dates = missing_limit_dates(conn, limit=int(getattr(args, "max_days", 0) or 0))
        elif days > 0:
            dates = [
                r["date"] for r in conn.execute(
                    "SELECT DISTINCT date FROM daily_bar ORDER BY date DESC LIMIT ?",
                    (days,),
                )
            ]
        else:
            row = conn.execute("SELECT MAX(date) AS d FROM daily_bar").fetchone()
            dates = [row["d"]] if row and row["d"] else []
    finally:
        conn.close()
    if not dates:
        print("[limits] daily_bar 为空" if not fill else "[limits] 涨跌停已覆盖全部交易日")
        return 0 if fill else 1

    max_minutes = float(getattr(args, "max_minutes", 0.0) or 0.0)
    workers = max(1, int(getattr(args, "workers", 1) or 1))
    with db.session() as conn:
        run_id = db.start_run(conn, task="price-limits", source=config.DATA_SOURCE)
    total = 0
    done_days = 0
    empty: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    t_all = time.monotonic()
    # 按波推进：一波最多 workers 天，波与波之间检查时间预算。
    # workers=1 时行为与串行完全一致（默认值，避免悄悄加大采集压力）。
    for start in range(0, len(dates), workers):
        done = time.monotonic() - t_all
        # 第一波必须跑：否则预算很小/时钟刚起步时会出现「一天都不采就退出」
        if start and max_minutes > 0 and done > max_minutes * 60:
            skipped = dates[start:]
            print(f"[limits] 已用 {done/60:.1f} 分钟，补了 {start} 天，"
                  f"还剩 {len(skipped)} 天，可再跑一次续采")
            break
        wave = dates[start:start + workers]
        n, bad, zero = limit_src.sync_price_limits_chunk(wave, workers=len(wave))
        total += n
        failed += bad
        empty += zero
        done_days += len(wave)
        i = start + len(wave)
        elapsed = time.monotonic() - t_all
        eta = elapsed / i * (len(dates) - i)
        print(f"[limits] {i}/{len(dates)} 累计 {total} 行"
              f" | 已用 {elapsed/60:.1f} 分钟 | 预计剩余 {eta/60:.1f} 分钟",
              flush=True)
    ok_days = done_days - len(empty)
    print(f"[limits] 成功 {ok_days} 天 / 空 {len(empty)} 天"
          + (f" / 未处理 {len(skipped)} 天（时间预算）" if skipped else ""))
    if empty:
        print(f"[limits] 本轮 {len(empty)} 天返回 0 行（服务器无数据或失败）："
              f"{', '.join(empty[:10])}{' …' if len(empty) > 10 else ''}")
    msg = f"补 {done_days} 天 / {total} 行（成功 {ok_days} 天，并发 {workers}）"
    if failed:
        msg += f"；失败 {len(failed)} 天：{', '.join(failed[:5])}"
    if skipped:
        msg += f"；时间预算到点，未处理 {len(skipped)} 天"
    with db.session() as conn:
        db.finish_run(conn, run_id, "success" if total else "failed", total, msg)
    if fill:
        conn = db.connect()
        try:
            left = len(missing_limit_dates(conn))
        finally:
            conn.close()
        print(f"[limits] 本轮补 {done_days} 天 / {total} 行，"
              f"仍未覆盖 {left} 天（再跑一次 --fill 续采）")
    if total == 0:
        # 一天都没采到：不能静默返回 0，update.bat / CI 要靠退出码发现问题
        print("[limits] 本轮没有采到任何涨跌停数据", file=sys.stderr)
        return 2
    return 0


# ── quality（复权因子异常检测）──────────────────────────────────────
def cmd_quality(args: argparse.Namespace) -> int:
    """复权因子异常检测：官方因子 + 库内单日跳变。

    **只报告不改口径**：自动剔除跨异常的区间收益会静默改变已公布的回测结论，
    必须先由使用者判断（见 analytics/quality.py 的模块说明）。
    """
    from .analytics import quality

    db.init_db()
    offline = bool(getattr(args, "offline", False))
    dry = bool(getattr(args, "dry_run", False))
    threshold = float(getattr(args, "threshold", 0.0) or quality.HFQ_JUMP_THRESHOLD)
    n_int = n_off = 0
    with db.session() as conn:
        run_id = db.start_run(
            conn, task="quality", source="internal" if offline else config.DATA_SOURCE)
    try:
        # ① 离线：库内单日跳变 + 价格水平异常（覆盖全历史，不联网）
        with db.session() as conn:
            jumps = quality.scan_hfq_jumps(conn, threshold)
            levels = quality.scan_price_levels(conn)
            internal = pd.concat([jumps, levels], ignore_index=True)
            n_int = quality.materialize(conn, internal, "internal") if not dry else len(internal)
            focus = set(quality.tiny_price_symbols(conn))
        suspects = jumps[jumps["kind"] == "hfq_jump"]
        no_limit = int((jumps["kind"] == "no_limit_day").sum())
        tiny = levels[levels["kind"] == "tiny_price"]
        nonpos = levels[levels["kind"] == "nonpositive_price"]
        print(f"[quality] 库内单日跳变：可疑 {len(suspects)} 处"
              f"（另有 {no_limit} 处无涨跌幅限制日，正常）")
        if not suspects.empty:
            for _, r in suspects.sort_values("ratio", key=abs, ascending=False).head(10).iterrows():
                print(f"    {r['symbol']} {r['date']} {float(r['ratio']) * 100:+.1f}%  {r['detail']}")
        print(f"[quality] 价格水平：极小价(<{quality.TINY_PRICE} 元) {len(tiny)} 行 / "
              f"非正价 {len(nonpos)} 行（RPS 已置空），涉及 {len(focus)} 只")
        if focus:
            print(f"    {', '.join(sorted(focus)[:12])}{' …' if len(focus) > 12 else ''}")

        # ② 联网：官方复权因子（权威源，解释上面这些价格为什么被压低）
        if not offline:
            with db.session() as conn:
                symbols = [r["symbol"] for r in conn.execute(
                    "SELECT symbol FROM daily_bar GROUP BY symbol ORDER BY symbol")]
                since = conn.execute(
                    "SELECT MIN(date) AS d FROM daily_bar").fetchone()["d"]
            print(f"[quality] 拉取官方复权因子：{len(symbols)} 只"
                  f"（{quality.FACTOR_BATCH} 只/批；负偏移只对库内价格被压低的股票记录）")
            factors = quality.fetch_factor_events(
                symbols, since=since, focus_symbols=focus,
                progress=lambda m: print(f"[quality] {m}", flush=True))
            if not dry:
                with db.session() as conn:
                    n_off = quality.materialize(conn, factors, "official")
            else:
                n_off = len(factors)
            neg = factors[factors["kind"] == "negative_offset"]
            jmps = factors[factors["kind"] == "factor_jump"]
            print(f"[quality] 官方因子：负偏移档位 {len(neg)} 处（{neg['symbol'].nunique()} 只）/ "
                  f"因子跳变 {len(jmps)} 处（阈值 {quality.FACTOR_JUMP_THRESHOLD * 100:.0f}%）")
            for _, r in neg.head(10).iterrows():
                print(f"    {r['symbol']} {r['date']} hfq_offset={float(r['ratio']):.4f} {r['detail']}")

        if dry:
            print("[quality] --dry-run：未写库")
        else:
            print(f"[quality] 已写入 adjust_event：internal {n_int} 行 / official {n_off} 行")
        with db.session() as conn:
            db.finish_run(conn, run_id, "success", n_int + n_off,
                          f"internal {n_int} / official {n_off}")
        return 0
    except Exception as exc:  # noqa: BLE001
        with db.session() as conn:
            db.finish_run(conn, run_id, "failed", 0, f"{type(exc).__name__}: {exc}")
        print(f"[quality] 失败: {exc}", file=sys.stderr)
        return 2


# ── industry-map（行业分类）─────────────────────────────────────────
def cmd_industry_map(args: argparse.Namespace) -> int:
    """用 eltdx 的一级行业分类写入 industry_map。"""
    from .sources import industry_src

    db.init_db()
    with db.session() as conn:
        run_id = db.start_run(conn, task="industry-map", source=config.DATA_SOURCE)
        try:
            n_sym, n_failed = industry_src.sync_industry_map(
                replace=bool(getattr(args, "replace", False)),
                from_csv=getattr(args, "from_csv", None),
                fill_missing=bool(getattr(args, "fill_missing", False)),
            )
            msg = f"覆盖 {n_sym} 只股票"
            if n_failed:
                # 数据可用（旧行保留）→ 退出码保持 0；但状态必须是 partial，
                # 否则页面「最近更新」看不出来有一批板块没采到
                msg += f"；{n_failed} 个板块失败（保留旧行，未做整表重建）"
            db.finish_run(conn, run_id, "partial" if n_failed else "success",
                          n_sym, msg)
            return 0
        except Exception as exc:  # noqa: BLE001
            db.finish_run(conn, run_id, "failed", 0, str(exc))
            print(f"[industry] 采集失败: {exc}", file=sys.stderr)
            return 2


# ── concept（概念/题材板块）─────────────────────────────────────────
def cmd_concept(args: argparse.Namespace) -> int:
    """采集概念成分股；--compute 只算 RPS 不重采。"""
    from .sources import board_src

    db.init_db()
    if not getattr(args, "compute_only", False):
        with db.session() as conn:
            run_id = db.start_run(conn, task="concept", source=config.DATA_SOURCE)
            try:
                n_sym, failed_boards = board_src.sync_concept_map()
                msg = f"覆盖 {n_sym} 只股票"
                if failed_boards:
                    msg += (f"；{len(failed_boards)} 个板块失败（保留旧成分）："
                            f"{'、'.join(failed_boards[:5])}")
                db.finish_run(conn, run_id,
                              "partial" if failed_boards else "success", n_sym, msg)
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

        long = matrix.load_long(conn, start, seed_before_start=(targets is not None))
        close = matrix.build_close_matrix(long, matrix.negative_offsets(conn))
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
        # 与个股同口径：横截面不足时不产出概念 RPS（成分股来自同一批个股，
        # 日期集合必须一致，否则同一行数据在个股表里没有、在概念表里却有）；
        # 另外单成员时百分位恒 100，那不是强度
        keep = rps_engine.cross_section_dates(frames, config.MIN_CROSS_SECTION)
        cdf = cdf[cdf["date"].isin(keep)]
        cdf = rps_engine.drop_thin_dates(
            rps_engine.clean_inf(cdf),
            [c for c in cdf.columns if c.startswith("ret")],
        )
        if targets is not None:
            cdf = cdf[cdf["date"].isin(set(targets))]
        # 全量分支由 _write_all 在同一事务里 DELETE + 写入；这里不再单独删一次并提交，
        # 否则中断会停在「已删未写」的空表状态
        n = db.replace_date_partition(
            conn, "concept_rps_daily", sorted(set(cdf["date"])), cdf
        ) if targets is not None else _write_all(conn, "concept_rps_daily", cdf)
        print(f"[concept] 概念 RPS {n} 行（{mode}），覆盖 {cdf['concept'].nunique()} 个概念")
    return 0


def _write_all(conn, table: str, df) -> int:
    """整表替换。DELETE 与写入必须同一事务：中间 commit 的话，中断就只剩空表。"""
    conn.execute(f"DELETE FROM {table}")
    if not df.empty:
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


# ── watch（自选股池）────────────────────────────────────────────────
def cmd_watch(args: argparse.Namespace) -> int:
    """自选股池：add / rm / ls（只落库，不做推送）。"""
    action = getattr(args, "action", None)
    symbol = getattr(args, "symbol", None)
    if action in ("add", "rm") and not (symbol and str(symbol).isdigit() and len(str(symbol)) == 6):
        print("[watch] 股票代码必须是 6 位数字，例如 600519", file=sys.stderr)
        return 2

    with db.session() as conn:
        if action == "add":
            conn.execute(
                "INSERT OR REPLACE INTO watchlist(symbol, note, added_at) VALUES (?,?,?)",
                (symbol, getattr(args, "note", None), db.now()),
            )
            conn.commit()
            print(f"[watch] 已加入自选：{symbol}")
            return 0
        if action == "rm":
            cur = conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))
            conn.commit()
            print(f"[watch] 已移除 {symbol}" if cur.rowcount else f"[watch] {symbol} 不在自选中")
            return 0

        cols = config.period_columns()
        rows = conn.execute(
            "SELECT w.symbol, w.note, w.added_at, s.name, "
            + ", ".join(f"r.{c}" for c in cols)
            + " FROM watchlist w "
            "LEFT JOIN security_master s ON s.symbol = w.symbol "
            "LEFT JOIN rps_daily r ON r.symbol = w.symbol "
            "  AND r.date = (SELECT MAX(date) FROM rps_daily) "
            "ORDER BY w.added_at DESC"
        ).fetchall()
        if not rows:
            print("[watch] 自选为空：cli watch add 600519 [--note 备注]")
            return 0
        head = "".join(f"{'RPS' + str(p):>8}" for p in config.PERIODS)
        print(f"{'代码':<8}{'名称':<10}{head}  备注")
        for r in rows:
            name = (r["name"] or "")[:8]
            def f(v):
                return "—" if v is None else f"{v:.0f}"
            vals = "".join(f"{f(r[c]):>8}" for c in cols)
            print(f"{r['symbol']:<8}{name:<10}{vals}  {r['note'] or ''}")
    return 0


# ── alert（条件预警）────────────────────────────────────────────────
def cmd_alert(args: argparse.Namespace) -> int:
    """条件预警：add / ls / rm / run（只落库 + 打印命中，不做推送）。"""
    from .analytics import alerts as alerts_engine

    action = getattr(args, "action", None)
    with db.session() as conn:
        if action == "add":
            kind = args.kind
            raw = getattr(args, "params", None) or "{}"
            try:
                params = json.loads(raw)
                if not isinstance(params, dict):
                    raise ValueError("必须是 JSON 对象")
            except (ValueError, TypeError) as exc:
                print(f"[alert] --params 不是合法 JSON 对象：{exc}", file=sys.stderr)
                return 2
            cur = conn.execute(
                "INSERT INTO alert_rule(name, kind, params, enabled, created_at) "
                "VALUES (?,?,?,1,?)",
                (args.name, kind, json.dumps(params, ensure_ascii=False), db.now()),
            )
            conn.commit()
            print(f"[alert] 已新增规则 #{cur.lastrowid} {args.name}（{kind} {raw}）")
            return 0
        if action == "rm":
            # 与 API 的 DELETE /api/alerts/rules/{id} 保持一致：连带命中记录一起删，
            # 否则 alert_hit 里会留下 rule_name 为空的孤儿行，页面/接口都会显示它们。
            cur = conn.execute("DELETE FROM alert_rule WHERE id = ?", (int(args.id),))
            conn.execute("DELETE FROM alert_hit WHERE rule_id = ?", (int(args.id),))
            conn.commit()
            print(f"[alert] 已删除规则 #{args.id}" if cur.rowcount
                  else f"[alert] 规则 #{args.id} 不存在")
            return 0
        if action == "run":
            hits = alerts_engine.run_rules(conn, getattr(args, "date", None))
            if not hits:
                print("[alert] 本次没有命中（没有规则、或规则都没触发）")
                return 0
            print(f"[alert] 命中 {len(hits)} 条：")
            for h in hits:
                print(f"  #{h['rule_id']:<3}{h['date']}  {h['symbol']}  "
                      f"{h['rule_name']}  {h['detail']}")
            return 0

        if action == "push":
            return _alert_push(args, conn)

        rows = conn.execute(
            "SELECT id, name, kind, params, enabled, created_at FROM alert_rule ORDER BY id"
        ).fetchall()
        if not rows:
            print("[alert] 没有规则：cli alert add --name RPS90 --kind rps_threshold "
                  "--params '{\"period\":60,\"min\":90,\"max\":100}'")
            return 0
        for r in rows:
            flag = "" if r["enabled"] else "（已停用）"
            hits = conn.execute(
                "SELECT COUNT(*) AS c FROM alert_hit WHERE rule_id = ?", (r["id"],)
            ).fetchone()["c"]
            print(f"#{r['id']:<3}{r['name']:<16}{r['kind']:<14}{r['params']}"
                  f"  累计命中 {hits}{flag}")
    return 0


# ── alert push（把命中推到手机）─────────────────────────────────────
def _alert_push(args: argparse.Namespace, conn) -> int:
    """把某一天的命中汇总推到 ShowDoc 推送服务（微信）。**只手动触发**。"""
    from . import notify

    if getattr(args, "run", False):
        from .analytics import alerts as alerts_engine
        n = len(alerts_engine.run_rules(conn, getattr(args, "date", None)))
        print(f"[alert] 先评估了一次，命中 {n} 条")

    date = getattr(args, "date", None)
    if not date:
        row = conn.execute("SELECT MAX(date) AS d FROM alert_hit").fetchone()
        date = row["d"] if row and row["d"] else None
    if not date:
        print("[alert] alert_hit 里没有任何命中记录：先跑 `cli alert run`", file=sys.stderr)
        return 1

    rows = conn.execute(
        "SELECT h.date, h.symbol, h.rule_id, h.detail, r.name AS rule_name, "
        "       s.name AS name "
        "FROM alert_hit h "
        "LEFT JOIN alert_rule r ON r.id = h.rule_id "
        "LEFT JOIN security_master s ON s.symbol = h.symbol "
        "WHERE h.date = ? ORDER BY h.rule_id, h.symbol",
        (date,),
    ).fetchall()
    hits = [dict(r) for r in rows]
    if not hits:
        print(f"[alert] {date} 没有命中记录，不推送")
        return 0

    title, content = notify.format_summary(date, hits)
    if getattr(args, "dry_run", False):
        print(f"[alert] --dry-run：不会真的推送。消息如下（{len(hits)} 条命中）\n")
        print(f"标题：{title}\n\n{content}")
        return 0

    try:
        notify.push(title, content)
    except notify.NotifyError as exc:
        print(f"[alert] {exc}", file=sys.stderr)
        return 2
    print(f"[alert] 已推送 {len(hits)} 条命中（{date}）到 ShowDoc 推送服务")
    return 0


# ── notify（推送通道自检）──────────────────────────────────────────
def cmd_notify(args: argparse.Namespace) -> int:
    """推送通道自检：发一条测试消息，或只打印将要发送的内容。"""
    from . import notify

    action = getattr(args, "action", None)
    if action != "test":
        print("[notify] 用法：cli notify test [--dry-run]", file=sys.stderr)
        return 2
    if getattr(args, "dry_run", False):
        print("[notify] --dry-run：不会真的联网。将要发送：标题「RpsDashboard 推送测试」"
              " + 一行正文")
        return 0
    if not notify.configured():
        print("[notify] 未配置 SHOWDOC_PUSH_URL：把 ShowDoc 推送服务的专属地址写进 "
              "仓库根 `.env.local`（gitignored）或设为环境变量", file=sys.stderr)
        return 2
    try:
        notify.push_test()
    except notify.NotifyError as exc:
        print(f"[notify] {exc}", file=sys.stderr)
        return 2
    print("[notify] 测试消息已发送，请查看微信")
    return 0


# ── status ──────────────────────────────────────────────────────────
def cmd_status(_: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        bar = db.count_rows(conn, "daily_bar")
        rps_n = db.count_rows(conn, "rps_daily")
        ind = db.count_rows(conn, "industry_rps_daily")
        lim = db.count_rows(conn, "price_limit_daily")
        # 未建库时**不能**直接查表：start.bat 的第一步就是这个命令，
        # 新人 clone 下来双击立刻吃一个 no such table traceback 是最糟的第一印象。
        if not db.table_exists(conn, "daily_bar"):
            print(f"数据库      {config.DB_PATH}")
            print("  尚未初始化：先运行  python -m rpsboard.cli init")
            return 0
        last = conn.execute("SELECT MAX(date) AS d FROM daily_bar").fetchone()["d"]
        n_sym = conn.execute("SELECT COUNT(*) AS c FROM security_master").fetchone()["c"]
        print(f"数据库      {config.DB_PATH}")
        print(f"证券        {n_sym} 只")
        print(f"日K         {bar} 行，最后交易日 {last}")
        print(f"RPS         {rps_n} 行")
        print(f"板块RPS     {ind} 行")
        print(f"涨跌停      {lim} 行")
        # 缺口与涨跌停覆盖都要能看见：两者都只会「静默地让回测/筛选失效」
        gaps = rps_engine.suspect_gaps(conn)
        if gaps:
            print(f"RPS 缺口    {len(gaps)} 天，最早 {gaps[0]}"
                  f"（中间空洞增量补不上，用 compute-rps --full 重算）")
        else:
            print("RPS 缺口    无")
        lim_rows = conn.execute(
            "SELECT MIN(date) AS lo, MAX(date) AS hi FROM price_limit_daily"
        ).fetchone()
        if lim_rows and lim_rows["lo"]:
            print(f"涨跌停覆盖  {lim_rows['lo']} ~ {lim_rows['hi']}")
        else:
            print("涨跌停覆盖  无（回测不会剔除涨停，可 cli price-limits --days N 回补）")
        # 复权异常：跨异常日的区间收益会被放大，必须能看见（只提示，不改口径）
        if not db.table_exists(conn, "adjust_event"):
            print("复权异常    未检测（跑一次 cli quality）")
        else:
            c = {r["kind"]: r["n"] for r in conn.execute(
                "SELECT kind, COUNT(*) AS n FROM adjust_event "
                "WHERE source = 'internal' GROUP BY kind")}
            o = {r["kind"]: r["n"] for r in conn.execute(
                "SELECT kind, COUNT(*) AS n FROM adjust_event "
                "WHERE source = 'official' GROUP BY kind")}
            if sum(c.values()) or sum(o.values()):
                print(f"复权异常    可疑跳变 {c.get('hfq_jump', 0)} 处 / "
                      f"极小价 {c.get('tiny_price', 0)} 行 / 非正价 {c.get('nonpositive_price', 0)} 行 / "
                      f"负偏移 {o.get('negative_offset', 0)} 只 → cli quality")
                print(f"            （信息性：除权/送转事件 {o.get('factor_jump', 0)} 处、"
                      f"无涨跌幅限制日 {c.get('no_limit_day', 0)} 处 —— 正常企业行为，"
                      f"复牌日不设限未进买入过滤）")
            else:
                print("复权异常    无")
    finally:
        conn.close()
    return 0


# ── trade（自建模拟盘 / QMT 只读·影子·券商模拟盘）──────────────────
# 退出码：0 正常；2 配置或连接失败；3 有差异/有跳过项；4 部分委托失败。
# **没有实盘分支**：`execute` 只在 plan.mode=sim 且带 --i-know-this-is-sim 时放行。
def cmd_trade_paper_run(args: argparse.Namespace) -> int:
    """自建模拟盘：在区间上逐日「出计划 → 次日撮合 → 盯市」。**不连 QMT。**"""
    from .broker import sim

    config.ensure_dirs()
    db.init_db()
    if args.reset and not args.yes_reset:
        print("[trade] --reset 会清空模拟账户、持仓与净值（不可恢复）。"
              "确认请加 --yes-reset", file=sys.stderr)
        return 2
    with db.session() as conn:
        run_id = db.start_run(conn, task="trade-paper", source="local")
        t0 = time.time()

        def progress(done: int, total: int, day: str) -> None:
            if done % 20 == 0 or done == total:
                print(f"  {day}  {done}/{total}  ({time.time() - t0:.0f}s)")

        try:
            res = sim.run(
                conn, from_date=args.from_date, to_date=args.to_date,
                cash=args.cash, period=args.period, threshold=args.threshold,
                hold=args.hold, max_positions=args.max_positions,
                exclude_st=not args.no_exclude_st, resonance=args.resonance,
                signal=args.signal,
                reset=args.reset and args.yes_reset, progress=progress)
        except sim.SimError as exc:
            db.finish_run(conn, run_id, "failed", 0, str(exc))
            print(f"[trade] 模拟盘运行失败：{exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001
            db.finish_run(conn, run_id, "failed", 0, f"{type(exc).__name__}: {exc}")
            print(f"[trade] 模拟盘运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        db.finish_run(conn, run_id, "success", res["filled"],
                      f"模拟盘 {res['plans']} 次调仓，收益 {res['return']:.2%}")
        summary = sim.summary(conn)

    print(f"\n[paper] 初始资金 {_money(summary['initial_cash'])} → "
          f"期末 {_money(summary['total'])}（{summary['return']:+.2%}，"
          f"{summary['days']} 个交易日 {summary['from']}~{summary['to']}）")
    if res.get("resumed_from"):
        print(f"[paper] 本次为续跑：已结算到 {res['resumed_from']}，"
              f"新增结算 {res['days']} 个交易日（历史净值不重算）")
    else:
        print(f"[paper] 本次新建曲线，结算 {res['days']} 个交易日")
    print(f"[paper] 最大回撤 {summary['max_drawdown']:.2%}｜期末持仓 "
          f"{len(summary['positions'])} 只｜计划 {res['plans']} 次，"
          f"委托 {res['orders']} 笔，成交 {res['filled']} 笔，"
          f"限价未触及 {res['canceled']} 笔，涨停/停牌顺延 {res['deferred']} 笔")
    print(f"[paper] 累计费用 {_money(res['fee'])}")
    return 0


def cmd_trade_paper(args: argparse.Namespace) -> int:
    """打印自建模拟盘账户：净值、持仓明细。"""
    from .broker import sim

    db.init_db()
    with db.session() as conn:
        try:
            s = sim.summary(conn)
        except sim.SimError as exc:
            print(f"[paper] {exc}", file=sys.stderr)
            return 2
    if not s["days"]:
        print("[paper] 模拟盘还是空的：cli trade run --from 2026-01-01 --cash 1000000")
        return 0
    print(f"初始资金  {_money(s['initial_cash'])}")
    print(f"当前净值  {_money(s['total'])}（{s['return']:+.2%}，净值 {s['nav']}）")
    print(f"最大回撤  {s['max_drawdown']:.2%}")
    print(f"区间      {s['from']} ~ {s['to']}（{s['days']} 个交易日）")
    print(f"\n{'代码':<8}{'股数':>8}{'成本':>9}{'现价':>9}{'市值':>14}{'浮盈':>12}")
    for p in s["positions"]:
        pnl = (p["close"] - p["avg_price"]) * p["volume"]
        print(f"{p['symbol']:<8}{p['volume']:>8}{p['avg_price']:>9.2f}"
              f"{p['close']:>9.2f}{p['market_value']:>14,.0f}{pnl:>+12,.0f}")
    return 0


def cmd_trade_archive(args: argparse.Namespace) -> int:
    """模拟盘曲线归档：`--save` 存一份、`--rm` 删一份、都不给则列出。**不连 QMT。**"""
    from .broker import sim

    db.init_db()
    with db.session() as conn:
        if args.rm is not None:
            ok = sim.delete_archive(conn, int(args.rm))
            print(f"[paper] 归档 #{args.rm} " + ("已删除" if ok else "不存在"))
            return 0 if ok else 2
        if getattr(args, "save", False):
            try:
                res = sim.archive(conn, args.label)
            except sim.SimError as exc:
                print(f"[paper] {exc}", file=sys.stderr)
                return 2
            print(f"[paper] 已归档 #{res['id']}「{res['label']}」"
                  f"{res['days']} 个交易日，收益 {res['return']:+.2%}")
            return 0
        rows = sim.list_archives(conn)

    if not rows:
        print("[paper] 还没有归档：cli trade archive --save --label RPS60")
        return 0
    print(f"{'ID':>4}  {'名称':<20}{'区间':<25}{'收益':>9}{'回撤':>9}{'天':>6}")
    for r in rows:
        mdd = float((r.get("metrics") or {}).get("max_drawdown") or 0.0)
        span = f"{r.get('from_date') or '—'}~{r.get('to_date') or '—'}"
        print(f"{r['id']:>4}  {str(r['label'])[:18]:<20}{span:<25}"
              f"{float(r['total_return']):>+8.2%}{-mdd:>9.2%}{int(r['days']):>6}")
    print("\n页面上可以在「交易 → 策略对照」里叠加比较；跑新策略前记得先 --save。")
    return 0


# ── trade（QMT：只读 / 影子 / 券商模拟盘；**没有实盘**）────────────
# 退出码：0 正常；2 配置或连接失败；3 有差异/有跳过项；4 部分委托失败。
# **没有实盘分支**：`execute` 只在 plan.mode=sim 且带 --i-know-this-is-sim 时放行。
def cmd_trade(args: argparse.Namespace) -> int:
    action = getattr(args, "action", None)
    fn = {
        "status": cmd_trade_status,
        "snapshot": cmd_trade_snapshot,
        "plan": cmd_trade_plan,
        "shadow": cmd_trade_shadow,
        "calibrate": cmd_trade_calibrate,
        "execute": cmd_trade_execute,
        "reconcile": cmd_trade_reconcile,
        "halt": cmd_trade_halt,
        "run": cmd_trade_paper_run,
        "paper": cmd_trade_paper,
        "archive": cmd_trade_archive,
    }.get(action)
    if fn is None:
        print("[trade] 用法：cli trade run|paper|archive|status|snapshot|plan|shadow|"
              "calibrate|execute|reconcile|halt", file=sys.stderr)
        return 2
    return fn(args)


def cmd_trade_status(args: argparse.Namespace) -> int:
    """连 QMT 拉账户与持仓，打印并写入 position_snapshot。"""
    from .broker import exec as trade_exec
    from .broker import qmt

    try:
        st = qmt.status() or {}
    except qmt.QmtError as exc:
        print(f"[trade] 连接失败：{exc}", file=sys.stderr)
        return 2

    conn = db.connect()
    run_id = db.start_run(conn, task="trade", source="qmt")
    try:
        n = trade_exec.snapshot_positions(conn)
        asset = st.get("asset") or {}
        # 账号与资金写进 collect_log.message：页面只读库就能显示，
        # 不必为了「显示一下」去连一次 QMT 客户端（见 api/trade.py）。
        db.finish_run(conn, run_id, "success", n, json.dumps({
            "account_masked": st.get("account_masked"),
            "asset": asset,
            "positions": n,
        }, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        db.finish_run(conn, run_id, "failed", 0, f"{type(exc).__name__}: {exc}")
        print(f"[trade] 持仓快照失败：{exc}", file=sys.stderr)
        conn.close()
        return 2

    print("连接成功    " + ("已连 QMT" if st.get("connected") else "未连接"))
    print(f"账号        {st.get('account_masked') or '****'}（RPSBOARD_QMT_ACCOUNT）")
    print(f"可用现金    {_money(asset.get('cash'))}")
    print(f"持仓市值    {_money(asset.get('market_value'))}")
    print(f"持仓        {n} 只（已写入 position_snapshot）")
    if config.TRADE_MODE != "sim":
        print(f"交易模式    {config.TRADE_MODE}（影子；发单需 --mode sim 且设 "
              f"RPSBOARD_TRADE_MODE=sim）")
    conn.close()
    return 0


def cmd_trade_snapshot(args: argparse.Namespace) -> int:
    """只刷持仓快照：不查资金、不下单（页面上的「重取快照」走这条）。"""
    from .broker import exec as trade_exec
    from .broker import qmt

    try:
        n = trade_exec.snapshot_positions(db.connect())
    except qmt.QmtError as exc:
        print(f"[trade] 连接失败：{exc}", file=sys.stderr)
        return 2
    print(f"[trade] 持仓快照已更新：{n} 只")
    return 0


def cmd_trade_plan(args: argparse.Namespace) -> int:
    """按回测口径生成交易计划（**不下单**）。"""
    from .broker import plan as plan_mod
    from .broker import qmt

    mode = (args.mode or config.TRADE_MODE or "paper").lower()
    if mode not in config.TRADE_MODES:
        print(f"[trade] --mode 只能是 {'/'.join(config.TRADE_MODES)}", file=sys.stderr)
        return 2
    if mode == "sim" and config.TRADE_MODE != "sim":
        print("[trade] --mode sim 需要 RPSBOARD_TRADE_MODE=sim（当前 "
              f"{config.TRADE_MODE!r}）", file=sys.stderr)
        return 2

    config.ensure_dirs()
    db.init_db()
    with db.session() as conn:
        plan_date = args.date or plan_mod.latest_signal_date(conn)
        if not plan_date:
            print("[trade] rps_daily 为空，先执行 cli compute-rps", file=sys.stderr)
            return 2
        if not args.date:
            print(f"[trade] --date 缺省，用最新信号日 {plan_date}")
        try:
            plan_id, n_items = plan_mod.build_plan(
                conn, plan_date=plan_date, period=args.period, threshold=args.threshold,
                hold=args.hold, exclude_st=not args.no_exclude_st,
                resonance=args.resonance, max_positions=args.max_positions,
                cash_budget=args.cash_budget, mode=mode,
            )
        except qmt.QmtError as exc:
            print(f"[trade] 连接失败：{exc}", file=sys.stderr)
            return 2
        except plan_mod.PlanError as exc:
            print(f"[trade] 计划生成失败：{exc}", file=sys.stderr)
            return 2

        plan, items = plan_mod.plan_rows(conn, plan_id)
        names = {}
        if db.table_has_rows(conn, "security_master"):
            names = {r["symbol"]: r["name"] for r in
                     conn.execute("SELECT symbol, name FROM security_master")}
        print(f"\n计划 #{plan_id}  {plan['plan_date']}  mode={mode}  "
              f"预算 {_money(plan['cash_budget'])}  条目 {n_items}")
        print(f"{'代码':<8}{'名称':<10}{'参考价':>9}{'委托价':>9}{'目标股数':>10}{'现持':>8}"
              f"{'差额':>8}  {'side':<5} reason")
        for it in items:
            name = (names.get(it["symbol"]) or "—")[:8]
            print(f"{it['symbol']:<8}{name:<10}{_num(it['ref_price']):>9}"
                  f"{_num(it['plan_price']):>9}"
                  f"{it['target_volume']:>10}{it['held']:>8}{it['delta']:>8}  "
                  f"{it['side']:<5} {it['reason']}")
        skipped = sum(1 for it in items if it["reason"] != "ok")
        print(f"\n口径：{plan['note']}")
        if skipped:
            print(f"[trade] {skipped}/{n_items} 条被跳过（原因见 reason 列）")
    return 3 if skipped else 0


def cmd_trade_shadow(args: argparse.Namespace) -> int:
    """影子成交：按计划价模拟买入，落 trade_fill（broker_order_id 为负）。"""
    from .broker import plan as plan_mod

    db.init_db()
    with db.session() as conn:
        try:
            res = plan_mod.shadow_fill(conn, int(args.plan_id))
        except plan_mod.PlanError as exc:
            print(f"[trade] 影子成交失败：{exc}", file=sys.stderr)
            return 2
        rows = conn.execute(
            "SELECT symbol, volume, price FROM trade_fill WHERE broker_order_id = ?"
            " ORDER BY broker_trade_id", (-res["plan_id"],)).fetchall()
    print(f"[trade] 计划 #{res['plan_id']} 影子成交 {res['fills']} 笔，"
          f"价格摩擦 {res['round_trip']:.4%}")
    for r in rows:
        print(f"  {r['symbol']:<8}{r['volume']:>8} 股  @{_money(r['price'])}")
    return 0


def cmd_trade_calibrate(args: argparse.Namespace) -> int:
    """影子往返摩擦 vs 成本模型理论值（**只打印**）。"""
    from .broker import plan as plan_mod

    db.init_db()
    with db.session() as conn:
        rows = plan_mod.calibrate(conn, days=args.days, n_positions=args.positions)
    if not rows:
        print("[trade] 没有影子成交记录：先执行 cli trade shadow --plan-id N")
        return 0
    print(f"{'日期':<12}{'计划':>6}{'只数':>6}{'金额':>14}"
          f"{'影子价格':>10}{'模型滑点':>10}{'模型往返':>10}")
    for r in rows:
        print(f"{r['date']:<12}{r['plan_id']:>6}{r['names']:>6}{r['amount']:>14,.2f}"
              f"{r['shadow_price']:>10.4%}{r['model_price']:>10.4%}"
              f"{r['model_round_trip']:>10.4%}")
    print("\n「模型滑点」只含滑点，与「影子价格」同口径；「模型往返」另含佣金/印花税/过户费。")
    return 0


def cmd_trade_execute(args: argparse.Namespace) -> int:
    """券商模拟盘发单（**限价**）。"""
    from .broker import exec as trade_exec
    from .broker import plan as plan_mod
    from .broker import qmt

    db.init_db()
    with db.session() as conn:
        try:
            res = trade_exec.execute(
                conn, int(args.plan_id), confirm=bool(args.i_know_this_is_sim),
                cancel_remaining=not args.no_cancel_remaining,
            )
        except qmt.QmtError as exc:
            print(f"[trade] 连接失败：{exc}", file=sys.stderr)
            return 2
        except plan_mod.PlanError as exc:
            msg = str(exc)
            print(f"[trade] {msg}", file=sys.stderr)
            return 3 if "不一致" in msg or "对账" in msg else 2
    print(f"[trade] 计划 #{res['plan_id']} 状态 {res['status']}："
          f"提交 {res['submitted']} 笔 / 跳过 {res['skipped']} / 失败 {res['failed']}"
          f" / 成交 {res['filled']} 笔")
    return 4 if res["failed"] else 0


def cmd_trade_reconcile(args: argparse.Namespace) -> int:
    """对比现持与计划目标（**不改数据**）。有差异退出码 3。"""
    from .broker import plan as plan_mod
    from .broker import qmt, risk

    db.init_db()
    with db.session() as conn:
        plan_id = int(args.plan_id) if args.plan_id else None
        if plan_id is None:
            row = conn.execute(
                "SELECT id FROM trade_plan ORDER BY id DESC LIMIT 1").fetchone()
            if row is None:
                print("[trade] 还没有计划：cli trade plan")
                return 0
            plan_id = int(row["id"])
        try:
            diffs = risk.reconcile_positions(conn, plan_id, live=None, against="target")
        except qmt.QmtError as exc:
            print(f"[trade] 连接失败：{exc}", file=sys.stderr)
            return 2
        except plan_mod.PlanError as exc:
            print(f"[trade] {exc}", file=sys.stderr)
            return 2
        print(f"{'代码':<8}{'现持':>10}{'目标':>10}{'差额':>10}  side")
        for d in diffs:
            print(f"{d['symbol']:<8}{d['have']:>10}{d['reference']:>10}"
                  f"{d['delta']:>10}  {d['side']}")
    if diffs:
        print(f"[trade] 计划 #{plan_id} 有 {len(diffs)} 只与目标不一致")
        return 3
    print(f"[trade] 计划 #{plan_id} 与账户完全一致")
    return 0


def cmd_trade_halt(args: argparse.Namespace) -> int:
    """halt 哨兵开关：存在即禁止任何发单（只是增删一个文件，不连 QMT）。"""
    on = bool(args.on)
    off = bool(args.off)
    if on == off:
        print("[trade] 用法：cli trade halt --on | --off", file=sys.stderr)
        return 2
    if on:
        config.TRADE_HALT_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.TRADE_HALT_FILE.write_text(
            f"halted at {db.now()}\n", encoding="utf-8")
    elif config.TRADE_HALT_FILE.exists():
        config.TRADE_HALT_FILE.unlink()

    config.ensure_dirs()
    db.init_db()
    with db.session() as conn:
        run_id = db.start_run(conn, task="trade-halt", source="cli")
        db.finish_run(conn, run_id, "success", 0,
                      "halt ON" if on else "halt OFF")
    state = "已开启" if config.TRADE_HALT_FILE.exists() else "已关闭"
    print(f"[trade] 发单开关{state}（{config.TRADE_HALT_FILE}）")
    return 0


def _money(v) -> str:
    return "—" if v is None else f"{float(v):,.2f}"


def _num(v) -> str:
    return "—" if v in (None, "") else f"{float(v):.2f}"


# ── backtest ────────────────────────────────────────────────────────
def cmd_backtest(args: argparse.Namespace) -> int:
    from .analytics import backtest as engine

    # 入口挡住明显非法的值：hold<1 会让 fwd 变成「未来负数天」的假收益，
    # rebalance<1 会和 hold 比较出荒谬的调仓间隔。
    if args.hold < 1 or (args.rebalance is not None and args.rebalance < 1):
        print("[backtest] --hold / --rebalance 必须 ≥ 1", file=sys.stderr)
        return 2

    conn = db.connect()
    try:
        res = engine.run_backtest(
            conn, period=args.period, threshold=args.threshold,
            hold=args.hold, rebalance=args.rebalance,
            resonance=args.resonance, exclude_st=not args.include_st,
            signal=args.signal, select_top=args.select_top,
        )
    finally:
        conn.close()

    top = res.params["select_top"]
    print(f"信号 {res.params['signal']}｜选股 "
          + ("全掩码等权" if top is None else f"前 {top} 只等权"))
    print(f"参数: {res.params}")
    print(f"指标: {res.metrics}")
    for w in res.warnings:
        print(f"⚠ {w}")
    print("分档前瞻收益:")
    for b in res.buckets:
        print(f"  {b['bucket']:>8} 样本 {b['samples']:>8}  "
              f"均值 {b['avg_forward_return']}  胜率 {b['win_rate']}")
    return 0


# ── factors（多因子评估台）──────────────────────────────────────────
def _fmt_opt(v, digits: int = 4) -> str:
    """None / NaN 显示成 `—`：没算出来的数字不能印成 0。"""
    if v is None or not isinstance(v, int | float):
        return "—"
    if v != v:  # NaN
        return "—"
    return f"{v:.{digits}f}"


def _print_factor_table(payload: dict) -> None:
    p = payload["params"]
    print(f"参数: period={p['period']} hold={p['hold']}（全样本研究口径）")
    print(f"{'因子':<14}{'IC均值':>9}{'IC_IR':>8}{'胜率':>8}{'样本':>8}{'自相关':>9}  说明")
    for f in payload["factors"]:
        print(
            f"{f['name']:<14}{_fmt_opt(f['ic_mean']):>9}{_fmt_opt(f['ic_ir'], 2):>8}"
            f"{_fmt_opt(f['ic_win'], 3):>8}{f['samples']:>8}"
            f"{_fmt_opt(f['autocorr'], 3):>9}  {f['label']}"
        )

    names = payload["corr"]["names"]
    matrix = payload["corr"]["matrix"]
    if not names:
        return
    print("\n因子相关矩阵（|corr| ≥ 0.7 标 *：这几个其实是同一个赌注）")
    print(" " * 14 + "".join(f"{n[:6]:>7}" for n in names))
    for i, name in enumerate(names):
        cells = []
        for j, v in enumerate(matrix[i]):
            if i == j:
                cells.append(f"{'—':>7}")
            elif v is None:
                cells.append(f"{'-':>7}")
            else:
                cells.append(f"{v:>6.2f}{'*' if abs(v) >= 0.7 else ' '}")
        print(f"{name:<14}" + "".join(cells))


def cmd_factors(args: argparse.Namespace) -> int:
    """多因子评估：全样本 IC/IR、分层收益、自相关、因子相关矩阵。

    `IC` 是全样本**研究口径**；策略净值用的因子方向取**滚动训练窗**的 IC 符号，
    两者不可混用为同一结论（页面与这里渲染的是同一份 payload）。
    """
    from .analytics import factors as fac

    db.init_db()
    with db.session() as conn:
        if not db.table_has_rows(conn, "daily_bar") or not db.table_has_rows(conn, "rps_daily"):
            print("[factors] 库为空：先执行 cli backfill && cli compute-rps", file=sys.stderr)
            return 2
        run_id = db.start_run(conn, task="factors", source="internal")
        t0 = time.time()
        try:
            payload = fac.evaluate_all(
                conn, period=args.period, hold=args.hold
            )
        except Exception as exc:  # noqa: BLE001
            db.finish_run(conn, run_id, "failed", 0, f"{type(exc).__name__}: {exc}")
            raise
        if not args.dry_run:
            conn.execute(
                "INSERT OR REPLACE INTO factor_eval(run_at, params, payload) VALUES (?,?,?)",
                (db.now(), json.dumps(payload["params"], ensure_ascii=False),
                 json.dumps(payload, ensure_ascii=False)),
            )
            conn.commit()
        n = len(payload["factors"])
        db.finish_run(conn, run_id, "success", n,
                      f"{n} 个因子，耗时 {time.time() - t0:.0f}s"
                      + ("（dry-run 未落库）" if args.dry_run else ""))

    _print_factor_table(payload)
    print("\nIC 为全样本研究口径；策略净值的因子方向由滚动训练窗决定，"
          "两者不可混用为同一结论。")
    return 0


# ── 供 /api/admin/update 派发 ───────────────────────────────────────
def _ns(cmd: str = "status", **over):
    """生成 argparse namespace：先按**真实 parser** 解析子命令，再叠加覆盖。

    默认取 `status`（无参数子命令），因此 `cli._ns(dry_run=True)` 这类测试写法
    照旧可用；而默认值只有 parser 一份 —— 早期实现手抄了一份默认值字典
    （workers/limit/period/market_filter/...），新增参数后必然漂移。

    例：`_ns("compute-rps", full=True)` 拿到的就是 `compute-rps --full` 的完整参数集。
    """
    base = vars(build_parser().parse_args([cmd]))
    base.update(over)
    return argparse.Namespace(**base)


def run_update_pipeline(progress=None, workers: int | None = None,
                        paper: bool = False) -> dict:
    """完整更新流程：拉日K → 采涨跌停 → 重算 RPS。

    供 API 后台线程与 update.bat 共用。任一步失败即中止并把错误抛出，
    调用方负责写 collect_log。`progress(phase)` 用于向前端播报当前阶段。

    每一步都用**该子命令自己的** parser 生成参数（`_ns("<cmd>")`），
    这样新增/改名参数不会让这条流水线悄悄跑偏。

    `paper=True` 时在末尾把自建模拟盘**增量推进**到今天（已有曲线时只结算
    新交易日，历史净值不重算）。默认关闭：它会加载全市场矩阵，耗时以分钟计，
    不该塞进每一次「更新数据」。
    """
    say = progress or (lambda _phase: None)
    say("bars")
    rc = cmd_backfill(_ns("backfill", workers=workers))
    if rc != 0:
        raise RuntimeError(f"日K 更新失败（退出码 {rc}）")

    say("limits")
    from .sources import limit_src
    n_lim = limit_src.sync_price_limits()

    # 行业分类只补空表：作者机器上已有证监会口径的数据不能被悄悄换掉
    with db.session() as conn:
        has_industry = db.table_has_rows(conn, "industry_map")
    if has_industry:
        print("[industry] industry_map 已有数据，跳过（要换口径用 cli industry-map --replace）")
    else:
        say("industry")
        rc = cmd_industry_map(_ns("industry-map"))
        if rc != 0:
            raise RuntimeError(f"行业分类采集失败（退出码 {rc}）")

    say("rps")
    rc = cmd_compute_rps(_ns("compute-rps"))
    if rc != 0:
        raise RuntimeError(f"RPS 计算失败（退出码 {rc}）")

    say("patterns")
    rc = cmd_patterns(_ns("patterns"))
    if rc != 0:
        raise RuntimeError(f"形态计算失败（退出码 {rc}）")

    # 数据更新后清掉回测的进程内矩阵缓存，否则最长 300s 内还在用旧数据出结论
    from .analytics import backtest as bt_engine
    bt_engine.clear_context_cache()

    # 条件预警：评估全部规则并把命中落库（只落库 + 终端输出，不做推送）
    say("alerts")
    from .analytics import alerts as alerts_engine
    with db.session() as conn:
        n_hits = len(alerts_engine.run_rules(conn))
    print(f"[alert] 命中 {n_hits} 条")

    # 复权异常 / 价格档位扫描：以前只能手动跑，于是「复权异常」永远显示上一次
    # 手动跑出来的数字（甚至从未跑过）。跟着更新一起刷新（实测约 20 秒）；
    # 它是诊断步骤，失败不阻断本次更新，但必须打印出来。
    say("quality")
    rc = cmd_quality(_ns("quality"))
    if rc != 0:
        print(f"[quality] 复权异常扫描失败（退出码 {rc}），本次更新继续")

    # 自建模拟盘增量推进（可选）：只结算上次之后的新交易日，历史净值不重算。
    # 失败不阻断更新的其余部分 —— 模拟盘是消费端，不该因为它把数据更新拖挂。
    paper_info: dict | None = None
    if paper:
        say("paper")
        from .broker import sim
        try:
            with db.session() as conn:
                if sim.last_settled_date(conn) is None:
                    print("[paper] 还没有模拟盘曲线，跳过"
                          "（首次：cli trade run --from <日期>）")
                else:
                    p = sim.resume_params(conn)
                    c = float(sim.meta_get(conn, "initial_cash") or 1_000_000.0)
                    st = sim.run(conn, cash=c, **p)
                    paper_info = {"days": st["days"], "total": st["final_total"],
                                  "return": st["return"]}
                    print(f"[paper] 推进 {st['days']} 个交易日，"
                          f"净值 {st['final_total']:,.0f}（{st['return']:+.2%}）")
        except Exception as exc:  # noqa: BLE001
            print(f"[paper] 推进失败：{type(exc).__name__}: {exc}（本次更新继续）")

    say("done")
    return {"ok": True, "price_limit_rows": n_lim, "alert_hits": n_hits,
            "paper": paper_info}


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

    up = sub.add_parser("update", help="完整更新：日K + 涨跌停 + RPS")
    # 以前 update 一个参数都没注册：`cli update --workers 4` 会直接 argparse 报错
    up.add_argument("--workers", type=int, default=config.TDX_WORKERS,
                    help="日K 采集并发线程数（默认 8）")
    up.add_argument("--paper", action="store_true",
                    help="更新后把自建模拟盘增量推进到今天（耗时以分钟计，默认关；"
                         "首次仍需先手动 cli trade run --from <日期>）")
    up.set_defaults(func=lambda ns: (run_update_pipeline(
        workers=ns.workers, paper=ns.paper), 0)[1])

    sp = sub.add_parser("industry-map", help="写入 industry_map（eltdx 行业分类或自带 CSV）")
    sp.add_argument("--replace", action="store_true",
                    help="整表重建（默认已有数据则跳过，避免覆盖已有口径）")
    sp.add_argument("--fill-missing", dest="fill_missing", action="store_true",
                    help="只给缺行业的股票补行（tdxhy.cfg 的行业代码，保留已有具名口径）")
    sp.add_argument("--from-csv", dest="from_csv", default=None,
                    help="从 CSV 导入（列：symbol,industry[,industry_cls]）")
    sp.set_defaults(func=cmd_industry_map)

    sp = sub.add_parser("tradestatus", help="按交易日口径重算 tradestatus（修长假误判，不联网）")
    sp.add_argument("--dry-run", action="store_true", help="只打印会改多少行，不写库")
    sp.set_defaults(func=cmd_tradestatus)

    sp = sub.add_parser("quality", help="复权因子异常检测（官方因子 + 库内单日跳变）")
    sp.add_argument("--offline", action="store_true",
                    help="只做离线扫描，不联网拉官方复权因子")
    sp.add_argument("--dry-run", action="store_true", help="只打印，不写 adjust_event")
    sp.add_argument("--threshold", type=float, default=0.0,
                    help="库内单日跳变阈值（小数，默认 0.31 = 超过北交所 30%% 上限）")
    sp.set_defaults(func=cmd_quality)

    sp = sub.add_parser("price-limits", help="采集涨跌停价")
    sp.add_argument("--days", type=int, default=0,
                    help="回补最近 N 个交易日（每次调用约 34 秒且服务端不缓存，"
                         "全历史 887 天需 8 小时以上；默认只补最近一天）")
    sp.add_argument("--fill", action="store_true",
                    help="补全历史：按 daily_bar 倒序补齐尚未覆盖的交易日（可反复运行续采）")
    sp.add_argument("--max-days", type=int, default=0, help="本轮最多补几天（0 = 不限）")
    sp.add_argument("--max-minutes", type=float, default=0.0,
                    help="本轮时间预算（分钟，0 = 不限）；到点后打印剩余天数并退出")
    sp.add_argument("--workers", type=int, default=1,
                    help="并发天数（默认 1 = 串行；接口单次 30~240 秒且服务端不缓存，"
                         "补全历史建议 6 左右）")
    sp.set_defaults(func=cmd_price_limits)

    sp = sub.add_parser("concept", help="采集概念板块并计算概念 RPS")
    sp.add_argument("--compute-only", action="store_true",
                    help="只重算 RPS，不重新采集成分股（采集约 31 分钟）")
    sp.add_argument("--full", action="store_true", help="概念 RPS 全量重算")
    sp.add_argument("--include-st", action="store_true", help="把 ST 也纳入排名")
    sp.set_defaults(func=cmd_concept)

    sub.add_parser("patterns", help="计算三升底形态").set_defaults(func=cmd_patterns)

    sp = sub.add_parser("factors", help="多因子评估：全样本 IC/IR、分层收益、相关矩阵")
    sp.add_argument("--period", type=int, default=config.DEFAULT_PERIOD,
                    choices=list(config.PERIODS), help="动量因子用哪个周期的 ret")
    sp.add_argument("--hold", type=int, default=20,
                    help="前瞻收益的持有期，必须与回测的 --hold 相同")
    sp.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    sp.set_defaults(func=cmd_factors)

    # ── 自选股池（只落库，不做推送）───────────────────────────────
    wsp = sub.add_parser("watch", help="自选股池：add / rm / ls")
    wsub = wsp.add_subparsers(dest="action", required=True)
    w_add = wsub.add_parser("add", help="加入自选")
    w_add.add_argument("symbol", help="6 位股票代码")
    w_add.add_argument("--note", default=None, help="备注")
    w_rm = wsub.add_parser("rm", help="移除自选")
    w_rm.add_argument("symbol", help="6 位股票代码")
    wsub.add_parser("ls", help="列出全部自选（含当日 RPS）")
    wsp.set_defaults(func=cmd_watch)

    # ── 条件预警（只落库 + CLI 输出）──────────────────────────────
    from .analytics.alerts import ALERT_KINDS

    asp = sub.add_parser("alert", help="条件预警：add / ls / rm / run")
    asub = asp.add_subparsers(dest="action", required=True)
    a_add = asub.add_parser("add", help="新增规则")
    a_add.add_argument("--name", required=True, help="规则名")
    a_add.add_argument("--kind", required=True, choices=ALERT_KINDS, help="规则类型")
    a_add.add_argument("--params", default="{}", help="规则参数（JSON 对象）")
    a_rm = asub.add_parser("rm", help="删除规则")
    a_rm.add_argument("id", help="规则 id")
    asub.add_parser("ls", help="列出规则与累计命中数")
    a_run = asub.add_parser("run", help="评估全部规则并写入命中记录")
    a_run.add_argument("--date", default=None, help="评估哪一天（默认最新交易日）")
    a_push = asub.add_parser("push", help="把某天的命中汇总推到手机（ShowDoc 推送服务）")
    a_push.add_argument("--date", default=None, help="推哪一天（默认 alert_hit 里最新的一天）")
    a_push.add_argument("--run", action="store_true", help="推送前先评估一次规则")
    a_push.add_argument("--dry-run", action="store_true", help="只打印消息，不发送")
    asp.set_defaults(func=cmd_alert)

    nsp = sub.add_parser("notify", help="推送通道：test")
    nsub = nsp.add_subparsers(dest="action", required=True)
    n_test = nsub.add_parser("test", help="发一条测试消息确认配置可用")
    n_test.add_argument("--dry-run", action="store_true", help="只打印，不发送")
    nsp.set_defaults(func=cmd_notify)

    sp = sub.add_parser("compute-rps", help="计算个股/板块 RPS")
    sp.add_argument("--include-st", action="store_true", help="把 ST 也纳入排名")
    sp.add_argument("--full", action="store_true",
                    help="全量重算（首次计算、或历史行情被订正后使用；默认只补新增交易日）")
    sp.set_defaults(func=cmd_compute_rps)

    # ── QMT 交易（只读 / 影子 / 券商模拟盘；**没有实盘**）───────────
    tsp = sub.add_parser("trade", help="交易接入：run/paper（自建模拟盘，不连券商）"
                                      " · status/snapshot/plan/shadow/calibrate/"
                                      "execute/reconcile/halt（QMT）")
    tsub = tsp.add_subparsers(dest="action", required=True)

    # 自建模拟盘：全程只读库里的日 K，不需要任何券商账号
    t_run = tsub.add_parser("run", help="自建模拟盘逐日推进：出计划 → 次日撮合 → 盯市")
    t_run.add_argument("--from", dest="from_date", default=None,
                       help="起始信号日（**只在首次建曲线时用**；已有曲线时被忽略，"
                            "曲线起点固定，要改起点请配合 --reset）")
    t_run.add_argument("--to", dest="to_date", default=None, help="结束日（缺省至今）")
    t_run.add_argument("--cash", type=float, default=1_000_000.0, help="初始资金（元）")
    t_run.add_argument("--period", type=int, default=config.DEFAULT_PERIOD,
                       choices=list(config.PERIODS))
    t_run.add_argument("--threshold", type=float, default=80.0)
    t_run.add_argument("--hold", type=int, default=20, help="调仓间隔（交易日）")
    t_run.add_argument("--max-positions", type=int, default=20)
    t_run.add_argument("--resonance", action="store_true")
    t_run.add_argument("--no-exclude-st", action="store_true")
    t_run.add_argument("--signal", choices=("rps", "composite"), default="rps",
                       help="信号源：rps 单因子 / composite 多因子等权合成")
    t_run.add_argument("--reset", action="store_true", help="先清空模拟盘再跑（不可恢复）")
    t_run.add_argument("--yes-reset", action="store_true", help="确认 --reset")
    tsub.add_parser("paper", help="打印模拟盘净值与持仓")

    # 曲线归档：跑第二种策略前先把第一种存下来（模拟盘只有一个活动账户）
    t_arch = tsub.add_parser("archive", help="模拟盘曲线归档：--save 存一份 / --rm 删一份 / 缺省列出")
    t_arch.add_argument("--save", action="store_true", help="把当前曲线归档一份")
    t_arch.add_argument("--label", default=None, help="归档名（配合 --save，缺省按信号源+时间生成）")
    t_arch.add_argument("--rm", type=int, default=None, help="删除指定 id 的归档")

    tsub.add_parser("status", help="拉账户与持仓并写 position_snapshot")
    tsub.add_parser("snapshot", help="只刷新持仓快照（不查资金、不下单）")

    t_plan = tsub.add_parser("plan", help="按回测口径生成交易计划（不下单）")
    t_plan.add_argument("--date", default=None, help="信号日（缺省取 rps_daily 最新日）")
    t_plan.add_argument("--period", type=int, default=config.DEFAULT_PERIOD,
                        choices=list(config.PERIODS),
                        help=f"RPS 周期，可选 {list(config.PERIODS)}")
    t_plan.add_argument("--threshold", type=float, default=80.0)
    t_plan.add_argument("--hold", type=int, default=20, help="记录用：回测的持有期口径")
    t_plan.add_argument("--max-positions", type=int, default=20, help="最多取前 N 只")
    t_plan.add_argument("--resonance", action="store_true", help="多周期共振（与回测同口径）")
    t_plan.add_argument("--no-exclude-st", action="store_true", help="不剔除 ST")
    t_plan.add_argument("--cash-budget", type=float, default=0.0,
                        help="总预算（元）；缺省 0 = QMT 可用现金 × 0.95")
    t_plan.add_argument("--mode", default=None, choices=list(config.TRADE_MODES),
                        help=f"计划模式，默认 {config.TRADE_MODE}")

    t_shadow = tsub.add_parser("shadow", help="影子成交（模拟买入并落 trade_fill）")
    t_shadow.add_argument("--plan-id", type=int, required=True)

    t_cal = tsub.add_parser("calibrate", help="影子往返摩擦 vs 成本模型（只打印）")
    t_cal.add_argument("--days", type=int, default=20, help="回看最近 N 天")
    t_cal.add_argument("--positions", type=int, default=20,
                       help="成本模型假设的持仓只数")

    t_exec = tsub.add_parser("execute", help="券商模拟盘发单（限价）")
    t_exec.add_argument("--plan-id", type=int, required=True)
    t_exec.add_argument("--i-know-this-is-sim", action="store_true",
                        help="确认 QMT 客户端登的是**模拟**账户；不带则拒绝发单")
    t_exec.add_argument("--no-cancel-remaining", action="store_true",
                        help="不撤未成交的挂单")

    t_rec = tsub.add_parser("reconcile", help="对比现持与计划目标（不改数据）")
    t_rec.add_argument("--plan-id", type=int, default=None, help="缺省取最新计划")

    t_halt = tsub.add_parser("halt", help="发单总开关（哨兵文件）")
    h_on = t_halt.add_mutually_exclusive_group(required=True)
    h_on.add_argument("--on", action="store_true", help="禁止一切发单")
    h_on.add_argument("--off", action="store_true", help="解除禁止")
    tsp.set_defaults(func=cmd_trade)

    sp = sub.add_parser("backtest", help="命令行回测")
    sp.add_argument("--period", type=int, default=config.DEFAULT_PERIOD,
                    choices=list(config.PERIODS),
                    help=f"RPS 周期，可选 {list(config.PERIODS)}")
    sp.add_argument("--threshold", type=float, default=80.0)
    sp.add_argument("--hold", type=int, default=20)
    sp.add_argument("--rebalance", type=int, default=None)
    sp.add_argument("--resonance", action="store_true")
    sp.add_argument("--include-st", action="store_true")
    sp.add_argument("--signal", choices=("rps", "composite"), default="rps",
                    help="信号源：rps 单因子 / composite 多因子等权合成")
    sp.add_argument("--select-top", dest="select_top", type=int, default=None,
                    help="按打分取前 N 只（配合 --threshold 0 = 全池前 N）；"
                         "缺省 = 掩码内全选等权")
    sp.set_defaults(func=cmd_backtest)

    return p


def main(argv: list[str] | None = None) -> int:
    mp.freeze_support()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
