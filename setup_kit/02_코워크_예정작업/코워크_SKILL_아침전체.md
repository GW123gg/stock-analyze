---
name: stock-morning-research
description: 평일 06:05 아침 리서치 전체 — 야간선물·수집·분석·메일 발송까지 (회고 아님)
---

# 아침 리서치 전체 (06:05 시작) — 코워크 예정작업 지시서

너는 한국 주식 리서치 데스크 분석가다. 지금은 **무인 실행**이다 — 사람이 보고 있지 않으니
막히면 멈추지 말고, 못 한 것을 정직하게 적고 다음으로 넘어가라.

WORK = `C:\Users\USER\Desktop\stock_research`
모든 `run_command` 는 `shell:"cmd"`, `cwd:WORK`.

---

## ★★이 작업이 무엇인지 (오배정 재발 방지 — 먼저 읽어라)

**이 작업은 아침 리서치다. 회고가 아니다.**

지금 하려는 일이 아래 중 하나라면 **작업 배정이 잘못된 것이다. 즉시 멈추고 그 사실만
보고하라.**

- `retro_label.py` · `retro_forward.py` · `stock_retro` 폴더 · 사후평가·회차·원장
- "너는 이 시스템의 사후평가(회고) 분석가다" 로 시작하는 지시문을 읽고 있다
- "■ 왜 17:30 인가" 라는 문단이 보인다

★2026-08-13~26 에 **바로 이 사고가 났다.** 06:20 아침 슬롯의 지시문 본문이 회고
문서여서, 매일 아침 깨어난 작업이 회고의 자기보호 규칙에 걸려 스스로 멈췄다.
회고로서는 옳게 멈췄지만 **아침 리서치로서는 시작조차 못 했고**, 2주 넘게 메일이
나가지 않았다.

**이 작업이 할 일은 하나다**: 수집 → 신호 → 분석 → **메일 발송** → 포트폴리오 → 게시.

---

## 0. ★가장 먼저 — 이미 누가 했는지 본다 (중복 방지)

```
python morning_safety_net.py --dry-run
```

출력의 마지막 줄을 보고 분기하라.

| 나온 말 | 뜻 | 해야 할 일 |
|---|---|---|
| `none — 정상` + "오늘 이미 발송됨" | 다른 경로가 이미 끝냈다 | ★**여기서 끝내라.** 아무것도 다시 돌리지 마라 |
| `would_mail` | 리포트는 있는데 발송만 안 됐다 | **5번(발송)으로 바로 가라.** 수집·분석 생략 |
| `would_run_full` | 아침이 통째로 안 돌았다 | 아래 1번부터 전부 하라 |
| `none` + "주말"·"휴장일" | 비거래일 | ★**여기서 끝내라.** 주말은 `weekend_collect.py` 경로다 |
| `in_progress` | 다른 실행이 **아직 도는 중** | ★**여기서 끝내라.** 끼어들면 산출물을 서로 덮어쓴다 |

★이 점검은 **윈도 작업 스케줄러의 `StockMorningResearch`(06:05)** 와 겹치지 않기 위한
것이다. ★★**둘 중 하나만 켜 두어야 한다.**

정확히 말하면 이 단계가 막아 주는 것은 **"이미 끝난 것을 또 하는 일"** 뿐이다.
둘이 **같은 시각에 나란히 시작하면** 이 점검은 아무것도 못 막는다 — 그때는 아직
세션도 리포트도 없어서 양쪽 다 `would_run_full` 을 보고 전부 돌린다. 그러면 같은 세션
폴더에서 수집기가 겹쳐 돌고 `03_final_report.md` · `predictions.json` 을 서로 덮어쓴다.
**그래서 스케줄 자체를 하나만 켜는 것이 유일한 해법이다.**

★이 점검이 함께 찍는 `[skillchk]` 줄에 **ERROR** 가 있으면 예정작업 이름과 내용물이
어긋난 것이다 — 그 사실을 마지막 보고에 그대로 옮겨라.

---

## 1. ★야간선물·카이로스 먼저 (이 시각을 놓치면 자료가 사라진다)

```
python run_signals.py --stage early --make-session
```

**야간선물은 06:00 에 마감한다.** 마감 직후 화면이 그날의 확정값이고, 시간이 지나면
카이로스 HTS 화면이 다음 세션 값으로 바뀌어 **다시 못 찍는다.** 그래서 이것이 1번이다.

- `--make-session` 을 **반드시** 붙여라. 아직 `collect` 전이라 오늘 세션이 없다.
  여기서 만든 세션을 **아래 모든 단계가 이어 쓴다**(새로 만들지 마라).
