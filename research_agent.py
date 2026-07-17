#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
research_agent.py  ─  헤드리스(터미널) 주식 뉴스 리서치 수집기

[설계 목표]
  - Streamlit UI 제거 → 순수 CLI. Claude(또는 cron)가 터미널에서 직접 실행.
  - API 키는 같은 폴더의 txt 파일에서 로드 (소스코드 하드코딩 금지).
  - KRX 종목 목록: 매 실행마다 신선 수집 시도 → 성공 시 캐시 저장 /
    실패 시 직전 캐시(json)로 폴백하여 분석 지속.
  - 결과를 구조화된 마크다운 파일로 output/ 폴더에 저장 → Claude가 읽고 분석.

[메일 발송 — 2중 구조]
  - 1순위 Gmail API (HTTPS/443) : Cowork 샌드박스에서도 통과 가능성 높음.
  - 2순위 SMTP (465)            : Windows 스케줄러 백업 발송용.
  - method=auto 면 API 먼저 시도 후 실패 시 SMTP 폴백.

[같은 폴더에 두어야 하는 파일]
  research_agent.py          ← 이 파일
  gemini_keys.txt            ← (직접 생성) Gemini API 키, 한 줄에 하나
  naver_api.txt              ← (직접 생성) 1행: client_id / 2행: client_secret
  mail_config.txt            ← (직접 생성) sender/app_password/to/method
  gmail_credentials.json     ← (직접 생성) Gmail API OAuth 클라이언트 (GCP 다운로드)
  gmail_token.json           ← (자동 생성) 최초 브라우저 인증 후 생성
  krx_tickers.json           ← (자동 생성) KRX 캐시
  output/                    ← (자동 생성) 수집 결과 마크다운
  logs/                      ← (자동 생성) 실행 로그

[사용법]
  python research_agent.py auto                 # 전체 파이프라인 (cron/routine용)
  python research_agent.py collect              # 광역 수집만
  python research_agent.py deep --stocks "삼성전자,SK하이닉스" \
                                --news "HBM 수요 2026" \
                                --naver "외국인 순매수,코스피 시황"
  python research_agent.py mail --session "폴더" --method auto   # 리포트+첨부 발송
  python research_agent.py mail --latest --method smtp --then-archive  # 스케줄러 백업
  python research_agent.py test                 # 키/네트워크/메일 연결 테스트
  python research_agent.py krx                   # KRX 종목 갱신만

옵션:
  --limit N        소스당 기사 수 (기본 12)
  --no-playwright  Step2 Playwright 비활성화 (cron 경량 실행 권장)
  --no-phase1      Gemini Phase1(명령어 자동발행) 생략, 광역 수집만
