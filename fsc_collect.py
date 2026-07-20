#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fsc_collect.py — 금융위원회(FSC) 주식시세정보 OpenAPI 수집기 (공식 종가/등락률/거래량)

[목적]
  공공데이터포털 '금융위원회_주식시세정보'(getStockPriceInfo) 로 watch_tickers 풀의
  일별 종가(clpr)·등락률(fltRt)·거래량(trqu)·시고저를 수집해 fsc_prices.json 으로 저장한다.
    - 아침 분석(PART A) : 추천/주의 종목을 'FSC 공식 시세'로 분석(메일 리포트의 가격 근거).
    - 회고(retro_label): 추천 이후 실제 등락률을 'FSC 종가'로 채점(get_close_series 재사용).

[API] https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo
  - 인증: serviceKey (fsc_api.txt, 공공데이터포털 '일반 인증키(Decoding)'). REST GET, resultType=json.
  - 갱신: 일 1회(전 거래일 종가까지). 초당 30 TPS.
  - 조회: likeSrtnCd(단축코드) + beginBasDt/endBasDt(YYYYMMDD) → 종목 일별 시세 목록.
  - 응답(item): basDt, srtnCd(6자리), itmsNm, mrktCtg, clpr(종가), vs(대비), fltRt(등락률%),
                mkp/hipr/lopr, trqu(거래량), trPrc(거래대금), lstgStCnt, mrktTotAmt.

[폴백] fsc_api.txt 키가 없거나 FSC 실패 시 FinanceDataReader 로 폴백 → 키 발급 전에도 동작.
       (source 필드로 'fsc'/'fdr'/'none' 출처를 기록 → 분석가가 신뢰도 판단.)

[설계] 독립 실행, 기존 파일 무수정, 종목별 try/except, ASCII 태그([fsc]), UTF-8,
       json ensure_ascii=False, 원자적 저장, 부분 실패해도 exit 0. 비밀키 출력 금지.

[사용법]
  python fsc_collect.py                  # 전체 → 오늘 세션(없으면 루트)/fsc_prices.json
  python fsc_collect.py --check          # 키/통신만 검증(샘플 1종목)
  python fsc_collect.py --tickers 005930 --days 40 --out x.json
