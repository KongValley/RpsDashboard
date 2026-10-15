"""FastAPI 应用工厂与公共依赖。

硬性约束：**除 `api/watchlist.py` 里对 watchlist / alert_rule / alert_hit 三张表的
少量写操作外，所有 handler 只读 SQLite，且都不在请求链路内做外部采集**
（eltdx 取数在 CLI 里，见 sources/tdxnet_src.py）。
外部数据采集一律由 CLI / 定时任务离线完成。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .. import __version__, db
from . import perf

_cache: dict[str, Any] = {"conn": None, "opened": None}


def get_conn() -> sqlite3.Connection:
    """复用单个只读连接（API 层不做写操作，无需每请求新建）。"""
    if _cache["conn"] is None:
        _cache["conn"] = db.connect()
    return _cache["conn"]  # type: ignore[return-value]


def latest_bar_date(conn: sqlite3.Connection) -> str | None:
    # 刻意不用 count_rows() 判空：COUNT(*) 在 400 万行的表上要 235ms，
    # 而这里每个请求都会走到一次。MAX(date) 走索引 seek，实测 0.0ms。
    if not db.table_has_rows(conn, "daily_bar"):
        return None
    row = conn.execute("SELECT MAX(date) AS d FROM daily_bar").fetchone()
    return row["d"] if row and row["d"] else None


def _latest_rps_date(conn: sqlite3.Connection) -> str | None:
    """rps_daily 的最新日期；表不存在或没有行时返回 None。

    用 table_has_rows 而不是 COUNT(*)：4 百万行的表上 COUNT 要 235ms，
    而这个函数每个请求都会走到一次。
    """
    if not db.table_has_rows(conn, "rps_daily"):
        return None
    row = conn.execute("SELECT MAX(date) AS d FROM rps_daily").fetchone()
    return row["d"] if row and row["d"] else None


def resolve_date(conn: sqlite3.Connection, raw: str | None) -> str | None:
    """把请求里的日期解析为一个**实际有 RPS 数据的交易日**。

    看板不能因为「周末请求」就报 400——回退到最近的有数据日期。
    前端接上日期下拉之后，这里必须校验传入日期是否真的存在：
    否则用户选到一个停市日或数据缺口日，页面会**静默显示空白**，
    看起来像「当天没有强势股」，实际是查询打在了不存在的日期上。

    回退策略是**就近**：优先取不晚于该日的最近交易日（周末/长假向后落回），
    一个都没有才取更早的下一个交易日。直接退回「最新交易日」是错的——
    用户选 2024-05-05 会被无声地换成 2026-10-12，看到的完全是另一天的数据。

    兜底日期取自 rps_daily，**不是** daily_bar：只拉了日K、还没算 RPS 时，
    用 K 线日期兜底会让排行榜安静地返回空表，看起来像「今天没有强势股」。
    返回 None 时调用方会给出「数据库为空，请先执行 cli init + backfill +
    compute-rps」的明确提示。
    """
    fallback = _latest_rps_date(conn)
    if not raw:
        return fallback
    raw = str(raw).strip()
    if not raw:
        return fallback
    hit = conn.execute(
        "SELECT 1 FROM rps_daily WHERE date = ? LIMIT 1", (raw,)
    ).fetchone()
    if hit:
        return raw
    near = conn.execute(
        "SELECT date FROM rps_daily WHERE date <= ? ORDER BY date DESC LIMIT 1", (raw,)
    ).fetchone()
    if near and near["date"]:
        return near["date"]
    near = conn.execute(
        "SELECT date FROM rps_daily WHERE date > ? ORDER BY date ASC LIMIT 1", (raw,)
    ).fetchone()
    return near["date"] if near and near["date"] else fallback


def nth_previous_date(conn: sqlite3.Connection, after: str, n: int) -> str | None:
    """`after` 之前的第 n 个**有 RPS 数据的交易日**（n=1 表示紧邻的前一日）。

    必须按交易日算而不是「日期减 n」：周末与节假日会让日历差和交易差
    越偏越远，比较基准日就会错位。
    """
    if n <= 0:
        return after
    row = conn.execute(
        "SELECT date FROM (SELECT DISTINCT date FROM rps_daily WHERE date < ? "
        "ORDER BY date DESC) LIMIT 1 OFFSET ?",
        (after, n - 1),
    ).fetchone()
    return row["date"] if row and row["date"] else None


def read_sql(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> pd.DataFrame:
    """执行只读查询。

    表不存在（如尚未 init_db）时返回空表；但**其它 SQL 错误必须抛出**——
    静默吞掉会把「列名写错」这类 bug 伪装成「查不到数据」，极难排查。
    """
    try:
        return pd.read_sql_query(sql, conn, params=params)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return pd.DataFrame()
        raise


def jsonable(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    return df.replace({float("nan"): None}).to_dict("records")


def create_app() -> FastAPI:
    app = FastAPI(
        title="A股 RPS 看板",
        description="欧奈尔相对价格强度看板 · 数据源 eltdx（通达信行情协议，TCP 直连）",
        version=__version__,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 放在 CORS 之后：Starlette 里后加者在外层，perf 要包住全部请求
    app.add_middleware(perf.PerfMiddleware)

    from . import backtest as bt_api
    from . import factors, meta, rps, stock, trade, watchlist

    app.include_router(perf.router)
    app.include_router(watchlist.router)
    app.include_router(meta.router)
    app.include_router(rps.router)
    app.include_router(stock.router)
    app.include_router(bt_api.router)
    app.include_router(trade.router)
    app.include_router(factors.router)

    _mount_frontend(app)

    @app.on_event("shutdown")
    def _close_conn() -> None:
        if _cache["conn"] is not None:
            _cache["conn"].close()
            _cache["conn"] = None

    return app


def _mount_frontend(app: FastAPI) -> None:
    """若已构建前端（frontend/dist），直接由后端托管，单端口即可访问。

    这样日常使用不必同时起 vite dev server —— 既省一个进程，
    也避开了 dev server 的 HMR/模块缓存在部分浏览器下不稳定的问题。
    开发时仍可用 vite dev（5173）配合本 API（8000）。
    """
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    dist = (Path(__file__).resolve().parents[3] / "frontend" / "dist").resolve()
    index = dist / "index.html"
    if not index.is_file():
        # 新人 clone 下来直接跑 uvicorn 时，/ 会是 404；把原因和两条出路直接说出来
        print("[api] 未找到 frontend/dist：本次只提供 API（/ 不会返回页面）。"
              "前端开发用 vite dev（5173），或先 `npm run build` 由后端单端口托管")
        return

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/", include_in_schema=False)
    def _root() -> FileResponse:
        return FileResponse(str(index))

    @app.get("/{path:path}", include_in_schema=False)
    def _spa(path: str) -> FileResponse:
        # 静态资源直接命中；其余路径一律回落到 index.html 交给 vue-router。
        # **必须**校验归一化后仍落在 dist 内：`Path(dist) / "/C:/Windows/win.ini"`
        # 会直接替换掉前缀、`../../.git/config` 会往上走，实测两者都能读到真实文件。
        candidate = (dist / path).resolve()
        if candidate.is_file() and candidate.is_relative_to(dist):
            return FileResponse(str(candidate))
        return FileResponse(str(index))


app = create_app()
