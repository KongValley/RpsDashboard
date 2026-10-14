"""测试全局约定。

合成数据的股票数只有几十只，而生产门槛 `config.MIN_CROSS_SECTION`（默认 1000）
会把**所有**日期判成「横截面太薄」，于是每个用到 RPS 的用例都会看到空表 ——
那不是被测逻辑坏了，是门槛在起作用。这里统一调低；要验证门槛本身的用例
（tests/test_rps.py::test_cross_section_gate_is_configurable）显式传参。
"""

from __future__ import annotations

import pytest
from rpsboard import config


@pytest.fixture(autouse=True)
def _small_cross_section(monkeypatch):
    """把横截面门槛降到 2（等价于「至少两只股票才比较」）。"""
    monkeypatch.setattr(config, "MIN_CROSS_SECTION", 2)
