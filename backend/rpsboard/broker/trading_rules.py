"""A 股申报数量与价格规则（板块相关）。

**单一真源**：模拟撮合、手工下单、三个交易工具都用这一份，避免「工具算出来的
数量下单却被拒」这种口径分裂。

规则（现金股票、竞价交易）：

| 板块 | 买入最小 | 步长 | 卖出零股 |
|---|---|---|---|
| 主板 | 100 股 | 100 股 | 余额不足 100 股须一次卖完 |
| 创业板 | 100 股 | 100 股 | 同上 |
| 科创板 | 200 股 | 1 股 | 余额不足 200 股须一次卖完 |
| 北交所 | 100 股 | 1 股 | 余额不足 100 股须一次卖完 |

来源：[上交所交易规则（2026）第 6.7 条](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml)
（科创板限价申报不小于 200 股、不超过 10 万股）、
[深交所数量/价位解读](https://investor.szse.cn/institute/rules/t20230706_601604.html)
（主板与创业板申报为 100 股或其整数倍；余额不足 100 股一次性申报卖出）、
[北交所交易规则第 3.3.8 条](https://www.bse.cn/jygl_list/200028217.html)
（不低于 100 股，余额不足 100 股一次性申报卖出）。

**这是日线模型，不承诺复原历年盘中细则**（有效申报价格范围、盘中临停等）。
"""

from __future__ import annotations

#: 板块 → (买入最小股数, 买入步长, 卖出最小股数)。
#: 科创板的「步长 1」允许 201 股这类非整手申报；主板/创业板必须整手。
BOARDS: dict[str, dict] = {
    "main":    {"label": "主板",   "buy_min": 100, "buy_step": 100, "sell_min": 100},
    "chinext": {"label": "创业板", "buy_min": 100, "buy_step": 100, "sell_min": 100},
    "star":    {"label": "科创板", "buy_min": 200, "buy_step": 1,   "sell_min": 200},
    "bse":     {"label": "北交所", "buy_min": 100, "buy_step": 1,   "sell_min": 100},
}

#: 代码前缀 → 板块。前缀按交易所公开代码段枚举，未列出的前缀一律拒绝。
_PREFIXES: tuple[tuple[str, str], ...] = (
    ("688", "star"), ("689", "star"),
    ("300", "chinext"), ("301", "chinext"),
    ("43", "bse"), ("83", "bse"), ("87", "bse"), ("88", "bse"), ("92", "bse"),
    ("000", "main"), ("001", "main"), ("002", "main"), ("003", "main"),
    ("600", "main"), ("601", "main"), ("603", "main"), ("605", "main"),
)

#: 价格最小变动单位（元）。
PRICE_TICK = 0.01


class TradeRuleError(ValueError):
    """违反申报规则（调用方转成 422）。"""


def is_valid_code(symbol: str) -> bool:
    """6 位 ASCII 数字。"全角/带后缀（600000.SH）一律不算。"""
    return isinstance(symbol, str) and len(symbol) == 6 and symbol.isascii() \
        and symbol.isdigit()


def board_of(symbol: str) -> str:
    """代码 → 板块名。未知品种抛 `TradeRuleError`（**不回落**成通用 100 股）。"""
    if not is_valid_code(symbol):
        raise TradeRuleError(f"证券代码必须是 6 位数字，收到 {symbol!r}")
    for prefix, board in _PREFIXES:
        if symbol.startswith(prefix):
            return board
    raise TradeRuleError(f"不支持的证券代码段：{symbol}（本项目只处理现金股票）")


def quantity_rule(symbol: str) -> dict:
    """该代码的申报数量规则：`{board, label, buy_min, buy_step, sell_min}`。"""
    board = board_of(symbol)
    return {"board": board, **BOARDS[board]}


def board_rule(board: str) -> dict:
    """纯计算工具用：直接按板块取规则（不查证券表）。"""
    if board not in BOARDS:
        raise TradeRuleError(
            f"板块必须是 {'/'.join(BOARDS)} 之一，收到 {board!r}")
    return {"board": board, **BOARDS[board]}


def is_legal_buy_volume(symbol: str, volume: int) -> bool:
    rule = BOARDS[board_of(symbol)]
    return (volume > 0 and volume >= rule["buy_min"]
            and volume % rule["buy_step"] == 0)


def is_pure_sell_of_remainder(symbol: str, volume: int, held: int) -> bool:
    """卖出「不足最小单位」的余额（须一次全部卖完）。"""
    rule = BOARDS[board_of(symbol)]
    return held > 0 and 0 < volume <= held and volume < rule["sell_min"] and volume == held


def validate_order_quantity(symbol: str, side: str, volume: int,
                            held: int = 0) -> None:
    """校验申报数量（`held` = 该账户当前总持股，卖出时用来判零股全清）。

    买入：>= 最小、是步长整数倍。
    卖出：允许「整手 + 一次性零股尾数全清」，也就是说 —— 卖出量合法当且仅当
    它本身是合法买入量，或它是把剩余全部卖出且不足最小单位。
    """
    if not isinstance(volume, int) or isinstance(volume, bool):
        raise TradeRuleError(f"申报数量必须是整数，收到 {volume!r}")
    if volume <= 0:
        raise TradeRuleError(f"申报数量必须为正，收到 {volume}")
    rule = BOARDS[board_of(symbol)]
    if side == "buy":
        if not is_legal_buy_volume(symbol, volume):
            raise TradeRuleError(
                f"{rule['label']}买入数量必须 ≥ {rule['buy_min']} 且为 "
                f"{rule['buy_step']} 的整数倍，收到 {volume}")
        return
    if side != "sell":
        raise TradeRuleError(f"方向只能是 buy/sell，收到 {side!r}")
    if is_legal_buy_volume(symbol, volume):
        return
    if is_pure_sell_of_remainder(symbol, volume, held):
        return
    raise TradeRuleError(
        f"{rule['label']}卖出数量必须 ≥ {rule['sell_min']} 且为 {rule['buy_step']} "
        f"的整数倍，或一次卖完不足 {rule['sell_min']} 股的余额；收到 {volume}")


def max_legal_buy_volume(symbol: str, cap: int) -> int:
    """不超过 `cap` 的最大合法买入量（0 = cap 连最小单位都不够）。

    科创板/北交所步长 1：直接取 cap（但不得小于最小量）；主板/创业板向下取整手。
    调仓减量、仓位工具求最大可买量都走这里，避免各写一遍取整逻辑。
    """
    rule = BOARDS[board_of(symbol)]
    if cap < rule["buy_min"]:
        return 0
    return (cap // rule["buy_step"]) * rule["buy_step"]


def quantize_price(price: float) -> float:
    """价格对齐到 0.01 网格（四舍五入到分）。"""
    return round(float(price) + 0.0, 2)