"""

import os
import re
import sys
import json
import html
import time
import random
import shutil
import base64
import logging
import argparse
import collections
import threading
import concurrent.futures
import urllib.parse
from datetime import datetime

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from bs4 import BeautifulSoup

# Windows에서 Playwright 멀티스레딩 NotImplementedError 방지
import asyncio
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

# ── 선택적 의존성 ──────────────────────────────────────────────
try:
    from google import genai as _genai_check          # noqa
    from google.genai import types as _genai_types_ck  # noqa
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

try:
    import trafilatura
    TRAF_AVAILABLE = True
except ImportError:
    TRAF_AVAILABLE = False

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    PW_AVAILABLE = True
except ImportError:
    PW_AVAILABLE = False
    PWTimeoutError = Exception

# undetected-chromedriver(셀레늄 스텔스) — requests/playwright 가 봇차단(403)당할 때 폴백.
# Python 3.12 는 distutils 가 빠져 setuptools 가 있어야 import 된다(없으면 SELENIUM_AVAILABLE=False).
try:
    import undetected_chromedriver as _uc
    SELENIUM_AVAILABLE = True
except Exception:
    SELENIUM_AVAILABLE = False

# curl_cffi — 실제 Chrome 의 TLS/JA3 지문을 위장해, 'requests 는 403 인데 브라우저면 통과'하는
# 봇월(Akamai/일부 Cloudflare 등)을 크로뮴(브라우저) 없이 우회. 미설치 시 graceful off.
#   pip install curl_cffi
try:
    from curl_cffi import requests as _cffi_requests
    CFFI_AVAILABLE = True
except Exception:
    CFFI_AVAILABLE = False

try:
    import yfinance as yf
    YF_AVAILABLE = True
except ImportError:
    YF_AVAILABLE = False

try:
    import FinanceDataReader as fdr
    FDR_AVAILABLE = True
except ImportError:
    FDR_AVAILABLE = False

# ── Gmail API (선택적) ─────────────────────────────────────────
#   pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
try:
    from google.oauth2.credentials import Credentials as _GCreds
    from google_auth_oauthlib.flow import InstalledAppFlow as _GFlow
    from google.auth.transport.requests import Request as _GRequest
    from googleapiclient.discovery import build as _gbuild
    GMAIL_API_AVAILABLE = True
except ImportError:
    GMAIL_API_AVAILABLE = False


# =====================================================================
# 0. 경로 설정 — 모든 파일을 스크립트와 같은 폴더 기준으로
# =====================================================================
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
GEMINI_KEY_FILE = os.path.join(BASE_DIR, "gemini_keys.txt")
NAVER_KEY_FILE  = os.path.join(BASE_DIR, "naver_api.txt")
KRX_CACHE_FILE  = os.path.join(BASE_DIR, "krx_tickers.json")
OUTPUT_DIR      = os.path.join(BASE_DIR, "output")
LOG_DIR         = os.path.join(BASE_DIR, "logs")
# (선택) 분석가 시스템 프롬프트를 별도 파일로 두면 그 내용을 INSTRUCTIONS에 사용.
# 없으면 코드 내장 ANALYST_PROMPT 사용.
ANALYST_PROMPT_FILE = os.path.join(BASE_DIR, "analyst_prompt.txt")
# 메일 발송 설정 (SMTP/Gmail API 공통). 형식은 load_mail_config 참고.
MAIL_CONFIG_FILE = os.path.join(BASE_DIR, "mail_config.txt")
# Gmail API OAuth 파일
GMAIL_CREDENTIALS_FILE = os.path.join(BASE_DIR, "gmail_credentials.json")
GMAIL_TOKEN_FILE       = os.path.join(BASE_DIR, "gmail_token.json")
GMAIL_SCOPES           = ["https://www.googleapis.com/auth/gmail.send"]

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)


# Windows 콘솔(cp949)에서 한글·이모지 출력 시 UnicodeEncodeError 방지.
# stdout/stderr 를 UTF-8 로 재설정 (Python 3.7+ reconfigure).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# =====================================================================
# 1. 로깅 — 콘솔 + 일자별 파일
# =====================================================================
def setup_logger() -> logging.Logger:
    lg = logging.getLogger("research_agent")
    if lg.handlers:
        return lg
    lg.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    lg.addHandler(sh)

    log_path = os.path.join(LOG_DIR, f"run_{datetime.now().strftime('%Y%m%d')}.log")
    try:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
        lg.addHandler(fh)
    except Exception:
        pass  # 파일 핸들러 실패해도 콘솔 로깅은 유지
    return lg


log = setup_logger()


# =====================================================================
# ★★★★★  사용자 설정 — 이 구역만 수정하면 됩니다  ★★★★★
# =====================================================================

# ── (1) 소스당 수집 기사 수 ──────────────────────────────────────
#   값을 바꾸면 모든 소스에 동일 적용됩니다. (예: 8, 12, 20 …)
ARTICLES_PER_SOURCE = 12

# ── (2) Playwright(Step2) 사용 여부 ──────────────────────────────
#   1 = 사용(봇 우회 강력하지만 느림) / 0 = 미사용(빠르고 안정, cron 권장)
USE_PLAYWRIGHT = 0

# 셀레늄(undetected-chromedriver) 봇차단 우회 폴백 사용 여부.
# 기본 ON. 네이버 외 대부분 매체가 requests 를 403 차단하므로, requests/playwright 가
# 막힌 기사에 한해 스텔스 브라우저로 본문을 재시도(폴백 전용, 느림). Chrome/uc 미설치 시
# SELENIUM_AVAILABLE=False 로 자동 비활성(graceful). 끄려면 환경변수 SELENIUM_USE=0.
# (기본 ON 이라, 데몬 재기동 없이도 collect 서브프로세스가 곧장 셀레늄을 쓴다.)
SELENIUM_USE = (os.getenv("SELENIUM_USE", "1") != "0")

# ── (3) 비-크로뮴 우회 폴백 토글 ──────────────────────────────────
#   크로뮴(Playwright/undetected-chromedriver)만으로 못 뚫거나 너무 느린 경우를 위해,
#   '브라우저 없이' 우회하는 두 기법을 폴백 체인에 추가했다.
#     CFFI_USE   : curl_cffi 로 실제 Chrome 의 TLS 지문을 위장(빠름). requests 가 막힌 기사에
#                  바로 재시도 → Akamai/봇월을 크로뮴 없이 통과. 기본 ON. 끄려면 CFFI_USE=0.
#     READER_USE : r.jina.ai 리더 프록시(서버측에서 렌더·정제한 텍스트)로 페이월/봇월 기사를
#                  Gemini(AI 추정) 전 '마지막 비-AI 폴백'으로 회수. 기본 ON. 끄려면 READER_USE=0.
CFFI_USE   = (os.getenv("CFFI_USE", "1") != "0")
READER_USE = (os.getenv("READER_USE", "1") != "0")

# ── (3) 수집 소스 ON/OFF 토글 ────────────────────────────────────
#   값이 1이면 그 사이트에서 수집, 0이면 건너뜁니다.
#   원하는 사이트만 1로 켜고 나머지는 0으로 끄세요.
SOURCE_TOGGLES = {
    # ─ 네이버 뉴스 섹션(국내) ─
    "네이버 증권":        1,
    "네이버 금융":        1,
    "네이버 글로벌경제":  1,
    "네이버 IT/과학":     1,   # section/105
    "네이버 세계":        1,   # section/104
    "네이버 경제일반":    1,   # breakingnews 101/263
    "네이버 산업/재계":   1,   # breakingnews 101/261
    "네이버 중기/벤처":   1,   # breakingnews 101/771
    "매경 이코노미":      1,   # list.naver oid=024 (레거시 .type06)
    "한경 비즈니스":      1,   # list.naver oid=050 (레거시 .type06)
    # ─ RSS 매체 ─
    "KR (매일경제)":      1,
    "KR (Google News)":   1,
    "KR (글로벌 매크로)": 1,
    "KR (한경 컨센서스)": 1,
    "KR (연합인포맥스)":  1,
    "US (Bloomberg)":     1,
    "US (Business Insider)": 1,
    "US (MarketWatch)":   1,
    "US (Reuters)":       1,
    "US (FT)":            1,   # 본문 페이월 多 → 헤드라인/요약 위주(차단 시 즉시 패스)
    "US (CNBC)":          0,   # ← 예시: CNBC는 기본 OFF
}

# 본문이 확정 페이월/하드차단이라 '크롤링해도 어차피 막히는' 소스.
# 이런 소스는 본문 시도를 아예 생략하고 헤드라인+RSS요약만 즉시 쓴다(= 바로 패스, 시간 절약).
# (미지의 사이트는 여기 없어도 자동 차단감지로 빠르게 패스됨 — _looks_hard_blocked.)
SUMMARY_ONLY_SOURCES = {
    "US (Reuters)",   # reuters.com 본문 페이월/봇차단
    "US (FT)",        # ft.com 본문 하드 페이월
}

# =====================================================================
# ★★★★★  여기까지 사용자 설정  ★★★★★
# =====================================================================


# ── 소스 정의 (URL 매핑 — 토글 키와 이름이 1:1 대응) ───────────────
NAVER_SECTIONS = {
    "네이버 증권":       "https://news.naver.com/breakingnews/section/101/258",
    "네이버 금융":       "https://news.naver.com/breakingnews/section/101/259",
    "네이버 글로벌경제": "https://news.naver.com/breakingnews/section/101/262",
    # ── 모던 섹션(div.sa_text 레이아웃 — collect_naver_section 기본 파싱) ──
    "네이버 IT/과학":    "https://news.naver.com/section/105",
    "네이버 세계":       "https://news.naver.com/section/104",
    "네이버 경제일반":   "https://news.naver.com/breakingnews/section/101/263",
    "네이버 산업/재계":  "https://news.naver.com/breakingnews/section/101/261",
    "네이버 중기/벤처":  "https://news.naver.com/breakingnews/section/101/771",
    # ── 언론사별 list.naver(레거시 .type06 레이아웃 — collect_naver_section 가 자동 폴백 파싱) ──
    "매경 이코노미":     "https://news.naver.com/main/list.naver?mode=LPOD&mid=sec&oid=024",
    "한경 비즈니스":     "https://news.naver.com/main/list.naver?mode=LPOD&mid=sec&oid=050",
}

RSS_SOURCES = {
    "KR (매일경제)":      "https://www.mk.co.kr/rss/50300009/",
    "KR (Google News)":  "https://news.google.com/rss/search?q=주식+when:1d&hl=ko&gl=KR&ceid=KR:ko",
    "KR (글로벌 매크로)": "https://kr.investing.com/rss/news_285.rss",
    "KR (한경 컨센서스)": "https://rss.hankyung.com/new/news_consensus.xml",
    "KR (연합인포맥스)":  "https://news.einfomax.co.kr/rss/allArticle.xml",
    "US (Bloomberg)":    "https://news.google.com/rss/search?q=site:bloomberg.com+when:1d&hl=en-US&gl=US&ceid=US:en",
    "US (Business Insider)": "https://news.google.com/rss/search?q=site:businessinsider.com+(stock+OR+chip+OR+market+OR+earnings)+when:2d&hl=en-US&gl=US&ceid=US:en",
    "US (MarketWatch)":  "https://news.google.com/rss/search?q=site:marketwatch.com+(stock+OR+market+OR+earnings+OR+Fed)+when:2d&hl=en-US&gl=US&ceid=US:en",
    "US (Reuters)":      "https://news.google.com/rss/search?q=site:reuters.com+when:1d&hl=en-US&gl=US&ceid=US:en",
    "US (FT)":           "https://www.ft.com/rss/home",
    "US (CNBC)":         "https://news.google.com/rss/search?q=site:cnbc.com+when:1d&hl=en-US&gl=US&ceid=US:en",
}


def is_source_on(name: str) -> bool:
    """토글 맵에서 해당 소스가 켜져 있는지(1) 확인. 미정의 시 기본 ON."""
    return int(SOURCE_TOGGLES.get(name, 1)) == 1


BLOCK_KEYWORDS = [
    "캡차", "captcha", "보안 문자", "access denied", "403 forbidden", "blocked",
    "enable javascript", "자바스크립트를 활성화", "robot", "bot detected", "ddos",
    "cloudflare", "just a moment", "checking your browser", "please wait", "잠시 후 다시",
]

# Gemini 모델 폴백 체인
_GEMINI_MODEL_PRIMARY   = "gemini-2.5-flash"
_GEMINI_MODEL_FALLBACKS = [
    "gemini-2.5-flash-lite", "gemini-flash-latest",
    "gemini-2.0-flash", "gemini-2.0-flash-001",
]

# 타임아웃
_GEMINI_API_TIMEOUT_SEC  = 30
_PW_STEP2_TIMEOUT_SEC     = 22
_SINGLE_ARTICLE_WALL_SEC  = 55
_PW_GOTO_TIMEOUT_MS       = 15_000
_BATCH_SIZE               = 5
_SELENIUM_STEP_TIMEOUT_SEC = 40   # 셀레늄 한 기사 본문 가져오기 최대 대기
# 설치된 Chrome 메이저 버전. undetected-chromedriver 는 이 버전과 맞춰야 'session not
# created' 없이 뜬다. 환경변수 CHROME_MAJOR 로 덮어쓸 수 있고, 미지정 시 자동탐지(None).
try:
    _CHROME_MAJOR = int(os.getenv("CHROME_MAJOR", "0")) or None
except Exception:
    _CHROME_MAJOR = None
_SELENIUM_DRIVER = None   # 드라이버 1개 재사용(매 기사 새로 띄우면 너무 느림)
# 드라이버는 1개뿐인데 process_single_article 은 스레드풀로 병렬 실행된다.
# Chrome 세션 1개에 여러 스레드가 동시에 .get() 하면 충돌하므로 셀레늄 호출을 직렬화.
_SELENIUM_LOCK = threading.Lock()
# Cloudflare 등 JS 챌린지 자동통과 폴링 최대 대기. 락을 쥐는 시간이므로 짧게.
# 헤드리스로 통과 가능한 챌린지는 보통 <8초; 25초+는 어차피 못 뚫는 하드 Cloudflare라 낭비.
_SELENIUM_CHALLENGE_WAIT_SEC = 12
# 셀레늄은 직렬화돼 기사당 ~7초. 막힌 기사가 많으면 수 분 추가될 수 있어 1회 수집당
# 호출 상한을 둔다(폭주 방지). 초과분은 셀레늄 생략(Gemini/RSS 폴백). 0이면 무제한.
_SELENIUM_MAX_CALLS = int(os.getenv("SELENIUM_MAX_CALLS", "60"))
_SELENIUM_CALLS = 0     # 현재 수집 실행에서 쓴 셀레늄 호출 수(quit 시 리셋)
_SELENIUM_CAP_LOGGED = False
# 봇월을 못 뚫은 도메인은 같은 수집 실행 내에서 더는 셀레늄 시도 안 함(빠른 실패).
# investing.com 처럼 반복적으로 25초씩 낭비되는 걸 막는다. quit 시 리셋.
_SELENIUM_BAD_DOMAINS = set()
# 봇차단/JS 챌린지 안내 페이지의 사람이 읽는 문구(추출 본문에서 검사). 이게 본문이면
# 진짜 기사가 아니라 봇월이므로 본문 인정하지 않고 ''(다음 폴백)으로 넘긴다.
_BOT_WALL_MARKERS = (
    "just a moment", "enable javascript and cookies", "checking your browser",
    "verifying you are human", "attention required", "please verify you are a human",
    "보안 확인", "악의적인 봇", "사람인지 확인", "응답을 기다리는 중", "잠시만 기다리",
)
_BATCH_INTER_DELAY_SEC    = 4.5


def _looks_like_bot_wall(text: str) -> bool:
    """추출 본문이 봇차단/JS 챌린지 안내문이면 True."""
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in _BOT_WALL_MARKERS)


# 확정 차단/페이월 문구 — 기다려도 안 풀린다(JS 챌린지와 달리 시간이 해결 못 함).
# 이게 보이면 셀레늄이 데드라인까지 기다리지 말고 '즉시 포기 + 도메인 스킵'(= 빠른 패스).
# 정상 기사/목록에는 거의 안 나오는 명확한 표현만(거짓양성 방지).
_HARD_BLOCK_MARKERS = (
    "access denied", "you have been blocked", "are you a robot", "are you human",
    "attention required", "403 forbidden", "error 1020", "error 1015", "error 1009",
    "subscribe to continue reading", "subscribe to read", "sign in to read",
    "register to continue reading", "to continue reading this article",
    "this article is for subscribers", "to read the full article",
    "유료회원", "유료 기사", "로그인 후 이용", "구독 후 이용", "구독자 전용",
)


def _looks_hard_blocked(text: str) -> bool:
    """확정 차단/페이월이면 True → 셀레늄은 더 기다리지 말고 즉시 패스."""
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in _HARD_BLOCK_MARKERS)


# =====================================================================
# 3. 키 파일 로딩
# =====================================================================
def load_gemini_keys() -> list:
    """gemini_keys.txt — 한 줄에 하나의 키 (# 주석/빈 줄 무시, 콤마도 허용).
    환경변수 DISABLE_GEMINI=1 이면 빈 리스트를 반환해 Gemini 사용을 끈다.
    (핸드셰이크 모드의 collect 는 광역수집만 하고 분석은 Cowork 가 하므로,
     본문 AI복구용 Gemini 호출을 꺼서 429 병목을 피한다. 평소엔 영향 없음.)"""
    if os.getenv("DISABLE_GEMINI") == "1":
        log.info("[Gemini] DISABLE_GEMINI=1 — Gemini 비활성화(키 0개 반환)")
        return []
    if not os.path.exists(GEMINI_KEY_FILE):
        log.warning(f"Gemini 키 파일 없음: {GEMINI_KEY_FILE}")
        return []
    keys = []
    with open(GEMINI_KEY_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for part in re.split(r"[,\s]+", line):
                part = part.strip()
                if part and not part.startswith("#"):
                    keys.append(part)
    # 중복 제거(순서 유지)
    seen, uniq = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


def load_naver_keys() -> tuple:
    """
    naver_api.txt 포맷(둘 다 지원):
      A) 1행: client_id / 2행: client_secret
      B) client_id=xxxx / client_secret=yyyy (key=value, 순서 무관)
    Returns: (client_id, client_secret)  실패 시 ("","")
    """
    if not os.path.exists(NAVER_KEY_FILE):
        log.warning(f"Naver 키 파일 없음: {NAVER_KEY_FILE}")
        return "", ""
    cid = csec = ""
    raw_lines = []
    with open(NAVER_KEY_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            raw_lines.append(line)

    # key=value 형식 우선 탐지
    kv = {}
    for line in raw_lines:
        m = re.match(r"(?i)\s*(client[_\s]?id|client[_\s]?secret|id|secret)\s*[=:]\s*(.+)", line)
        if m:
            key = re.sub(r"[\s_]", "", m.group(1)).lower()
            val = m.group(2).strip().strip('"\'')
            kv[key] = val
    if kv:
        cid  = kv.get("clientid") or kv.get("id") or ""
        csec = kv.get("clientsecret") or kv.get("secret") or ""

    # 줄 순서 형식 폴백
    if not (cid and csec):
        plain = [l for l in raw_lines if "=" not in l and ":" not in l]
        if len(plain) >= 2:
            cid, csec = plain[0], plain[1]
        elif len(plain) == 1 and raw_lines:
            # 한 줄만 있고 다른 줄이 kv였을 수도
            cid = cid or plain[0]

    return cid.strip(), csec.strip()


def validate_naver_credentials(cid: str, csec: str) -> tuple:
    if not cid or not csec:
        return False, "Client ID/Secret 미입력"
    if len(cid) < 5 or len(csec) < 5:
        return False, "길이 부족(잘못 복사 가능성)"
    if re.search(r"\s", cid) or re.search(r"\s", csec):
        return False, "공백 포함 — 다시 복사"
    return True, ""


# =====================================================================
# 3-b. 메일 발송 — 2중 구조 (Gmail API 1순위 + SMTP 2순위)
# =====================================================================
def load_mail_config() -> dict:
    """
    mail_config.txt 를 읽어 메일 발송 설정을 반환.
    형식 (key=value, 한 줄에 하나):
        sender   = your_id@gmail.com
        app_password = abcd efgh ijkl mnop   (Gmail 16자리 앱 비밀번호; SMTP용)
        to       = receiver@example.com       (콤마로 여러 명 가능)
        method   = auto                       (auto|api|smtp, 기본 auto)
        smtp_host = smtp.gmail.com             (선택, 기본 gmail)
        smtp_port = 465                        (선택, 기본 465=SSL)
    Returns dict 또는 {} (파일 없거나 불완전 시)
    """
    if not os.path.exists(MAIL_CONFIG_FILE):
        return {}
    cfg = {}
    with open(MAIL_CONFIG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"\s*([A-Za-z_]+)\s*[=:]\s*(.+)", line)
            if m:
                cfg[m.group(1).strip().lower()] = m.group(2).strip()
    # 앱 비밀번호의 공백 제거 (Gmail은 표시상 4자리씩 띄어줌)
    if "app_password" in cfg:
        cfg["app_password"] = cfg["app_password"].replace(" ", "")
    cfg.setdefault("smtp_host", "smtp.gmail.com")
    cfg.setdefault("smtp_port", "465")
    cfg.setdefault("method", "auto")
    return cfg


def validate_mail_config(cfg: dict, method: str = "auto") -> tuple:
    """
    메일 설정 검증. method 에 따라 요구 항목이 다르다.
      - api  : sender, to 만 있으면 됨 (인증은 gmail_token.json/credentials)
      - smtp : sender, to, app_password(16자리) 필요
      - auto : sender, to 필수 + app_password는 SMTP 폴백 시 권장(없어도 경고만)
    """
    if not cfg:
        return False, "mail_config.txt 없음 또는 비어 있음"
    for k in ("sender", "to"):
        if not cfg.get(k):
            return False, f"'{k}' 누락"
    if "@" not in cfg["sender"]:
        return False, "sender 형식 오류"
    if method == "smtp":
        if not cfg.get("app_password"):
            return False, "'app_password' 누락 (SMTP 발송에 필요)"
        if len(cfg["app_password"]) < 12:
            return False, "app_password 길이 부족 (Gmail 16자리 앱 비밀번호 확인)"
    return True, ""


def _build_mime_message(sender: str, recipients: list, subject: str,
                        body: str, attachments=None, html_body: str = ""):
    """
    SMTP/Gmail API 공용 MIME 메시지 빌더 (한글 파일명 RFC2231 인코딩).
    html_body 가 있으면 multipart/alternative 로 plain + html 둘 다 넣어
    Gmail에서 HTML(표·제목 등)이 렌더링되게 한다. plain은 폴백용.
    """
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email import encoders
    from email.utils import formataddr

    msg = MIMEMultipart("mixed")
    msg["From"] = formataddr(("리서치 에이전트", sender))
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject

    if html_body:
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body or "리포트를 확인하세요.", "plain", "utf-8"))
        alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)
    else:
        msg.attach(MIMEText(body, "plain", "utf-8"))

    for path in (attachments or []):
        if not path or not os.path.exists(path):
            log.warning(f"[메일] 첨부 파일 없음(건너뜀): {path}")
            continue
        try:
            with open(path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            fname = os.path.basename(path)
            part.add_header("Content-Disposition", "attachment",
                            filename=("utf-8", "", fname))
            msg.attach(part)
            log.info(f"[메일] 첨부 추가: {fname}")
        except Exception as e:
            log.warning(f"[메일] 첨부 실패({path}): {e}")
    return msg


def _mail_recipients(cfg: dict) -> list:
    return [r.strip() for r in re.split(r"[,;]", cfg.get("to", "")) if r.strip()]


def send_email_smtp(subject: str, body: str, attachments=None, cfg=None,
                    html_body: str = "") -> tuple:
    """
    SMTP(465 SSL)로 메일을 직접 발송. Windows 스케줄러 백업 발송 경로.
    Returns (성공여부:bool, 메시지:str)
    """
    import smtplib

    if cfg is None:
        cfg = load_mail_config()
    ok, why = validate_mail_config(cfg, method="smtp")
    if not ok:
        return False, f"메일 설정 오류: {why}"

    sender = cfg["sender"]
    pw = cfg["app_password"]
    recipients = _mail_recipients(cfg)
    host = cfg.get("smtp_host", "smtp.gmail.com")
    port = int(cfg.get("smtp_port", "465"))

    msg = _build_mime_message(sender, recipients, subject, body, attachments, html_body)

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls()
        try:
            server.login(sender, pw)
            server.sendmail(sender, recipients, msg.as_string())
        finally:
            server.quit()
        log.info(f"[메일/SMTP] ✅ 발송 완료 → {', '.join(recipients)}")
        return True, f"발송 완료(SMTP): {', '.join(recipients)}"
    except smtplib.SMTPAuthenticationError:
        return False, ("SMTP 인증 실패 — Gmail 앱 비밀번호 확인 "
                       "(2단계 인증 켜고 앱 비밀번호 생성 필요)")
    except Exception as e:
        return False, f"SMTP 발송 실패: {type(e).__name__}: {e}"


def _gmail_load_credentials(allow_browser: bool = True):
    """
    gmail_token.json 로드 → 만료 시 refresh → (allow_browser면) 브라우저 OAuth 플로우.
    Returns (creds, err_str). 성공 시 err_str="".

    allow_browser:
      True  → 유효한 토큰이 없으면 브라우저 인증창을 띄운다(본인 PC 최초 1회용).
      False → 브라우저를 절대 띄우지 않는다. 토큰 갱신까지만 시도하고
              안 되면 즉시 실패 반환(헤드리스/스케줄러용 — 멈춤 방지).
    """
    if not os.path.exists(GMAIL_CREDENTIALS_FILE):
        return None, f"gmail_credentials.json 없음: {GMAIL_CREDENTIALS_FILE}"

    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        try:
            creds = _GCreds.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
        except Exception as e:
            log.warning(f"[Gmail API] 토큰 로드 실패: {e}")
            creds = None

    if creds and creds.valid:
        return creds, ""

    # 만료 + refresh_token 있으면 갱신 (헤드리스에서도 가능 — 브라우저 불필요)
    if creds and getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
        try:
            creds.refresh(_GRequest())
            _gmail_save_token(creds)
            return creds, ""
        except Exception as e:
            log.warning(f"[Gmail API] 토큰 갱신 실패 → 재인증 필요: {e}")
            creds = None

    # 비대화형(스케줄러)에서는 여기서 멈추지 않고 즉시 실패 — 인증창 대기로 멈추는 사고 방지
    if not allow_browser:
        return None, ("Gmail 토큰 없음/만료(갱신 불가) — 비대화형 모드. "
                      "본인 PC에서 'python research_agent.py mail --method api ...' "
                      "(--no-browser 없이) 1회 실행해 gmail_token.json 을 새로 만드세요. "
                      "[참고] OAuth 앱이 '테스트' 상태면 refresh_token 이 7일 후 만료됩니다 → "
                      "GCP 동의화면을 '프로덕션(게시)'으로 전환 권장.")

    # 최초 인증 (브라우저 필요) — 본인 PC에서 1회만 수행
    try:
        flow = _GFlow.from_client_secrets_file(GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES)
        creds = flow.run_local_server(port=0)
        _gmail_save_token(creds)
        return creds, ""
    except Exception as e:
        return None, (f"OAuth 인증 실패: {type(e).__name__}: {e} "
                      f"(브라우저 인증이 필요합니다. 본인 PC에서 "
                      f"'python research_agent.py mail --method api ...' 1회 실행)")


def _gmail_save_token(creds):
    try:
        with open(GMAIL_TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
        log.info(f"[Gmail API] 토큰 저장 → {GMAIL_TOKEN_FILE}")
    except Exception as e:
        log.warning(f"[Gmail API] 토큰 저장 실패: {e}")


def send_email_gmail_api(subject: str, body: str, attachments=None, cfg=None,
                         allow_browser: bool = True, html_body: str = "") -> tuple:
    """
    Gmail API(HTTPS/443)로 메일 발송. Cowork 샌드박스 우회 1순위 경로.
    gmail_credentials.json + gmail_token.json 필요.
    allow_browser=False 면 토큰 없을 때 인증창을 띄우지 않고 즉시 실패(스케줄러용).
    Returns (성공여부:bool, 메시지:str)
    """
    if not GMAIL_API_AVAILABLE:
        return False, ("Gmail API 패키지 미설치 — "
                       "pip install google-api-python-client "
                       "google-auth-httplib2 google-auth-oauthlib")
    if cfg is None:
        cfg = load_mail_config()
    ok, why = validate_mail_config(cfg, method="api")
    if not ok:
        return False, f"메일 설정 오류: {why}"

    creds, err = _gmail_load_credentials(allow_browser=allow_browser)
    if not creds:
        return False, err

    sender = cfg["sender"]
    recipients = _mail_recipients(cfg)
    msg = _build_mime_message(sender, recipients, subject, body, attachments, html_body)

    try:
        service = _gbuild("gmail", "v1", credentials=creds, cache_discovery=False)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        log.info(f"[메일/API] ✅ 발송 완료 → {', '.join(recipients)}")
        return True, f"발송 완료(Gmail API): {', '.join(recipients)}"
    except Exception as e:
        return False, f"Gmail API 발송 실패: {type(e).__name__}: {e}"


def send_email_appscript(subject: str, html_body: str, cfg=None) -> tuple:
    """
    Google Apps Script 웹앱으로 발송 (데몬 watch_and_send 와 동일 경로).
      - gmail_credentials.json(API)·SMTP app_password 가 없어도 발송된다.
      - 필요 설정: appscript_config.txt (appscript_url·appscript_secret) + mail_config.txt 의 to(수신자).
      - 첨부는 지원하지 않는다(HTML 본문만; scan_once 와 동일 payload).
    Returns (성공:bool, 메시지:str).
    """
    if cfg is None:
        cfg = load_mail_config()
    try:
        import watch_and_send as _wsend
    except Exception as e:
        return False, f"watch_and_send import 실패: {type(e).__name__}: {e}"
    try:
        url, secret = _wsend.load_appscript_config()
    except Exception as e:
        return False, f"appscript_config 로드 실패: {type(e).__name__}: {e}"
    if not url or not secret:
        return False, "appscript_config.txt 미설정(appscript_url/appscript_secret)"
    to = (cfg.get("to") or "").strip()
    if not to:
        return False, "수신자(mail_config.txt 의 to) 미설정"
    payload = {
        "secret": secret,
        "to": to,
        "subject": _wsend._strip_non_bmp(subject),
        "htmlBody": _wsend._strip_non_bmp(html_body or ""),
    }
    ok, info = _wsend.post_to_appscript(url, payload)
    return (True, f"appscript 발송 성공: {info}") if ok else (False, f"appscript 발송 실패: {info}")


def send_email(subject: str, body: str, attachments=None, cfg=None, method=None,
               allow_browser: bool = True, html_body: str = "") -> tuple:
    """
    발송 디스패처.
      method=api       → Gmail API 만
      method=smtp      → SMTP 만
      method=appscript → Apps Script 웹앱만 (자격증명 불필요, 첨부 없음)
      method=auto      → Gmail API → SMTP → Apps Script 순 폴백 (기본)
    allow_browser=False 면 Gmail API 토큰 부재 시 인증창 없이 즉시 실패(스케줄러용).
    html_body 가 있으면 HTML 본문(표·제목 렌더링)으로 발송.
    Returns (성공여부:bool, 메시지:str)
    """
    if cfg is None:
        cfg = load_mail_config()
    if method is None or method == "":
        method = cfg.get("method", "auto")
    method = (method or "auto").lower()

    if method == "smtp":
        return send_email_smtp(subject, body, attachments, cfg, html_body)
    if method == "api":
        return send_email_gmail_api(subject, body, attachments, cfg, allow_browser, html_body)
    if method == "appscript":
        return send_email_appscript(subject, html_body, cfg)

    # auto: API 우선 → SMTP 폴백 → Apps Script 폴백
    ok, msg = send_email_gmail_api(subject, body, attachments, cfg, allow_browser, html_body)
    if ok:
        return True, msg
    log.warning(f"[메일] API 발송 실패 → SMTP 폴백 시도: {msg}")
    ok2, msg2 = send_email_smtp(subject, body, attachments, cfg, html_body)
    if ok2:
        return True, f"{msg2} (API 실패 후 SMTP 폴백)"
    log.warning(f"[메일] SMTP 도 실패 → Apps Script 폴백 시도: {msg2}")
    ok3, msg3 = send_email_appscript(subject, html_body, cfg)
    if ok3:
        return True, f"{msg3} (API/SMTP 실패 후 Apps Script 폴백)"
    return False, f"API 실패({msg}) / SMTP 실패({msg2}) / Apps Script 실패({msg3})"


# =====================================================================
# 4. KeyRotator (라운드로빈 + 쿨다운)
# =====================================================================
class KeyRotator:
    def __init__(self, keys: list):
        valid = [k.strip() for k in keys if k and k.strip()]
        self._lock = threading.Lock()
        self._keys = collections.deque(valid)
        self._cooldowns = {}
        self._all_keys = list(valid)

    def __len__(self):
        return len(self._all_keys)

    def get_next(self) -> str:
        with self._lock:
            if not self._keys:
                return ""
            now = time.time()
            self._cooldowns = {k: t for k, t in self._cooldowns.items() if t > now}
            for _ in range(len(self._keys)):
                cand = self._keys[0]
                self._keys.rotate(-1)
                if cand not in self._cooldowns:
                    return cand
            if self._cooldowns:
                earliest = min(self._cooldowns, key=self._cooldowns.get)
                wait = self._cooldowns[earliest] - now
                if wait > 0:
                    log.info(f"[KeyRotator] 모든 키 쿨다운 — {wait:.1f}s 대기")
                    self._lock.release()
                    try:
                        time.sleep(min(wait + 0.5, 60))
                    finally:
                        self._lock.acquire()
                self._cooldowns.pop(earliest, None)
                return earliest
            return self._keys[0] if self._keys else ""

    def send_to_back(self, key: str):
        with self._lock:
            if key in self._keys:
                self._keys.remove(key)
                self._keys.append(key)

    def set_cooldown(self, key: str, seconds: float):
        with self._lock:
            self._cooldowns[key] = time.time() + seconds


def _parse_retry_delay(error) -> float:
    s = str(error)
    m = re.search(r'retryDelay["\']?\s*[:=]\s*["\']?([\d.]+)s?', s, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    m = re.search(r'retry\s*after\s+([\d.]+)\s*s', s, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    m = re.search(r'(\d+\.?\d*)s', s)
    if m:
        try:
            v = float(m.group(1))
            if 1 <= v <= 300:
                return v
        except ValueError:
            pass
    return 35.0


# =====================================================================
# 5. Gemini 공통 호출 헬퍼 (안전 추출 + 모델 폴백 + 스레드 격리)
# =====================================================================
def _safe_extract_gemini_text(response) -> tuple:
    if response is None:
        return "", "no_response"
    try:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            pf = getattr(response, "prompt_feedback", None)
            if pf is not None:
                br = getattr(pf, "block_reason", None)
                if br:
                    return "", f"prompt_blocked:{br}"
            return "", "no_candidates"
        cand = candidates[0]
        fr = getattr(cand, "finish_reason", None)
        fr_str = str(fr) if fr is not None else ""
        content = getattr(cand, "content", None)
        if content is None:
            return "", f"no_content:fr={fr_str}"
        parts = getattr(content, "parts", None) or []
        chunks = [getattr(p, "text", None) for p in parts if getattr(p, "text", None)]
        if chunks:
            return "".join(chunks), ""
        if "SAFETY" in fr_str.upper():
            return "", f"safety_blocked:{fr_str}"
        if "RECITATION" in fr_str.upper():
            return "", f"recitation_blocked:{fr_str}"
        return "", f"empty_text:fr={fr_str}"
    except Exception as exc:
        try:
            t = getattr(response, "text", None) or ""
            if t:
                return t, ""
        except Exception:
            pass
        return "", f"extract_error:{type(exc).__name__}"


def _safe_close_loop(loop):
    if loop is None:
        return
    try:
        if loop.is_running():
            return
    except Exception:
        return
    try:
        if not loop.is_closed():
            loop.close()
    except Exception:
        pass


def _gemini_isolated_call(api_key, contents, generation_config=None,
                          use_search_tool=False, timeout_sec=None) -> tuple:
    if timeout_sec is None:
        timeout_sec = _GEMINI_API_TIMEOUT_SEC
    if not GENAI_AVAILABLE:
        return "", ("genai_not_installed", None)
    if not api_key:
        return "", ("empty_key", None)

    result_box = [None]
    error_box  = [None]

    def _worker():
        loop = None
        try:
            try:
                if sys.platform.startswith("win"):
                    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            except Exception:
                pass
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            except Exception:
                loop = None

            from google import genai as _genai
            from google.genai import types as _types

            gen_kwargs = {}
            if generation_config:
                for k in ("temperature", "max_output_tokens", "top_p", "top_k"):
                    if generation_config.get(k) is not None:
                        gen_kwargs[k] = generation_config[k]
            if use_search_tool:
                try:
                    gen_kwargs["tools"] = [_types.Tool(google_search=_types.GoogleSearch())]
                except Exception:
                    pass
            try:
                config = _types.GenerateContentConfig(**gen_kwargs)
            except Exception:
                config = _types.GenerateContentConfig()

            client = _genai.Client(api_key=api_key)
            models = [_GEMINI_MODEL_PRIMARY] + [m for m in _GEMINI_MODEL_FALLBACKS
                                                if m != _GEMINI_MODEL_PRIMARY]
            last_err = None
            for model_name in models:
                try:
                    resp = client.models.generate_content(
                        model=model_name, contents=contents, config=config)
                    text, reason = _safe_extract_gemini_text(resp)
                    if text:
                        result_box[0] = (text, model_name)
                        return
                    if reason and reason.startswith(
                            ("safety_blocked", "prompt_blocked", "recitation_blocked")):
                        error_box[0] = (reason, None)
                        return
                    last_err = ("empty_response", reason)
                except Exception as ce:
                    es = str(ce).lower()
                    last_err = ("api_error", ce)
                    if any(k in es for k in ("404", "not_found", "not found",
                                             "permission_denied", "not supported",
                                             "unsupported", "is not found for api version")):
                        continue
                    if any(k in es for k in ("429", "resource_exhausted", "quota", "rate limit")):
                        error_box[0] = ("quota_error", ce)
                        return
                    if any(k in es for k in ("401", "403", "api key", "invalid", "unauthenticated")):
                        error_box[0] = ("auth_error", ce)
                        return
                    continue
            error_box[0] = last_err or ("unknown_error", None)
        except Exception as oe:
            error_box[0] = ("worker_exception", oe)
        finally:
            _safe_close_loop(loop)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    if t.is_alive():
        return "", ("timeout", None)
    if error_box[0] is not None:
        return "", error_box[0]
    if result_box[0] is None:
        return "", ("no_result", None)
    return result_box[0][0], None


def gemini_call(rotator: KeyRotator, contents, generation_config=None,
                use_search_tool=False, timeout_sec=None, max_attempts=None) -> tuple:
    """KeyRotator + retryDelay + 모델 폴백 통합 진입점. Returns (text, err_str)."""
    if len(rotator) == 0:
        return "", "no_keys"
    if max_attempts is None:
        max_attempts = min(len(rotator), 3)
    last = ""
    for attempt in range(max_attempts):
        key = rotator.get_next()
        if not key:
            return "", "no_available_keys"
        text, err = _gemini_isolated_call(key, contents, generation_config,
                                          use_search_tool, timeout_sec)
        if text:
            return text, ""
        cat = err[0] if isinstance(err, tuple) else str(err)
        detail = err[1] if isinstance(err, tuple) and len(err) > 1 else ""
        last = f"{cat}:{str(detail)[:160]}"

        if cat == "quota_error":
            d = _parse_retry_delay(detail) if detail else 35.0
            log.warning(f"[Gemini] 429 retryDelay={d:.0f}s ({attempt+1}/{max_attempts})")
            rotator.set_cooldown(key, d)
            rotator.send_to_back(key)
            if attempt < max_attempts - 1:
                time.sleep(min(d, 60))
                continue
            return "", last
        if cat == "auth_error":
            log.error(f"[Gemini] 인증 실패 — 키 격리 ({attempt+1}/{max_attempts})")
            rotator.set_cooldown(key, 9999)
            if attempt < max_attempts - 1:
                continue
            return "", last
        if cat == "timeout":
            log.warning(f"[Gemini] 타임아웃 ({attempt+1}/{max_attempts})")
            rotator.send_to_back(key)
            if attempt < max_attempts - 1:
                continue
            return "", last
        if cat in ("safety_blocked", "prompt_blocked", "recitation_blocked"):
            log.warning(f"[Gemini] 콘텐츠 차단: {cat}")
            return "", last
        log.warning(f"[Gemini] 일시 오류 {cat} ({attempt+1}/{max_attempts})")
        rotator.send_to_back(key)
        if attempt < max_attempts - 1:
            time.sleep(2)
            continue
        return "", last
    return "", last or "all_attempts_failed"


# =====================================================================
# 6. KRX 종목 캐시 (신선 수집 → 저장 / 실패 시 캐시 폴백)
# =====================================================================
_KRX_MAP_CACHE = None  # 프로세스 전역 캐시 (resolve_ticker가 사용)


def _load_krx_tickers_impl(force_refresh: bool = True) -> dict:
    """
    KRX(KOSPI+KOSDAQ) 종목명→티커 매핑을 반환합니다.

    동작:
      1) FinanceDataReader로 신선 수집 시도
      2) 성공 → krx_tickers.json 에 {"updated": iso, "map": {...}} 저장
      3) 실패 → 직전 캐시(json) 로드 후 분석 지속, 경고 로그
      4) 캐시도 없으면 빈 dict (티커 변환은 입력값 그대로 통과)
    """
    fresh = {}
    if FDR_AVAILABLE and force_refresh:
        for market, suffix in (("KOSPI", ".KS"), ("KOSDAQ", ".KQ")):
            try:
                df = fdr.StockListing(market)
                df.columns = [c.strip() for c in df.columns]
                code_col = next((c for c in df.columns if c in ("Code", "Symbol")), None)
                name_col = next((c for c in df.columns if c in ("Name", "ShortName")), None)
                if code_col and name_col:
                    for _, row in df.iterrows():
                        name = str(row[name_col]).strip()
                        code = str(row[code_col]).strip().zfill(6)
                        if name and code and name not in fresh:
                            fresh[name] = f"{code}{suffix}"
                    log.info(f"[KRX] {market} 수집 OK ({len(df)}건)")
            except Exception as e:
                log.warning(f"[KRX] {market} 수집 실패: {e}")

    if fresh:
        try:
            with open(KRX_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"updated": datetime.now().isoformat(), "map": fresh},
                          f, ensure_ascii=False)
            log.info(f"[KRX] 캐시 저장 완료 — {len(fresh)}개 종목 → {KRX_CACHE_FILE}")
        except Exception as e:
            log.warning(f"[KRX] 캐시 저장 실패: {e}")
        return fresh

    # ── 폴백: 캐시 로드 ──────────────────────────────────────────
    if os.path.exists(KRX_CACHE_FILE):
        try:
            with open(KRX_CACHE_FILE, encoding="utf-8") as f:
                cached = json.load(f)
            cmap = cached.get("map", {})
            updated = cached.get("updated", "unknown")
            log.warning(
                f"[KRX] ⚠️ 신선 수집 실패 → 캐시 폴백 사용 "
                f"({len(cmap)}개, 마지막 갱신: {updated})"
            )
            return cmap
        except Exception as e:
            log.error(f"[KRX] 캐시 로드 실패: {e}")
    log.error("[KRX] 신선 수집/캐시 모두 실패 — 티커 변환 없이 진행")
    return {}


def load_krx_tickers(force_refresh: bool = True) -> dict:
    """_load_krx_tickers_impl 호출 후 전역 캐시(_KRX_MAP_CACHE)를 갱신한다."""
    global _KRX_MAP_CACHE
    result = _load_krx_tickers_impl(force_refresh)
    _KRX_MAP_CACHE = result  # 항상 최신 결과로 갱신 (빈 dict라도)
    return result


def resolve_ticker(query: str) -> str:
    global _KRX_MAP_CACHE
    if _KRX_MAP_CACHE is None:
        load_krx_tickers(force_refresh=False)
    return (_KRX_MAP_CACHE or {}).get(query.strip(), query.strip())


# =====================================================================
# 7. 공통 크롤링 유틸
# =====================================================================
def get_random_header() -> dict:
    agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    ]
    return {
        "User-Agent": random.choice(agents),
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.google.com/",
    }


def _is_blocked(text: str) -> bool:
    low = text.lower()
    return any(kw in low for kw in BLOCK_KEYWORDS)


def _clean_desc(raw_html: str, max_chars: int = 500) -> str:
    if not raw_html:
        return ""
    return BeautifulSoup(raw_html, "html.parser").get_text(strip=True)[:max_chars]


def resolve_url(url: str, timeout: int = 8) -> str:
    if "news.google.com" not in url:
        return url
    try:
        resp = requests.get(url, headers=get_random_header(), timeout=timeout,
                            allow_redirects=True, verify=False)
        final = resp.url
        return url if "news.google.com" in final else final
    except Exception:
        return url


def _extract_with_trafilatura(html: str, max_chars: int = 4000) -> str:
    if not TRAF_AVAILABLE or not html:
        return ""
    try:
        text = trafilatura.extract(html, include_comments=False, include_tables=False,
                                   no_fallback=False, favor_recall=True)
        if not text:
            return ""
        return re.sub(r"\n{3,}", "\n\n", text).strip()[:max_chars]
    except Exception:
        return ""


# =====================================================================
# 8. 4-Tier Fallback 크롤링
# =====================================================================
def _fetch_by_requests(url: str, max_chars: int = 4000) -> tuple:
    try:
        resp = requests.get(url, headers=get_random_header(), timeout=9,
                            allow_redirects=True, verify=False)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        if _is_blocked(resp.text[:3000]):
            return "", "blocked"
        text = _extract_with_trafilatura(resp.text, max_chars)
        if len(text) < 100:
            return "", "short"
        return text, ""
    except requests.HTTPError:
        return "", "http_error"
    except Exception:
        return "", "error"


def _fetch_by_cffi(url: str, max_chars: int = 4000) -> str:
    """[비-크로뮴 우회] curl_cffi 로 실제 Chrome 의 TLS/JA3 지문을 위장해 본문을 가져온다.
    'requests 는 403 인데 브라우저면 통과'하는 봇월(Akamai/일부 Cloudflare 등)을, 무거운
    크로뮴 없이 빠르게 우회. 받은 HTML 은 _fetch_by_requests 와 동일하게 trafilatura 로 추출."""
    if not CFFI_AVAILABLE or not TRAF_AVAILABLE:
        return ""
    try:
        resp = _cffi_requests.get(url, impersonate="chrome", timeout=12,
                                  allow_redirects=True, verify=False)
        html = resp.text or ""
        if not html or _is_blocked(html[:3000]):
            return ""
        text = _extract_with_trafilatura(html, max_chars)
        if len(text) < 100 or _looks_hard_blocked(text):
            return ""
        return text
    except Exception:
        return ""


def _fetch_by_reader_proxy(url: str, max_chars: int = 4000) -> str:
    """[비-크로뮴 우회] r.jina.ai 리더 프록시로 본문을 받아온다(크로뮴 없이, 서버측에서
    렌더·정제한 텍스트). 페이월/봇월에 막힌 기사의 'Gemini(AI 추정) 직전 마지막 비-AI 폴백'.
    무료 외부 서비스라 rate-limit/실패는 조용히 패스(다음 폴백으로)."""
    if not READER_USE:
        return ""
    try:
        hdr = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")}
        r = requests.get("https://r.jina.ai/" + url, headers=hdr, timeout=20, verify=False)
        if r.status_code != 200:
            return ""
        txt = r.text or ""
        # 리더는 'Title:/URL Source:/Markdown Content:' 머리말을 붙인다 → 본문만 취함.
        if "Markdown Content:" in txt:
            txt = txt.split("Markdown Content:", 1)[1]
        txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
        if len(txt) < 120 or _looks_hard_blocked(txt):
            return ""
        return txt[:max_chars]
    except Exception:
        return ""


def _detect_chrome_major():
    """설치된 Chrome 메이저 버전을 런타임 탐지. uc 의 자동탐지는 최신 드라이버(149 등)를
    받아 설치본(148)과 어긋나 'session not created' 를 내므로, 레지스트리에서 실제 설치
    버전을 읽어 version_main 으로 못박는다. 환경변수 CHROME_MAJOR 가 있으면 그게 우선.
    탐지 실패 시 None(uc 자동탐지에 맡김)."""
    if _CHROME_MAJOR:
        return _CHROME_MAJOR
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for sub in (r"Software\Google\Chrome\BLBeacon",
                        r"Software\Wow6432Node\Google\Chrome\BLBeacon"):
                try:
                    k = winreg.OpenKey(hive, sub)
                    v, _ = winreg.QueryValueEx(k, "version")
                    winreg.CloseKey(k)
                    if v:
                        return int(str(v).split(".")[0])
                except Exception:
                    continue
    except Exception:
        pass
    return None


def _get_selenium_driver():
    """undetected-chromedriver 드라이버를 1개 만들어 재사용(매 기사 새로 띄우면 느림).
    설치된 Chrome 버전을 런타임 탐지(_detect_chrome_major)해 version_main 고정.
    실패 시 None — 호출자는 그냥 다음 폴백으로 넘어간다."""
    global _SELENIUM_DRIVER
    if _SELENIUM_DRIVER is not None:
        return _SELENIUM_DRIVER
    if not SELENIUM_AVAILABLE:
        return None
    try:
        major = _detect_chrome_major()
        opts = _uc.ChromeOptions()
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1280,900")
        opts.add_argument("--lang=ko-KR")
        # headless 는 uc 인자로 줘야 스텔스가 유지된다.
        d = _uc.Chrome(options=opts, version_main=major, headless=True)
        d.set_page_load_timeout(_SELENIUM_STEP_TIMEOUT_SEC)
        _SELENIUM_DRIVER = d
        log.info(f"[Selenium] 드라이버 기동 OK (chrome_major={major or 'auto'})")
        return d
    except Exception as e:
        log.warning(f"[Selenium] 드라이버 기동 실패: {type(e).__name__}: {str(e)[:120]} "
                    f"→ 셀레늄 폴백 비활성")
        return None


def _quit_selenium_driver():
    """수집 끝나고 드라이버 정리(좀비 chrome 방지) + 호출 카운터 리셋. collect 종료 시 호출."""
    global _SELENIUM_DRIVER, _SELENIUM_CALLS, _SELENIUM_CAP_LOGGED
    if _SELENIUM_DRIVER is not None:
        try:
            _SELENIUM_DRIVER.quit()
        except Exception:
            pass
        _SELENIUM_DRIVER = None
    _SELENIUM_CALLS = 0
    _SELENIUM_CAP_LOGGED = False
    _SELENIUM_BAD_DOMAINS.clear()


def _fetch_by_selenium(url: str, max_chars: int = 4000) -> str:
    """undetected-chromedriver(스텔스)로 봇차단(403) 사이트 본문 추출.
    Cloudflare 등 JS 챌린지는 자동통과될 때까지 최대 _SELENIUM_CHALLENGE_WAIT_SEC 폴링.
    챌린지를 못 뚫고 안내문만 나오면 본문 인정하지 않고 ''(다음 폴백)로 넘긴다."""
    if not SELENIUM_AVAILABLE or not TRAF_AVAILABLE:
        return ""
    try:
        dom = urllib.parse.urlparse(url).netloc.replace("www.", "")
    except Exception:
        dom = ""
    # 이번 수집에서 이미 봇월에 막힌 도메인은 빠른 실패(락 낭비 방지).
    if dom and dom in _SELENIUM_BAD_DOMAINS:
        return ""
    # 드라이버 1개를 여러 스레드가 공유하므로 한 번에 한 기사만 처리(직렬화).
    with _SELENIUM_LOCK:
        global _SELENIUM_CALLS, _SELENIUM_CAP_LOGGED
        if dom and dom in _SELENIUM_BAD_DOMAINS:   # 락 대기 중 다른 스레드가 등록했을 수도
            return ""
        if _SELENIUM_MAX_CALLS and _SELENIUM_CALLS >= _SELENIUM_MAX_CALLS:
            if not _SELENIUM_CAP_LOGGED:
                log.warning(f"[Selenium] 호출 상한 {_SELENIUM_MAX_CALLS} 도달 — "
                            f"이후 기사는 셀레늄 생략(Gemini/RSS 폴백)")
                _SELENIUM_CAP_LOGGED = True
            return ""
        d = _get_selenium_driver()
        if d is None:
            return ""
        _SELENIUM_CALLS += 1
        try:
            import time as _t
            d.get(url)
            deadline = max(6, _SELENIUM_CHALLENGE_WAIT_SEC)
            waited, step = 0.0, 2.5
            last_body = ""
            while True:
                _t.sleep(step)
                waited += step
                html = d.page_source or ""
                if html and len(html) >= 500:
                    # 확정 차단/페이월이면 기다려도 소용없다 → 즉시 포기(빠른 패스) + 도메인 스킵
                    if _looks_hard_blocked(html[:8000]):
                        if dom:
                            _SELENIUM_BAD_DOMAINS.add(dom)
                        log.info(f"[Selenium] 확정차단/페이월 감지 → 즉시 패스 {url[:50]}")
                        return ""
                    body = _extract_with_trafilatura(html, max_chars)
                    if body:
                        last_body = body
                        if _looks_hard_blocked(body):   # 추출 본문이 페이월 안내문
                            if dom:
                                _SELENIUM_BAD_DOMAINS.add(dom)
                            log.info(f"[Selenium] 페이월 본문 감지 → 즉시 패스 {url[:50]}")
                            return ""
                    # 본문이 충분하고 봇월 안내문이 아니면 챌린지 통과로 보고 성공
                    if body and len(body) >= 150 and not _looks_like_bot_wall(body):
                        return body
                if waited >= deadline:
                    break
            # 데드라인까지 챌린지 못 뚫음 → 봇월 안내문이면 실패(거짓양성 차단)
            if last_body and not _looks_like_bot_wall(last_body):
                return last_body
            # 이 도메인은 봇월을 못 뚫었으니 이번 수집 동안 재시도 안 함(빠른 실패).
            if dom:
                _SELENIUM_BAD_DOMAINS.add(dom)
            log.warning(f"[Selenium] 봇월 미통과 {url[:50]} ({waited:.0f}s) — 도메인 스킵 등록")
            return ""
        except Exception as e:
            log.warning(f"[Selenium] 본문 실패 {url[:40]}: {type(e).__name__}")
            return ""


def _fetch_raw_by_selenium(url: str) -> str:
    """봇차단된 RSS 피드/목록 페이지의 '원본 마크업'을 Chromium 으로 받아온다(파싱은 호출자 몫).
    _fetch_by_selenium 은 기사 본문(trafilatura) 전용이고, 이건 피드/목록 discovery 용 —
    page_source 를 그대로 반환해 BeautifulSoup 으로 item/링크를 뽑게 한다. 실패 시 ''."""
    if not SELENIUM_AVAILABLE:
        return ""
    try:
        dom = urllib.parse.urlparse(url).netloc.replace("www.", "")
    except Exception:
        dom = ""
    if dom and dom in _SELENIUM_BAD_DOMAINS:
        return ""
    with _SELENIUM_LOCK:
        global _SELENIUM_CALLS, _SELENIUM_CAP_LOGGED
        if dom and dom in _SELENIUM_BAD_DOMAINS:
            return ""
        if _SELENIUM_MAX_CALLS and _SELENIUM_CALLS >= _SELENIUM_MAX_CALLS:
            if not _SELENIUM_CAP_LOGGED:
                log.warning(f"[Selenium] 호출 상한 {_SELENIUM_MAX_CALLS} 도달 — 목록 셀레늄 생략")
                _SELENIUM_CAP_LOGGED = True
            return ""
        d = _get_selenium_driver()
        if d is None:
            return ""
        _SELENIUM_CALLS += 1
        try:
            import time as _t
            d.get(url)
            deadline = max(6, _SELENIUM_CHALLENGE_WAIT_SEC)
            waited, step = 0.0, 2.5
            html = ""
            while True:
                _t.sleep(step)
                waited += step
                html = d.page_source or ""
                # 확정 차단/페이월이면 즉시 포기(빠른 패스) + 도메인 스킵
                if html and _looks_hard_blocked(html[:8000]):
                    if dom:
                        _SELENIUM_BAD_DOMAINS.add(dom)
                    log.info(f"[Selenium] 목록 확정차단 감지 → 즉시 패스 {url[:50]}")
                    return ""
                # 봇월(챌린지) 아니고 충분히 로드됐으면 성공
                if html and len(html) >= 500 and not _looks_like_bot_wall(html[:6000]):
                    return html
                if waited >= deadline:
                    break
            if html and not _looks_like_bot_wall(html[:6000]):
                return html
            if dom:
                _SELENIUM_BAD_DOMAINS.add(dom)
            log.warning(f"[Selenium] 목록/피드 봇월 미통과 {url[:50]} — 도메인 스킵 등록")
            return ""
        except Exception as e:
            log.warning(f"[Selenium] 목록/피드 실패 {url[:40]}: {type(e).__name__}")
            return ""


def _parse_rss_items(markup, limit: int) -> list:
    """RSS 마크업에서 (title, link, pub, desc) 목록 추출.
    requests 의 원본 XML 과 셀레늄 page_source(HTML 로 감싼 XML) 양쪽을 모두 처리:
      - xml 파서 우선, 0건이면 html.parser 재시도
      - html.parser 에서는 <link> 가 void 라 URL 이 next_sibling 에 옴 → 보정
      - pubDate 는 파서별 대소문자(pubDate/pubdate) 모두 대응"""
    def _extract(soup):
        rows = []
        for item in (soup.find_all("item") or []):
            if len(rows) >= limit:
                break
            try:
                t = item.find("title")
                title = t.get_text(strip=True) if t else "제목없음"
                lk = item.find("link")
                link = ""
                if lk:
                    link = (lk.get_text(strip=True) or
                            (str(lk.next_sibling).strip() if lk.next_sibling else "")).strip()
                pb = item.find("pubdate") or item.find("pubDate")
                pub = pb.get_text()[:16] if pb else "Recent"
                ds = item.find("description")
                desc = ds.get_text() if ds else ""
                if link:
                    rows.append((title, link, pub, desc))
            except Exception:
                continue
        return rows
    try:
        rows = _extract(BeautifulSoup(markup, "xml"))
    except Exception:
        rows = []
    if not rows:
        try:
            rows = _extract(BeautifulSoup(markup, "html.parser"))
        except Exception:
            rows = []
    return rows


def _fetch_by_playwright(url: str, max_chars: int = 4000) -> str:
    if not PW_AVAILABLE or not TRAF_AVAILABLE:
        return ""
    try:
        if sys.platform.startswith("win"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    except Exception:
        loop = None

    browser = context = page = None
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, args=[
                    "--disable-blink-features=AutomationControlled", "--no-sandbox",
                    "--disable-gpu", "--disable-dev-shm-usage", "--window-size=1280,900"])
            except Exception as e:
                log.warning(f"[PW] launch 실패: {e}")
                return ""
            try:
                context = browser.new_context(
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
                    locale="ko-KR", viewport={"width": 1280, "height": 900}, java_script_enabled=True)
                context.set_default_timeout(_PW_GOTO_TIMEOUT_MS)
                context.set_default_navigation_timeout(_PW_GOTO_TIMEOUT_MS)
            except Exception as e:
                log.warning(f"[PW] new_context 실패: {e}")
                return ""
            try:
                page = context.new_page()
                page.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                    "window.chrome={runtime:{}};"
                    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]});")
                page.set_extra_http_headers({
                    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
                    "Referer": "https://www.google.com/"})
                try:
                    page.goto(url, timeout=_PW_GOTO_TIMEOUT_MS, wait_until="networkidle")
                except PWTimeoutError:
                    pass
                except Exception as e:
                    log.warning(f"[PW] nav 예외: {e}")
                    return ""
                try:
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                try:
                    html = page.content()
                except Exception:
                    return ""
                if _is_blocked(html[:3000]):
                    return ""
                text = _extract_with_trafilatura(html, max_chars)
                if len(text) >= 100:
                    return text
                try:
                    raw = page.evaluate("document.body.innerText") or ""
                except Exception:
                    raw = ""
                raw = re.sub(r"\s{2,}", " ", raw).strip()
                return raw[:max_chars] if len(raw) >= 100 else ""
            finally:
                for obj in (page, context):
                    try:
                        if obj:
                            obj.close()
                    except Exception:
                        pass
    except Exception as e:
        log.warning(f"[PW] 예외: {e}")
        return ""
    finally:
        try:
            if browser and browser.is_connected():
                browser.close()
        except Exception:
            pass
        _safe_close_loop(loop)


def _fetch_by_gemini(title: str, url: str, gemini_keys: list, max_chars: int = 4000) -> str:
    if not GENAI_AVAILABLE:
        return ""
    rotator = KeyRotator(gemini_keys)
    if len(rotator) == 0:
        return ""
    prompt = (
        "너는 뉴스 정보 검색 에이전트야. [기사 제목]과 [링크]를 바탕으로 "
        "내장 구글 검색으로 해당 기사의 실제 원문 전체를 찾아 그대로 출력해. "
        "요약/의견 금지. 원문 못 찾으면 '[FAIL]'만 출력.\n\n"
        f"[기사 제목]: {title}\n[링크]: {url}")
    text, err = gemini_call(rotator, prompt,
                            generation_config={"temperature": 0.2, "max_output_tokens": 4096},
                            use_search_tool=True, timeout_sec=_GEMINI_API_TIMEOUT_SEC,
                            max_attempts=min(len(rotator), 3))
    if not text:
        return ""
    text = text.strip()
    if "[FAIL]" in text.upper():
        return ""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 100:
        return ""
    return text[:max_chars] + "\n\n(🤖 Gemini + 구글검색 그라운딩 복원)"


def process_single_article(title, raw_link, pub_date, desc_raw, gemini_keys,
                           use_pw=True, max_chars=4000) -> str:
    st = (title or "")[:40]
    desc_clean = _clean_desc(desc_raw, 500)
    rss_block = f"[RSS 요약]\n{desc_clean if desc_clean else '(없음)'}"
    try:
        real = resolve_url(raw_link)
    except Exception:
        real = raw_link

    # Step 1
    try:
        t1, r1 = _fetch_by_requests(real, max_chars)
    except Exception as e:
        t1, r1 = "", f"exc:{type(e).__name__}"
    if t1:
        log.info(f"[Step1] ✅ {st}")
        return f"[{pub_date}] 🟢 [원문] {title}\n[링크] {real}\n{rss_block}\n[기사 본문]\n{t1}\n"

    # Step 1.5 curl_cffi (비-크로뮴) — requests 가 403 인데 '브라우저면 통과'하는 TLS 봇월을
    # 실제 Chrome 지문 위장으로 우회. 크로뮴보다 빠르므로 무거운 Playwright/셀레늄 전에 시도.
    if CFFI_USE and CFFI_AVAILABLE:
        try:
            t15 = _fetch_by_cffi(real, max_chars)
        except Exception:
            t15 = ""
        if t15 and len(t15) >= 100:
            log.info(f"[Step1.5] ✅ curl_cffi {st}")
            return f"[{pub_date}] 🟢 [원문] {title}\n[링크] {real}\n{rss_block}\n[기사 본문]\n{t15}\n"

    # Step 2 (옵션)
    t2 = ""
    if use_pw and PW_AVAILABLE:
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(_fetch_by_playwright, real, max_chars)
                try:
                    t2 = fut.result(timeout=_PW_STEP2_TIMEOUT_SEC)
                except concurrent.futures.TimeoutError:
                    fut.cancel()
                    log.warning(f"[Step2] ⏱️ timeout {st}")
        except Exception as e:
            log.warning(f"[Step2] exc {st}: {e}")
    if t2 and len(t2) >= 100:
        log.info(f"[Step2] ✅ {st}")
        return f"[{pub_date}] 🟢 [원문] {title}\n[링크] {real}\n{rss_block}\n[기사 본문]\n{t2}\n"

    # Step 2.5 Selenium (undetected-chromedriver) — requests/playwright 가 봇차단(403)
    # 당한 사이트(investing 등)를 스텔스 브라우저로 우회. SELENIUM_USE 켜졌을 때만.
    # 별도 타임아웃 래퍼 없이 직접 호출: _fetch_by_selenium 은 page_load_timeout +
    # 챌린지 폴링으로 내부 유계이고, 단일 드라이버는 _SELENIUM_LOCK 으로 직렬화된다.
    # (래퍼+타임아웃을 쓰면 락 대기시간까지 타임아웃에 포함돼, 성공한 본문을 버리게 됨)
    t25 = ""
    if SELENIUM_USE and SELENIUM_AVAILABLE:
        try:
            t25 = _fetch_by_selenium(real, max_chars)
        except Exception as e:
            log.warning(f"[Step2.5] exc {st}: {e}")
    if t25 and len(t25) >= 100:
        log.info(f"[Step2.5] ✅ 셀레늄 {st}")
        return f"[{pub_date}] 🟢 [원문] {title}\n[링크] {real}\n{rss_block}\n[기사 본문]\n{t25}\n"

    # Step 2.7 리더 프록시(r.jina.ai, 비-크로뮴) — 크로뮴까지 막혔을 때 서버측 클린 텍스트로
    # 회수. Gemini(AI 추정) 전 '마지막 비-AI 폴백'이라 신뢰도는 원문보다 한 단계 낮춰 🔵 표기.
    if READER_USE:
        try:
            t27 = _fetch_by_reader_proxy(real, max_chars)
        except Exception:
            t27 = ""
        if t27 and len(t27) >= 100:
            log.info(f"[Step2.7] ✅ 리더프록시 {st}")
            return f"[{pub_date}] 🔵 [리더] {title}\n[링크] {real}\n{rss_block}\n[기사 본문]\n{t27}\n"

    # Step 3 Gemini
    t3 = ""
    valid = [k for k in gemini_keys if k and k.strip()]
    if valid:
        try:
            t3 = _fetch_by_gemini(title, real, valid, max_chars)
        except Exception as e:
            log.warning(f"[Step3] exc {st}: {e}")
    if t3 and len(t3) >= 100:
        log.info(f"[Step3] ✅ AI복구 {st}")
        return f"[{pub_date}] 🔵 [AI복구] {title}\n[링크] {real}\n{rss_block}\n[기사 본문]\n{t3}\n"

    # Step 4 fallback
    if desc_clean:
        return f"[{pub_date}] 🟡 [요약본] {title}\n[링크] {raw_link}\n{rss_block}\n[기사 본문]\n(원문 실패 — RSS 요약 참고)\n"
    return f"[{pub_date}] 🔴 [실패] {title}\n[링크] {raw_link}\n[RSS 요약]\n(없음)\n[기사 본문]\n(수집 데이터 없음)\n"


# =====================================================================
# 9. RSS / 네이버 섹션 수집
# =====================================================================
def collect_rss_source(source_name, rss_url, limit, gemini_keys, use_pw=True) -> str:
    lines = [f"### {source_name}"]
    try:
        # 1) requests 로 피드 가져오기(평소 경로)
        metas = []
        try:
            res = requests.get(rss_url, headers=get_random_header(), timeout=10, verify=False)
            metas = _parse_rss_items(res.content, limit)
        except Exception as e:
            log.warning(f"[RSS] {source_name} requests 실패: {type(e).__name__}")
        # 2) 0건/차단이면 Chromium(셀레늄)으로 피드 재시도 — 봇 방지 사이트 대응
        if not metas and SELENIUM_USE and SELENIUM_AVAILABLE:
            log.info(f"[RSS] {source_name} requests 0건 → 셀레늄 피드 폴백 시도")
            raw = _fetch_raw_by_selenium(rss_url)
            if raw:
                metas = _parse_rss_items(raw, limit)
                if metas:
                    log.info(f"[RSS] {source_name} 셀레늄 피드 회수 {len(metas)}건")
        if not metas:
            return f"### {source_name}: RSS 항목 없음"

        # 요약 전용 소스(확정 페이월)는 본문 시도 없이 헤드라인+요약만 즉시 출력(바로 패스).
        if source_name in SUMMARY_ONLY_SOURCES:
            for (title, link, pub, desc) in metas:
                dc = _clean_desc(desc, 500)
                rb = f"[RSS 요약]\n{dc if dc else '(없음)'}"
                lines.append(f"[{pub}] 🟡 [요약본] {title}\n[링크] {link}\n{rb}\n"
                             f"[기사 본문]\n(요약 전용 소스 — 본문 페이월로 생략)\n")
            return "\n".join(lines)

        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            f2i = {ex.submit(process_single_article, m[0], m[1], m[2], m[3],
                             gemini_keys, use_pw, 4000): i for i, m in enumerate(metas)}
            for fut in concurrent.futures.as_completed(f2i):
                i = f2i[fut]
                try:
                    txt = fut.result(timeout=_SINGLE_ARTICLE_WALL_SEC)
                    if txt:
                        results[i] = txt
                except Exception as e:
                    m = metas[i]
                    results[i] = (f"[{m[2]}] 🔴 [실패] {m[0]}\n[링크] {m[1]}\n"
                                  f"[기사 본문]\n(처리 예외: {e})\n")
        for i in sorted(results):
            lines.append(results[i])
    except Exception as e:
        lines.append(f"Error: {e}")
    return "\n".join(lines)


def _parse_naver_list_links(soup, limit) -> list:
    """네이버 뉴스 목록 페이지에서 (title, link, 'Recent', '') 추출.
    모던 레이아웃(div.sa_text — /section/, /breakingnews/section/) 우선,
    0건이면 레거시 레이아웃(.type06 — 언론사별 list.naver?mode=LPOD&oid=)으로 자동 폴백."""
    metas, seen = [], set()
    # 1) 모던 레이아웃
    for item in soup.select("div.sa_text"):
        if len(metas) >= limit:
            break
        a = item.select_one("a.sa_text_title")
        if not a:
            continue
        title = a.get_text(strip=True)
        link = a.get("href", "")
        if title and link and link not in seen:
            seen.add(link)
            metas.append((title, link, "Recent", ""))
    if metas:
        return metas
    # 2) 레거시 레이아웃(list.naver 언론사 목록) — li 안에서 '텍스트 있는' a 가 제목 링크.
    #    첫 a 는 보통 썸네일(빈 텍스트)이라 건너뛴다.
    for li in soup.select("ul.type06_headline li, ul.type06 li"):
        if len(metas) >= limit:
            break
        a = None
        for cand in li.select("dl dt a, dt a, a"):
            if cand.get_text(strip=True):
                a = cand
                break
        if not a:
            continue
        title = a.get_text(strip=True)
        link = a.get("href", "")
        if title and link and link not in seen:
            seen.add(link)
            metas.append((title, link, "Recent", ""))
    return metas


def collect_naver_section(section_name, url, limit, gemini_keys, use_pw=True) -> str:
    lines = [f"### {section_name}"]
    try:
        metas = []
        try:
            res = requests.get(url, headers=get_random_header(), timeout=8, verify=False)
            metas = _parse_naver_list_links(BeautifulSoup(res.text, "html.parser"), limit)
        except Exception as e:
            log.warning(f"[네이버] {section_name} requests 실패: {type(e).__name__}")
        # 0건/차단이면 Chromium(셀레늄)으로 목록 페이지 재시도 — 봇 방지 대응
        if not metas and SELENIUM_USE and SELENIUM_AVAILABLE:
            log.info(f"[네이버] {section_name} requests 0건 → 셀레늄 목록 폴백 시도")
            raw = _fetch_raw_by_selenium(url)
            if raw:
                metas = _parse_naver_list_links(BeautifulSoup(raw, "html.parser"), limit)
                if metas:
                    log.info(f"[네이버] {section_name} 셀레늄 목록 회수 {len(metas)}건")
        if not metas:
            return f"### {section_name}: 목록 항목 없음"
        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            f2i = {ex.submit(process_single_article, m[0], m[1], m[2], m[3],
                             gemini_keys, use_pw, 4000): i for i, m in enumerate(metas)}
            for fut in concurrent.futures.as_completed(f2i):
                i = f2i[fut]
                try:
                    txt = fut.result(timeout=_SINGLE_ARTICLE_WALL_SEC)
                    if txt:
                        results[i] = txt
                except Exception:
                    pass
        for i in sorted(results):
            lines.append(results[i])
    except Exception as e:
        lines.append(f"Error: {e}")
    return "\n".join(lines)


# =====================================================================
# 10. 도구: SEARCH_NEWS / SEARCH_STOCK / 네이버 API
# =====================================================================
def tool_search_news(keyword, gemini_keys, use_pw=True) -> str:
    enc = urllib.parse.quote(keyword)
    rss = f"https://news.google.com/rss/search?q={enc}+when:1d&hl=ko&gl=KR&ceid=KR:ko"
    lines = [f"\n=== [NEWS] '{keyword}' ==="]
    try:
        res = requests.get(rss, headers=get_random_header(), timeout=10, verify=False)
        soup = BeautifulSoup(res.content, "xml")
        items = (soup.find_all("item") or [])[:5]
        metas = []
        for item in items:
            if len(metas) >= 3:
                break
            try:
                title = item.title.text.strip() if item.title else "제목없음"
                link = item.link.text.strip() if item.link else ""
                pub = item.pubDate.text[:16] if item.pubDate else "Recent"
                desc = item.description.text if item.description else ""
                if link:
                    metas.append((title, link, pub, desc))
            except Exception:
                continue
        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            f2i = {ex.submit(process_single_article, m[0], m[1], m[2], m[3],
                             gemini_keys, use_pw, 3000): i for i, m in enumerate(metas)}
            for fut in concurrent.futures.as_completed(f2i):
                i = f2i[fut]
                try:
                    txt = fut.result(timeout=_SINGLE_ARTICLE_WALL_SEC)
                    if txt:
                        results[i] = txt
                except Exception:
                    pass
        for i in sorted(results):
            lines.append(results[i])
    except Exception as e:
        lines.append(f"  오류: {e}")
    return "\n".join(lines)


def tool_search_stock(ticker_query) -> str:
    q = ticker_query.strip()
    ticker = resolve_ticker(q)
    lines = [f"\n=== [STOCK] {q} ({ticker}) ==="]
    if not YF_AVAILABLE:
        lines.append("  yfinance 미설치")
        return "\n".join(lines)
    try:
        info = yf.Ticker(ticker).info
        price = info.get("regularMarketPrice") or info.get("currentPrice")
        fields = {
            "현재가": price,
            "전일종가": info.get("previousClose"),
            "52주고가": info.get("fiftyTwoWeekHigh"),
            "52주저가": info.get("fiftyTwoWeekLow"),
            "시총(억)": round(info.get("marketCap", 0)/1e8, 1) if info.get("marketCap") else None,
            "PER": info.get("trailingPE"),
            "PBR": info.get("priceToBook"),
            "EPS": info.get("trailingEps"),
            "배당수익률(%)": round(info.get("dividendYield", 0)*100, 2) if info.get("dividendYield") else None,
            "섹터": info.get("sector"),
            "업종": info.get("industry"),
            "요약": (info.get("longBusinessSummary") or "")[:300],
        }
        for k, v in fields.items():
            if v is not None:
                lines.append(f"  • {k}: {v}")
        if not price:
            lines.append(f"  ※ 조회 실패 — 직접 '005930.KS' 형식 입력 권장")
    except Exception as e:
        lines.append(f"  오류: {e}")
    return "\n".join(lines)


def fetch_naver_news_api(keyword, cid, csec, display=10, sort="date", max_retries=2) -> str:
    ok, why = validate_naver_credentials(cid, csec)
    if not ok:
        return f"[NAVER] '{keyword}' ⚠️ 자격증명 오류: {why}"
    kw = (keyword or "").strip().strip('"\'')
    if not kw:
        return "[NAVER] (빈 키워드)"
    time.sleep(random.uniform(0.1, 0.25))
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": cid.strip(), "X-Naver-Client-Secret": csec.strip(),
               "User-Agent": "Mozilla/5.0 (compatible; ResearchAgent/1.0)", "Accept": "application/json"}
    params = {"query": kw, "display": max(1, min(int(display), 100)), "start": 1,
              "sort": sort if sort in ("sim", "date") else "sim"}
    lines = [f"\n=== [NAVER] '{kw}' ==="]
    last = None
    for attempt in range(1, max_retries + 2):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=12)
            if r.status_code == 200:
                pass
            elif r.status_code == 401:
                lines.append("  🚨 401 인증 실패 — Client ID/Secret 확인")
                return "\n".join(lines)
            elif r.status_code == 403:
                lines.append("  🚨 403 권한 거부 — 네이버 '검색 API' 사용신청 확인")
                return "\n".join(lines)
            elif r.status_code in (404, 429):
                lines.append(f"  🚨 {r.status_code} 오류")
                return "\n".join(lines)
            elif 500 <= r.status_code < 600:
                if attempt <= max_retries:
                    time.sleep(1.5 * attempt)
                    continue
                lines.append(f"  🚨 서버오류 {r.status_code}")
                return "\n".join(lines)
            else:
                lines.append(f"  🚨 HTTP {r.status_code}")
                return "\n".join(lines)
            data = r.json()
            items = data.get("items", []) or []
            total = data.get("total", 0)
            if not items:
                lines.append(f"  (결과 없음 — total={total})")
                return "\n".join(lines)
            lines.append(f"  (총 {total}건 중 {len(items)}건)")
            for i, it in enumerate(items, 1):
                title = BeautifulSoup(it.get("title", "") or "", "html.parser").get_text(strip=True)
                desc = BeautifulSoup(it.get("description", "") or "", "html.parser").get_text(strip=True)
                link = it.get("originallink") or it.get("link", "")
                lines.append(f"\n[{i}] {title}\n  [링크] {link}\n  [날짜] {it.get('pubDate','')}\n  [요약] {desc[:300]}")
            return "\n".join(lines)
        except requests.Timeout:
            last = "Timeout"
            if attempt <= max_retries:
                time.sleep(attempt)
                continue
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            break
    lines.append(f"  🚨 재시도 실패: {last}")
    return "\n".join(lines)


# =====================================================================
# 11. Phase 1: Gemini 배치 분석 → 듀얼 수집 명령어 발행
# =====================================================================
_BATCH_PROMPT = """[ROLE]
You are a senior equity research analyst (momentum + risk).
Analyze each article INDEPENDENTLY. Output MUST be in ENGLISH.