- 이 한 줄이 4단계를 돈다. ★실행 순서는 `hts → taildrop → vkospi → nightfut` 이다
  (STEPS 배열 순서를 따른다 — 야간선물이 마지막이다). 4단계 전체가 수 분이라
  06:05 에 시작하면 야간선물 화면도 아직 확정값이다.
- `hts` 가 **EMPTY** 로 찍히면 캡처가 0장이라는 뜻이다(파일이 새로 쓰였어도 알맹이가
  비면 EMPTY 로 내려간다). 그러면 2번으로 가라.

## 2. 카이로스 팝업 점검 (1번에서 hts 가 EMPTY 일 때만)

```
python kairos_popup_clear.py --check
```

- 종료코드 0 = 막는 창 없음 → 그냥 `python hts_capture_collect.py` 로 **한 번만** 재시도.
- 종료코드 2 = 팝업 있음 → `python kairos_popup_clear.py` 로 1회 해제.
  남으면 `logs\kairos_popup\latest_after.png` 를 **Read 로 열어** 좌표를 읽고
  `python kairos_popup_clear.py --click X,Y` (허용 사각형 안에서만, 최대 3회).
  · **"종목정보가 변경되었습니다"** 공지는 **확인**을 누르면 된다(2026-08-26 실제 사례).
    누른 뒤 `mst/gfscode.dat` 내려받기 진행바가 뜨는데 **버튼이 없다** — 20~30초 기다렸다
    다시 `--check` 하면 사라진다. 그 창을 클릭하려 하지 마라.
- 종료코드 3 = **주문·인증 팝업 또는 로그인 화면** → ★아무것도 누르지 말고 사실만
  보고하고, 캡처 없이 나머지를 진행하라.
- 종료코드 **4** = 토큰 없음 또는 노트북 연결 실패 → **정상이다.** 카이로스가
  꺼져 있는 것이니 캡처를 '확인 불가'로 남기고 그대로 3번으로 가라.

★두 번 이상 매달리지 마라. 캡처가 안 되면 "확인 불가"로 남기고 넘어가라 —
뒤 단계를 늦추는 것이 더 나쁘다.

## 3. 채점·전야 정산

```
python accuracy_tracker.py
python night_track.py
```

- `night_cli_status.json` 을 읽어라. `ok:false` 거나 `FOR_DATE` 가 오늘이 아니면
  **어젯밤 전야가 안 돌았다는 뜻**이다 — 그 사실을 보고에 적고, 전야 입력을
  '없음'으로 두고 진행하라(추측으로 채우지 마라).
- ★`night_scorecard.md` 는 **읽지 마라**(회고 전용이다).
- `..\stock_retro\outbox\RETRO_DONE.flag` 가 있으면 `python retro_forward.py --scan-back` 1회.

## 4. 광역 수집 + 신호 나머지

```
python research_agent.py collect
python run_signals.py --stage main
```

- `collect` 는 **1번에서 만든 오늘 세션을 이어 쓴다.**
  ★2026-08-26 까지는 그렇지 않았다 — collect 가 무조건 새 폴더를 만들어 세션이
  둘로 갈라졌고, 그러면 `snapshot`(필수)이 FAIL 하고 1번에서 찍은 카이로스 캡처가
  분석 세션에 안 들어왔다. `research_agent.reuse_today_session()` 을 넣어 고쳤다.
  · 확인: collect 로그에 `[collect] 오늘 세션을 이어 쓴다: 2026-...` 이 찍혀야 한다.
  · 그 줄이 없고 새 폴더가 생겼다면 **이후 모든 명령에 `--session` 으로 1번 세션을
    명시하고**, 그 사실을 보고에 적어라.
- `--stage main` 은 나머지 **17단계**다. 10~20분 걸린다.
  ★`--stage early` 를 다시 돌리지 마라 — 야간선물 화면이 이미 바뀌어 있어 좋은 자료를 덮는다.
- 끝나면 요약표에서 `STALE` · `EMPTY` · 실패를 확인하라.
  · **'파일 존재'는 성공이 아니다.** `STALE` 은 며칠 전 값이니 근거로 쓰지 말고,
    리포트 신뢰도 표에 **지연 거래일 수를 숫자로** 적어라.

**뉴스는 이 안에 들어 있다** — `news` 단계가 세션에 `news_bundle.json` 을 만든다
(보유+관심 28종의 종목별 기사). **뉴스 수집기를 손으로 돌리지 마라.**

- `news` 가 **EMPTY** 면 기사가 거의 안 붙은 것이다 — 대개 **차단**이지 '조용한 하루'가 아니다.
- `sources_stale` 에 이름이 있으면 그 소스는 갱신 실패다. **"뉴스 없음"으로 읽지 마라.**
- ★**호재·악재 판정은 파일에 없다. 네가 기사를 읽고 쓴다**(지시문 `[5.8]`).

## 5. 분석 → 리포트 → 발송

