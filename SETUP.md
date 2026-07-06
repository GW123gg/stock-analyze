# stock_research 세팅 가이드 (supervisor 통합판 / 핸드셰이크)

이 PC(호스트)에서 매일 아침 **수집 → (Cowork)분석 → 메일 발송**이 자동으로 돌아가게
만드는 전체 절차다. 한 번만 따라하면 그다음부터 사용자 개입이 필요 없다.

## 핵심 구조 — 호스트와 Cowork 의 '신호 핸드셰이크'

```
  supervisor.py (보이는 콘솔 창 하나만 켜둠, 30초 루프)
    1) 매일 MORNING_TIME(기본 06:30) → 파이썬이 직접 research_agent collect 실행
       (.bat 안 거침) → COLLECT_DONE.flag
    2) COLLECT_DONE.flag → force_analysis → force_scores.json
    3) commands.txt(Cowork 발행) → research_agent deep 자동 실행 → DEEP_DONE.flag
    4) REPORT_DONE.flag → Apps Script 웹앱 메일 발송 → _archive 보관

  Cowork(Claude Desktop): COLLECT_DONE 감지 → 1차분석 → commands.txt 작성
                        → DEEP_DONE 감지 → 웹검색·악재검증 → 리포트 → report-done
```

- **메일 발송**: Google Apps Script 웹앱 (OAuth 만료 없음)
- **워처**: supervisor.py 하나로 통합 + watchdog.py(5분 주기 자가복구)
- **수집**: supervisor 가 .bat 없이 파이썬으로 직접 호출. collect 는 광역수집만
  (Gemini 본문복구는 DISABLE_GEMINI=1 로 꺼서 429 병목 회피. 분석은 Cowork 담당)
- **수집 시각**: supervisor.py 상단 `MORNING_TIME = "06:30"` 변수로 지정(저장 시 25초 내 반영)

작업 폴더: `C:\Users\USER\Desktop\stock_research`

---

# 0. 세팅 순서 요약

| 단계 | 한 번만? | 소요 |
|---|---|---|
| 1. Python 확인 | 한 번 | 1분 |
| 2. 패키지 설치 | 한 번 | 5분 |
| 3. API 키 파일 확인 | 한 번 | 2분 |
| 4. Cloudflare WARP / VPN | 항상 ON 권장 | 1분 |
| 5. **Apps Script 메일 발송 배포** | 한 번 | 5분 |
| 6. **appscript_config.txt 작성** | 한 번 | 1분 |
| 7. **supervisor 자동 시작 등록** | 한 번 | 2분 |
| 8. 동작 확인 | 한 번 | 2분 |
| 9. 매일 메일 받기 | 끝없이 | 0분 |

---

# 1. Python 확인

```powershell
python --version
```
`Python 3.10` 이상이면 OK. 없으면 https://www.python.org/downloads/ 에서 설치
("Add Python to PATH" 체크).

---

# 2. 패키지 설치

```powershell
cd C:\Users\USER\Desktop\stock_research
pip install -r requirements.txt
```

설치 확인:
```powershell
python -c "import requests, bs4, google.genai, pykrx, FinanceDataReader, markdown; print('OK')"
```
→ `OK` 나오면 통과. (Apps Script 발송으로 전환했으므로 google-api-python-client 등 Gmail
   OAuth 패키지는 더 이상 필수가 아니다.)

⚠️ `pip install` 차단 시: Cloudflare WARP 켜고 재시도, 또는 휴대폰 핫스팟.

### 셀레늄 봇우회(기본 ON) — 네이버 외 매체 본문 회수

네이버를 제외한 대부분 매체(이투데이·뉴시스·더벨·EBN 등)는 `requests` 를 403 으로 막는다.
이를 위해 수집 시 `undetected-chromedriver`(스텔스 크롬)로 본문을 재시도한다(기본 켜짐).

- 사전 조건: PC 에 **Google Chrome 설치**. Chrome 버전은 레지스트리에서 자동 탐지(고정 불필요).
- 설치: `pip install -r requirements.txt` 에 `selenium`, `undetected-chromedriver`,
  `setuptools<81`(Python 3.12 distutils 대체) 포함됨.
- 동작: requests/playwright 가 막힌 기사에 한해서만 폴백(느려서 폴백 전용). 단일 드라이버를
  Lock 으로 직렬화, 1회 수집당 60건 상한, 봇월(Cloudflare 챌린지) 못 뚫은 도메인은 즉시 스킵.
- 끄려면: 환경변수 `SELENIUM_USE=0`. Chrome/uc 미설치면 자동 비활성(graceful, 에러 없음).
- 확인: `python -c "import undetected_chromedriver, selenium; print('selenium OK')"`
- **목록(피드)도 Chromium 폴백:** RSS 피드 자체가 봇차단(Cloudflare 등)되면 requests 0건 →
  Chromium 으로 피드를 받아 파싱한다(`_fetch_raw_by_selenium`). 본문·목록 양쪽 다 우회.
