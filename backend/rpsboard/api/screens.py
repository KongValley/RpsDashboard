"""横截面筛选条件（排行榜的"我的看板"）的保存与复用。

排行榜的筛选条件此前只能存在 URL 里：换个页面回来就得重新点一遍。
这里存成具名条目，页面下拉即可套用。

**参数按白名单落库**：只接受排行榜请求里那几个筛选项，且逐项夹取到合法区间 ——
存进去的东西将来要直接拼进查询参数，不能让任意键值透传（写坏一条，
页面每次套用都会报错，而且很难看出是谁写坏的）。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from .. import config, db

router = APIRouter(prefix="/api", tags=["screens"])

#: 允许保存的参数（与 `api/rps.py::ranking` 的查询参数同名）。
#: 分页（page/limit）刻意不在内：看板存的是"筛选口径"，不是"第几页"。
INT_KEYS = ("period", "delta_days", "sanxingdi")
FLOAT_KEYS = ("rps_min", "rps_max", "min_amount")
STR_KEYS = ("industries", "sort", "direction")
BOOL_KEYS = ("exclude_st",)

#: 排序键白名单（与 api/rps.py 的 _SORTS 同源；写死一份是为了不让页面传进任意列名）
SORTS = ("rps", "ret", "pct_chg", "amount", "close")
DIRECTIONS = ("asc", "desc")


def _clamp(value, lo, hi, default):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(v, hi))


def normalize_params(raw: dict) -> dict:
    """白名单 + 夹取。未知键直接拒绝（422），而不是静默丢掉。"""
    if not isinstance(raw, dict):
        raise ValueError("params 必须是对象")
    unknown = set(raw) - set(INT_KEYS + FLOAT_KEYS + STR_KEYS + BOOL_KEYS)
    if unknown:
        raise ValueError(f"不认识的参数：{sorted(unknown)}")

    out: dict = {}
    if "period" in raw:
        p = int(_clamp(raw["period"], 0, 10**6, config.DEFAULT_PERIOD))
        out["period"] = p if p in config.PERIODS else config.DEFAULT_PERIOD
    if "delta_days" in raw:
        out["delta_days"] = int(_clamp(raw["delta_days"], 0, 250, 0))
    if "sanxingdi" in raw:
        s = int(_clamp(raw["sanxingdi"], 0, 250, 0))
        out["sanxingdi"] = s if s in (0, 1, 5, 10, 20) else 0
    if "rps_min" in raw:
        out["rps_min"] = round(_clamp(raw["rps_min"], 0, 100, 0.0), 2)
    if "rps_max" in raw:
        out["rps_max"] = round(_clamp(raw["rps_max"], 0, 100, 100.0), 2)
    if "min_amount" in raw:
        out["min_amount"] = round(max(0.0, _clamp(raw["min_amount"], 0, 1e13, 0.0)), 2)
    if "industries" in raw:
        # 行业名会展开成 SQL 的 IN (?,?,…)：与 api/rps.py 的 MAX_INDUSTRY_FILTER 同一上限
        names = [str(x).strip() for x in str(raw["industries"]).split(",") if str(x).strip()]
        out["industries"] = ",".join(names[:200])
    if "sort" in raw:
        s = str(raw["sort"])
        out["sort"] = s if s in SORTS else "rps"
    if "direction" in raw:
        d = str(raw["direction"]).lower()
        out["direction"] = d if d in DIRECTIONS else "desc"
    if "exclude_st" in raw:
        out["exclude_st"] = bool(raw["exclude_st"])
    return out


class ScreenIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    params: dict

    @field_validator("params")
    @classmethod
    def _check(cls, v: dict) -> dict:
        return normalize_params(v)


@router.get("/screens")
def list_screens() -> dict:
    """全部已保存的筛选条件（新的在前）。"""
    conn = db.connect()
    try:
        if not db.table_exists(conn, "saved_screen"):
            return {"rows": []}
        rows = []
        for r in conn.execute(
                "SELECT id, name, params, created_at FROM saved_screen"
                " ORDER BY id DESC"):
            d = dict(r)
            try:
                d["params"] = json.loads(d["params"])
            except (TypeError, ValueError):
                d["params"] = {}
            rows.append(d)
        return {"rows": rows}
    finally:
        conn.close()


@router.post("/screens")
def save_screen(item: ScreenIn) -> dict:
    """保存（或同名覆盖）一条筛选条件。**写库。**"""
    name = item.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="名称不能为空")
    with db.session() as conn:
        conn.execute(
            "INSERT INTO saved_screen(name, params, created_at) VALUES (?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET params=excluded.params,"
            " created_at=excluded.created_at",
            (name, json.dumps(item.params, ensure_ascii=False), db.now()))
        conn.commit()
        row = conn.execute("SELECT id FROM saved_screen WHERE name = ?",
                           (name,)).fetchone()
    return {"ok": True, "id": int(row["id"]), "name": name}


@router.delete("/screens/{screen_id}")
def delete_screen(screen_id: int) -> dict:
    with db.session() as conn:
        cur = conn.execute("DELETE FROM saved_screen WHERE id = ?", (int(screen_id),))
        conn.commit()
    return {"ok": bool(cur.rowcount), "id": int(screen_id)}
