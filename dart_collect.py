#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dart_collect.py — 장투(장기투자) 펀더멘털 수집기

[목적]
  장전(06:30) supervisor 파이프라인의 market_collect 직후 실행돼, watch_tickers.txt
  종목 풀의 '연간 펀더멘털'을 수집/계산해 오늘자 세션 폴더에 fundamentals.json 으로
  저장한다. 분석(Cowork)이 이 파일을 읽어 [장투가능] 판정에 활용한다.

[수집/계산 항목] — 사용자 요청(장투 고려요소)
  - 매출액(Revenue), 영업이익(Operating Income), 당기순이익(Net Income)
  - 매출총이익률 GPM = 매출총이익 / 매출액
  - 영업이익률   OPM = 영업이익 / 매출액
  - 순이익률     NPM = 당기순이익 / 매출액
  - 잉여현금흐름 FCF = 영업활동현금흐름(CFO) - 자본적지출(CAPEX)
  - FCF 마진 = FCF / 매출액, 부채비율(있으면)
  + 최근 4개 연도 추세(매출 YoY, OPM/GPM 추세, FCF 흑자 지속 여부) + 장투 적합도 힌트

[데이터 소스 — 2단계]
  1) DART(OpenDART, 공식 공시) : dart_api.txt 에 API 키가 있으면 우선 사용(정확).
     - 무료 키 발급: https://opendart.fss.or.kr  → '인증키 신청/관리'
     - 키를 dart_api.txt 에 한 줄로 넣으면 자동 활성화(재시작 불필요, 다음 수집부터).
  2) yfinance(야후 파이낸스)  : 키가 없거나 DART 결측이면 폴백. 키 없이 바로 동작.
     - 한국 종목은 .KS(코스피)/.KQ(코스닥) 접미사로 조회. 대형·중형주 커버리지 양호.

[설계 원칙] (market_collect.py 와 동일 기조)
  - 종목별 개별 try/except — 일부 실패해도 나머지 진행. 사유는 notes/per-ticker notes.
  - 콘솔 print 는 ASCII 태그([dart])만 — 윈도우 cp949 콘솔 크래시 방지(이모지 금지).
  - 파일 IO 는 UTF-8 명시, json ensure_ascii=False. 원자적 저장(.tmp → replace).
  - 캐시(cache/fundamentals_cache.json): 같은 날 재실행 시 재조회 생략(연간 재무는 분기당
    1회만 갱신되므로 하루 캐시로 충분). 부분 실패해도 exit 0(파이프라인 무중단).
  - 기존 파일(research_agent/supervisor/force_analysis/market_collect)은 일절 수정하지
    않는다. 이 스크립트는 독립 실행되며 fundamentals.json 만 남긴다.

[사용법]
  python dart_collect.py                  # watch_tickers 전체 → 오늘 세션/fundamentals.json
  python dart_collect.py --tickers 005930,000660   # 특정 종목만(테스트)
  python dart_collect.py --limit 5        # 풀 앞에서 N개만(테스트)
  python dart_collect.py --no-cache       # 캐시 무시하고 재조회