- **수집 매체 추가/끄기:** `research_agent.py` 상단의 `RSS_SOURCES`(이름→RSS URL)와
  `SOURCE_TOGGLES`(이름→1 켜짐/0 꺼짐)에서 관리. 봇차단 매체도 그냥 추가하면 위 폴백이 처리.
  (기본 포함: 매일경제·한경컨센서스·연합인포맥스·MarketWatch·Bloomberg·Business Insider·
   Reuters·FT·Google News 등)
- **차단 시 빠른 패스:** 확정 차단/페이월(`access denied`, `subscribe to read`, `유료회원` 등
  `_HARD_BLOCK_MARKERS`)이 감지되면 셀레늄이 기다리지 않고 즉시 포기 + 그 도메인은 이번 수집
  동안 재시도 안 함. JS 챌린지(`Just a moment` 등)는 통과 가능성이 있어 최대 12초만 대기.
- **요약 전용 소스:** 본문이 확실히 페이월인 매체(`SUMMARY_ONLY_SOURCES` = Reuters·FT)는 본문
  크롤링을 생략하고 헤드라인+RSS요약만 즉시 수집(시간 낭비 0). 본문도 받고 싶으면 그 set 에서 빼라.

---

# 3. API 키 파일 확인

| 파일 | 내용 | 비고 |
|---|---|---|
| `gemini_keys.txt` | Gemini API 키(한 줄에 하나) | 수집·분석용 |
| `naver_api.txt` | `client_id=...` / `client_secret=...` | 네이버 검색 |
| `mail_config.txt` | `to = 수신자` (콤마로 여러 명) | 수신자만 쓰임 |
| `watch_tickers.txt` | force_analysis 종목 풀(6자리 코드) | 자유 편집 |
| `krx_tickers.json` | KRX 종목 캐시 | 자동 생성/갱신 |

`mail_config.txt` 예시 (Apps Script 체제에선 **to만 있으면 됨**):
```
to = student01@example.kr, friend@example.com
```

> 참고: `gmail_credentials.json`, `gmail_token.json` 은 **Apps Script 전환으로 더 이상
> 쓰지 않는다.** 지워도 무방하지만, 혹시 Gmail API로 되돌릴 가능성에 대비해 그냥 둬도
> 된다(작은 파일). 깔끔히 정리하려면 삭제 가능.

---

# 4. Cloudflare WARP / VPN

학교망·회사망 프록시 차단 환경이면 외부 사이트 접속이 막힌다.
- **Cloudflare WARP**(무료): https://1.1.1.1/  — 켜두면 대부분 통과
- KRX(`data.krx.co.kr`)는 가끔 차단 → force_analysis 수급 데이터만 빠짐(캐시로 폴백)
- RSS도 막히면 supervisor 가 자동으로 **api_collect.py**(Gemini+Naver API)로 폴백

확인:
```powershell
python research_agent.py test
```
→ "Naver 정상", "패키지 ... True" 보이면 핵심은 OK.
(Gemini 429 는 키 1개 테스트 한계라 무시. 실제 운영은 9키 로테이션)

---

# 5. ★ Apps Script 메일 발송 배포 (한 번만) ★

OAuth 토큰 7일 만료 문제를 영영 없애는 핵심 단계.

### ① 브라우저에서
1. https://script.google.com → **"새 프로젝트"**
2. 기본 코드 다 지우고 → 폴더의 **`mail_webapp.gs` 내용 전체 복사 붙여넣기**
3. 맨 위 `var SECRET_TOKEN = "CHANGE_ME_..."` 의 따옴표 안을 **본인만 아는 무작위 문자열**로
   변경 (영문+숫자, 32자 정도). 예: `var SECRET_TOKEN = "mystock2026KEYx7h3q9zR4";`
   → **이 값을 메모** (6단계에서 또 씀)
4. 우측 상단 **"배포" → "새 배포"**
   - 톱니바퀴 → **"웹 앱"**
   - 실행 주체: **나**
   - 액세스 권한: **모든 사용자**
   - **"배포"**
5. 권한 승인 → 본인 계정 → "고급" → "(프로젝트명)로 이동" → **Gmail 권한 허용**
   (이 승인이 유일한 인증이며 **만료되지 않음**)
6. 나오는 **"웹 앱 URL"** 복사
   (`https://script.google.com/macros/s/.../exec`)
7. 배포 확인: 그 URL을 브라우저로 열어 `{"ok":true,"status":"running"}` 보이면 정상

### ② 코드 수정 시 재배포
SECRET_TOKEN 등 바꾸면 → "배포 관리" → 기존 배포 연필(편집) → 버전 "새 버전" → 배포.
(URL 유지됨. "새 배포"로 또 만들면 URL이 새로 생기니 주의)