[RULES]
1. Articles delimited by --- ARTICLE [N] START/END ---.
2. Never mix info across articles.
3. Output TWO sections.

=== SECTION 1: ANALYSIS JSON ===
Return a JSON array, one object per article:
```json
[{"article_index":1,"title_original":"...","summary_en":"2-3 sentences",
"sentiment":"BULLISH|BEARISH|NEUTRAL","related_stocks":[{"name":"","ticker":"005930.KS","impact":"POSITIVE|NEGATIVE|INDIRECT","reasoning":""}],
"momentum_signal":"STRONG_BUY|BUY|HOLD|SELL|STRONG_SELL","key_catalyst":"","risk_flags":[],"time_sensitivity":"IMMEDIATE|SHORT_TERM|MEDIUM_TERM|LONG_TERM"}]
```

=== SECTION 2: DATA COLLECTION COMMANDS ===
Track A (financials/global news):
[SEARCH_STOCK: company or ticker]
[SEARCH_NEWS: keyword]

Track B (Korean realtime, MUST be Korean keywords):
<NAVER_API>{"keyword":"한글 검색어","reason":"need reason"}</NAVER_API>

REQUIRED: at least 2 Track A and 3 Track B commands. Keep total under 10.

[ARTICLES]
"""


def parse_articles_from_text(collected_text: str) -> list:
    """수집 마크다운에서 개별 기사 파싱."""
    articles = []
    blocks = re.split(r'(?=\[[^\]]+\]\s*(?:🟢|🔵|🟡|🔴))', collected_text)
    pat = re.compile(r'\[([^\]]+)\]\s*(🟢|🔵|🟡|🔴)\s*\[([^\]]+)\]\s*(.+?)(?=\n)', re.MULTILINE)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        m = pat.match(block)
        if not m:
            continue
        date_str, emoji, tag, title = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        lk = re.search(r'\[링크\]\s*(https?://\S+)', block)
        link = lk.group(1) if lk else ""
        bm = re.search(r'\[기사 본문\]\s*\n(.+)', block, re.DOTALL)
        body = bm.group(1).strip()[:3000] if bm else ""
        rm = re.search(r'\[RSS 요약\]\s*\n(.+?)(?=\n\[)', block, re.DOTALL)
        rss = rm.group(1).strip()[:500] if rm else ""
        articles.append({"title": title, "link": link, "body": body or rss,
                         "status": f"{emoji} {tag}", "date": date_str})
    return articles


def batch_analyze(articles, gemini_keys, progress=None) -> list:
    if not articles or not GENAI_AVAILABLE:
        return []
    rotator = KeyRotator(gemini_keys)
    if len(rotator) == 0:
        return []
    batches = [articles[i:i+_BATCH_SIZE] for i in range(0, len(articles), _BATCH_SIZE)]
    results = []
    for bi, batch in enumerate(batches):
        if progress:
            progress(bi+1, len(batches))
        blocks = []
        for j, a in enumerate(batch, 1):
            blocks.append(f"--- ARTICLE [{j}] START ---\nTitle: {a['title']}\n"
                          f"URL: {a['link']}\nStatus: {a['status']}\n"
                          f"Content:\n{a['body']}\n--- ARTICLE [{j}] END ---")
        prompt = _BATCH_PROMPT + "\n\n".join(blocks) + \
                 f"\n\n[INSTRUCTION] Analyze {len(batch)} articles independently."
        raw, err = gemini_call(rotator, prompt,
                               generation_config={"temperature": 0.3, "max_output_tokens": 4096},
                               timeout_sec=_GEMINI_API_TIMEOUT_SEC + 15,
                               max_attempts=min(len(rotator), 3))
        if raw:
            jm = re.search(r'```json\s*(\[[\s\S]*?\])\s*```', raw) or re.search(r'(\[[\s\S]*?\])', raw)
            if jm:
                try:
                    parsed = json.loads(jm.group(1))
                    results.extend(parsed)
                    results.append({"_raw": True, "raw_text": raw})
                except json.JSONDecodeError:
                    results.append({"raw_text": raw, "batch_index": bi+1})
            else:
                results.append({"raw_text": raw, "batch_index": bi+1})
            log.info(f"[Batch {bi+1}/{len(batches)}] ✅ {len(raw):,}자")
        else:
            results.append({"error": err, "raw_text": "", "batch_index": bi+1})
            log.error(f"[Batch {bi+1}/{len(batches)}] 🚨 {err}")
        if bi < len(batches) - 1:
            time.sleep(_BATCH_INTER_DELAY_SEC)
    return results


def parse_dual_commands(text: str) -> dict:
    result = {"stock_cmds": [], "news_cmds": [], "naver_cmds": []}
    result["stock_cmds"] = re.findall(r'\[SEARCH_STOCK:\s*(.+?)\]', text, re.IGNORECASE)
    result["news_cmds"]  = re.findall(r'\[SEARCH_NEWS:\s*(.+?)\]', text, re.IGNORECASE)
    for raw in re.findall(r'<NAVER_API>([\s\S]*?)</NAVER_API>', text, re.IGNORECASE):
        raw = raw.strip()
        cmd = None
        try:
            cmd = json.loads(raw)
        except json.JSONDecodeError:
            jm = re.search(r'\{[\s\S]*\}', raw)
            if jm:
                try:
                    cmd = json.loads(jm.group())
                except json.JSONDecodeError:
                    pass
        if cmd and isinstance(cmd, dict) and "keyword" in cmd:
            result["naver_cmds"].append(cmd)
    return result


def format_phase1_markdown(results: list) -> str:
    lines = ["## Phase 1 — AI 독립 분석 (영어)\n"]
    for item in results:
        if item.get("_raw") or "raw_text" in item or "error" in item:
            continue
        emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(item.get("sentiment", ""), "⚪")
        lines.append(f"### [{item.get('article_index','?')}] {emoji} {item.get('sentiment','')} "
                     f"| {item.get('momentum_signal','')} | {item.get('time_sensitivity','')}")
        lines.append(f"- Title: {item.get('title_original','')}")
        lines.append(f"- Summary: {item.get('summary_en','')}")
        lines.append(f"- Catalyst: {item.get('key_catalyst','')}")
        for s in item.get("related_stocks", []):
            lines.append(f"  - {s.get('name','')} ({s.get('ticker','')}) "
                         f"[{s.get('impact','')}] {s.get('reasoning','')}")
        if item.get("risk_flags"):
            lines.append(f"  - ⚠️ Risk: {', '.join(item['risk_flags'])}")
        lines.append("")
    return "\n".join(lines)


# =====================================================================
# 12. Phase 2: 듀얼 심층 수집 (Track A + Track B)
# =====================================================================
def run_deep_collection(stock_cmds, news_cmds, naver_cmds,
                        gemini_keys, naver_cid, naver_csec, use_pw=True) -> dict:
    track_a, track_b = [], []

    def _track_a():
        for q in stock_cmds:
            try:
                track_a.append(tool_search_stock(q.strip()))
                log.info(f"[TrackA] STOCK ✅ {q}")
            except Exception as e:
                track_a.append(f"[STOCK Error: {q}] {e}")
        if news_cmds:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
                f2q = {ex.submit(tool_search_news, q.strip(), gemini_keys, use_pw): q for q in news_cmds}
                for fut in concurrent.futures.as_completed(f2q):
                    try:
                        track_a.append(fut.result(timeout=120))
                        log.info(f"[TrackA] NEWS ✅ {f2q[fut]}")
                    except Exception as e:
                        track_a.append(f"[NEWS Error] {e}")

    def _track_b():
        ok, why = validate_naver_credentials(naver_cid, naver_csec)
        if not ok:
            track_b.append(f"[NAVER 건너뜀: {why}]")
            log.warning(f"[TrackB] 네이버 비활성: {why}")
            return
        cmds = naver_cmds or [
            {"keyword": "코스피 오늘 시황", "reason": "당일 시장 동향"},
            {"keyword": "외국인 기관 순매수 종목", "reason": "당일 수급"},
            {"keyword": "테마주 급등 오늘", "reason": "모멘텀 강도"},
        ]
        for c in cmds:
            kw = c.get("keyword", "")
            if not kw:
                continue
            try:
                res = fetch_naver_news_api(kw, naver_cid, naver_csec, display=10, sort="date")
                track_b.append(f"[사유: {c.get('reason','')}]\n{res}")
                log.info(f"[TrackB] ✅ {kw}")
            except Exception as e:
                track_b.append(f"[NAVER Error: {kw}] {e}")
                log.error(f"[TrackB] 🚨 {kw}: {e}")

    log.info("[Phase2] 듀얼 병렬 수집 시작")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="phase2") as ex:
        fa = ex.submit(_track_a)
        fb = ex.submit(_track_b)
        for name, fut in (("A", fa), ("B", fb)):
            try:
                fut.result(timeout=180)
                log.info(f"[Phase2] Track {name} 완료")
            except concurrent.futures.TimeoutError:
                log.warning(f"[Phase2] Track {name} 타임아웃")
                fut.cancel()
            except Exception as e:
                log.error(f"[Phase2] Track {name} 예외: {e}")
    return {
        "track_a": "\n\n".join(track_a) if track_a else "(Track A 결과 없음)",
        "track_b": "\n\n".join(track_b) if track_b else "(Track B 결과 없음)",
    }


# =====================================================================
# 13. 광역 수집 오케스트레이션 (SOURCE_TOGGLES 기반)
# =====================================================================
# 광역수집 전체 월타임(초). 한 소스가 멈춰도(예: 셀레늄 스레드 미사망으로 inner executor 가
# 종료를 못 함) 이 시간이 지나면 '완료된 소스만으로' 진행한다(멈춘 소스 포기). 이 캡이 없어서
# 2026-06-22/24 에 collect 가 30분 영구 동결 → COLLECT_DONE 미생성으로 아침 파이프라인이
# 통째로 무너졌다. 환경변수 BROAD_WALL_SEC 로 조정(기본 540초=9분).
_BROAD_WALL_SEC = int(os.getenv("BROAD_WALL_SEC", "540"))


def run_broad_collection(limit, gemini_keys, use_pw=True) -> str:
    """
    SOURCE_TOGGLES 에서 값이 1인 소스만 수집합니다.
    네이버 섹션과 RSS 매체를 토글 키 이름으로 통합 관리.
    [내구성] 한 소스가 멈춰도 _BROAD_WALL_SEC 가 지나면 '완료분만'으로 반환한다(영구 동결 방지).
    멈춘 소스 스레드는 기다리지 않고 버린다 — collect 는 서브프로세스라 반환 직후 프로세스가
    끝나며 그 스레드도 함께 죽는다. 핵심: 반드시 반환해서 collect 가 01_broad + COLLECT_DONE 을
    쓰게 한다(그래야 force_analysis·Cowork 핸드셰이크가 진행된다).
    """
    parts = []
    tasks = {}
    enabled, skipped = [], []
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=8)
    try:
        # ── 네이버 섹션 ──
        for name, url in NAVER_SECTIONS.items():
            if is_source_on(name):
                tasks[ex.submit(collect_naver_section, name, url, limit, gemini_keys, use_pw)] = name
                enabled.append(name)
            else:
                skipped.append(name)
        # ── RSS 매체 ──
        for name, url in RSS_SOURCES.items():
            if is_source_on(name):
                tasks[ex.submit(collect_rss_source, name, url, limit, gemini_keys, use_pw)] = name
                enabled.append(name)
            else:
                skipped.append(name)

        log.info(f"[광역수집] 켜짐({len(enabled)}): {', '.join(enabled)}")
        if skipped:
            log.info(f"[광역수집] 꺼짐({len(skipped)}): {', '.join(skipped)}")

        total = len(tasks)
        done = 0
        pending = set(tasks.keys())
        deadline = time.time() + _BROAD_WALL_SEC
        while pending:
            remaining = deadline - time.time()
            if remaining <= 0:
                stuck = [tasks[f] for f in pending]
                log.warning(f"[광역수집] ⏱️ 전체 월타임 {_BROAD_WALL_SEC}s 초과 — "
                            f"완료 {done}/{total} 만 사용. 미완(포기): {', '.join(stuck)}")
                break
            completed, pending = concurrent.futures.wait(
                pending, timeout=min(remaining, 10),
                return_when=concurrent.futures.FIRST_COMPLETED)
            for fut in completed:
                name = tasks[fut]
                done += 1
                try:
                    parts.append(fut.result())
                    log.info(f"[광역수집] ✅ {name} ({done}/{total})")
                except Exception as e:
                    log.error(f"[광역수집] 🚨 {name}: {e}")
    finally:
        # 멈춘 소스 스레드를 기다리지 않고 즉시 반환(wait=False). 미시작 future 는 취소.
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            ex.shutdown(wait=False)   # Python<3.9 호환
    return "\n\n".join(parts)


# =====================================================================
# 분석가 시스템 프롬프트 (Cowork/Claude가 읽을 INSTRUCTIONS에 삽입)
#   - 같은 폴더에 analyst_prompt.txt 가 있으면 그 내용을 우선 사용
#   - 없으면 아래 내장 기본값 사용
# =====================================================================
ANALYST_PROMPT_DEFAULT = r"""[역할]
당신은 시장의 '기대감(조짐)'을 포착하여 선취매하는 모멘텀 주식 트레이딩 수석 전략가이자,
기업의 펀더멘털(재무 건전성)을 깐깐하게 검증하는 리스크 관리 책임자입니다.

