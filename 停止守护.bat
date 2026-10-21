@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在停止 WorkBuddy 断流守护...
taskkill /f /im pythonw.exe /fi "WINDOWTITLE eq WorkBuddyGuard*" >nul 2>&1
for /f "tokens=2" %%p in ('tasklist /fi "imagename eq pythonw.exe" /fo csv /nh 2^>nul') do (
    taskkill /f /pid %%~p >nul 2>&1
)
echo 已停止。日志：logs\wb_guard.log
timeout /t 2 >nul
