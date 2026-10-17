"""交易成本模型。

现状：本项目原来的回测是**零摩擦**的——`backtest.py` 全文检索
commission / stamp / slippage / fee / cost / 佣金 / 印花 / 冲击 全部 0 次。
一个每 20 个交易日全仓换手一次、跨 3 年共 45 次调仓的策略，
真实执行里至少要付掉百分之十几的往返摩擦，忽略它会系统性高估结果。

费率（A 股现行规则）：

| 项            | 费率      | 方向     |
|---------------|-----------|----------|
| 佣金          | 0.025%    | 买 + 卖  |
| 印花税        | 0.05%     | 仅卖     |
| 过户费        | 0.001%    | 买 + 卖  |
| 滑点          | 0.05%     | 买 + 卖  |

**印花税有历史变更**：2023-08-28 起由 0.1% 下调到 0.05%。
本项目的回测样本从 2023-07 开始，横跨这次调整，因此按日期分段计算，
而不是用一个常数糊弄过去。

注意：**分档前瞻收益 `compute_buckets` 不计成本**。那张图衡量的是
「RPS 这个信号本身有没有预测力」，混入成本只会把信号质量和执行摩擦
两件事搅在一起。页面与 README 都需要显式说明两者口径不同。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pandas as pd

#: 印花税下调生效日
STAMP_CUTOFF = date(2023, 8, 28)

#: 假设的组合名义金额（元）。最低佣金是否生效完全取决于「单笔金额 vs 5 元」，
#: 因此成本必须相对一个具体的本金来谈，默认按 100 万计。
DEFAULT_PORTFOLIO_NOTIONAL = 1_000_000.0


@dataclass(frozen=True)
class CostModel:
    """一次调仓的往返摩擦（以小数表示，如 0.002 表示 0.2%）。"""

    enabled: bool = True
    commission_rate: float = 0.00025      # 万分之 2.5
    commission_min: float = 5.0           # 单笔最低 5 元
    transfer_fee_rate: float = 0.00001    # 过户费，十万分之一
    slippage_rate: float = 0.0005         # 单边滑点
    #: 印花税：调整前 0.1%，调整后 0.05%，仅卖出收取
    stamp_before: float = 0.001
    stamp_after: float = 0.0005
    stamp_cutoff: date = field(default=STAMP_CUTOFF)

    # ── 单边 ──────────────────────────────────────────────────────
    def buy_cost(self, notional: float = 1.0) -> float:
        """买入一单位名义金额的成本（**研究口径**，浮点、不量化）。

        记账口径请用 `costs.fee_decimal`：它同一个公式，但一次量化到分。
        """
        return _raw_components(self, notional, "buy", None)

    def sell_cost(self, notional: float = 1.0, on: date | None = None) -> float:
        """卖出一单位名义金额的成本（研究口径）。印花税按 on 当日是否已下调分段。"""
        return _raw_components(self, notional, "sell", on)

    # ── 往返 ──────────────────────────────────────────────────────
    def buy_rate(self, n_positions: int, portfolio: float) -> float:
        """买入成本占组合名义金额的比例。

        佣金有「单笔最低 5 元」，而本项目是等权分散持有**上千只**股票：
        100 万本金摊到 1000 只，每笔才 1000 元，按 0.025% 算只有 0.25 元，
        但券商按 5 元起收 —— 于是**最低佣金才是真正生效的那一项**，
        单边佣金成本高达 0.5%，远高于比例费率。

        这不是模型 bug，是小资金分散持仓的真实摩擦，也正是零成本回测
        最容易掩盖的东西。因此这里按「每笔下单」逐笔计算，
        并把 n_positions / portfolio 暴露成参数。
        """
        if not self.enabled or n_positions <= 0 or portfolio <= 0:
            return 0.0
        per_order = portfolio / n_positions
        commission = n_positions * max(per_order * self.commission_rate,
                                       self.commission_min) / portfolio
        return commission + self.transfer_fee_rate + self.slippage_rate

    def sell_rate(self, n_positions: int, portfolio: float,
                  on: date | None = None) -> float:
        """卖出成本占组合名义金额的比例（多一项印花税）。"""
        if not self.enabled or n_positions <= 0 or portfolio <= 0:
            return 0.0
        per_order = portfolio / n_positions
        commission = n_positions * max(per_order * self.commission_rate,
                                       self.commission_min) / portfolio
        d = _as_date(on)
        stamp_rate = self.stamp_after if (d and d >= self.stamp_cutoff) else self.stamp_before
        return (commission + self.transfer_fee_rate + self.slippage_rate
                + stamp_rate)

    def round_trip_cost(
        self,
        on: date | pd.Timestamp | None = None,
        n_positions: int = 50,
        portfolio: float = DEFAULT_PORTFOLIO_NOTIONAL,
    ) -> float:
        """一次调仓（等权全仓换手）的总摩擦。"""
        if not self.enabled:
            return 0.0
        return (self.buy_rate(n_positions, portfolio)
                + self.sell_rate(n_positions, portfolio, on))

    def describe(self, n_positions: int = 50) -> dict:
        """用于页面/CLI 展示的口径说明。"""
        return {
            "enabled": self.enabled,
            "commission_rate": self.commission_rate,
            "commission_min": self.commission_min,
            "transfer_fee_rate": self.transfer_fee_rate,
            "slippage_rate": self.slippage_rate,
            "stamp_after": self.stamp_after,
            "stamp_before": self.stamp_before,
            "stamp_cutoff": self.stamp_cutoff.isoformat(),
            "portfolio_notional": DEFAULT_PORTFOLIO_NOTIONAL,
            "assume_positions": n_positions,
            "round_trip_after": round(
                self.round_trip_cost(date(2024, 1, 1), n_positions), 6),
            "round_trip_before": round(
                self.round_trip_cost(date(2023, 1, 1), n_positions), 6),
        }


def _as_date(x) -> date | None:
    if x is None:
        return None
    if isinstance(x, date) and not isinstance(x, pd.Timestamp):
        return x
    try:
        return pd.Timestamp(x).date()
    except Exception:  # noqa: BLE001
        return None


def _raw_components(model: CostModel, notional: float, side: str,
                    on: date | None) -> float:
    """未量化的单边成本（元）。`buy_cost`/`sell_cost` 与 `fee_decimal` 共用它，
    所以研究口径与记账口径**永远同源**，不会一个改了另一个没改。"""
    if not model.enabled or notional <= 0:
        return 0.0
    commission = max(notional * model.commission_rate, model.commission_min)
    total = commission + notional * model.transfer_fee_rate + notional * model.slippage_rate
    if side == "sell":
        d = _as_date(on)
        stamp_rate = (model.stamp_after if (d and d >= model.stamp_cutoff)
                      else model.stamp_before)
        total += notional * stamp_rate
    return total


def fee_decimal(model: CostModel, notional: Decimal, *, side: str,
                on: date | None = None) -> Decimal:
    """**记账用**的单边费用：一次算完后 `ROUND_HALF_UP` 到分。

    为什么不能沿用 `buy_cost`/`sell_cost` 的 float：它们是研究口径，逐项
    累加的浮点误差在跨日部分成交里会被反复放大约分位，而现金、成本余额、
    已实现盈亏都是要长期对账的数。这里把费率从**十进制字符串**转成 Decimal，
    佣金/过户费/滑点/印花税累加后**只量化一次** —— 先分别取整再相加会和
    单笔实际收费不一致。

    最低佣金（5 元）与按日分段的印花税都由同一个 `_raw_components` 决定。
    """
    if not model.enabled or notional <= 0:
        return Decimal("0.00")
    raw = _raw_components(model, float(notional), side, on)
    return Decimal(repr(raw)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def default_cost_model(enabled: bool = True) -> CostModel:
    return CostModel(enabled=enabled)

