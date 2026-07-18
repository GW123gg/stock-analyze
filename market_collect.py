#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
market_collect.py — 장전 시장 컨텍스트 수집기 (월가식 top-down 분석용)

[목적]
  장전(06:30) supervisor 파이프라인의 collect 직후 실행돼, 시장 방향성 판단에
  필요한 "시장 컨텍스트"를 수집해 오늘자 세션 폴더에 market_context.json 으로
  저장한다. 분석(Cowork)이 이 파일을 읽어 인터마켓·섹터 로테이션·시장 국면을
  종합 판단한다.

[수집 항목]
  1) intermarket  : 미국 지수/금리/원자재/환율 (yfinance) — 종가 + 전일대비 등락률
  2) kr_sectors   : 한국 주요 업종지수 등락률 + 20일 모멘텀 → RS 랭킹 (FDR/pykrx)
  3) breadth      : 직전 거래일 코스피+코스닥 등락 종목수 / 신고가·신저가 (pykrx)
  4) flows        : 외국인·기관 시장 전체 순매수 5일/20일 누적 (pykrx 투자자별)
  5) regime       : 추세·breadth·수급·VIX·환율 종합 risk-on 점수(0~100) + 라벨

[설계 원칙]
  - 각 항목은 개별 try/except — 일부 실패해도 나머지 진행. 실패는 notes[] 에 기록.
  - 값을 못 구하면 null, 사유는 notes 에. 부분 실패해도 exit 0 (파이프라인 무중단).
  - 콘솔 print 는 ASCII 태그([market])만 — 윈도우 cp949 콘솔 크래시 방지(이모지 금지).
  - 파일 IO 는 UTF-8 명시, json ensure_ascii=False.
  - 네트워크 호출은 타임아웃·예외 방어. pykrx 전종목 조회는 직전 영업일 1회만.

[기존 시스템과의 관계]
  research_agent.py / supervisor.py / force_analysis.py 등 기존 파일은 일절 수정하지
  않는다. 이 스크립트는 독립 실행되며 결과 파일(market_context.json)만 남긴다.

[사용법]
  python market_collect.py
