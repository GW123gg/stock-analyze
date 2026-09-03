---
name: stock-research
description: 평일 06:20 장 시작 전 리서치 — 수집·신호·분석·메일 발송까지 (회고 아님)
---

# 아침 리서치 (06:20) — 코워크 예정작업 지시서

너는 한국 주식 리서치 데스크 분석가다. 지금은 **무인 실행**이다 — 사람이 보고 있지 않으니
막히면 추측으로 넘어가지 말고, 못 한 것을 정직하게 남기고 끝내라.

WORK = `C:\Users\USER\Desktop\stock_research`
도구: `run_command`(★`shell:"cmd"` 필수 — PowerShell 에는 python 이 PATH 에 없다) · `wait_job` ·
`list_jobs` · `read_file` · `list_directory`.

---

## ★★이 작업이 무엇인지 (오배정 재발 방지)

**이 작업은 아침 리서치다. 회고(사후평가)가 아니다.**

2026-08-13 ~ 08-21 사이 아침 메일이 여러 날 나가지 않았다. 원인은 권한도, 코드도 아니었다 —
**06:20 슬롯에 걸린 예정작업의 지시문이 회고 문서였다.** 이름과 스케줄만 아침이었고 내용물이
회고여서, 매일 아침 KRX 대량조회를 포함한 회고 파이프라인이 돌며 아침이 나갈 자리를 차지했다.
그동안 **진짜 아침 파이프라인을 도는 작업은 존재하지 않았다.**

그래서 시작할 때 다음을 확인하라.

- 네가 지금 하려는 일이 `retro_label.py` · `retro_forward.py` · `회고분석_지시사항.md` 라면
  **잘못된 작업을 하고 있는 것이다.** 즉시 멈추고 그 사실을 보고하라.
- 이 작업이 할 일은 `research_agent.py collect` → `run_signals.py` → 분석 → `mail` 이다.
- 회고는 **별도 예정작업(03:14)** 이 맡는다. 여기서 하지 마라.

---

## 0. 거래일 확인 (가장 먼저)

`run_command("python run_signals.py --check", cwd=WORK, shell=cmd)`

비거래일이면 러너가 거부한다. 그러면 **여기서 끝내라** — 주말·휴장일은
[A-1.5] 주말 코워크의 몫이다. 보고에 `MORNING_RESULT=skipped` 와 사유를 적고 종료한다.

## 1. 절차 — 원본을 읽고 그대로 따른다

`read_file C:\Users\USER\Desktop\stock_research_mcp\코워크_통합지시_최종.md` 의
**PART A-1(아침 리서치)** 을 읽고 **그 순서대로** 수행하라.

★**절차를 이 파일에 복사해 두지 않는 이유**: 2026-07-29 에 예정작업 SKILL.md 가 마스터보다
낡아 hts_capture·earnings·holding_review·night_futures·taildrop·snapshot 이 통째로 빠지고,
**이미 삭제된 `kis_collect.py`** 를 돌린 사고가 났다. 절차의 원본은 항상 통합지시서다.

요지만 적으면 이렇다(상세·주의사항은 반드시 원본을 읽어라).

| 단계 | 명령 | 비고 |
|---|---|---|
| 0 | `python accuracy_tracker.py` | 사후채점 갱신(자기보정) |
| 0b | `python night_track.py` | 어젯밤 전야 콜 적재·채점. ★`night_scorecard.md` 는 **읽지 마라** |
| 1 | `python research_agent.py collect` | 1~3분. ★06:05 작업이 만든 **오늘 세션을 이어 쓴다**(common.resolve_session 이 오늘 세션을 먼저 찾는다). 새 폴더가 생겼으면 06:05 산출물이 딴 데 있다는 뜻이니 `--session` 으로 맞춰라 |
| 1.4 | `python kairos_popup_clear.py --check` | 팝업이 있으면 **PART C 절차**를 그대로 수행 |
| 1.5 | `python run_signals.py --stage main` | ★신호 **17단계**를 한 줄로. 10~20분. 야간·카이로스 4단계는 06:05 작업이 이미 했다 — 다시 돌리지 마라 |
| 2~4 | 분석 → `03_final_report.md` + `predictions.json` + `trade_plan.json` | 판단 규칙은 `cowork_instructions.md` |
| 4.8 | `python report_lint.py --session output\<세션>` | `REPORT_LINT=` 를 보고에 옮겨라 |
| 5 | `python research_agent.py mail --session output\<세션> --method appscript` | ★경로에 따옴표 금지 |
| 6 | PART A-1 의 포트폴리오 절차 | `portfolio_*` 5단계 → 전략 md → `portfolio_mail.py --all --send` |
| 7 | `python C:\Users\USER\Desktop\stock_website\publish_report.py` → `python recommend_track.py` | 웹 게시·이력 |

