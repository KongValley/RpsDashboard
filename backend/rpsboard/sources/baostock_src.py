"""baostock 数据源：证券列表 / 行业分类 / 交易日历 / 后复权日 K。

并发模型的关键约束：
- **baostock 非线程安全**，同一进程内多线程会串数据，因此必须
  `multiprocessing.Pool`，且**每个 worker 进程独立 login/logout**。
- Windows 的 spawn 启动方式要求 worker 函数定义在**模块顶层**（可 pickle），
  不能用闭包或 lambda；调用方还必须带 `if __name__ == "__main__"` 守卫。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .. import config, db
from .base import SourceError

K_FIELDS = (
    "date,open,high,low,close,volume,amount,turn,pctChg,isST,tradestatus"
)


# ── 会话内查询（单进程使用）──────────────────────────────────────────
def login() -> None:
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        raise SourceError(f"baostock 登录失败: {lg.error_code} {lg.error_msg}")


def logout() -> None:
    import baostock as bs

    try:
        bs.logout()
    except Exception:  # noqa: BLE001 - logout 失败无需阻断
        pass


def _query_to_frame(rs: Any) -> pd.DataFrame:
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    return pd.DataFrame(rows, columns=rs.fields) if rows else pd.DataFrame()


# ── 元数据（单次调用即可拿全量）─────────────────────────────────────
def fetch_security_master() -> pd.DataFrame:
    """全市场 A 股代码表（上市 + 股票类型）。"""
    import baostock as bs

    login()
    try:
        rs = bs.query_stock_basic(code_name="", code="")
        df = _query_to_frame(rs)
    finally:
        logout()

    if df.empty:
        raise SourceError("query_stock_basic 返回空结果")

    # code / code_name / ipoDate / outDate / type / status
    df = df[(df["type"] == "1") & (df["status"] == "1")].copy()
    df["symbol"] = df["code"].str.split(".").str[-1]
    out = pd.DataFrame(
        {
            "symbol": df["symbol"],
            "name": df["code_name"].str.strip(),
            "exchange": df["code"].str.split(".").str[0],
            "ipo_date": df["ipoDate"],
            "status": df["status"],
        }
    )
    out["is_st"] = out["name"].str.contains("ST", na=False).astype(int)
    return out.reset_index(drop=True)


def fetch_industry_map() -> pd.DataFrame:
    """全市场行业分类——一次调用拿全部股票，板块 RPS 的关键。"""
    import baostock as bs

    login()
    try:
        rs = bs.query_stock_industry()
        df = _query_to_frame(rs)
    finally:
        logout()

    if df.empty:
        raise SourceError("query_stock_industry 返回空结果")

    out = pd.DataFrame(
        {
            "symbol": df["code"].str.split(".").str[-1],
            "industry": df["industry"],
            "industry_cls": df["industryClassification"],
        }
    )
    return out[out["industry"].notna() & (out["industry"] != "")].reset_index(drop=True)


def fetch_trade_calendar(start: str, end: str) -> pd.DataFrame:
    """交易日历。start/end 为 YYYY-MM-DD。"""
    import baostock as bs

    login()
    try:
        rs = bs.query_trade_dates(start_date=start, end_date=end)
        df = _query_to_frame(rs)
    finally:
        logout()

    if df.empty:
        raise SourceError("query_trade_dates 返回空结果")

    out = pd.DataFrame({"date": df["calendar_date"], "is_open": df["is_trading_day"]})
    return out[out["date"].notna()].reset_index(drop=True)


def fetch_benchmark(code: str = "sh.000300", start: str = "", end: str = "") -> pd.DataFrame:
    """沪深300 日 K（后复权），用作回测基准。"""
    import baostock as bs

    login()
    try:
        rs = bs.query_history_k_data_plus(
            code, "date,close", start_date=start, end_date=end,
            frequency="d", adjustflag="1",
        )
        df = _query_to_frame(rs)
    finally:
        logout()

    if df.empty:
        return pd.DataFrame(columns=["symbol", "date", "close"])
    df = df[["date", "close"]].copy()
    df["symbol"] = code.split(".")[-1]
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna(subset=["close"]).reset_index(drop=True)


def fetch_all_metadata(start: str, end: str, benchmark: str = "sh.000300") -> dict:
    """**一次登录**拿全部元数据。

    实测本机网络下单次 baostock login 约 85 秒——这是整个采集链路最大的
    固定开销。把原先 4 次独立 login/logout 合并成 1 次会话，可以直接省下
    约 3.5 分钟。
    """
    import baostock as bs

    login()
    try:
        rs = bs.query_stock_basic(code_name="", code="")
        master = _query_to_frame(rs)
        rs = bs.query_stock_industry()
        industry = _query_to_frame(rs)
        rs = bs.query_trade_dates(start_date=start, end_date=end)
        calendar = _query_to_frame(rs)
        rs = bs.query_history_k_data_plus(
            benchmark, "date,close", start_date=start, end_date=end,
            frequency="d", adjustflag="1",
        )
        bench = _query_to_frame(rs)
    finally:
        logout()

    out: dict[str, pd.DataFrame] = {}

    if not master.empty:
        m = master[(master["type"] == "1") & (master["status"] == "1")].copy()
        m["symbol"] = m["code"].str.split(".").str[-1]
        out["security_master"] = pd.DataFrame({
            "symbol": m["symbol"],
            "name": m["code_name"].str.strip(),
            "exchange": m["code"].str.split(".").str[0],
            "ipo_date": m["ipoDate"],
            "status": m["status"],
        })
        out["security_master"]["is_st"] = (
            out["security_master"]["name"].str.contains("ST", na=False).astype(int)
        )

    if not industry.empty:
        ind = pd.DataFrame({
            "symbol": industry["code"].str.split(".").str[-1],
            "industry": industry["industry"],
            "industry_cls": industry["industryClassification"],
        })
        out["industry_map"] = ind[
            ind["industry"].notna() & (ind["industry"] != "")
        ].drop_duplicates(subset=["symbol"], keep="first")

    if not calendar.empty:
        out["trade_calendar"] = pd.DataFrame({
            "date": calendar["calendar_date"],
            "is_open": calendar["is_trading_day"],
        }).dropna(subset=["date"])

    if not bench.empty:
        b = bench[["date", "close"]].copy()
        b["symbol"] = benchmark.split(".")[-1]
        b["close"] = pd.to_numeric(b["close"], errors="coerce")
        out["benchmark_bar"] = b.dropna(subset=["close"])

    if "security_master" not in out:
        raise SourceError("query_stock_basic 返回空结果")
    return out


# ── 日 K 采集（多进程）───────────────────────────────────────────────
def to_baostock_code(symbol: str) -> str:
    """6 位纯数字代码 → baostock 的 sh./sz./bj. 格式。"""
    return f"{config.exchange_of(symbol)}.{symbol}"


def _fetch_chunk_main(argv: list[str]) -> int:
    """子进程入口：抓一批股票写库后立刻退出。用法见 `fetch_chunk`。

    设计原因（全部来自实测）：baostock 的**长驻会话会退化**——
    在一个进程里连续抓几百只之后，连接会断而不报错，客户端退化成
    100% CPU 空转且不再有任何网络连接（实测连续 350s CPU、0 条 ESTABLISHED
    连接、daily_bar 始终 0 行）。把工作切成短生命周期子进程，
    每块登录一次、抓有限只数就退出，从根上规避这种退化。
    """
    import baostock as bs
    import pandas as pd

    db_path, symbols_json, start_date, end_date = argv[0:4]
    symbols = json.loads(symbols_json)

    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    lg = bs.login()
    if lg.error_code != "0":
        conn.close()
        print(json.dumps({"ok": 0, "failed": len(symbols),
                          "error": f"login {lg.error_code} {lg.error_msg}"},
                         ensure_ascii=False), flush=True)
        return 2

    n_rows = n_ok = n_fail = 0
    buf: list[list] = []
    gap = config.BAOSTOCK_QPS_GAP
    try:
        for i, symbol in enumerate(symbols):
            code = to_baostock_code(symbol)
            rows: list[list] = []
            # baostock 也会限流：连续高频查询会返回
            # `10001011 服务器繁忙，请稍后再试`。实测需要显式的请求间隔。
            if gap and i:
                time.sleep(gap)
            for attempt in range(config.MAX_RETRIES):
                try:
                    rs = bs.query_history_k_data_plus(
                        code, K_FIELDS, start_date=start_date, end_date=end_date,
                        frequency="d", adjustflag="1",  # 后复权
                    )
                    if rs.error_code != "0":
                        raise RuntimeError(rs.error_msg)
                    rows = []
                    while rs.error_code == "0" and rs.next():
                        rows.append(rs.get_row_data())
                    break
                except Exception:  # noqa: BLE001
                    rows = []
                    if attempt < config.MAX_RETRIES - 1:
                        time.sleep(2 ** (attempt + 1) + 1.5)   # 限流要多退避
            if not rows:
                n_fail += 1
                continue
            n_ok += 1
            n_rows += len(rows)
            buf.extend([symbol, *r] for r in rows)
            if len(buf) >= 20000:
                _flush(conn, buf)
    finally:
        _flush(conn, buf)
        try:
            bs.logout()
        except Exception:  # noqa: BLE001
            pass
        conn.close()

    print(json.dumps({"ok": n_ok, "failed": n_fail, "rows": n_rows},
                     ensure_ascii=False), flush=True)
    return 0


def fetch_chunk(
    db_path: str,
    symbols: list[str],
    start_date: str,
    end_date: str,
    timeout: float = config.CHUNK_TIMEOUT_SEC,
) -> dict:
    """跑一个短生命周期子进程抓一批股票。超时会被强杀并如实报告。

    返回 {"ok", "failed", "rows", "timed_out"}。
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "rpsboard.sources.baostock_src",
         db_path, json.dumps(symbols), start_date, end_date],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8",
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, _ = proc.communicate(timeout=15)
        except Exception:  # noqa: BLE001
            out = ""
        return {"ok": 0, "failed": len(symbols), "rows": 0, "timed_out": True}

    for line in reversed((out or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and '"ok"' in line:
            try:
                d = json.loads(line)
                d["timed_out"] = False
                return d
            except json.JSONDecodeError:
                continue
    return {"ok": 0, "failed": len(symbols), "rows": 0, "timed_out": False,
            "error": (out or "")[-300:]}


def _flush(conn: sqlite3.Connection, buf: list[list]) -> None:
    """把缓冲区写入 daily_bar（按 股票×日期 定点删除，保证幂等且不误伤他人）。"""
    if not buf:
        return
    df = pd.DataFrame(buf, columns=BAR_COLUMNS)
    for c in ("open", "high", "low", "close", "volume", "amount", "turn", "pctChg"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["isST"] = pd.to_numeric(df["isST"], errors="coerce").fillna(0).astype(int)
    df["tradestatus"] = (
        pd.to_numeric(df["tradestatus"], errors="coerce").fillna(1).astype(int)
    )
    df = df.dropna(subset=["close"])
    df = df[df["volume"].fillna(0) > 0]
    df = df.drop_duplicates(subset=["symbol", "date"], keep="last")
    df = df.rename(columns=BAR_RENAME)[DB_COLUMNS]
    if df.empty:
        buf.clear()
        return

    # 只删本批真正覆盖的 (symbol, date)，不能用「按日期整片删」——
    # 多个进程并行写同一个库时，删整片会把别人刚写好的数据抹掉。
    pairs = list(zip(df["symbol"], df["date"]))
    conn.executemany("DELETE FROM daily_bar WHERE symbol = ? AND date = ?", pairs)
    conn.commit()
    df.to_sql("daily_bar", conn, if_exists="append", index=False, chunksize=2000)
    conn.commit()
    buf.clear()


def _flush(conn: sqlite3.Connection, buf: list[list]) -> None:
    """把缓冲区写入 daily_bar（按 股票×日期 定点删除，保证幂等且不误伤他人）。"""
    if not buf:
        return
    df = pd.DataFrame(buf, columns=BAR_COLUMNS)
    for c in ("open", "high", "low", "close", "volume", "amount", "turn", "pctChg"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["isST"] = pd.to_numeric(df["isST"], errors="coerce").fillna(0).astype(int)
    df["tradestatus"] = (
        pd.to_numeric(df["tradestatus"], errors="coerce").fillna(1).astype(int)
    )
    df = df.dropna(subset=["close"])
    df = df[df["volume"].fillna(0) > 0]
    df = df.drop_duplicates(subset=["symbol", "date"], keep="last")
    df = df.rename(columns=BAR_RENAME)[DB_COLUMNS]
    if df.empty:
        buf.clear()
        return

    # 只删本批真正覆盖的 (symbol, date)，不能用「按日期整片删」——
    # 多个 worker 并行写同一个库，删整片会把别人刚写好的数据抹掉。
    pairs = list(zip(df["symbol"], df["date"]))
    conn.executemany(
        "DELETE FROM daily_bar WHERE symbol = ? AND date = ?", pairs
    )
    conn.commit()
    df.to_sql("daily_bar", conn, if_exists="append", index=False, chunksize=2000)
    conn.commit()
    buf.clear()


BAR_COLUMNS = [
    "symbol", "date", "open", "high", "low", "close",
    "volume", "amount", "turn", "pctChg", "isST", "tradestatus",
]

#: baostock 原始字段名 → daily_bar 表列名
BAR_RENAME = {"pctChg": "pct_chg", "isST": "is_st"}

#: daily_bar 的实际列顺序（必须与 db.SCHEMA 一致）
DB_COLUMNS = [
    "symbol", "date", "open", "high", "low", "close",
    "volume", "amount", "turn", "pct_chg", "is_st", "tradestatus",
]


def backfill_bars(
    conn: sqlite3.Connection,
    symbols: list[str],
    start_date: str,
    end_date: str,
    *,
    workers: int = config.BACKFILL_WORKERS,
    stagger: float = config.LOGIN_STAGGER_SEC,
) -> tuple[int, int]:
    """回填/增量更新日 K（后复权）。

    实现方式：**分块短生命周期子进程**。父进程按 CHUNK_SIZE 把待抓列表切片，
    每片起一个子进程（独立 login → 抓完 → 写库 → 退出），并带硬超时；
    超时/异常的子进程被强杀，该片的股票留到下次重跑（断点续传）。

    这么设计是因为实测 baostock 的**长驻会话会退化**：单进程连续抓几百只后
    连接会断而不报错，客户端 100% CPU 空转且不再有网络连接，数据永远停在 0 行。

    返回 (成功股票数, 写入行数)。
    """
    latest: dict[str, str] = {}
    for sym, d in conn.execute(
        "SELECT symbol, MAX(date) AS d FROM daily_bar GROUP BY symbol"
    ):
        latest[sym] = d

    tasks: list[tuple[str, str]] = []   # (symbol, start)
    skipped = 0
    for symbol in symbols:
        last = latest.get(symbol)
        if last and last >= end_date:
            skipped += 1
            continue
        start = last or start_date
        if last:
            start = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
        if start > end_date:
            skipped += 1
            continue
        tasks.append((symbol, start))

    total = len(symbols)
    print(f"[backfill] 待抓取 {len(tasks)} 只，已最新跳过 {skipped} 只，共 {total} 只")

    if not tasks:
        return skipped, 0

    db_path = str(Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve())

    # **串行短会话块**。实测结论：
    #   - 4 进程错峰(140s) 登录成功，但每进程只抓 20 只就退出 → 未登录查询 = 0
    #   - 6 进程同时启动、各自抓 ~870 只 → 全部登录成功但**卡在等待**，
    #     CPU 近零、无数据增长（服务端限制同 IP 的并发长会话）
    #   - 单进程连续抓几百只会退化成 100% CPU 空转且无网络连接
    # 因此唯一稳定模式是：**一次一个进程，抓有限只，立刻退出释放连接**。
    chunk_size = config.CHUNK_SIZE
    n_chunks = (len(tasks) + chunk_size - 1) // chunk_size

    done = skipped
    total_rows = 0
    total_failed = 0
    t0 = time.time()

    for i in range(n_chunks):
        part = tasks[i * chunk_size:(i + 1) * chunk_size]
        chunk_syms = [s for s, _ in part]
        start = min(st for _, st in part)
        res = fetch_chunk(db_path, chunk_syms, start, end_date)
        done += res.get("ok", 0)
        total_rows += res.get("rows", 0)
        total_failed += res.get("failed", 0)
        flag = " [超时]" if res.get("timed_out") else ""
        err = f" err={res['error']}" if res.get("error") else ""
        print(
            f"[backfill] 块 {i+1}/{n_chunks}（{len(chunk_syms)} 只，累计 "
            f"{time.time()-t0:.0f}s）: 成功 {res.get('ok',0)} 失败 {res.get('failed',0)} "
            f"行 {res.get('rows',0)} | 累计 {done}/{total} 只{flag}{err}",
            flush=True,
        )
        if res.get("timed_out"):
            print("[backfill] 本块超时，已写入的数据保留；重跑会自动跳过", flush=True)

    print(f"[backfill] 完成：{done} 只股票，写入 {total_rows} 行，失败 {total_failed} 只，"
          f"总用时 {time.time()-t0:.0f}s")
    return done, total_rows


if __name__ == "__main__":
    # 子进程入口：python -m rpsboard.sources.baostock_src <db> <symbols_json> <start> <end>
    _fetch_chunk_main(sys.argv[1:])
