"""eltdx 数据源（通达信行情协议）—— **主采集源**。

为什么是它（2026-10-12 本机实测）：
  - `connect()`            **0.2 秒**（baostock 要 60~100 秒）
  - `latest_stock_list()`  **1.6 秒 / 5580 只**（baostock ~90 秒）
  - `bars.get(hfq)`        **0.04 秒/只**（baostock 0.4s、腾讯 0.09s 但高并发触发 WAF）
  - 数据当日更新（本机取到 2026-10-12）
  - TCP 直连行情服务器，**不走网页接口 → 无 IP 限流、无 WAF、无代理问题**

对比其它源在本机的实测：
  - baostock   可用但慢 25 倍，且多进程并发登录会返回 10001001「用户未登录」
  - 腾讯       0.09s/只，但 12 线程并发直接触发 waf.tencent.com 封禁
  - akshare/东财 网络阻断（ProxyError / RemoteDisconnected）
  - 本地通达信 .day 文件 22 秒全量但只有不复权价，且数据滞后一个月

单次 `bars.get` 的 count 上限为 800（通达信协议分页限制），
800 根 ≈ 3.3 年，足够 RPS250 预热 + 图表展示。
"""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

from .. import config, db
from .base import tradestatus_from_calendar, with_retry

MAX_COUNT = 800          # 通达信协议单页上限
CONNECT_POOL = 8
CONNECT_TIMEOUT = 10

#: 基准指数：eltdx 用的全代码 → 库内短代码
BENCHMARKS = (("sh000300", "000300"), ("sh000001", "000001"), ("sz399001", "399001"))


def to_full_code(symbol: str) -> str:
    """6 位代码 → 通达信完整代码（sh600519 / sz000001 / bj920000）。"""
    c = str(symbol).zfill(6)
    if c.startswith(("6", "9")) and not c.startswith("92"):
        return "sh" + c
    if c.startswith(("4", "8", "92")):
        return "bj" + c
    return "sz" + c