단기간에 +15~20%의 슈팅이 나올 수 있는 '명확한 트리거 주식 종목'을 찾되,
만년 적자 기업이나 자본잠식 등 재무 리스크가 있는 '잡주'는 꼭 '잡주'라고 표시하고
본업에서 돈을 버는(흑자) 우량/성장주 위주로 타점을 잡습니다.
리스크나 악재 등에 의해 단기간에 하락할 가능성이 있는 종목에 대한 숏 전략도 함께 제시하세요.

표시 규칙:
- 단기 급등 후 재하락 가능성 → ⚡ 단기 스윙
- 장기 보유 우량 종목 → 🌱 장투 가능
- 다음 영업일 장 개장 전 외국인·기관 매수 예상 선취매 → 🔔 장전 선취매 주목

────────────────────────────────────────
[수집 상태 태그 안내]
- 🟢 [원문]    : 기사 원문 전체 수집 — 높은 신뢰도
- 🔵 [AI복구] : Gemini 검색 복구 — 자체 검색으로 출처 재확인 필수
- 🟡 [요약본] : 원문 실패, RSS 요약 대체 — 어그로성 제목 주의, 출처 재확인
- 🔴 [실패]    : 데이터 없음 — 자체 웹검색으로 직접 조회

────────────────────────────────────────
[Phase 1 — 데이터 평가 & 듀얼 트랙 수집 명령어 발행]
제공된 광역 수집 데이터를 검토 후, 부족한 부분을 보강하기 위한 수집 명령어를 발행하세요.
명령어만 commands.txt 에 저장하고, 분석 코멘트는 쓰지 마세요.