"""
import os
import sys
import io
import json
import time
import zipfile
import argparse
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
CACHE_DIR = os.path.join(HERE, "cache")
TICKERS_FILE = os.path.join(HERE, "watch_tickers.txt")
DART_KEY_FILE = os.path.join(HERE, "dart_api.txt")
CACHE_FILE = os.path.join(CACHE_DIR, "fundamentals_cache.json")
CORP_CACHE_FILE = os.path.join(CACHE_DIR, "dart_corpcodes.json")

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("dart")

YEARS_BACK = 4          # 최근 N개 연도(추세용)
MAX_WORKERS = 6         # 네트워크 동시성(yfinance/DART rate-limit 고려해 보수적으로)
HTTP_TIMEOUT = 20

try:
    import requests
except Exception:
    requests = None

try:
    import yfinance as yf
    YF_OK = True
except Exception:
    YF_OK = False


# =====================================================================
# 종목 풀 / 키 로딩
# =====================================================================
def load_universe():
    """watch_tickers.txt → [(code6, name), ...]. 주석(# 뒤)에서 종목명 추출."""
    out = []
    if not os.path.isfile(TICKERS_FILE):
        return out
    try:
        with open(TICKERS_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                code = line.split("#")[0].strip()
                if not (code.isdigit() and len(code) == 6):
                    continue
                name = ""
                if "#" in line:
                    name = line.split("#", 1)[1].strip()
                out.append((code, name))
    except Exception as e:
        log.warning("[dart] watch_tickers 읽기 실패: %s", e)
    return out


def load_dart_key():
    """dart_api.txt 에서 OpenDART 인증키 1개. 없으면 ''(→ yfinance 폴백)."""
    if not os.path.isfile(DART_KEY_FILE):
        return ""
    try:
        with open(DART_KEY_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # KEY=xxxx 또는 그냥 xxxx 한 줄 모두 허용
                if "=" in line:
                    line = line.split("=", 1)[1].strip()
                line = line.strip().strip('"').strip("'")
                if len(line) >= 20:        # OpenDART 키는 40자 hex
                    return line
    except Exception:
        pass
    return ""


# =====================================================================
# 캐시
# =====================================================================
def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


from common import save_json_atomic as _save_json_atomic  # 원자적 JSON 저장(common.py 통합)


# =====================================================================
# 지표 계산 (소스 공통)
#   periods_raw: [{"year": "2025", "revenue":..., "gross_profit":...(opt),
#                  "cogs":...(opt), "operating_income":..., "net_income":...,
#                  "cfo":...(opt), "capex":...(opt, 음수 가능), "fcf":...(opt),
#                  "liabilities":...(opt), "equity":...(opt)}], 최신연도 먼저
# =====================================================================
def _pct(n, d):
    try:
        if n is None or not d:
            return None
        return round(100.0 * float(n) / float(d), 1)
    except Exception:
        return None


def _ratio(n, d):
    try:
        if n is None or not d:
            return None
        return round(float(n) / float(d), 3)
    except Exception:
        return None


def compute_metrics(periods_raw):
    """원자료 연도 리스트 → 지표/추세/장투힌트가 채워진 dict."""
    periods = []
    for p in periods_raw[:YEARS_BACK]:
        rev = p.get("revenue")
        gp = p.get("gross_profit")
        cogs = p.get("cogs")
        if gp is None and rev is not None and cogs is not None:
            try:
                gp = float(rev) - float(cogs)
            except Exception:
                gp = None
        oi = p.get("operating_income")
        ni = p.get("net_income")
        cfo = p.get("cfo")
        capex = p.get("capex")     # 보통 음수(현금 유출)
        fcf = p.get("fcf")
        if fcf is None and cfo is not None and capex is not None:
            try:
                fcf = float(cfo) + float(capex)   # capex 가 음수이므로 더한다
            except Exception:
                fcf = None
        liab = p.get("liabilities")
        eq = p.get("equity")
        periods.append({
            "year": p.get("year"),
            "revenue": rev,
            "gross_profit": gp,
            "operating_income": oi,
            "net_income": ni,
            "cfo": cfo,
            "capex": capex,
            "fcf": fcf,
            "gpm": _pct(gp, rev),
            "opm": _pct(oi, rev),
            "npm": _pct(ni, rev),
            "fcf_margin": _pct(fcf, rev),
            "debt_ratio": _pct(liab, eq),     # 부채비율(%) = 부채/자본
        })

    trend = {}
    flags = {}
    if periods:
        cur = periods[0]
        prev = periods[1] if len(periods) > 1 else None
        # 매출 YoY
        if prev and cur.get("revenue") and prev.get("revenue"):
            trend["revenue_yoy_pct"] = _pct(
                float(cur["revenue"]) - float(prev["revenue"]), prev["revenue"])
        else:
            trend["revenue_yoy_pct"] = None
        # OPM/GPM 추세(전년 대비 pp 변화)
        trend["opm_delta_pp"] = (round(cur["opm"] - prev["opm"], 1)
                                 if prev and cur.get("opm") is not None and prev.get("opm") is not None else None)
        trend["gpm_delta_pp"] = (round(cur["gpm"] - prev["gpm"], 1)
                                 if prev and cur.get("gpm") is not None and prev.get("gpm") is not None else None)
        # FCF 흑자 지속
        fcfs = [p.get("fcf") for p in periods if p.get("fcf") is not None]
        trend["fcf_positive_latest"] = (cur.get("fcf") is not None and cur["fcf"] > 0)
        trend["fcf_positive_3y"] = (len(fcfs) >= 3 and all(x > 0 for x in fcfs[:3]))
        # 종합 불리언
        flags["fcf_positive"] = bool(trend["fcf_positive_latest"])
        flags["opm_improving"] = bool(trend.get("opm_delta_pp") is not None and trend["opm_delta_pp"] >= 0)
        flags["revenue_growing"] = bool(trend.get("revenue_yoy_pct") is not None and trend["revenue_yoy_pct"] > 0)
        opm = cur.get("opm")
        flags["margin_quality"] = ("high" if (opm is not None and opm >= 10)
                                   else "mid" if (opm is not None and opm >= 5)
                                   else "low" if opm is not None else None)
        # 장투 적합도 힌트(간단 휴리스틱 — 최종 판단은 Cowork)
        grade = "관찰"
        if opm is not None and cur.get("fcf") is not None:
            if cur["fcf"] > 0 and opm >= 10 and flags["opm_improving"] and flags["revenue_growing"]:
                grade = "A"
            elif cur["fcf"] > 0 and opm >= 5:
                grade = "B"
            elif opm > 0:
                grade = "C"
            else:
                grade = "관찰"
        flags["long_term_grade"] = grade

    return {"periods": periods, "latest": (periods[0] if periods else None),
            "trend": trend, "quality_flags": flags}


# =====================================================================
# 소스 1: yfinance (키 불필요, 폴백/기본)
# =====================================================================
def _yf_row(df, *names):
    if df is None:
        return None
    try:
        for n in names:
            if n in df.index:
                return df.loc[n]
    except Exception:
        return None
    return None


def fetch_yf(code, name):
    """yfinance 로 연간 펀더멘털. .KS 먼저, 비면 .KQ. 실패 시 None."""
    if not YF_OK:
        return None
    last_err = None
    for suf in (".KS", ".KQ"):
        try:
            t = yf.Ticker(code + suf)
            I = t.income_stmt
            if I is None or getattr(I, "empty", True):
                continue
            C = t.cashflow
            B = t.balance_sheet
            rev = _yf_row(I, "Total Revenue", "Operating Revenue")
            gp = _yf_row(I, "Gross Profit")
            cogs = _yf_row(I, "Cost Of Revenue", "Reconciled Cost Of Revenue")
            oi = _yf_row(I, "Operating Income", "Total Operating Income As Reported")
            ni = _yf_row(I, "Net Income", "Net Income Common Stockholders")
            fcf = _yf_row(C, "Free Cash Flow")
            cfo = _yf_row(C, "Operating Cash Flow", "Cash Flow From Continuing Operating Activities")
            capex = _yf_row(C, "Capital Expenditure")
            liab = _yf_row(B, "Total Liabilities Net Minority Interest", "Total Liabilities")
            eq = _yf_row(B, "Stockholders Equity", "Total Equity Gross Minority Interest")

            cols = list(I.columns)
            periods_raw = []

            def val(series, i):
                try:
                    if series is None:
                        return None
                    v = series.iloc[i]
                    return None if v != v else float(v)   # NaN 체크
                except Exception:
                    return None

            for i, c in enumerate(cols[:YEARS_BACK]):
                try:
                    yr = str(c.date())[:4]
                except Exception:
                    yr = str(c)[:4]
                periods_raw.append({
                    "year": yr,
                    "revenue": val(rev, i),
                    "gross_profit": val(gp, i),
                    "cogs": val(cogs, i),
                    "operating_income": val(oi, i),
                    "net_income": val(ni, i),
                    "cfo": val(cfo, i),
                    "capex": val(capex, i),
                    "fcf": val(fcf, i),
                    "liabilities": val(liab, i),
                    "equity": val(eq, i),
                })
            # 최소 매출/영업이익이 하나라도 있어야 유효
            if not any(p.get("revenue") for p in periods_raw):
                continue
            m = compute_metrics(periods_raw)
            m.update({"source": "yfinance", "yf_symbol": code + suf,
                      "currency": "KRW"})
            return m
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
    return None if last_err is None else {"_error": last_err}


# =====================================================================
# 소스 2: DART(OpenDART) — 공식 공시(키 있을 때 우선)
# =====================================================================
DART_BASE = "https://opendart.fss.or.kr/api"


def load_corp_map(key, force=False):
    """stock_code(6) -> corp_code(8) 매핑. corpCode.xml(zip) 캐시(30일)."""
    cache = _load_json(CORP_CACHE_FILE, None)
    if cache and not force:
        try:
            age_days = (time.time() - cache.get("_ts", 0)) / 86400.0
            if age_days < 30 and cache.get("map"):
                return cache["map"]
        except Exception:
            pass
    if not (requests and key):
        return (cache or {}).get("map", {}) if cache else {}
    try:
        r = requests.get(f"{DART_BASE}/corpCode.xml",
                         params={"crtfc_key": key}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        xml = zf.read(zf.namelist()[0]).decode("utf-8")
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
        m = {}
        for el in root.iter("list"):
            sc = (el.findtext("stock_code") or "").strip()
            cc = (el.findtext("corp_code") or "").strip()
            if sc and sc.isdigit() and len(sc) == 6 and cc:
                m[sc] = cc
        if m:
            _save_json_atomic(CORP_CACHE_FILE, {"_ts": time.time(), "map": m})
        return m
    except Exception as e:
        log.warning("[dart] corpCode 매핑 실패: %s", e)
        return (cache or {}).get("map", {}) if cache else {}


def _dart_find(items, sj_div, *name_substrs):
    """fnlttSinglAcntAll items 에서 (sj_div, account_nm 부분일치) 의 thstrm_amount(float)."""
    for it in items:
        if it.get("sj_div") != sj_div:
            continue
        nm = (it.get("account_nm") or "").replace(" ", "")
        if any(s.replace(" ", "") in nm for s in name_substrs):
            raw = (it.get("thstrm_amount") or "").replace(",", "").strip()
            try:
                return float(raw)
            except Exception:
                return None
    return None


def _dart_year(key, corp, year):
    """단일 연도(사업보고서 11011) 전체재무제표 → 원자료 dict 또는 None."""
    for fs_div in ("CFS", "OFS"):       # 연결 우선, 없으면 별도
        try:
            r = requests.get(f"{DART_BASE}/fnlttSinglAcntAll.json",
                             params={"crtfc_key": key, "corp_code": corp,
                                     "bsns_year": str(year), "reprt_code": "11011",
                                     "fs_div": fs_div}, timeout=HTTP_TIMEOUT)
            j = r.json()
            if j.get("status") != "000":
                continue
            items = j.get("list", [])
            if not items:
                continue
            rev = (_dart_find(items, "IS", "매출액", "수익(매출액)", "영업수익")
                   or _dart_find(items, "CIS", "매출액", "수익(매출액)", "영업수익"))
            cogs = (_dart_find(items, "IS", "매출원가")
                    or _dart_find(items, "CIS", "매출원가"))
            gp = (_dart_find(items, "IS", "매출총이익")
                  or _dart_find(items, "CIS", "매출총이익"))
            oi = (_dart_find(items, "IS", "영업이익")
                  or _dart_find(items, "CIS", "영업이익"))
            ni = (_dart_find(items, "IS", "당기순이익")
                  or _dart_find(items, "CIS", "당기순이익"))
            cfo = _dart_find(items, "CF", "영업활동현금흐름", "영업활동으로인한현금흐름",
                             "영업활동순현금흐름")
            capex_t = _dart_find(items, "CF", "유형자산의취득", "유형자산의증가")
            capex_i = _dart_find(items, "CF", "무형자산의취득", "무형자산의증가")
            capex = None
            if capex_t is not None or capex_i is not None:
                capex = -(abs(capex_t or 0) + abs(capex_i or 0))   # 유출(음수)로 정규화
            liab = _dart_find(items, "BS", "부채총계")
            eq = _dart_find(items, "BS", "자본총계")
            if rev is None and oi is None:
                continue
            return {"year": str(year), "revenue": rev, "cogs": cogs, "gross_profit": gp,
                    "operating_income": oi, "net_income": ni, "cfo": cfo,
                    "capex": capex, "liabilities": liab, "equity": eq, "_fs": fs_div}
        except Exception:
            continue
    return None


def fetch_dart(code, name, key, corp_map):
    corp = corp_map.get(code)
    if not (requests and key and corp):
        return None
    this_year = datetime.now().year
    years = [this_year - 1, this_year - 2, this_year - 3, this_year - 4]
    periods_raw = []
    for y in years:
        d = _dart_year(key, corp, y)
        if d:
            periods_raw.append(d)
    if not periods_raw or not any(p.get("revenue") for p in periods_raw):
        return None
    m = compute_metrics(periods_raw)
    m.update({"source": "dart", "corp_code": corp, "currency": "KRW"})
    return m


# =====================================================================
# 종목 1개 처리(캐시 → DART → yfinance)
# =====================================================================
def process_one(code, name, key, corp_map, cache, use_cache, today):
    # 캐시 히트(같은 날)
    if use_cache:
        c = cache.get(code)
        if c and c.get("date") == today and c.get("data"):
            d = dict(c["data"]); d["_cached"] = True
            return code, d
    result = None
    if key:
        try:
            result = fetch_dart(code, name, key, corp_map)
        except Exception as e:
            log.warning("[dart] %s DART 예외: %s", code, e)
    if not result or not result.get("latest"):
        yf_res = fetch_yf(code, name)
        if yf_res and yf_res.get("latest"):
            result = yf_res
        elif result is None and isinstance(yf_res, dict) and yf_res.get("_error"):
            result = {"source": "none", "latest": None, "periods": [],
                      "trend": {}, "quality_flags": {}, "notes": [yf_res["_error"]]}
    if not result:
        result = {"source": "none", "latest": None, "periods": [],
                  "trend": {}, "quality_flags": {}, "notes": ["조회 실패"]}
    result["ticker"] = code
    result["name"] = name
    # 캐시에 저장(성공분만)
    if result.get("latest"):
        cache[code] = {"date": today, "data": {k: v for k, v in result.items()
                                               if k not in ("_cached",)}}
    return code, result


# =====================================================================
# 저장 위치
# =====================================================================
def _today_latest_session():
    """H-3: common.resolve_session 위임 — 자정 경계 완화(6h 폴백) + 11곳 복제 제거."""
    from common import resolve_session
    return resolve_session(OUTPUT_DIR)


def _resolve_output_path(explicit):
    if explicit:
        return os.path.abspath(explicit), bool(_today_latest_session())
    sess = _today_latest_session()
    if sess:
        return os.path.join(sess, "fundamentals.json"), True
    return os.path.join(HERE, "fundamentals.json"), False


# =====================================================================
# 메인
# =====================================================================
def main():
    ap = argparse.ArgumentParser(
        description="장투 펀더멘털 수집기(DART 우선 + yfinance 폴백) → fundamentals.json")
    ap.add_argument("--tickers", default="", help="쉼표구분 6자리 코드만(테스트)")
    ap.add_argument("--limit", type=int, default=0, help="풀 앞에서 N개만(테스트)")
    ap.add_argument("--no-cache", action="store_true", help="캐시 무시 재조회")
    ap.add_argument("--out", default="", help="출력 경로 직접 지정(테스트)")
    args = ap.parse_args()

    key = load_dart_key()
    corp_map = load_corp_map(key) if key else {}
    universe = load_universe()
    if args.tickers:
        want = [t.strip() for t in args.tickers.split(",") if t.strip()]
        nm = {c: n for c, n in universe}
        universe = [(c, nm.get(c, "")) for c in want]
    if args.limit and args.limit > 0:
        universe = universe[:args.limit]

    today = datetime.now().strftime("%Y-%m-%d")
    cache = {} if args.no_cache else _load_json(CACHE_FILE, {})
    if not isinstance(cache, dict):
        cache = {}

    log.info("[dart] 시작 — 종목 %d개 / 소스: %s",
             len(universe), "DART(키있음)+yfinance" if key else "yfinance(키없음)")
    if not key:
        log.info("[dart] OpenDART 키 없음 → yfinance 사용. (dart_api.txt 에 키 넣으면 다음부터 DART 우선)")
    if not YF_OK:
        log.info("[dart] 경고: yfinance 미설치 — 폴백 불가")

    results = {}
    notes = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(process_one, c, n, key, corp_map, cache,
                          not args.no_cache, today): c for c, n in universe}
        done = 0
        for fut in as_completed(futs):
            c = futs[fut]
            done += 1
            try:
                code, res = fut.result()
                results[code] = res
                g = (res.get("quality_flags") or {}).get("long_term_grade", "-")
                src = res.get("source", "?")
                log.info("[dart] (%d/%d) %s %s grade=%s",
                         done, len(universe), code, src, g)
            except Exception as e:
                notes.append(f"{c}: {type(e).__name__}: {e}")
                log.warning("[dart] %s 처리 예외: %s", c, e)

    # 원래 풀 순서 유지
    ordered = []
    by_source = {"dart": 0, "yfinance": 0, "none": 0}
    for c, n in universe:
        r = results.get(c)
        if not r:
            continue
        r.pop("_cached", None)
        ordered.append(r)
        by_source[r.get("source", "none")] = by_source.get(r.get("source", "none"), 0) + 1

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "horizon": "장투(장기투자) 연간 펀더멘털",
        "metrics_explained": {
            "GPM": "매출총이익률 = 매출총이익/매출액 (제품 자체의 마진 체력)",
            "OPM": "영업이익률 = 영업이익/매출액 (본업 수익성)",
            "NPM": "순이익률 = 당기순이익/매출액",
            "FCF": "잉여현금흐름 = 영업활동현금흐름(CFO) - 자본적지출(CAPEX). 기업이 실제 쓸 수 있는 현금",
            "fcf_margin": "FCF/매출액",
            "debt_ratio": "부채총계/자본총계 (%)",
            "long_term_grade": "장투 적합도 힌트(A/B/C/관찰) — 단순 휴리스틱, 최종판단은 분석가",
        },
        "source_priority": "DART(공식) 우선, 없으면 yfinance",
        "dart_key_present": bool(key),
        "universe": len(universe),
        "by_source": by_source,
        "tickers": ordered,
        "notes": notes,
        "disclaimer": "공개데이터(DART/yfinance) 기반 추정치이며 연간 기준이다. 투자자문이 아니다.",
    }

    out_path, used_session = _resolve_output_path(args.out)
    try:
        _save_json_atomic(out_path, payload)
    except Exception as e:
        log.warning("[dart] 저장 실패(%s) → BASE_DIR 재시도", e)
        out_path = os.path.join(HERE, "fundamentals.json")
        _save_json_atomic(out_path, payload)
        used_session = False

    # 캐시 저장
    try:
        _save_json_atomic(CACHE_FILE, cache)
    except Exception:
        pass

    if not used_session:
        log.info("[dart] 오늘자 세션 폴더 없음 → %s 에 저장", out_path)
    log.info("[dart] 저장 완료: %s (dart=%d, yfinance=%d, none=%d)",
             out_path, by_source.get("dart", 0), by_source.get("yfinance", 0),
             by_source.get("none", 0))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        log.warning("[dart] 치명적 예외(무시하고 종료): %s: %s", type(e).__name__, e)
        sys.exit(0)
