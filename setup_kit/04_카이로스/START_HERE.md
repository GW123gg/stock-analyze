# 카이로스 전용 PC 셋업 — Claude Code 프롬프트

> **사용법**: 이 폴더(`kairos_setup`)를 전용 노트북에 복사 → **피코를 그 노트북 USB에 꽂고** →
> 노트북에서 Claude Code 실행 → **아래 내용을 그대로 붙여넣기**.
> (서버는 이제 이 노트북에서만 돈다. 원래 PC 의 서버·Funnel 은 껐다.)

---

이 노트북을 **미래에셋 카이로스 전용 PC**로 셋업해줘. 같은 폴더의 `kairos_pc.zip` 이 코드다(72파일).
목표 두 가지:
- **A) 매일 새벽 자동 로그인**
- **B) 상시 서버** — 이 노트북 화면(호가창)을 스트리밍하고, 다른 PC/아이폰에서 온 주문을 피코로 대신 넣는다.

아래 **하드윈 지식**을 먼저 이해하고 진행해. 안 지키면 반드시 실패한다.

## 0. 이 프로젝트가 뭔가

```
┌─ 사용자 PC / 아이폰 ────┐          ┌─ 이 노트북 (카이로스 전용) ────────┐
│  브라우저(웹UI)         │          │  FastAPI 서버 (포트 8443)          │
│   주문폼 ──────POST /order───────▶ │   ─▶ 피코 HID ─▶ [0611] 주문창     │
│   호가창 ◀────GET /stream.mjpg──── │   ◀─ 자기 화면 캡쳐                │
│   [공유 스위치] ─POST /capture/live▶│   캡쳐 스레드 시작/정지            │
│   [긴급정지] ──POST /emergency_stop▶│   주문 차단 + 미체결 취소          │
└─────────────────────────┘  HTTPS   └────────────────────────────────────┘
                        Tailscale Funnel
```
- 캡쳐되는 화면 = **서버가 실행 중인 PC(=이 노트북)의 화면**. 그래서 서버가 여기서 돌아야 한다.
- 로그인 = **공동인증서 로그인**: 비번을 Windows **keyring** → 클립보드 → **피코가 Ctrl+V** → Enter.

## 1. ★ 하드윈 지식 (필수 — 전부 실제로 겪은 것)

1. **합성입력 전면 차단** — 카이로스는 AhnLab/nProtect 보안모듈이 pyautogui·pywinauto 같은
   소프트웨어 가짜 키/마우스를 **전부 막는다**. **라즈베리 파이 피코(실물 USB HID)** 로만 통한다.
   (`server/control.py` 의 pyautogui 수동조작은 카이로스엔 **안 통함** — 구조화 주문폼 경로를 쓸 것)
2. **비번 출력/로그 금지.** **거래비밀번호·공동인증서는 자동입력 절대 금지.**
3. **kairos.exe 강제종료 불가** — 관리자 taskkill 로도 안 죽는다(커널 보호, 결과 128).
   → **닫으려면 OS 재부팅.** 그래서 매일 새벽 재부팅으로 초기화한다.
4. **kairos.exe 는 관리자 권한 필요** → 예약작업 **KairosLaunch**(최고권한)로 UAC 없이 실행.
   작업 생성만 관리자 1회, 실행(/Run)은 일반권한 OK.
5. **경고팝업 자가복구** — 빈 비번으로 로그인하면 **'공동인증비밀번호를 입력하여 주시기바랍니다'**
   팝업(제목 'Kairos', 작은 창)이 **비번칸을 덮어 악순환**이 된다.
   `login` 이 이걸 **감지 → Enter 로 닫고 → 재입력**한다(내장 재시도 루프).
6. **화면 배율 100% 필수** — 좌표가 실제 픽셀 기준.
7. **TOML 함정** — `config.example.toml` 의 `[dde.items]` 한글 키는 TOML 문법 위반이다.
   **`"현재가" = "..."` 처럼 반드시 따옴표.** 안 그러면 서버가 config 로드부터 실패한다.
