"""推送的真实 HTTP 路径测试：用本地临时服务当 ShowDoc 推送服务。

不联网、不需要真 token，但走完整的 「form 编码 → HTTP → 解析 error_code」链路，
并把「凭据不回显」在**真的发送过请求**的前提下断言（而不只是看异常消息）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from rpsboard import config, notify

TOKEN = "FAKE-TOKEN-abc123"


class _Handler(BaseHTTPRequestHandler):
    """记录最后一次 POST 的表单字段，按 handler 上配置的 error_code 回应。"""

    seen: list[dict] = []
    error_code = 0
    error_message = "ok"

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        type(self).seen.append({
            "path": self.path,
            "form": dict(urllib.parse.parse_qsl(raw)),
        })
        body = json.dumps({"error_code": type(self).error_code,
                           "error_message": type(self).error_message},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):        # 静音，避免污染测试输出
        return


@pytest.fixture()
def fake_push(monkeypatch):
    _Handler.seen = []
    _Handler.error_code, _Handler.error_message = 0, "ok"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/server/api/push/{TOKEN}"
    monkeypatch.setattr(config, "SHOWDOC_PUSH_URL", url)
    monkeypatch.setattr(config, "SHOWDOC_PUSH_TIMEOUT", 5.0)
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


def test_push_sends_form_encoded_title_and_content(fake_push):
    hits = [{"date": "2026-01-05", "symbol": "600519", "rule_id": 1,
             "rule_name": "RPS90", "detail": "RPS60=95.0", "name": "贵州茅台"}]
    title, content = notify.push_hits("2026-01-05", hits)

    assert len(_Handler.seen) == 1
    sent = _Handler.seen[0]["form"]
    assert sent["title"] == title == "RPS 预警命中 1 条（2026-01-05）"
    assert sent["content"] == content
    assert "贵州茅台" in sent["content"] and "RPS60=95.0" in sent["content"]


def test_push_reports_showdoc_error_code(fake_push):
    _Handler.error_code, _Handler.error_message = 10103, "url或token不正确"
    with pytest.raises(notify.NotifyError) as exc:
        notify.push("标题", "正文")
    message = str(exc.value)
    assert "10103" in message and "url或token不正确" in message
    assert TOKEN not in message


def _env(**overrides) -> dict:
    """子进程环境：继承当前环境（PATH / PYTHONUTF8 / SystemRoot 都要），再覆盖几个键。

    只传 PATH 会让子进程用中文 Windows 的 GBK 输出，父进程按 utf-8 解码就炸。
    """
    import os

    env = {**os.environ, **{k: str(v) for k, v in overrides.items()}}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def test_cli_notify_test_never_echoes_credentials(fake_push, tmp_path):
    """真的发出去一条测试消息，且 stdout/stderr 里都不出现 token。"""
    env = _env(RPSBOARD_DB_PATH=tmp_path / "notify.db", SHOWDOC_PUSH_URL=fake_push)
    r = subprocess.run(
        [sys.executable, "-m", "rpsboard.cli", "notify", "test"],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(Path(__file__).resolve().parents[2]), env=env,
    )
    assert r.returncode == 0, r.stderr
    assert TOKEN not in r.stdout + r.stderr
    assert "已发送" in r.stdout
    assert len(_Handler.seen) == 1
    assert "RpsDashboard 推送测试" in _Handler.seen[0]["form"]["title"]


def test_cli_alert_push_dry_run_sends_nothing(fake_push, tmp_path):
    """--dry-run 只打印，不发送；有命中时才打印 Markdown 表格。"""
    db = tmp_path / "alertpush.db"
    import sqlite3

    from rpsboard import db as dbmod

    conn = sqlite3.connect(str(db))
    conn.executescript(dbmod.SCHEMA)
    conn.execute("INSERT INTO security_master(symbol, name, exchange, is_st) "
                 "VALUES ('600519', '贵州茅台', 'sh', 0)")
    conn.execute("INSERT INTO alert_rule(id, name, kind, params, enabled, created_at) "
                 "VALUES (1, 'RPS90', 'rps_threshold', '{}', 1, '2026-01-05')")
    conn.execute("INSERT INTO alert_hit(rule_id, date, symbol, detail) "
                 "VALUES (1, '2026-01-05', '600519', 'RPS60=95.0')")
    conn.commit()
    conn.close()

    env = _env(RPSBOARD_DB_PATH=db, SHOWDOC_PUSH_URL=fake_push)
    r = subprocess.run(
        [sys.executable, "-m", "rpsboard.cli", "alert", "push", "--dry-run"],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(Path(__file__).resolve().parents[2]), env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "RPS 预警命中 1 条（2026-01-05）" in r.stdout
    assert "贵州茅台" in r.stdout
    assert TOKEN not in r.stdout + r.stderr
    assert _Handler.seen == [], "--dry-run 不应发送任何请求"
