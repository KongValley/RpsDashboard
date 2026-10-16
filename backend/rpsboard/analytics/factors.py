"""多因子库 + 等权合成（全部由**现有表**推导，不新增任何数据源）。

库里没有股本、没有财报（`security_master` 只有 symbol/name/exchange/ipo_date/is_st），
所以市值/PE/PB 这类基本面因子**不在本版范围内** —— 那需要新的数据源，另立计划。

两条口径必须分清，混用会得出相反结论：

- **全样本 IC/IR**（`evaluate`）：衡量因子本身的预测力，是研究口径，页面上只展示它。
- **滚动训练窗的 IC 符号**（`rolling_ic` + `composite`）：策略真正用的方向，
  训练窗整体落在 `t - hold` 之前（无未来函数），且**零幅度拟合** ——
  只用符号、不用大小，避免在 64 个调仓点上过拟合出一组漂亮的权重。

因子矩阵**不落库**（13 × 1285 × 5574 × 8B ≈ 750 MB），落库的只有评估结果（约 30 KB
的 JSON，见 db.factor_eval）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import numpy as np
import pandas as pd

from .. import config
from . import matrix
from .backtest import _forward_return, compute_buckets, get_frames, load_context

#: 因子名 → 一句话说明。页面与 CLI 直接渲染它，**不要在前端另写一份文案**。
FACTOR_LABELS: dict[str, str] = {
    "mom": "区间动量（与所选 period 同窗口）",
    "rev5": "5 日反转",
    "rev20": "20 日反转",
    "vol20": "20 日波动率",
    "amp20": "20 日平均振幅",
    "amt20": "20 日平均成交额",
    "amt_ratio": "成交额 20 日 / 60 日（量能变化）",
    "near_high250": "距 52 周高点（收盘 / 250 日最高价）",
    "bias20": "20 日均线乖离",
    "age": "上市交易日数",
    "ind_rps": "所属证监会行业的 RPS",
    "con_rps": "所属概念中最强的一个 RPS",
    "triple": "三升底确认日（事件标记）",
}

#: 滚动训练窗长度（约一年交易日）
DEFAULT_TRAIN_DAYS = 250
#: MAD → σ 的正态一致估计（1/0.6745）
MAD_SCALE = 1.4826
#: 去极值的截断倍数
WINSOR_SIGMA = 3.0
#: 训练窗内至少这么多有效 IC 才算得出符号
MIN_TRAIN_IC = 20
#: 单个交易日的 IC 至少这么多只票才可信（与 config.MIN_CROSS_SECTION 同理）
MIN_CROSS_IC = 100

#: 口径说明。**固定文案**：页面与 CLI 都直接渲染它，不要在两处各写一份。
NOTES: list[str] = [
    "IC 为**全样本**研究口径，衡量因子本身的预测力。",
    "策略净值的因子方向由**滚动训练窗**的 IC 符号决定（训练窗整体落在 t-hold 之前），"
    "两套数字不可混用为同一结论。",
    "横截面不足 100 只票的交易日不计入 IC；因子自相关越低，排名换得越快、成本越高。",
]


# ── 横截面 ──────────────────────────────────────────────────────────
def _nan_frame(close: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(np.nan, index=close.index, columns=close.columns)


def _pair_corr(a: pd.DataFrame, b: pd.DataFrame) -> pd.Series:
    """逐日横截面 Pearson（**只用两边同时有效的样本**）。

    单日样本数低于 `MIN_CROSS_IC` 的日期置 NaN：和 IC 一样，几十只票算出来的
    相关系数不是「这两个因子是不是同一个赌注」的证据。
    """
    mask = a.notna() & b.notna()
    n = mask.sum(axis=1).astype("float64")
    aa, bb = a.where(mask), b.where(mask)
    sx, sy = aa.sum(axis=1), bb.sum(axis=1)
    sxx = (aa * aa).sum(axis=1)
    syy = (bb * bb).sum(axis=1)
    sxy = (aa * bb).sum(axis=1)
    cov = sxy - sx * sy / n
    vx = sxx - sx * sx / n
    vy = syy - sy * sy / n
    with np.errstate(invalid="ignore", divide="ignore"):
        out = cov / np.sqrt(vx * vy)
    return out.where(n >= MIN_CROSS_IC)


def _rps_wide(conn: sqlite3.Connection, table: str, key: str, period: int) -> pd.DataFrame:
    """板块 RPS 宽表（index=date, columns=industry/concept）。表不存在 → 空表。"""
    try:
        df = pd.read_sql_query(
            f"SELECT date, {key} AS k, rps{int(period)} AS v FROM {table}", conn
        )
    except sqlite3.Error:
        return pd.DataFrame()
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    return df.pivot_table(index="date", columns="k", values="v", aggfunc="last").sort_index()


def industry_rps(conn: sqlite3.Connection, close: pd.DataFrame, period: int) -> pd.DataFrame:
    """所属证监会行业的 RPS，经 `industry_map` 广播到成分股。"""
    ind = matrix.load_industry_map(conn)
    wide = _rps_wide(conn, "industry_rps_daily", "industry", period)
    if wide.empty or ind.empty:
        return _nan_frame(close)
    out = wide.reindex(columns=close.columns.map(ind))
    out.columns = close.columns
    return out.reindex(index=close.index)


def concept_rps(conn: sqlite3.Connection, close: pd.DataFrame, period: int) -> pd.DataFrame:
    """所属概念里最强的一个 RPS（一只票属多个概念 → 取最大值）。"""
    try:
        cm = pd.read_sql_query("SELECT symbol, concept FROM concept_map", conn)
    except sqlite3.Error:
        cm = pd.DataFrame(columns=["symbol", "concept"])
    wide = _rps_wide(conn, "concept_rps_daily", "concept", period)
    if cm.empty or wide.empty:
        return _nan_frame(close)
    sub = wide.reindex(index=close.index, columns=sorted(set(cm["concept"])))
    by_sym = cm.groupby("symbol")["concept"].agg(list)
    data: dict[str, pd.Series] = {}
    for sym, cs in by_sym.items():
        use = [c for c in cs if c in sub.columns]
        if use:
            data[sym] = sub[use].max(axis=1)
    # 分组结果的列顺序与 close 不一定相同：不对齐就是静默错位，必须 reindex
    return pd.DataFrame(data).reindex(index=close.index, columns=close.columns)


def pattern_marker(conn: sqlite3.Connection, close: pd.DataFrame) -> pd.DataFrame:
    """三升底**确认日当天** = 1，其余 = 0（事件标记，不是「处于形态中」的状态）。"""
    try:
        df = pd.read_sql_query("SELECT date, symbol FROM pattern_daily", conn)
    except sqlite3.Error:
        df = pd.DataFrame(columns=["date", "symbol"])
    arr = np.zeros(close.shape, dtype="float64")
    if not df.empty:
        di = close.index.get_indexer(pd.to_datetime(df["date"]))
        si = close.columns.get_indexer(df["symbol"])
        ok = (di >= 0) & (si >= 0)
        arr[di[ok], si[ok]] = 1.0
    return pd.DataFrame(arr, index=close.index, columns=close.columns)


# ── 因子清单 ────────────────────────────────────────────────────────
def compute_factors(
    ctx: dict, conn: sqlite3.Connection, *, period: int = config.DEFAULT_PERIOD
) -> Iterator[tuple[str, pd.DataFrame]]:
    """逐个 yield `(因子名, 原始值矩阵)`，共 13 个。

    **生成器而不是返回 dict**：单个 date × symbol 矩阵约 1285 × 5574 × 8B = 57 MB，
    13 个原始矩阵 + 13 个标准化矩阵同时驻留就是 1.5 GB，与 ctx（约 300 MB）和
    `_frames`（约 570 MB）叠加会直接打爆内存。生成器让峰值停在
    「1 份原始 + 累积中的标准化」。
    """
    missing = [k for k in ("high", "low", "amount") if k not in ctx]
    if missing:
        raise ValueError(f"ctx 缺少 {missing}：请用 load_context(..., with_ohlcv=True) 取矩阵")
    if period not in config.PERIODS:
        period = config.DEFAULT_PERIOD

    close = ctx["close"]
    high, low, amount = ctx["high"], ctx["low"], ctx["amount"]
    frames = get_frames(ctx, True)

    # 每个因子矩阵都对齐到 close 的 shape。**必须**：`build_pctchg_matrix` 走的
    # pivot_table 会把「整行全 NaN」的日期丢掉（实测 pct_chg 有 2 个日期全空 →
    # 1732 行 vs close 的 1734 行），行列不一致会让 composite 静默错位、
    # 让 compute_buckets 直接广播失败。
    index, columns = close.index, close.columns

    def aligned(x: pd.DataFrame) -> pd.DataFrame:
        return x.reindex(index=index, columns=columns)

    yield "mom", aligned(frames[f"ret{period}"])
    yield "rev5", aligned(close / close.shift(5) - 1.0)
    yield "rev20", aligned(close / close.shift(20) - 1.0)
    yield "vol20", aligned(ctx["pct"].rolling(20, min_periods=20).std())
    yield "amp20", aligned(((high - low) / close).rolling(20, min_periods=20).mean())
    yield "amt20", aligned(amount.rolling(20, min_periods=20).mean())
    yield "amt_ratio", aligned(
        amount.rolling(20, min_periods=20).mean()
        / amount.rolling(60, min_periods=60).mean()
    )
    yield "near_high250", aligned(close / high.rolling(250, min_periods=250).max())
    yield "bias20", aligned(close / close.rolling(20, min_periods=20).mean() - 1.0)
    # 上市交易日数：close 已 ffill，上市前为 NaN，逐行累计非空数就是「上市第几天」。
    # **不用 limits.first_bar_dates**：那要逐只股票查一次库（5,580 次查询）。
    yield "age", aligned((~close.isna()).cumsum())
    yield "ind_rps", aligned(industry_rps(conn, close, period))
    yield "con_rps", aligned(concept_rps(conn, close, period))
    yield "triple", aligned(pattern_marker(conn, close))


def standardize_factors(
    raw: Iterator[tuple[str, pd.DataFrame]], *, st_flags: pd.DataFrame | None = None
) -> dict[str, pd.DataFrame]:
    """逐日横截面标准化：**MAD 去极值 → z-score**（顺序固定）。

    边消费 `raw` 生成器边累积，因此不会同时持有全部原始矩阵。

    `mad == 0`（超过半数同值，例如 `triple` 某天一个形态都没有）时
    `lo == hi == med`，`clip` 把整行压成常数 → `sd == 0` → 该行全 NaN。
    **这是期望行为**：那天这个因子没有区分度，就不该参与合成。
    """
    out: dict[str, pd.DataFrame] = {}
    for name, x in raw:
        if x is None or x.empty:
            continue
        med = x.median(axis=1)
        mad = x.sub(med, axis=0).abs().median(axis=1) * MAD_SCALE
        x = x.clip(lower=med - WINSOR_SIGMA * mad, upper=med + WINSOR_SIGMA * mad, axis=0)
        mu, sd = x.mean(axis=1), x.std(axis=1)
        z = x.sub(mu, axis=0).div(sd.replace(0.0, np.nan), axis=0)
        if st_flags is not None and not st_flags.empty:
            z = z.where(
                ~st_flags.reindex(index=z.index, columns=z.columns).fillna(False).astype(bool)
            )
        out[name] = z
    return out


# ── 评估台（全样本研究口径）─────────────────────────────────────────
def ic_series(z: pd.DataFrame, fwd: pd.DataFrame) -> pd.Series:
    """逐日 Spearman IC（因子值与前瞻收益的横截面秩相关）。

    库里没有 scipy，用「先秩变换再 Pearson」等价实现 Spearman。
    """
    if z is None or z.empty or fwd is None or fwd.empty:
        return pd.Series(dtype="float64")
    rf = fwd.reindex_like(z).rank(axis=1)
    rz = z.rank(axis=1)
    ic = rz.corrwith(rf, axis=1)
    n = (rz.notna() & rf.notna()).sum(axis=1)
    return ic.where(n >= MIN_CROSS_IC)


def _r4(v) -> float | None:
    """四舍五入到 4 位；NaN/None → None（JSON 里 NaN 不是合法值）。"""
    if v is None or not np.isfinite(v):
        return None
    return round(float(v), 4)


def _mean_xs(s: pd.Series) -> float | None:
    """只对有效日期取均值；整段无效 → None。"""
    return _r4(s.mean()) if s.notna().any() else None


def evaluate(z: dict[str, pd.DataFrame], fwd: pd.DataFrame) -> list[dict]:
    """每个因子的全样本统计：IC/IR、胜率、自相关、5 档分层收益。

    `autocorr` 是**换手代理**：自相关越低，因子的排名换得越快，交易成本越高。
    """
    out: list[dict] = []
    for name, zk in z.items():
        ic = ic_series(zk, fwd)
        mean, std = ic.mean(), ic.std()
        out.append({
            "name": name,
            "label": FACTOR_LABELS.get(name, name),
            "ic_mean": _r4(mean) if ic.notna().any() else None,
            "ic_std": _r4(std) if ic.notna().any() else None,
            "ic_ir": _r4(mean / std) if ic.notna().any() and std else None,
            "ic_win": _r4((ic > 0).sum() / ic.notna().sum()) if ic.notna().any() else None,
            "samples": int(ic.notna().sum()),
            "autocorr": _mean_xs(_pair_corr(zk, zk.shift(1))),
            "buckets": compute_buckets(to_percentile(zk), fwd),
        })
    # 按 |IC 均值| 降序（读起来先看到「最像信号」的那几个）；没有 IC 的排最后
    out.sort(key=lambda f: -(abs(f["ic_mean"]) if f["ic_mean"] is not None else -1.0))
    return out


def factor_corr(z: dict[str, pd.DataFrame]) -> dict:
    """时间平均的横截面相关矩阵（用于看「这几个因子是不是同一个赌注」）。"""
    names = list(z)
    n = len(names)
    m = np.full((n, n), np.nan, dtype="float64")
    for i in range(n):
        m[i, i] = 1.0
        for j in range(i + 1, n):
            s = _pair_corr(z[names[i]], z[names[j]])
            v = s.mean() if s.notna().any() else np.nan
            m[i, j] = m[j, i] = v
    return {
        "names": names,
        "matrix": [[_r4(v) for v in row] for row in m],
    }


def rolling_ic(
    ic: pd.Series,
    *,
    hold: int,
    train_days: int = DEFAULT_TRAIN_DAYS,
    min_periods: int = MIN_TRAIN_IC,
) -> pd.Series:
    """t 日的训练窗 = `(t - hold - train_days, t - hold]`。

    **必须整体落在 `t - hold` 之前**：IC 在日期 s 需要 `fwd(s) = close(s+hold)/close(s)-1`，
    只有 `s + hold <= t` 时才不构成未来函数。少这一个 shift，训练窗就偷看了未来
    —— 这种错误在净值曲线上表现为「好得不真实」，很难靠肉眼发现
    （test_factors.py::test_no_lookahead_in_composite 用扰动法钉死它）。

    `hold` **必须与回测的 hold 相同**，否则训练窗的边界对不上实际持仓期。
    """
    if ic.empty:
        return ic
    return ic.shift(int(hold)).rolling(int(train_days), min_periods=int(min_periods)).mean()


def composite(
    z: dict[str, pd.DataFrame], ic_roll: dict[str, pd.Series]
) -> pd.DataFrame:
    """等权合成：方向 = `np.sign(滚动 IC)`，缺失因子按**可用因子数**归一。

    不能把缺失当 0：某只票只有 5/13 个因子有值时，当成 0 等于强行断言
    「缺失的 8 个因子等于市场平均」，会把新股与停牌股推向分数中位。
    """
    num: pd.DataFrame | None = None
    den: pd.DataFrame | None = None
    for k, zk in z.items():
        sign = ic_roll.get(k, pd.Series(dtype="float64")).reindex(zk.index)
        # np.sign(NaN) = NaN：训练窗样本不足的日期 → 整列 NaN → 不参与合成
        contrib = zk.mul(np.sign(sign), axis=0)
        filled = contrib.fillna(0.0)
        num = filled if num is None else num + filled
        cnt = contrib.notna().astype("float64")
        den = cnt if den is None else den + cnt
    if num is None or den is None:
        return pd.DataFrame()
    return num / den.replace(0.0, np.nan)


def to_percentile(score: pd.DataFrame) -> pd.DataFrame:
    """横截面百分位 0-100（复用 `compute_buckets` 的固定档位，可与 RPS 分档并排看）。"""
    return score.rank(axis=1, pct=True) * 100.0


def composite_score(
    ctx: dict,
    conn: sqlite3.Connection,
    *,
    period: int = config.DEFAULT_PERIOD,
    hold: int = 20,
    exclude_st: bool = True,
) -> pd.DataFrame:
    """合成分的横截面百分位（0-100）。策略侧**唯一**的合成入口。

    `hold` 必须与策略实际持有的交易日数一致 —— 训练窗的右端就是 `t - hold`。

    13 个标准化矩阵约 750 MB，用完立刻释放：调用方拿到的是百分位矩阵，
    与原始因子值无关。
    """
    fwd = _forward_return(ctx["close"], hold)
    z = standardize_factors(
        compute_factors(ctx, conn, period=period),
        st_flags=ctx["st_flags"] if exclude_st else None,
    )
    ic_roll = {k: rolling_ic(ic_series(zk, fwd), hold=hold) for k, zk in z.items()}
    return to_percentile(composite(z, ic_roll))


def evaluate_all(
    conn: sqlite3.Connection,
    *,
    period: int = config.DEFAULT_PERIOD,
    hold: int = 20,
) -> dict:
    """把因子库串成一份可直接 JSON 序列化的 payload（`cli factors` 与页面共用）。

    **全样本口径**：`evaluate` 用全部有效日期的 IC，衡量因子本身的预测力，
    不涉及滚动训练窗（那是策略侧 `composite_score` 的事）。曾经这里收一个
    `train_days` 参数并原样写进 `params`，但评估完全没用它 —— 传 30 或 250
    给出的数字逐项相同，页面却按它显示「训练窗」，等于误导。
    """
    if period not in config.PERIODS:
        period = config.DEFAULT_PERIOD
    ctx = load_context(conn, None, with_ohlcv=True)
    fwd = _forward_return(ctx["close"], hold)
    raw = compute_factors(ctx, conn, period=period)
    z = standardize_factors(raw, st_flags=ctx["st_flags"])
    return {
        "factors": evaluate(z, fwd),
        "corr": factor_corr(z),
        "params": {"period": int(period), "hold": int(hold)},
        "notes": list(NOTES),
    }
