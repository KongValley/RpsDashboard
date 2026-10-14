"""复权因子异常检测。

**要解决的问题**：后复权价 = 原始价 × scale + offset，`offset` 可以为**负数**。
当股票的原始价接近「变换零点」（`-offset / scale`）时，后复权价会被压成极小值甚至
穿零，而**百分比收益在接近零的价位上会被放大到失真**：

- 实测 6 只股票的库内后复权价 < 1 元，其中 23 行 `close <= 0`（RPS 已按无效置空）；
- 600076 在 2024-12-18 的 `ret60 = +998%`（窗口起点价 0.46）就是这么来的：
  同样的原始价涨幅（3.7 → 4.0，+8%）在这个价位上被放大成 +189%。

**三类可验证的检测线索**（都写进 `adjust_event`，都只报告、不改口径）：

| source   | kind               | 判据（全部可复算） |
|----------|--------------------|--------------------|
| internal | `hfq_jump`         | 库内单日后复权收益超出任何板块的涨跌停上限（主板 10%/创业板·科创板 20%/北交所 30%），且不是上市前 5 个交易日、不是复牌日 |
| internal | `tiny_price`       | 库内 `0 < close < 1` 的行（正数极小，无法用「非正」判据剔除，跨该点的区间收益不可信） |
| internal | `nonpositive_price`| 库内 `close <= 0` 的行（RPS 已经置空，留档以说明影响面） |
| official | `factor_jump`      | 官方复权因子相邻档位 `hfq_scale` 变化超阈值（除权/送转，跨该日的区间收益含送转成分） |
| official | `negative_offset`  | 官方因子中**在本库数据区间内生效**且负偏移的档位，**只对库内价格确实被压到极小的股票记录**（否则 789 只里绝大多数根本没受影响） |

**自动剔除的边界**（写死在这里，避免口径漂移）：

- **自动**：区间收益的**端点价**低于 `config.ANOMALY_MIN_PRICE`（默认 1 元，含 ≤0）
  时，`close(t)/close(t−N)` 直接不算 —— 见 `matrix.build_close_matrix`。
  比值只取决于两个端点，所以「端点落在被放大的价位」才是真正的失真条件；
  实测 600076 在 2024-12-18 的 `ret60` 因此在重算后变为 NULL。
- **不自动**：端点价都正常、只是窗口「跨过」了某个负偏移档位或因子跳变日 ——
  这类窗口里的收益是**真实价格**与**送转成分**的混合，按什么比例裁剪是判断而不是事实，
  自动处理会静默改变已公布的回测结论。这类情况只报告（本模块 + `cli quality`）。

本模块负责把两类异常摆到台面上：`cli quality` 打印、结果进 `adjust_event`、
`cli status` 显示一行汇总、`cli compute-rps` 结束时提醒一句。
"""

from __future__ import annotations

import sqlite3

import pandas as pd

from .. import config
from .limits import BSE_PREFIXES, IPO_NO_LIMIT_DAYS

#: 因子跳变告警阈值：scale 相对变化超过该比例（如 10 送 10 = 100%）才记录
FACTOR_JUMP_THRESHOLD = 0.3

#: 库内单日跳变阈值（小数）：超过任何板块的涨跌停上限（北交所 30%）
HFQ_JUMP_THRESHOLD = 0.31

#: 「极小后复权价」阈值（元）：与 RPS / 回测的**端点剔除**共用同一个开关
#: （`config.ANOMALY_MIN_PRICE`，环境变量 RPSBOARD_ANOMALY_MIN_PRICE 可调）
TINY_PRICE = config.ANOMALY_MIN_PRICE

#: kind → 人话（CLI / README 共用）
KIND_LABELS = {
    "hfq_jump": "库内单日跳变超涨跌停上限（疑因子跳变/数据错误）",
    "tiny_price": "后复权价极小（<1 元），区间收益被放大",
    "nonpositive_price": "后复权价非正（已置空，留档）",
    "factor_jump": "复权因子跳变（除权/送转）",
    "negative_offset": "负偏移档位（本库区间生效且价格被压低）",
    "no_limit_day": "无涨跌幅限制日（上市前 5 日/复牌日）",
}

#: 官方因子接口一次最多 75 只
FACTOR_BATCH = 75

EVENT_COLS = ["symbol", "date", "kind", "ratio", "detail"]


