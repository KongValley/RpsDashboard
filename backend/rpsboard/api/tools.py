"""交易工具接口：仓位预算、往返成本、持仓压力测试。

纯计算的两项**不查证券表、不读行情**（`symbol` 只用来判定板块与数量规则），
所以数据库还没初始化也能用；压力测试依赖某个账户的持仓快照。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .. import db
from ..analytics import tools as tools_mod

router = APIRouter(prefix="/api/tools", tags=["tools"])


class _Strict(BaseModel):
    # 未知字段直接 422：静默忽略会让用户以为参数生效了。
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PositionSizeIn(_Strict):
    symbol: str = Field(min_length=6, max_length=6)
    equity: float = Field(gt=0)
    cash: float = Field(ge=0)
    entry: float = Field(gt=0)
    stop: float = Field(gt=0)
    risk_pct: float = Field(default=0.01, gt=0, le=1)
    max_weight: float = Field(default=0.25, gt=0, le=1)
    trade_date: str


class RoundTripIn(_Strict):
    symbol: str = Field(min_length=6, max_length=6)
    volume: int = Field(gt=0)
    buy_price: float = Field(gt=0)
    sell_price: float = Field(gt=0)
    trade_date: str


class Shock(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False,
                              populate_by_name=True)
    symbol: str = Field(min_length=6, max_length=6)
    return_: float = Field(alias="return", ge=-1, le=10)


class StressIn(_Strict):
    account_id: int = Field(gt=0)
    date: str | None = None
    shocks: list[Shock] = Field(default_factory=list, max_length=500)


def _fail(exc: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


@router.post("/position-size")
def position_size(body: PositionSizeIn) -> dict:
    try:
        return tools_mod.position_size(
            symbol=body.symbol, equity=body.equity, cash=body.cash,
            entry=body.entry, stop=body.stop, risk_pct=body.risk_pct,
            max_weight=body.max_weight, trade_date=body.trade_date)
    except tools_mod.ToolError as exc:
        raise _fail(exc) from exc


@router.post("/round-trip")
def round_trip(body: RoundTripIn) -> dict:
    try:
        return tools_mod.round_trip(
            symbol=body.symbol, volume=body.volume, buy_price=body.buy_price,
            sell_price=body.sell_price, trade_date=body.trade_date)
    except tools_mod.ToolError as exc:
        raise _fail(exc) from exc


@router.post("/stress")
def stress(body: StressIn) -> dict:
    conn = db.connect()
    try:
        return tools_mod.stress_test(
            conn, account_id=body.account_id, date=body.date,
            shocks=[s.model_dump(by_alias=True) for s in body.shocks])
    except tools_mod.ToolStateError as exc:
        # 账户不存在/没有曲线属于**冲突**（资源状态问题），不是格式错
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except tools_mod.ToolError as exc:
        raise _fail(exc) from exc
    finally:
        conn.close()
