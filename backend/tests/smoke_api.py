"""接口契约冒烟：对**运行中的服务**逐条断言关键契约（只用标准库）。

这些断言对应的是修过的真实缺陷 —— 手点页面容易漏，脚本每次都能跑：

1. `/api/meta/status` 必须带数据质量块（涨跌停覆盖 / 复权异常 / 概念覆盖）
2. 排行榜按成交额排序：页内单调递减，且**分页连续**（第 1 页最小值 ≥ 第 2 页最大值）
   —— 旧实现内层 `LIMIT 200` 没有 ORDER BY，选中的是索引扫描顺序那一批弱势股，
   页面与 CSV 导出互相矛盾
3. 全市场 RPS 第一名的「跑赢全市场」必须是 100% —— 旧实现语义反了，显示 0.0%
4. 周期值为 NULL 的股票详情/曲线必须 200（旧实现把 NaN 丢给 Starlette 直接 500）
5. 导出 CSV 必须可被 `csv` 模块解析、行数 = total + 1、代码列保持 6 位
6. 非法周期必须回落到默认周期而不是报错
7. QMT 交易端点在**没有交易数据**时也必须返回结构化空结果（不是 500），
   且任何接口里都不出现未脱敏的资金账号
8. 模拟持仓端点在**没有任何账户**时返回空列表而不是 500；不猜默认账户 id
9. 交易工具的纯计算（仓位/往返成本）在**空库**上也必须能算（不读证券表/行情）

用法（服务默认 http://127.0.0.1:8000）：
    backend/.venv/Scripts/python.exe -m rpsboard.cli serve     # 另开一个终端
    backend/.venv/Scripts/python.exe backend/tests/smoke_api.py
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request

FAILS: list[str] = []


def check(ok: bool, title: str, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {title}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAILS.append(title)


def get_json(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_text(base: str, path: str) -> str:
    with urllib.request.urlopen(base + path, timeout=120) as resp:
        return resp.read().decode("utf-8-sig")


def main() -> int:
    ap = argparse.ArgumentParser(description="RPS 看板接口契约冒烟")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    print(f"[smoke] 目标 {base}")

    try:
        status = get_json(base, "/api/meta/status")
    except Exception as exc:  # noqa: BLE001
        print(f"[smoke] 服务不可达: {type(exc).__name__}: {exc}")
        return 2

    check(bool(status.get("db_ready")), "服务就绪且库已初始化")
    quality = status.get("quality") or {}
    check(quality.get("price_limit") is not None, "status 带涨跌停覆盖")
    check(quality.get("adjust_events") is not None, "status 带复权异常计数")
    check(quality.get("concept") is not None, "status 带概念覆盖")
    lim = quality.get("price_limit") or {}
    if lim.get("days"):
        print(f"       涨跌停实测 {lim['days']}/{lim.get('total_days')} 天"
              f"（{lim.get('first')} ~ {lim.get('last')}）")

    periods = get_json(base, "/api/meta/periods")
    check(isinstance(periods, list) and len(periods) >= 3,
          "周期清单非空", str(periods))

    # ── 排行榜：成交额排序 + 分页连续性 ────────────────────────────
    p1 = get_json(base, "/api/rps/ranking?period=60&sort=amount&limit=100&page=1")
    p2 = get_json(base, "/api/rps/ranking?period=60&sort=amount&limit=100&page=2")
    rows1, rows2 = p1.get("rows", []), p2.get("rows", [])
    check(bool(rows1) and bool(rows2), "成交额排行榜有两页数据",
          f"total={p1.get('total')}")
    amounts1 = [r["amount"] for r in rows1 if r.get("amount") is not None]
    check(amounts1 == sorted(amounts1, reverse=True), "页内按成交额单调递减",
          f"首 {amounts1[0]:,}" if amounts1 else "")
    amounts2 = [r["amount"] for r in rows2 if r.get("amount") is not None]
    if amounts1 and amounts2:
        check(min(amounts1) >= max(amounts2), "分页连续（第 1 页 ≥ 第 2 页）",
              f"{min(amounts1):,} vs {max(amounts2):,}")
    symbols = [r["symbol"] for r in rows1 + rows2]
    check(len(symbols) == len(set(symbols)), "两页之间没有重复股票")

    # ── 跑赢全市场：第一名必须 100% ────────────────────────────────
    top = get_json(base, "/api/rps/ranking?period=60&sort=rps&limit=1")
    top_rows = top.get("rows", [])
    check(bool(top_rows), "RPS 榜首可取到")
    if top_rows:
        sym = top_rows[0]["symbol"]
        detail = get_json(base, f"/api/stock/{sym}")
        pct = (detail.get("market_percentile") or {}).get("rps60")
        check(pct is not None and pct >= 99.9,
              "榜首「跑赢全市场」= 100%", f"{sym} → {pct}")

    # ── NULL 周期不得 500 ─────────────────────────────────────────
    for sym in ("688796", "600076"):
        try:
            detail = get_json(base, f"/api/stock/{sym}")
            check(isinstance(detail.get("info"), dict), f"/api/stock/{sym} 返回 200")
        except Exception as exc:  # noqa: BLE001
            check(False, f"/api/stock/{sym} 返回 200", f"{type(exc).__name__}: {exc}")
    try:
        curve = get_json(base, "/api/stock/688796/curve?days=250")
        check(isinstance(curve.get("dates"), list), "/api/stock/688796/curve 返回 200")
    except Exception as exc:  # noqa: BLE001
        check(False, "/api/stock/688796/curve 返回 200", f"{type(exc).__name__}: {exc}")

    # ── 导出 CSV ──────────────────────────────────────────────────
    # 导出端点**忽略 limit**：它一次把全市场（上限 20 万行）写出来，
    # 所以行数应当等于排行榜的 total。
    text = get_text(base, "/api/rps/ranking/export?period=60&sort=amount")
    parsed = list(csv.reader(io.StringIO(text)))
    check(len(parsed) >= 2, "导出 CSV 可解析")
    if len(parsed) >= 2:
        header, body = parsed[0], parsed[1:]
        total = int(p1.get("total") or 0)
        check(len(body) == total, "导出行数 = 排行榜 total", f"{len(body)} vs {total}")
        col = header.index("代码") if "代码" in header else 0
        code = body[0][col]
        bare = code.strip('="')
        check(bare.isdigit() and len(bare) == 6, "代码列保持 6 位", repr(code))

    # ── 非法参数回落而不是 500 ────────────────────────────────────
    try:
        bad = get_json(base, "/api/rps/ranking?period=999")
        check(isinstance(bad.get("rows"), list), "非法周期回落（不报错）",
              f"period={bad.get('period')}")
    except Exception as exc:  # noqa: BLE001
        check(False, "非法周期回落（不报错）", f"{type(exc).__name__}: {exc}")

    # ── 三升底筛选（历史日期）─────────────────────────────────────
    try:
        pat = get_json(base, f"/api/rps/ranking?date={status.get('last_bar_date')}&sanxingdi=5")
        check(isinstance(pat.get("rows"), list), "三升底筛选返回 200")
    except Exception as exc:  # noqa: BLE001
        check(False, "三升底筛选返回 200", f"{type(exc).__name__}: {exc}")

    # ── QMT 交易端点（只读库；空库也要返回结构而不是 500）────────
    for ep in ("/api/trade/summary", "/api/trade/positions", "/api/trade/orders",
               "/api/trade/fills", "/api/trade/plans"):
        try:
            body = get_json(base, ep)
            check(isinstance(body, dict), f"{ep} 返回 200")
        except Exception as exc:  # noqa: BLE001
            check(False, f"{ep} 返回 200", f"{type(exc).__name__}: {exc}")
    try:
        s = get_json(base, "/api/trade/summary")
        check(isinstance(s.get("mode"), str) and isinstance(s.get("halt"), bool),
              "/api/trade/summary 含 mode / halt", f"mode={s.get('mode')}")
        check(isinstance(s.get("positions"), list) and isinstance(s.get("reconcile"), list),
              "/api/trade/summary 含 positions / reconcile")
        # 账号只允许脱敏形态出现在任何接口里
        acct = s.get("account_masked") or ""
        check(acct == "" or acct.startswith("****"), "账号已脱敏", acct)
    except Exception as exc:  # noqa: BLE001
        check(False, "/api/trade/summary 契约", f"{type(exc).__name__}: {exc}")

    # ── 模拟持仓（多账户、原价、不连券商）──────────────────────────
    try:
        accs = get_json(base, "/api/portfolio/accounts")
        rows = accs.get("rows") or []
        check(isinstance(rows, list), "/api/portfolio/accounts 返回 200")
        if rows:
            # **用返回的真实 id**，绝不猜默认账户（没有「默认账户」这回事）
            aid = rows[0]["id"]
            detail = get_json(base, f"/api/portfolio/accounts/{aid}")
            check(isinstance(detail.get("days"), int)
                  and isinstance(detail.get("curve"), list),
                  "账户详情返回 200", f"#{aid} {detail.get('days')} 天")
            check(isinstance(detail.get("available_cash"), (int, float)),
                  "账户详情含可用现金（工具带入资金要用它）")
            risk = get_json(base, f"/api/portfolio/accounts/{aid}/risk")
            check(risk.get("account_id") == aid and "alerts" in risk,
                  "账户风险体检带 account_id")
        else:
            check(True, "模拟盘为空（还没有建过账户）")
    except Exception as exc:  # noqa: BLE001
        check(False, "/api/portfolio/accounts 契约", f"{type(exc).__name__}: {exc}")

    # 旧的单账户端点必须已经下线
    try:
        old = get_json(base, "/api/trade/paper")
        check(not isinstance(old, dict) or "days" not in old,
              "/api/trade/paper 已下线（不再返回模拟盘结构）")
    except Exception:
        check(True, "/api/trade/paper 已下线（返回错误而不是 JSON）")

    # ── 交易工具：纯计算不依赖数据库 ──────────────────────────────
    try:
        body = json.dumps({"symbol": "000001", "volume": 1000, "buy_price": 10.0,
                           "sell_price": 11.0, "trade_date": "2026-01-05"}).encode()
        req = urllib.request.Request(base + "/api/tools/round-trip", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            rt = json.loads(resp.read().decode("utf-8"))
        check(abs(rt.get("net_pnl", 0) - 984.29) < 0.01
              and abs(rt.get("breakeven_price", 0) - 10.02) < 0.001,
              "往返成本工具可独立于行情计算",
              f"net_pnl={rt.get('net_pnl')} breakeven={rt.get('breakeven_price')}")
    except Exception as exc:  # noqa: BLE001
        check(False, "/api/tools/round-trip 契约", f"{type(exc).__name__}: {exc}")

    # ── 因子评估（只读库；没跑过时要返回空结构而不是 500）──────────
    try:
        fc = get_json(base, "/api/factors")
        check(isinstance(fc.get("factors"), list) and isinstance(fc.get("corr"), dict),
              "/api/factors 返回 200")
        names = (fc.get("corr") or {}).get("names") or []
        matrix = (fc.get("corr") or {}).get("matrix") or []
        if fc.get("factors"):
            check(len(matrix) == len(names) and all(len(row) == len(names) for row in matrix),
                  "因子相关矩阵为 N×N",
                  f"run_at={fc.get('run_at')} 因子 {len(fc['factors'])} 个")
        else:
            check(True, "尚无因子评估（尚未跑 cli factors）")
    except Exception as exc:  # noqa: BLE001
        check(False, "/api/factors 返回 200", f"{type(exc).__name__}: {exc}")

    print(f"[smoke] 完成：{len(FAILS)} 项失败")
    for f in FAILS:
        print(f"  - {f}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
