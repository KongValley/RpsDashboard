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


def fetch_boards(src: TdxSource | None = None, category: str = "概念") -> pd.DataFrame:
    """拉取板块清单。返回列：board_code / full_code / board_name / is_concept。

    `category` 直接透传给 eltdx（概念 / 地区 / 风格 / 一级行业 / 二级行业 /
    三级行业 / 880行业 / 全部）。`is_concept` 是按名字猜的启发式，只对概念分类
    有意义，其它分类请忽略该列（行业采集只取 full_code / board_name）。
    """
    own = src is None
    src = src or TdxSource()
    try:
        table = src._client.helpers.board_quotes(category=category)  # noqa: SLF001
        # 空选择时 rows 是空 tuple：原来写成 `rows or table` 会在空选择上
        # 变成 list(BoardQuoteTable) → TypeError，把「这个分类没数据」伪装成崩溃
        rows = list(getattr(table, "rows", ()) or ())
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
        board_code = code or (full[2:] if len(full) > 2 else "")
        if not board_code:
            # 畸形行（只有交易所前缀、没有板块代码）：丢了也不能让它变成一条
            # 空键的行 —— 空 board_code 后续会被 DELETE 匹配成一片空白。
            continue
        recs.append({
            "board_code": board_code,
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


def sync_concept_map(progress=None) -> tuple[int, list[str]]:
    """抓取全部概念板块成分股，**按板块增量**写入 concept_map。

    返回 (覆盖的股票数, 失败板块名列表)。

    每 3.7 秒一个板块、约 500 个板块，全程约 31 分钟。
    失败的板块**保留旧行**而不是整表替换：31 分钟的长任务里一个坏板块就清空
    整张表，会让概念 RPS 当天直接算不出东西（而本轮结果其实基本可用）。
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

    grabbed: list[tuple[dict, list[str]]] = []
    failed_boards: list[str] = []
    src = TdxSource()
    try:
        for i, (_, b) in enumerate(concepts.iterrows(), 1):
            try:
                members = list(dict.fromkeys(fetch_members(b["full_code"], src)))
                grabbed.append((b, members))
            except Exception as exc:  # noqa: BLE001
                failed_boards.append(b["board_name"])
                if len(failed_boards) <= 3:
                    print(f"[concept] 板块 {b['board_name']} 失败: {exc}")
            if i % 25 == 0:
                el = time.time() - t0
                print(f"[concept] {i}/{len(concepts)} 失败 {len(failed_boards)} "
                      f"| 已用 {el/60:.1f} 分钟 | 预计剩余 {el/i*(len(concepts)-i)/60:.1f} 分钟",
                      flush=True)
    finally:
        src.close()

    if not grabbed:
        raise RuntimeError("未取到任何概念成分股")

    conn = db.connect()
    try:
        # board_code 为 NULL 的旧行不会被任何板块的 DELETE 命中，
        # 是「整表替换」时代留下的孤儿，一次性清掉
        conn.execute("DELETE FROM concept_map WHERE board_code IS NULL")
        for b, members in grabbed:
            conn.execute(
                "DELETE FROM concept_map WHERE board_code = ?", (b["board_code"],))
            if not members:
                continue
            now = db.now()
            conn.executemany(
                "INSERT OR REPLACE INTO concept_map"
                "(symbol, concept, board_code, updated_at) VALUES (?,?,?,?)",
                [(s, b["board_name"], b["board_code"], now) for s in members],
            )
        conn.commit()
        n_sym = conn.execute(
            "SELECT COUNT(DISTINCT symbol) AS n FROM concept_map").fetchone()["n"]
        n_rows = conn.execute("SELECT COUNT(*) AS n FROM concept_map").fetchone()["n"]
        n_concepts = conn.execute(
            "SELECT COUNT(DISTINCT concept) AS n FROM concept_map").fetchone()["n"]
    finally:
        conn.close()

    print(f"[concept] 完成：{n_rows} 条 成分关系，覆盖 {n_sym} 只股票、"
          f"{n_concepts} 个概念，失败 {len(failed_boards)} 个板块，"
          f"总耗时 {(time.time()-t0)/60:.1f} 分钟")
    if failed_boards:
        print(f"[concept] ⚠ 失败板块（保留旧成分）："
              f"{', '.join(failed_boards[:10])}"
              f"{' 等' if len(failed_boards) > 10 else ''}")
    return n_sym, failed_boards
