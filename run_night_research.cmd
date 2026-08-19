@echo off
setlocal enabledelayedexpansion
REM =====================================================================
REM  전야 리서치(23:00) — Claude Code CLI 헤드리스 실행
REM
REM  왜 코워크가 아니라 CLI 인가
REM    · 23시는 사람이 없다. 코워크 예정작업이 피크타임에 밀리거나 실패해도
REM      아무도 재시도하지 못한다. CLI 는 일정이 이 PC 에 있고 재시도도 여기서 한다.
REM    · 코워크 일정은 계정에 묶여 있다 — 2026-08-13~18 에 계정 전환으로 통째로
REM      사라져 3거래일 미발행이 났고, 24회차 회고 때야 발견됐다(#A45).
REM    · 지시문이 git 추적 파일(prompts\night_research.md)이라 사본이 낡지 않는다
REM      (2026-07-29 에 낡은 SKILL.md 로 6단계가 통째로 누락된 사고의 재발 방지).
REM
REM  수동 실행:  run_night_research.cmd
REM  로그:       logs\night_cli_YYYYMMDD.log  (실행마다 append)
REM  상태:       night_cli_status.json        (아침 작업이 읽고 실패를 알린다)
REM =====================================================================

cd /d "%~dp0"

set "PROMPT=%~dp0prompts\night_research.md"
REM 시험용 프롬프트 교체(실메일 발송 없이 배선만 점검할 때):
REM   set NIGHT_PROMPT=...\smoke.md  ^&  run_night_research.cmd haiku
if not "%NIGHT_PROMPT%"=="" set "PROMPT=%NIGHT_PROMPT%"
set "WEBSITE=C:\Users\USER\Desktop\stock_website"

REM 모델: 작업 스케줄러 인자로 바꿀 수 있다.  예) run_night_research.cmd sonnet
set "MODEL=%~1"
if "%MODEL%"=="" set "MODEL=opus"

for /f "tokens=2 delims==" %%d in ('wmic os get LocalDateTime /value 2^>nul ^| find "="') do set "DT=%%d"
set "TODAY=%DT:~0,8%"
set "STAMP=%DT:~0,4%-%DT:~4,2%-%DT:~6,2% %DT:~8,2%:%DT:~10,2%:%DT:~12,2%"
set "LOG=%~dp0logs\night_cli_%TODAY%.log"

if not exist "%~dp0logs" mkdir "%~dp0logs"
if not exist "%PROMPT%" (
    echo [%STAMP%] FATAL 프롬프트 파일 없음: %PROMPT% >> "%LOG%"
    exit /b 9
)

echo. >> "%LOG%"
echo ============================================================ >> "%LOG%"
echo [%STAMP%] 전야 리서치 시작 ^(model=%MODEL%^) >> "%LOG%"
echo ============================================================ >> "%LOG%"

REM --- 실행 --------------------------------------------------------------
REM  --permission-mode acceptEdits + --allowedTools : 무인이라 승인 프롬프트가
REM    뜨면 그대로 멈춘다. 필요한 도구만 미리 열어둔다(bypassPermissions 는 쓰지 않는다).
REM  --add-dir : publish_report.py 가 있는 웹사이트 폴더 접근 허용.
REM  프롬프트는 stdin 으로 넘긴다(따옴표·길이 문제 회피).
type "%PROMPT%" | claude -p ^
    --model %MODEL% ^
    --permission-mode acceptEdits ^
    --add-dir "%WEBSITE%" ^
    --allowedTools "Bash(python *) Read Write Edit Glob Grep WebSearch WebFetch" ^
    >> "%LOG%" 2>&1

set "RC=%ERRORLEVEL%"

for /f "tokens=2 delims==" %%d in ('wmic os get LocalDateTime /value 2^>nul ^| find "="') do set "DT2=%%d"
set "END=%DT2:~0,4%-%DT2:~4,2%-%DT2:~6,2% %DT2:~8,2%:%DT2:~10,2%:%DT2:~12,2%"

echo. >> "%LOG%"
echo [%END%] 종료코드=%RC% >> "%LOG%"

REM --- 상태 파일 (아침 작업이 읽는다) -----------------------------------
python "%~dp0night_cli_status.py" --rc %RC% --log "%LOG%" --model "%MODEL%" >> "%LOG%" 2>&1

if not "%RC%"=="0" (
    echo [%END%] ★실패 — 로그를 확인하라: %LOG% >> "%LOG%"
    exit /b %RC%
)
exit /b 0
