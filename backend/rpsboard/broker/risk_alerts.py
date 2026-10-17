"""模拟盘持仓的风险告警（阈值触发，**默认不推送**）。

与 `analytics/alerts.py` 的区别：
- 那套按**交易日**评估市场信号（RPS / 三升底 / 涨停）并把命中落库、供页面查看；
- 这套按**当前持仓**评估组合风险（单票集中度 / 行业集中度 / 两两相关性 /
  当前回撤），默认只在终端打印，`--push` 才发微信。

阈值：默认取自 `config`（可用 `RPSBOARD_RISK_*` 环境变量改），命令行也能逐次覆盖。
刻意**不**落库、不自动跑：组合风险是"你去看的时候"才关心的东西，
塞进 `cli update` 只会让每天的更新多一步没人看的输出。
"""

from __future__ import annotations

import sqlite3

from .. import config
from . import sim

#: 告警种类 → 人类可读名（前端与推送都用同一套措辞）
KINDS = {
    "single_weight": "单票集中度",
    "industry_weight": "行业集中度",
    "correlation": "持仓相关性",
    "drawdown": "当前回撤",
}


def thresholds(
    *,
    max_single: float | None = None,
    max_industry: float | None = None,
    max_avg_corr: float | None = None,
    max_drawdown: float | None = None,
) -> dict:
    """生效阈值（命令行给了就用命令行）。"""
    return {
        "max_single_weight": config.RISK_MAX_SINGLE_WEIGHT if max_single is None else float(max_single),
        "max_industry_weight": config.RISK_MAX_INDUSTRY_WEIGHT if max_industry is None else float(max_industry),
        "max_avg_corr": config.RISK_MAX_AVG_CORR if max_avg_corr is None else float(max_avg_corr),
        "max_drawdown": config.RISK_MAX_CURRENT_DRAWDOWN if max_drawdown is None else float(max_drawdown),
    }


def current_drawdown(conn: sqlite3.Connection, *,
                     account_id: int | None = None,
                     date: str | None = None) -> float | None:
    """**当前**回撤幅度（0.2 = 距历史峰值跌了 20%）。没有曲线时 None。"""
    if account_id is None:
        return None
    curve = sim.nav_curve(conn, account_id=int(account_id), date=date)
    if not curve:
        return None
    peak = max(float(r["total"]) for r in curve)
    if peak <= 0:
        return None
    return max(0.0, 1.0 - float(curve[-1]["total"]) / peak)


def evaluate(conn: sqlite3.Connection, **over) -> list[dict]:
    """按阈值检查某个模拟账户，返回触发的告警（**不推送、不落库**）。

    账户为空时返回空列表：没有持仓就没有组合风险可谈。
    `account_id` 必须显式给出（没有「默认账户」这回事）。
    """
    account_id = over.pop("account_id", None)
    date = over.pop("date", None)
    if account_id is None:
        return []
    t = thresholds(**over)
    review = sim.review(conn, account_id=int(account_id), date=date)
    risk = review.get("risk") or {}
    if not risk:
        return []

    out: list[dict] = []

    def add(kind: str, value: float, limit: float, detail: str) -> None:
        out.append({"kind": kind, "name": KINDS[kind], "value": round(float(value), 4),
                    "limit": round(float(limit), 4), "detail": detail})

    top = risk.get("top_weight") or {}
    if top.get("weight", 0) > t["max_single_weight"]:
        add("single_weight", top["weight"], t["max_single_weight"],
            f"单票 {top['symbol']} 占 {top['weight']:.2%} > 上限 {t['max_single_weight']:.0%}")

    ind = risk.get("top_industry") or {}
    if ind.get("weight", 0) > t["max_industry_weight"]:
        add("industry_weight", ind["weight"], t["max_industry_weight"],
            f"行业 {ind['industry']} 占 {ind['weight']:.2%} > 上限 {t['max_industry_weight']:.0%}")

    avg = risk.get("corr_avg")
    if avg is not None and avg > t["max_avg_corr"]:
        worst = risk.get("corr_max") or {}
        add("correlation", avg, t["max_avg_corr"],
            f"平均两两相关 {avg:.2f} > 上限 {t['max_avg_corr']:.2f}"
            + (f"（最像的一对 {worst.get('a')}·{worst.get('b')} = {worst.get('value')}）"
               if worst else ""))

    dd = current_drawdown(conn, account_id=account_id, date=date)
    if dd is not None and dd > t["max_drawdown"]:
        add("drawdown", dd, t["max_drawdown"],
            f"当前回撤 {dd:.2%} > 上限 {t['max_drawdown']:.0%}（距历史峰值）")

    return out


def format_alerts(alerts: list[dict], t: dict) -> tuple[str, str]:
    """(title, content)：Markdown 汇总，供推送与 `--dry-run` 复用。"""
    if not alerts:
        return ("模拟盘风险体检：无告警", "所有指标都在阈值内。")
    title = f"模拟盘风险告警 {len(alerts)} 条"
    lines = [f"当前持仓触发 **{len(alerts)}** 条风险阈值："]
    lines.append("| 项 | 当前 | 上限 | 说明 |")
    lines.append("| --- | --- | --- | --- |")
    for a in alerts:
        lines.append(f"| {a['name']} | {a['value']:.2%} | {a['limit']:.2%} | {a['detail']} |")
    lines.append("\n阈值：" + "、".join(f"{KINDS[k]} ≤ {v:.2%}" for k, v in (
        ("single_weight", t["max_single_weight"]),
        ("industry_weight", t["max_industry_weight"]),
        ("correlation", t["max_avg_corr"]),
        ("drawdown", t["max_drawdown"]),
    )))
    lines.append("\n（来自 RpsDashboard 模拟盘；`cli trade risk` 可随时复查）")
    return title, "\n".join(lines)