▶ Track A — 재무·글로벌·밸류체인 (yfinance + 구글뉴스)
[SEARCH_STOCK: 종목명 또는 티커]
[SEARCH_NEWS: 검색 키워드]

▶ Track B — 네이버 실시간 속보·수급 (네이버 API, 키워드는 반드시 한글)
<NAVER_API>{"keyword": "한글 검색어", "reason": "필요 이유"}</NAVER_API>

규칙:
- Track A 2개 이상 + Track B 3개 이상 발행 권장
- 총 명령어 10개 이내
- Track B keyword는 반드시 한글

────────────────────────────────────────
[Phase 3 — 최종 한글 마스터 리포트 양식]
# 🔥 퀄리티 모멘텀 투자 마스터 리포트 ({date})
> 데이터 소스: Track A(재무·글로벌) + Track B(네이버 실시간) 듀얼 수집 반영

## 0. 🌐 매크로 환경 진단 & 시장 방향성 예측
금리·환율·원자재·글로벌 증시 흐름 종합 → '지금 왜 이 방향인가' 인과 논리로 방향성 예측.
Track B 당일 이슈를 통합할 것.

## 1. 🚨 핵심 테마 & 조짐
어떤 이벤트·뉴스가 기대감을 형성 중인지, 왜 지금 선취매인지. Track B 모멘텀 신호 포함.

## 2. 🎯 타점 진입 대기 종목 TOP 7+ (안전마진 확보 우량주)
| 순위 | 종목명 (티커) | 🏷️ 보유유형 | 진입 트리거 | 재무 건전성 | 목표 익절 시그널 |
|:---:|:---|:---:|:---|:---:|:---|
종목별 상세 코멘트: 모멘텀 강도(Track B) / 재무 안전성(Track A, PER·ROE·영업이익) /
밸류체인 포지션 / 매도 전략 / 출처(자체검색·TrackA·TrackB 명기).
※ 적자·자본잠식 종목은 이 표 금지 → 아래 잡주 섹션으로 분리.
※ "○○ 관련주" 모호 표현 절대 금지, 반드시 종목명+티커.

## 2-⚠️. 잡주 경고 섹션 (테마 연결 적자주 — 매수 금지)
| 종목명 (티커) | 재무 위험 사유 | 왜 매수 금지인가 |
|:---|:---|:---|

## 3. 🚫 투자 주의 & 숏(Short) 전략
| 종목명 (티커) | 숏 사유 | 예상 하락폭 | 손절 조건 |
|:---|:---|:---:|:---|
+ 손절 시그널(롱 청산 기준) 서술.

## 4. 🔍 내일장 개장 전 체크할 핵심 트리거 뉴스 (5개+)
각 키워드 + 한 줄 이유.

## 5. 📚 데이터 출처 및 신뢰도 평가
| 구분 | 출처 | 날짜 | 신뢰도 | 비고 |
|:---:|:---|:---:|:---:|:---|