8. **포트 8443 충돌** — 서버가 안 뜨면(errno 10048) 죽은 python 이 잡고 있는 것.
   `Get-NetTCPConnection -LocalPort 8443 -State Listen` 확인 후 그 python 만 종료.
   (tailscaled 도 8443 에 보이는데 tailnet IP 에만 묶인 거라 무관)
9. **.cmd 파일엔 한글 금지** — PowerShell 5.1/cp949 에서 깨져 문법 오류. 주석·echo 전부 영어로.
10. **Git Bash 에서 `cmd /c x.cmd` 금지** — MSYS 가 `/c` 를 `C:\` 로 바꾼다. PowerShell 로 실행.
11. **Funnel 은 덮어쓰기 조심** — 이미 다른 serve/funnel 설정이 있으면 공개 포트가 겹칠 수 있다.
    `--https=<공개포트>` 를 명시해 **항목을 추가**하고, 끌 때도 `--https=<포트> off` 로 그것만 지운다.

## 2. 압축 구조 (`kairos_pc.zip`, 72파일)

- **`cowork/`** — 자동로그인 번들(자기완결형)
  - `kairos_iscorrect.py` — 핵심 CLI: `run`(무인 전체)·`login`(자가복구)·`status`·`launch`·`setup`
  - `hid_serial.py`(피코 드라이버) · `pico_cli.py` · `capture.py`(화면캡쳐) · `notify.py`(오류메일)
  - `*.cmd` — python 풀경로 래퍼 · `kairos_login_coords.json` — 로그인 좌표
  - `setup_task.ps1` — **예약작업 4개 생성**(관리자 1회)
- **`server/`** — 상시 서버
  - `app.py`(인증·MJPEG스트림·/order·긴급정지·킬스위치·감사로그)
  - `pico_kairos_driver.py`(**피코로 [0611] 주문창 구동**, 기본 DRY-RUN, OCR 읽기검증)
  - `capture.py`(**화면공유 on/off**) · `account.py`(**enforce_cash 스위치**) · `safety.py` · `config.py`
- **`web/index.html`** — 웹 UI(호가창 + **화면공유 스위치** + 주문폼 + 긴급정지). 아이폰 사파리 → 홈화면 추가 = PWA
- **`start_server.cmd`** — **Tailscale Funnel + 서버** 한 번에
- **`stop_server.cmd`** — 서버 종료 + **우리 Funnel 항목만** 제거(다른 설정 안 건드림)
- **`tools/`** — `gen_token.py`(토큰 생성) · `pick_region.py`(호가창 영역) · `kairos_macro.py`(주문 좌표 보정)
- **`firmware/pico/code.py`** — 피코 CircuitPython 펌웨어
- `kairos_coords.json`([0611] 주문창 좌표) · `config.example.toml` · `requirements.txt`
- ※ `config.toml` 은 **없다**(비밀). B-1 에서 새로 만든다.

---

## A. 자동 로그인 셋업

**A-0. 압축 풀기** — 예: `C:\kairos` (이 경로를 기억)

**A-1. Python + 패키지** — Python 3.10+ → `pip install -r requirements.txt`

**A-2. ★ 경로 수정 (제일 먼저)**
원래 PC 경로 `C:\Users\USER\Desktop\mirae asset securities auto\...` 가 박혀 있다. 이 노트북 경로로 전부 치환:
- `cowork\*.cmd` (kairos/pico/notify/capture) — 스크립트 절대경로 + python 탐색 경로
- `start_server.cmd` / `stop_server.cmd` 의 **`PROJ`** 변수
- `cowork\setup_task.ps1` 의 여러 경로
- `cowork\kairos_login_coords.json` 의 `launch_path` (기본 `C:\미래에셋증권\카이로스\kairos.exe`)
- ※ `cowork\*.py`, `server\*.py` 는 `Path(__file__)` 기준이라 **수정 불필요**

**A-3. 피코** — CircuitPython + `adafruit_hid` + `firmware\pico\code.py`(→CIRCUITPY 의 `code.py`) 올리고 USB 연결.
(기존 피코를 옮겼으면 이미 올라가 있음. 포트는 VID 2E8A 자동탐색)

**A-4. 비밀번호 저장** (파일에 없음) — `python cowork\kairos_iscorrect.py setup`

**A-5. 로그인 좌표 재보정** (해상도/배율이 원본과 다르면 **필수**)
배율 100% 로 맞추고, 카이로스 로그인창을 띄운 뒤:
```
python cowork\kairos_iscorrect.py status      # 로그인폼 창 크기 확인
python cowork\capture.py --window Kairos      # 캡쳐해서 비번칸 위치 확인
python cowork\capture.py --list               # 창 목록/좌표
```
→ `cowork\kairos_login_coords.json` 의 `login_window`(크기범위)·`pw_field_offset` 갱신
- **원본 기준값**: 로그인폼 **420×530**, 비번칸 오프셋 **[122,255]**(창 좌상단 기준)

**A-6. Windows 설정**
- **재부팅 후 자동 로그인** 켜기 ← **제일 중요** (잠금화면이면 피코·캡쳐·실행 전부 불가)
- **클립보드 기록 OFF** — 비번이 잠깐 클립보드에 오름
- **디스플레이 배율 100%**
- **매일 04:45 재부팅** 예약 (카이로스 종료용)
- 카이로스를 **시작프로그램에 넣지 말 것**

---

## B. 서버 셋업 (호가창 + 원격 주문)

**B-1. config.toml**
```
copy config.example.toml config.toml
python tools\gen_token.py --write      # access_token 자동 생성·주입(백업+TOML검증)
python tools\gen_token.py --show       # 이 값을 웹/앱 '보안키' 칸에 입력
```
```toml
[server]   host="0.0.0.0"   port=8443
[auth]     cookie_secure=true          # Funnel(HTTPS) 뒤에선 true
[capture]  mode="region"  window_title="미래에셋"   # 좌표는 B-2
[trading]  live_enabled=false          # ★검증 전까지 반드시 false (DRY-RUN)
           max_order_qty=10  max_notional_krw=2000000  price_band_pct=5.0
