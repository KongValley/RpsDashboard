"""合成行情夹具：全部离线，不触网。

生成可控的日 K 面板（date × symbol），并提供「扰动指定日期之后的行情」
的能力，用于验证无未来函数。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def make_long(
    n_symbols: int = 50,
    n_days: int = 300,
    start: str = "2024-01-01",
    seed: int = 42,
    listed_bars: dict[str, int] | None = None,
    tradestatus: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """生成几何布朗运动价格的长表。

    listed_bars: symbol → 实际有行情的天数（模拟新股上市晚）
    tradestatus: date × symbol 的停牌掩码，True=停牌
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start=start, periods=n_days)
    symbols = [f"{i:06d}" for i in range(n_symbols)]

    frames = []
    for sym in symbols:
        drift = rng.normal(0.0004, 0.0002)
        vol = rng.uniform(0.01, 0.03)
        steps = rng.normal(drift, vol, n_days)
        close = 20.0 * np.exp(np.cumsum(steps))
        # 注入横截面差异：让 RPS 排名有意义且稳定
        close = close * (1.0 + rng.uniform(-0.3, 0.3))
        nbars = (listed_bars or {}).get(sym, n_days)
        frame = pd.DataFrame({
            "symbol": sym,
            "date": dates[:nbars].strftime("%Y-%m-%d"),
            "close": close[:nbars].round(4),
            "pct_chg": pd.Series(close[:nbars]).pct_change().round(6).fillna(0).values,
            "tradestatus": 1,
            "is_st": 0,
        })
        frames.append(frame)

    df = pd.concat(frames, ignore_index=True)

    if tradestatus is not None:
        # tradestatus 的契约是 **date × symbol** 的矩阵，非空/True 处表示停牌。
        # 这里直接取出停牌 (date, symbol) 对，避免逐行 in 判定的 O(n²) 开销。
        susp = tradestatus.reindex(
            index=dates.strftime("%Y-%m-%d"), columns=symbols
        )
        flag = np.asarray(susp.notna())
        stop = set(zip(*[dates.strftime("%Y-%m-%d")[row] for row in np.nonzero(flag)[0]],
                       [symbols[c] for c in np.nonzero(flag)[1]]))
        df["tradestatus"] = [
            0 if (d, s) in stop else 1
            for d, s in zip(df["date"], df["symbol"])
        ]
    return df


def perturb_after(df: pd.DataFrame, cutoff: str, seed: int = 7, scale: float = 0.5) -> pd.DataFrame:
    """把 cutoff 之后的所有价格乘上随机扰动因子（返回新表）。

    用于无未来函数断言：信号日 T 的 RPS 不应因 T 之后行情变化而改变。
    """
    rng = np.random.default_rng(seed)
    out = df.copy()
    mask = out["date"] > cutoff
    factors = np.exp(rng.normal(0.0, scale, mask.sum()))
    out.loc[mask, "close"] = (out.loc[mask, "close"].values * factors).round(4)
    return out


def make_industry_map(symbols: list[str], n_industries: int = 5) -> pd.Series:
    """symbol → industry 的映射。"""
    return pd.Series(
        {s: f"IND{i % n_industries}" for i, s in enumerate(sorted(symbols))}
    )


def make_industry_map_zh(symbols: list[str], n_industries: int = 5) -> pd.Series:
    """带中文行业名的映射（贴近真实场景）。"""
    names = ["半导体", "白酒", "新能源", "银行", "医药"]
    return pd.Series(
        {s: names[i % n_industries] for i, s in enumerate(sorted(symbols))}
    )
