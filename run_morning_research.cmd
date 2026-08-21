@echo off
setlocal
REM =====================================================================
REM  Morning research - Claude Code CLI headless runner
REM
REM  ASCII ONLY. Do not put Korean text in this file.
REM    Task Scheduler runs cmd in the system OEM codepage (cp949 here) while
REM    this file is saved as UTF-8. Korean bytes then decode to garbage and
REM    the parser breaks mid-script - measured 2026-08-19 on the night runner:
REM    direct runs worked, scheduler runs died right after the banner rc=255.
REM    All human-facing Korean lives in prompts\morning_research.md (read by
REM    claude as UTF-8, never parsed by cmd).
REM
REM  Why CLI instead of Cowork: the morning job kept not running.
REM    2026-08-13..18 three trading days missing (#A45, found only by the
REM    24th retro on 08-19), 08-20 missing (retro held the slot 03:15-07:20),
REM    08-21 missing (retro and night both fine, morning alone did not run).
REM    Night (23:00) and retro (03:30) have been stable since moving to the
REM    scheduler, so morning follows the same path.
REM
REM  Usage:   run_morning_research.cmd [model]     default model: opus
REM  Runtime: 40-80 min (signals alone are 10-20 min). Task limit is 2h.
REM  Log:     logs\morning_cli_YYYYMMDD.log
REM  Status:  morning_cli_status.json
REM =====================================================================

cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

set "PROMPT=%~dp0prompts\morning_research.md"
REM Test hook: point MORNING_PROMPT at a smoke file to exercise wiring without sending mail.
if not "%MORNING_PROMPT%"=="" set "PROMPT=%MORNING_PROMPT%"

set "MODEL=%~1"
if "%MODEL%"=="" set "MODEL=opus"

REM Timestamp without wmic (deprecated on newer Windows).
for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%t"
set "TODAY=%TS:~0,8%"
set "STAMP=%TS:~0,4%-%TS:~4,2%-%TS:~6,2% %TS:~9,2%:%TS:~11,2%:%TS:~13,2%"
set "LOG=%~dp0logs\morning_cli_%TODAY%.log"

if not exist "%~dp0logs" mkdir "%~dp0logs"
if not exist "%PROMPT%" (
    echo [%STAMP%] FATAL prompt file missing: %PROMPT%>>"%LOG%"
    exit /b 9
)

echo.>>"%LOG%"
echo ============================================================>>"%LOG%"
echo [%STAMP%] morning research start ^(model=%MODEL%^)>>"%LOG%"
echo ============================================================>>"%LOG%"
echo [diag] cwd=%CD%>>"%LOG%"
echo [diag] prompt=%PROMPT%>>"%LOG%"
where claude>>"%LOG%" 2>&1
if errorlevel 1 echo [diag] WARNING claude not found on PATH - check Task Scheduler environment>>"%LOG%"

REM  acceptEdits + allowedTools: unattended, so a permission prompt would hang.
REM    Open only what is needed; Bash is limited to python so no arbitrary shell.
REM    bypassPermissions is deliberately NOT used.
REM  add-dir website: publish_report.py lives there (step 8).
REM  strict-mcp-config: skip .mcp.json - not needed here, adds startup cost.
REM  Prompt goes through stdin to dodge quoting and length limits.
set "WEBSITE=C:\Users\USER\Desktop\stock_website"
echo [claude call start]>>"%LOG%"
type "%PROMPT%" | claude -p --model %MODEL% --permission-mode acceptEdits --strict-mcp-config --add-dir "%WEBSITE%" --allowedTools "Bash(python *) Read Write Edit Glob Grep WebSearch WebFetch">>"%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [claude call end rc=%RC%]>>"%LOG%"

for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format \"yyyy-MM-dd HH:mm:ss\""') do set "END=%%t"
echo [%END%] exit=%RC%>>"%LOG%"

python "%~dp0morning_cli_status.py" --rc %RC% --log "%LOG%" --model "%MODEL%">>"%LOG%" 2>&1

if not "%RC%"=="0" exit /b %RC%
exit /b 0