---

# 6. appscript_config.txt 작성 (한 번만)

stock_research 폴더에 **`appscript_config.txt`** 새 파일을 만들고:
```
appscript_url = (5단계에서 복사한 웹 앱 URL)
appscript_secret = (5단계 ③에서 정한 SECRET_TOKEN 과 똑같은 값)
```

⚠️ 메모장 저장 시 **인코딩 UTF-8**, 확장자가 `.txt` 인지 확인.

> supervisor 가 켜져 있으면 이 파일을 **25초 내 자동 감지**해서 발송을 활성화한다(재시작 불필요).

---

# 7. supervisor 자동 시작 (이미 구성됨)

자동 시작은 **두 가지**로 보장된다 (이미 세팅 완료 상태):

1. **로그온 시 보이는 창으로 자동 시작**
   - Windows 시작프로그램 폴더의 바로가기 `StockResearchSupervisor.lnk`
     → `start_supervisor_visible.bat` → `python supervisor.py`
   - 로그인하면 "StockResearch Supervisor (visible)" 콘솔 창이 떠서 활동이 보인다.

2. **죽거나 얼면 자동 복구**
   - 작업 스케줄러 `StockResearchWatchdog` (5분 주기) → `watchdog.py`
   - supervisor 가 죽었거나 하트비트가 멈추면(프리즈) 보이는 창으로 재가동.

### 지금 즉시 시작(또는 재시작)하려면
```
start_supervisor_visible.bat  더블클릭
```
또는 창에서: `python supervisor.py`

> 참고: 옛 숨김 작업 `StockResearchSupervisor`(task)는 비활성(Disabled)로 두었다.
> 보이는 창 방식을 쓰므로 그대로 두면 된다.

---

# 8. 동작 확인

```powershell
schtasks /query /tn "StockResearchSupervisor"     # "준비됨" 이면 등록 OK
Get-Content logs\supervisor.log -Tail 20          # 로그 확인
```

로그에 다음이 보이면 정상:
```
[..] [supervisor] supervisor 시작 (통합 상시 데몬)
[..] [supervisor]   종목 풀       : 58개
[..] [supervisor] ✅ 메일 설정 감지됨 — 발송 활성화 (수신: ...)
```

또는 친절한 점검:
```powershell
.\check_supervisor.bat
```

### 수집을 지금 한 번 강제 실행해보고 싶으면
```powershell
$env:DISABLE_GEMINI="1"; python research_agent.py collect --no-playwright
dir output\
```
→ `output\` 에 새 세션 폴더 + `01_broad_collection.md` + COLLECT_DONE.flag 생기면 OK.
   (force_scores.json 은 supervisor 가 켜져 있으면 잠시 후 자동 생성)

### 수집 시각을 바꿔 테스트하려면
`supervisor.py` 상단 `MORNING_TIME = "06:30"` 을 원하는 시각(예: `"14:05"`)으로 바꿔
저장 → supervisor 가 25초 내 자동 반영. (그날 이미 돌았으면
`supervisor_state.json` 의 `last_morning_run` 을 지워야 다시 돈다.)

---

# 9. 매일 운영 — 사용자가 하는 일

세팅 후 **아무것도 안 해도 됩니다.**

```
06:30  supervisor → 파이썬 직접 collect (RSS/Naver, 막히면 api_collect 폴백)
06:3x  supervisor → COLLECT_DONE → force_analysis → force_scores.json
07:0x  Cowork → COLLECT_DONE 감지 → 1차분석 → commands.txt
       supervisor → commands.txt 감지 → deep 자동 실행 → DEEP_DONE
07:1x  Cowork → DEEP_DONE 감지 → 웹검색·악재검증 → 리포트 → report-done
       supervisor → REPORT_DONE 감지 → Apps Script 메일 발송 → _archive 보관