"""

import os
import sys
import json
import math
import logging
from datetime import datetime, timedelta


# ── KRX 계정 로더 (pykrx import 보다 반드시 먼저) ──────────────────
# pykrx 는 import 시점에 KRX_ID / KRX_PW 환경변수를 읽어 자동 로그인한다.
# force_analysis.py 와 동일하게 krx_account.txt 의 값을 import 전에 주입한다.
# 파일이 없거나 비어 있으면 조용히 패스 → 인증 데이터는 결측 처리(무손상).
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
        pass


_load_krx_account_into_env()


# ── 선택적 의존성 (graceful 처리) ──────────────────────────────
# pykrx 는 import 시점에 KRX 로그인을 시도하는데, KRX 가 IP 를 403 으로 차단하면
# 로그인 응답(HTML)을 JSON 파싱하다 죽는다 → BaseException 으로 흡수(무손상).
try:
    from pykrx import stock as _krx
    PYKRX_AVAILABLE = True
    PYKRX_IMPORT_ERROR = ""
except BaseException as _e:   # ImportError + JSONDecodeError + 기타 전부
    _krx = None
    PYKRX_AVAILABLE = False
    PYKRX_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

try:
    import FinanceDataReader as fdr
    FDR_AVAILABLE = True
except ImportError:
    FDR_AVAILABLE = False

try:
    import yfinance as _yf
    YF_AVAILABLE = True
except ImportError:
    YF_AVAILABLE = False

try:
    import pandas as _pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False

KRX_LOGIN_CONFIGURED = bool(os.getenv("KRX_ID") and os.getenv("KRX_PW"))

# Windows 콘솔 UTF-8 (stdout/stderr) — 그래도 print 에 이모지는 쓰지 않는다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# =====================================================================
# 상수 / 설정
# =====================================================================
HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
MOMENTUM_DAYS = 20          # 섹터 모멘텀 lookback (거래일)
FLOW_5D = 5
FLOW_20D = 20

# 인터마켓 심볼(yfinance) → 출력 키 매핑. (key, yahoo_symbol)
INTERMARKET_SYMBOLS = [
    ("sp500",  "^GSPC"),
    ("nasdaq", "^IXIC"),
    ("dow",    "^DJI"),
    ("sox",    "^SOX"),
    ("vix",    "^VIX"),
    ("ust10y", "^TNX"),     # 미 10년물 — yield(%) + bp 변화로 별도 처리
    ("wti",    "CL=F"),
    ("gold",   "GC=F"),
    ("dxy",    "DX-Y.NYB"),
    ("usdkrw", "KRW=X"),
    # EWY(미국 상장 iShares MSCI Korea ETF) — 한국장 마감 후 미국시간에 거래되므로
    # 06:30 분석 시점의 '간밤 한국물 센티먼트'(갭 예측) 프록시. 미국 전일 종가라 룩어헤드 없음.
    ("ewy",    "EWY"),
]

# 한국 업종지수 — 월가식 테마별로 KRX 표준 업종지수(검증된 코드)를 best-fit 매핑.
# KRX 표준 산업분류라 "반도체/2차전지/방산" 등 순수 테마와 1:1은 아니지만,
# 시장에서 통용되는 가장 가까운 대표 섹터로 연결한다.
# (market: KOSPI=1xxx, KOSDAQ=2xxx / pykrx get_index_ohlcv_by_date 로 조회)
#   name             : 출력에 쓸 테마/섹터명
#   code             : KRX 지수코드
#   proxy(설명)       : 매핑 근거(notes 표기용은 아니고 코드 가독성용 주석)
KR_SECTOR_INDICES = [
    {"name": "전기전자(반도체)",   "code": "2072"},  # KOSDAQ 전기전자
    {"name": "화학(2차전지소재)",  "code": "1008"},  # KOSPI 화학
    {"name": "제약·바이오",        "code": "1009"},  # KOSPI 제약
    {"name": "헬스케어(코스닥)",   "code": "2217"},  # KOSDAQ150 헬스케어
    {"name": "운송장비·부품(자동차)", "code": "1015"},  # KOSPI 운송장비·부품
    {"name": "금융업",             "code": "1024"},  # KOSPI 증권(금융 대표 프록시)
    {"name": "은행·보험",          "code": "1025"},  # KOSPI 보험
    {"name": "기계·장비(조선·방산)", "code": "1012"},  # KOSPI 기계·장비
    {"name": "철강·금속",          "code": "1011"},  # KOSPI 금속
    {"name": "IT서비스(인터넷)",   "code": "2118"},  # KOSDAQ IT서비스
    {"name": "오락·문화(게임)",    "code": "2037"},  # KOSDAQ 오락·문화
    {"name": "통신",               "code": "1020"},  # KOSPI 통신
    {"name": "건설",               "code": "1018"},  # KOSPI 건설
    {"name": "유통",               "code": "1016"},  # KOSPI 유통
]

# 지수코드(KRX) → FinanceDataReader 심볼 (지수 OHLCV FDR 폴백용 — 주요 지수만)
_FDR_INDEX_SYMBOL = {"1001": "KS11", "2001": "KQ11"}

INDEX_KOSPI = "1001"
INDEX_KOSDAQ = "2001"


# ── 로깅 ────────────────────────────────────────────────────────
def _setup_logger():
    lg = logging.getLogger("market_collect")
    if lg.handlers:
        return lg
    lg.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%H:%M:%S"))
    lg.addHandler(sh)
    return lg


log = _setup_logger()


# =====================================================================
# 유틸
# =====================================================================
def _r(v, ndigits=2):
    """안전 round. None/NaN/inf → None."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, ndigits)


def _date_range(days_back):
    end = datetime.now()
    start = end - timedelta(days=days_back)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _won_to_eok(v):
    """원 단위 금액 → 억원(소수1자리). None → None."""
    if v is None:
        return None
    try:
        return round(float(v) / 1e8, 1)
    except (TypeError, ValueError):
        return None