def full_code(symbol: str) -> str:
    """6 位代码 → eltdx 的带市场前缀代码（sh/sz/bj）。

    北交所必须先判：920xxx 是以 9 开头的**北交所**新代码段，不是沪市 B 股（900xxx）。
    """
    if symbol[:2] in BSE_PREFIXES or symbol.startswith(("4", "8")):
        return f"bj{symbol}"
    if symbol.startswith(("5", "6", "9")):
        return f"sh{symbol}"
    return f"sz{symbol}"


# ── 离线：库内单日跳变 ──────────────────────────────────────────────
def scan_hfq_jumps(
    conn: sqlite3.Connection, threshold: float = HFQ_JUMP_THRESHOLD
) -> pd.DataFrame:
    """扫描库内单日后复权收益超阈值的行，并分类。

    - 该股最早的 `IPO_NO_LIMIT_DAYS` 根 bar → `no_limit_day`（上市初期无涨跌幅限制）
    - `tradestatus = 0`（复牌当天）→ `no_limit_day`（重大事项复牌首日无限制）
    - 其余 → `hfq_jump`：单日涨跌幅超过任何板块的上限，只可能是因子跳变或数据错误
    """
    sql = """
    WITH bars AS (
      SELECT symbol, date, close, tradestatus,
             ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date) AS rn,
             LAG(close) OVER (PARTITION BY symbol ORDER BY date) AS prev
      FROM daily_bar)
    SELECT symbol, date, prev, close, tradestatus, rn,
           (close / prev - 1.0) AS ret
    FROM bars
    WHERE prev > 0 AND close > 0 AND ABS(close / prev - 1.0) > ?
    ORDER BY symbol, date
    """
    df = pd.read_sql_query(sql, conn, params=(threshold,))
    if df.empty:
        return pd.DataFrame(columns=EVENT_COLS)
    no_limit = (df["rn"] <= IPO_NO_LIMIT_DAYS) | (df["tradestatus"] == 0)
    out = pd.DataFrame({
        "symbol": df["symbol"],
        "date": df["date"],
        "kind": no_limit.map({True: "no_limit_day", False: "hfq_jump"}),
        "ratio": df["ret"].round(6),
        "detail": ("prev=" + df["prev"].round(4).astype(str)
                   + " close=" + df["close"].round(4).astype(str)),
    })
    return out.reset_index(drop=True)


# ── 离线：价格水平异常 ──────────────────────────────────────────────
def scan_price_levels(
    conn: sqlite3.Connection, tiny: float = TINY_PRICE
) -> pd.DataFrame:
    """库内后复权价极小 / 非正的 (symbol, date) 明细。

    判据与自动剔除**同界**（`close < tiny`，默认 1 元）：`0 < close < tiny` 是真正
    需要人工判断的那一类 —— 既不是脏数据（值算得出来），也不是正常价位（百分比收益
    被放大到失真）；`close <= 0` 已在 RPS 里置空，这里留档只为说明影响面。
    恰好等于门槛（如 1.00 元整）的行**保留**，所以不在这里报。
    """
    df = pd.read_sql_query(
        "SELECT symbol, date, close FROM daily_bar WHERE close < ? ORDER BY symbol, date",
        conn, params=(tiny,),
    )
    if df.empty:
        return pd.DataFrame(columns=EVENT_COLS)
    nonpos = df["close"] <= 0
    return pd.DataFrame({
        "symbol": df["symbol"],
        "date": df["date"],
        "kind": nonpos.map({True: "nonpositive_price", False: "tiny_price"}),
        "ratio": df["close"].round(6),
        "detail": "close=" + df["close"].round(6).astype(str),
    }).reset_index(drop=True)


