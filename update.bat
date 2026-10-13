@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   A股 RPS 看板 - 一键更新数据
echo   流程: 拉取日K -> 采集涨跌停 -> 重算 RPS
echo   预计 4~5 分钟 (日K 约209s + 涨跌停约35s + RPS 约10s)
echo ============================================================
echo.

where uv >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 uv，请先安装： https://docs.astral.sh/uv/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [错误] 未找到虚拟环境，请先运行 start.bat 完成初始化
  pause
  exit /b 1
)

echo [1/2] 更新数据...
".venv\Scripts\python.exe" -m rpsboard.cli update
if errorlevel 1 (
  echo.
  echo [失败] 更新过程出错，请检查上面的错误信息
  pause
  exit /b 1
)

echo.
echo [2/2] 当前状态:
".venv\Scripts\python.exe" -m rpsboard.cli status

echo.
echo 完成。打开 http://127.0.0.1:8000/ 查看。
pause