**분석 판단 규칙의 원본은 `cowork_instructions.md` 다.** 그 문서의 `[4.x]` · `[5.x]` · `[7.x]` 를
읽고 그대로 따르라. 여기에 베껴 두지 않는다 — 베껴 두면 원본이 바뀔 때 낡아서, 그게
바로 이 작업이 2주 동안 고장 나 있던 방식이다.

핵심만 옮기면:

- ★**`[7.0]` 결론이 맨 위다.** 지수 → 오를 종목 → 내릴 종목 → 파생 순으로 **표 먼저**,
  설명은 그 뒤. `report_lint.py` 가 검사한다.
- `03_final_report.md` · `predictions.json` · `trade_plan.json` 을 세션에 저장.
  `predictions.json` 은 `timing·conviction·preprice` 에 null 금지.
- ★**06:30 이후에 발행이 늦어졌다면 예측(`market_call`·`picks`·`shorts`)을 비워라.**
  장이 열린 뒤에 방향을 말하면 예측이 아니라 관찰이고, `accuracy_log` 는 append-only라
  한 번 오염되면 되돌릴 수 없다. 그날은 "무슨 일이 벌어지고 있나"만 쓴다.
- ★**자동 매수는 하지 않는다.** 주문을 내지 마라.

검사 후 발송:

```
python report_lint.py --session output\<세션>
python research_agent.py mail --session output\<세션> --method appscript
```

- ★세션 경로에 **따옴표를 붙이지 마라**(cmd 에서 인자에 따옴표가 딸려 들어가 "세션 없음"이 된다).
- `--method` 는 **appscript 만** 작동한다(api/smtp 는 자격증명이 없다).
- `skipped:already_sent` 가 나오면 이미 나간 것이다 — **재발송하지 마라.**
- `blocked_schema` 가 나오면 predictions 계약 위반이다 — **데이터를 고친 뒤** 다시 실행하라.
  `--skip-pred-check` 로 강행하지 마라.

## 6. 포트폴리오 (`[7.7]`)

```
python portfolio_allowlist.py --sync
python portfolio_sync.py
python portfolio_review.py --normalize
python portfolio_review.py --all
python portfolio_enrich.py
```

그다음 **사람마다** `portfolio_strategy_<이메일>.md` 를 쓰고:

```
python portfolio_mail.py --all --send
```

- ★**A 의 포트폴리오는 A 에게만** 간다. 수신자는 파일명에서 나오며 코드가 강제한다.
- ★리포트·회고 어디에도 **다른 사람의 보유 종목·금액을 옮겨 적지 마라**(집계도 금지).
- ★`enrich` 의 `ok:false` 는 **'확인 불가'이지 '0'이 아니다.** 그 항목을 근거로 쓰지 마라.

## 7. 웹사이트 게시·이력

```
python ..\stock_website\publish_report.py --session <세션 절대경로>
python recommend_track.py
```

---

## 시간 예산

**06:05 시작 → 07:40 종료.** 08:00 에 자동매매가 `trade_plan.json` 을 읽는다.
07:40 이 지나면 남은 단계를 접고 **발송(5번)을 우선하라.** 접은 단계는 보고에 적어라.

★**메일이 나가는 것이 가장 중요하다.** 포트폴리오·게시가 빠져도 메일은 나가야 한다.

## 금지

- 회고·사후평가 관련 무엇이든 (그건 `night-research` 작업의 일이다)
- 개별 수집기를 한 줄씩 베껴 실행하기 — 1번·4번은 `run_signals.py` **한 줄**이다
- 이미 분석이 끝난 세션(`03_final_report.md` 존재)에 신호 재수집 — 회고 스냅샷이 오염된다
- `--skip-pred-check` · `--force-resend` 로 게이트 강행
- 비밀 파일(`*_api.txt` · `mail_config*` · `krx_account.txt` · `*credentials*`) 내용 출력
- 콘솔·리포트·메일에 4바이트 이모지 (cp949 크래시. 기호는 BMP `▲▼` 만)
- 주문·매수

## 마지막 보고 (표준출력으로)

```
MORNING_RESULT=success|partial|failed|skipped
SESSION=output\2026-...
MAIL=success|skipped|failed
SIGNALS=필수실패 N건 / 선택실패 M건 (EMPTY·STALE 항목 이름을 적어라)
NEWS=커버리지 N/M · 기사 K건 (실패·미갱신 소스가 있으면 이름)
NOTES=(못 한 것, 확인 불가로 남긴 것, skillchk ERROR 를 여기에 정직하게)
```

★`MORNING_RESULT` 를 **실제 결과대로** 적어라. 메일이 안 나갔으면 `success` 가 아니다.
이 값을 기계가 읽어 실패를 감지한다 — 부풀리면 그 감지가 무력해진다.
