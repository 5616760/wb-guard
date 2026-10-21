@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.14.3\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" wb_guard.py install-autostart
echo.
pause