# ── 联网：官方复权因子 ──────────────────────────────────────────────
def fetch_factor_events(
    symbols: list[str],
    *,
    threshold: float = FACTOR_JUMP_THRESHOLD,
    batch_size: int = FACTOR_BATCH,
    since: str | None = None,
    focus_symbols: set[str] | None = None,
    progress=None,
) -> pd.DataFrame:
    """联网拉取官方复权因子，返回 (symbol, date, kind, ratio, detail) 事件表。

    - `factor_jump`：该日 `hfq_scale` 相对上一档变化超过 threshold。
    - `negative_offset`：该档 `hfq_offset < 0`、**在本库数据区间（`since` 之后）生效**，
      且股票在 `focus_symbols` 里（＝库内价格确实被压到极小的那些）。
      不加后一个条件时会有 789 只命中，而实际受影响的只有个位数 —— 检测结果必须
      能对上库里的价格，否则只是噪声。

    实测：5574 只 / 75 批，本机约 6 秒。失败**不吞**：调用方负责报给用户。
    """
    from ..sources.tdxnet_src import TdxSource

    say = progress or (lambda _m: None)
    rows: list[dict] = []
    focus = focus_symbols or set()
    src = TdxSource()
    try:
        batch = max(1, int(batch_size))
        total = len(symbols)
        for i in range(0, total, batch):
            chunk = symbols[i:i + batch]
            say(f"factors:{i + len(chunk)}/{total}")
            resp = src._client.corporate.adjustment_factors(  # noqa: SLF001
                [full_code(s) for s in chunk], batch_size=batch,
            )
            for item in resp.responses:
                sym = str(item.code).zfill(6)[-6:]
                recs = [(str(f.date),
                         float(f.hfq_scale) if f.hfq_scale is not None else None,
                         float(f.hfq_offset) if f.hfq_offset is not None else None)
                        for f in list(item.items or [])]
                prev_scale: float | None = None
                for idx, (date, scale, offset) in enumerate(recs):
                    nxt = recs[idx + 1][0] if idx + 1 < len(recs) else None
                    if (sym in focus and offset is not None and offset < 0
                            and (nxt is None or since is None or nxt > since)):
                        zero_raw = (-offset / scale) if scale else None
                        rows.append({
                            "symbol": sym, "date": date, "kind": "negative_offset",
                            "ratio": offset,
                            "detail": (f"hfq_scale={scale}，生效至 {nxt or '最新'}"
                                       f"，变换零点≈原始价 {zero_raw:.2f} 元"
                                       if zero_raw is not None else
                                       f"hfq_scale={scale}，生效至 {nxt or '最新'}"),
                        })
                    if (prev_scale and scale and prev_scale > 0
                            and abs(scale / prev_scale - 1.0) > threshold):
                        rows.append({
                            "symbol": sym, "date": date, "kind": "factor_jump",
                            "ratio": round(scale / prev_scale - 1.0, 6),
                            "detail": f"{prev_scale}→{scale}",
                        })
                    prev_scale = scale
    finally:
        src.close()
    return pd.DataFrame(rows, columns=EVENT_COLS)


# ── 落库与汇总 ──────────────────────────────────────────────────────
def materialize(conn: sqlite3.Connection, events: pd.DataFrame, source: str) -> int:
    """把某个来源的事件写进 adjust_event（先删同源旧行，幂等）。"""
    if source not in ("official", "internal"):
        raise ValueError(f"未知来源: {source}")
    conn.execute("DELETE FROM adjust_event WHERE source = ?", (source,))
    if events is None or events.empty:
        conn.commit()
        return 0
    out = events.copy()
    out["source"] = source
    out["updated_at"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
    cols = ["symbol", "date", "kind", "ratio", "detail", "source", "updated_at"]
    out[cols].to_sql("adjust_event", conn, if_exists="append", index=False)
    conn.commit()
    return len(out)


def summary(conn: sqlite3.Connection) -> dict:
    """汇总：按 (source, kind) 计数 + 受影响股票数 + 库内两个价格档位的行数。"""
    out: dict = {"kinds": {}, "symbols": 0, "zero_close": 0, "tiny_close": 0}
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='adjust_event'"
    ).fetchone():
        return out
    for r in conn.execute(
        "SELECT source, kind, COUNT(*) AS n, COUNT(DISTINCT symbol) AS s "
        "FROM adjust_event GROUP BY source, kind ORDER BY n DESC"
    ):
        out["kinds"][f"{r['source']}/{r['kind']}"] = {"rows": r["n"], "symbols": r["s"]}
    out["symbols"] = conn.execute(
        "SELECT COUNT(DISTINCT symbol) AS n FROM adjust_event").fetchone()["n"]
    out["zero_close"] = conn.execute(
        "SELECT COUNT(*) AS n FROM daily_bar WHERE close <= 0").fetchone()["n"]
    out["tiny_close"] = conn.execute(
        "SELECT COUNT(*) AS n FROM daily_bar WHERE close > 0 AND close < ?",
        (TINY_PRICE,)).fetchone()["n"]
    return out


def tiny_price_symbols(conn: sqlite3.Connection, tiny: float = TINY_PRICE) -> list[str]:
    """库内 `close < tiny`（含穿零）的股票 —— 与自动剔除同界。"""
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM daily_bar WHERE close < ? ORDER BY symbol",
        (tiny,)).fetchall()
    return [r["symbol"] for r in rows]
