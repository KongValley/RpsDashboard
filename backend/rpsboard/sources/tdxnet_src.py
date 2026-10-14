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

    # ── 后复权日K ────────────────────────────────────────────────
    def _bar_pages(self, code: str, count: int) -> list:
        """按 `start` 偏移翻页取日 K（单页上限 800 根）。

        `start` 是**距最新一根的回退偏移**（实测 count=800,start=800 → 2020-03~2023-06）。
        第二页比第一页更早，所以调用方必须自己按日期排序。
        """
        out: list = []
        start = 0
        while len(out) < count:
            n = min(MAX_COUNT, count - len(out))
            page = self._client.bars.get(
                code, period="day", adjust="hfq", count=n, start=start
            )
            bars = getattr(page, "bars", page) or []
            if not bars:
                break
            out.extend(bars)
            start += len(bars)
            if len(bars) < n:  # 已到该标的上市首日
                break
        return out

    def hfq_bars(self, symbol: str, count: int = config.BARS_LIMIT) -> pd.DataFrame:
        bars = self._bar_pages(to_full_code(symbol), count)
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
                getattr(b, "volume_lots", None),
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

    # ── 指数 ─────────────────────────────────────────────────────
    def index_bars(self, symbol: str, count: int = config.BARS_LIMIT) -> pd.DataFrame:
        bars = self._bar_pages(symbol, count)
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
def backfill_bars(
    conn: sqlite3.Connection,
    symbols: list[str] | None = None,
    *,
    count: int = config.BARS_LIMIT,
    workers: int = config.TDX_WORKERS,
    progress_every: int = 300,
) -> tuple[int, int, list[str]]:
    """用 eltdx 全量刷新日 K（后复权）。

    返回 (成功股票数, 写入行数, 失败代码列表)。

    ⚠ 失败列表必须往上传：调用方要靠它区分「全成功」与「只补到一部分」，
    并把失败代码写进 collect_log —— 旧版本把 failed 打成一行 print 就丢掉了，
    页面「最近更新」于是永远显示成功。
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
        print(f"[eltdx] 待抓取 {len(todo)} 只，{workers} 线程并发")

        frames: list[pd.DataFrame] = []
        failed: list[str] = []
        done = 0

        def one(sym: str):
            try:
                # 单只失败重试（config.MAX_RETRIES）：全市场 5000+ 只里偶发超时很常见，
                # 一次失败就丢掉这只股票的整段历史代价太大
                bars = with_retry(lambda: src.hfq_bars(sym, count=count),
                                  max_retries=config.MAX_RETRIES)
                return sym, bars
            except Exception:  # noqa: BLE001
                return sym, pd.DataFrame()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(one, s) for s in todo]
            for fut in as_completed(futs):
                sym, df = fut.result()
                done += 1
                if df.empty:
                    failed.append(sym)
                else:
                    df = df.assign(symbol=sym)
                    frames.append(df)
                if progress_every and done % progress_every == 0:
                    el = time.time() - t_all
                    rate = done / max(el, 1e-9)
                    eta = (len(todo) - done) / max(rate, 1e-9)
                    print(f"[eltdx] {done}/{len(todo)} 失败 {len(failed)} "
                          f"| {rate:.0f} 只/秒 | 预计剩余 {eta/60:.1f} 分钟", flush=True)

        # ── 指数 ──────────────────────────────────────────────────
        bench_failed: list[str] = []
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
            except Exception as exc:  # noqa: BLE001
                # 原来这里是 pass：基准取不到时回测的基准线会整条空掉，而用户
                # 只会以为「基准没画出来」，找不到原因
                bench_failed.append(f"{sym}({exc})")
        if bench_failed:
            print(f"[eltdx] 基准指数抓取失败: {'; '.join(bench_failed)} "
                  f"—— 回测的基准线会为空，可用 cli backfill 重试")

        if not frames:
            print("[eltdx] 未取到任何数据")
            return 0, 0, failed

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
        cols = ["symbol", "date", "open", "high", "low", "close",
                "volume", "amount", "pct_chg", "is_st", "tradestatus"]
        df = df[cols]

        # 写入必须是**单事务 upsert**：(symbol, date) 是主键，INSERT OR REPLACE 天然幂等。
        # 旧写法是「先把 4.47M 个 (symbol,date) 对逐个 DELETE 并 commit，再 to_sql 插入
        # 并 commit」——两次提交之间有一个**真实的空窗**：这中间被杀/断电/磁盘满，
        # 数据库就停在「已删未写」的状态，全市场日 K 直接蒸发（2026-10-14 实测发生：
        # 435 万行只剩 1 万行，且因为删除已提交，取数进程被中断后无法回滚）。
        # 现在改成先写临时表，最后一条 INSERT OR REPLACE ... SELECT 在同一条事务里
        # 完成替换：要么全是旧数据，要么全是新数据，中途中断不留空窗。
        # 另外不再按 (symbol,date) 删旧行 —— 那样会把超出 800 根窗口的**历史**（早期
        # 少数股票只在这里有 2023-02~06 的 bar）也一并删掉，而 upsert 不会。
        stage = "_bars_stage"
        col_list = ", ".join(cols)
        df[cols].to_sql(stage, conn, if_exists="replace", index=False, chunksize=5000)
        conn.commit()
        # 按 symbol 分批 upsert，每批一次提交。相比「一条大语句写完所有行」：
        # (a) 任何时刻都不会出现「已删未写」的空窗 —— 每批提交只是新增/覆盖若干股票；
        # (b) 写锁不会被一条几分钟的长事务占住（实测旁路命令直接 database is locked）；
        # (c) 中途被杀只需重跑，已写入的批不受影响。
        conn.execute(f"CREATE INDEX IF NOT EXISTS ix_bars_stage_symbol ON {stage}(symbol)")
        symbols = list(df["symbol"].drop_duplicates())
        for i in range(0, len(symbols), 400):
            chunk = symbols[i:i + 400]
            marks = ",".join("?" * len(chunk))
            conn.execute(
                f"INSERT OR REPLACE INTO daily_bar({col_list}) "
                f"SELECT {col_list} FROM {stage} WHERE symbol IN ({marks})",
                tuple(chunk),
            )
            conn.commit()
        conn.execute(f"DROP TABLE {stage}")
        conn.commit()

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

        n_sym = df["symbol"].nunique()
        print(f"[eltdx] 完成：{n_sym} 只 / {len(df)} 行，失败 {len(failed)} 只，"
              f"总用时 {time.time()-t_all:.0f}s")
        return n_sym, len(df), failed
    finally:
        src.close()