[공통 지침]
- 정보 불완전(🟡·🔴)·AI복구(🔵) 기사는 반드시 자체 웹검색으로 팩트 재확인 + 출처 명기.
- 신뢰 출처(공신력 언론·공식 IR·거래소 공시)만. 블로그·유튜브·릴스 사용 시 "재확인 필요" 명시.
- 중복 기사에 가중치 부여 금지. 모든 기사 동등 비중.
"""


def load_analyst_prompt() -> str:
    """analyst_prompt.txt 가 있으면 그 내용을, 없으면 내장 기본값을 반환."""
    if os.path.exists(ANALYST_PROMPT_FILE):
        try:
            with open(ANALYST_PROMPT_FILE, encoding="utf-8") as f:
                txt = f.read().strip()
            if txt:
                return txt
        except Exception as e:
            log.warning(f"[프롬프트] analyst_prompt.txt 읽기 실패 → 내장값 사용: {e}")
    return ANALYST_PROMPT_DEFAULT


# =====================================================================
# 14. 세션 폴더 기반 출력 + Cowork 핸드오프
# =====================================================================
#
# [파일 핸드오프 프로토콜]  output/세션폴더/ 안에서:
#   01_broad_collection.md   ← (스크립트) 광역 수집 결과
#   INSTRUCTIONS.md          ← (스크립트) 분석가 프롬프트 + 단계별 지시
#   commands.txt             ← (Cowork)   Phase1 수집 명령어 발행
#   02_deep_collection.md    ← (스크립트) commands.txt 실행 결과
#   03_final_report.md       ← (Cowork)   최종 한글 리포트 → 메일 발송
# =====================================================================

def new_session_dir() -> str:
    """타임스탬프(초 단위) 기반 새 세션 폴더. 충돌 시 접미사로 유일성 보장."""
    base = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    d = os.path.join(OUTPUT_DIR, base)
    suffix = 1
    while os.path.exists(d):
        d = os.path.join(OUTPUT_DIR, f"{base}_{suffix}")
        suffix += 1
    os.makedirs(d, exist_ok=True)
    return d


def latest_session_dir() -> str:
    """output/ 에서 가장 최근 세션 폴더 경로(없으면 ''). _로 시작하는 폴더(_archive 등) 제외."""
    if not os.path.isdir(OUTPUT_DIR):
        return ""
    dirs = [os.path.join(OUTPUT_DIR, d) for d in os.listdir(OUTPUT_DIR)
            if os.path.isdir(os.path.join(OUTPUT_DIR, d)) and not d.startswith("_")]
    if not dirs:
        return ""
    return max(dirs, key=os.path.getmtime)


def write_broad_and_instructions(session_dir, broad_text, meta=None,
                                 phase1_results=None) -> tuple:
    """광역 수집 결과 + INSTRUCTIONS.md 작성. Returns (broad_path, instr_path)."""
    sep = "=" * 70
    broad_path = os.path.join(session_dir, "01_broad_collection.md")
    with open(broad_path, "w", encoding="utf-8") as f:
        f.write(f"# 광역 수집 데이터 — {datetime.now().strftime('%Y-%m-%d %H:%M')} KST\n\n")
        if meta:
            f.write("## 메타데이터\n")
            for k, v in meta.items():
                f.write(f"- {k}: {v}\n")
            f.write("\n")
        if phase1_results:
            f.write(f"{sep}\n## (참고) Gemini 사전 분석\n{sep}\n\n")
            f.write(format_phase1_markdown(phase1_results) + "\n\n")
        f.write(f"{sep}\n## 광역 수집 기사\n{sep}\n\n{broad_text}\n")

    date_str = datetime.now().strftime("%Y-%m-%d")
    instr_path  = os.path.join(session_dir, "INSTRUCTIONS.md")
    cmd_file    = os.path.join(session_dir, "commands.txt").replace("\\", "/")
    deep_out    = os.path.join(session_dir, "02_deep_collection.md").replace("\\", "/")
    report_out  = os.path.join(session_dir, "03_final_report.md").replace("\\", "/")
    deep_flag   = os.path.join(session_dir, "DEEP_DONE.flag").replace("\\", "/")
    deep_fail   = os.path.join(session_dir, "DEEP_FAILED.flag").replace("\\", "/")
    deep_log    = os.path.join(session_dir, "deep_run.log").replace("\\", "/")
    txt1_path   = os.path.join(session_dir, f"{date_str}_1차수집.txt").replace("\\", "/")
    txt2_path   = os.path.join(session_dir, f"{date_str}_2차심층수집.txt").replace("\\", "/")
    sess = session_dir.replace("\\", "/")
    with open(instr_path, "w", encoding="utf-8") as f:
        f.write(f"# 분석 지시서 (Cowork 전용) — {date_str}\n\n")
        f.write(f"> 이 세션 폴더: `{sess}`\n")
        f.write(f"> **중요**: 아래 모든 파일은 이 폴더 안의 것만 사용하라. "
                f"다른 날짜 폴더나 `_archive` 폴더는 절대 읽지 마라.\n")
        f.write(f"> 터미널 명령은 반드시 이 폴더에서 실행하라 "
                f"(스크립트 `research_agent.py`는 상위 폴더에 있다).\n\n")
        f.write("## 작업 순서\n\n")

        f.write(f"### 1. 광역 수집 데이터 읽기\n")
        f.write(f"이 폴더의 `01_broad_collection.md` 를 읽어라.\n\n")

        f.write(f"### 2. 수집 명령어 발행\n")
        f.write(f"아래 [분석가 역할]에 따라 Phase 1 수집 명령어를 만들어 "
                f"`{cmd_file}` 에 **덮어쓰기 저장**하라. (명령어만, 설명 금지)\n")
        f.write(f"형식:\n```\n[SEARCH_STOCK: 종목명]\n[SEARCH_NEWS: 키워드]\n"
                f'<NAVER_API>{{"keyword": "한글키워드", "reason": "이유"}}</NAVER_API>\n```\n')
        f.write(f"- Track A(SEARCH_STOCK/SEARCH_NEWS) 2개 이상, "
                f"Track B(NAVER_API) 3개 이상 권장.\n")
        f.write(f"- 전체 명령어는 20개 이내로. 단 네이버(NAVER_API)는 부담이 적으니 "
                f"필요하면 많이 사용해도 무방하다.\n")
        f.write(f"- NAVER_API의 keyword는 반드시 한글로 작성하라.\n\n")

        f.write(f"### 3. 심층 수집 실행 (백그라운드)\n")
        f.write(f"심층 수집은 수 분~수십 분 걸릴 수 있으므로 **백그라운드로 실행**하고 "
                f"출력은 로그 파일로 보낸다. 스크립트가 있는 폴더(상위)로 이동해서 실행하라.\n\n")
        f.write(f"**Windows (PowerShell):**\n")
        f.write(f"```\n")
        f.write(f'Start-Process -NoNewWindow python -ArgumentList '
                f"'research_agent.py','deep','--session','{sess}' "
                f'-RedirectStandardOutput "{deep_log}" -RedirectStandardError "{deep_log}.err"\n')
        f.write(f"```\n")
        f.write(f"**Windows (CMD) 대안:**\n")
        f.write(f"```\n")
        f.write(f'start /B cmd /c "python research_agent.py deep --session ""{sess}"" > ""{deep_log}"" 2>&1"\n')
        f.write(f"```\n")
        f.write(f"**Mac/Linux:**\n")
        f.write(f"```\n")
        f.write(f'nohup python research_agent.py deep --session "{sess}" > "{deep_log}" 2>&1 &\n')
        f.write(f"```\n\n")

        f.write(f"### 4. 완료 대기 (폴링)\n")
        f.write(f"아래 두 파일 중 하나가 생길 때까지 **30초 간격으로 확인**하라 (최대 30분):\n")
        f.write(f"- `{deep_flag}` → **성공**. 5단계로 진행.\n")
        f.write(f"- `{deep_fail}` → **실패**. `{deep_log}` 를 읽어 원인을 확인하고, "
                f"commands.txt 를 수정한 뒤 3단계부터 재실행하라.\n")
        f.write(f"30분이 지나도 둘 다 없으면 `{deep_log}` 를 확인하고 3단계를 재실행하라.\n\n")

        f.write(f"### 5. 심층 결과 검증\n")
        f.write(f"`{deep_out}` 를 읽어라. **파일 맨 끝에 "
                f"`<<<DEEP_COLLECTION_COMPLETE ...>>>` 마커가 있는지 반드시 확인**하라. "
                f"마커가 없으면 미완료이니 3단계를 재실행하라.\n\n")

        f.write(f"### 6. 보강 검색\n")
        f.write(f"🟡(요약본)·🔴(실패)·🔵(AI복구) 태그가 붙은 정보는 "
                f"**너의 웹검색**으로 사실을 재확인하고 출처(매체명·날짜 또는 URL)를 기록하라. "
                f"신뢰할 수 있는 출처만 사용하라.\n")
        f.write(f"**수집이 불안정하거나 오류가 났거나 데이터가 부족하면, "
                f"너의 자체 웹검색 기능을 적극적으로 활용해 직접 분석에 필요한 정보를 채워라.** "
                f"(실적 흑자/적자, 52주 신고가, 외국인·기관 수급, 밸류체인 2·3차 벤더 등)\n\n")

        f.write(f"### 7. 최종 리포트 작성\n")
        f.write(f"[분석가 역할]의 Phase 3 양식대로 한글 리포트를 작성하여 "
                f"`{report_out}` 에 저장하라.\n\n")

        f.write(f"### 8. 리포트 완료 신호 보내기 (발송은 호스트가 자동으로)\n")
        f.write(f"이 시스템은 **신호 기반 완전 무인 발송** 구조다. 네가 메일을 직접 보내지 않는다.\n")
        f.write(f"리포트(`03_final_report.md`)를 저장했으면, 아래 명령으로 '발송 대기' 신호만 남겨라:\n")
        f.write(f"```\n   python research_agent.py report-done --session \"{sess}\"\n```\n")
        f.write(f"출력에 `REPORT_DONE=success:...` 가 보이면 끝이다.\n")
        f.write(f"(`REPORT_DONE=failed:no_report` 이면 리포트가 저장 안 된 것이니 7단계로 돌아가라.)\n")
        f.write(f"이 신호가 남으면 호스트(이 PC)에서 상시 돌고 있는 감시 프로세스가 "
                f"**수 초~수십 초 내에** Gmail API로 발송하고 세션을 자동 보관한다.\n")
        f.write(f"**중요: 너는 mail/archive 명령을 실행하지 마라.** 샌드박스에서는 네트워크가 막혀 실패한다. "
                f"발송·보관은 호스트 감시 프로세스의 몫이다.\n\n")

        f.write(f"### 9. 끝 — 폴더를 건드리지 마라\n")
        f.write(f"세션 보관(archive)은 발송 성공 후 감시 프로세스가 자동 수행한다. "
                f"너는 세션폴더를 옮기거나 지우지 마라(감시 프로세스가 그 폴더의 리포트를 발송해야 한다).\n\n")

        f.write("=" * 70 + "\n## [분석가 역할]\n" + "=" * 70 + "\n\n")
        f.write(load_analyst_prompt().replace("{date}", date_str))
    return broad_path, instr_path


def _make_txt_attachment(session_dir, src_md_name, label) -> str:
    """
    .md 파일을 메일 첨부용 .txt 사본으로 복사.
    파일명 예: 2026-05-22_1차수집.txt / 2026-05-22_2차심층수집.txt
    Cowork가 이 txt를 그대로 메일에 첨부하면 사용자가 수집 원본을 검토할 수 있다.
    """
    src = os.path.join(session_dir, src_md_name)
    if not os.path.exists(src):
        return ""
    date_str = datetime.now().strftime("%Y-%m-%d")
    dst = os.path.join(session_dir, f"{date_str}_{label}.txt")
    try:
        with open(src, encoding="utf-8") as f:
            content = f.read()
        with open(dst, "w", encoding="utf-8") as f:
            f.write(content)
        return dst
    except Exception as e:
        log.warning(f"[첨부] txt 사본 생성 실패({label}): {e}")
        return ""


def write_deep_result(session_dir, dual_cmds, phase2) -> str:
    sep = "=" * 70
    path = os.path.join(session_dir, "02_deep_collection.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# 심층 수집 결과 — {datetime.now().strftime('%Y-%m-%d %H:%M')} KST\n\n")
        f.write(f"{sep}\n## 발행된 명령어\n{sep}\n\n")
        f.write(f"- Track A STOCK: {dual_cmds.get('stock_cmds', [])}\n")
        f.write(f"- Track A NEWS: {dual_cmds.get('news_cmds', [])}\n")
        f.write(f"- Track B NAVER: {[c.get('keyword') for c in dual_cmds.get('naver_cmds', [])]}\n\n")
        f.write(f"{sep}\n## Track A — 재무/글로벌\n{sep}\n\n{phase2.get('track_a','')}\n\n")
        f.write(f"{sep}\n## Track B — 네이버 실시간\n{sep}\n\n{phase2.get('track_b','')}\n\n")
        # 완료 마커 — Cowork가 "수집이 끝까지 정상 완료됐는지" 확인하는 신호
        f.write(f"{sep}\n<<<DEEP_COLLECTION_COMPLETE "
                f"{datetime.now().strftime('%Y-%m-%d_%H%M%S')}>>>\n")
    return path


def update_status(session_dir, stage, extra=None):
    """
    세션 진행 상태를 status.json 에 기록.
    Cowork가 '지금 어느 단계인지' 명확히 알 수 있어 오래된 파일 오독을 방지.
    stage: collected | commands_ready | deep_done | report_done | mailed | archived
    """
    path = os.path.join(session_dir, "status.json")
    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data["session_dir"] = session_dir
    data["stage"] = stage
    data["updated"] = datetime.now().isoformat()
    data.setdefault("history", []).append(
        {"stage": stage, "at": datetime.now().isoformat()})
    if extra:
        data.update(extra)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"[status] 기록 실패: {e}")
    return path


def read_commands_file(session_dir, max_age_sec=3600) -> dict:
    """
    commands.txt(Phase1 형식)를 읽어 명령어 dict로 파싱.

    [오래된 파일 오독 방지]
      - 파일 수정 시각이 max_age_sec(기본 1시간)보다 오래되면 거부.
        → 어제/이전 실행에서 남은 commands.txt를 실수로 읽는 사고 차단.
      - 빈 명령어면 거부.
    """
    path = os.path.join(session_dir, "commands.txt")
    empty = {"stock_cmds": [], "news_cmds": [], "naver_cmds": []}
    if not os.path.exists(path):
        log.warning(f"[deep] commands.txt 없음: {path}")
        return empty

    age = time.time() - os.path.getmtime(path)
    if age > max_age_sec:
        log.error(f"[deep] ⚠️ commands.txt 가 너무 오래됨 ({age/60:.0f}분 전 작성). "
                  f"이전 세션 잔여 파일일 수 있어 거부합니다. "
                  f"Cowork가 새로 작성하도록 하세요.")
        return empty

    with open(path, encoding="utf-8") as f:
        text = f.read()
    cmds = parse_dual_commands(text)
    n = len(cmds["stock_cmds"]) + len(cmds["news_cmds"]) + len(cmds["naver_cmds"])
    if n == 0:
        log.warning("[deep] commands.txt 에서 유효 명령어 0개 — 형식 확인 필요")
    else:
        log.info(f"[deep] commands.txt 로드 OK (작성 {age/60:.1f}분 전, 명령어 {n}개)")
    return cmds


def archive_session(session_dir) -> str:
    """
    메일 발송 완료 후 호출. 세션 폴더를 output/_archive/ 로 이동.
    삭제가 아닌 '이동'이라 데이터는 보존되면서, 다음 실행이 옛 파일을 읽을
    위험은 사라집니다. (실패 시에도 원본은 그대로 남음)
    """
    archive_root = os.path.join(OUTPUT_DIR, "_archive")
    os.makedirs(archive_root, exist_ok=True)
    base = os.path.basename(session_dir.rstrip("/\\"))
    dest = os.path.join(archive_root, base)
    # 동일 이름 충돌 시 접미사
    if os.path.exists(dest):
        dest = dest + "_" + datetime.now().strftime("%H%M%S")
    try:
        shutil.move(session_dir, dest)
        log.info(f"[archive] 세션 보관 완료 → {dest}")
        return dest
    except Exception as e:
        log.error(f"[archive] 이동 실패 (원본 유지): {e}")
        return session_dir


# =====================================================================
# 15. 메인 (argparse 서브커맨드)
# =====================================================================
def cmd_test(args):
    log.info("=== 연결 테스트 시작 ===")
    gk = load_gemini_keys()
    cid, csec = load_naver_keys()
    log.info(f"Gemini 키: {len(gk)}개 | Naver: cid={'O' if cid else 'X'} secret={'O' if csec else 'X'}")
    log.info(f"패키지: genai={GENAI_AVAILABLE} traf={TRAF_AVAILABLE} pw={PW_AVAILABLE} "
             f"yf={YF_AVAILABLE} fdr={FDR_AVAILABLE} gmail_api={GMAIL_API_AVAILABLE}")
    on = [n for n in list(NAVER_SECTIONS) + list(RSS_SOURCES) if is_source_on(n)]
    log.info(f"수집 ON 소스({len(on)}): {', '.join(on)}")
    log.info(f"기사 수/소스: {ARTICLES_PER_SOURCE} | Playwright: {USE_PLAYWRIGHT}")

    if gk and GENAI_AVAILABLE:
        rot = KeyRotator(gk[:1])
        t0 = time.time()
        txt, err = gemini_call(rot, "Reply with the single word: OK",
                               generation_config={"temperature": 0, "max_output_tokens": 10},
                               timeout_sec=20, max_attempts=1)
        if txt and "OK" in txt.upper():
            log.info(f"✅ Gemini 정상 ({time.time()-t0:.1f}s): {txt.strip()[:40]}")
        else:
            log.error(f"❌ Gemini 실패 ({time.time()-t0:.1f}s): {err}")

    if cid and csec:
        t0 = time.time()
        res = fetch_naver_news_api("테스트", cid, csec, display=1, max_retries=0)
        if "🚨" in res:
            log.error(f"❌ Naver 실패:\n{res[:300]}")
        else:
            log.info(f"✅ Naver 정상 ({time.time()-t0:.1f}s)")

    # ── 메일 설정 점검 (2중 구조) ──
    mcfg = load_mail_config()
    method = (mcfg.get("method", "auto") if mcfg else "auto").lower()
    log.info(f"메일 발송 방식(method): {method}")

    # 공통 필수값
    base_ok, base_why = validate_mail_config(mcfg, method="api")  # sender/to 만 검사
    if base_ok:
        log.info(f"✅ 메일 기본 설정 OK — 발신:{mcfg['sender']} → 수신:{mcfg['to']}")
    else:
        log.warning(f"⚠️ 메일 기본 설정 미완료: {base_why}")

    # Gmail API 경로 점검
    if method in ("auto", "api"):
        if not GMAIL_API_AVAILABLE:
            log.warning("⚠️ Gmail API 패키지 미설치 — "
                        "pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib")
        elif not os.path.exists(GMAIL_CREDENTIALS_FILE):
            log.warning(f"⚠️ gmail_credentials.json 없음: {GMAIL_CREDENTIALS_FILE} "
                        f"(GCP에서 OAuth 클라이언트 다운로드 필요)")
        elif not os.path.exists(GMAIL_TOKEN_FILE):
            log.warning("⚠️ gmail_token.json 없음 — 본인 PC에서 "
                        "'python research_agent.py mail --method api ...' 1회 실행해 "
                        "브라우저 인증으로 토큰을 생성하세요.")
        else:
            log.info("✅ Gmail API 자격 파일(credentials/token) 존재 — API 발송 준비됨")

    # SMTP 경로 점검 (백업)
    if method in ("auto", "smtp"):
        smtp_ok, smtp_why = validate_mail_config(mcfg, method="smtp")
        if smtp_ok:
            log.info("✅ SMTP 백업 설정 OK (app_password 확인됨)")
        else:
            log.warning(f"⚠️ SMTP 백업 설정 미완료: {smtp_why} "
                        f"(Windows 스케줄러 백업 발송에 필요)")

    log.info("   (실제 발송 테스트: 'python research_agent.py mail --session ... --method auto')")
    log.info("=== 연결 테스트 종료 ===")


def cmd_krx(args):
    m = load_krx_tickers(force_refresh=True)
    log.info(f"KRX 종목 {len(m)}개 확보 완료")


def cmd_archive(args):
    """메일 발송 완료 후 세션 폴더를 보관 이동. (Cowork가 마지막에 호출)"""
    sess = args.session
    if getattr(args, "latest", False) and not sess:
        sess = latest_session_dir()
    if not sess or not os.path.isdir(sess):
        log.error(f"[archive] 세션 폴더 없음: {sess}")
        return
    update_status(sess, "mailed")
    dest = archive_session(sess)
    print(f"\nARCHIVED_TO={dest}")


def _md_inline(text: str) -> str:
    """인라인 마크다운(굵게/기울임/코드/링크) → HTML. HTML 이스케이프 포함."""
    text = html.escape(text, quote=False)
    # `code`
    text = re.sub(r"`([^`]+)`",
                  r'<code style="background:#f5f5f5;padding:1px 5px;border-radius:3px;font-size:12px;">\1</code>',
                  text)
    # **bold**
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    # *italic* (단순)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    # [텍스트](URL)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)",
                  r'<a href="\2" style="color:#1a73e8;">\1</a>', text)
    return text


# =====================================================================
# 리포트 색상화(가독성) — 이모지 대신 '색'으로 구조/신호를 강조한다.
#   Gmail 은 <style> 를 일부 제거하므로 전부 '인라인 스타일'(span color)로 주입.
#   태그/속성은 건드리지 않도록 '<...>' 로 split 한 텍스트 조각만 변환한다.
# =====================================================================
_C_POS = "#1a7f37"    # 긍정(녹색): 호재·흑자·매집·상승
_C_NEG = "#c0392b"    # 부정(적색): 악재·적자·분산·하락
_C_WARN = "#b26a00"   # 주의(호박색): 관망·중립·선반영·미확인
_C_MUTE = "#6a737d"   # 약함(회색)

# 대괄호 짧은 태그 → 배지(badge). (라벨, 배경색, 글자색)
_BADGE_MAP = {
    "장투가능": ("#1a7f37", "#ffffff"),
    "단기스윙": ("#1f6feb", "#ffffff"),
    "장전선취매": ("#8250df", "#ffffff"),
    "잡주 경고": ("#c0392b", "#ffffff"),
    "잡주": ("#c0392b", "#ffffff"),
    "주의": ("#c0392b", "#ffffff"),
    "경고": ("#c0392b", "#ffffff"),
    "호재": ("#1a7f37", "#ffffff"),
    "악재": ("#c0392b", "#ffffff"),
    "강세": ("#1a7f37", "#ffffff"),
    "약세": ("#c0392b", "#ffffff"),
    "중립": ("#6a737d", "#ffffff"),
    "관망": ("#6a737d", "#ffffff"),
    "확실": ("#0a5fb4", "#ffffff"),
    "미확인": ("#b26a00", "#ffffff"),
}

# 자유 텍스트 키워드 색(긴 단어 우선 매칭 위해 길이 내림차순 정렬해 사용)
_POS_WORDS = ["흑자전환", "흑자지속", "순매수", "우상향", "흑자", "매집", "강세", "상승", "개선", "성장"]
_NEG_WORDS = ["자본잠식", "어닝쇼크", "상장폐지", "순매도", "급감", "적자", "분산", "약세", "하락",
              "과열", "횡령", "배임", "감액", "악재"]
_WARN_WORDS = ["데이터 노후", "미확인", "선반영", "관망", "중립", "결측", "노후"]


def _span(color, text, bold=False):
    w = "font-weight:600;" if bold else ""
    return f'<span style="color:{color};{w}">{text}</span>'


def _badge(label, bg, fg):
    return (f'<span style="display:inline-block;padding:1px 7px;border-radius:4px;'
            f'background:{bg};color:{fg};font-size:12px;font-weight:600;'
            f'white-space:nowrap;">[{label}]</span>')


def _colorize_report_html(html: str) -> str:
    """HTML 텍스트 조각에 의미색을 입힌다(태그/속성은 보존)."""
    parts = re.split(r'(<[^>]+>)', html)   # 짝수=텍스트, 홀수=태그
    pos_w = sorted(_POS_WORDS, key=len, reverse=True)
    neg_w = sorted(_NEG_WORDS, key=len, reverse=True)
    warn_w = sorted(_WARN_WORDS, key=len, reverse=True)
    for idx in range(0, len(parts), 2):
        seg = parts[idx]
        if not seg or not seg.strip():
            continue
        store = []

        # 1) 짧은 대괄호 태그 → 배지(placeholder 로 보호해 이후 변환에서 제외)
        def _protect(m):
            label = m.group(1).strip()
            bf = _BADGE_MAP.get(label)
            if not bf:
                return m.group(0)
            store.append(_badge(label, bf[0], bf[1]))
            return f"\x00B{len(store) - 1}\x00"
        seg = re.sub(r'\[([^\[\]]{1,8})\]', _protect, seg)

        # 2) 부호 숫자(+12% / -30 / +33조 …) — 날짜(2026-06-18)는 제외(부호 앞이 단어/숫자면 skip)
        seg = re.sub(
            r'(?<![\w\d])([+\-]\d[\d,\.]*\s?(?:%|pp|조|억|원|배|bp)?)',
            lambda m: _span(_C_POS if m.group(1).lstrip()[0] == '+' else _C_NEG,
                            m.group(1), True),
            seg)

        # 3) 키워드 색(긴 단어 우선)
        for w in pos_w:
            seg = seg.replace(w, _span(_C_POS, w))
        for w in neg_w:
            seg = seg.replace(w, _span(_C_NEG, w))
        for w in warn_w:
            seg = seg.replace(w, _span(_C_WARN, w))

        # 4) 배지 placeholder 복원
        seg = re.sub(r'\x00B(\d+)\x00', lambda m: store[int(m.group(1))], seg)
        parts[idx] = seg

    html = "".join(parts)

    # 5) 표 본문 행 zebra 줄무늬(헤더행 <tr><th> 는 제외)
    cnt = [0]

    def _zebra(_m):
        cnt[0] += 1
        return f'<tr style="background:{"#ffffff" if cnt[0] % 2 else "#f6f8fa"};">'
    html = re.sub(r'<tr>(?=\s*<td)', _zebra, html)
    return html


def _markdown_to_html(md_text: str) -> str:
    """
    마크다운 리포트를 Gmail에서 깔끔히 렌더링되는 HTML로 변환.
    - markdown 패키지가 있으면 사용(표/펜스코드 확장), 없으면 자체 변환기로 폴백.
    - Gmail은 <style> 블록을 일부 제거하므로 표/제목 등에 '인라인 스타일'을 직접 주입.
    - 이모지 대신 '색'으로 신호를 강조한다(_colorize_report_html).
    """
    body_html = None
    try:
        import markdown as _mdlib
        body_html = _mdlib.markdown(
            md_text, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])
    except Exception:
        body_html = _fallback_md_to_html(md_text)

    # ── 인라인 스타일 주입 (Gmail 호환) ──
    repl = {
        "<table>": '<table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;margin:14px 0;font-size:13px;border:1px solid #e3e7ee;">',
        "<th>": '<th style="border:1px solid #d4dbe8;padding:9px 11px;background:#e8edf6;color:#1f2a44;text-align:left;font-weight:700;font-size:12.5px;">',
        "<td>": '<td style="border:1px solid #e6eaf1;padding:9px 11px;vertical-align:top;font-size:13px;">',
        "<blockquote>": '<blockquote style="border-left:4px solid #1f4e8c;background:#f4f7fc;margin:12px 0;padding:10px 15px;border-radius:0 8px 8px 0;color:#3a4a66;">',
        "<h1>": '<h1 style="font-size:22px;margin:6px 0 12px;padding-bottom:8px;border-bottom:2px solid #1f2a44;color:#1f2a44;font-weight:800;">',
        "<h2>": '<h2 style="font-size:17px;margin:24px 0 10px;padding:9px 14px;background:#eef2fa;border-left:5px solid #1f4e8c;border-radius:0 8px 8px 0;color:#1f3a66;font-weight:800;">',
        "<h3>": '<h3 style="font-size:15px;margin:18px 0 7px;padding-left:9px;border-left:3px solid #8aa0c0;color:#2b3a55;font-weight:700;">',
        "<h4>": '<h4 style="font-size:14px;margin:14px 0 6px;color:#3a4a66;font-weight:700;">',
        "<hr>": '<hr style="border:none;border-top:1px solid #e6eaf1;margin:20px 0;">',
        "<hr />": '<hr style="border:none;border-top:1px solid #e6eaf1;margin:20px 0;">',
        "<ul>": '<ul style="margin:9px 0;padding-left:22px;">',
        "<ol>": '<ol style="margin:9px 0;padding-left:22px;">',
        "<code>": '<code style="background:#eef1f6;padding:1px 5px;border-radius:3px;font-size:12px;">',
    }
    for k, v in repl.items():
        body_html = body_html.replace(k, v)
    # markdown 라이브러리가 정렬 스타일을 단 th/td 처리 (콜론 뒤 공백 허용)
    body_html = re.sub(
        r'<th style="text-align:\s*(\w+)\s*;?\s*">',
        lambda m: f'<th style="border:1px solid #d4dbe8;padding:9px 11px;background:#e8edf6;'
                  f'color:#1f2a44;font-weight:700;font-size:12.5px;text-align:{m.group(1)};">', body_html)
    body_html = re.sub(
        r'<td style="text-align:\s*(\w+)\s*;?\s*">',
        lambda m: f'<td style="border:1px solid #e6eaf1;padding:9px 11px;vertical-align:top;'
                  f'font-size:13px;text-align:{m.group(1)};">', body_html)

    # ── 의미색 입히기(이모지 대신 색으로 신호 강조) ──
    body_html = _colorize_report_html(body_html)

    return (
        '<div style="font-family:\'Apple SD Gothic Neo\',\'Malgun Gothic\','
        "'Segoe UI',Roboto,sans-serif;font-size:14px;line-height:1.6;color:#1a1a1a;"
        'max-width:860px;">' + body_html + "</div>"
    )


def _fallback_md_to_html(md_text: str) -> str:
    """markdown 패키지가 없을 때 쓰는 최소 변환기. 표·제목·목록·인용·구분선 지원."""
    lines = md_text.split("\n")
    out = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        s = line.strip()

        # 표: |...| 로 시작하고 다음 줄이 구분행(|---|)
        if s.startswith("|") and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]):
            header = [c.strip() for c in s.strip("|").split("|")]
            i += 2  # 헤더 + 구분행 건너뜀
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            t = ["<table>", "<thead><tr>"]
            t += [f"<th>{_md_inline(h)}</th>" for h in header]
            t.append("</tr></thead><tbody>")
            for r in rows:
                t.append("<tr>" + "".join(f"<td>{_md_inline(c)}</td>" for c in r) + "</tr>")
            t.append("</tbody></table>")
            out.append("".join(t))
            continue

        # 제목
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_md_inline(m.group(2))}</h{lvl}>")
            i += 1
            continue

        # 구분선
        if re.match(r"^(-{3,}|={3,}|\*{3,})$", s):
            out.append("<hr>")
            i += 1
            continue

        # 인용
        if s.startswith(">"):
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(_md_inline(lines[i].strip()[1:].strip()))
                i += 1
            out.append("<blockquote>" + "<br>".join(quote) + "</blockquote>")
            continue

        # 목록
        if re.match(r"^[-*+]\s+", s):
            items = []
            while i < n and re.match(r"^[-*+]\s+", lines[i].strip()):
                items.append("<li>" + _md_inline(re.sub(r"^[-*+]\s+", "", lines[i].strip())) + "</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue

        # 빈 줄
        if not s:
            i += 1
            continue

        # 일반 문단 (연속 줄 묶기)
        para = []
        while i < n and lines[i].strip() and not re.match(r"^(#{1,4}\s|>|\||[-*+]\s|-{3,}|={3,})", lines[i].strip()):
            para.append(_md_inline(lines[i].strip()))
            i += 1
        out.append("<p>" + "<br>".join(para) + "</p>")

    return "\n".join(out)


# 메일 본문 하단에 자동 첨부되는 '세력강도(force_score) 점수 설명' (이모지 금지 — 4바이트 깨짐 방지)
_FORCE_SCORE_EXPLAINER = """

---

## 세력강도(force_score) 점수 설명

이 리포트의 '세력강도(force_score)'는 공개 데이터로 산출한 종목별 수급·모멘텀 종합 점수입니다.
범위는 -100 ~ +100 이며, 직전 거래일 종가 기준의 추정치입니다.

산출 4축:
- 수급: 외국인·기관의 순매수 강도 (매집/분산)
- 거래량: 평균 대비 거래량 급증 여부
- 모멘텀: RSI (과열/침체)
- OBV: 누적 매수세 추세

점수 해석:
- +40 이상: 강한 매집 (세력 유입) — 동행 검토 신호
- +15 ~ +40: 매집 우위 — 긍정 가산
- -15 ~ +15: 중립 / 관망
- -40 ~ -15: 분산 우위 — 경계
- -40 이하: 강한 분산 (떠넘기기 의심) — 주의

수급 출처 표기:
- krx: KRX 인증 데이터 (외국인+기관+개인 모두 포함, 신뢰 높음)
- naver: KRX 일시 실패 시 폴백 (외국인+기관만, 개인 결측 — 신뢰 중간)
- 결측: 수급 데이터 없음 (수급축 중립 처리, 모멘텀/OBV 위주로 판단)

