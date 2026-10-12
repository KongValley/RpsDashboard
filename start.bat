@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   A股 RPS 看板 - 本地启动
echo ============================================================
echo.

where uv >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 uv，请先安装： https://docs.astral.sh/uv/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/4] 创建 Python 环境...
  uv venv --python 3.12
  if errorlevel 1 ( pause & exit /b 1 )
  echo [1/4] 安装依赖...
  uv pip install -e ".[dev]"
  if errorlevel 1 ( pause & exit /b 1 )
) else (
  echo [1/4] Python 环境已就绪
)

echo [2/4] 检查数据库...
".venv\Scripts\python.exe" -m rpsboard.cli status

echo.
echo [3/4] 启动后端 (uvicorn :8000)...
start "RPS Backend" cmd /k ".venv\Scripts\python.exe -m uvicorn rpsboard.api:app --app-dir backend --host 127.0.0.1 --port 8000"

timeout /t 3 >nul

echo [4/4] 启动前端 (vite :5173)...
cd frontend
if not exist "node_modules" (
  echo      首次运行，安装前端依赖...
  call npm install
)
call npm run dev

echo.
echo 前端地址: http://localhost:5173
echo 后端文档: http://localhost:8000/docs
echo.
pause
