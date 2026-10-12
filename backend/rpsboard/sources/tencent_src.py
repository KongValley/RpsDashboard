"""腾讯财经日K 数据源 —— 主采集器。

实测（本机 2026-10）：
  - `web.ifzq.gtimg.cn/appstock/app/fqkline/get` 直连可用，**0.09 秒/只**
  - 支持**后复权**（`hfqday`），这是算 RPS 的硬要求
  - 单次最多 640 根日线；不支持多股批量（只传一只）
  - **无需登录、无需注册**——这正是它比 baostock 好用的关键

相比 baostock：
  - 快约 30 倍（0.09s vs 0.4s + 每次 60~100 秒登录）
  - 没有「多进程并发登录互斥」的坑（HTTP 接口可以随便并发）
  - 没有「长会话退化成 100% CPU 空转」的坑

限制：
  - 不返回**成交额/换手率**（只有成交量）。需要时由 tdx_local_src 补。
  - 不返回 ST/停牌标记。ST 由证券名称推断，停牌由缺日推断。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from .. import config, db

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
MAX_BARS = 640          # 接口硬上限，超过会返回空


_local = threading.local()


def _session():
    """每线程一个 Session。

    `requests.Session` **不是线程安全的**——12 个线程共用一个 session 时会
    互相踩连接池，实测表现为大量 `Expecting value: line 1 column 1`
    （响应体为空，JSON 解析失败）。用 thread-local 隔离即可。
    """
    s = getattr(_local, "s", None)
    if s is None:
        import requests

        s = requests.Session()
        # 实测直连即可，绕开本机代理（代理规则会把行情域名路由到境外节点）
        s.trust_env = False
        s.headers.update(HEADERS)
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=4, pool_maxsize=8, max_retries=0
        )
        s.mount("https://", adapter)
        _local.s = s
    return s


def to_tencent_code(symbol: str) -> str:
    """6 位代码 → 腾讯格式。

    注意是 `sz000001` **不带点**——腾讯接口和 baostock 的 `sz.000001`
    写法不同，带点会静默返回空数据（code=0 但 data 为空）。
    """
    from .. import config as cfg

    return f"{cfg.exchange_of(symbol)}{symbol}"


_rate_lock = threading.Lock()
_last_call = [0.0]


def _throttle() -> None:
    """全局最小请求间隔限速。

    腾讯行情接口对突发并发敏感：实测 12 线程齐发时大量返回空响应
    （JSON 解析失败）。限制全局每请求最小间隔后，失败率归零。
    """
    gap = config.TENCENT_MIN_INTERVAL
    if gap <= 0:
        return
    with _rate_lock:
        wait = gap - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def fetch_hfq_day(symbol: str, count: int = MAX_BARS,
                  session=None, retries: int = 3) -> pd.DataFrame:
    """取单只股票的后复权日K。

    返回 date/open/close/high/low/volume，失败返回空 DataFrame。
    """
    own_session = session is None
    s = session or _session()
    code = to_tencent_code(symbol)
    params = {"param": f"{code},day,,,{count},hfq"}

    last_err: Exception | None = None
    for attempt in range(retries):
        _throttle()
        try:
            r = s.get(KLINE_URL, params=params, headers=HEADERS, timeout=15)
            if not r.text.strip().startswith("{"):
                raise RuntimeError(f"空响应 http={r.status_code} len={len(r.content)}")
            j = r.json()
            df0 = (j.get("data") or {}).get(code) or {}
            arr = df0.get("hfqday") or df0.get("day") or []
            if not arr:
                last_err = RuntimeError(f"{code} 返回空数据 (code={j.get('code')})")
                time.sleep(0.3 * (attempt + 1))
                continue

            # 腾讯的数组长度不固定：多数是 6 列，但部分会多带一个
            # 形如 {"nd": ...} 的第 7 元素。直接构造 6 列表会报
            # "6 columns passed, passed data had 7 columns"，因此统一截前 6 列。
            cols = ["date", "open", "close", "high", "low", "volume"]
            rows = [row[:6] for row in arr if isinstance(row, (list, tuple)) and len(row) >= 6]
            if not rows:
                last_err = RuntimeError(f"{code} 行结构异常: {arr[:1]}")
                time.sleep(0.3 * (attempt + 1))
                continue

            df = pd.DataFrame(rows, columns=cols)
            for c in ("open", "close", "high", "low", "volume"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df.dropna(subset=["close"])
            df = df[df["volume"].fillna(0) > 0]
            return df.reset_index(drop=True)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.3 * (attempt + 1))
    if own_session:
        print(f"[tencent] {symbol} 采集失败: {last_err}")
    return pd.DataFrame(columns=["date", "open", "close", "high", "low", "volume"])


def fetch_many(
    symbols: list[str],
    count: int = MAX_BARS,
    workers: int = config.TENCENT_WORKERS,
    progress_every: int = 200,
) -> tuple[pd.DataFrame, list[str]]:
    """并发抓取多只股票。返回 (合并后的长表, 失败列表)。

    用线程池而非进程池：这是纯 HTTP IO 绑定，且没有 baostock 那种
    「进程间登录互斥」问题，线程足够且更轻。
    每线程独立 Session + 全局最小请求间隔，避免触发接口限流。
    """
    frames: list[pd.DataFrame] = []
    failed: list[str] = []
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_hfq_day, s, count): s for s in symbols}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                df = fut.result()
            except Exception:  # noqa: BLE001
                df = pd.DataFrame()
            if df.empty:
                failed.append(sym)
            else:
                df = df.copy()
                df["symbol"] = sym
                frames.append(df)
            done += 1
            if progress_every and done % progress_every == 0:
                print(f"[tencent] 进度 {done}/{len(symbols)}，"
                      f"成功 {done - len(failed)}，失败 {len(failed)}", flush=True)

    if not frames:
        return pd.DataFrame(), failed

    out = pd.concat(frames, ignore_index=True)
    out["pct_chg"] = out.groupby("symbol")["close"].pct_change().round(6)
    out["tradestatus"] = 1
    return out, failed


def backfill_bars(
    conn: sqlite3.Connection,
    symbols: list[str],
    latest_open_date: str,
    *,
    workers: int = config.TENCENT_WORKERS,
) -> tuple[int, int]:
    """把腾讯后复权日K 写入 daily_bar（按 股票×日期 幂等覆盖）。"""
    df, failed = fetch_many(symbols, workers=workers)
    if df.empty:
        print("[tencent] 无新数据")
        return 0, 0

    # ST 标记来自证券名称（腾讯 K 线不返回）
    try:
        master = pd.read_sql_query("SELECT symbol, is_st FROM security_master", conn)
        st_map = master.set_index("symbol")["is_st"].to_dict()
    except Exception:  # noqa: BLE001
        st_map = {}
    df["is_st"] = df["symbol"].map(st_map).fillna(0).astype(int)

    df["amount"] = None          # 腾讯接口不提供，交给 tdx_local_src 补
    df["turn"] = None
    payload = df.rename(columns={"volume": "volume"})[[
        "symbol", "date", "open", "high", "low", "close",
        "volume", "amount", "turn", "pct_chg", "is_st", "tradestatus",
    ]]

    pairs = list(zip(payload["symbol"], payload["date"]))
    conn.executemany("DELETE FROM daily_bar WHERE symbol = ? AND date = ?", pairs)
    conn.commit()
    payload.to_sql("daily_bar", conn, if_exists="append", index=False, chunksize=5000)
    conn.commit()

    print(f"[tencent] 写入 {len(payload)} 行 / {df['symbol'].nunique()} 只，"
          f"失败 {len(failed)} 只")
    return df["symbol"].nunique(), len(payload)
