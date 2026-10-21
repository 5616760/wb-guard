@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PYW=%USERPROFILE%\.workbuddy\binaries\python\versions\3.14.3\pythonw.exe"
set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.14.3\python.exe"

if exist "%PYW%" (
    start "" "%PYW%" wb_guard.py gui
) else (
    start "" "%PY%" wb_guard.py gui
)