[account]  cash_only=true              # 미수/융자/공매도 금지 (바꾸지 말 것)
           enforce_cash=1              # 1=예수금·매도가능 감시 / 0=감시 안 함(테스트용)
```
⚠️ `[dde.items]` 한글 키는 **따옴표 필수**(하드윈 7번).

**B-2. 호가창 캡쳐 영역** — 카이로스를 로그인시켜 **[0611] 주식주문** 창을 띄운 뒤:
```
python tools\pick_region.py       # 호가 부분 드래그 → 좌표를 config.toml [capture] 에 기입
```

**B-3. 서버 테스트 (Funnel 없이)**
```
start_server.cmd 8443 nofunnel
```
→ 브라우저 `http://127.0.0.1:8443` → 보안키 → **호가창이 보이면 성공**

**B-4. Tailscale Funnel (외부 접속)** — Tailscale 설치 + 로그인 후:
```
start_server.cmd            # funnel(공개443 → 로컬8443) + 서버
```
→ 나온 **`https://<기기>.<tailnet>.ts.net`** 이 외부 접속 주소.
- 내부: `tailscale funnel --bg --yes --https=443 8443` (`--yes` 라 예약작업에서도 프롬프트 없음)
- ⚠️ Funnel 은 **인터넷 공개** → 강한 access_token 필수. 처음이면 admin 콘솔에서 Funnel 활성화 필요.
- 내 기기끼리만 쓸 거면 `tailscale serve`(비공개)가 더 안전. 확인: `tailscale funnel status`
- 끄기: `stop_server.cmd` (서버 + 우리 funnel 항목만 제거)

**B-5. 예약작업 4개 생성** (관리자 PowerShell **1회**)
```
powershell -ExecutionPolicy Bypass -File "C:\kairos\cowork\setup_task.ps1"
```
| 작업 | 시각 | 역할 |
|---|---|---|
| KairosLaunch | (호출용) | 카이로스 관리자 실행 |
| KairosKill | (미사용) | — |
| **KairosAutoLogin** | **매일 05:00** | 실행 → 로그인 → 검증 → 실패 시 이메일 |
| **KairosServer** | **매일 05:00** | **Funnel + 서버** (재부팅까지, 크래시 시 3회 재시작) |

