"""定位剩余 3 个失败测试的具体数值。"""
import sys
sys.path.insert(0, "backend")
sys.path.insert(0, ".")

import pandas as pd
from backend.tests.fixtures import make_industry_map_zh
from rpsboard.analytics import rps as rps_mod
from rpsboard.analytics.matrix import build_close_matrix

print("=== 1. test_industry_return_is_equal_weight_mean ===")
dates = pd.bdate_range("2024-01-01", periods=5)
closes = {
    "000001": [10, 11, 12, 13, 14],
    "000002": [20, 22, 24, 26, 28],
    "000003": [50, 45, 40, 35, 30],
}
rows = [{"symbol": s, "date": d, "close": c, "pct_chg": 0.0,
         "tradestatus": 1, "is_st": 0}
        for s, arr in closes.items() for d, c in zip(dates, arr)]
df = pd.DataFrame(rows)
close = build_close_matrix(df)
frames = rps_mod.compute_rps_frames(close, periods=(2,))
print("ret2 matrix:\n", (close / close.shift(2) - 1).round(4))
ind_map = make_industry_map_zh(list(closes), 3)
print("ind_map:\n", ind_map)
ind = rps_mod.compute_industry_rps(frames, ind_map, periods=(2,))
print("industry result:\n", ind)

print()
print("=== 2. curve 长度不一致 ===")
from backend.tests.fixtures import make_long
d2 = make_long(n_symbols=10, n_days=300, seed=5)
sym = d2["symbol"].iloc[0]
print("symbol:", sym)
print("daily_bar dates:", d2[d2.symbol == sym]["date"].nunique())
print("close matrix rows:", build_close_matrix(d2).shape)

print()
print("=== 3. Series.reset_index 列名 ===")
s = pd.Series({"000001": "半导体", "000002": "白酒"})
print("via rename_axis:", list(s.rename("industry").rename_axis("symbol").reset_index().columns))
print("via old way    :", list(s.rename("industry").reset_index().rename(columns={"index": "symbol"}).columns))