# =====================================================================
# 1. 인터마켓 (yfinance)
# =====================================================================
def collect_intermarket(notes):
    """
    미국 지수/금리/원자재/환율을 yfinance 로 일괄 수집.
    각 심볼: close + chg_pct(전일대비 %). ^TNX 는 yield(%) + chg_bp.
    한 번에 download 하고, 실패한 심볼만 notes 에 기록.
    """
    result = {key: None for key, _ in INTERMARKET_SYMBOLS}
    if not YF_AVAILABLE:
        notes.append("intermarket: yfinance 미설치 — 전체 결측")
        return result

    symbols = [sym for _, sym in INTERMARKET_SYMBOLS]
    data = None
    # 1순위: 일괄 download (최근 7일 → 최소 2 거래일 확보)
    try:
        data = _yf.download(
            tickers=" ".join(symbols),
            period="7d",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=True,
            timeout=30,
        )
    except Exception as e:
        notes.append(f"intermarket: yf.download 일괄 실패 ({type(e).__name__}: {e}) "
                     f"→ 심볼별 개별 재시도")
        data = None

    def _close_series(sym):
        """download 결과에서 sym 의 Close 시리즈(결측 제거) 추출. 실패 시 None."""
        if data is None or not PANDAS_AVAILABLE:
            return None
        try:
            # 다중 심볼: 컬럼이 MultiIndex (field, ticker)
            if isinstance(data.columns, _pd.MultiIndex):
                if ("Close", sym) in data.columns:
                    s = data[("Close", sym)]
                else:
                    return None
            else:
                # 단일 심볼이면 평면 컬럼
                if "Close" in data.columns:
                    s = data["Close"]
                else:
                    return None
            s = s.dropna()
            return s if len(s) >= 1 else None
        except Exception:
            return None

    def _fetch_one(sym):
        """개별 심볼 폴백 — Ticker.history."""
        try:
            t = _yf.Ticker(sym)
            h = t.history(period="7d", interval="1d", auto_adjust=False, timeout=20)
            if h is not None and not h.empty and "Close" in h.columns:
                s = h["Close"].dropna()
                return s if len(s) >= 1 else None
        except Exception:
            return None
        return None

    for key, sym in INTERMARKET_SYMBOLS:
        s = _close_series(sym)
        if s is None or len(s) < 1:
            s = _fetch_one(sym)
        if s is None or len(s) < 1:
            notes.append(f"intermarket.{key}({sym}): 데이터 결측")
            continue
        try:
            close = float(s.iloc[-1])
            prev = float(s.iloc[-2]) if len(s) >= 2 else None
        except Exception:
            notes.append(f"intermarket.{key}({sym}): 종가 파싱 실패")
            continue

        if key == "ust10y":
            # ^TNX 는 '수익률 × 10' 이 아니라 이미 % 단위(예: 43.2 == 4.32%? 야후는
            # ^TNX 를 percent 로 제공: 4.32 형태). yield 그대로 사용, bp 변화 계산.
            chg_bp = None
            if prev is not None:
                chg_bp = round((close - prev) * 100, 1)  # %p → bp
            result[key] = {"yield": _r(close, 3), "chg_bp": chg_bp}
        else:
            chg_pct = None
            if prev is not None and prev != 0:
                chg_pct = _r((close / prev - 1) * 100, 2)
            result[key] = {"close": _r(close, 2), "chg_pct": chg_pct}

    got = sum(1 for v in result.values() if v is not None)
    log.info("[market] intermarket %d/%d 항목 수집", got, len(INTERMARKET_SYMBOLS))
    return result


# =====================================================================
# 2. 한국 섹터 (pykrx 지수 OHLCV → 등락률 + 20일 모멘텀 + RS 랭킹)
# =====================================================================
def _index_closes(code, days=60):
    """
    KRX 지수코드의 종가 리스트(오름차순). pykrx 1순위, 주요지수는 FDR 폴백.
    실패 시 None.
    """
    start, end = _date_range(days * 2)
    if PYKRX_AVAILABLE:
        try:
            df = _krx.get_index_ohlcv_by_date(start, end, code)
            if df is not None and not df.empty and "종가" in df.columns:
                vals = [float(x) for x in df["종가"].dropna().values]
                if len(vals) >= 2:
                    return vals
        except Exception as e:
            log.warning("[market] pykrx 지수 %s OHLCV 실패: %s: %s",
                        code, type(e).__name__, e)
    # FDR 폴백 (주요 지수만 매핑됨)
    sym = _FDR_INDEX_SYMBOL.get(str(code))
    if sym and FDR_AVAILABLE:
        try:
            d0 = datetime.strptime(start, "%Y%m%d").strftime("%Y-%m-%d")
            df = fdr.DataReader(sym, d0)
            if df is not None and not df.empty and "Close" in df.columns:
                vals = [float(x) for x in df["Close"].dropna().values]
                if len(vals) >= 2:
                    return vals
        except Exception as e:
            log.warning("[market] FDR 지수 %s(%s) 실패: %s: %s",
                        code, sym, type(e).__name__, e)
    return None


def collect_kr_sectors(notes):
    """
    주요 업종지수 등락률(직전 거래일) + 20일 모멘텀(%) 수집.
    모멘텀 기준 내림차순 RS 랭킹(rs_rank) 부여. 일부 실패해도 나머지 진행.
    """
    if not (PYKRX_AVAILABLE or FDR_AVAILABLE):
        notes.append("kr_sectors: pykrx·FDR 모두 사용 불가 — 전체 결측")
        return []

    sectors = []
    for spec in KR_SECTOR_INDICES:
        name, code = spec["name"], spec["code"]
        closes = _index_closes(code, days=max(MOMENTUM_DAYS + 15, 40))
        if not closes or len(closes) < 2:
            notes.append(f"kr_sectors.{name}({code}): 지수 데이터 결측")
            sectors.append({"name": name, "code": code, "chg_pct": None,
                            "mom_20d_pct": None, "rs_rank": None})
            continue
        # 직전 거래일 등락률
        chg_pct = None
        if closes[-2] != 0:
            chg_pct = _r((closes[-1] / closes[-2] - 1) * 100, 2)
        # 20일 모멘텀 (충분치 않으면 가용한 만큼)
        mom = None
        lb = MOMENTUM_DAYS
        if len(closes) <= lb:
            lb = len(closes) - 1
        if lb >= 1 and closes[-lb - 1] != 0:
            mom = _r((closes[-1] / closes[-lb - 1] - 1) * 100, 2)
        sectors.append({"name": name, "code": code, "chg_pct": chg_pct,
                        "mom_20d_pct": mom, "rs_rank": None})

    # RS 랭킹: 20일 모멘텀 내림차순. mom 이 None 인 항목은 랭킹에서 제외(맨 뒤 None).
    ranked = [s for s in sectors if s["mom_20d_pct"] is not None]
    ranked.sort(key=lambda s: s["mom_20d_pct"], reverse=True)
    for i, s in enumerate(ranked, 1):
        s["rs_rank"] = i

    got = sum(1 for s in sectors if s["chg_pct"] is not None)
    log.info("[market] kr_sectors %d/%d 업종 수집 (RS 랭킹 %d개)",
             got, len(KR_SECTOR_INDICES), len(ranked))
    return sectors