class TdxSource:
    """连接池化的 eltdx 客户端。线程安全（底层自带连接池）。"""

    def __init__(self) -> None:
        import eltdx

        self._client = eltdx.TdxClient.from_hosts(
            pool_size=CONNECT_POOL, timeout=CONNECT_TIMEOUT, probe_hosts=False
        )
        self._client.connect()

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    # ── 证券清单 ──────────────────────────────────────────────────
    def stock_list(self) -> pd.DataFrame:
        rows = self._client.helpers.latest_stock_list() or []
        out = []
        for r in rows:
            code = getattr(r, "code", None)
            if not code:
                continue
            out.append({
                "symbol": str(code).zfill(6),
                "name": getattr(r, "name", "") or "",
                "exchange": getattr(r, "exchange", "") or "",
            })
        df = pd.DataFrame(out)
        if df.empty:
            raise RuntimeError("eltdx latest_stock_list 返回空")
        df = df.drop_duplicates(subset=["symbol"], keep="first")
        df["is_st"] = df["name"].str.contains("ST", na=False).astype(int)
        return df

    # ── 后复权 / 不复权日K ───────────────────────────────────────
    def _bar_pages(self, code: str, count: int, *,
                   adjust: str | None = "hfq") -> list:
        """按 `start` 偏移翻页取日 K（单页上限 800 根）。

        `adjust` 直接透传给 SDK：`"hfq"` 取后复权（研究与 RPS 用），
        `None` 取**不复权**（模拟账户的成交与估值用，金额/股数才是真实现金口径）。
        **不从 HFQ 反算原价**：官方明示本地反算与服务端复权会差到 0.01~0.02 元。

        `start` 是**距最新一根的回退偏移**（实测 count=800,start=800 → 2020-03~2023-06）。
        第二页比第一页更早，所以调用方必须自己按日期排序。
        """
        out: list = []
        start = 0
        while len(out) < count:
            n = min(MAX_COUNT, count - len(out))
            page = self._client.bars.get(
                code, period="day", adjust=adjust, count=n, start=start
            )
            bars = getattr(page, "bars", page) or []
            if not bars:
                break
            out.extend(bars)
            start += len(bars)
            if len(bars) < n:  # 已到该标的上市首日
                break
        return out

    def _bars_frame(self, symbol: str, count: int, *,
                    adjust: str | None, volume_attr: str) -> pd.DataFrame:
        bars = self._bar_pages(to_full_code(symbol), count, adjust=adjust)
        if not bars:
            return pd.DataFrame()
        recs = []
        for b in bars:
            t = getattr(b, "time", None)
            if t is None:
                continue
            recs.append((
                t.strftime("%Y-%m-%d"),
                getattr(b, "open", None),
                getattr(b, "high", None),
                getattr(b, "low", None),
                getattr(b, "close", None),
                getattr(b, volume_attr, None),
                getattr(b, "amount", None),
            ))
        df = pd.DataFrame(
            recs, columns=["date", "open", "high", "low", "close", "volume", "amount"]
        )
        for c in ("open", "high", "low", "close", "volume", "amount"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["close"]).drop_duplicates(subset=["date"], keep="last")
        # 排序是硬要求：分页取回来的顺序是「新→旧」，而 tradestatus 的判定
        # （sources/base.py）要求 (symbol, date) 升序，顺序不对会直接报错。
        return df.sort_values("date").reset_index(drop=True)

    def hfq_bars(self, symbol: str, count: int = config.BARS_LIMIT) -> pd.DataFrame:
        """后复权日 K（`volume` 是**手**，与 daily_bar.volume 同口径）。"""
        return self._bars_frame(symbol, count, adjust="hfq",
                                volume_attr="volume_lots")

    def raw_bars(self, symbol: str, count: int = config.BARS_LIMIT) -> pd.DataFrame:
        """不复权日 K（`volume` 是**股**，取 SDK 的 `volume_wire_value`）。

        模拟账户用这条：现金、股数、佣金起收点、整手取整全在真实价格空间里，
        不再拿后复权价当钱用（实测同一只票 hfq/raw 平均差 8.8 倍）。
        """
        return self._bars_frame(symbol, count, adjust=None,
                                volume_attr="volume_wire_value")

    # ── 指数 ─────────────────────────────────────────────────────
    def index_bars(self, symbol: str, count: int = config.BARS_LIMIT) -> pd.DataFrame:
        # 指数不需要复权；显式传 'hfq' 保持与历史行为一致（服务端对指数忽略它）。
        bars = self._bar_pages(symbol, count, adjust="hfq")
        if not bars:
            return pd.DataFrame()
        recs = [(b.time.strftime("%Y-%m-%d"), getattr(b, "close", None)) for b in bars]
        df = pd.DataFrame(recs, columns=["date", "close"])
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        return (
            df.dropna(subset=["close"])
            .drop_duplicates(subset=["date"], keep="last")
            .sort_values("date")
            .reset_index(drop=True)
        )


# ── 全市场回填 ──────────────────────────────────────────────────────
#: daily_bar（后复权，volume=手）与 raw_bar（不复权，volume=股）的列清单。
_BAR_COLS = ["symbol", "date", "open", "high", "low", "close",
             "volume", "amount", "pct_chg", "is_st", "tradestatus"]
_RAW_COLS = ["symbol", "date", "open", "high", "low", "close",
             "volume", "amount", "tradestatus"]


