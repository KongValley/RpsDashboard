"""RPS（Relative Price Strength，欧奈尔相对价格强度）计算引擎。

口径定义（与 Sequoia-X 实现、通达信扩展数据归一化一致）：

    ret_N(t) = close(t) / close(t - N) - 1
    rps_N(t) = ret_N 在 t 日全部有效股票中的百分位排名 × 100   # 榜首 = 100

关键语义：
- **无未来函数**：ret_N 只用到 ≤ t 的收盘价，由 shift(N) 结构保证。
  test_rps.py 用「扰动 t 之后的行情，t 日 RPS 必须逐值不变」来硬断言这一点。
- **新股自动排除**：上市不足 N 日的股票 shift(N) 为 NaN → RPS 为 NaN，
  等价于方法论要求的「剔除新股」。
- **停牌不参与排名**：tradestatus != 1 的日子掩码为 NaN，但价格用 ffill 补齐，
  这样停牌期间的区间收益仍然可算（价格确实没动）。
- **ST 默认剔除**：避免 ST 股因连续涨停霸榜，可通过 exclude_st 放开。
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from .. import config, db
from . import matrix


# ── 核心计算 ────────────────────────────────────────────────────────
def compute_rps_frames(
    close_wide: pd.DataFrame,
    tradable: pd.DataFrame | None = None,
    st_flags: pd.DataFrame | None = None,
    exclude_st: bool = True,
    periods: tuple[int, ...] = config.PERIODS,
) -> dict[str, pd.DataFrame]:
    """从宽矩阵算出各周期的 ret / rps 矩阵。

    返回 {"ret20": DataFrame, "rps20": DataFrame, ...}，index=date，columns=symbol。
    """
    if close_wide.empty:
        return {}

    # 有效掩码：停牌日、ST 日标记为无效
    valid = pd.DataFrame(True, index=close_wide.index, columns=close_wide.columns)
    if tradable is not None and not tradable.empty:
        valid &= tradable.reindex_like(close_wide).fillna(False).astype(bool)
    if exclude_st and st_flags is not None and not st_flags.empty:
        valid &= ~st_flags.reindex_like(close_wide).fillna(False).astype(bool)

    out: dict[str, pd.DataFrame] = {}
    for n in periods:
        prev = close_wide.shift(n)
        ret = close_wide / prev - 1.0
        # 只在有效日参与排名
        ret = ret.where(valid & prev.notna())
        out[f"ret{n}"] = ret
        out[f"rps{n}"] = (ret.rank(axis=1, pct=True) * 100.0).round(2)
    return out


def compute_industry_rps(
    frames: dict[str, pd.DataFrame],
    industries: pd.Series,
    leader_rps: pd.DataFrame | None = None,
    names: pd.Series | None = None,
    periods: tuple[int, ...] = config.PERIODS,
) -> pd.DataFrame:
    """板块 RPS：成分股**等权平均**区间涨幅 → 在全部行业间做百分位排名。

    返回长表，每行是一个 (date, industry)，各周期各占一列：
    date, industry, rps{n}, ret{n}, member_count, leader_symbol, leader_name。

    实现说明（2026-10-12 性能改造）：
    早期实现是 `for n in periods: for date in ret.index:` 的双重 Python 循环，
    实测 4 个周期 × 887 个交易日 = 3548 次 `ret.loc[date]` 标签查找 + 3548 次
    `groupby`，在全市场规模下**单这一项就占 38.9 秒**（compute-rps 全程 132 秒）。

    现在改为**沿列轴一次性向量化**：
    - `ret.groupby(ind_of_column, axis=1).mean() / .count()` 每个周期各一次，
      直接得到 date × industry 的均值与成分数；
    - 用 `stack(dropna=True)` 把「存在该组的日期」还原成长表（等价于旧代码
      「dropna 后才建记录」的语义）；
    - 领涨股用 `groupby(industry).idxmax(axis=0)`，一次拿到 industry × date 的
      argmax，不再逐日构造 DataFrame。

    输出与旧实现逐值一致（含 member_count 的覆盖语义，见下）。
    """
    if industries is None or industries.empty or not frames:
        return pd.DataFrame()

    ind_map = industries.to_dict()

    # ── 1. 每个周期一次列轴聚合 ────────────────────────────────────
    means: dict[int, pd.DataFrame] = {}
    counts: dict[int, pd.DataFrame] = {}
    for n in periods:
        ret = frames.get(f"ret{n}")
        if ret is None or ret.empty:
            continue
        # 未映射到行业的 symbol 得到 NaN，groupby 默认 dropna，天然被排除。
        # pandas 3.0 移除了 DataFrame.groupby(axis=...)，统一转置后按行（symbol）分组。
        # ret 的 dtype 全是 float64，转置是廉价操作。
        ind_of_row = pd.Series(ret.columns, index=ret.columns).map(ind_map)
        g = ret.T.groupby(ind_of_row, sort=True)
        means[n] = g.mean().T      # date × industry
        counts[n] = g.count().T    # date × industry

    if not means:
        return pd.DataFrame()

    # ── 2. 摊平成 (date, industry) 长表 ───────────────────────────
    parts: list[pd.Series] = []
    cparts: list[pd.Series] = []
    for n, m in means.items():
        # pandas 3.0 的 stack 不会引入 NA 行，因此 `.stack()` 天然等价于旧实现的
        # 「dropna 后才建记录」——不存在均值的位置直接不进结果。
        m = m.rename_axis(index="date", columns="industry")
        parts.append(m.stack().rename(f"ret{n}"))
        # 让成分数与「均值是否存在」对齐：全 NaN 的组 count 为 0，
        # 旧实现里这种组根本不会被建记录，必须一并丢掉。
        c = counts[n].where(m.notna()).rename_axis(index="date", columns="industry")
        cparts.append(c.stack().rename(f"cnt{n}"))

    long = pd.concat(parts, axis=1)
    # member_count 沿用旧实现的**覆盖**语义：多个周期都命中同一 (date, industry) 时，
    # 取最后一个命中周期的成分数（通常是周期最长的 250，样本最少）。
    cnt_long = pd.concat(cparts, axis=1)
    long["member_count"] = cnt_long.ffill(axis=1).iloc[:, -1]
    # 只保留「至少一个周期有有效成分股」的组 —— 等价于旧实现里
    # `row.dropna()` 为空就不建记录、以及 stack 后全 NaN 组被丢弃的行为。
    long = long.dropna(subset=[f"ret{n}" for n in means], how="all").reset_index()
    long["member_count"] = long["member_count"].astype("int64")

    # ── 3. 每个日期内的横截面排名 ─────────────────────────────────
    for n in means:
        long[f"rps{n}"] = (long.groupby("date")[f"ret{n}"].rank(pct=True) * 100.0).round(2)

    long = long.sort_values(["date", "industry"]).reset_index(drop=True)
    long["date"] = pd.to_datetime(long["date"]).dt.strftime("%Y-%m-%d")

    # ── 4. 领涨股：各行业内 leader_rps 最高的成分股 ───────────────
    # 注意 leader_rps 传入的是「date × symbol」矩阵，与 compute_rps_frames 一致，
    # 因此先转置成「symbol × date」才能沿行按行业分组。
    if leader_rps is not None and not leader_rps.empty:
        lr = leader_rps.T.dropna(how="all")
        if lr.empty:
            lead = pd.DataFrame(columns=["industry", "date", "leader_symbol"])
        else:
            ind_series = pd.Series(lr.index.map(ind_map), index=lr.index)
            # pandas 3.0 的 groupby.idxmax 默认 skipna=False，且遇到「组内整列皆 NA」
            # 会直接抛 ValueError。先把 NaN 填成 -inf（真实取值都是 0~100 的 RPS，
            # 不可能是 -inf），argmax 结果再用 max 值的有限性筛掉全 NA 组。
            g = lr.fillna(-np.inf).groupby(ind_series, sort=True)
            best = g.idxmax()      # index=industry, columns=date，值为 symbol
            gmax = g.max()         # 同形状，-inf 表示该组当日全无有效值
            n_ind, n_date = best.shape
            ok = np.isfinite(gmax.to_numpy().ravel())
            # ravel 是行主序 (industry, date)：行业用 repeat、日期用 tile。
            # 写成 columns.repeat(n_ind) 会把第 i 行整体配到日期 columns[i]，
            # 导致 (date, industry) 错位、领涨股挂到别的日期上去。
            lead = pd.DataFrame({
                "industry": np.repeat(best.index.to_numpy(), n_date)[ok],
                "date": np.tile(best.columns.to_numpy(), n_ind)[ok],
                "leader_symbol": best.to_numpy().ravel()[ok],
            })
            # lead 的 date 来自矩阵列（Timestamp），long 的 date 已转成字符串——
            # 必须对齐类型，否则 merge 会静默匹配不到任何行。
            lead["date"] = pd.to_datetime(lead["date"]).dt.strftime("%Y-%m-%d")

        long = long.merge(lead, on=["date", "industry"], how="left")
        if names is not None and not names.empty:
            long["leader_name"] = long["leader_symbol"].map(names.to_dict())

    cols_order = ["date", "industry"]
    for n in periods:
        cols_order += [f"rps{n}", f"ret{n}"]
    cols_order += ["member_count", "leader_symbol", "leader_name"]
    return long[[c for c in cols_order if c in long.columns]]


def materialize_group_rps(
    frames: dict[str, pd.DataFrame],
    mapping: pd.Series,
    leader_rps: pd.DataFrame | None = None,
    names: pd.Series | None = None,
    periods: tuple[int, ...] = config.PERIODS,
) -> pd.DataFrame:
    """按任意分组（行业 / 概念）计算板块 RPS —— 与 compute_industry_rps 同构。

    概念板块 RPS 之所以能复用同一份实现，是因为它和行业 RPS 的算法完全一样，
    只是「symbol → 分组」的映射表不同。上一轮把该函数向量化之后，
    新增一种分组维度几乎零成本。
    """
    return compute_industry_rps(
        frames, mapping, leader_rps=leader_rps, names=names, periods=periods
    )


def load_concept_map(conn) -> pd.Series:
    """symbol → concept 的多对多映射展开成 symbol → 主分组。

    一只股票属于多个概念时，取**第一个**作为它的主分组，
    保证每个 symbol 只属于一个组——否则同一只票的涨幅会被重复计入多个概念，
    概念 RPS 就失去可比性（成分数也会虚高）。
    """
    try:
        df = pd.read_sql_query(
            "SELECT symbol, concept FROM concept_map "
            "ORDER BY concept, symbol", conn
        )
    except sqlite3.Error:
        return pd.Series(dtype="object")
    if df.empty:
        return pd.Series(dtype="object")
    return df.drop_duplicates("symbol").set_index("symbol")["concept"]


def compute_concept_rps(
    conn, frames: dict[str, pd.DataFrame], names: pd.Series | None = None,
    periods: tuple[int, ...] = config.PERIODS,
) -> pd.DataFrame:
    """概念板块 RPS，复用行业 RPS 的向量化实现。"""
    mapping = load_concept_map(conn)
    if mapping.empty:
        return pd.DataFrame()
    leader = frames.get("rps60")
    if leader is None:
        leader = frames.get(f"rps{periods[0]}")
    df = compute_industry_rps(
        frames, mapping, leader_rps=leader, names=names, periods=periods
    )
    if df.empty:
        return df
    return df.rename(columns={"industry": "concept"})


# ── 落库 ────────────────────────────────────────────────────────────
def pending_dates(
    conn: sqlite3.Connection, table: str = "rps_daily"
) -> list[str]:
    """还没有算出 RPS 的交易日（升序）。

    判据是「晚于目标表里**最后一个已算出的日期**」，而不是朴素的
    「daily_bar 里有、rps_daily 里没有」——后者会永远返回一批算不出结果的日期：
    实测 2023-05-04~09 这四天 daily_bar 有 13~14 只股票的 K 线，但它们当时都还没有
    满 21 根前置 K 线，四个周期全为 NaN，落库时被 dropna 掉了，于是永远缺失。
    区间起点之前的 2023-02-13~03-10 同理。

    取「晚于 dmax」还有一个好处：新交易日如果当天只回补到少量股票（数据不全），
    这天算不出任何行、dmax 不推进，第二天会自动重试，直到算出为止。

    中间出现**真正的空洞**（例如某次抓取部分失败）增量补不上，用 `--full` 重算。
    """
    tgt = "rps_daily" if table == "rps_daily" else "industry_rps_daily"
    row = conn.execute(f"SELECT MAX(date) AS dmax FROM {tgt}").fetchone()
    if not row or not row["dmax"]:
        # 目标表为空 → 整体都算待算
        return [r["date"] for r in conn.execute(
            "SELECT DISTINCT date FROM daily_bar ORDER BY date"
        )]
    return [
        r["date"] for r in conn.execute(
            "SELECT DISTINCT d.date AS date FROM daily_bar d "
            "WHERE d.date > ? ORDER BY d.date",
            (row["dmax"],),
        )
    ]


def window_start_for(
    conn: sqlite3.Connection, first_target: str, periods: tuple[int, ...] = config.PERIODS
) -> str:
    """算 first_target 当天的 RPS 需要往前多少历史。

    ret_N 需要 N 根前置 K 线，因此取 `first_target` 之前最近的 max(periods) 个交易日，
    再多留 10 根缓冲（跨节假日、停牌导致的有效交易日变少）。
    """
    need = max(periods) + 10
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily_bar WHERE date < ? ORDER BY date DESC LIMIT ?",
        (first_target, need),
    ).fetchall()
    return rows[-1]["date"] if rows else ""


def materialize(
    conn: sqlite3.Connection,
    close_wide: pd.DataFrame,
    tradable: pd.DataFrame | None,
    st_flags: pd.DataFrame | None,
    industries: pd.Series,
    names: pd.Series | None,
    exclude_st: bool = True,
    periods: tuple[int, ...] = config.PERIODS,
    mode: str = "full",
    target_dates: list[str] | None = None,
) -> tuple[int, int]:
    """计算并写入 rps_daily / industry_rps_daily。返回 (个股行数, 板块行数)。

    mode:
      - ``full``        全量重算：先清空两张表再整体写入。用于首次计算，
                        以及**历史行情被订正**之后（增量察觉不到订正）。
      - ``incremental`` 只重算 ``target_dates`` 这些日期并原地覆盖。
                        正确性依据：某个日期的横截面百分位只依赖该日期及其之前的
                        收盘价，新数据不会改变任何历史日期的名次。

    注意 `member_count` / 领涨股等字段也一并重算，因此增量与全量对同一天的输出
    完全一致（见 backend/tests/verify_incremental.py）。
    """
    run_id = db.start_run(conn, task="compute-rps", source="internal")

    frames = compute_rps_frames(close_wide, tradable, st_flags, exclude_st, periods)
    if not frames:
        db.finish_run(conn, run_id, "failed", 0, "close_wide 为空")
        return 0, 0

    dates = close_wide.index.strftime("%Y-%m-%d")
    symbol_cols = list(close_wide.columns)

    payload: dict[str, np.ndarray] = {"date": np.repeat(dates, len(symbol_cols))}
    flat_sym = np.tile(np.array(symbol_cols), len(dates))
    payload["symbol"] = flat_sym
    for n in periods:
        payload[f"rps{n}"] = frames[f"rps{n}"].to_numpy().ravel()
        payload[f"ret{n}"] = frames[f"ret{n}"].to_numpy().ravel()

    rps_df = pd.DataFrame(payload)
    # 至少一个周期有效才落库（全 NaN 行没有信息量）
    rps_cols = [f"rps{n}" for n in periods]
    rps_df = rps_df.dropna(subset=rps_cols, how="all")
    rps_df = rps_df.replace([np.inf, -np.inf], np.nan)

    # 领涨股以 60 日周期为代表（中期代表性最好）；注意不能用 `a or b`——
    # DataFrame 的真值是未定义的，会抛 ValueError。
    leader = frames.get("rps60")
    if leader is None:
        leader = frames.get(f"rps{periods[0]}")
    ind_df = compute_industry_rps(
        frames, industries, leader_rps=leader, names=names, periods=periods,
    )

    if mode == "incremental" and target_dates is not None:
        wanted = set(target_dates)
        rps_df = rps_df[rps_df["date"].isin(wanted)]
        if not ind_df.empty:
            ind_df = ind_df[ind_df["date"].isin(wanted)]

    n_stock = 0
    n_ind = 0
    if mode == "incremental" and target_dates is not None:
        # 原地覆盖指定日期：先删这些日期再写，天然幂等
        if not rps_df.empty:
            n_stock = db.replace_date_partition(
                conn, "rps_daily", target_dates, rps_df
            )
        if not ind_df.empty:
            n_ind = db.replace_date_partition(
                conn, "industry_rps_daily", target_dates, ind_df
            )
    else:
        conn.execute("DELETE FROM rps_daily")
        conn.execute("DELETE FROM industry_rps_daily")
        conn.commit()

        if not rps_df.empty:
            rps_df.to_sql("rps_daily", conn, if_exists="append", index=False, chunksize=2000)
            n_stock = len(rps_df)
        if not ind_df.empty:
            ind_df.to_sql(
                "industry_rps_daily", conn, if_exists="append", index=False, chunksize=2000
            )
            n_ind = len(ind_df)
        conn.commit()

    n_dates = len(target_dates) if (mode == "incremental" and target_dates) else 0
    db.finish_run(conn, run_id, "success", n_stock + n_ind,
                  f"mode={mode} 日期={n_dates or '全部'} 个股 {n_stock} 行 / 板块 {n_ind} 行")
    return n_stock, n_ind


def load_matrices(conn: sqlite3.Connection, start: str | None = None):
    """一次性加载回测/计算所需的全部矩阵。"""
    long = matrix.load_long(conn, start)
    return (
        long,
        matrix.build_close_matrix(long),
        matrix.build_tradable_matrix(long),
        matrix.build_st_flags(long),
    )