# =====================================================================
# 3. breadth (pykrx 전종목 등락 — 직전 거래일 1회)
# =====================================================================
def _recent_business_day():
    """pykrx 기준 직전 영업일 YYYYMMDD. 실패 시 어제 날짜 문자열."""
    if PYKRX_AVAILABLE:
        try:
            d = _krx.get_nearest_business_day_in_a_week()
            if d:
                return d
        except Exception:
            pass
    return (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")


def _breadth_for_market(day, market, notes):
    """
    한 시장(KOSPI/KOSDAQ)의 직전 거래일 등락 종목수.
    get_market_ohlcv(day, market=...) 의 등락률 컬럼으로 집계. (전종목 1회 조회)
    반환 dict 또는 None.
    """
    if not PYKRX_AVAILABLE:
        return None
    try:
        df = _krx.get_market_ohlcv(day, market=market)
    except Exception as e:
        notes.append(f"breadth.{market}: 전종목 OHLCV 실패 ({type(e).__name__}: {e})")
        return None
    if df is None or df.empty:
        notes.append(f"breadth.{market}: 전종목 OHLCV 빈 응답 (인증/휴장 의심)")
        return None
    # 등락률 컬럼 우선, 없으면 종가-시가 폴백
    adv = dec = unch = 0
    if "등락률" in df.columns:
        for v in df["등락률"].values:
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f > 0:
                adv += 1
            elif f < 0:
                dec += 1
            else:
                unch += 1
    else:
        notes.append(f"breadth.{market}: 등락률 컬럼 없음")
        return None
    return {"advancers": adv, "decliners": dec, "unchanged": unch,
            "total": adv + dec + unch}


def collect_breadth(notes):
    """
    코스피+코스닥 합산 등락 종목수. 신고가/신저가·MA20상회는 비용이 커
    가능 범위에서만(현재는 결측 처리, 사유 notes 기록).
    """
    result = {"advancers": None, "decliners": None, "unchanged": None,
              "new_highs": None, "new_lows": None, "pct_above_ma20": None}
    if not PYKRX_AVAILABLE:
        notes.append("breadth: pykrx 사용 불가 — 결측")
        return result

    day = _recent_business_day()
    adv = dec = unch = 0
    any_ok = False
    for market in ("KOSPI", "KOSDAQ"):
        b = _breadth_for_market(day, market, notes)
        if b is None:
            continue
        any_ok = True
        adv += b["advancers"]
        dec += b["decliners"]
        unch += b["unchanged"]
    if any_ok:
        result["advancers"] = adv
        result["decliners"] = dec
        result["unchanged"] = unch
        log.info("[market] breadth (%s) 상승 %d / 하락 %d / 보합 %d",
                 day, adv, dec, unch)
    else:
        notes.append(f"breadth: {day} 전종목 등락 집계 실패 (전 시장 결측)")
    # 신고가/신저가, MA20 상회 비율은 전종목 히스토리 필요 → 비용 과다로 결측.
    notes.append("breadth.new_highs/new_lows/pct_above_ma20: 전종목 히스토리 "
                 "비용 과다로 미수집(결측)")
    return result


# =====================================================================
# 4. flows (외국인·기관 시장 전체 순매수 5일/20일 누적)
# =====================================================================
# 시장 전체 투자자별 순매수는 get_market_trading_value_by_investor(start,end,market)
# 가 정답(기간 합산, '순매수' 컬럼, 인덱스=투자자구분). 지수코드(1001 등)를
# get_market_trading_value_by_date 에 넣으면 종목 ISIN 조회로 빠져 빈 응답이 난다.
_INVESTOR_FOREIGN_KEYS = ["외국인", "외국인합계", "외인합계", "외인"]
_INVESTOR_INST_KEYS = ["기관합계", "기관", "기관계"]


def _nth_prev_business_day(end_ymd, n_back):
    """
    end_ymd(YYYYMMDD)로부터 영업일 기준 n_back 일 전 날짜(YYYYMMDD).
    pykrx 영업일 리스트 사용, 실패 시 달력일 근사(영업일≈주중)로 폴백.
    """
    if PYKRX_AVAILABLE:
        try:
            start_guess = (datetime.strptime(end_ymd, "%Y%m%d")
                           - timedelta(days=n_back * 2 + 10)).strftime("%Y%m%d")
            days = _krx.get_previous_business_days(fromdate=start_guess, todate=end_ymd)
            ymds = [d.strftime("%Y%m%d") for d in days]
            if ymds:
                idx = max(0, len(ymds) - n_back)
                return ymds[idx]
        except Exception:
            pass
    # 폴백: 달력일 근사 (영업일 ≈ 주중, 7/5 비율 보정)
    cal_back = int(round(n_back * 7 / 5)) + 3
    return (datetime.strptime(end_ymd, "%Y%m%d")
            - timedelta(days=cal_back)).strftime("%Y%m%d")


def _market_net_by_investor(market, start, end):
    """
    한 시장(KOSPI/KOSDAQ)의 [start,end] 기간 투자자별 순매수(원) 합계.
    반환: (foreign_net, inst_net) — 각 None 가능. 조회 실패 시 (None, None).
    """
    if not PYKRX_AVAILABLE:
        return None, None
    try:
        df = _krx.get_market_trading_value_by_investor(start, end, market)
    except Exception:
        return None, None
    if df is None or df.empty or "순매수" not in df.columns:
        return None, None

    def _pick(keys):
        for k in keys:
            if k in df.index:
                try:
                    return float(df.loc[k, "순매수"])
                except Exception:
                    return None
        return None

    return _pick(_INVESTOR_FOREIGN_KEYS), _pick(_INVESTOR_INST_KEYS)


def collect_flows(notes):
    """
    코스피+코스닥 합산 외국인·기관 순매수 5일/20일 누적(억원).
    각 윈도우(5/20 영업일)에 대해 시장별 투자자 순매수를 합산.
    trend: 외국인+기관 5일 누적 부호로 inflow/outflow/neutral.
    """
    result = {"foreign_net_5d": None, "inst_net_5d": None,
              "foreign_net_20d": None, "inst_net_20d": None, "trend": None}
    if not PYKRX_AVAILABLE:
        notes.append("flows: pykrx 사용 불가 — 결측")
        return result

    end = _recent_business_day()
    start_5 = _nth_prev_business_day(end, FLOW_5D)
    start_20 = _nth_prev_business_day(end, FLOW_20D)

    f5 = i5 = f20 = i20 = None
    any_ok = False
    for market in ("KOSPI", "KOSDAQ"):
        # 5일 윈도우
        fv, iv = _market_net_by_investor(market, start_5, end)
        if fv is not None or iv is not None:
            any_ok = True
            if fv is not None:
                f5 = (f5 or 0) + fv
            if iv is not None:
                i5 = (i5 or 0) + iv
        # 20일 윈도우
        fv2, iv2 = _market_net_by_investor(market, start_20, end)
        if fv2 is not None or iv2 is not None:
            any_ok = True
            if fv2 is not None:
                f20 = (f20 or 0) + fv2
            if iv2 is not None:
                i20 = (i20 or 0) + iv2

    if not any_ok:
        notes.append("flows: 시장 투자자별 데이터 전부 결측 (KRX 인증 차단 의심)")
        return result

    result["foreign_net_5d"] = _won_to_eok(f5)
    result["inst_net_5d"] = _won_to_eok(i5)
    result["foreign_net_20d"] = _won_to_eok(f20)
    result["inst_net_20d"] = _won_to_eok(i20)

    # trend: 외국인+기관 5일 누적 부호
    combo5 = None
    if f5 is not None or i5 is not None:
        combo5 = (f5 or 0) + (i5 or 0)
    if combo5 is None:
        result["trend"] = None
    elif combo5 > 0:
        result["trend"] = "inflow"
    elif combo5 < 0:
        result["trend"] = "outflow"
    else:
        result["trend"] = "neutral"

    log.info("[market] flows 외국인5d %s / 기관5d %s 억원 (trend=%s)",
             result["foreign_net_5d"], result["inst_net_5d"], result["trend"])
    return result


# =====================================================================
# 5. regime (시장 국면 점수 0~100 + 라벨 + drivers)
# =====================================================================
def _kospi_ma_aligned(notes):
    """
    코스피 정배열 판정: 현재가 > MA5 > MA20 > MA60 면 강한 추세.
    반환: (trend_score 0~100, driver_text) — 데이터 없으면 (None, None).
    """
    closes = _index_closes(INDEX_KOSPI, days=120)
    if not closes or len(closes) < 21:
        notes.append("regime.trend: 코스피 지수 데이터 부족 — 추세 축 제외")
        return None, None
    cur = closes[-1]

    def ma(n):
        if len(closes) < n:
            return None
        return sum(closes[-n:]) / n

    ma5, ma20, ma60 = ma(5), ma(20), ma(60)
    # 점수: 조건 충족 개수 기반 0~100
    checks = []
    if ma5 is not None:
        checks.append(cur > ma5)
    if ma5 is not None and ma20 is not None:
        checks.append(ma5 > ma20)
    if ma20 is not None and ma60 is not None:
        checks.append(ma20 > ma60)
    if ma20 is not None:
        checks.append(cur > ma20)
    if not checks:
        return None, None
    score = round(sum(1 for c in checks if c) / len(checks) * 100)
    if ma20 is not None:
        if cur > ma20 and (ma5 is None or ma5 > ma20):
            drv = "코스피 20일선 상회(정배열 우위)"
        elif cur < ma20:
            drv = "코스피 20일선 하회(추세 약화)"
        else:
            drv = "코스피 20일선 부근(중립)"
    else:
        drv = "코스피 단기 추세"
    return score, drv


def compute_regime(intermarket, breadth, flows, notes):
    """
    risk-on 점수(0~100) 종합. 가용 축만 가중 평균 → 라벨/drivers.
      - trend   (코스피 MA 정배열)           : 0~100
      - breadth (상승종목 비율)               : 0~100
      - flows   (외국인+기관 5일 수급 부호)    : 0/50/100
      - vix     (VIX 레벨)                    : 낮을수록 risk-on
      - fx      (원달러 — 약세/원화강세 risk-on): 일중 등락 기반
    각 축은 가용할 때만 평균에 포함. 전부 결측이면 score=None.
    """
    drivers = []
    parts = []   # (label, score, weight)

    # 1) trend
    t_score, t_drv = _kospi_ma_aligned(notes)
    if t_score is not None:
        parts.append(("trend", t_score, 0.30))
        if t_drv:
            drivers.append(t_drv)

    # 2) breadth — 상승 비율
    adv, dec = breadth.get("advancers"), breadth.get("decliners")
    if adv is not None and dec is not None and (adv + dec) > 0:
        ratio = adv / (adv + dec)
        b_score = round(ratio * 100)
        parts.append(("breadth", b_score, 0.25))
        if ratio >= 0.6:
            drivers.append(f"시장 폭 양호(상승 {adv} vs 하락 {dec})")
        elif ratio <= 0.4:
            drivers.append(f"시장 폭 부진(상승 {adv} vs 하락 {dec})")
        else:
            drivers.append(f"시장 폭 중립(상승 {adv} vs 하락 {dec})")

    # 3) flows — 외국인+기관 5일 수급 방향
    trend = flows.get("trend")
    f5, i5 = flows.get("foreign_net_5d"), flows.get("inst_net_5d")
    if trend in ("inflow", "outflow", "neutral"):
        if trend == "inflow":
            fl_score = 100
            drivers.append(f"외국인·기관 순유입(외인5d {f5} / 기관5d {i5} 억원)")
        elif trend == "outflow":
            fl_score = 0
            drivers.append(f"외국인·기관 순유출(외인5d {f5} / 기관5d {i5} 억원)")
        else:
            fl_score = 50
            drivers.append("외국인·기관 수급 중립")
        parts.append(("flows", fl_score, 0.25))

    # 4) VIX — 레벨 기반 risk-on (낮을수록 위험선호)
    vix = (intermarket.get("vix") or {}).get("close") if intermarket.get("vix") else None
    if vix is not None:
        # VIX 12 이하 → 100, 35 이상 → 0 선형
        v_score = max(0, min(100, round((35 - vix) / (35 - 12) * 100)))
        parts.append(("vix", v_score, 0.10))
        if vix >= 25:
            drivers.append(f"VIX 고공({vix}) — 위험회피")
        elif vix <= 16:
            drivers.append(f"VIX 안정({vix}) — 위험선호")
        else:
            drivers.append(f"VIX 중간({vix})")

    # 5) 환율 — 원달러 등락(원화 강세=risk-on). 일중 등락률 기반.
    usdkrw = intermarket.get("usdkrw") or {}
    fx_chg = usdkrw.get("chg_pct") if usdkrw else None
    if fx_chg is not None:
        # 원달러 -0.5% (원화강세) → 100, +0.5% (원화약세) → 0
        fx_score = max(0, min(100, round((0.5 - fx_chg) / 1.0 * 100)))
        parts.append(("fx", fx_score, 0.10))
        if fx_chg <= -0.3:
            drivers.append(f"원화 강세(원달러 {fx_chg}%) — risk-on")
        elif fx_chg >= 0.3:
            drivers.append(f"원화 약세(원달러 +{fx_chg}%) — risk-off")

    if not parts:
        notes.append("regime: 가용 축 없음 — 점수 산출 불가")
        return {"score": None, "label": None, "drivers": drivers}

    # 가중 평균(가용 축의 가중치 합으로 정규화)
    tot_w = sum(w for _, _, w in parts)
    score = round(sum(s * w for _, s, w in parts) / tot_w) if tot_w > 0 else None

    if score is None:
        label = None
    elif score >= 60:
        label = "risk-on"
    elif score >= 40:
        label = "neutral"
    else:
        label = "risk-off"

    used = ", ".join(n for n, _, _ in parts)
    log.info("[market] regime score=%s label=%s (축: %s)", score, label, used)
    return {"score": score, "label": label, "drivers": drivers}


# =====================================================================
# 저장 위치 결정
# =====================================================================
def _today_latest_session():
    """H-3: common.resolve_session 위임 — 자정 경계 완화(6h 폴백) + 11곳 복제 제거."""
    from common import resolve_session
    return resolve_session(OUTPUT_DIR)


def _resolve_output_path():
    """저장 경로(절대) 반환. 오늘자 세션 우선, 없으면 BASE_DIR. (path, used_session)"""
    sess = _today_latest_session()
    if sess:
        return os.path.join(sess, "market_context.json"), True
    return os.path.join(HERE, "market_context.json"), False


# =====================================================================
# 메인
# =====================================================================
def collect_kr_index_momentum(notes):
    """
    KOSPI/KOSDAQ '직전 5거래일 수익률(%)' — 회고(2026-07-03, 3회 재현)의
    '강세장 추격 진입 게이트'(pre_kospi_ret5d) 를 아침 분석이 기계적으로 읽게 하는 필드.
    06:30 실행 시 마지막 종가 = 전일이므로 룩어헤드 없음(진입 시점에 아는 값).
    +2% 초과 = 강세추격 국면(회고 실측: 픽 ret_h -9.3%/적중 14%) → cowork [3-차익실현](5) 게이트.
    """
    out = {"kospi_ret5d_pct": None, "kosdaq_ret5d_pct": None, "asof_close": None}
    for key, code in (("kospi_ret5d_pct", INDEX_KOSPI), ("kosdaq_ret5d_pct", INDEX_KOSDAQ)):
        try:
            closes = _index_closes(code, days=15)
            if closes and len(closes) >= 6:
                out[key] = round((closes[-1] / closes[-6] - 1) * 100, 2)
                if key == "kospi_ret5d_pct":
                    out["asof_close"] = round(float(closes[-1]), 2)
            else:
                notes.append(f"kr_index: {code} 종가 부족(5d 수익률 결측)")
        except Exception as e:
            notes.append(f"kr_index: {code} 실패 ({type(e).__name__}: {e})")
    if out["kospi_ret5d_pct"] is not None:
        log.info("[market] kr_index KOSPI 5d %+0.2f%% / KOSDAQ 5d %s%%",
                 out["kospi_ret5d_pct"],
                 out["kosdaq_ret5d_pct"] if out["kosdaq_ret5d_pct"] is not None else "?")
    return out


def build_context():
    notes = []

    if not YF_AVAILABLE:
        notes.append("환경: yfinance 미설치 — 인터마켓 결측")
    if not PYKRX_AVAILABLE:
        if PYKRX_IMPORT_ERROR:
            notes.append(f"환경: pykrx 사용 불가(KRX 접속 차단/로그인 실패 추정: "
                         f"{PYKRX_IMPORT_ERROR}) — 섹터/breadth/flows 일부 결측")
        else:
            notes.append("환경: pykrx 미설치 — 섹터/breadth/flows 결측")
    elif not KRX_LOGIN_CONFIGURED:
        notes.append("환경: KRX 로그인 미설정 — 투자자별 flows 가 결측될 수 있음")

    # 각 항목 개별 방어 (한 항목 실패가 전체를 막지 않게)
    try:
        intermarket = collect_intermarket(notes)
    except Exception as e:
        notes.append(f"intermarket: 예외 ({type(e).__name__}: {e})")
        intermarket = {key: None for key, _ in INTERMARKET_SYMBOLS}

    try:
        kr_sectors = collect_kr_sectors(notes)
    except Exception as e:
        notes.append(f"kr_sectors: 예외 ({type(e).__name__}: {e})")
        kr_sectors = []

    try:
        breadth = collect_breadth(notes)
    except Exception as e:
        notes.append(f"breadth: 예외 ({type(e).__name__}: {e})")
        breadth = {"advancers": None, "decliners": None, "unchanged": None,
                   "new_highs": None, "new_lows": None, "pct_above_ma20": None}

    try:
        flows = collect_flows(notes)
    except Exception as e:
        notes.append(f"flows: 예외 ({type(e).__name__}: {e})")
        flows = {"foreign_net_5d": None, "inst_net_5d": None,
                 "foreign_net_20d": None, "inst_net_20d": None, "trend": None}

    try:
        regime = compute_regime(intermarket, breadth, flows, notes)
    except Exception as e:
        notes.append(f"regime: 예외 ({type(e).__name__}: {e})")
        regime = {"score": None, "label": None, "drivers": []}

    try:
        kr_index = collect_kr_index_momentum(notes)
    except Exception as e:
        notes.append(f"kr_index: 예외 ({type(e).__name__}: {e})")
        kr_index = {"kospi_ret5d_pct": None, "kosdaq_ret5d_pct": None, "asof_close": None}

    try:
        etf_flow = collect_etf_flow(notes)
    except Exception as e:
        notes.append(f"etf_flow: 예외 ({type(e).__name__}: {e})")
        etf_flow = None

    context = {
        "generated_at": datetime.now().isoformat(),
        "intermarket": intermarket,
        "kr_sectors": kr_sectors,
        "breadth": breadth,
        "flows": flows,
        "regime": regime,
        "kr_index": kr_index,
        "etf_flow": etf_flow,
        "notes": notes,
    }
    return context


def collect_etf_flow(notes):
    """KODEX 레버리지(122630)/인버스2X(252670) 거래대금 — 개인 방향성 베팅 강도(F8 투기심리 보조).
    lev_inv_ratio > 1 이면 상방 베팅 우세, 급등 시 과열 방향 쏠림. pykrx 1순위, FDR 폴백(Close*Volume 근사)."""
    out = {"lev_value_eok": None, "inv_value_eok": None, "lev_inv_ratio": None,
           "asof": None, "source": None}
    pairs = [("lev", "122630"), ("inv", "252670")]
    start, end = _date_range(14)
    for tag, code in pairs:
        val = asof = src = None
        if PYKRX_AVAILABLE:
            try:
                df = _krx.get_market_ohlcv_by_date(start, end, code)
                if df is not None and len(df) and "거래대금" in df.columns:
                    val = _won_to_eok(float(df["거래대금"].iloc[-1]))
                    asof = str(df.index[-1])[:10]
                    src = "krx"
            except Exception as e:
                notes.append(f"etf_flow {code}: pykrx 실패({type(e).__name__})")
        if val is None and FDR_AVAILABLE:
            try:
                df = fdr.DataReader(code)
                if df is not None and len(df) and "Close" in df.columns and "Volume" in df.columns:
                    val = _won_to_eok(float(df["Close"].iloc[-1]) * float(df["Volume"].iloc[-1]))
                    asof = str(df.index[-1])[:10]
                    src = "fdr(Close*Volume 근사)"
            except Exception as e:
                notes.append(f"etf_flow {code}: FDR 폴백 실패({type(e).__name__})")
        out[f"{tag}_value_eok"] = val
        out["asof"] = out["asof"] or asof
        out["source"] = out["source"] or src
    if out["lev_value_eok"] and out["inv_value_eok"]:
        out["lev_inv_ratio"] = round(out["lev_value_eok"] / out["inv_value_eok"], 2)
    return out


def main():
    log.info("[market] 시장 컨텍스트 수집 시작 "
             "(pykrx=%s, fdr=%s, yfinance=%s, krx_login=%s)",
             PYKRX_AVAILABLE, FDR_AVAILABLE, YF_AVAILABLE, KRX_LOGIN_CONFIGURED)

    try:
        context = build_context()
    except Exception as e:
        # 최후 방어 — 컨텍스트 구성 자체가 실패해도 최소 골격을 남기고 exit 0
        log.error("[market] build_context 치명 예외: %s: %s", type(e).__name__, e)
        context = {
            "generated_at": datetime.now().isoformat(),
            "intermarket": None, "kr_sectors": None, "breadth": None,
            "flows": None, "regime": None, "kr_index": None, "etf_flow": None,
            "notes": [f"build_context 치명 예외: {type(e).__name__}: {e}"],
        }

    out_path, used_session = _resolve_output_path()
    if not used_session:
        log.warning("[market] 오늘자 세션 폴더 없음 → BASE_DIR 에 저장: %s", out_path)

    # 원자적 저장 (.tmp → replace)
    tmp = out_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(context, f, ensure_ascii=False, indent=2)
        os.replace(tmp, out_path)
    except Exception as e:
        log.error("[market] 저장 실패: %s: %s", type(e).__name__, e)
        # 저장 실패해도 파이프라인 중단 금지
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass
        # exit 0 유지
        return

    # 요약 로그 (ASCII 태그만)
    im = context.get("intermarket") or {}
    im_got = sum(1 for v in im.values() if v) if isinstance(im, dict) else 0
    secs = context.get("kr_sectors") or []
    sec_got = sum(1 for s in secs if s.get("chg_pct") is not None) if secs else 0
    rg = context.get("regime") or {}
    log.info("[market] 저장 완료: %s", out_path)
    log.info("[market] 요약 | intermarket %d | sectors %d | regime %s(%s) | notes %d",
             im_got, sec_got, rg.get("score"), rg.get("label"),
             len(context.get("notes") or []))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # 어떤 경우에도 파이프라인을 중단시키지 않는다 (exit 0).
        try:
            log.error("[market] 최상위 예외(무시하고 종료): %s: %s",
                      type(e).__name__, e)
        except Exception:
            pass
    sys.exit(0)
