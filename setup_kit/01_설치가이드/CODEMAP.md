# CODEMAP — stock_research 전체 코드 레퍼런스

> **용도**: 어떤 모델(opus/sonnet/haiku)이든 이 파일만 읽으면 "어느 파일이 무슨 일을 하고, 무엇을 읽고 쓰는지"를
> 재탐색 없이 아는 것. 규칙·함정은 `CLAUDE.md`(필독), 운영 절차는 `..\stock_research_mcp\코워크_통합지시_최종.md`.
> ⚠️ 여기 요약과 실제 코드가 다르면 **코드가 진실** — 수정 시 이 파일도 갱신하라.

## 시스템 한눈에

```
[02:00 precollect] → [06:30 collect(세션 생성)] → [신호 수집기 15종] → [Cowork 분석: commands.txt→deep→03_final_report+predictions]
       → [발송: report-done→watch_and_send 또는 mail --method appscript] → [recommend_track]
[03:30 회고] retro_label → retro_forward --push → (회고 Cowork) → --scan-back → retro_feedback.md → 다음 아침 [0.5] 자기보정
[채점] accuracy_tracker(scorecard.md, 매일) · gen_scorecard(예측채점_리포트.md, 회고용)
오케스트레이터: supervisor(데몬) 또는 온디맨드 MCP(현재 기본) — 개별 스크립트는 양쪽에서 동일하게 동작
```

