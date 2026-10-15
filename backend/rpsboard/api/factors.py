"""因子评估接口（只读 `factor_eval` 的最新一行）。

**不在这里现算因子**：一次评估要读完全部日 K（本机 3 分钟以上）并建 13 个
全市场矩阵（约 750 MB），请求线程必然超时 —— 与 `api/backtest.py` 把采集
放到后台线程是同一个理由。评估是离线研究步骤，由 `cli factors` 落库，
页面只读结果。
"""

from __future__ import annotations

import json

from fastapi import APIRouter

from .. import db
from ..analytics.factors import NOTES

router = APIRouter(prefix="/api/factors", tags=["factors"])


@router.get("")
def latest() -> dict:
    """最近一次 `cli factors` 的结果。空库/未跑过时返回**同构**的空结构。

    结构与有数据时一致（`factors: []`、`corr.names: []`），页面不需要分支；
    直接抛 404/500 会让「还没跑过评估」看起来像故障。
    """
    empty = {
        "run_at": None, "params": {},
        "factors": [], "corr": {"names": [], "matrix": []},
        "notes": list(NOTES),
    }
    conn = db.connect()
    try:
        if not db.table_exists(conn, "factor_eval"):
            return empty
        row = conn.execute(
            "SELECT run_at, params, payload FROM factor_eval ORDER BY run_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return empty
        out = json.loads(row["payload"])
        out["run_at"] = row["run_at"]
        return out
    finally:
        conn.close()