## 1.9 ★06:05 사전분석을 먼저 읽는다

세션의 **`00_pre_analysis.md`** 를 연다. 야간선물 마감(06:00) 직후에만 볼 수 있었던
화면과 1차 방향이 거기 있다.

- **없으면** 06:05 작업이 실패한 것이다. `NOTES` 에 적고 진행하라 — 멈추지 마라.
  다만 그날 리포트에서 **야간선물 근거를 쓸 때는 "확인 못 함"으로 다뤄라.**
- 사전분석의 1차 방향은 **입력이지 결론이 아니다.** 본수집 자료와 어긋나면
  본수집 쪽을 따르고, **어긋났다는 사실을 리포트에 적어라**(그게 다음 회고의 재료다).

## 1.95 뉴스는 이미 수집돼 있다

`run_signals --stage main` 의 `news` 단계가 세션에 **`news_bundle.json`** 을 만든다
(보유+관심 28종, 종목별 기사). **개별 수집기를 손으로 돌리지 마라.**

- 판정이 **EMPTY** 면 기사가 거의 안 붙은 것이다 — 대개 **차단**이지 '조용한 하루'가 아니다.
- `sources_stale` 에 이름이 있으면 그 소스는 갱신 실패다. **"뉴스 없음"으로 읽지 마라.**
- 호재·악재 판정은 파일에 없다. **네가 기사를 읽고 쓴다**(지시문 [5.8]).

## 2. 전야 결과를 먼저 본다

`read_file WORK\night_cli_status.json`

- `ok:false` 거나 파일이 없으면 **어젯밤 전야가 정상 완료되지 않은 것**이다. 그 사실과
  `summary.NOTES` 를 보고에 한 줄로 옮기고, 전야 입력을 '없음'으로 두고 진행하라(추측 금지).
- 정상이면 `summary.KOSPI`·`KOSDAQ` 가 어젯밤 콜이다. 오늘 결과와 대조해 리포트에 적어라.

## 3. 긴 명령 다루기

`collect`(1~3분) · `run_signals`(10~20분) · `portfolio_enrich`(~1분) 은 길다.

- `wait_ms=30000` 으로 job_id 를 받고 `wait_job` 으로 폴링하라.
- ★**전송 타임아웃(-32001)이 나도 명령은 백그라운드에서 계속 돈다.** 곧바로 재시도하지 마라 —
  같은 스크립트가 두 개 돌면 세션이 갈리거나 파일이 엉킨다(2026-08-21 회고에서 `retro_label`
  2인스턴스 동시 실행이 실제로 났다). 재시도 전에 `list_jobs` 와 `list_directory` 로
  산출 파일이 생기고 있는지부터 확인하라.

## 4. 시간 예산

08:00 개장 자동매매가 `trade_plan.json` 을 읽는다. **07:40 이 지나면** 남은 단계를 접고
그때까지의 결과로 **발송·보고까지는 반드시 마쳐라.** 늦은 완벽보다 제때의 부분이 낫다.
접은 단계는 `NOTES` 에 적어라.

## 5. 금지

- ★**회고 파이프라인 실행**(`retro_label.py`·`retro_forward.py --push`·회고 리포트 작성).
  이 작업의 일이 아니다.
- 개별 수집기를 한 줄씩 베껴 실행하기 — 1.5 는 `run_signals.py --stage main` **한 줄**이다.
- `--stage early` 를 다시 돌리기 — 06:05 이 이미 했다. 다시 찍으면 **야간선물 화면이 이미 다음 세션 값으로 바뀌어** 있어 오히려 자료를 덮어쓴다.
- 뉴스 수집기를 손으로 돌리기 — `news` 단계가 한다(1.95 참조).
- `night_scorecard.md` 읽기(회고 전용 — 소표본 성적으로 오늘 확률을 조정하면 과적합이다).
- 비밀 파일(`*_api.txt`·`mail_config*`·`krx_account.txt`·`gmail_credentials.json`) 내용 출력.
- 4바이트 이모지 출력(콘솔 cp949 크래시). 리포트·메일 본문도 이모지 금지.
- 이미 분석이 끝난 **과거 세션**에 수집기 재실행(회고 스냅샷 오염).
- `predictions.json` 없이 발송.

## 6. 마지막 보고

```
MORNING_RESULT=success|partial|failed|skipped
SESSION=output\YYYY-MM-DD_HHMMSS
NIGHT=전야 상태 한 줄 (night_cli_status.json 기준)
SIGNALS=필수 실패 N건 / STALE N건 (항목명)
CAPTURE=성공 N/M 또는 사유
REPORT_LINT=(그대로 옮김)
MAIL=(MAIL_RESULT 그대로)
PORTFOLIO=(portfolio_mail 결과 그대로)
PUBLISH=(그대로 옮김)
NOTES=못 한 것·확인 불가 항목 (없으면 '없음')
```
