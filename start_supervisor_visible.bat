@echo off
REM ============================================================
REM  start_supervisor_visible.bat  (ASCII-safe)
REM  Starts the supervisor in a VISIBLE console window so you can
REM  watch what it is doing (06:30 collection, force, mail send).
REM
REM  - Uses 'python' (NOT pythonw) so the window stays visible.
REM  - 'title' names the window so watchdog can detect it.
REM  - If supervisor exits, this window stays open showing the
REM    last lines (so you can see why), until you close it.
REM
REM  This is meant to run at logon via the Startup folder, OR you
REM  can double-click it any time to bring the supervisor up.
REM ============================================================

cd /d "%~dp0"
if not exist logs mkdir logs

title StockResearch Supervisor (visible)

echo ============================================================
echo  StockResearch Supervisor  - VISIBLE MODE
echo  %DATE% %TIME%
echo  This window shows live activity. Do NOT close it unless you
echo  want to stop the supervisor. (watchdog will restart it within
echo  5 minutes if it dies, but keeping this open is best.)
echo ============================================================
echo.

REM python (visible console).
REM Do NOT pass --morning-time : supervisor.py uses its MORNING_TIME variable.
REM (To change the test time, edit MORNING_TIME in supervisor.py only.)
python supervisor.py --interval 30

echo.
echo ============================================================
echo  [!] supervisor process ENDED at %DATE% %TIME%
echo      Scroll up to see why. watchdog should relaunch it soon.
echo      You can also just re-run this file.
echo ============================================================
pause