def _upsert_by_symbol(
    conn: sqlite3.Connection,
    df: pd.DataFrame,
    table: str,
    cols: list[str],
    *,
    chunk: int = 400,
) -> int:
    """先写临时表，再按 symbol 分批 `INSERT OR REPLACE ... SELECT` 落进目标表。

    写入必须是**单事务 upsert**：(symbol, date) 是主键，INSERT OR REPLACE 天然幂等。
    旧写法是「先把 447 万个 (symbol,date) 对逐个 DELETE 并 commit，再 to_sql 插入
    并 commit」——两次提交之间有一个**真实的空窗**：这中间被杀/断电/磁盘满，
    数据库就停在「已删未写」的状态，全市场日 K 直接蒸发（2026-10-14 实测发生：
    435 万行只剩 1 万行，且因为删除已提交，取数进程被中断后无法回滚）。
    现在改成先写临时表，最后一条 INSERT OR REPLACE ... SELECT 在同一条事务里
    完成替换：要么全是旧数据，要么全是新数据，中途中断不留空窗。
    另外不再按 (symbol,date) 删旧行 —— 那样会把超出 800 根窗口的**历史**（早期
    少数股票只在这里有 2023-02~06 的 bar）也一并删掉，而 upsert 不会。

    按 symbol 分批提交的好处：(a) 任何时刻都不会出现「已删未写」的空窗；
    (b) 写锁不会被一条几分钟的长事务占住（实测旁路命令直接 database is locked）；
    (c) 中途被杀只需重跑，已写入的批不受影响。
    """
    if df.empty:
        return 0
    stage = f"_{table}_stage"
    col_list = ", ".join(cols)
    df[cols].to_sql(stage, conn, if_exists="replace", index=False, chunksize=5000)
    conn.commit()
    conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{stage}_symbol ON {stage}(symbol)")
    syms = list(df["symbol"].drop_duplicates())
    for i in range(0, len(syms), chunk):
        part = syms[i:i + chunk]
        marks = ",".join("?" * len(part))
        conn.execute(
            f"INSERT OR REPLACE INTO {table}({col_list}) "
            f"SELECT {col_list} FROM {stage} WHERE symbol IN ({marks})",
            tuple(part),
        )
        conn.commit()
    conn.execute(f"DROP TABLE {stage}")
    conn.commit()
    return len(df)


def _raw_tradestatus(df: pd.DataFrame) -> pd.Series:
    """不复权行的「可交易」标记：1 = 正常可成交，0 = 停牌/无效（都不可成交）。

    无效 = OHLC 非有限正数、或 high/low 与 open/close 不自洽、或成交量非正。
    这类行**保留在库里**（它们是原始事实），但绝不能参与撮合或估值 ——
    拿一个 0 或 NaN 的收盘价做市值会把净值算到荒谬的地方。
    """
    ohlc = df[["open", "high", "low", "close"]]
    finite = np.isfinite(ohlc).all(axis=1) & (ohlc > 0).all(axis=1)
    consistent = (df["low"] <= df[["open", "close"]].min(axis=1)) & \
                 (df["high"] >= df[["open", "close"]].max(axis=1))
    vol_ok = np.isfinite(df["volume"]) & (df["volume"] > 0)
    return (finite & consistent & vol_ok).astype(int)