**세션 폴더**(`output\YYYY-MM-DD_HHMMSS\`): 00_precollect.md · 01_broad_collection.md · INSTRUCTIONS.md · commands.txt ·
02_deep_collection.md · 03_final_report.md/.html · predictions.json · force_scores/market_context/fsc_prices/flow_data/
overheat/fundamentals/disclosures/mirae_data/short/earnings_calendar.json · signals_snapshot_*.json(루트 신호 동결) · *.flag. 발송 후 `_archive\`로 이동.
**루트 저장 신호**(세션 아님): deriv_sentiment · ecos_macro · market_caution · vkospi · credit_balance(5종) + 뉴스 6종(news_rss/gdelt/media_rss/naver_stock_news/yahoo_news/analyst_reco).

## 1. 오케스트레이션·감시

| 파일 | 역할 | 핵심 |
|---|---|---|
| `supervisor.py` (55KB) | 24h 데몬: 02:00 precollect→03:30 retro→06:30 morning(step0~16)→발송 감시 | run_morning_pipeline·one_cycle(30s)·heartbeat·RUN_NOW.flag. CLI `--interval`·`--morning-time`. 시각은 파일 상수 수정 시 25초 내 반영 |
| `watchdog.py` | supervisor 프리즈(살아있으나 멈춤) 감지→재기동. 작업스케줄러 5분마다 | supervisor.lock PID+heartbeat 검사. 락 없으면 콜드스타트(start_supervisor_visible.bat) |
| `watch_and_analyze.py` | COLLECT_DONE 감시→force_analysis 실행→**세션에 force_scores.json 저장** | ★force_scores 만들 땐 이것(`--once`) — force_analysis 직접 실행은 저장 안 함 |
| `watch_and_send.py` | REPORT_DONE 감시→**Apps Script 발송**→sent_index 중복방지→세션 _archive 이동 | `--once`=1회. appscript_config.txt(URL+secret)+mail_config.txt(to). post_to_appscript/_strip_non_bmp는 research_agent appscript 발송이 재사용 |
| `recover.py` | 상태 진단→빠진 단계 복구(세션없음→api_collect, force없음→force_analysis) | 수동 진입점. 인자 없음 |
| `check/start/stop_supervisor.bat` | 상태점검 5항목 / 보이는 콘솔로 기동 / lock PID 기반 강제종료 | stop은 온디맨드 전환 시 데몬 종료용 |
| `run_morning_auto.bat` | (구) 아침 자동수집 체인 — count_articles stdout 정수로 폴백 판정 | ⚠️ count_articles 삭제/통합 금지 사유 |

## 2. 메인 에이전트

**`research_agent.py` (175KB — 전체 읽기 금지, 필요한 함수만 grep)**
- 역할: 뉴스 광역수집(collect: 구글RSS+네이버+Gemini, 세션 생성) · 심층수집(deep --session: commands.txt 실행) ·
  발송(mail) · 발송신호(report-done: REPORT_DONE.flag+**render_report_html**) · archive · auto.
- 발송: `send_email` 디스패처 = api(gmail_credentials.json)/smtp(app_password)/**appscript**(유일 설정됨)/auto(api→smtp→appscript 폴백).
  `render_report_html(sess)`=헤더배너+predictions 대시보드+**전일등락률표(_prev_day_change_md)**+본문+세력강도설명 → 03_final_report.html.
  cmd_mail도 render_report_html 사용(데몬과 동일 품질).
- 세션 탐색: `latest_session_dir()`은 `_`로 시작하는 폴더(_archive/_designtest) 제외.
- 함정: Selenium 전역(_SELENIUM_DRIVER 등)·USE_PLAYWRIGHT=0 dead 함수·_fallback_md_to_html은 의도적(CLAUDE.md 지뢰).

## 3. 신호 수집기 (15종 — 실행 순서·위치는 MCP 지시 표)

| 파일 | 산출(위치) | 데이터원(키) | 비고 |
|---|---|---|---|
| `force_analysis.py` (40KB) | stdout JSON — **저장은 watch_and_analyze가** | pykrx(krx_account.txt)→Naver 폴백 | 세력강도 -100~+100(수급/거래량/모멘텀/OBV 4축). CLI `--ticker --market --json` |
| `market_collect.py` (37KB) | market_context.json(세션) | yfinance+pykrx+FDR | 인터마켓 10종·섹터RS 14·breadth·flows·regime·**kr_index(KOSPI/KOSDAQ 5일)** |
| `fsc_collect.py` | fsc_prices.json(세션) | 금융위 주식시세 API(fsc_api.txt)→FDR 폴백 | 공식 종가=회고 채점 기준. source 필드로 출처 기록 |
| `flow_collect.py` | flow_data.json(세션) | pykrx(KRX 로그인) | 외인/기관/개인 5d·20d+연속순매도+risk_off. 인자 없이=수집. `get_flow_asof`를 retro_label이 재사용 |
| `overheat_collect.py` | overheat.json(세션) | FDR 1년 일봉 | 이격도·연속상승·52주고가·RSI·OBV다이버전스·ret_20d |
| `dart_collect.py` (26KB) | fundamentals.json(세션)+cache | DART(dart_api.txt)→yfinance 폴백 | 4년 재무 GPM/OPM/FCF 추세, 장투 게이트 |
| `disclosure_collect.py` | disclosures.json(세션) | DART 공시 | 증자/CB/자사주/대주주 오버행 분류. retro_label이 classify 재사용 |
| `mirae_collect.py` | mirae_data.json(세션)+토큰캐시 | 미래에셋(mirae_api.txt)→yfinance 폴백 | 투자자별 순매수·외인보유율 |
| `short_collect.py` | short.json(세션) | pykrx | 공매도 잔고비중·10d 증감(T+1~2 지연 — `get_short_asof`는 당일 제외) |
| `deriv_collect.py` | deriv_sentiment.json(**루트**) | pykrx(KRX) | KOSPI200 PCR+개별 풋콜. flow_collect import로 세션 워밍업 필수 |
| `ecos_collect.py` | ecos_macro.json(**루트**) | 한국은행 ECOS(ecos_api.txt) | 기준금리·환율 5일. 플레이스홀더 키 거부. 저녁 타임아웃 잦음 |
| `vkospi_collect.py` | vkospi.json(**루트**) | 금융위 지수시세(vkospi_api.txt→fsc 키 폴백) | VKOSPI 수준/5일변화/60d백분위/공포라벨 — F1 입력. 키 활용신청 필요 |
| `credit_collect.py` | credit_balance.json(**루트**) | 금투협 freesis 공개 JSON(**키 불필요**) | 신용잔고(빚투)·증시자금·반대매매 [5.12]. 컬럼 매핑 언론 실측 대조. 실패 시 생략=정상 |
| `earnings_collect.py` | earnings_calendar.json(**세션**) | investing.com(requests→curl 폴백) | 향후 2주 실적발표 [5.13]. 실패 시 웹검색 폴백=정상 |
| `holding_review.py` | holding_review.json(**세션**) | 과거 predictions.json + FDR 종가 | **"어제 산 걸 계속 들고 있어도 되나"** [5.16]·리포트 1.95. ★판정 안 함 — 픽이 선언한 target/stop/trailing/horizon/expected_peak_days 대비 현재 위치만 측정(새 임계값 0). priority 1~5·attention·alpha 병기. ★계약 미선언 픽은 level_flags 키 자체가 없다(False=미이탈 오독 방지). 직전 거래일까지만(룩어헤드 없음) |
| `intraday_review.py` | intraday_review.json(**세션**) | 세션 predictions + FDR 당일 시고저종 | **장중 재분석**(v11.3 · skill `intraday-review`, 권장 13:00/15:40). ★핵심은 `entry_window` **사후 검증** — '아침에 말한 자리에서 실제로 살 수 있었나'(갭 +3% 초과=당일시가 진입 불가, 저가가 진입가 위=눌림 진입 불가). 내일 대비용 next_day 갱신 입력. ★당일 장중가라 `is_intraday=true`·`retro_use=forbidden_as_pre_feature` — 회고 진입피처 금지 |
| `kairos_client.py` | (라이브러리) | 카이로스 캡처 에이전트 `http://desktop-psk2gpr:8788` (테일넷 전용·토큰 `kairos_api.txt`) | 노트북 호출 래퍼. ★3중 검증(marker_text 화면번호·sha256·settled)·watchlist set/reset·이름→IP 폴백. `--health`/`--screens`/`--capture` CLI |
| `hts_capture_collect.py` | hts_capture.json + hts_captures/*.png(**세션**) | kairos_client 경유 | **KRX 403 차단분(공매도·대차잔고) 유일 대체** [5.15]. 검증 통과한 장만 저장하고 폐기 사유를 rejected 에 남긴다. 0231/0261 은 watchlist 필요(try/finally 로 reset 보장). 9314 는 카이로스 미지원이라 UNAVAILABLE 분리. 토큰 없으면 무동작=정상 |
| `kairos_popup_clear.py` | logs\kairos_popup\latest_{before,after,manual}.png + latest.json | 노드 8788 `/popups`·`/screenshot`·`/popups/close` | **화면을 막은 창 원격 해제**(v11.24) — 막는 창이 있으면 hts 캡처가 통째로 `popup_blocked`/`modal_blocked` 로 죽는다(실측 2026-08-18: 07:00 5/5·16:30 4/4 전멸). ★두 종류를 **둘 다** 본다 — 제목 있는 공지(`list_popups`)와 **제목 없는 모달**(`modal_blocking`, `IsWindowEnabled` 기반). 후자는 카이로스가 직접 그려 표준 버튼이 없어 노드 혼자 못 닫는다 → pc21 이 `/screenshot` 을 받아 **눈으로 보고 좌표 지정**. ★클릭은 **열거된 창 사각형 안**에서만(서버 재검사·덮임 확인), **주문·인증은 제목 또는 버튼 글자로 판정해 전부 중단**, 제목 없는 모달엔 **ENTER·모서리 추정 클릭 금지**(ESC·안전버튼만), 로그인 화면 무동작. 종료코드 0=없음/2=남음/3=사람확인/4=오프라인. 노드 로직 `agent/popups.py`(+`agent/test_popups.py` 28케이스) |
| `taildrop_receive.py` | hts_capture_batch.json + hts_captures/*.png(**세션**) | Taildrop(`tailscale file get`) → 다운로드 폴더 kairos_*.zip | `/batch` 배치 캡처 수신 자동화. manifest 의 sha256·marker_text·settled 로 검증 후 **통과분만** 세션에 푼다. ★zip slip(`..`)·비허용 확장자 차단, 처리한 zip 은 `Downloads/_taildrop_done/` 로 이동(삭제 안 함), 다른 다운로드 파일은 건드리지 않음. `--check`/`--no-fetch` |
| `market_caution.py` | market_caution.json(**루트**) | 위 산출물 합성(deriv/flow/ecos+FDR) | 국면 종합게이트 0~100·regime_kind·allow_market_up_call + **inputs_age_h/stale_inputs·missing_axes/inputs_incomplete**(입력 신선도·결측). ★신호 중 맨 마지막 실행 |
| `snapshot_signals.py` | 세션에 signals_snapshot_* 5종 | 루트 5종 복사(동결) | **market_caution 다음 필수** — 회고가 그날 국면입력(F1/F8)을 학습하는 유일한 경로. retro_label 이 pre_caution/pcr/vkospi/margin/거시 피처로 읽음 |

## 4. 뉴스 수집기 (전부 루트 저장, Cowork가 [5.8]에서 직접 실행)

| 파일 | 산출 | 특징 |
|---|---|---|
| `news_rss_collect.py` | news_rss.json | 구글뉴스 RSS(국문·무키). `--keywords --n --lang` |
| `gdelt_collect.py` | gdelt_news.json | 글로벌(무키). 5.3초 rate-limit 자동준수 |
| `media_rss_collect.py` | media_rss.json | 연합·한경·매경 등 직접 RSS(media_rss_feeds.txt) |
| `naver_stock_news.py` | naver_stock_news.json | 네이버금융 종목별(모바일 JSON API) |
| `yahoo_news.py` | yahoo_news.json | 야후 영문 RSS(.KS→.KQ 폴백, 셀레늄 없음) |
| `analyst_reco.py` | analyst_reco.json | 증권사 컨센서스(투자의견·목표가) |
| `fetch_html.py` | --out 파일 | 막힌 기사 HTML(curl_cffi TLS위장→requests→`--selenium` 타이머 40s) |
| `apify_key.py` | (모듈) | Apify 토큰 로테이션 로더(다계정·402 폴백) — 현재 무료 수집기로 대체됨 |

## 5. 수집 보조·후처리

| 파일 | 역할 |
|---|---|
| `precollect.py` | 02:00 1차 뉴스수집(precollect\<날짜>\, 세션/flag 안 만듦) → `--merge`로 아침 세션에 00_precollect.md 합침 |
| `api_collect.py` | RSS 차단 시 Gemini+Naver 폴백 수집(`--session`으로 기존 세션 보강) |
| `morning_postprocess.py` | 수집 빈약 판정(기사수 임계). exit 0=충분/2=폴백필요/3=세션없음. supervisor step2가 `--check-only` |
| `count_articles.py` | 01_broad 기사수 stdout 정수 — ⚠️.bat이 파싱, 삭제·import 통합 금지 |
| `collection_report.py` | 수집점검 txt(collection_check\) — 메일 첨부용 1차/2차 txt |
| `rebuild_consolidated.py` | cowork_instructions+mock가이드 → cowork_지시사항_통합본.md 재생성(통합본은 생성물, 직접 수정 금지) |

## 6. 회고·채점 (자기개선 루프)

| 파일 | 역할 | 입출력 |
|---|---|---|
| `accuracy_tracker.py` (35KB) | 만기 예측 채점→**scorecard.md**(아침 [0.5] 자기보정 입력). 멱등 | predictions(활성+아카이브)+FDR → accuracy_log.json+scorecard.md(루트) |
| `retro_label.py` (41KB) | 회고 학습 데이터셋(피처 pre_*+라벨 ret_h/days_to_peak 등). fsync 저장 | → retro_dataset.json/csv(루트). flow/short의 asof 함수·disclosure classify 재사용. 룩어헤드 금지 설계 |
| `retro_archive_parse.py` | 6/18 이전 md 리포트 표 파싱→예측 형식(_src_kind='archive') | retro_label이 import |
| `retro_forward.py` | 회고 Cowork 브리지: `--push`(inbox 적재+지시사항 원본 복사)·`--scan-back`(outbox→retro_feedback.md 회수) | retro_config.txt(enabled/folder=stock_retro) |
| `retro_manual_refresh.py` | 수동 회고 데이터 갱신(stock_retro_manual용) | |
| `recommend_track.py` | 발송된 추천→recommended_history.json+recommended_universe.txt(fsc/flow가 watch풀과 합산)+회고폴더 복사 | 발송 성공 후 실행 |
| `prediction_scorecard\gen_scorecard.py` | 종목별·지수별 맞춤/틀림 채점(회고 §근거) — 진입일=세션시각 기반(아침=당일/저녁=익일) | → 예측채점_리포트.md |
| `mock_forward.py` | REPORT_DONE 리포트를 모의투자 Cowork 전달폴더로 복사(mock_forward_config.txt enabled=1일 때) | latest.json+중복방지 인덱스 |

## 7. 공통·설정·데이터 파일

- **`common.py`** — `save_json_atomic`·`atomic_write_text` + **`validate_predictions`(발송 계약 게이트)** + **`resolve_session`(세션 해석 통합 — 자정 6h 폴백)**. **새 저장 코드는 반드시 이걸 사용**. 부작용 없는 순수함수만 추가 가능.
- **`tests/run_tests.py`** — 골든 하네스(검증 게이트 0단계, ~5초·네트워크 0). 라벨 수학·계약·복사 검증·LABEL_COLS 완전성 자동 체크. **새 라벨/피처 추가 후 반드시 실행**.
- **키 파일**(루트 *.txt — 내용 출력·커밋 금지): dart/fsc/mirae/naver/ecos/gdelt/apify/vkospi_api.txt·gemini_keys·krx_account·mail_config(.full)·appscript_config. 각 로더는 제공자별 검증이 달라 통합 금지.
- **설정**: watch_tickers.txt(58종 유니버스)·media_rss_feeds.txt·retro_config.txt·mock_forward_config.txt.
- **상태**(생성물): supervisor.lock/heartbeat/state·sent_index.json·daily_status.json·accuracy_log.json·recommended_*.
- **지시 md**: cowork_instructions.md(아침 분석 판단 — source of truth)·회고분석_지시사항.md(회고 원본, stock_retro로 복사됨)·retro_feedback.md(회고→아침 폐루프)·scorecard.md(채점 결과).

## 8. 다른 폴더와의 관계

- `..\stock_research_mcp\` — 온디맨드 Cowork 지시(통합최종본=진입점). 코드는 전부 이 폴더 것을 실행.
- `..\stock_retro\` — 회고 Cowork 데이터 폴더(inbox/outbox). retro_forward가 push/scan-back.
- `..\auto stock\` — 자동매매 봇. `bot\research_feed.py`가 이 폴더 output을 **읽기만** 함(CODEMAP은 그쪽 폴더에).
- `..\stock_backtest\` — 과거시점 백테스트 RL 루프(price_cache·bt_loop·policy.md). 이 폴더 코드를 import하나 라이브 미변경.
