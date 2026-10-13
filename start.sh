#!/usr/bin/env bash
# A股 RPS 看板 - 本地启动（Linux / macOS）。Windows 用 start.bat。
set -euo pipefail
cd "$(dirname "$0")"

echo "============================================================"
echo "  A股 RPS 看板 - 本地启动"
echo "============================================================"
echo

command -v uv >/dev/null || {
  echo "[错误] 未找到 uv，请先安装： https://docs.astral.sh/uv/"
  exit 1
}

if [ ! -x .venv/bin/python ]; then
  echo "[1/4] 创建 Python 环境并安装依赖..."
  uv venv --python 3.12
  uv pip install -e ".[dev]"
else
  echo "[1/4] Python 环境已就绪"
fi

echo "[2/4] 检查数据库..."
.venv/bin/python -m rpsboard.cli status

echo
echo "[3/4] 启动后端 (uvicorn :8000)..."
.venv/bin/python -m uvicorn rpsboard.api:app --app-dir backend --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!
trap 'kill $BACKEND_PID 2>/dev/null || true' EXIT INT TERM

echo "[4/4] 启动前端 (vite :5173)..."
cd frontend
if [ ! -d node_modules ]; then
  echo "      首次运行，安装前端依赖..."
  npm install
fi

echo
echo "前端地址: http://localhost:5173"
echo "后端文档: http://localhost:8000/docs"
echo
npm run dev
