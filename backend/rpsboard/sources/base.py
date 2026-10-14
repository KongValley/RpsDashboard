"""数据源公共设施：异常类型、重试工具与 K 线口径判定。"""

from __future__ import annotations

import time
from collections.abc import Callable

import pandas as pd


class SourceError(RuntimeError):
    """数据源不可用（连接失败、接口全量失败等）。

    与「单只股票取不到数据」区分：后者是可跳过的软失败，不应抛这个异常。
    """


def tradestatus_from_calendar(
    df: pd.DataFrame, calendar: pd.Index | None = None
) -> pd.Series:
    """tradestatus：1 = 正常交易，0 = 该 bar 之前缺了交易日（停牌复牌当天）。

    df 需要 `symbol` / `date` 两列，且**按 (symbol, date) 升序**（两条调用路径都由
    SQL 的 `ORDER BY symbol, date` 与 eltdx 返回的升序保证）；顺序不对会直接报错，
    而不是悄悄算出差一个交易日错的旗标。
    calendar = 市场交易日（升序去重），缺省取 df 的日期集合。

    判定**必须按交易日而不是自然日**：长假（国庆 7 个自然日）与长期停牌在自然日口径下
    无法区分，会把全市场整天标成停牌 —— 实测 21 个交易日 / 112,379 行因此没有 RPS。

    该列**不参与 RPS 掩码**（复牌当天是真实成交日，见
    matrix.build_tradable_matrix），只作为数据质量标记与复牌日诊断使用。
    """
    cal = pd.Index(sorted(df["date"].unique())) if calendar is None else calendar
    pos = pd.Series(cal.get_indexer(df["date"]), index=df.index)
    if (pos < 0).any():
        raise ValueError("calendar 未覆盖 DataFrame 里的日期")
    gap = pos.groupby(df["symbol"].to_numpy()).diff()
    if (gap.dropna() < 1).any():
        raise ValueError("df 必须按 (symbol, date) 升序排列，且同一 symbol 的日期不能重复")
    return (gap.isna() | (gap <= 1)).astype(int)


def with_retry[T](
    fn: Callable[[], T],
    *,
    max_retries: int = 3,
    base_wait: float = 2.0,
    on_retry: Callable[[int, Exception], None] | None = None,
) -> T:
    """指数退避重试：2s → 4s → 8s。"""
    last: Exception | None = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 数据源异常类型不可控
            last = exc
            if attempt < max_retries - 1:
                wait = base_wait ** (attempt + 1)
                if on_retry:
                    on_retry(attempt + 1, exc)
                time.sleep(wait)
    assert last is not None
    raise last
