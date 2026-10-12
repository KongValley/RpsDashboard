"""数据源公共设施：异常类型与重试/退避工具。"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class SourceError(RuntimeError):
    """数据源不可用（连接失败、接口全量失败等）。

    与「单只股票取不到数据」区分：后者是可跳过的软失败，不应抛这个异常。
    """


def with_retry(
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