[주의] force_score 는 전 거래일 기준 공개데이터 추정치이며, 현재가·최신 뉴스와 교차검증이
필요합니다. 본 리포트는 리서치 참고 자료이며 투자 자문이 아닙니다.
"""


# =====================================================================
# 메일 본문 상단 대시보드(주식 정보 사이트 느낌) — 이메일 호환(인라인 스타일 + 테이블 막대).
#   predictions.json 의 시장 방향성·핵심 픽을 시각 카드로 요약해 본문 위에 얹는다.
#   외부 CSS/JS/SVG 없이 width% bgcolor 테이블 막대로 그려 Gmail/네이버/Outlook 호환.
#   데이터가 없으면 각 위젯은 조용히 생략되어 기존 동작과 호환된다.
# =====================================================================
_FONT = "'Apple SD Gothic Neo','Malgun Gothic','Segoe UI',Roboto,sans-serif"

# 시장 방향(dir) → (표시문구, 강조색, 카드 배경틴트)
_DIR_MAP = {
    "up":      ("상승 우위", _C_POS, "#eaf6ee"),
    "down":    ("하락 우위", _C_NEG, "#fdecea"),
    "neutral": ("중립/관망", _C_MUTE, "#eef1f6"),
}
# 선반영(preprice) → (표시문구, 색)
_PRE_MAP = {
    "강함":   ("선반영 강함", _C_NEG),
    "부분":   ("부분 반영",   _C_WARN),
    "미반영": ("미반영",      _C_POS),
}
# 예상 상승 시점(timing) → (표시문구, 배경, 글자색). 보유태그와 별개 축(v8.3).
_TIMING_PILL = {
    "임박": ("임박 · 오늘~내일", "#fbe5d6", "#b3471a"),
    "단기": ("단기 · ~1주",      "#e3eefc", "#1f4e8c"),
    "중기": ("중기 · 2주+",      "#eceff3", "#475569"),
}


def _timing_pill(timing) -> str:
    """예상 상승 시점 칩(없으면 '')."""
    t = _TIMING_PILL.get(str(timing).strip())
    if not t:
        return ""
    label, bg, fg = t
    return (f'<div style="margin-top:7px;"><span style="display:inline-block;'
            f'padding:2px 10px;border-radius:11px;background:{bg};color:{fg};'
            f'font-size:11px;font-weight:700;">{label}</span></div>')


def _esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def _load_json_safe(path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _meter_bar(pct, color, height=8) -> str:
    """이메일 호환 가로 막대그래프(테이블 2칸, width% + bgcolor). pct 0~100."""
    try:
        p = max(0, min(100, int(round(float(pct)))))
    except Exception:
        p = 0
    rest = 100 - p
    cells = ""
    if p > 0:
        cells += (f'<td bgcolor="{color}" width="{p}%" style="background:{color};'
                  f'height:{height}px;line-height:{height}px;font-size:0;">&nbsp;</td>')
    if rest > 0:
        cells += (f'<td bgcolor="#e7ecf4" width="{rest}%" style="background:#e7ecf4;'
                  f'height:{height}px;line-height:{height}px;font-size:0;">&nbsp;</td>')
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
            f'style="border-collapse:separate;width:100%;table-layout:fixed;'
            f'border-radius:{height}px;overflow:hidden;"><tr>{cells}</tr></table>')


def _fmt_pct(v) -> tuple:
    """숫자 → ('+1.5%', 색). 실패 시 ('', mute)."""
    try:
        f = float(v)
    except Exception:
        return "", _C_MUTE
    sign = "+" if f > 0 else ""
    color = _C_POS if f > 0 else (_C_NEG if f < 0 else _C_MUTE)
    return f"{sign}{f:g}%", color


def _market_card(title, call) -> str:
    """시장(코스피/코스닥) 방향성 카드: 방향 + 목표% + 확신도 막대 + 무효화."""
    if not isinstance(call, dict):
        return ""
    d = str(call.get("dir", "")).lower()
    label, color, tint = _DIR_MAP.get(d, _DIR_MAP["neutral"])
    try:
        conv_f = float(call.get("conviction"))
    except Exception:
        conv_f = None
    conv_txt = f"확신도 {conv_f:.2f}" if conv_f is not None else "확신도 -"
    pct = conv_f * 100 if conv_f is not None else 0
    tgt_txt, tgt_color = _fmt_pct(call.get("target_pct"))
    tgt_html = (f'<span style="font-size:12px;color:{tgt_color};font-weight:700;'
                f'margin-left:9px;">목표 {tgt_txt}</span>') if tgt_txt else ""
    inval = _esc(call.get("invalidation", ""))
    inval_html = (f'<div style="font-size:11px;color:#6a737d;margin-top:8px;'
                  f'line-height:1.45;">무효화: {inval}</div>') if inval else ""
    # 박스별 추가 설명(Cowork가 predictions.json 의 comment 에 쓰면 흰색 인셋으로 표시)
    comment = _esc(call.get("comment", ""))
    comment_html = (f'<div style="margin-top:9px;background:#ffffff;border:1px solid #e6eaf1;'
                    f'border-radius:8px;padding:8px 10px;font-size:12px;color:#46506a;'
                    f'line-height:1.55;">{comment}</div>') if comment else ""
    return (
        f'<td width="50%" valign="top" style="padding:5px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'bgcolor="{tint}" style="background:{tint};border:1px solid #e1e6ee;'
        f'border-left:4px solid {color};border-radius:11px;">'
        f'<tr><td style="padding:13px 15px;">'
        f'<div style="font-size:11px;color:#56607a;font-weight:700;letter-spacing:.5px;">{_esc(title)}</div>'
        f'<div style="margin:5px 0 10px;"><span style="font-size:20px;font-weight:800;'
        f'color:{color};">{label}</span>{tgt_html}</div>'
        f'<div style="font-size:11px;color:#56607a;margin-bottom:4px;">{conv_txt}</div>'
        f'{_meter_bar(pct, color)}{comment_html}{inval_html}'
        f'</td></tr></table></td>'
    )


def _top_picks_html(picks) -> str:
    """핵심 픽 TOP 3 카드(확신도 높은 순): 종목/티커 + 태그 배지 + 확신도 막대 + 선반영 + 논지."""
    if not isinstance(picks, list) or not picks:
        return ""
    clean = [p for p in picks if isinstance(p, dict)]

    def _conv(x):
        try:
            return float(x.get("conviction") or 0)
        except Exception:
            return 0.0
    top = sorted(clean, key=_conv, reverse=True)[:3]
    if not top:
        return ""
    rows = []
    for p in top:
        name = _esc(p.get("name", ""))
        tk = _esc(p.get("ticker", ""))
        tag = str(p.get("tag", "")).strip()
        bf = _BADGE_MAP.get(tag)
        badge = _badge(tag, bf[0], bf[1]) if bf else ""
        c = _conv(p)
        pre = str(p.get("preprice", "")).strip()
        pl, pc = _PRE_MAP.get(pre, ("", _C_MUTE))
        pre_html = (f'<span style="font-size:11px;color:{pc};font-weight:700;'
                    f'margin-left:7px;">{pl}</span>') if pl else ""
        thesis = _esc(str(p.get("thesis") or "").strip())
        thesis_html = (f'<div style="font-size:11px;color:#6a737d;margin-top:7px;'
                       f'line-height:1.5;">{thesis}</div>') if thesis else ""
        # 박스별 추가 설명(Cowork가 predictions.json picks[].comment 에 쓰면 옅은 인셋으로 표시)
        comment = _esc(str(p.get("comment") or "").strip())
        comment_html = (f'<div style="margin-top:7px;background:#f6f8fc;border-radius:8px;'
                        f'padding:7px 10px;font-size:12px;color:#46506a;line-height:1.5;">'
                        f'{comment}</div>') if comment else ""
        timing_html = _timing_pill(p.get("timing", ""))   # 예상 상승 시점 칩(v8.3)
        rows.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'bgcolor="#ffffff" style="background:#ffffff;border:1px solid #e6eaf1;'
            f'border-radius:10px;margin:6px 0;"><tr><td style="padding:11px 14px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td valign="middle" style="font-size:14px;font-weight:800;color:#1f2a44;">'
            f'{name} <span style="font-size:11px;color:#8893a8;font-weight:600;">{tk}</span></td>'
            f'<td valign="middle" align="right" style="white-space:nowrap;">{badge}{pre_html}</td>'
            f'</tr></table>{timing_html}'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin-top:8px;"><tr>'
            f'<td width="64%" valign="middle">{_meter_bar(c * 100, _C_POS if c >= 0.5 else _C_WARN, 7)}</td>'
            f'<td width="36%" valign="middle" align="right" '
            f'style="font-size:11px;color:#56607a;font-weight:600;">확신도 {c:.2f}</td>'
            f'</tr></table>{thesis_html}{comment_html}'
            f'</td></tr></table>'
        )
    head = ('<div style="font-size:13px;font-weight:800;color:#1f2a44;'
            'margin:16px 2px 4px;">오늘의 핵심 픽 TOP 3</div>')
    return head + "".join(rows)


def _report_date_str(session_dir) -> str:
    base = os.path.basename(os.path.normpath(session_dir))
    m = re.match(r"(\d{4}-\d{2}-\d{2})", base)
    if m:
        return m.group(1)
    pred = _load_json_safe(os.path.join(session_dir, "predictions.json"))
    if pred.get("date"):
        return str(pred["date"])
    return datetime.now().strftime("%Y-%m-%d")


def _header_banner(date_str) -> str:
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'bgcolor="#1f2a44" style="background:#1f2a44;'
        f'background:linear-gradient(120deg,#1f2a44 0%,#28406e 58%,#2f5a8f 100%);'
        f'border-radius:14px;margin:0 0 16px;"><tr><td style="padding:20px 22px;">'
        f'<div style="font-size:11px;color:#9db4d8;font-weight:700;letter-spacing:2px;">'
        f'DAILY EQUITY RESEARCH</div>'
        f'<div style="font-size:23px;color:#ffffff;font-weight:800;margin-top:5px;">'
        f'모멘텀 투자 리포트</div>'
        f'<div style="font-size:13px;color:#c5d4ea;margin-top:7px;">'
        f'{_esc(date_str)} &nbsp;·&nbsp; 한국 주식 리서치 데스크</div>'
        f'</td></tr></table>'
    )


def _dashboard_html(session_dir) -> str:
    """predictions.json → 시장 방향성 카드 + 핵심 픽 카드. 데이터 없으면 ''(생략)."""
    pred = _load_json_safe(os.path.join(session_dir, "predictions.json"))
    mc = pred.get("market_call") if isinstance(pred.get("market_call"), dict) else {}
    k = _market_card("KOSPI · 코스피", mc.get("kospi")) if mc else ""
    q = _market_card("KOSDAQ · 코스닥", mc.get("kosdaq")) if mc else ""
    cards = ""
    if k or q:
        cards = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                 f'style="margin:2px 0;"><tr>{k}{q}</tr></table>')
    picks = _top_picks_html(pred.get("picks"))
    if not cards and not picks:
        return ""
    regime = _esc(pred.get("regime", ""))
    regime_html = (f'<div style="font-size:12px;color:#56607a;margin:2px 2px 10px;">'
                   f'<span style="font-weight:800;color:#1f2a44;">국면</span> &nbsp;{regime}</div>'
                   ) if regime else ""
    head = ('<div style="font-size:13px;font-weight:800;color:#1f2a44;margin:2px 2px 6px;">'
            '시장 방향성 한눈에</div>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'bgcolor="#f7f9fc" style="background:#f7f9fc;border:1px solid #e6eaf1;'
            f'border-radius:14px;margin:0 0 18px;"><tr><td style="padding:13px 13px 15px;">'
            f'{head}{regime_html}{cards}{picks}</td></tr></table>')


def _footer_html() -> str:
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:24px 0 2px;"><tr><td style="border-top:1px solid #e6eaf1;'
        'padding:14px 2px 0;font-size:11px;color:#8893a8;line-height:1.65;">'
        '본 리포트는 리서치 보조 자료이며 투자자문이 아닙니다. 표시된 점수·방향성·확신도는 '
        '공개데이터 기반 추정치로 수익을 보장하지 않으며, 투자 판단과 책임은 본인에게 있습니다.'
        '</td></tr></table>'
    )


def _email_wrap(inner_html) -> str:
    """전체를 '연한 회색 페이지 + 흰색 카드' 레이아웃으로 감싼다(웹페이지 느낌, 모바일 대응)."""
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'bgcolor="#eef1f6" style="background:#eef1f6;width:100%;margin:0;padding:0;">'
        '<tr><td align="center" style="padding:18px 12px;">'
        '<table role="presentation" width="880" cellpadding="0" cellspacing="0" '
        'style="width:100%;max-width:880px;background:#ffffff;border-radius:16px;'
        'border:1px solid #e4e8f0;overflow:hidden;">'
        f'<tr><td style="padding:18px 20px 22px;font-family:{_FONT};'
        'font-size:14px;line-height:1.6;color:#1a1a1a;">'
        + inner_html +
        '</td></tr></table></td></tr></table>'
    )


def _prev_day_change_md(session_dir: str) -> str:
    """
    추천 종목(predictions.json picks/shorts)의 '전일 등락률'(= 추천 직전 거래일 종가 등락)을
    마크다운 표로 반환한다. 아침 06:30 발송이면 장 시작 전이라 이 값이 곧 '전일 등락률'.

    목적: 추천 종목의 재료가 이미 주가에 **선반영**됐는지(어제 크게 뛰었는지) 독자가 점검하게 함.
    데이터는 세션 안에 이미 있는 파일에서만 얻어 **새 API 호출/룩어헤드 없음**:
      1순위 force_scores.json(detail.price_change_pct = 일봉 1일 전일대비, 실행시점 최신 = 진짜 전일)
      2순위 fsc_prices.json(change_pct; 금융위 API 는 하루 지연될 수 있어 폴백)
    데이터 없거나 predictions.json 없으면 "" (본문 흐름 무영향).
    ※ 콘솔 print 금지 문자열이 아니며 파일은 UTF-8 저장. astral(4byte) 이모지는
       발송경로의 surrogate-strip 에 지워질 수 있어 BMP 기호(▲▼)만 사용.
    """
    try:
        pj = os.path.join(session_dir, "predictions.json")
        if not os.path.exists(pj):
            return ""
        with open(pj, encoding="utf-8") as f:
            pred = json.load(f)
        picks = pred.get("picks") or []
        shorts = pred.get("shorts") or []
        if not picks and not shorts:
            return ""

        def _t6(v):
            m = re.search(r"\d{6}", str(v or ""))
            return m.group(0) if m else None

        # ticker6 -> change_pct(%)  ※ 1순위 force_scores(실행시점 최신 = 진짜 전일),
        #                              2순위 fsc_prices(금융위 API 는 하루 지연될 수 있어 폴백)
        chg = {}
        fs = os.path.join(session_dir, "force_scores.json")
        if os.path.exists(fs):
            try:
                with open(fs, encoding="utf-8") as f:
                    d = json.load(f)
                for t in (d.get("tickers") or []):
                    tk = _t6(t.get("ticker"))
                    cp = (t.get("detail") or {}).get("price_change_pct")
                    if tk and cp is not None:
                        chg[tk] = cp
            except Exception:
                pass
        fp = os.path.join(session_dir, "fsc_prices.json")
        if os.path.exists(fp):
            try:
                with open(fp, encoding="utf-8") as f:
                    d = json.load(f)
                rows = d.get("tickers") if isinstance(d, dict) else d
                for t in (rows or []):
                    tk = _t6(t.get("ticker"))
                    cp = t.get("change_pct")
                    if tk and tk not in chg and cp is not None:   # force_scores 없는 종목만 보충
                        chg[tk] = cp
            except Exception:
                pass
        if not chg:
            return ""

        def _fmt(pct):
            arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "–")
            return f"{arrow} {pct:+.2f}%"

        rows = ["", "### 추천 종목 전일 등락률 — 선반영 점검 (추천 직전 거래일 종가 기준)",
                "> 추천 직전 거래일 종가 등락률입니다. 이미 큰 폭 오른(내린) 종목은 재료가 "
                "주가에 **선반영**됐을 수 있으니 추격 진입에 유의하세요.",
                "",
                "| 구분 | 종목 (티커) | 전일 등락률 |",
                "|:---:|:---|:---:|"]
        for p in picks:
            tk = _t6(p.get("ticker"))
            nm = p.get("name") or tk or "?"
            v = chg.get(tk)
            rows.append(f"| 롱 | {nm} ({tk or '—'}) | {_fmt(v) if v is not None else '—'} |")
        for s in shorts:
            tk = _t6(s.get("ticker"))
            nm = s.get("name") or tk or "?"
            v = chg.get(tk)
            rows.append(f"| 숏 | {nm} ({tk or '—'}) | {_fmt(v) if v is not None else '—'} |")
        rows.append("")
        return "\n".join(rows)
    except Exception as e:
        log.warning(f"[prev_change] 전일등락률 블록 생성 실패(무시): {e}")
        return ""


def render_report_html(session_dir: str) -> str:
    """
    세션의 03_final_report.md → 03_final_report.html 생성 후 경로 반환.
    상단에 시장 방향성/핵심 픽 대시보드(predictions.json 기반)를 얹어 '주식 정보 사이트'
    느낌의 메일 본문을 만든다. 리포트가 없으면 "" 반환.
    """
    md_path = os.path.join(session_dir, "03_final_report.md")
    if not os.path.exists(md_path):
        return ""
    try:
        with open(md_path, encoding="utf-8") as f:
            md = f.read()
        # 추천 종목 '전일 등락률(선반영 점검)' 표를 본문 상단에 삽입(데이터 없으면 무영향)
        _prev = _prev_day_change_md(session_dir)
        if _prev:
            md = _prev + "\n" + md
        md = md + _FORCE_SCORE_EXPLAINER   # 메일 본문에 세력강도 점수 설명 자동 첨부
        body_html = _markdown_to_html(md)
        # 대시보드(predictions.json 기반)는 '선택 위젯'이라 실패해도 헤더/푸터/본문은 유지한다.
        try:
            dash = _dashboard_html(session_dir)
        except Exception as e:
            log.warning(f"[render] 대시보드 합성 실패(헤더/본문/푸터는 정상 유지): {e}")
            dash = ""
        # 헤더 배너 + 대시보드 + 본문 + 푸터를 이메일 호환 카드 레이아웃으로 합성.
        try:
            inner = (_header_banner(_report_date_str(session_dir))
                     + dash + body_html + _footer_html())
            html = _email_wrap(inner)
        except Exception as e:
            log.warning(f"[render] 이메일 프레임 합성 실패, 본문만 사용: {e}")
            html = body_html
        html_path = os.path.join(session_dir, "03_final_report.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        return html_path
    except Exception as e:
        log.warning(f"[render] HTML 변환 실패: {e}")
        return ""


def cmd_report_done(args):
    """
    Cowork가 최종 리포트 작성을 끝낸 뒤 호출. '발송 대기' 신호를 남긴다.
    호스트의 감시 프로세스(watch_and_send.py)가 이 신호를 보고 즉시 발송한다.

    동작:
      1) 03_final_report.md 존재 확인 (없으면 신호 안 남기고 실패)
      2) HTML 변환 미리 생성 (03_final_report.html)
      3) 세션 폴더에 REPORT_DONE.flag 작성 (감시 프로세스의 트리거)
    """
    sess = args.session
    if getattr(args, "latest", False) and not sess:
        sess = latest_session_dir()
    if not sess or not os.path.isdir(sess):
        log.error(f"[report-done] 세션 폴더 없음: {sess}")
        print(f"\nERROR=session_not_found:{sess}")
        return

    report_path = os.path.join(sess, "03_final_report.md")
    if not os.path.exists(report_path):
        log.error(f"[report-done] 리포트 없음: {report_path}")
        print(f"\nREPORT_DONE=failed:no_report")
        return

    # HTML 미리 변환 (발송 때 재사용)
    try:
        render_report_html(sess)
    except Exception as e:
        log.warning(f"[report-done] HTML 변환 실패(무시): {e}")

    update_status(sess, "report_done")
    flag = _write_flag(sess, "report", ok=True)   # REPORT_DONE.flag
    log.info(f"✅ 리포트 완료 신호 작성 → {flag}")
    print(f"\nSESSION_DIR={sess.replace(chr(92), '/')}")
    print(f"REPORT_DONE=success:{flag.replace(chr(92), '/')}")


def cmd_mailinfo(args):
    """
    Composio Gmail 발송에 필요한 값들을 한 번에 출력하는 헬퍼.
    Cowork가 파일을 일일이 파싱하지 않고 정확한 발송 인자를 얻도록 한다.
    출력(파싱하기 쉬운 KEY=VALUE 형식):
      SESSION_DIR=  세션 폴더 절대경로
      MAIL_TO=      수신자 (mail_config.txt 의 to, 콤마 구분 가능)
      MAIL_SENDER=  발신자 (mail_config.txt 의 sender, 참고용)
      MAIL_SUBJECT= 자동 생성 제목 (--subject 로 덮어쓰기 가능)
      REPORT_FILE=  03_final_report.md 절대경로 (없으면 빈 값 + REPORT_MISSING=1)
      REPORT_HTML_FILE= 03_final_report.html 절대경로 (md를 HTML로 변환해 자동 생성)
      ATTACH_1=     1차수집 txt 절대경로 (없으면 생략)
      ATTACH_2=     2차심층수집 txt 절대경로 (없으면 생략)
    """
    sess = args.session
    if getattr(args, "latest", False) and not sess:
        sess = latest_session_dir()
    if not sess or not os.path.isdir(sess):
        log.error(f"[mailinfo] 세션 폴더 없음: {sess}")
        print(f"\nERROR=session_not_found:{sess}")
        return

    cfg = load_mail_config()
    to = (getattr(args, "to", "") or cfg.get("to", "")).strip()
    sender = cfg.get("sender", "").strip()
    date_str = datetime.now().strftime("%Y-%m-%d")
    subject = getattr(args, "subject", "") or \
        f"[데일리 리서치] {date_str} 모멘텀 투자 리포트"

    report_path = os.path.join(sess, "03_final_report.md")
    report_ok = os.path.exists(report_path)
    html_path = render_report_html(sess) if report_ok else ""

    attach1 = attach2 = ""
    for fn in sorted(os.listdir(sess)):
        full = os.path.join(sess, fn)
        if fn.endswith("_1차수집.txt"):
            attach1 = full
        elif fn.endswith("_2차심층수집.txt"):
            attach2 = full

    print(f"\nSESSION_DIR={sess.replace(chr(92), '/')}")
    print(f"MAIL_TO={to}")
    print(f"MAIL_SENDER={sender}")
    print(f"MAIL_SUBJECT={subject}")
    print(f"REPORT_FILE={report_path.replace(chr(92), '/') if report_ok else ''}")
    print(f"REPORT_HTML_FILE={html_path.replace(chr(92), '/') if html_path else ''}")
    if not report_ok:
        print("REPORT_MISSING=1")
    if attach1:
        print(f"ATTACH_1={attach1.replace(chr(92), '/')}")
    if attach2:
        print(f"ATTACH_2={attach2.replace(chr(92), '/')}")


def cmd_mail(args):
    """
    세션 폴더의 최종 리포트(03_final_report.md)를 발송한다.
    1·2차 수집 txt를 자동 첨부. 2중 구조(Gmail API → SMTP 폴백) 사용.
      python research_agent.py mail --session "세션폴더" --method auto
      python research_agent.py mail --latest --method smtp --then-archive  (스케줄러 백업)
    옵션: --method auto|api|smtp|appscript / --subject / --to / --no-attach / --latest / --then-archive
          --force-resend(중복발송 게이트 우회) / --skip-pred-check(predictions 계약검증 우회)
    발송 전 2중 게이트: (1) sent_index 중복확인 (2) predictions.json 계약(timing·conviction·preprice 필수).
    """
    sess = args.session
    if getattr(args, "latest", False) and not sess:
        sess = latest_session_dir()
        if sess:
            log.info(f"[mail] --latest → 최근 세션 선택: {sess}")
    if not sess or not os.path.isdir(sess):
        log.error(f"[mail] 세션 폴더 없음: {sess}")
        print(f"\nMAIL_RESULT=failed:session_not_found:{sess}")
        return

    cfg = load_mail_config()
    if getattr(args, "to", ""):
        cfg["to"] = args.to
    method = (getattr(args, "method", "") or cfg.get("method", "auto")).lower()

    # 기본 설정(발신/수신) 검증 — 세부 인증은 각 발송 함수가 검사
    ok, why = validate_mail_config(cfg, method="api")
    if not ok:
        log.error(f"[mail] 설정 오류: {why}")
        print(f"\nMAIL_RESULT=failed:{why}")
        return

    report_path = os.path.join(sess, "03_final_report.md")
    if not os.path.exists(report_path):
        log.error(f"[mail] 리포트 없음: {report_path}")
        print(f"\nMAIL_RESULT=failed:no_report")
        return

    # ── 게이트 1: 중복 발송 방지(#P3) ──────────────────────────────────
    # 데몬(watch_and_send)과 온디맨드(mail)가 동시에 살아있으면 같은 세션이 두 번 나갈 수 있다.
    # 데몬이 쓰는 sent_index.json(세션 basename 키)을 그대로 재사용해 양쪽이 한 장부를 본다.
    # --force-resend 로만 우회(사용자가 의도적으로 재발송할 때).
    # abspath: '--session .' 이 '.' 이라는 키가 되는 것 방지 / strip: 붙여넣기 시 딸려온 후행 공백 제거
    # (둘 다 데몬 키(os.listdir 산출 basename)와 어긋나 중복 게이트가 뚫리는 실측 경로였다)
    _sess_key = os.path.basename(os.path.normpath(os.path.abspath(str(sess).strip())))
    if not getattr(args, "force_resend", False):
        try:
            import watch_and_send as _wsend      # send_email_appscript 가 이미 쓰는 검증된 import
            if _wsend.already_sent(_sess_key):
                log.warning(f"[mail] 이미 발송된 세션(sent_index): {_sess_key}")
                print(f"\nMAIL_RESULT=skipped:already_sent:{_sess_key}")
                print("  재발송하려면 --force-resend 를 붙여라.")
                return
        except Exception as e:
            log.warning(f"[mail] 중복확인 생략(sent_index 접근 실패): {type(e).__name__}: {e}")

    # ── 게이트 2: predictions.json 계약 검증(#P0-3) ────────────────────
    # timing·conviction·preprice·entry_ref 가 비면 회고 데이터셋이 조용히 오염된다(회고 6회 요청).
    # 지시문에만 있던 '필수' 규약을 발송 직전에 코드로 강제한다. --skip-pred-check 로만 우회.
    if not getattr(args, "skip_pred_check", False):
        _pred_path = os.path.join(sess, "predictions.json")
        if not os.path.exists(_pred_path):
            log.error(f"[mail] predictions.json 없음: {_pred_path}")
            print(f"\nMAIL_RESULT=blocked_schema:no_predictions")
            print("  발송 차단: predictions.json 이 있어야 사후채점·회고가 성립한다.")
            print("  (형식만 확인하고 강행하려면 --skip-pred-check)")
            return
        try:
            with open(_pred_path, encoding="utf-8") as f:
                _pred = json.load(f)
            from common import validate_predictions
            _errs = validate_predictions(_pred)
        except Exception as e:
            _errs = [f"predictions.json 파싱 실패: {type(e).__name__}: {e}"]
        if _errs:
            log.error(f"[mail] predictions 계약 위반 {len(_errs)}건 — 발송 차단")
            print(f"\nMAIL_RESULT=blocked_schema:{len(_errs)}_errors")
            for _e in _errs[:15]:
                print(f"  - {_e}")
            if len(_errs) > 15:
                print(f"  ... 외 {len(_errs) - 15}건")
            # ★ REPORT_DONE.flag 격리: 이걸 남겨두면 데몬(watch_and_send)이 30초 뒤 같은 세션을
            #   그대로 발송해 이 차단이 조용히 뒤집힌다(게이트 무력화). 오류를 적어 보관하고,
            #   predictions 를 고친 뒤 다시 mail 하면 정상 발송된다.
            _rd = os.path.join(sess, "REPORT_DONE.flag")
            if os.path.exists(_rd):
                try:
                    with open(os.path.join(sess, "SCHEMA_BLOCKED.flag"), "w", encoding="utf-8") as _f:
                        _f.write("blocked_at=%s\nerrors=%d\n%s\n"
                                 % (datetime.now().isoformat(timespec="seconds"),
                                    len(_errs), "\n".join(_errs[:30])))
                    os.remove(_rd)
                    print("  REPORT_DONE.flag 격리 -> SCHEMA_BLOCKED.flag (데몬 자동발송 차단)")
                except Exception as _e2:
                    log.warning(f"[mail] flag 격리 실패: {type(_e2).__name__}: {_e2}")
            print("  predictions.json 을 고친 뒤 다시 발송하라(강행: --skip-pred-check).")
            return

    with open(report_path, encoding="utf-8") as f:
        body = f.read()
    # HTML 본문 — 데몬(watch_and_send) 경로와 동일 품질로 완성한다:
    #   render_report_html = 헤더배너 + predictions 대시보드 + 전일등락률표 + 본문 + 세력강도설명 + 카드레이아웃.
    #   (예전엔 md→_markdown_to_html 로 축약본을 보냈다. MCP 온디맨드 발송이 데몬과 같은 메일을 내도록 통일.)
    html_body = ""
    try:
        rp = render_report_html(sess)
        if rp and os.path.exists(rp):
            with open(rp, encoding="utf-8") as f:
                html_body = f.read()
    except Exception as e:
        log.warning(f"[mail] render_report_html 실패 → md 폴백: {e}")
    # 전일 등락률 표를 plain 본문에도 반영(그리고 html 렌더 실패 시 폴백 대비)
    _prev = _prev_day_change_md(sess)
    if _prev:
        body = _prev + "\n" + body
    if not html_body:
        try:
            html_body = _markdown_to_html(body)
        except Exception as e:
            log.warning(f"[mail] HTML 변환 실패 → plain 발송: {e}")

    # 첨부 파일 수집 (1·2차 txt)
    attachments = []
    if not getattr(args, "no_attach", False):
        for fn in os.listdir(sess):
            if fn.endswith("_1차수집.txt") or fn.endswith("_2차심층수집.txt"):
                attachments.append(os.path.join(sess, fn))
        attachments.sort()

    date_str = datetime.now().strftime("%Y-%m-%d")
    subject = getattr(args, "subject", "") or \
        f"[데일리 리서치] {date_str} 모멘텀 투자 리포트"

    allow_browser = not getattr(args, "no_browser", False)
    sent, msg = send_email(subject, body, attachments, cfg, method=method,
                           allow_browser=allow_browser, html_body=html_body)
    print(f"\nSESSION_DIR={sess}")
    if sent:
        # #P3: 데몬과 같은 장부(sent_index.json)에 기록 — flag 제거·아카이브보다 '먼저'.
        # (watch_and_send.scan_once 와 동일한 순서: 기록이 최종 방어선이라 이후 단계가 실패해도 재발송 차단)
        try:
            import watch_and_send as _wsend
            _wsend.mark_sent(_sess_key, {"to": cfg.get("to", ""), "subject": subject})
        except Exception as e:
            log.warning(f"[mail] sent_index 기록 실패(발송은 성공): {type(e).__name__}: {e}")
        update_status(sess, "mailed", {"attachments": len(attachments), "method": method})
        _write_flag(sess, "mail", ok=True)
        # 발송 트리거였던 REPORT_DONE.flag 제거 (감시 프로세스의 중복 발송 방지)
        rd = os.path.join(sess, "REPORT_DONE.flag")
        if os.path.exists(rd):
            try:
                os.remove(rd)
            except Exception:
                pass
        print(f"MAIL_RESULT=success:{msg}")
        print(f"MAIL_ATTACHMENTS={len(attachments)}")
        # 발송 성공 시 자동 보관 (스케줄러 백업 경로용)
        if getattr(args, "then_archive", False):
            dest = archive_session(sess)
            print(f"ARCHIVED_TO={dest}")
    else:
        _write_flag(sess, "mail", ok=False)
        log.error(f"[mail] {msg}")
        print(f"MAIL_RESULT=failed:{msg}")


def _resolve_limit(args):
    """--limit 가 주어지면 그 값, 아니면 설정 변수 ARTICLES_PER_SOURCE."""
    return args.limit if getattr(args, "limit", None) else ARTICLES_PER_SOURCE


def _resolve_pw(args):
    """--no-playwright 우선, 아니면 설정 변수 USE_PLAYWRIGHT."""
    if getattr(args, "no_playwright", False):
        return False
    return bool(USE_PLAYWRIGHT) and PW_AVAILABLE


def _write_flag(session_dir, stage, ok=True):
    """
    완료/실패 신호 파일 작성 — Cowork 폴링용.
      성공: {STAGE}_DONE.flag    (예: COLLECT_DONE.flag, DEEP_DONE.flag)
      실패: {STAGE}_FAILED.flag  (예: DEEP_FAILED.flag)
    Cowork는 이 파일의 존재로 스크립트 완료/실패를 감지합니다.
    """
    suffix = "DONE" if ok else "FAILED"
    fname = f"{stage.upper()}_{suffix}.flag"
    path = os.path.join(session_dir, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"STAGE={stage}\n")
        f.write(f"RESULT={'success' if ok else 'failed'}\n")
        f.write(f"AT={datetime.now().isoformat()}\n")
        f.write(f"SESSION_DIR={session_dir}\n")
    return path


def cmd_collect(args):
    """광역 수집 → 세션 폴더에 01_broad + INSTRUCTIONS 작성. (Cowork 핸드오프 시작점)"""
    sess = None
    try:
        gk = load_gemini_keys()
        cid, csec = load_naver_keys()
        use_pw = _resolve_pw(args)
        limit = _resolve_limit(args)
        load_krx_tickers(force_refresh=True)

        log.info(f"광역 수집 시작 (limit={limit}, playwright={use_pw})")
        broad = run_broad_collection(limit, gk, use_pw=use_pw)
        arts = parse_articles_from_text(broad)
        if len(arts) == 0:
            log.warning("[collect] ⚠️ 수집된 기사가 0개입니다. "
                        "네트워크/소스 토글/차단 여부를 확인하세요. (그래도 세션은 생성)")

        sess = new_session_dir()
        meta = {"수집 기사 수": len(arts), "소스당 기사 수": limit,
                "Gemini 키": len(gk), "Naver": "O" if (cid and csec) else "X",
                "KRX 종목": len(_KRX_MAP_CACHE or {}),
                "Playwright": use_pw}
        broad_path, instr_path = write_broad_and_instructions(sess, broad, meta)
        txt1 = _make_txt_attachment(sess, "01_broad_collection.md", "1차수집")
        update_status(sess, "collected", {"articles": len(arts)})
        flag = _write_flag(sess, "collect", ok=True)      # ← 성공 플래그
        log.info(f"✅ 광역 수집 완료 — 기사 {len(arts)}개")
        log.info(f"   세션 폴더: {sess}")
        print(f"\nSESSION_DIR={sess}")
        print(f"BROAD_FILE={broad_path}")
        print(f"INSTRUCTIONS_FILE={instr_path}")
        print(f"COLLECT_DONE_FLAG={flag}")                # ← Cowork가 감지할 파일
        print(f"ARTICLE_COUNT={len(arts)}")
        if txt1:
            print(f"ATTACH_TXT_1={txt1}")                # ← 메일 첨부용 1차 수집 txt
    except Exception as e:
        log.error(f"[collect] 치명적 오류: {e}")
        if sess and os.path.isdir(sess):
            try:
                update_status(sess, "collect_failed", {"reason": str(e)[:200]})
                _write_flag(sess, "collect", ok=False)
            except Exception:
                pass
        print(f"ERROR=collect_exception:{str(e)[:120]}")
    finally:
        _quit_selenium_driver()   # 셀레늄 드라이버(있으면) 정리 — 좀비 chrome 방지


def cmd_deep(args):
    """
    심층 수집. 두 가지 입력 방식:
      (1) --session DIR  → DIR/commands.txt(Phase1 형식)를 읽어 실행 (Cowork 핸드오프)
      (2) --stocks/--news/--naver  → CLI 직접 지정
    결과를 세션 폴더의 02_deep_collection.md 에 저장.
    어떤 경우에도 마지막에 DEEP_DONE.flag(성공) 또는 DEEP_FAILED.flag(실패)를 남겨
    Cowork가 무한 대기에 빠지지 않게 한다.
    """
    sess = None
    try:
        gk = load_gemini_keys()
        cid, csec = load_naver_keys()
        use_pw = _resolve_pw(args)
        load_krx_tickers(force_refresh=False)

        if args.session:
            sess = args.session
            if not os.path.isdir(sess):
                log.error(f"[deep] 세션 폴더 없음: {sess}")
                print(f"\nERROR=session_not_found:{sess}")
                return
            # 재실행 대비: 이전 deep 플래그 제거 (Cowork가 옛 플래그를 보고 오판하지 않게)
            for fn in ("DEEP_DONE.flag", "DEEP_FAILED.flag"):
                fp = os.path.join(sess, fn)
                if os.path.exists(fp):
                    try:
                        os.remove(fp)
                    except Exception:
                        pass
            cmds = read_commands_file(sess)
            stocks, news, naver = cmds["stock_cmds"], cmds["news_cmds"], cmds["naver_cmds"]
        else:
            sess = new_session_dir()
            stocks = [s.strip() for s in (args.stocks or "").split(",") if s.strip()]
            news = [n.strip() for n in (args.news or "").split(",") if n.strip()]
            naver = [{"keyword": k.strip(), "reason": "수동 지정"}
                     for k in (args.naver or "").split(",") if k.strip()]

        total_cmds = len(stocks) + len(news) + len(naver)
        if total_cmds == 0:
            log.error("[deep] 실행할 명령어가 0개입니다. commands.txt 형식/신선도를 확인하세요.")
            # 빈 결과라도 결과 파일과 실패 플래그를 남김 (Cowork 대기 해제)
            write_deep_result(sess, {"stock_cmds": [], "news_cmds": [], "naver_cmds": []},
                              {"track_a": "(명령어 없음)", "track_b": "(명령어 없음)"})
            update_status(sess, "deep_failed", {"reason": "no_commands"})
            flag = _write_flag(sess, "deep", ok=False)
            print(f"\nSESSION_DIR={sess}")
            print(f"DEEP_FAILED_FLAG={flag}")
            return

        log.info(f"심층 수집: STOCK={len(stocks)} NEWS={len(news)} NAVER={len(naver)}")
        p2 = run_deep_collection(stocks, news, naver, gk, cid, csec, use_pw=use_pw)
        dual = {"stock_cmds": stocks, "news_cmds": news, "naver_cmds": naver}
        path = write_deep_result(sess, dual, p2)
        txt2 = _make_txt_attachment(sess, "02_deep_collection.md", "2차심층수집")
        update_status(sess, "deep_done", {"cmd_count": total_cmds})
        flag = _write_flag(sess, "deep", ok=True)         # ← 성공 플래그
        log.info(f"✅ 심층 수집 완료 → {path}")
        print(f"\nSESSION_DIR={sess}")
        print(f"DEEP_FILE={path}")
        print(f"DEEP_DONE_FLAG={flag}")                   # ← Cowork가 감지할 파일
        if txt2:
            print(f"ATTACH_TXT_2={txt2}")                # ← 메일 첨부용 2차 수집 txt
    except Exception as e:
        log.error(f"[deep] 치명적 오류: {e}")
        if sess and os.path.isdir(sess):
            try:
                update_status(sess, "deep_failed", {"reason": str(e)[:200]})
                flag = _write_flag(sess, "deep", ok=False)
                print(f"\nSESSION_DIR={sess}")
                print(f"DEEP_FAILED_FLAG={flag}")
            except Exception:
                pass
        print(f"ERROR=deep_exception:{str(e)[:120]}")
    finally:
        _quit_selenium_driver()   # 셀레늄 드라이버(있으면) 정리 — 좀비 chrome 방지


def cmd_auto(args):
    """전체 무인 파이프라인 (cron용): 광역 → Gemini Phase1 → Phase2 심층 → 세션 폴더 저장."""
    gk = load_gemini_keys()
    cid, csec = load_naver_keys()
    use_pw = _resolve_pw(args)
    limit = _resolve_limit(args)
    load_krx_tickers(force_refresh=True)

    log.info(f"=== AUTO 파이프라인 시작 (limit={limit}, pw={use_pw}) ===")
    broad = run_broad_collection(limit, gk, use_pw=use_pw)
    arts = parse_articles_from_text(broad)
    log.info(f"광역 수집: {len(arts)}개 기사")

    sess = new_session_dir()
    phase1, dual, p2 = None, None, None
    if not args.no_phase1 and gk and GENAI_AVAILABLE and arts:
        phase1 = batch_analyze(arts, gk,
                               progress=lambda i, t: log.info(f"[Phase1] 배치 {i}/{t}"))
        raw_all = "\n".join(it.get("raw_text", "") for it in phase1 if it.get("raw_text"))
        dual = parse_dual_commands(raw_all)
        log.info(f"명령어: STOCK={len(dual['stock_cmds'])} "
                 f"NEWS={len(dual['news_cmds'])} NAVER={len(dual['naver_cmds'])}")
        p2 = run_deep_collection(dual["stock_cmds"], dual["news_cmds"], dual["naver_cmds"],
                                 gk, cid, csec, use_pw=use_pw)

    meta = {"수집 기사 수": len(arts), "소스당 기사 수": limit, "Gemini 키": len(gk),
            "Naver": "O" if (cid and csec) else "X",
            "KRX 종목": len(_KRX_MAP_CACHE or {}), "Playwright": use_pw}
    broad_path, instr_path = write_broad_and_instructions(sess, broad, meta, phase1)
    if dual and p2:
        deep_path = write_deep_result(sess, dual, p2)
        log.info(f"   심층 결과: {deep_path}")
    log.info(f"=== AUTO 완료 → {sess} ===")
    print(f"\nSESSION_DIR={sess}")
    print(f"BROAD_FILE={broad_path}")
    print(f"INSTRUCTIONS_FILE={instr_path}")


def main():
    ap = argparse.ArgumentParser(description="헤드리스 주식 뉴스 리서치 수집기")
    sub = ap.add_subparsers(dest="cmd")

    def add_common(p):
        # 기본값 None → 설정 변수(ARTICLES_PER_SOURCE) 사용
        p.add_argument("--limit", type=int, default=None,
                       help="소스당 기사 수 (미지정 시 설정변수 ARTICLES_PER_SOURCE)")
        p.add_argument("--no-playwright", action="store_true",
                       help="Step2 Playwright 강제 비활성화")

    p_auto = sub.add_parser("auto", help="전체 파이프라인 (cron용)")
    add_common(p_auto)
    p_auto.add_argument("--no-phase1", action="store_true", help="Gemini Phase1 생략")

    p_col = sub.add_parser("collect", help="광역 수집 + INSTRUCTIONS 작성 (Cowork 핸드오프)")
    add_common(p_col)

    p_deep = sub.add_parser("deep", help="심층 수집 (--session 또는 키워드 직접)")
    add_common(p_deep)
    p_deep.add_argument("--session", default="", help="세션 폴더 경로 (commands.txt 읽기)")
    p_deep.add_argument("--stocks", default="", help="콤마구분 종목")
    p_deep.add_argument("--news", default="", help="콤마구분 뉴스 키워드")
    p_deep.add_argument("--naver", default="", help="콤마구분 네이버 키워드")

    sub.add_parser("test", help="키/네트워크/소스토글/메일 점검")
    sub.add_parser("krx", help="KRX 종목 갱신")

    p_arch = sub.add_parser("archive", help="세션 폴더 보관 이동 (메일 발송 후)")
    p_arch.add_argument("--session", default="", help="보관할 세션 폴더 경로")
    p_arch.add_argument("--latest", action="store_true", help="가장 최근 세션 자동 선택")

    p_mail = sub.add_parser("mail", help="최종 리포트 + 1·2차 첨부 발송 (API→SMTP 폴백)")
    p_mail.add_argument("--session", default="", help="세션 폴더 경로")
    p_mail.add_argument("--latest", action="store_true", help="가장 최근 세션 자동 선택")
    p_mail.add_argument("--method", default="", help="발송 방식: auto|api|smtp (미지정 시 설정파일 method 또는 auto)")
    p_mail.add_argument("--subject", default="", help="메일 제목(미지정 시 자동)")
    p_mail.add_argument("--to", default="", help="수신자(설정 파일 덮어쓰기)")
    p_mail.add_argument("--no-attach", action="store_true", help="첨부 없이 본문만")
    p_mail.add_argument("--no-browser", action="store_true",
                        help="Gmail API 토큰 부재 시 인증창 없이 즉시 실패 (스케줄러/헤드리스용)")
    p_mail.add_argument("--then-archive", action="store_true",
                        help="발송 성공 시 세션 폴더 자동 보관 (스케줄러 백업용)")
    p_mail.add_argument("--force-resend", action="store_true",
                        help="이미 발송된 세션(sent_index)이어도 강제 재발송 (중복 발송 주의)")
    p_mail.add_argument("--skip-pred-check", action="store_true",
                        help="predictions.json 계약 검증(timing/conviction/preprice 필수) 생략하고 강행")

    p_minfo = sub.add_parser("mailinfo",
                             help="Composio 발송용 값(수신자/제목/리포트·첨부 경로) 출력")
    p_minfo.add_argument("--session", default="", help="세션 폴더 경로")
    p_minfo.add_argument("--latest", action="store_true", help="가장 최근 세션 자동 선택")
    p_minfo.add_argument("--subject", default="", help="메일 제목(미지정 시 자동)")
    p_minfo.add_argument("--to", default="", help="수신자(설정 파일 덮어쓰기)")

    p_rdone = sub.add_parser("report-done",
                             help="리포트 완료 신호(REPORT_DONE.flag) 작성 — 감시 발송 트리거")
    p_rdone.add_argument("--session", default="", help="세션 폴더 경로")
    p_rdone.add_argument("--latest", action="store_true", help="가장 최근 세션 자동 선택")

    args = ap.parse_args()
    if args.cmd == "auto":
        cmd_auto(args)
    elif args.cmd == "collect":
        cmd_collect(args)
    elif args.cmd == "deep":
        cmd_deep(args)
    elif args.cmd == "test":
        cmd_test(args)
    elif args.cmd == "krx":
        cmd_krx(args)
    elif args.cmd == "archive":
        cmd_archive(args)
    elif args.cmd == "mail":
        cmd_mail(args)
    elif args.cmd == "mailinfo":
        cmd_mailinfo(args)
    elif args.cmd == "report-done":
        cmd_report_done(args)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()