def backfill_bars(
    conn: sqlite3.Connection,
    symbols: list[str] | None = None,
    *,
    count: int = config.BARS_LIMIT,
    workers: int = config.TDX_WORKERS,
    progress_every: int = 300,
) -> tuple[int, int, list[str]]:
    """用 eltdx 全量刷新日 K：**同时**取后复权（daily_bar，供研究）与
    不复权（raw_bar，供模拟账户的成交与估值）。

    返回 (两口径都成功的股票数, 写入行数(daily_bar + raw_bar), 失败代码列表)。

    ⚠ 失败列表必须往上传：调用方要靠它区分「全成功」与「只补到一部分」，
    并把失败代码写进 collect_log —— 旧版本把 failed 打成一行 print 就丢掉了，
    页面「最近更新」于是永远显示成功。现在按两种口径分别记录（日志里能看到
    是 hfq 挂了还是 raw 挂了），返回值里是去重并集。

    收尾还会写一条 `raw-bar-sync` 检查点（collect_log）：`market_date` 是本次
    指数的最新交易日，`latest_complete_date` 只在**全市场**（symbols=None）且
    原价全成功时推进 —— 局部回补成功证明不了全市场都齐。
    """
    t_all = time.time()
    src = TdxSource()
    try:
        t0 = time.time()
        master = src.stock_list()
        print(f"[eltdx] 证券清单 {len(master)} 只，用时 {time.time()-t0:.1f}s")

        st_map = master.set_index("symbol")["is_st"].to_dict()
        db.upsert_master(conn, master[["symbol", "name", "exchange"]].assign(
            ipo_date=None, is_st=master["is_st"], updated_at=db.now()
        ))

        todo = list(master["symbol"]) if symbols is None else symbols
        print(f"[eltdx] 待抓取 {len(todo)} 只，{workers} 线程并发（hfq + raw）")

        hfq_frames: list[pd.DataFrame] = []
        raw_frames: list[pd.DataFrame] = []
        hfq_failed: list[str] = []
        raw_failed: list[str] = []
        done = 0

        def one(sym: str):
            """同一只票取两种口径；任一失败不影响另一口径。"""
            out = {"hfq": pd.DataFrame(), "raw": pd.DataFrame()}
            # 单只失败重试（config.MAX_RETRIES）：全市场 5000+ 只里偶发超时很常见，
            # 一次失败就丢掉这只股票的整段历史代价太大。
            try:
                out["hfq"] = with_retry(lambda: src.hfq_bars(sym, count=count),
                                        max_retries=config.MAX_RETRIES)
            except Exception:  # noqa: BLE001
                pass
            try:
                out["raw"] = with_retry(lambda: src.raw_bars(sym, count=count),
                                        max_retries=config.MAX_RETRIES)
            except Exception:  # noqa: BLE001
                pass
            return sym, out

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(one, s) for s in todo]
            for fut in as_completed(futs):
                sym, out = fut.result()
                done += 1
                if out["hfq"].empty:
                    hfq_failed.append(sym)
                else:
                    hfq_frames.append(out["hfq"].assign(symbol=sym))
                if out["raw"].empty:
                    raw_failed.append(sym)
                else:
                    raw_frames.append(out["raw"].assign(symbol=sym))
                if progress_every and done % progress_every == 0:
                    el = time.time() - t_all
                    rate = done / max(el, 1e-9)
                    eta = (len(todo) - done) / max(rate, 1e-9)
                    print(f"[eltdx] {done}/{len(todo)} hfq失败 {len(hfq_failed)} "
                          f"raw失败 {len(raw_failed)} | {rate:.0f} 只/秒 "
                          f"| 预计剩余 {eta/60:.1f} 分钟", flush=True)

        # ── 指数 ──────────────────────────────────────────────────
        bench_failed: list[str] = []
        mkt: str | None = None
        for code, sym in BENCHMARKS:
            try:
                idx = src.index_bars(code)
                if not idx.empty:
                    idx = idx.assign(symbol=sym)
                    conn.execute("DELETE FROM benchmark_bar WHERE symbol=?", (sym,))
                    idx[["symbol", "date", "close"]].to_sql(
                        "benchmark_bar", conn, if_exists="append", index=False)
                    conn.commit()
                    print(f"[eltdx] 基准 {code} {len(idx)} 根")
                    if sym == "000001":
                        mkt = str(idx["date"].max())
            except Exception as exc:  # noqa: BLE001
                # 原来这里是 pass：基准取不到时回测的基准线会整条空掉，而用户
                # 只会以为「基准没画出来」，找不到原因
                bench_failed.append(f"{sym}({exc})")
        if bench_failed:
            print(f"[eltdx] 基准指数抓取失败: {'; '.join(bench_failed)} "
                  f"—— 回测的基准线会为空，可用 cli backfill 重试")

        n_hfq = _write_hfq(conn, hfq_frames, st_map)
        n_raw = _write_raw(conn, raw_frames)

        # ── 覆盖区间与检查点 ──────────────────────────────────────
        if mkt:
            _update_raw_coverage(conn, raw_frames, mkt)
        scope = "all" if symbols is None else "subset"
        prev = db.read_raw_checkpoint(conn)
        prev_complete = (prev or {}).get("latest_complete_date")
        # 只有全市场任务 + 原价全成功 + 指数成功，才推进全市场完整日。
        complete = mkt if (scope == "all" and not raw_failed and mkt) else prev_complete
        db.write_raw_checkpoint(
            conn, scope=scope, market_date_value=mkt,
            latest_complete_date=complete, raw_failed=raw_failed,
            hfq_failed=hfq_failed + bench_failed)

        ok_both = sorted(set(f for f in todo if f not in set(hfq_failed))
                         & set(f for f in todo if f not in set(raw_failed)))
        failed = sorted(set(hfq_failed) | set(raw_failed))
        print(f"[eltdx] 完成：两口径均成功 {len(ok_both)} 只 / "
              f"daily_bar {n_hfq} 行 + raw_bar {n_raw} 行，"
              f"hfq失败 {len(hfq_failed)} 只，raw失败 {len(raw_failed)} 只，"
              f"总用时 {time.time()-t_all:.0f}s")
        return len(ok_both), n_hfq + n_raw, failed
    finally:
        src.close()


