# stock_research_mcp — 코워크 터미널-MCP 판 (안전 복사본)

기존 `stock_research` 의 코워크 지시사항을 **터미널 MCP**로 동작하도록 바꾼 **복사본**입니다.
원본(`stock_research\cowork_instructions.md`, `회고분석_지시사항.md`)은 **건드리지 않았습니다** — 여기서 자유롭게 편집하세요.

## ★ 온디맨드 전환 (2026-07-06) — 데몬 끄고 아무 때나 실행
이제 **supervisor 데몬(스케줄 자동실행) 대신 이 MCP-코워크가 온디맨드로 전 과정**을 돈다. 6:30/6:45 고정이 아니라 **원할 때 실행하면 그 자리에서** collect→신호12종→deep→리포트→(발송)까지 수행한다(임의 시각 차단 하드코딩 없음 — 검증됨).

**아침 분석 실행법**: Claude Desktop 코워크에 `코워크_통합지시_최종.md` **PART A** 를 물리고 "오늘 리서치 돌려줘"라고 한다.
절차는 `python run_signals.py --stage early/main` 한 줄씩이다(개별 수집기를 베껴 실행하지 않는다). 아래 구판 요약은 이력용:
`0)accuracy_tracker(scorecard갱신) → 1)collect → 신호 13종(force/market/fsc/flow/overheat/deriv/ecos/dart/disclosure/mirae/short/★market_caution) → 2)1차분석·commands.txt → 3)deep → 4)리포트+predictions.json → 5)mail(완성HTML) → 6)recommend_track → 7)archive`.
**회고 실행법**: 코워크를 `review_instructions_mcp.md` 로 열면 retro_label→push→gen_scorecard→회고리포트→`retro_feedback.md` 갱신까지 데몬 없이 폐루프.

**데몬 끄기(관리자 PowerShell 1회)** — 자동 재기동을 막는다(스케줄 태스크는 admin 필요, 이 세션에선 권한부족으로 사용자 실행):
```powershell
Disable-ScheduledTask -TaskName "StockResearchWatchdog"
Disable-ScheduledTask -TaskName "StockResearchStop"
# StockResearchSupervisor 는 이미 Disabled
```
**실행중 프로세스 종료(수동)**: `cmd /c "C:\Users\USER\Desktop\stock_research\stop_supervisor.bat"` (supervisor.lock PID·창·잔여 python 정리). 되돌리려면 위 두 태스크를 `Enable-ScheduledTask` 로 복원.

## 파일
| 파일 | 역할 | 비고 |
|---|---|---|
| ★★`코워크_통합지시_최종.md` | **최종 통합 진입점(권장)** | PART A(리서치+회고)+PART B(모의투자+**실전 확장 훅**) 한 파일. 분석 판단은 원본 read_file 참조. **코워크엔 이걸 물려라.** |
| `코워크_온디맨드_런북.md` | PART A 요약본(리서치+회고만) | 모의투자 없이 분석만 돌릴 때. 통합본의 PART A와 동일 |
| `analyze_instructions_mcp.md` | 아침 분석 상세본(**자동 생성 사본**) | `python build_mcp_instructions.py` 가 원본 `cowork_instructions.md` 에서 재생성(머리에 원본 sha256 마커). 손으로 고치지 말 것. **참고용 폴백** |
| `review_instructions_mcp.md` | 회고 자기완결 상세본 | [MCP 모드] 헤더가 RETRO_GO/DONE·inbox/outbox 대체. **참고용 폴백** |
| `README_SETUP.md` | (이 파일) | 세팅·동작 설명 |

> **어느 걸 코워크에 물리나**: 기본은 **`코워크_통합지시_최종.md`** 하나면 된다.
> - 리서치/회고 인스턴스 → 이 파일의 **PART A**를 지시문으로.
> - 모의투자 인스턴스(별도) → 이 파일의 **PART B**를 지시문으로. (한 인스턴스에 둘 다 맡기지 말 것.)
>
> **모드**: 현재 **모의투자(paper)**. 실전(live)은 증권사 API 키(`mirae_api.txt`) + `trade_config.txt`(trade_mode=live·autotrade_enabled=1)를 갖추면 자동 활성 — 그 전까지 전부 가상(PART B [B-7]). 실전에서도 **Cowork는 실주문을 직접 내지 않고**, `auto stock` 프로젝트의 executor가 체결(DRY 기본).

## 핵심 변경 — flag → 터미널 MCP
**기존(호스트 핸드셰이크):** 호스트(supervisor)가 수집/심층/발송을 실행하고 `*.flag`로 신호 → 코워크가 30초 간격 폴링 대기.
**바뀜(MCP 자가실행):** 코워크가 **`run_command`로 직접** `research_agent.py collect/deep/mail` 을 실행하고, 긴 작업은 **`wait_job`**, 결과는 **`read_file`/`list_directory`** 로 확인. **flag·대기 없음.**

- 쓰는 MCP 도구: `run_command`, `wait_job`, `cancel_job`, `list_jobs`, `read_file`, `list_directory`.
- 실행 위치(WORK): `C:\Users\USER\Desktop\stock_research` — **스크립트·output·logs 를 공유**(복제 안 함).

