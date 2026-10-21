@echo off
chcp 65001 >nul
title WorkBuddy 断流守护（只观察，不发送）
cd /d "%~dp0"

set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.14.3\python.exe"
if not exist "%PY%" set "PY=python"

echo ============================================================
echo  WorkBuddy 断流守护 - 只观察模式
echo  会检测中断并打印，但绝不发送任何消息，也不切换会话。
echo  确认判定符合预期后，改用「启动监控.bat」。
echo ============================================================
echo.

"%PY%" wb_guard.py watch --dry-run
echo.
echo 已退出只观察模式。按任意键关闭窗口。
pause >nul
