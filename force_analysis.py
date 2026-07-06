#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
force_analysis.py — 세력 강도 분석 모듈 (독립 스크립트)

[목적]
  공개 데이터로 "세력(외국인·기관)이 매집 중인지 / 개미에게 분산(떠넘기기) 중인지"를
  -100 ~ +100 점수로 추정한다.
    +점수(매집 우위): 세력 유입 가능성 → 동행 검토
    -점수(분산 우위): 물량 떠넘기기 의심 → 경계
    0 근처: 중립/관망

[데이터 소스]
  pykrx (1순위, 종목·지수 OHLCV + 투자자별 매매)
  FinanceDataReader (폴백, OHLCV 만)
  증권사 API는 사용하지 않는다.

[사용법]
  python force_analysis.py --ticker 005930
  python force_analysis.py --ticker 005930,000660,247540   # 콤마로 여러 종목
  python force_analysis.py --market                          # 코스피·코스닥 시장
  python force_analysis.py --ticker 005930 --json            # JSON 출력

[주의]
  이 점수는 공개데이터 기반 '추정치'이며 투자판단의 보조 지표일 뿐이다.
  실시간 분봉이 아니라 일봉(전일까지) 기준이며, 휴장일·데이터 부족 시 해당 축은
  중립 처리된다.

[기존 시스템과의 관계]
  research_agent.py / watch_and_send.py 는 일절 수정하지 않는다.
  이 모듈은 완전 독립 실행되며, 추후 통합은 별도 작업으로 한다.
