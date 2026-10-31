@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo 未找到 python 命令，请先安装 Python 3.10 或更高版本并加入 PATH。
    pause
    exit /b 1
)

python gui\server.py %*
if errorlevel 1 pause
