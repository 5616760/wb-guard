@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PYW=%USERPROFILE%\.workbuddy\binaries\python\versions\3.14.3\pythonw.exe"
set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.14.3\python.exe"

rem Panel auto-starts monitoring since v3.4 - no need to click "start".
rem `start` hands the process to the shell so it survives this window closing.

if exist "%PYW%" (
    start "WorkBuddyGuard" /min "%PYW%" wb_guard.py gui
    goto started
)
if exist "%PY%" (
    start "WorkBuddyGuard" /min "%PY%" wb_guard.py gui
    goto started
)

echo [ERROR] Python not found. Make sure WorkBuddy is installed properly.
pause
exit /b 1

:started
echo Panel launched in background and monitoring has started.
echo Close this window freely - the guard keeps running.
echo To stop: run 停止守护.bat, or click "停止" in the panel.
timeout /t 3 >nul
exit /b 0
