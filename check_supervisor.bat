@echo off
REM ============================================================
REM  check_supervisor.bat  (ASCII-safe)
REM  Quick status check for the all-in-one StockResearchSupervisor.
REM ============================================================

set "WORKDIR=%~dp0"
if "%WORKDIR:~-1%"=="\" set "WORKDIR=%WORKDIR:~0,-1%"

echo ============================================================
echo  StockResearchSupervisor status check
echo  Work dir: %WORKDIR%
echo ============================================================
echo.

echo [1/4] Task Scheduler registration
schtasks /query /tn "StockResearchSupervisor" /fo LIST 2>nul
if errorlevel 1 (
  echo   [X] Not registered. Run start_supervisor_visible.bat (or reboot).
) else (
  echo   [OK] Registered
)
echo.

echo [2/5] Supervisor process running?
set "SUPPID="
if exist "%WORKDIR%\supervisor.lock" set /p SUPPID=<"%WORKDIR%\supervisor.lock"
if not defined SUPPID goto sup_nolock
tasklist /fi "PID eq %SUPPID%" 2>nul | findstr /i "python" >nul
if errorlevel 1 goto sup_dead
echo   [OK] running (PID %SUPPID%)
goto sup_done
:sup_dead
echo   [X] Not running (lock PID %SUPPID% is dead). Start: schtasks /run /tn "StockResearchSupervisor"
goto sup_done
:sup_nolock
echo   [X] Not running (no supervisor.lock). Start: schtasks /run /tn "StockResearchSupervisor"
:sup_done
echo.

echo [3/5] Heartbeat freshness (alive-but-frozen check)
if exist "%WORKDIR%\supervisor_heartbeat.txt" (
  powershell -NoProfile -Command "$a=[int]((Get-Date)-(Get-Item '%WORKDIR%\supervisor_heartbeat.txt').LastWriteTime).TotalSeconds; if($a -lt 900){Write-Host ('  [OK] heartbeat '+$a+'s ago (healthy)')}else{Write-Host ('  [X] heartbeat '+$a+'s ago (>=900s = FROZEN; watchdog will restart)')}"
) else (
  echo   [X] no heartbeat file yet (supervisor not started with new code)
)
echo.

echo [4/5] Mail config (appscript_config.txt + mail_config.txt)
if exist "%WORKDIR%\appscript_config.txt" (
  echo   [OK] appscript_config.txt exists
) else (
  echo   [X] appscript_config.txt missing - mail will NOT send. See SETUP.md.
)
if exist "%WORKDIR%\mail_config.txt" (
  echo   [OK] mail_config.txt exists
) else (
  echo   [X] mail_config.txt missing
)
echo.

echo [5/5] Recent log (logs\supervisor.log, last 20 lines)
if exist "%WORKDIR%\logs\supervisor.log" (
  powershell -NoProfile -Command "Get-Content '%WORKDIR%\logs\supervisor.log' -Tail 20"
) else (
  echo   (no log yet - supervisor has not run)
)
echo.

echo ============================================================
echo  Run now : schtasks /run /tn "StockResearchSupervisor"
echo  Stop    : schtasks /end /tn "StockResearchSupervisor"
echo  Delete  : schtasks /delete /tn "StockResearchSupervisor" /f
echo  Manual  : python supervisor.py
echo ============================================================
pause
