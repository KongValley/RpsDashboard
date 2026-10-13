"""预警推送到手机（ShowDoc 推送服务）。

机制（官方文档 https://www.showdoc.com.cn/push）：ShowDoc 旗下的独立推送工具，
每个账号有一个专属地址 `https://push.showdoc.com.cn/server/api/push/<token>`，
POST/GET 传 `title` / `content`（支持 Markdown），成功后返回
`{"error_code": 0, "error_message": "ok"}`，消息进微信。免费、不限请求数。

约定（与项目其它部分一致）：
- **只手动触发**：`cli alert push` / `cli notify test`。`cli update` 不自动推送。
- 地址含 token，属凭据：只从 `SHOWDOC_PUSH_URL`（`.env.local` 或环境变量）读，
  **代码与输出都不回显**（失败时只说错误码/原因，不打印 URL）。
- 推送失败不影响任何数据链路：调用方按返回值决定退出码，不抛到采集流程里。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from . import config

#: 一条汇总消息里最多列多少条明细（533 条命中时全列出来没人看）
MAX_DETAIL = 30


class NotifyError(RuntimeError):
    """推送失败（未配置 / 网络错误 / ShowDoc 返回非 0 错误码）。"""


def configured() -> bool:
    return bool(config.SHOWDOC_PUSH_URL)


def format_summary(date: str, hits: list[dict], max_detail: int = MAX_DETAIL) -> tuple[str, str]:
    """把命中记录格式化成 (title, content)。

    content 是 Markdown：按规则分组的小标题 + 明细表格，超过 max_detail 只列前若干条。
    """
    n = len(hits)
    by_rule: dict[str, list[dict]] = {}
    for h in hits:
        by_rule.setdefault(h.get("rule_name") or f"规则{h.get('rule_id')}", []).append(h)

    title = f"RPS 预警命中 {n} 条（{date}）"
    lines = [f"**{date}** 条件预警命中 **{n}** 条："]
    for name, group in by_rule.items():
        lines.append(f"\n**{name}**（{len(group)} 条）")
        shown = group[:max_detail]
        lines.append("| 代码 | 名称 | 说明 |")
        lines.append("| --- | --- | --- |")
        for h in shown:
            lines.append(f"| {h.get('symbol', '')} | {h.get('name') or '—'} "
                         f"| {h.get('detail') or ''} |")
        if len(group) > len(shown):
            lines.append(f"| … | 还有 {len(group) - len(shown)} 条 | |")
    lines.append("\n（来自 RpsDashboard 条件预警；完整命中见页面「自选预警」或 `cli alert ls`）")
    return title, "\n".join(lines)


def push(title: str, content: str) -> None:
    """POST 到专属推送地址。成功返回 None，失败抛 NotifyError（消息里不含 URL）。"""
    url = config.SHOWDOC_PUSH_URL
    if not url:
        raise NotifyError(
            "未配置 SHOWDOC_PUSH_URL：把 ShowDoc 推送服务的专属地址写进仓库根 "
            "`.env.local`（gitignored）或设为环境变量"
        )
    body = urllib.parse.urlencode({"title": title, "content": content}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")   # 不打日志，避免泄漏 token
    try:
        with urllib.request.urlopen(req, timeout=config.SHOWDOC_PUSH_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raise NotifyError(f"推送失败：HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NotifyError(f"推送失败：{type(exc).__name__} {exc}") from None

    try:
        data = json.loads(raw)
    except ValueError:
        # 非 JSON 响应（例如被网关改写）：HTTP 200 且正文含 ok 视为成功
        if status == 200 and "ok" in raw.lower():
            return
        raise NotifyError(f"推送失败：响应不是 JSON（HTTP {status}）") from None

    code = data.get("error_code")
    if code == 0:
        return
    raise NotifyError(f"推送失败：error_code={code} {data.get('error_message') or ''}".strip())


def push_hits(date: str, hits: list[dict]) -> tuple[str, str]:
    """格式化并推送一组命中；返回推送出去的内容（便于 --dry-run 与测试复用）。"""
    title, content = format_summary(date, hits)
    push(title, content)
    return title, content


def push_test() -> None:
    """发一条测试消息，用于确认凭据/网络是否可用。"""
    push("RpsDashboard 推送测试",
         "这是一条测试消息。收到即表示 `.env.local` 里的 `SHOWDOC_PUSH_URL` 可用。")
