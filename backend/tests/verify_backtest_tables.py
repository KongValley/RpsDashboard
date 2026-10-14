"""生成 README「回测结论」三张表的实测数字（一次性脚本，不进 CI）。

用法：
    .venv\\Scripts\\python.exe backend/tests/verify_backtest_tables.py

与 README 的对应关系：
- 零成本 / 含成本两表：period=60、阈值 80/90、hold=20、真实涨停过滤
- 大盘择时表：同样的参数，market_filter 取 ma200 / above_ma50 / trend_up
- 顺延持仓数：warnings 里的「卖出受阻」与 equity_curve 的 holdings 之和
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from rpsboard import db  # noqa: E402
from rpsboard.analytics import backtest as bt  # noqa: E402
from rpsboard.analytics.costs import default_cost_model  # noqa: E402


def main() -> int:
    conn = db.connect()
    zero = default_cost_model(False)
    cost = default_cost_model(True)

    def run(tag: str, **kw):
        t0 = time.time()
        params = {"period": 60, "threshold": 80, "hold": 20}
        params.update(kw)
        r = bt.run_backtest(conn, **params)
        m = r.metrics
        holdings = sum(c["holdings"] for c in r.equity_curve)
        deferred = sum(c.get("deferred", 0) for c in r.equity_curve)
        cash = sum(1 for c in r.equity_curve if c["holdings"] == 0)
        bench = r.benchmark_curve
        print(f"{tag}: pts={len(r.equity_curve)} cash={cash} holdings={holdings} "
              f"deferred={deferred} "
              f"total={m['total_return'] * 100:.2f}% ann={m['annualized'] * 100:.2f}% "
              f"mdd={m['max_drawdown'] * 100:.2f}% sharpe={m['sharpe']} "
              f"years={m['years']} "
              f"bench={(bench[-1]['equity'] - 1) * 100 if bench else float('nan'):.2f}% "
              f"| {time.time() - t0:.0f}s")
        if tag.endswith("cost=0"):
            print("   分档:", [(b["bucket"], b["samples"], b["avg_forward_return"],
                              b["win_rate"]) for b in r.buckets])
            print("   limit_up_filtered:", r.params.get("limit_up_filtered"),
                  "| cost:", r.params["cost"].get("assume_positions"))
        return r

    print("== 主表（零成本 / 阈值 80）")
    r80 = run("t80 cost=0", cost_model=zero)
    print("== 阈值 90")
    run("t90 cost=0", threshold=90, cost_model=zero)
    print("== 含成本")
    run("t80 cost=1", cost_model=cost)
    run("t80+ma50 cost=1", cost_model=cost, market_filter="above_ma50")
    run("t80+ma200 cost=1", cost_model=cost, market_filter="ma200")
    print("== 择时（零成本）")
    run("t80+ma200 cost=0", cost_model=zero, market_filter="ma200")
    run("t80+ma50 cost=0", cost_model=zero, market_filter="above_ma50")
    run("t80+trend cost=0", cost_model=zero, market_filter="trend_up")
    print("警告样例:", r80.warnings)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