```

가끔(주 1회) 점검:
```powershell
.\check_supervisor.bat
Get-Content logs\supervisor.log -Tail 30
```

종목 풀 변경: `watch_tickers.txt` 편집 → 저장만 하면 자동 반영.

Cowork 에는 매일 `cowork_instructions.md` 내용을 전달.

---

# 10. 트러블슈팅

| 증상 | 해결 |
|---|---|
| supervisor 가 안 켜짐 | `start_supervisor_visible.bat` 더블클릭(또는 재부팅). watchdog 이 5분 내 자동 복구도 함 |
| 06:30에 수집 안 됨 | `Get-Content logs\supervisor.log` + `Get-Content logs\morning_auto.log`. 절전 모드면 전원 옵션에서 끄기 |
| 수집이 느림(Gemini 429) | collect 는 DISABLE_GEMINI=1 로 Gemini 본문복구를 끄므로 빠름. 느리면 supervisor 새 코드인지 확인(재시작) |
| 수집 0건 (RSS 차단) | supervisor 가 자동으로 api_collect.py 폴백. WARP 켜두면 더 안정 |
| deep 안 돎(commands.txt 무시) | supervisor.log 에서 deep 워처 동작 확인. commands.txt 가 오늘자 세션에 있어야 함 |
| force_scores.json 안 생김 | watch_tickers.txt 확인. pykrx 미설치면 2단계 재설치 |
| 메일 안 감 ("메일 발송: 미설정") | appscript_config.txt 의 url/secret + mail_config.txt 의 to 확인 |
| 메일 발송 실패 (unauthorized) | Apps Script 의 SECRET_TOKEN 과 appscript_config.txt 의 appscript_secret 불일치. 5단계 재배포 또는 값 맞추기 |
| 메일 발송 실패 (non_json/로그인 페이지) | 웹앱 액세스 권한이 "모든 사용자"가 아님. 5단계 재배포 |
| 같은 리포트 중복 발송 | sent_index.json 이 막아줌. 그래도 의심되면 세션의 REPORT_DONE.flag 수동 삭제 |
| supervisor 두 개 켜짐 | supervisor.lock 이 막아줌(자동) |

---

# 11. 파일 구조 (정리 후)

```
stock_research/
├─ research_agent.py        ← 본체 (collect/deep/report-done/render_report_html)
├─ api_collect.py           ← RSS 차단 시 Gemini+Naver API 광역수집 폴백
├─ force_analysis.py        ← 세력강도 4축 분석 (KRX→Naver/FDR 폴백)
├─ count_articles.py        ← 세션 기사 수 카운트 (morning_postprocess 가 사용)
├─ morning_postprocess.py   ← collect 후 폴백 필요 판단 (--check-only)
├─ collection_report.py     ← 수집 점검 txt 저장 (collection_check/)
├─ watch_and_send.py        ← 메일 발송 로직 (supervisor 가 재사용)
├─ watch_and_analyze.py     ← force_analysis 실행 로직 (supervisor 가 재사용)
├─ recover.py               ← 수동 복구: 빠진 단계 자동 감지·채움
├─ supervisor.py            ← ★ 통합 상시 데몬 (MORNING_TIME 변수, deep 워처 포함)
├─ watchdog.py              ← supervisor 자가복구 (5분 주기 작업이 실행)
├─ start_supervisor_visible.bat ← 보이는 창으로 supervisor 시작 (시작프로그램 등록됨)
├─ check_supervisor.bat     ← supervisor 상태 점검
├─ mail_webapp.gs           ← Apps Script 에 붙여넣을 발송 코드
│
├─ gemini_keys.txt / naver_api.txt / mail_config.txt
├─ krx_account.txt          ← (선택) KRX 로그인 → 정밀 수급. 없으면 Naver 폴백
├─ appscript_config.txt     ← 웹앱 URL + secret
├─ watch_tickers.txt        ← force_analysis 종목 풀
├─ krx_tickers.json         ← KRX 캐시 (자동)
├─ requirements.txt / SETUP.md / cowork_instructions.md
│
├─ output/                  ← 세션 폴더 (자동) + _archive/
├─ collection_check/        ← 수집 점검 txt (자동)
├─ logs/                    ← supervisor.log / morning_auto.log / watch.log
│                              watch_analyzer.log / watchdog.log
├─ supervisor_state.json    ← 마지막 수집 실행일 (자동)
├─ supervisor_heartbeat.txt ← 하트비트 (자동, watchdog 가 봄)
└─ sent_index.json          ← 메일 발송 영구 기록 (중복방지, 자동)
```

---

# 12. 명령어 치트시트

```powershell
cd C:\Users\USER\Desktop\stock_research

# === supervisor 제어 ===
.\start_supervisor_visible.bat                    # 보이는 창으로 시작/재시작
.\check_supervisor.bat                            # 상태 점검(작업/프로세스/하트비트)
python supervisor.py --once --no-morning          # 1회 테스트(수집 제외)

# === 복구/진단 ===
python recover.py                                 # 빠진 단계 자동 감지·복구
python research_agent.py test                     # 키/패키지/네이버 점검
python force_analysis.py --market                 # 시장 우호도
python force_analysis.py --ticker 005930          # 특정 종목

# === 수동 수집/발송 ===
$env:DISABLE_GEMINI="1"; python research_agent.py collect --no-playwright  # 수집 1회
python watch_and_send.py --once                   # 발송 대기분 1회 처리

# === 로그 ===
Get-Content logs\supervisor.log -Tail 30
Get-Content logs\morning_auto.log -Tail 30
```

---

세팅이 끝나면 내일 아침부터 자동으로 돌아갑니다. 첫 며칠은 `logs\supervisor.log` 를
가끔 확인해 정상 동작을 검증하세요. 🚀
