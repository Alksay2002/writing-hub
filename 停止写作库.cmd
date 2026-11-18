@echo off
setlocal
cd /d "%~dp0"
echo Stopping WritingHub...

rem ---- try PID file first ----
if exist "%~dp0.pid" (
  set /a PID= < "%~dp0.pid"
  if defined PID (
    taskkill /PID %PID% /F >nul 2>nul
    del "%~dp0.pid" >nul 2>nul
    echo Sent kill to PID %PID%
  )
)

rem ---- fallback: kill windowed exe by process name (version-agnostic) ----
powershell -NoProfile -Command "Get-Process | Where-Object { $_.ProcessName -like '写作库_*' } | Stop-Process -Force" >nul 2>nul

rem ---- fallback: find any process holding port 8765 ----
timeout /t 1 >nul
netstat -ano | findstr /R /C:":8765 .*LISTENING" >nul
if not errorlevel 1 (
  for /f "tokens=5" %%a in ('netstat -ano ^| findstr /R /C:":8765 .*LISTENING"') do (
    echo Killing port-8765 holder PID %%a
    taskkill /PID %%a /F >nul 2>nul
  )
)

timeout /t 1 >nul
netstat -ano | findstr /R /C:":8765 .*LISTENING" >nul
if errorlevel 1 (
  echo WritingHub stopped.
) else (
  echo Port 8765 still busy — run as Administrator to kill, or restart Windows.
)
pause
