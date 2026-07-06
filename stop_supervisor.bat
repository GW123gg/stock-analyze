@echo off
REM ============================================================
REM  stop_supervisor.bat  (ASCII-safe)
REM  On-demand scheduler calls this at each window's END to make
REM  sure the supervisor is stopped (so it does NOT run 24/7).
REM
REM  Order of kills (most reliable first):
REM   1) PID in supervisor.lock  -> taskkill /T (children too)
REM   2) visible console window (title "StockResearch Supervisor")
REM   3) fallback: any python(w) running supervisor.py  (PowerShell)
REM   4) remove the lock so the next window cold-starts clean
REM
REM  Safe to run even if nothing is running (each step no-ops).
REM ============================================================
cd /d "%~dp0"
if not exist logs mkdir logs
echo [%DATE% %TIME%] stop_supervisor called >> logs\stop_supervisor.log

REM 1) kill by lock PID (children included via /T)
set "SVPID="
if exist supervisor.lock set /p SVPID=<supervisor.lock
if defined SVPID (
  echo   killing lock PID %SVPID% >> logs\stop_supervisor.log
  taskkill /PID %SVPID% /T /F >> logs\stop_supervisor.log 2>&1
)

REM 2) close the visible supervisor console (if watchdog launched it visibly)
taskkill /FI "WINDOWTITLE eq StockResearch Supervisor*" /T /F >> logs\stop_supervisor.log 2>&1

REM 3) fallback: kill any lingering python(w) running supervisor.py
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and $_.CommandLine -like '*supervisor.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >> logs\stop_supervisor.log 2>&1

REM 4) clean the lock so the next window's watchdog cold-starts a fresh supervisor
if exist supervisor.lock del /q supervisor.lock
echo [%DATE% %TIME%] stop done >> logs\stop_supervisor.log
