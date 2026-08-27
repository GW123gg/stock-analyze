@echo off
setlocal
REM =====================================================================
REM  Retro review - Claude Code CLI headless runner
REM
REM  ASCII ONLY. Do not put Korean text in this file.
REM    Task Scheduler runs cmd in the system OEM codepage (cp949 here) while
REM    this file is saved as UTF-8. Korean bytes then decode to garbage and
REM    the parser breaks mid-script - measured 2026-08-19: direct runs worked,
REM    scheduler runs died right after the banner with rc=255.
REM    All human-facing Korean lives in prompts\retro_review.md (read by
REM    claude as UTF-8, never parsed by cmd).
REM
REM  Why CLI instead of Cowork: on 2026-08-20 the retro Cowork job ran 03:15-07:20
REM    and swallowed the 06:20 morning slot - that day had no research at all.
REM    Moving retro off the Cowork queue frees the morning slot structurally.
REM
REM  Usage:   run_retro_review.cmd [model]      default model: opus
REM  Runtime: can exceed 1h when KRX blocks pykrx (retry storms). Task limit is 3h.
REM  Log:     logs\retro_cli_YYYYMMDD.log
REM  Status:  retro_cli_status.json  (morning job reads this)
REM =====================================================================

cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

set "PROMPT=%~dp0prompts\retro_review.md"
REM Test hook: point RETRO_PROMPT at a smoke file to exercise wiring without sending mail.
if not "%RETRO_PROMPT%"=="" set "PROMPT=%RETRO_PROMPT%"

set "MODEL=%~1"
if "%MODEL%"=="" set "MODEL=opus"

REM Timestamp without wmic (deprecated on newer Windows).
for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%t"
set "TODAY=%TS:~0,8%"
set "STAMP=%TS:~0,4%-%TS:~4,2%-%TS:~6,2% %TS:~9,2%:%TS:~11,2%:%TS:~13,2%"
set "LOG=%~dp0logs\retro_cli_%TODAY%.log"

if not exist "%~dp0logs" mkdir "%~dp0logs"
if not exist "%PROMPT%" (
    echo [%STAMP%] FATAL prompt file missing: %PROMPT%>>"%LOG%"
    exit /b 9
)

echo.>>"%LOG%"
echo ============================================================>>"%LOG%"
echo [%STAMP%] retro review start ^(model=%MODEL%^)>>"%LOG%"
echo ============================================================>>"%LOG%"
echo [diag] cwd=%CD%>>"%LOG%"
echo [diag] prompt=%PROMPT%>>"%LOG%"
where claude>>"%LOG%" 2>&1
if errorlevel 1 echo [diag] WARNING claude not found on PATH - check Task Scheduler environment>>"%LOG%"

REM  acceptEdits + allowedTools: unattended, so a permission prompt would hang.
REM    Open only what is needed; Bash is limited to python so no arbitrary shell.
REM    bypassPermissions is deliberately NOT used.
REM  strict-mcp-config: skip .mcp.json (apify) - not needed here, adds startup
REM    cost and noisy "listTools() ... does not advertise tools" lines.
REM  Prompt goes through stdin to dodge quoting and length limits.
echo [claude call start]>>"%LOG%"
type "%PROMPT%" | claude -p --model %MODEL% --permission-mode acceptEdits --strict-mcp-config --allowedTools "Bash(python *) Read Write Edit Glob Grep WebSearch WebFetch">>"%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [claude call end rc=%RC%]>>"%LOG%"

REM  delims= : without it, for /f splits on the space and END keeps only the date.
REM  Space before >> : `exit=0>>file` makes cmd read the 0 as a stream handle and
REM    the code never reaches the log (measured 2026-08-26 on morning; night and
REM    retro runners still had it on 2026-08-27 - night_cli_20260826.log had no
REM    exit= line at all).
for /f "delims=" %%t in ('powershell -NoProfile -Command "Get-Date -Format \"yyyy-MM-dd HH:mm:ss\""') do set "END=%%t"
echo [%END%] exit=%RC% >>"%LOG%"

python "%~dp0retro_cli_status.py" --rc %RC% --log "%LOG%" --model "%MODEL%">>"%LOG%" 2>&1

if not "%RC%"=="0" exit /b %RC%
exit /b 0
