@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\pythonw.exe" (
    echo [7u] Missing the local Python environment.
    echo Run "uv sync --locked" in the repository root, then open this file again.
    pause
    exit /b 1
)
start "7u PC maintenance" ".venv\Scripts\pythonw.exe" -m maintenance.app
