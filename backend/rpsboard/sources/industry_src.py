"""行业分类采集 —— 写入 `industry_map`。

为什么需要它：`industry_map` 是板块 RPS / 行业筛选 / 行业内排名 / 个股行业列的
唯一数据源，但仓库里**没有任何生产写入方**（只有测试写临时库）。作者机器上的
5224 行来自已删除的一次性脚本，别人 clone 下来这块永远是空的。

数据来源按优先级（实测 2026-10-13 本机结论）：

1. **eltdx 具名行业板块**：`board_quotes(category=...)` 依次试
   `一级行业 / 二级行业 / 三级行业 / 880行业`。本机实测这四个分类都是 **0 个板块**：
   板块定义文件 `tdxzs.cfg` / `tdxzs3.cfg` 不在本地、服务器 `download_file` 也返回
   0 字节（只有 `infoharbor_block.dat` 的 概念(269) + 风格(158) 可用）。因此这条路
   在取到数据时才会走，取不到就落到下一条。
2. **`tdxhy.cfg`（通达信行业映射，服务器可下）**：5668 行、每行一只票、无重复，
   形如 `0|000001|T1001|||X500102`。第 6 列是层级键（`X` + 2/4/6 位 = 一/二/三级），
   本机取 **一级前缀**（`X` + 2 位，共 31 组）作为行业键。该文件**不含行业名称**，
   所以这一路的 `industry` 只能是代码（如 `X32`），`industry_cls='通达信行业代码'`。
3. **用户自带映射**：`--from-csv path.csv`，列 `symbol,industry[,industry_cls]`。
   想恢复「证监会行业分类」这类具名口径就只能走这条（本机原先那 5224 行就来自
   已删除的脚本，仓库里没有可复现的名称来源）。

无论哪一路，服务端**默认不覆盖已有数据**（`--replace` 才整表重建）：不同口径的
行业表混在一起会让「行业 RPS」失去可比性。
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from .. import config, db
from .board_src import fetch_boards, fetch_members
from .tdxnet_src import TdxSource

#: 依次尝试的 eltdx 具名行业分类（能取到名字时优先用它）
CATEGORIES = ("一级行业", "二级行业", "三级行业", "880行业")

#: tdxhy.cfg 里的行业层级键前缀：X + 2 位 = 一级行业
TDXHY_LEVEL_PREFIX = 3

#: 写进 industry_map.industry_cls 的口径标记
CLS_NAMED = "通达信{}"
CLS_CODE = "通达信行业代码"
CLS_CSV = "外部导入"


def _pick_one_industry(rows: list[dict]) -> pd.DataFrame:
    """同一只股票只保留一个行业。

    一只票可能同时属于多个行业板块，若全保留，行业 RPS 会把同一只股票的涨幅
    重复计入多个行业，成分数也虚高（与 concept 采集「每只票只归一个主分组」
    的既有口径一致）。规则：取**板块成员数最少**的那个行业；平手时取板块名
    排序靠前者，保证结果稳定可复现。
    """
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    sizes = df.groupby("industry")["symbol"].transform("size")
    df = df.assign(_size=sizes).sort_values(["_size", "industry", "symbol"])
    return df.drop_duplicates(subset=["symbol"], keep="first").drop(columns="_size")


def _from_named_boards(progress=None) -> tuple[pd.DataFrame, str, int]:
    """eltdx 具名行业板块 → (df[symbol, industry], 口径标签, 失败板块数)。

    取不到任何分类时返回空表 + 0 失败（由调用方落到下一条数据源）。
    失败数必须往上传：调用方要靠它决定「能不能整表重建」。
    """
    for category in CATEGORIES:
        boards = fetch_boards(category=category)
        if boards.empty:
            continue
        say = progress or (lambda _m: None)
        say("industry")
        print(f"[industry] 使用 eltdx 板块分类 {category}：{len(boards)} 个板块")
        rows: list[dict] = []
        failed: list[str] = []
        src = TdxSource()
        try:
            for i, (_, b) in enumerate(boards.iterrows(), 1):
                try:
                    for sym in fetch_members(b["full_code"], src):
                        rows.append({"symbol": sym, "industry": b["board_name"]})
                except Exception as exc:  # noqa: BLE001
                    failed.append(b["board_name"])
                    if len(failed) <= 3:
                        print(f"[industry] 板块 {b['board_name']} 失败: {exc}")
                if i % 10 == 0:
                    print(f"[industry] {i}/{len(boards)} 失败 {len(failed)}", flush=True)
        finally:
            src.close()
        if rows:
            print(f"[industry] {len(rows)} 条成分关系，失败 {len(failed)} 个板块")
            return _pick_one_industry(rows), CLS_NAMED.format(category), len(failed)
    return pd.DataFrame(), "", 0


def _ensure_tdxhy(path: Path) -> Path:
    """确保 tdxhy.cfg 在本地；缺失时从服务器下（eltdx 的 boards 缓存也会下它）。"""
    if path.is_file() and path.stat().st_size > 0:
        return path
    src = TdxSource()
    try:
        payload = src._client.resources.download_file("tdxhy.cfg")  # noqa: SLF001
    finally:
        src.close()
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        raise RuntimeError(
            f"tdxhy.cfg 既不在 {path} 也无法从服务器下载；"
            f"请用 `cli industry-map --from-csv <你自己的映射.csv>` 提供行业分类"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(payload))
    print(f"[industry] 已下载 tdxhy.cfg → {path}（{len(payload)} 字节）")
    return path


def _from_tdxhy(progress=None) -> tuple[pd.DataFrame, str, int]:
    """tdxhy.cfg → (df[symbol, industry], 口径标签, 失败数)。行业键是层级代码，无名称。"""
    say = progress or (lambda _m: None)
    say("industry")
    path = _ensure_tdxhy(Path(config.DOWNLOAD_DIR) / "tdxhy.cfg")
    rows: list[dict] = []
    for line in path.read_text(encoding="gbk", errors="replace").splitlines():
        fields = line.split("|")
        if len(fields) < 6:
            continue
        symbol = fields[1].strip()
        key = fields[5].strip()
        if len(symbol) != 6 or not symbol.isdigit():
            continue
        if len(key) <= TDXHY_LEVEL_PREFIX:
            continue
        rows.append({"symbol": symbol, "industry": key[:TDXHY_LEVEL_PREFIX]})
    df = _pick_one_industry(rows)
    print(f"[industry] tdxhy.cfg：{len(df)} 只股票 / "
          f"{df['industry'].nunique() if not df.empty else 0} 个行业键"
          f"（该文件不含行业名称，键为层级代码）")
    # 文件是**整份**映射，不存在部分失败
    return df, CLS_CODE, 0


def _from_csv(path: Path, progress=None) -> tuple[pd.DataFrame, str, int]:
    """用户自带映射：symbol,industry[,industry_cls]。"""
    say = progress or (lambda _m: None)
    say("industry")
    df = pd.read_csv(path, dtype=str).fillna("")
    if "symbol" not in df.columns or "industry" not in df.columns:
        raise RuntimeError(f"{path} 需要至少包含 symbol 与 industry 两列")
    df = df[df["symbol"].str.fullmatch(r"\d{6}") & (df["industry"] != "")]
    if df.empty:
        raise RuntimeError(f"{path} 没有有效的 (symbol, industry) 行")
    cls = df["industry_cls"].iloc[0] if "industry_cls" in df.columns else CLS_CSV
    df = df.assign(industry_cls=cls or CLS_CSV)
    print(f"[industry] 从 {path} 读入 {len(df)} 只股票 / {df['industry'].nunique()} 个行业")
    picked = _pick_one_industry(df[["symbol", "industry"]])
    return picked.assign(industry_cls=cls or CLS_CSV), cls or CLS_CSV, 0


def sync_industry_map(
    replace: bool = False, from_csv: str | Path | None = None, progress=None
) -> tuple[int, int]:
    """写入 industry_map，返回 (覆盖的股票数, 失败的板块数)。

    已有数据且未指定 replace 时返回 (0, 0)（保护已有的具名口径，例如证监会行业分类）。

    写入是**按 symbol 覆盖**而不是先清空整表：部分板块采集失败时，整表重建会把
    那些失败板块覆盖的股票连旧行一起删掉，行业 RPS 当天就出现整块缺口。
    只有在「--replace 且本轮零失败」时才真正清空重建（这才是文档承诺的
    「整表重建」语义）。
    """
    conn = db.connect()
    try:
        existing = conn.execute("SELECT COUNT(*) AS c FROM industry_map").fetchone()["c"]
        if existing and not replace:
            cls = conn.execute(
                "SELECT industry_cls, COUNT(*) AS c FROM industry_map "
                "GROUP BY industry_cls ORDER BY c DESC LIMIT 1"
            ).fetchone()
            label = f"{cls['industry_cls']}（{cls['c']} 行）" if cls else "未知"
            print(f"[industry] industry_map 已有 {existing} 行，口径：{label}，跳过；"
                  f"要重建请加 --replace")
            return 0, 0

        t0 = time.time()
        if from_csv:
            df, cls, n_failed = _from_csv(Path(from_csv), progress=progress)
        else:
            df, cls, n_failed = _from_named_boards(progress=progress)
            if df.empty:
                df, cls, n_failed = _from_tdxhy(progress=progress)
        if df.empty:
            raise RuntimeError(
                "没有取到任何行业分类；请用 `cli industry-map --from-csv <映射.csv>` 提供"
            )

        df = df.assign(industry_cls=df["industry_cls"] if "industry_cls" in df else cls,
                       updated_at=db.now())
        cols = df[["symbol", "industry", "industry_cls", "updated_at"]]
        if replace and not n_failed:
            conn.execute("DELETE FROM industry_map")
        conn.executemany(
            "INSERT OR REPLACE INTO industry_map"
            "(symbol, industry, industry_cls, updated_at) VALUES (?,?,?,?)",
            list(cols.itertuples(index=False, name=None)),
        )
        conn.commit()

        n_sym = int(df["symbol"].nunique())
        print(f"[industry] 完成：{n_sym} 只股票 / {df['industry'].nunique()} 个行业"
              f"（口径：{cls}），总耗时 {(time.time()-t0)/60:.1f} 分钟")
        if n_failed:
            print(f"[industry] ⚠ {n_failed} 个板块失败：这些股票保留原有行业行，"
                  f"本轮不做整表重建")
        return n_sym, n_failed
    finally:
        conn.close()