def _write_hfq(conn: sqlite3.Connection, frames: list[pd.DataFrame],
               st_map: dict) -> int:
    """后复权帧 → daily_bar（口径与列清单保持与历史完全一致）。"""
    if not frames:
        print("[eltdx] 后复权未取到任何数据")
        return 0
    df = pd.concat(frames, ignore_index=True)
    df["pct_chg"] = df.groupby("symbol")["close"].pct_change().round(6)
    # 前收盘为 0（或缺失）时 pct_change 会产出 ±inf。实测库里曾有 3 行这种值，
    # 它们会污染任何阈值比较与图表绘制，必须在落库前清掉。
    df["pct_chg"] = df["pct_chg"].replace([np.inf, -np.inf], np.nan)
    df["is_st"] = df["symbol"].map(st_map).fillna(0).astype(int)
    # eltdx 在停牌期间不给 bar（只在复牌日补一根），所以「与上一个交易日的间隔」
    # 就是停牌信号；判定口径见 tradestatus_from_calendar（按交易日，不按自然日）。
    df["tradestatus"] = tradestatus_from_calendar(df)
    df = df.drop_duplicates(subset=["symbol", "date"], keep="last")

    n = _upsert_by_symbol(conn, df, "daily_bar", _BAR_COLS)

    # 组内**首行**的 pct_chg 必为 NaN，而首行正是本帧最早的那一天：
    # 每次运行都会把上一次算好的值覆盖成 NULL（实测库内 pct_chg IS NULL
    # 的 10,662 行全部聚在窗口边界日期上）。这里就地把边界日期补回来，
    # 前值不存在的行（新股首日、前面没有 close>0 的 bar）保持 NULL。
    boundary = sorted(set(df.groupby("symbol")["date"].first()))
    if boundary:
        marks = ",".join("?" * len(boundary))
        conn.execute(
            f"UPDATE daily_bar SET pct_chg = ("
            f"  SELECT ROUND(daily_bar.close / q.close - 1, 6) FROM daily_bar q "
            f"  WHERE q.symbol = daily_bar.symbol AND q.date < daily_bar.date "
            f"    AND q.close > 0 ORDER BY q.date DESC LIMIT 1) "
            f"WHERE date IN ({marks}) AND pct_chg IS NULL "
            f"  AND close IS NOT NULL AND close > 0",
            tuple(boundary),
        )
        conn.commit()
    return n


def _write_raw(conn: sqlite3.Connection, frames: list[pd.DataFrame]) -> int:
    """不复权帧 → raw_bar（volume 单位**股**，tradestatus 由有效性推导）。"""
    if not frames:
        print("[eltdx] 不复权未取到任何数据")
        return 0
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["symbol", "date"], keep="last")
    df["tradestatus"] = _raw_tradestatus(df)
    return _upsert_by_symbol(conn, df, "raw_bar", _RAW_COLS)


def _update_raw_coverage(conn: sqlite3.Connection, frames: list[pd.DataFrame],
                         mkt: str) -> int:
    """记录每只股票「已完整取回」的区间：首根 bar 的日期 → 本轮市场交易日。

    只在 raw 抓取成功的股票上推进；失败的保留旧覆盖（下次成功再更新）。
    """
    if not frames:
        return 0
    rows = []
    for f in frames:
        if f.empty:
            continue
        sym = str(f["symbol"].iloc[0])
        rows.append((sym, str(f["date"].min()), mkt, db.now()))
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO raw_coverage(symbol, date_from, date_to, updated_at)"
        " VALUES (?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET"
        " date_from=excluded.date_from, date_to=excluded.date_to,"
        " updated_at=excluded.updated_at",
        rows)
    conn.commit()
    return len(rows)
