#!/usr/bin/env bash
# A股 RPS 看板 - 一键更新数据（Linux / macOS）。Windows 用 update.bat。
set -euo pipefail
cd "$(dirname "$0")"

echo "============================================================"
echo "  A股 RPS 看板 - 一键更新数据"
echo "  流程: 拉取日K -> 采集涨跌停 -> 重算 RPS"
echo "  预计 4~5 分钟 (日K 约209s + 涨跌停约35s + RPS 约10s)"
echo "============================================================"
echo

[ -x .venv/bin/python ] || {
  echo "[错误] 虚拟环境不存在，请先运行 ./start.sh"
  exit 1
}

echo "[1/2] 更新数据..."
.venv/bin/python -m rpsboard.cli update

echo
echo "[2/2] 当前状态:"
.venv/bin/python -m rpsboard.cli status

echo
echo "完成。打开 http://127.0.0.1:8000/ 查看。"