"""
import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except Exception:
    requests = None

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("fsc")

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
TICKERS_FILE = os.path.join(HERE, "watch_tickers.txt")
KEY_FILE = os.path.join(HERE, "fsc_api.txt")

FSC_URL = ("https://apis.data.go.kr/1160100/service/"
           "GetStockSecuritiesInfoService/getStockPriceInfo")
MAX_WORKERS = 5          # 30 TPS 한도 내 보수적
REQ_TIMEOUT = 15
DEFAULT_DAYS = 40

# FDR 폴백
try:
    import FinanceDataReader as fdr
    FDR_OK = True
except Exception:
    fdr = None
    FDR_OK = False

# 종목별 일별시세 캐시 (retro_label 가 픽마다 호출해도 1회만 조회)
_HIST_CACHE = {}


# =====================================================================
# 설정/유니버스
# =====================================================================
def load_fsc_key() -> str:
    """fsc_api.txt 에서 serviceKey 를 읽는다. 'key=...'/'servicekey=...' 또는 키 한 줄.
    비밀값이므로 절대 로그/출력 금지."""
    if not os.path.isfile(KEY_FILE):
        return ""
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip().lower() in ("key", "servicekey", "service_key", "fsc_key", "apikey"):
                        return v.strip().strip('"').strip("'")
                    continue
                # '=' 없는 한 줄이면 그 자체를 키로 본다
                return line.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def load_universe():
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
                if code.isdigit() and len(code) == 6:
                    name = line.split("#", 1)[1].strip() if "#" in line else ""
                    out.append((code, name))
    except Exception as e:
        log.warning("[fsc] watch_tickers 읽기 실패: %s", e)
    # 최근 추천 종목(watch 풀 밖이어도) 합산 — recommend_track 가 만든 recommended_universe.txt
    try:
        import recommend_track
        have = {c for c, _ in out}
        for c, n in recommend_track.load_recommended_codes():
            if c not in have:
                out.append((c, n)); have.add(c)
    except Exception:
        pass
    return out


# =====================================================================
# FSC 호출
# =====================================================================
def _to_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


def _to_int(v):
    f = _to_float(v)
    return int(f) if f is not None else None


def _fsc_request(key, params, num_rows=100, retries=2) -> list:
    """FSC getStockPriceInfo 호출 → item 리스트. '정상 0건'은 [] 지만, 인증/한도/HTTP 오류는
    '경고 로그'를 남기고 [] 반환해 (조용한 폴백으로 은폐되지 않게) 구분한다. 429/5xx 는 짧게 재시도."""
    if requests is None or not key:
        return []
    p = {"serviceKey": key, "resultType": "json",
         "numOfRows": str(num_rows), "pageNo": "1"}
    p.update(params)
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(FSC_URL, params=p, timeout=REQ_TIMEOUT)
        except Exception as e:
            last = "req_exc:%s" % type(e).__name__
            time.sleep(0.4 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            last = "http_%d" % r.status_code
            time.sleep(0.6 * (attempt + 1))
            continue
        if r.status_code != 200:
            log.warning("[fsc] HTTP %d — 인증/요청 확인 필요", r.status_code)
            return []
        # 인증오류/한도초과는 data.go.kr 이 JSON 대신 XML(OpenAPI_ServiceResponse) 을 줄 수 있다.
        try:
            d = r.json()
        except Exception:
            body = (r.text or "").replace("\n", " ")
            if "cmmMsgHeader" in body or "OpenAPI_ServiceResponse" in body:
                log.warning("[fsc] 인증/서비스 오류 응답 — 키/승인상태/Decoding키 확인: %s", body[:140])
            else:
                log.warning("[fsc] 응답 JSON 파싱 실패")
            return []
        try:
            resp = d.get("response", {}) or {}
            hdr = resp.get("header", {}) or {}
            code = str(hdr.get("resultCode", "")).strip()
            if code and code != "00":
                log.warning("[fsc] resultCode=%s %s — 정상 아님(키/파라미터/한도 확인)",
                            code, hdr.get("resultMsg", ""))
                return []
            body = resp.get("body", {}) or {}
            items = body.get("items")
            if not items:
                return []   # 정상 0건
            item = items.get("item") if isinstance(items, dict) else None
            if item is None:
                return []
            return item if isinstance(item, list) else [item]
        except Exception:
            return []
    if last:
        log.warning("[fsc] 요청 실패(재시도 소진): %s", last)
    return []


def fetch_history_fsc(key, code, begin, end) -> list:
    """FSC 로 한 종목의 일별시세(begin~end, YYYYMMDD) 조회 → 정규화 dict 리스트(날짜 오름차순).
    likeSrtnCd 는 '포함' 검색이라 응답에서 srtnCd==code 만 취한다.
    [중요] FSC 는 최신순으로 주므로 numOfRows 가 기간 거래일수보다 작으면 '가장 오래된'(기준일) 행이
    잘린다 → 기간 캘린더일수에 맞춰 numOfRows 를 키운다(거래일수 <= 캘린더일수)."""
    try:
        span = (datetime.strptime(end, "%Y%m%d") - datetime.strptime(begin, "%Y%m%d")).days
    except Exception:
        span = 60
    num_rows = min(900, max(120, span + 20))
    raw = _fsc_request(key, {"likeSrtnCd": code, "beginBasDt": begin, "endBasDt": end},
                       num_rows=num_rows)
    rows = []
    code6 = str(code).zfill(6)
    for it in raw:
        if str(it.get("srtnCd", "")).strip().zfill(6) != code6:
            continue
        bd = str(it.get("basDt", "")).strip()
        if len(bd) != 8:
            continue
        rows.append({
            "date": "%s-%s-%s" % (bd[:4], bd[4:6], bd[6:]),
            "close": _to_float(it.get("clpr")),
            "change_pct": _to_float(it.get("fltRt")),
            "volume": _to_int(it.get("trqu")),
            "open": _to_float(it.get("mkp")), "high": _to_float(it.get("hipr")),
            "low": _to_float(it.get("lopr")),
            "mrkt": str(it.get("mrktCtg", "")).strip(),
            "source": "fsc",
        })
    rows = [r for r in rows if r["close"] is not None]
    rows.sort(key=lambda r: r["date"])
    return rows


def fetch_history_fdr(code, begin, end) -> list:
    """FDR 폴백: 일별 종가/거래량/등락률 → 같은 정규화 형식."""
    if not FDR_OK:
        return []
    try:
        start = "%s-%s-%s" % (begin[:4], begin[4:6], begin[6:])
        df = fdr.DataReader(code, start)
    except Exception:
        return []
    if df is None or getattr(df, "empty", True):
        return []
    rows = []
    try:
        for idx, r in df.iterrows():
            d = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
            close = r.get("Close")
            chg = r.get("Change")
            rows.append({
                "date": d, "close": _to_float(close),
                "change_pct": (round(_to_float(chg) * 100, 2)
                               if _to_float(chg) is not None else None),
                "volume": _to_int(r.get("Volume")),
                "open": _to_float(r.get("Open")), "high": _to_float(r.get("High")),
                "low": _to_float(r.get("Low")), "mrkt": "", "source": "fdr",
            })
    except Exception:
        return []
    rows = [x for x in rows if x["close"] is not None]
    rows.sort(key=lambda x: x["date"])
    return rows


def get_history(code, days=DEFAULT_DAYS, key=None) -> list:
    """한 종목의 최근 days 일 일별시세(FSC 우선, 실패 시 FDR). 캐시 사용.
    반환: [{date,'YYYY-MM-DD', close, change_pct, volume, open/high/low, mrkt, source}] 오름차순."""
    ckey = (code, int(days))
    if ckey in _HIST_CACHE:
        return _HIST_CACHE[ckey]
    if key is None:
        key = load_fsc_key()
    # endBasDt 는 '미만(<)' 경계라 +1일로 잡아 직전 거래일 종가까지 포함시킨다.
    end = (datetime.now() + timedelta(days=1)).strftime("%Y%m%d")
    begin = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    rows = fetch_history_fsc(key, code, begin, end) if key else []
    # #A12(회고 15회차 최우선 지적): FSC 가 '성공하되 며칠 뒤처진' 데이터를 줄 수 있다
    # (실측 2026-07-20: FSC 마지막 07-15 vs 실제 최근 거래일 07-16 — 4개 드롭 연속 만기 동결,
    #  07-16 폭락일 -6.37% 누락으로 진행중 부분수익이 전량 낙관 편향).
    # FSC 성공이 FDR 폴백을 가리는 구조가 원인 → 마지막 날짜가 3일+ 낡았으면 FDR 로 꼬리 보강.
    if rows:
        try:
            _gap = (datetime.now().date()
                    - datetime.strptime(rows[-1]["date"], "%Y-%m-%d").date()).days
            if _gap >= 3:
                fdr_rows = fetch_history_fdr(code, begin, end)
                if fdr_rows and fdr_rows[-1]["date"] > rows[-1]["date"]:
                    _last = rows[-1]["date"]
                    rows = rows + [r for r in fdr_rows if r["date"] > _last]
        except Exception:
            pass
    if not rows:
        rows = fetch_history_fdr(code, begin, end)
    _HIST_CACHE[ckey] = rows
    return rows


def get_close_series(code, start_date, key=None):
    """retro_label 용: start_date(date) 이후를 충분히 덮는 (date_obj, close) 리스트(오름차순).
    FSC 우선, FDR 폴백. 실패 시 []."""
    return [(d, c) for (d, c, _v) in get_ohlcv_series(code, start_date, key)]


def get_ohlcv_series(code, start_date, key=None):
    """retro_label 용: start_date(date) 이후를 충분히 덮는 (date_obj, close, volume) 리스트(오름차순).
    종가(상승률 계산)와 거래량(차익실현·큰손 매도 신호)을 함께 준다. FSC 우선, FDR 폴백. 실패 시 []."""
    from datetime import date as _date
    days = max(DEFAULT_DAYS, (datetime.now().date() - start_date).days + 10) if isinstance(start_date, _date) else DEFAULT_DAYS
    rows = get_history(code, days=days, key=key)
    out = []
    for r in rows:
        try:
            y, m, d = r["date"].split("-")
            vol = r.get("volume")
            out.append((datetime(int(y), int(m), int(d)).date(), float(r["close"]),
                        (int(vol) if vol is not None else None)))
        except Exception:
            continue
    return out


# =====================================================================
# 수집(유니버스) → fsc_prices.json
# =====================================================================
def _compute_one(code, name, days, key):
    try:
        rows = get_history(code, days=days, key=key)
        if not rows:
            return {"ticker": code, "name": name, "source": "none",
                    "notes": ["시세 없음(FSC/FDR 모두 실패)"]}
        last = rows[-1]
        return {
            "ticker": code, "name": name, "source": last.get("source", "none"),
            "asof": last["date"], "close": last["close"],
            "change_pct": last.get("change_pct"), "volume": last.get("volume"),
            "open": last.get("open"), "high": last.get("high"), "low": last.get("low"),
            "mrkt": last.get("mrkt", ""),
            "series": rows[-10:],   # 최근 10거래일
        }
    except Exception as e:
        return {"ticker": code, "name": name, "source": "none",
                "notes": ["%s: %s" % (type(e).__name__, e)]}


from common import save_json_atomic as _save_json_atomic  # 원자적 JSON 저장(common.py 통합)


def _today_latest_session():
    """H-3: common.resolve_session 위임 — 자정 경계 완화(6h 폴백) + 11곳 복제 제거."""
    from common import resolve_session
    return resolve_session(OUTPUT_DIR)


def main():
    ap = argparse.ArgumentParser(description="금융위(FSC) 주식시세정보 → fsc_prices.json")
    ap.add_argument("--tickers", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--out", default="")
    ap.add_argument("--check", action="store_true", help="키/통신 검증(샘플 1종목)")
    args = ap.parse_args()

    key = load_fsc_key()
    key_present = bool(key)
    log.info("[fsc] FSC 키 %s / FDR 폴백 %s",
             "있음" if key_present else "없음(→FDR 폴백)", "가능" if FDR_OK else "불가")

    if args.check:
        sample = "005930"
        rows = fetch_history_fsc(key, sample, (datetime.now() - timedelta(days=15)).strftime("%Y%m%d"),
                                 datetime.now().strftime("%Y%m%d")) if key else []
        if rows:
            log.info("[fsc] --check: FSC 정상 (%s 최근 종가 %s, 등락률 %s%%)",
                     sample, rows[-1]["close"], rows[-1]["change_pct"])
        elif key:
            log.info("[fsc] --check: FSC 응답 없음 — 키/승인상태/Decoding키 여부 확인 필요(FDR 폴백 사용됨)")
        else:
            fb = fetch_history_fdr(sample, (datetime.now() - timedelta(days=15)).strftime("%Y%m%d"),
                                   datetime.now().strftime("%Y%m%d"))
            log.info("[fsc] --check: FSC 키 없음 → FDR 폴백 %s",
                     ("정상 (%s 종가 %s)" % (sample, fb[-1]["close"]) if fb else "도 실패"))
        return 0

    universe = load_universe()
    if args.tickers:
        want = [t.strip() for t in args.tickers.split(",") if t.strip()]
        nm = {c: n for c, n in universe}
        universe = [(c, nm.get(c, "")) for c in want]
    if args.limit > 0:
        universe = universe[:args.limit]

    log.info("[fsc] 시작 — 종목 %d개 (days=%d)", len(universe), args.days)
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(_compute_one, c, n, args.days, key): c for c, n in universe}
        done = 0
        for fut in as_completed(futs):
            done += 1
            r = fut.result()
            results[r["ticker"]] = r
            if done % 10 == 0:
                log.info("[fsc] %d/%d", done, len(universe))
            time.sleep(0.03)   # rate-limit 여유

    ordered = [results[c] for c, _ in universe if c in results]
    by_src = {"fsc": 0, "fdr": 0, "none": 0}
    for r in ordered:
        by_src[r.get("source", "none")] = by_src.get(r.get("source", "none"), 0) + 1
    asof = next((r.get("asof") for r in ordered if r.get("asof")), None)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "what": "금융위원회(FSC) 주식시세정보 — 일별 공식 종가(clpr)/등락률(fltRt)/거래량(trqu). 일1회 갱신.",
        "key_present": key_present,
        "asof_date": asof,
        "by_source": by_src,
        "universe": len(universe),
        "field_guide": {"close": "종가(원)", "change_pct": "전일대비 등락률(%)",
                        "volume": "거래량", "series": "최근 10거래일"},
        "tickers": ordered,
        "disclaimer": "금융위원회 공공데이터(FSC) 공식 시세. 투자자문이 아니다.",
    }

    out_path = os.path.abspath(args.out) if args.out else None
    if not out_path:
        sess = _today_latest_session()
        out_path = os.path.join(sess, "fsc_prices.json") if sess else os.path.join(HERE, "fsc_prices.json")
    try:
        _save_json_atomic(out_path, payload)
        log.info("[fsc] 저장 완료: %s (출처 fsc=%d fdr=%d none=%d, asof=%s)",
                 out_path, by_src.get("fsc", 0), by_src.get("fdr", 0), by_src.get("none", 0), asof)
    except Exception as e:
        log.warning("[fsc] 저장 실패: %s", e)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        log.warning("[fsc] 치명적 예외(무시): %s: %s", type(e).__name__, e)
        sys.exit(0)
