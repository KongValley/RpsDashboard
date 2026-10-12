"""回测接口 + 管理接口（手动触发采集）。"""

from __future__ import annotations

from fastapi import APIRouter, Body
from pydantic import BaseModel

from .. import config, db
from ..analytics import backtest as engine

router = APIRouter(prefix="/api", tags=["backtest"])

#: 回测结果的进程内缓存。回测计算量在秒级，而页面上调阈值/持有期/周期
#: 会反复触发同一批参数的请求，参数不变时直接复用即可。
#: key 里带上 rps_daily 的最新日期，数据一旦更新就自动失效。
_CACHE: dict[tuple, tuple[str, dict]] = {}
_CACHE_MAX = 32


def _cache_token(conn) -> str:
    row = conn.execute("SELECT MAX(date) AS d FROM rps_daily").fetchone()
    return str(row["d"]) if row and row["d"] else ""


@router.get("/backtest/run")
def run(
    period: int = config.DEFAULT_PERIOD,
    threshold: float = 80.0,
    hold: int = 20,
    rebalance: int | None = None,
    resonance: bool = False,
    exclude_st: bool = True,
    exclude_limit_up: bool = True,
    benchmark: str = "000300",
    market_filter: str = "none",
    lookback_days: int | None = None,
) -> dict:
    """执行 RPS 策略回测。

    计算量在秒级，同参数请求走进程内缓存直接返回；返回值中始终携带幸存者偏差警示。
    """
    if period not in config.PERIODS:
        period = config.DEFAULT_PERIOD
    hold = max(1, min(int(hold), 250))
    threshold = max(0.0, min(float(threshold), 100.0))
    if market_filter not in engine.MARKET_FILTERS:
        market_filter = "none"

    key = (
        period, round(threshold, 4), hold, rebalance, resonance,
        exclude_st, exclude_limit_up, benchmark, market_filter, lookback_days,
    )

    conn = db.connect()
    try:
        token = _cache_token(conn)
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == token:
            return dict(cached[1], cached=True)

        res = engine.run_backtest(
            conn,
            period=period,
            threshold=threshold,
            hold=hold,
            rebalance=rebalance,
            resonance=resonance,
            exclude_st=exclude_st,
            exclude_limit_up=exclude_limit_up,
            benchmark_symbol=benchmark,
            market_filter=market_filter,
            lookback_days=lookback_days,
        )
        payload = {
            "equity_curve": res.equity_curve,
            "benchmark_curve": res.benchmark_curve,
            "market_curve": res.market_curve,
            "metrics": res.metrics,
            "buckets": res.buckets,
            "params": res.params,
            "warnings": res.warnings,
            "cached": False,
        }
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = (token, payload)
        return payload
    finally:
        conn.close()


class CollectRequest(BaseModel):
    task: str


@router.post("/admin/collect")
def collect(req: CollectRequest = Body(...)) -> dict:
    """手动触发采集任务（离线执行，接口只负责派发与返回状态）。

    **注意**：这是本地单人使用的看板，不做鉴权，因此只允许触发**只读/幂等**
    的数据同步，不接受任意命令执行。
    """
    allowed = {"backfill", "sync", "spot", "compute-rps"}
    if req.task not in allowed:
        return {"ok": False, "error": f"不支持的任务: {req.task}，可选 {sorted(allowed)}"}

    from ..cli import run_task

    return run_task(req.task)
