# stock_research — 한국주식 리서치 파이프라인 (라이브 프로덕션)

매일 아침 뉴스·수급을 수집→분석→추천 메일 발송하는 **실운영 시스템**이다. git 있음(작업 전 커밋 확인).
supervisor 데몬이 상주할 수 있다(현재는 온디맨드 MCP 전환 중). **여기서의 실수는 실제 발송 메일·회고 데이터셋을 오염시킨다.**

## ★ 절대 규칙 (위반 금지)

1. **비밀 파일 내용 출력·전송·커밋 금지**: `*_api.txt`(dart/fsc/kis/naver/gemini/ecos/gdelt/apify)·`gemini_keys.txt`·`mail_config.txt(.full)`·`appscript_config.txt`·`krx_account.txt`·`gmail_credentials.json`. 존재 확인은 크기만. `.gitignore`가 `*.txt` 전체를 차단하니 **git add -f 금지**, 커밋 전 `git status`로 비밀 미포함 확인.
2. **콘솔에 4바이트 이모지 출력 금지**(cp949 크래시). 리포트·메일 본문도 이모지 금지, 기호는 BMP(▲▼)만. 파일 IO는 UTF-8, JSON은 `ensure_ascii=False`.
3. **supervisor/데몬을 임의로 재시작·종료하지 마라**(사용자 확인 필요). 종료는 `stop_supervisor.bat`(lock PID 기반)로만.
4. **`output/_archive`·`_designtest`·`_retired`·다른 날짜 세션을 분석·수정하지 마라.**
5. **추측 금지**: CLI 인자·파일 위치·함수 동작이 불확실하면 **먼저 grep/Read로 코드를 확인**하라. "아마 이럴 것"으로 실행하지 마라(아래 CLI 진실표의 함정들이 그렇게 생겼다).
6. 라이브 파일 수정 전 `_backup/`에 타임스탬프 백업(또는 git 커밋), 수정 후 반드시 [검증 게이트] 통과.

## ★ CLI 진실표 (겉과 속이 다른 함정 — 검증된 사실)

| 하고 싶은 것 | ✅ 올바른 명령 | ❌ 함정 |
|---|---|---|
| force_scores.json 생성(세력강도) | `python watch_and_analyze.py --once` (collect 세션 자동탐지→저장, ~75s) | `python force_analysis.py`는 인자 필요+**stdout 출력만, 저장 안 함** |
| 메일 발송 | `python research_agent.py mail --session <세션> --method appscript` | `--method auto/api/smtp`는 gmail_credentials.json·app_password 필요(현재 없음→실패). **작동하는 건 Apps Script뿐** |
| 발송 대안(데몬식) | `report-done --session <세션>` 후 `python watch_and_send.py --once` (dedup+아카이브 포함) | |
| 수급 수집 | `python flow_collect.py` (인자 없이도 기본=수집) | `--check`는 점검만 |
| 회고 데이터 전달 | `python retro_forward.py --push` / 피드백 회수 `--scan-back` | |
| 세션 경로 인자 | 따옴표 없이: `--session output\2026-…` | cmd에서 `--session "경로"`는 따옴표가 인자에 포함돼 "세션 없음" 오류 |

- **신호파일 위치**: 대부분 세션폴더에 저장되지만 **deriv_sentiment.json·ecos_macro.json·market_caution.json·vkospi.json 4개는 루트에 저장**된다(정상 — 분석 지시 [5.9]/[5.10]가 루트에서 읽음). 세션에 없다고 실패 아님.
- **신호 수집기는 '오늘 날짜 세션'을 자동 타겟**(`_today_latest_session`): collect로 오늘 세션 만든 직후, 같은 날에 실행해야 함(자정 넘기면 어긋남). 오늘 세션 없으면 루트 폴백(무용).
- **market_caution.py는 flow/deriv/ecos 산출물을 읽으므로 신호 중 맨 마지막에 실행.**
- KRX(pykrx)·BOK(ecos)는 **저녁·밤에 간헐 실패**(krx=0, timeout) — 스크립트는 exit 0 graceful. 데이터 완전성은 장중/아침이 최고.
- 온디맨드 전 과정 절차는 **`..\stock_research_mcp\코워크_통합지시_최종.md`**(PART A) 참조.

## ★ 리팩터 지뢰 (겉보기 중복이지만 통합·삭제하면 기능 깨짐)

- **두 이메일 렌더 경로**(`render_report_html` vs 구식 md 변환): 의도적으로 다름. 통합 금지.
- **RSI/OBV 3변형 + 숫자변환 헬퍼 4변형**: 반올림·기본값 비호환(force flat=50/2dp, overheat=100/1dp, deriv `_num` 실패=0.0). 영구 retro_dataset·비선형 임계값에 먹힘 → 병합 금지.
- **`*_api.txt` 키 로더들**: 겉은 중복이나 제공자별 검증(길이·화이트리스트·키회전)이 다름 → 통합 금지.
- **count_articles.py**: `run_morning_auto.bat`이 stdout 정수를 파싱함 → 삭제·import 통합 금지(import 시 stdout.reconfigure가 출력 오염).
- **research_agent.py의 Selenium 전역**(_SELENIUM_DRIVER 등): 분할 시 함수와 함께 이동 필수.
- **USE_PLAYWRIGHT=0의 dead 함수·_fallback_md_to_html**: 의도된 토글/폴백 → 삭제 금지.
- 원칙: 통합·삭제 전 "출력 바이트 동일 + 부작용 동일"을 실측으로 증명 못 하면 하지 마라.

## 공통 헬퍼 (새 코드는 이걸 써라)

- 원자적 저장: `from common import save_json_atomic, atomic_write_text` (fsync=True 옵션=무결성용). **다시 인라인 복붙하지 마라.**
- common.py는 부작용 없는 순수함수만 담는다(import 시 stdout 건드리는 코드 추가 금지).

## ★ 검증 게이트 (코드 수정 후 필수 — 생략 금지)

1. `python -m py_compile <수정파일>` 2. `python -c "import <모듈>"`(의존 모듈 포함) 3. 대표 스크립트 **라이브 1회 실행**으로 산출물 확인(예: fsc_collect→세션 json) 4. `git status`로 비밀 미스테이징 확인 후 커밋. 5. 결과를 **정직하게** 보고(실패·부분성공을 성공으로 포장 금지).

## 도메인 규칙 (분석·회고·백테스트 작업 시)

- **룩어헤드 금지**: 예측·회고 피처는 그 시점(06:30 KST) 이전 데이터만. KRX 일별 데이터는 전 거래일 기준.
- 분석 판단 규칙의 원본(source of truth)은 `cowork_instructions.md`(아침)·`회고분석_지시사항.md`(회고, retro_forward가 stock_retro로 복사) — **stock_retro 사본만 고치면 되돌려진다. 원본을 고쳐라.**
- predictions.json은 `timing·conviction·preprice` null 금지, `entry_ref`=예측 시점 가격.
