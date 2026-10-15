"""推送（ShowDoc 推送服务）的离线测试：格式化、错误处理、凭据不回显。

全部离线：真正发请求的用例打桩到本地临时 HTTP 服务（见 test_notify_http.py），
这里只碰纯函数与异常路径。
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from rpsboard import config, notify


def _hit(symbol, rule="RPS90", detail="RPS60=95.0", name=None, date="2026-01-05"):
    return {"date": date, "symbol": symbol, "rule_id": 1, "rule_name": rule,
            "detail": detail, "name": name}


def test_push_requires_configuration(monkeypatch):
    """未配置推送地址时必须给出可执行的指引，而不是网络错误。"""
    monkeypatch.setattr(config, "SHOWDOC_PUSH_URL", "")
    assert notify.configured() is False
    with pytest.raises(notify.NotifyError) as exc:
        notify.push("t", "c")
    assert "SHOWDOC_PUSH_URL" in str(exc.value)
    assert ".env.local" in str(exc.value)


def test_whole_suite_cannot_reach_the_real_push_channel():
    """全局守卫（conftest._no_real_push）：本机上配了真地址，跑测试也发不出去。

    没有这道守卫时，断线用例会走 `_notify_disconnect()` 把测试消息真的推到开发者
    微信上（2026-10-15 实测），而且只有手工去 `.env.local` 删掉才停得下来。
    """
    assert notify.configured() is False
    with pytest.raises(notify.NotifyError):
        notify.push("t", "c")


def test_format_summary_lists_details_and_truncates():
    """超过上限的明细要折叠成「还有 N 条」，标题带条数与日期。"""
    hits = [_hit(f"{i:06d}") for i in range(notify.MAX_DETAIL + 5)]
    title, content = notify.format_summary("2026-01-05", hits)
    assert title == f"RPS 预警命中 {notify.MAX_DETAIL + 5} 条（2026-01-05）"
    assert "| 代码 | 名称 | 说明 |" in content
    assert content.count("| 000000 |") == 1
    assert "还有 5 条" in content
    assert "RPS90" in content


def test_format_summary_writes_dash_for_missing_name():
    title, content = notify.format_summary("2026-01-05", [_hit("600519", name=None)])
    assert "| 600519 | — | RPS60=95.0 |" in content
    assert "2026-01-05" in title


def test_push_error_message_never_contains_token(monkeypatch):
    """失败信息里不能出现专属地址（token 就在 URL 里）。"""
    monkeypatch.setattr(config, "SHOWDOC_PUSH_URL",
                        "https://push.showdoc.com.cn/server/api/push/SECRET-TOKEN-xyz")

    class _Fail:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"error_code": 10103, "error_message": "url或token不正确"},
                              ensure_ascii=False).encode("utf-8")

    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda *a, **k: _Fail())
    with pytest.raises(notify.NotifyError) as exc:
        notify.push("标题", "正文")
    assert "10103" in str(exc.value)
    assert "SECRET-TOKEN-xyz" not in str(exc.value)


def test_env_file_is_loaded_without_overriding(monkeypatch, tmp_path):
    """`.env.local` 提供凭据；已存在的环境变量优先（CI/命令行的注入不被文件盖掉）。"""
    from rpsboard import config as cfg

    monkeypatch.delenv("SHOWDOC_PUSH_URL", raising=False)
    tmp_root = tmp_path / "repo"
    tmp_root.mkdir()
    (tmp_root / ".env.local").write_text(
        "# 注释行\nSHOWDOC_PUSH_URL=https://example.invalid/push/from-file\n"
        "SHOWDOC_PUSH_TIMEOUT=7\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg, "ROOT_DIR", tmp_root)
    cfg._load_env_file()
    import os
    assert os.environ["SHOWDOC_PUSH_URL"] == "https://example.invalid/push/from-file"
    assert os.environ["SHOWDOC_PUSH_TIMEOUT"] == "7"

    # 已有环境变量优先
    monkeypatch.setenv("SHOWDOC_PUSH_URL", "https://example.invalid/push/from-env")
    cfg._load_env_file()
    assert os.environ["SHOWDOC_PUSH_URL"] == "https://example.invalid/push/from-env"
    os.environ.pop("SHOWDOC_PUSH_TIMEOUT", None)


def test_cli_notify_test_dry_run_does_not_need_config(tmp_path, monkeypatch):
    """`cli notify test --dry-run` 不联网、也不要求配置凭据。"""
    monkeypatch.setenv("RPSBOARD_DB_PATH", str(tmp_path / "n.db"))
    monkeypatch.delenv("SHOWDOC_PUSH_URL", raising=False)
    r = subprocess.run(
        [sys.executable, "-m", "rpsboard.cli", "notify", "test", "--dry-run"],
        capture_output=True, text=True, cwd=_repo_root(),
    )
    assert r.returncode == 0, r.stderr
    assert "推送测试" in r.stdout
    assert "未配置" not in r.stderr


def _repo_root() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parents[2])
