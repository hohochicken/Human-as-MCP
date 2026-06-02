@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   Human-as-MCP Server
echo ============================================
echo.
echo Installing dependencies...
pip install -r requirements.txt -q
echo.
echo Starting server...
echo   Dashboard: http://localhost:4350/dashboard
echo   MCP:       http://localhost:4350/mcp
echo.
echo Press Ctrl+C to stop.
echo ============================================
echo.

python server/main.py
pause