"""

import os
import sys
import json
import math
import argparse
import logging
from datetime import datetime, timedelta


# ── KRX 계정 로더 (pykrx import 보다 반드시 먼저!) ──────────────────
# pykrx 는 import 시점에 환경변수 KRX_ID / KRX_PW 를 읽어 자동 로그인한다.
# 따라서 krx_account.txt 의 값을 'pykrx import 전에' os.environ 으로 주입해야
# 인증이 걸린다. (naver_api.txt 와 동일한 'key = value' 형식)
#   krx_account.txt 예시:
#     krx_id = your_id
#     krx_pw = your_password
# 파일이 없거나 비어 있으면 조용히 패스 → Naver 수급 폴백으로 동작(무손상).
def _load_krx_account_into_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "krx_account.txt")
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip().lower()
                val = val.strip().strip('"').strip("'")
                if not val:
                    continue
                if key in ("krx_id", "id") and not os.getenv("KRX_ID"):
                    os.environ["KRX_ID"] = val
                elif key in ("krx_pw", "pw", "password") and not os.getenv("KRX_PW"):
                    os.environ["KRX_PW"] = val
    except Exception:
        # 읽기 실패해도 치명적 아님 — Naver 폴백으로 진행
        pass


_load_krx_account_into_env()


# ── 선택적 의존성 (graceful 처리) ──────────────────────────────
# pykrx 는 import 시점에 KRX 로그인을 시도하는데, KRX 가 IP 를 403 으로 차단하면
# 로그인 응답(HTML)을 JSON 으로 파싱하다 JSONDecodeError 로 import 자체가 죽는다.
# → ImportError 만 잡으면 그 크래시가 프로그램 전체를 죽인다. 어떤 예외든(403/네트워크/
#   로그인 실패 포함) 흡수해서 "pykrx 없음"으로 처리하고 FDR·Naver 폴백으로 넘어간다.
try:
    from pykrx import stock as _krx
    PYKRX_AVAILABLE = True
except BaseException as _e:   # ImportError + JSONDecodeError + 기타 전부
    _krx = None
    PYKRX_AVAILABLE = False
    PYKRX_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"
else:
    PYKRX_IMPORT_ERROR = ""

try:
    import FinanceDataReader as fdr
    FDR_AVAILABLE = True
except ImportError:
    FDR_AVAILABLE = False

# Naver Finance 수급 스크래핑 폴백용 (KRX 인증 차단 시 외인/기관 순매매 대체 수집)
try:
    import requests as _requests
    from bs4 import BeautifulSoup as _BeautifulSoup
    NAVER_SCRAPE_AVAILABLE = True
except ImportError:
    NAVER_SCRAPE_AVAILABLE = False

# pykrx 가 KRX_ID/KRX_PW 환경변수로 인증 로그인을 지원하는지(설정 여부) 표시.
# 설정돼 있으면 pykrx 1차 경로가 인증 데이터(투자자별 매매 등)를 받아온다.
KRX_LOGIN_CONFIGURED = bool(os.getenv("KRX_ID") and os.getenv("KRX_PW"))

# Windows 콘솔 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# =====================================================================
# ★ 사용자 튜닝 구역 — 가중치·임계값 한 곳에서 관리 ★
# =====================================================================

# ── 4축 가중치 (합 = 1.0) ────────────────────────────────────────
W_SUPPLY    = 0.40   # 축1. 수급 주체 (외인+기관 vs 개인) — 가장 중요
W_VOLUME    = 0.25   # 축2. 거래량 이상 (조용한 매집 vs 분산 의심)
W_MOMENTUM  = 0.20   # 축3. 가격 모멘텀/과열 (RSI)
W_OBV       = 0.15   # 축4. 매집/분산 누적 (OBV 기울기)

# ── RSI 임계값 ───────────────────────────────────────────────────
RSI_PERIOD              = 14
RSI_OVERBOUGHT_EXTREME  = 75   # 이상 → 과매수 극단(고점 떠넘기기 위험)
RSI_OVERBOUGHT          = 70   # 이상 → 과열
RSI_OVERSOLD            = 30   # 이하 → 과매도(반등 여지)

# ── 거래량 임계값 (최근 거래량 / 20일 평균 거래량 비율) ──────────
VOL_LOOKBACK            = 20
VOL_RATIO_NORMAL_MAX    = 1.5   # 이하: 정상/조용한 매집 가능 → +20
VOL_RATIO_SUSPECT_MAX   = 2.5   # 이하: 중립 0, 초과: 분산 의심 → -40

# ── OBV (On-Balance Volume) ─────────────────────────────────────
OBV_LOOKBACK            = 20

# ── 수급 스케일링 ────────────────────────────────────────────────
# 외인+기관 5일 누적 순매수 금액(원) 기준. SUPPLY_SCALE 원이면 score 100점.
# 100억원 = ±100점이 되도록 1e10 으로 시작. 대형주는 빠르게 만점/만점이 될 수 있음.
SUPPLY_SCALE            = 1e10

# 떠넘기기 의심 페널티: 개인 5일 순매수 > 0 이고 (외인+기관) 5일 < 0 일 때
DUMP_PENALTY            = -30

# ── 분석 기간 ────────────────────────────────────────────────────
HISTORY_DAYS            = 80   # OHLCV 충분히 받아 RSI14·OBV20·VOL20 계산
SUPPLY_5D               = 5    # 단기 수급 누적
SUPPLY_20D              = 20   # 중기 수급 누적

# ── 시장 코드 (pykrx 지수 코드) ──────────────────────────────────
INDEX_CODES = {"KOSPI": "1001", "KOSDAQ": "2001"}

# =====================================================================
# (사용자 튜닝 구역 끝)
# =====================================================================


# ── 로깅 ────────────────────────────────────────────────────────
def _setup_logger() -> logging.Logger:
    lg = logging.getLogger("force_analysis")
    if lg.handlers:
        return lg
    lg.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%H:%M:%S"))
    lg.addHandler(sh)
    return lg

log = _setup_logger()


# =====================================================================
# 1. 유틸
# =====================================================================
def clamp(v, lo, hi):
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return max(lo, min(hi, v))


def _date_range(days_back: int) -> tuple:
    """오늘부터 days_back일 전까지의 (YYYYMMDD, YYYYMMDD)."""
    end = datetime.now()
    start = end - timedelta(days=days_back)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


# =====================================================================
# 2. 데이터 페치 (pykrx → FDR 폴백, graceful)
# =====================================================================
def fetch_ohlcv(ticker: str, days: int = HISTORY_DAYS):
    """
    종목 일봉 OHLCV를 pandas DataFrame 으로 반환. 실패 시 None.
    컬럼은 한글(시가/고가/저가/종가/거래량) 또는 영문 둘 다 가능 — 호출자가 추상화.
    """
    start, end = _date_range(days * 2)  # 영업일 보정용 여유
    if PYKRX_AVAILABLE:
        try:
            df = _krx.get_market_ohlcv(start, end, ticker)
            if df is not None and not df.empty:
                # pykrx 컬럼: 시가/고가/저가/종가/거래량/등락률
                return df
        except Exception as e:
            log.warning(f"[데이터] pykrx OHLCV 실패 ({ticker}): {type(e).__name__}: {e}")
    if FDR_AVAILABLE:
        try:
            d0 = datetime.strptime(start, "%Y%m%d").strftime("%Y-%m-%d")
            df = fdr.DataReader(ticker, d0)
            if df is not None and not df.empty:
                # FDR 컬럼: Open/High/Low/Close/Volume/Change
                return df
        except Exception as e:
            log.warning(f"[데이터] FDR OHLCV 실패 ({ticker}): {type(e).__name__}: {e}")
    log.error(f"[데이터] 🔴 {ticker} OHLCV 수집 실패 — 모든 소스 실패")
    return None


def _ohlcv_cols(df) -> dict:
    """DataFrame이 한글 컬럼이면 {'close':'종가',...} 영문이면 {'close':'Close',...}"""
    cols = list(df.columns)
    if "종가" in cols:
        return {"open": "시가", "high": "고가", "low": "저가",
                "close": "종가", "volume": "거래량"}
    if "Close" in cols:
        return {"open": "Open", "high": "High", "low": "Low",
                "close": "Close", "volume": "Volume"}
    return {}


_NAVER_FRGN_URL = "https://finance.naver.com/item/frgn.naver"
_NAVER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Referer": "https://finance.naver.com",
}


def _parse_korean_int(s: str):
    """'+5,314,304' / '-1,061,741' / '317,000' → int. 실패 시 None."""
    if not s:
        return None
    t = s.replace(",", "").replace("+", "").strip()
    if not t or t in ("-", "N/A"):
        return None
    try:
        return int(t)
    except ValueError:
        try:
            return int(float(t))
        except ValueError:
            return None


def fetch_investor_net_value_naver(ticker: str, days: int = 30):
    """
    [폴백] Naver Finance frgn 페이지에서 일별 외국인·기관 '순매매 수량'을 긁어
    종가를 곱해 '순매수 금액(원)' DataFrame 으로 환산해 반환. 실패 시 None.

    Naver frgn 테이블 컬럼(0-base):
      0 날짜 / 1 종가 / 2 전일비 / 3 등락률 / 4 거래량
      5 기관 순매매량 / 6 외국인 순매매량 / 7 보유주수 / 8 보유율
    → pykrx 와 동일 단위(원)로 맞추기 위해 (순매매 수량 × 종가) 로 금액 환산.
      개인(개인) 칼럼은 이 페이지에 없어 결측(None) — score_supply 는 외인+기관만으로
      정상 계산되며 개인은 떠넘기기 페널티에만 쓰여 영향 적음.

    반환 DataFrame 컬럼: '외국인', '기관' (원 단위, 날짜 오름차순 인덱스).
    df.attrs['source']='naver' 로 출처를 태깅(리포트 신뢰도 표기용).
    """
    if not NAVER_SCRAPE_AVAILABLE:
        return None
    # 필요한 페이지 수(페이지당 약 20영업일). days 만큼 여유 있게.
    pages = max(1, min(4, (days // 18) + 1))
    rows = []  # (date_str, close, inst_qty, foreign_qty)
    try:
        for pg in range(1, pages + 1):
            resp = _requests.get(f"{_NAVER_FRGN_URL}?code={ticker}&page={pg}",
                                 headers=_NAVER_HEADERS, timeout=10)
            resp.encoding = "euc-kr"
            soup = _BeautifulSoup(resp.text, "html.parser")
            table = None
            for t in soup.find_all("table"):
                head = " ".join(th.get_text(strip=True) for th in t.find_all("th"))
                if "외국인" in head and "기관" in head:
                    table = t
                    break
            if table is None:
                continue
            for tr in table.find_all("tr"):
                tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(tds) < 7 or not tds[0] or "." not in tds[0]:
                    continue
                close = _parse_korean_int(tds[1])
                inst_qty = _parse_korean_int(tds[5])
                foreign_qty = _parse_korean_int(tds[6])
                if close is None or (inst_qty is None and foreign_qty is None):
                    continue
                rows.append((tds[0], close, inst_qty, foreign_qty))
    except Exception as e:
        log.warning(f"[데이터] 🟡 {ticker} Naver 수급 스크래핑 실패: "
                    f"{type(e).__name__}: {e}")
        return None

    if not rows:
        return None

    # pandas DataFrame 구성 (금액 = 수량 × 종가). 날짜 오름차순.
    try:
        import pandas as _pd
    except Exception:
        return None
    rows.sort(key=lambda x: x[0])   # 날짜 오름차순
    idx, foreign_won, inst_won = [], [], []
    for date_str, close, inst_qty, foreign_qty in rows:
        idx.append(date_str)
        foreign_won.append(foreign_qty * close if foreign_qty is not None else None)
        inst_won.append(inst_qty * close if inst_qty is not None else None)
    df = _pd.DataFrame({"외국인": foreign_won, "기관": inst_won}, index=idx)
    try:
        df.attrs["source"] = "naver"
    except Exception:
        pass
    log.info(f"[데이터] 🟢 {ticker} 수급 Naver 폴백 성공 ({len(df)}일, 수량×종가 환산)")
    return df


def fetch_investor_net_value(ticker: str, days: int = 30):
    """
    종목별 투자자별 일자별 순매수 금액 DataFrame.
    1순위 pykrx(KRX_ID/PW 환경변수 있으면 인증 데이터), 빈 응답·실패 시
    Naver Finance 스크래핑 폴백(수량×종가 환산). 둘 다 실패 시 None('결측').
    pykrx 컬럼은 보통: 기관합계 / 기타법인 / 개인 / 외국인합계 / 전체
    ※ KRX 가 인증을 요구하면 pykrx 가 예외 없이 '빈 DataFrame' 을 돌려주므로
      (응답 본문이 LOGOUT), 빈 응답이어도 Naver 폴백으로 넘어간다.
    """
    start, end = _date_range(days * 2)
    # 1순위: pykrx (KRX 로그인 설정 시 인증 데이터)
    if PYKRX_AVAILABLE:
        try:
            df = _krx.get_market_trading_value_by_date(start, end, ticker)
            if df is not None and not df.empty:
                try:
                    df.attrs["source"] = "krx"
                except Exception:
                    pass
                return df
            log.warning(f"[데이터] 🟡 {ticker} pykrx 수급 빈 응답"
                        f"(KRX 인증 차단 의심) → Naver 폴백")
        except Exception as e:
            log.warning(f"[데이터] 🟡 {ticker} pykrx 수급 실패: "
                        f"{type(e).__name__}: {e} → Naver 폴백")
    # 2순위: Naver Finance 스크래핑
    df = fetch_investor_net_value_naver(ticker, days)
    if df is not None and not df.empty:
        return df
    log.warning(f"[데이터] 🟡 {ticker} 수급 결측: pykrx·Naver 모두 실패 → 수급 축 제외")
    return None


# 지수코드(KRX) → FinanceDataReader 심볼 (지수 OHLCV FDR 폴백용)
_FDR_INDEX_SYMBOL = {"1001": "KS11", "2001": "KQ11"}


def fetch_index_ohlcv(index_code: str, days: int = HISTORY_DAYS):
    """
    지수(코스피 1001 / 코스닥 2001) OHLCV.
    1순위 pykrx, 빈 응답·실패 시 FinanceDataReader(KS11/KQ11) 폴백. 둘 다 실패 시 None.
    ※ KRX 가 인증을 요구하면 pykrx 가 예외 없이 빈 DataFrame 을 돌려주므로,
      빈 응답일 때도 FDR 폴백으로 넘어간다.
    """
    if not PYKRX_AVAILABLE and not FDR_AVAILABLE:
        return None
    start, end = _date_range(days * 2)
    # 1순위: pykrx
    if PYKRX_AVAILABLE:
        try:
            df = _krx.get_index_ohlcv_by_date(start, end, index_code)
            if df is not None and not df.empty:
                return df
            log.warning(f"[데이터] 🟡 지수 {index_code} pykrx OHLCV 빈 응답"
                        f"(인증 차단 의심) → FDR 폴백")
        except Exception as e:
            log.warning(f"[데이터] 🟡 지수 {index_code} pykrx OHLCV 실패: "
                        f"{type(e).__name__}: {e} → FDR 폴백")
    # 2순위: FinanceDataReader (영문 컬럼 — _ohlcv_cols 가 자동 인식)
    if FDR_AVAILABLE:
        sym = _FDR_INDEX_SYMBOL.get(str(index_code))
        if not sym:
            log.warning(f"[데이터] 🟡 지수 {index_code} FDR 심볼 매핑 없음")
            return None
        try:
            d0 = datetime.strptime(start, "%Y%m%d").strftime("%Y-%m-%d")
            df = fdr.DataReader(sym, d0)
            if df is not None and not df.empty:
                return df
            log.warning(f"[데이터] 🟡 지수 {index_code}({sym}) FDR OHLCV 빈 응답")
        except Exception as e:
            log.warning(f"[데이터] 🟡 지수 {index_code}({sym}) FDR OHLCV 실패: "
                        f"{type(e).__name__}: {e}")
    return None


def fetch_index_investor_net_value(index_code: str, days: int = 30):
    """지수 단위 투자자별 일자별 순매수 금액. 실패·빈 응답 시 None('결측')."""
    if not PYKRX_AVAILABLE:
        return None
    start, end = _date_range(days * 2)
    try:
        df = _krx.get_market_trading_value_by_date(start, end, index_code)
        if df is not None and not df.empty:
            return df
        log.warning(f"[데이터] 🟡 지수 {index_code} 수급 결측: KRX 빈 응답"
                    f"(인증 차단 의심) → 시장 수급 축 제외")
    except Exception as e:
        log.warning(f"[데이터] 🟡 지수 {index_code} 수급 결측: "
                    f"{type(e).__name__}: {e} → 시장 수급 축 제외")
    return None


def get_ticker_name(ticker: str) -> str:
    """티커 → 종목명. 실패 시 티커 그대로."""
    if PYKRX_AVAILABLE:
        try:
            n = _krx.get_market_ticker_name(ticker)
            if n:
                return n
        except Exception:
            pass
    return ticker


# =====================================================================
# 3. 지표 계산 (OHLCV → RSI, OBV, 거래량비)
# =====================================================================
def compute_rsi(closes, period: int = RSI_PERIOD):
    """
    표준 RSI. 가격 데이터 부족 시 None.
    Wilder 공식(EMA 변형) 대신 단순 평균(Cutler RSI)으로 안정성 확보.
    """
    if closes is None or len(closes) < period + 1:
        return None
    closes = list(closes)
    gains = []
    losses = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    # 최근 period 구간 평균
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi, 2)


def compute_obv(closes, volumes):
    """
    OBV 누적 시계열 반환(list). 길이는 closes 와 동일.
    종가 전일대비 상승 → +거래량 누적, 하락 → -거래량 누적, 보합 → 누적값 유지.
    """
    if closes is None or volumes is None or len(closes) != len(volumes) or len(closes) < 2:
        return None
    obv = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])
    return obv


def obv_slope_score(obv_series, lookback: int = OBV_LOOKBACK):
    """
    최근 lookback일 OBV의 변화량을 -100~+100 로 정규화.
    첫값 대비 마지막값 변화율. 첫값이 0 근처면 lookback 구간 최대 절대값으로 정규화.
    데이터 부족 시 None.
    """
    if obv_series is None or len(obv_series) < lookback:
        return None
    recent = obv_series[-lookback:]
    first, last = recent[0], recent[-1]
    if abs(first) < 1:
        scale = max((abs(v) for v in recent), default=0) or 1
        change = (last - first) / scale * 100
    else:
        change = (last - first) / abs(first) * 100
    return clamp(change, -100, 100)


def compute_vol_ratio(volumes, lookback: int = VOL_LOOKBACK):
    """최근 거래량 / 20일 평균. 데이터 부족 시 None."""
    if volumes is None or len(volumes) < lookback + 1:
        return None
    avg = sum(volumes[-lookback - 1:-1]) / lookback   # 직전 20일 평균
    if avg <= 0:
        return None
    latest = volumes[-1]
    return round(latest / avg, 3)


def compute_price_change_pct(closes, lookback: int = 1):
    """직전 lookback일 대비 종가 변화율(%). 데이터 부족 시 None."""
    if closes is None or len(closes) < lookback + 1:
        return None
    prev = closes[-lookback - 1]
    curr = closes[-1]
    if prev <= 0:
        return None
    return round((curr / prev - 1) * 100, 2)


def compute_ma_disparity(closes, lookback: int = 20):
    """이격도 = 현재가 / 20일 이동평균 × 100. 데이터 부족 시 None. (보조 지표)"""
    if closes is None or len(closes) < lookback:
        return None
    ma = sum(closes[-lookback:]) / lookback
    if ma <= 0:
        return None
    return round(closes[-1] / ma * 100, 2)


# =====================================================================
# 4. 축별 점수 (각 -100 ~ +100)
# =====================================================================
def score_supply(foreign_5d, inst_5d, indiv_5d):
    """
    축1. 수급 주체 — 외국인+기관 vs 개인.
    foreign_5d, inst_5d, indiv_5d: 5일 누적 순매수 금액(원). None 가능.
    None 인 경우(데이터 실패) 0(중립) 반환.
    """
    if foreign_5d is None and inst_5d is None:
        return 0.0
    fi = (foreign_5d or 0) + (inst_5d or 0)
    score = clamp(fi / SUPPLY_SCALE * 100, -100, 100)
    # 떠넘기기: 개인이 받아주고 (외인+기관) 이 파는 전형적 패턴
    if (indiv_5d or 0) > 0 and fi < 0:
        score = clamp(score + DUMP_PENALTY, -100, 100)
    return round(score, 1)


def score_volume(vol_ratio, price_change_pct=None):
    """
    축2. 거래량 이상.
      vol_ratio <= 1.5  → +20  (정상/조용한 매집 가능)
      1.5 < ratio <= 2.5 → 0    (중립)
      ratio > 2.5       → -40  (분산 의심: 거래량 폭증)
        + price_change_pct <= 0 (가격이 안 오르는데 거래량만 폭증) → 추가 -20
    """
    if vol_ratio is None:
        return 0.0
    if vol_ratio <= VOL_RATIO_NORMAL_MAX:
        s = 20.0
    elif vol_ratio <= VOL_RATIO_SUSPECT_MAX:
        s = 0.0
    else:
        s = -40.0
        if price_change_pct is not None and price_change_pct <= 0:
            s -= 20.0   # 가격 정체/하락 + 거래량 폭증 → 분산 강한 의심
    return round(clamp(s, -100, 100), 1)


def score_momentum(rsi):
    """
    축3. RSI 모멘텀/과열.
      RSI >= 75 → -60 (과매수 극단)
      70 <= RSI < 75 → -30
      RSI <= 30 → +30 (과매도, 반등 여지)
      그 외 → +20 (건강 구간)
    """
    if rsi is None:
        return 0.0
    if rsi >= RSI_OVERBOUGHT_EXTREME:
        return -60.0
    if rsi >= RSI_OVERBOUGHT:
        return -30.0
    if rsi <= RSI_OVERSOLD:
        return 30.0
    return 20.0


def score_obv(obv_slope_normalized):
    """축4. OBV 기울기 — 이미 정규화된 -100~+100 값 그대로(클램프만)."""
    if obv_slope_normalized is None:
        return 0.0
    return round(clamp(obv_slope_normalized, -100, 100), 1)


def combine_force_score(s_supply, s_volume, s_momentum, s_obv):
    """4축 가중합 → 최종 -100~+100 (반올림 소수 1자리)."""
    total = (s_supply * W_SUPPLY + s_volume * W_VOLUME
             + s_momentum * W_MOMENTUM + s_obv * W_OBV)
    return round(clamp(total, -100, 100), 1)


def force_label(score: float) -> str:
    if score is None:
        return "데이터 부족"
    if score >= 40:
        return "강한 매집 — 세력 유입"
    if score >= 15:
        return "매집 우위"
    if score >= -15:
        return "중립/관망"
    if score >= -40:
        return "분산 우위 — 경계"
    return "강한 분산 — 떠넘기기 의심"


# =====================================================================
# 5. 투자자 컬럼 추출 헬퍼 (pykrx 컬럼명 버전차 대응)
# =====================================================================
_INVESTOR_KEYS = {
    "foreign":  ["외국인합계", "외국인", "외인합계", "외인"],
    "inst":     ["기관합계", "기관", "기관계"],
    "indiv":    ["개인"],
}

def _extract_investor_series(df):
    """투자자별 매매 DataFrame에서 (외국인, 기관, 개인) 일자별 시리즈 추출."""
    if df is None or df.empty:
        return None, None, None
    cols = list(df.columns)
    def pick(keys):
        for k in keys:
            if k in cols:
                return df[k]
        return None
    return (pick(_INVESTOR_KEYS["foreign"]),
            pick(_INVESTOR_KEYS["inst"]),
            pick(_INVESTOR_KEYS["indiv"]))


def _sum_last_n(series, n: int):
    """시리즈의 마지막 n개 합. None/빈 시 None."""
    if series is None:
        return None
    try:
        vals = list(series.dropna().values)
    except Exception:
        try:
            vals = list(series)
        except Exception:
            return None
    if not vals:
        return None
    n = min(n, len(vals))
    return float(sum(vals[-n:]))


# =====================================================================
# 6. 종목 분석 (메인 API)
# =====================================================================
def analyze_ticker(ticker: str) -> dict:
    """
    한 종목에 대한 세력 강도 분석.
    Returns: {ticker, name, force_score, label, detail{...}, error?}
    실패해도 dict 를 반환(다른 종목 분석에 영향 없음).
    """
    ticker = (ticker or "").strip()
    if not ticker:
        return {"ticker": "", "error": "empty_ticker"}
    name = get_ticker_name(ticker)

    df = fetch_ohlcv(ticker)
    if df is None or df.empty:
        return {"ticker": ticker, "name": name,
                "force_score": None, "label": "데이터 부족",
                "error": "ohlcv_fetch_failed"}

    cmap = _ohlcv_cols(df)
    if not cmap:
        return {"ticker": ticker, "name": name,
                "force_score": None, "label": "데이터 부족",
                "error": f"unknown_columns:{list(df.columns)}"}
    closes = list(df[cmap["close"]].values)
    volumes = list(df[cmap["volume"]].values)

    # 지표
    rsi = compute_rsi(closes, RSI_PERIOD)
    obv = compute_obv(closes, volumes)
    obv_score_val = obv_slope_score(obv, OBV_LOOKBACK)
    vol_ratio = compute_vol_ratio(volumes, VOL_LOOKBACK)
    price_change = compute_price_change_pct(closes, 1)
    ma_disparity = compute_ma_disparity(closes, 20)

    # 수급
    inv_df = fetch_investor_net_value(ticker, SUPPLY_20D + 10)
    supply_source = None
    if inv_df is not None and not inv_df.empty:
        try:
            supply_source = inv_df.attrs.get("source")
        except Exception:
            supply_source = None
    f_ser, i_ser, p_ser = _extract_investor_series(inv_df)
    foreign_5d = _sum_last_n(f_ser, SUPPLY_5D)
    inst_5d    = _sum_last_n(i_ser, SUPPLY_5D)
    indiv_5d   = _sum_last_n(p_ser, SUPPLY_5D)
    foreign_20d = _sum_last_n(f_ser, SUPPLY_20D)
    inst_20d    = _sum_last_n(i_ser, SUPPLY_20D)
    indiv_20d   = _sum_last_n(p_ser, SUPPLY_20D)

    # 축 점수
    s_supply   = score_supply(foreign_5d, inst_5d, indiv_5d)
    s_volume   = score_volume(vol_ratio, price_change)
    s_momentum = score_momentum(rsi)
    s_obv      = score_obv(obv_score_val)

    force = combine_force_score(s_supply, s_volume, s_momentum, s_obv)
    lbl = force_label(force)

    return {
        "ticker": ticker, "name": name,
        "force_score": force, "label": lbl,
        "detail": {
            "supply":   s_supply,
            "volume":   s_volume,
            "momentum": s_momentum,
            "obv":      s_obv,
            "rsi":      rsi,
            "vol_ratio":     vol_ratio,
            "price_change_pct": price_change,
            "ma_disparity_20": ma_disparity,
            "obv_slope_score": obv_score_val,
            "foreign_5d":  foreign_5d,
            "inst_5d":     inst_5d,
            "indiv_5d":    indiv_5d,
            "foreign_20d": foreign_20d,
            "inst_20d":    inst_20d,
            "indiv_20d":   indiv_20d,
            "data_points": len(closes),
            "supply_data_available": inv_df is not None and not inv_df.empty,
            "supply_source": supply_source,
        },
    }


# =====================================================================
# 7. 시장 분석 (--market 모드)
# =====================================================================
def analyze_market(market_name: str = "KOSPI") -> dict:
    """
    시장 레벨 세력 우호도 점수.
    수급(외인+기관 5일) + 모멘텀(RSI) 축 만으로 가중 산출.
    OBV/거래량 비는 시장 단위에서 의미가 약해 제외.
    """
    code = INDEX_CODES.get(market_name.upper())
    if not code:
        return {"market": market_name, "error": f"unknown_market:{market_name}"}

    df = fetch_index_ohlcv(code)
    closes = None
    rsi = None
    if df is not None and not df.empty:
        cmap = _ohlcv_cols(df)
        if cmap:
            closes = list(df[cmap["close"]].values)
            rsi = compute_rsi(closes, RSI_PERIOD)

    inv_df = fetch_index_investor_net_value(code, SUPPLY_20D + 10)
    f_ser, i_ser, p_ser = _extract_investor_series(inv_df)
    foreign_5d = _sum_last_n(f_ser, SUPPLY_5D)
    inst_5d    = _sum_last_n(i_ser, SUPPLY_5D)
    indiv_5d   = _sum_last_n(p_ser, SUPPLY_5D)

    # 시장 모드 가중치 재배분 (수급 + 모멘텀 = 1.0)
    w_supply_m = 0.60
    w_momentum_m = 0.40
    s_supply   = score_supply(foreign_5d, inst_5d, indiv_5d)
    s_momentum = score_momentum(rsi)
    total = s_supply * w_supply_m + s_momentum * w_momentum_m
    score = round(clamp(total, -100, 100), 1)
    lbl = force_label(score)

    return {
        "market": market_name.upper(), "index_code": code,
        "force_score": score, "label": lbl,
        "detail": {
            "supply":   s_supply,
            "momentum": s_momentum,
            "rsi":      rsi,
            "foreign_5d": foreign_5d,
            "inst_5d":    inst_5d,
            "indiv_5d":   indiv_5d,
            "ohlcv_available":  closes is not None,
            "supply_data_available": inv_df is not None and not inv_df.empty,
            "weights": {"supply": w_supply_m, "momentum": w_momentum_m},
        },
    }


# =====================================================================
# 8. 출력 포매팅
# =====================================================================
def _fmt_money(v):
    """원 단위 금액 → 사람 친화적 표기."""
    if v is None:
        return "(데이터 없음)"
    sign = "+" if v > 0 else ("-" if v < 0 else " ")
    a = abs(v)
    if a >= 1e12:
        return f"{sign}{a/1e12:.2f}조원"
    if a >= 1e8:
        return f"{sign}{a/1e8:.1f}억원"
    if a >= 1e4:
        return f"{sign}{a/1e4:.1f}만원"
    return f"{sign}{a:,.0f}원"


def _fmt_num(v, suffix=""):
    if v is None:
        return "(N/A)"
    if isinstance(v, float):
        return f"{v:.2f}{suffix}"
    return f"{v}{suffix}"


def render_ticker_human(r: dict) -> str:
    """사람용 출력 (KEY=VALUE + 표 형식 혼합)."""
    if r.get("error") and r.get("force_score") is None:
        return (f"\n[{r.get('ticker','?')}] {r.get('name','?')}\n"
                f"  ❌ 분석 실패: {r.get('error')}\n")
    d = r.get("detail", {})
    score = r.get("force_score")
    lbl = r.get("label", "")
    bar = _ascii_bar(score)
    lines = []
    lines.append(f"\n┌─ [{r['ticker']}] {r['name']} ───────────────────────────")
    lines.append(f"│ 세력 강도 점수: {score:>+6.1f}  {bar}  ({lbl})")
    lines.append(f"├─ 4축 세부 점수 (가중치: 수급 {W_SUPPLY:.0%} | 거래량 {W_VOLUME:.0%} | "
                 f"모멘텀 {W_MOMENTUM:.0%} | OBV {W_OBV:.0%})")
    lines.append(f"│   ① 수급   {d.get('supply'):>+6.1f}  (외인+기관 매집 vs 개인 떠넘기기)")
    lines.append(f"│   ② 거래량 {d.get('volume'):>+6.1f}  (vol_ratio={_fmt_num(d.get('vol_ratio'),'x')})")
    lines.append(f"│   ③ 모멘텀 {d.get('momentum'):>+6.1f}  (RSI{RSI_PERIOD}={_fmt_num(d.get('rsi'))})")
    lines.append(f"│   ④ OBV    {d.get('obv'):>+6.1f}  (OBV{OBV_LOOKBACK}일 기울기)")
    lines.append(f"├─ 수급 (5일 누적 / 20일 누적)")
    lines.append(f"│   외국인: {_fmt_money(d.get('foreign_5d'))} / {_fmt_money(d.get('foreign_20d'))}")
    lines.append(f"│   기관  : {_fmt_money(d.get('inst_5d'))} / {_fmt_money(d.get('inst_20d'))}")
    lines.append(f"│   개인  : {_fmt_money(d.get('indiv_5d'))} / {_fmt_money(d.get('indiv_20d'))}")
    _src = d.get("supply_source")
    if not d.get("supply_data_available"):
        lines.append(f"│   ⚠️ 투자자별 매매 데이터 미수신 → 수급 축 중립 처리")
    elif _src == "naver":
        lines.append(f"│   ※ 수급 출처: Naver(수량×종가 환산, 개인 결측) — KRX 인증 시 정밀")
    elif _src == "krx":
        lines.append(f"│   ※ 수급 출처: KRX 인증 데이터")
    lines.append(f"├─ 보조 지표")
    lines.append(f"│   당일 등락률: {_fmt_num(d.get('price_change_pct'),'%')}  "
                 f"| 20일 이격도: {_fmt_num(d.get('ma_disparity_20'))}")
    lines.append(f"│   데이터 포인트: {d.get('data_points')}일")
    lines.append(f"└─ ※ 공개데이터 기반 추정치이며 투자판단의 보조 지표입니다")
    return "\n".join(lines)


def render_market_human(r: dict) -> str:
    if r.get("error"):
        return f"\n[시장 {r.get('market','?')}] ❌ {r.get('error')}\n"
    d = r.get("detail", {})
    score = r.get("force_score")
    bar = _ascii_bar(score)
    lines = []
    lines.append(f"\n┌─ 시장: {r['market']} (지수코드 {r['index_code']}) ─────────")
    lines.append(f"│ 시장 세력 우호도: {score:>+6.1f}  {bar}  ({r['label']})")
    lines.append(f"├─ 2축 세부 (수급 60% | 모멘텀 40%)")
    lines.append(f"│   ① 수급   {d.get('supply'):>+6.1f}  | RSI{RSI_PERIOD} = {_fmt_num(d.get('rsi'))}")
    lines.append(f"│   ② 모멘텀 {d.get('momentum'):>+6.1f}")
    lines.append(f"├─ 5일 누적 순매수")
    lines.append(f"│   외국인: {_fmt_money(d.get('foreign_5d'))}")
    lines.append(f"│   기관  : {_fmt_money(d.get('inst_5d'))}")
    lines.append(f"│   개인  : {_fmt_money(d.get('indiv_5d'))}")
    if not d.get("ohlcv_available"):
        lines.append(f"│   ⚠️ 지수 OHLCV 미수신 → 모멘텀 축 중립")
    if not d.get("supply_data_available"):
        lines.append(f"│   ⚠️ 지수 투자자별 매매 미수신 → 수급 축 중립")
    lines.append(f"└─ ※ 공개데이터 기반 추정치 (보조 지표)")
    return "\n".join(lines)


def _ascii_bar(score, width: int = 21) -> str:
    """-100~+100 점수를 21칸 막대로 시각화. 중앙 0."""
    if score is None:
        return "[" + " " * width + "]"
    half = width // 2
    # -100~+100 → -half ~ +half
    pos = int(round(score / 100 * half))
    pos = max(-half, min(half, pos))
    cells = [" "] * width
    cells[half] = "|"
    if pos == 0:
        pass
    elif pos > 0:
        for i in range(half + 1, half + 1 + pos):
            cells[i] = "▶"
    else:
        for i in range(half + pos, half):
            cells[i] = "◀"
    return "[" + "".join(cells) + "]"


# =====================================================================
# 9. CLI
# =====================================================================
def main():
    ap = argparse.ArgumentParser(
        description="세력 강도 분석 (공개데이터 기반 추정치 — 보조 지표).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "예시:\n"
            "  python force_analysis.py --ticker 005930\n"
            "  python force_analysis.py --ticker 005930,000660,247540\n"
            "  python force_analysis.py --market\n"
            "  python force_analysis.py --ticker 005930 --json\n"
        ),
    )
    ap.add_argument("--ticker", default="", help="종목 코드(콤마구분, 예: 005930,000660)")
    ap.add_argument("--market", action="store_true",
                    help="코스피·코스닥 시장 레벨 세력 우호도")
    ap.add_argument("--json", action="store_true",
                    help="JSON 출력 (Cowork/다른 코드 파싱용)")
    args = ap.parse_args()

    if not args.ticker and not args.market:
        ap.print_help()
        return

    # 환경 안내
    if not PYKRX_AVAILABLE:
        if PYKRX_IMPORT_ERROR:
            # 설치는 됐는데 import 시 크래시(대부분 KRX 403 차단으로 로그인 실패)
            log.warning(f"[환경] 🟡 pykrx 사용 불가 — KRX 접속 차단/로그인 실패 추정 "
                        f"({PYKRX_IMPORT_ERROR}). FDR·Naver 폴백으로 진행하며 "
                        f"투자자별 정밀 수급은 결측 처리됩니다.")
        else:
            log.error("[환경] 🔴 pykrx 미설치 — `pip install pykrx` 필요. "
                      "투자자별 수급 데이터를 얻을 수 없어 수급/시장 분석이 중립 처리됩니다.")
    if not FDR_AVAILABLE and not PYKRX_AVAILABLE:
        log.error("[환경] 🔴 pykrx + FinanceDataReader 둘 다 사용 불가 — 분석 불가.")
        return

    results = {"tickers": [], "markets": []}

    # --ticker
    if args.ticker:
        ticker_list = [t.strip() for t in args.ticker.split(",") if t.strip()]
        for tk in ticker_list:
            try:
                r = analyze_ticker(tk)
            except Exception as e:
                # 한 종목 실패가 다른 종목에 영향 없게
                log.error(f"[분석] {tk} 예외: {type(e).__name__}: {e}")
                r = {"ticker": tk, "name": tk, "force_score": None,
                     "label": "분석 실패", "error": f"{type(e).__name__}: {e}"}
            results["tickers"].append(r)

    # --market
    if args.market:
        for m in ("KOSPI", "KOSDAQ"):
            try:
                r = analyze_market(m)
            except Exception as e:
                log.error(f"[시장] {m} 예외: {type(e).__name__}: {e}")
                r = {"market": m, "force_score": None, "label": "분석 실패",
                     "error": f"{type(e).__name__}: {e}"}
            results["markets"].append(r)

    # 출력
    if args.json:
        out = {"generated_at": datetime.now().isoformat(),
               "tickers": results["tickers"], "markets": results["markets"]}
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    else:
        for r in results["tickers"]:
            print(render_ticker_human(r))
        for r in results["markets"]:
            print(render_market_human(r))


if __name__ == "__main__":
    main()