※ 예약작업은 **만든 권한으로만 수정 가능**하다. 관리자로 만들었으면 끄고 켜는 것도 관리자로.

---

## C. 하루 흐름 (완성 후)
```
04:45  재부팅 → Windows 자동로그인
05:00  ├ KairosAutoLogin → 카이로스 실행 + 로그인
       └ KairosServer    → Tailscale Funnel + 서버 → 재부팅까지 계속
사용자 → https://<기기>.ts.net → 보안키 → 호가창 보고 주문
```

## D. 명령어
```
python cowork\kairos_iscorrect.py status    # RESULT: NO_WINDOW / LOGIN_OR_STARTING / LOGGED_IN
python cowork\kairos_iscorrect.py run       # 무인 전체(실행→로그인→검증)
start_server.cmd 8443 nofunnel              # 서버만(로컬 테스트)
start_server.cmd                            # Funnel + 서버
stop_server.cmd                             # 서버 종료 + 우리 funnel 제거
schtasks /Run /TN KairosAutoLogin           # 예약작업 즉시 테스트
schtasks /Run /TN KairosServer
python tools\gen_token.py --show            # 보안키 확인
python cowork\notify.py --test              # 오류 알림 메일 테스트
tailscale funnel status                     # funnel 확인
```

## E. 실제 주문(LIVE) — 충분히 검증한 뒤에만
기본은 **DRY-RUN**(폼만 채우고 주문버튼 안 누름). 실거래 순서:
1. **주문창 좌표 재보정** — `python tools\kairos_macro.py` '한 칸 테스트'로
   탭/종목/수량/단가/주문버튼 확인 → `kairos_coords.json` 갱신
2. **거래비밀번호는 카이로스에 '저장'** — 드라이버는 거래비번을 **절대 자동입력하지 않는다**.
   [0611]에서 거래비번 저장을 켜 둘 것(프롬프트가 뜨면 드라이버가 중단)
3. **DRY-RUN 검증** — 웹에서 주문 전송 → 서버가 **폼만 채움** → 스트림으로 값 **눈으로 확인**
4. **읽기검증(OCR)** — `kairos_coords.json` 에 `verify_symbol`/`verify_qty`/`verify_price`
   = `[left,top,w,h]` 추가 + `pip install pytesseract` + Tesseract 바이너리
   → LIVE 는 **화면값이 요청과 일치할 때만** 주문버튼을 누른다
5. **LIVE 켜기** — `live_enabled=true`, **반드시 1주 등 소액부터** → 체결/주문내역 확인

## F. 🔒 안전 (항상)
- **완전 자동매매 루프 없음** — 매 주문 사람이 UI 에서 값 확인 후 전송
- 현금계좌만 · 수량/금액/가격밴드 한도 · 킬스위치/긴급정지 · 감사로그(`logs\audit.log`)
- `enforce_cash=0` 으로 꺼도 **cash_only·한도·킬스위치·DRY-RUN 은 살아있고**,
  최종적으로 **증권사가 예수금 부족/과매도를 거부**한다. 꺼지면 웹UI 에 경고 배너.
- **화면 공유 스위치**를 끄면 캡쳐 스레드가 멈춰 화면이 아예 안 나간다(마지막 프레임도 폐기).
- `config.toml`(access_token) · `cowork\notify.json`(메일 시크릿) 은 **비밀** — 공유/커밋 금지
- 브로커 인증서 비밀번호는 파일에 없다(keyring). 이 노트북에서 `setup` 으로 저장.

---

**먼저 압축 풀고 → A-2(경로 수정) 부터 시작해줘. 각 단계 끝나면 뭘 했는지 알려줘.**
막히기 쉬운 3곳: ①경로 수정 ②로그인 좌표 재보정(해상도 다르면) ③호가창 캡쳐 영역(이 노트북 화면에서 실측)
