@echo off
chcp 65001 >nul
title WorkBuddy 断流守护
cd /d "%~dp0"

set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.14.3\python.exe"
if not exist "%PY%" set "PY=python"

echo ============================================================
echo  WorkBuddy 断流守护 - 控制台模式
echo  检测到模型中断会自动发送「继续」；关闭本窗口即停止。
echo ============================================================
echo.

"%PY%" wb_guard.py watch
echo.
echo 守护已退出。按任意键关闭窗口。
pause >nul
