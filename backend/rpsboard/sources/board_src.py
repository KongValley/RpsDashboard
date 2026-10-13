"""概念/题材板块采集（eltdx 通达信协议）。

为什么需要概念板块：证监会行业只有 83 个，粒度太粗
（`C39 计算机、通信…` 里塞了 619 只），而 A 股实盘看的是概念。

接口实测（本机 2026-10-12）：

| 接口                          | 结果                                        |
|-------------------------------|---------------------------------------------|
| `board_quotes()`              | ✅ **79.3 秒**返回全量板块清单（含概念板块）  |
| `board_member_quotes(code)`   | ✅ **3.7 秒**返回该板块成分股代码            |
| `topic_stocks(code)`          | ❌ `topic not found`，不可用                 |
| `theme_strength_rank()`       | ❌ 90.7 秒 `ResponseTimeoutError`            |
| `stock_topics(code)`          | ⚠️ 1.5 秒/只（全市场 2.3 小时，仅作退路）     |

预计成本：79 秒 + 约 500 个板块 × 3.7 秒 ≈ **31 分钟**，适合夜间或手动触发。

注意 `board_member_quotes` **必须传完整代码**（`sh880515` 而不是 `880515`），
传 6 位会被拼成 `bj880515` 从而报「topic not found」。
"""

from __future__ import annotations

import time

import pandas as pd

from .. import db
from .tdxnet_src import TdxSource

#: 排除这些「板块」——它们是通达信自带的宽基/风格指数，不是概念题材
NON_CONCEPT_PREFIX = ("通达信", "沪深", "上证", "深证", "中证", "国证", "上证50")


def _is_concept(board_name: str) -> bool:
    n = str(board_name or "")
    return bool(n) and not n.startswith(NON_CONCEPT_PREFIX)


def fetch_boards(src: TdxSource | None = None) -> pd.DataFrame:
    """拉取全量板块清单。返回列：board_code / full_code / board_name / is_concept。"""
    own = src is None
    src = src or TdxSource()
    try:
        table = src._client.helpers.board_quotes()  # noqa: SLF001
        rows = list(getattr(table, "rows", None) or table or [])
    finally:
        if own:
            src.close()

    recs = []
    for r in rows:
        code = str(getattr(r, "board_code", "") or "")
        full = str(getattr(r, "full_code", "") or "")
        if not code and not full:
            continue
        if not full and code:
            full = ("sh" if code.startswith(("88", "000")) else "sz") + code
        name = getattr(r, "board_name", "") or ""
        recs.append({
            "board_code": code or full[2:],
            "full_code": full,
            "board_name": name,
            "is_concept": 1 if _is_concept(name) else 0,
        })
    return pd.DataFrame(recs).drop_duplicates(subset=["board_code"])


def fetch_members(full_code: str, src: TdxSource) -> list[str]:
    """取某个板块的成分股代码列表。"""
    table = src._client.helpers.board_member_quotes(full_code)  # noqa: SLF001
    members = list(getattr(table, "raw_members", None) or [])
    out = []
    for m in members:
        code = m.get("code") if isinstance(m, dict) else getattr(m, "code", None)
        if code:
            out.append(str(code).zfill(6))
    return out


def sync_concept_map(progress=None) -> int:
    """抓取全部概念板块成分股，写入 concept_map。返回覆盖的股票数。

    每 3.7 秒一个板块、约 500 个板块，全程约 31 分钟。
    中途失败只记录并继续，不让一个坏板块毁掉整轮采集。
    """
    say = progress or (lambda _msg: None)
    t0 = time.time()
    say("boards")
    boards = fetch_boards()
    if boards.empty:
        raise RuntimeError("board_quotes 返回空，无法采集概念板块")
    concepts = boards[boards["is_concept"] == 1]
    say(f"boards:{len(concepts)}")
    print(f"[concept] 板块 {len(boards)} 个，其中概念题材 {len(concepts)} 个，"
          f"清单耗时 {time.time()-t0:.0f}s")

    rows: list[dict] = []
    failed = 0
    src = TdxSource()
    try:
        for i, (_, b) in enumerate(concepts.iterrows(), 1):
            try:
                for sym in fetch_members(b["full_code"], src):
                    rows.append({"symbol": sym, "concept": b["board_name"],
                                 "board_code": b["board_code"],
                                 "updated_at": db.now()})
            except Exception as exc:  # noqa: BLE001
                failed += 1
                if failed <= 3:
                    print(f"[concept] 板块 {b['board_name']} 失败: {exc}")
            if i % 25 == 0:
                el = time.time() - t0
                print(f"[concept] {i}/{len(concepts)} 失败 {failed} "
                      f"| 已用 {el/60:.1f} 分钟 | 预计剩余 {el/i*(len(concepts)-i)/60:.1f} 分钟",
                      flush=True)
    finally:
        src.close()

    if not rows:
        raise RuntimeError("未取到任何概念成分股")

    df = pd.DataFrame(rows).drop_duplicates(subset=["symbol", "concept"])
    conn = db.connect()
    try:
        conn.execute("DELETE FROM concept_map")
        df.to_sql("concept_map", conn, if_exists="append", index=False)
        conn.commit()
    finally:
        conn.close()

    n_sym = df["symbol"].nunique()
    print(f"[concept] 完成：{len(df)} 条 成分关系，覆盖 {n_sym} 只股票、"
          f"{df['concept'].nunique()} 个概念，失败 {failed} 个板块，"
          f"总耗时 {(time.time()-t0)/60:.1f} 分钟")
    return n_sym
