"""技术形态识别：三升底（Rising Three，又称「上升三部曲」）。

形态定义（酒田五法的 Rising Three），在**第 T 日**成立时，三根阳线是
T-2 / T-1 / T：

    1. T-2、T-1、T **连续三根阳线**（close > open）
    2. 收盘价**逐根抬高**：close(T) > close(T-1) > close(T-2)
    3. **第一根实体不超过第三根的一半**（第一根可以是小阳线甚至十字星，
       实体由小到大是形态的关键特征）
    4. **第三根收盘突破第一根最高价**（形态的确认条件）

**失效条件**：跌破第一根阳线的最低价即形态破坏。第一根的 low 会被存进
`base_low`，供「形态是否仍然有效」判断。

两个容易踩的坑（都已在本文件修掉）：

- `Series.shift()` 在 `(symbol, date)` 这种**非唯一索引**上是整表位移，
  会取到「上一只股票的上一根 K 线」。必须用 `groupby(symbol).shift()`。
  早期版本因此全市场命中 0 只。
- `pct_change` / `shift` 都会跨股票串行，判定前务必确认数据已按
  `symbol, date` 排序且分组正确。

判定口径共三档，本项目采用**标准档**（上面四条）。
若要放宽/收紧，只需改 `SANXINGDI_RULES` 里的阈值，不必改调用方。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

#: 第一根阳线实体 / 第三根阳线实体的上限比（越小越严格）
FIRST_BODY_RATIO = 0.5

#: 形态成立当日的收盘必须严格高于第一根最高价
REQUIRE_BREAKOUT = True


@dataclass(frozen=True)
class SanxingdiRules:
    """判定参数。默认即标准口径。"""

    first_body_ratio: float = FIRST_BODY_RATIO
    require_breakout: bool = REQUIRE_BREAKOUT
    #: 第一根实体极小时的容差（相对价格），避免「第一根是十字星」被浮点误差判否
    body_eps: float = 1e-9


DEFAULT_RULES = SanxingdiRules()


def compute_sanxingdi(
    ohlc: pd.DataFrame,
    rules: SanxingdiRules = DEFAULT_RULES,
) -> pd.DataFrame:
    """从日 K 长表算出所有成立的三升底。

    ohlc 需含 symbol / date / open / high / low / close，
    且已按 (symbol, date) 升序。

    返回列（只含成立的形态，稀疏表）：
        date        第三根阳线（形态确认日）
        symbol
        base_date   第一根阳线日期
        base_low    第一根最低价 —— 跌破即失效
        base_high   第一根最高价
        strength    三根合计涨幅（第三根收盘 / 第一根开盘 - 1）
        body_ratio  第一根实体 / 第三根实体
    """
    required = {"symbol", "date", "open", "high", "low", "close"}
    missing = required - set(ohlc.columns)
    if missing:
        raise ValueError(f"日K 缺少列: {sorted(missing)}")
    if ohlc.empty:
        return pd.DataFrame(
            columns=["date", "symbol", "base_date", "base_low",
                     "base_high", "strength", "body_ratio"]
        )

    d = ohlc.sort_values(["symbol", "date"], kind="mergesort").copy()
    d["date"] = pd.to_datetime(d["date"])
    g = d.groupby("symbol", sort=False)

    body = d["close"] - d["open"]
    # 三根分别是 T-2(k=2) / T-1(k=1) / T(k=0)
    c = {k: g["close"].shift(k) for k in (0, 1, 2)}
    h = {k: g["high"].shift(k) for k in (0, 1, 2)}
    l = {k: g["low"].shift(k) for k in (0, 1, 2)}
    b = {k: body.groupby(d["symbol"], sort=False).shift(k) for k in (0, 1, 2)}
    date_of = {k: g["date"].shift(k) for k in (0, 1, 2)}
    bull = {k: (b[k] > 0) for k in (0, 1, 2)}

    ok = bull[2] & bull[1] & bull[0]
    ok &= (c[0] > c[1]) & (c[1] > c[2])                       # 收盘逐根抬高
    ok &= (b[2].abs() <= b[0].abs() * rules.first_body_ratio + rules.body_eps)
    if rules.require_breakout:
        ok &= (c[0] > h[2])                                 # 突破第一根最高价

    out = d.loc[ok, ["date", "symbol"]].copy()
    out["base_date"] = date_of[2][ok].to_numpy()
    out["base_low"] = l[2][ok].to_numpy()
    out["base_high"] = h[2][ok].to_numpy()
    out["strength"] = (c[0][ok] / d["open"][ok].to_numpy() - 1.0)
    # 第一根实体 / 第三根实体。
    # ⚠ 必须向量化：用 `[b[2][ok].iloc[i] for i in range(n)]` 这种
    # 逐行 .iloc 在 20 万个形态上是分钟级的开销（Series.iloc 单点取值常数极大）。
    first = np.abs(b[2][ok].to_numpy())
    third = np.abs(b[0][ok].to_numpy())
    out["body_ratio"] = np.where(third > 0, first / np.where(third > 0, third, 1.0), 0.0)
    return out.reset_index(drop=True)


def materialize(
    conn,
    ohlc: pd.DataFrame,
    rules: SanxingdiRules = DEFAULT_RULES,
) -> int:
    """计算并写入 pattern_daily（整表替换）。返回形态条数。"""
    from .. import db

    df = compute_sanxingdi(ohlc, rules)
    with db.session() as c:
        c.execute("DELETE FROM pattern_daily")
        c.commit()
        if not df.empty:
            out = df.copy()
            out["date"] = out["date"].dt.strftime("%Y-%m-%d")
            out["base_date"] = pd.to_datetime(out["base_date"]).dt.strftime("%Y-%m-%d")
            out.to_sql("pattern_daily", c, if_exists="append", index=False)
            c.commit()
    return len(df)


def window_start(conn, date: str, n: int) -> str | None:
    """以 date 结尾、**含 date 自身**的最近 n 个交易日中的最早一个。

    n=1 返回 date 本身，n=5 返回「date 往前数第 5 个交易日」。
    必须按**交易日**回退而不是日历日：周末与长假会让两者越偏越远。

    ⚠ 早期版本用 `WHERE date < ?` + OFFSET n-1，把 n=1 算成了「前一个交易日」，
    于是"近 1 日"实际覆盖 2 天，命中数对不上（250 vs 86）。
    """
    if n <= 0:
        return date
    row = conn.execute(
        "SELECT date FROM (SELECT DISTINCT date FROM rps_daily WHERE date <= ? "
        "ORDER BY date DESC) LIMIT 1 OFFSET ?",
        (date, n - 1),
    ).fetchone()
    return row["date"] if row and row["date"] else None