## 분석 로그 '공유'
복사본은 로그를 **복제하지 않고 원본 폴더를 읽어 공유**합니다. 코워크가 참고할 위치:
- `WORK\output\_archive\` (과거 세션), `WORK\logs\` (데몬 로그), `WORK\retro_reports\` (회고)
- `WORK\scorecard.md` (정확도), `WORK\retro_feedback.md` (회고→아침 피드백 폐루프)
- 회고 inbox/outbox: `C:\Users\USER\Desktop\stock_retro\inbox\` (retro_dataset·collections)

## 세팅 (요약 — 자세한 건 채팅 답변 참고)
1. **터미널 MCP 권한**: 코워크에서 `run_command` 등을 'Allow always'로 허용(도구 단위 전역).
2. **지시사항 지정**: 아침 분석·회고 작업 모두 `코워크_통합지시_최종.md`(PART A)를 코워크에 물린다. `analyze_instructions_mcp.md`·`review_instructions_mcp.md` 는 참고용 폴백이지 진입점이 아니다(2026-09-08 정정 — 구판 안내가 낡은 사본을 물리게 했다).
3. **WORK 경로 확인**: 두 파일 상단 [MCP 모드]의 `WORK = ...stock_research` 가 네 환경과 맞는지 확인.
4. **수집 겹침 주의**: 기존 자동 파이프라인(supervisor 06:30/02:00)과 **시간이 겹치지 않게** 수동 실행(사용자 확인됨). 필요하면 supervisor 의 해당 시간대 자동수집을 끄거나, MCP-코워크 실행 시간을 분리.
5. **발송**: 자동 발송을 원치 않으면 analyze 5)번(`mail`)을 건너뛰고 리포트만 저장하도록 지시.

## 안전
- 원본 `stock_research` 의 지시·스크립트·supervisor 미수정. 이 폴더만 편집.
- `research_agent.py` 자체는 공유(원본)라, 동작 로직은 동일하고 **오케스트레이션(누가 실행하나)만** 바뀜.
- 문제가 생기면 이 폴더를 지우고 원본 핸드셰이크로 즉시 복귀 가능.

## ✅ 테스트 결과 (2026-06-30, 실제 터미널 MCP로 검증)
- **MCP run_command/wait_job/read_file/list_directory 모두 동작.** `research_agent.py --help`·`test` 정상 실행, 세션 폴더 list/read 정상.
- **★ 셸은 반드시 `shell:"cmd"`** — 이 환경의 기본 PowerShell엔 `python`/`py`가 PATH에 없어 실패. cmd 셸 + `cwd` 로만 잡힘(지시사항에 반영함).
- ~~메일 발송은 현재 불가~~ → **[해결됨 2026-07-06]** `send_email`에 **appscript** 방식 추가: `mail --session <세션> --method appscript` 가 자격증명 없이 Apps Script로 발송(실발송 검증). `--method auto`도 api→smtp→appscript 폴백.
- **Gemini 키 429(쿼터 소진)** — collect 시 Gemini 결과가 비어도 정상(Naver·폴백으로 진행). 전체 흐름엔 무관.
- 스크립트 존재 확인: research_agent.py / retro_label.py / retro_forward.py / watchdog.py.
- **★ 긴 명령 = MCP 전송 타임아웃** — `collect`(1~3분)·`deep`(수십분)·`retro_label`은 MCP 전송 한도(~60s)를 넘겨 `run_command`가 **-32001 타임아웃**을 낸다. 명령 자체는 **백그라운드로 계속 실행됨**(라이브 리허설서 collect 가 그렇게 정상 완료). → 대처: `wait_ms`를 **30초**로 짧게 줘 job_id 받고 `wait_job` 폴링, 끊기면 `list_directory`로 결과파일 생성 직접 확인(지시사항에 반영).
- **★ 풀 리허설(2026-06-30, collect→deep→report·메일없음)**: 호스트가 무시하는 `_designtest` 세션에서 **collect→commands.txt→deep→03_final_report+predictions.json 전 과정을 MCP로 실행 성공**(supervisor 정지 불필요, 호스트 무간섭). 메일·report-done 미실행.
  - **#5 신호파일 스텝 누락(중요·검증완료)**: 아침 파이프라인은 호스트의 **~16스텝**(force_analysis→force_scores, market_collect→market_context, fsc/flow/deriv/ecos/overheat/dart_collect…)이다. collect만으론 신호파일이 안 생긴다 → MCP-코워크가 **8개 신호 스크립트도 실행**해야 함(지시사항에 표로 반영). **검증: `market_collect.py`를 MCP(shell=cmd)로 풀실행 확인**(regime score·breadth·flows 수집 OK). ⚠️ 이 스크립트들은 `--session` 없이 **'오늘 날짜 최신 활성 세션'을 자동 대상**(없으면 루트로 폴백되니 **collect로 오늘 세션 만든 직후** 실행 필수). 대안: 신호는 supervisor에 맡기고 분석만.
  - **#6 경로 따옴표 금지**: cmd via MCP에서 `--session "경로"` 의 따옴표가 인자에 포함돼 오류 → **따옴표 없이**(공백 없는 경로). 지시사항·예시 수정.
  - 리허설 산출물: `output\_designtest\REHEARSAL_2026-06-30_004601\` (01_broad 614KB·02_deep·03_final_report·predictions.json, 호스트 분석 제외 폴더).

## 한계/확인 필요
- ~~retro_forward.py --push 인자 확인 필요~~ → **[확인됨]** `--push` 단독으로 동작(추가 인자 불필요), `--scan-back`으로 피드백 회수.
- `research_agent.py collect` 가 세션을 `WORK\output\` 에 만들므로 공유 output 사용 시 **기존 supervisor(06:30/02:00)와 시간분리 필수**. 완전 분리를 원하면 별도 output 경로 옵션 추가 필요(현 스크립트는 기본 output).
- 세션 탐색 시 `_archive`·`_designtest` 제외(지시사항에 반영).
