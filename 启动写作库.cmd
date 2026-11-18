@echo off
setlocal
cd /d "%~dp0"
title WritingHub

rem ---- prefer bundled exe: windowed app, no console, opens browser itself ----
if exist "%~dp0写作库_v*.exe" (
  for %%F in ("%~dp0写作库_v*.exe") do (
    start "" "%%~fF"
    exit /b 0
  )
)

rem ---- locate Python 3 ----
set "PYEXE="
python --version >nul 2>nul && set "PYEXE=python"
if not defined PYEXE ( py -3 --version >nul 2>nul && set "PYEXE=py -3" )
if not defined PYEXE (
  echo [ERROR] Python 3 was not found in PATH.
  echo Install Python 3 and check "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

rem ---- if already running, just open the page ----
netstat -ano | findstr /R /C:":8765 .*LISTENING" >nul
if not errorlevel 1 (
  echo WritingHub is already running on port 8765.
  start "" http://127.0.0.1:8765/
  timeout /t 2 >nul
  exit /b 0
)

rem ---- find the .py file ----
set "PYFILE="
for %%F in ("%~dp0*.py") do set "PYFILE=%%F"
if not defined PYFILE (
  echo [ERROR] No .py file found next to this script.
  pause
  exit /b 1
)

rem ---- check cached PID for leftover process ----
if exist "%~dp0.pid" (
  set /a OLDPID= < "%~dp0.pid"
  if defined OLDPID (
    tasklist /FI "PID eq %OLDPID%" 2>nul | findstr "%OLDPID%" >nul
    if not errorlevel 1 (
      echo Stale PID %OLDPID% still alive, killing...
      taskkill /PID %OLDPID% /F >nul 2>nul
      timeout /t 1 >nul
    )
  )
)

rem ---- start in background, record PID ----
echo Starting WritingHub...
start "" /MIN cmd /c "echo $pid ^> "%~dp0.pid"; %PYEXE% "%PYFILE%""
timeout /t 4 >nul

rem ---- verify ----
netstat -ano | findstr /R /C:":8765 .*LISTENING" >nul
if not errorlevel 1 (
  echo Service is up on http://127.0.0.1:8765/
  start "" http://127.0.0.1:8765/
  echo.
  echo Close this window or run stop_writing_hub.cmd to shut down.
) else (
  echo [WARN] Service did not come up on port 8765. Check the Python error above.
  del "%~dp0.pid" 2>nul
)
timeout /t 6 >nul
exit /b 0
