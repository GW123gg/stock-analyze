#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kis_collect.py — 한국투자증권(KIS) Open API 수급/시세 수집기 (프레임워크)

[현재 상태]  KIS 키가 있으면 KIS 로 수급+시세를 수집한다. 키가 없거나 토큰 발급에 실패하면
            'yfinance 폴백'으로 시세(현재가·52주·PER/PBR·거래량)를 모은다. (외국인/기관/개인
            '수급'은 KIS/KRX 전용이라 yfinance 로는 못 받는다 → 수급은 force_scores.json 참조.)
            나중에 키를 kis_api.txt 에 붙여넣으면(메모장) '바로' KIS 로 전환된다(재시작 불필요).

[목적]  watch_tickers 풀에 대해 KIS REST API 로 다음을 수집해 오늘자 세션 폴더에
        kis_data.json 으로 저장한다. 분석(Cowork)이 수급/되돌림 판단에 활용한다.
  - 수급(종목별 투자자 매매동향): 외국인/기관/개인 '일별 순매수' 추이
      → endpoint /uapi/domestic-stock/v1/quotations/inquire-investor  (tr_id FHKST01010900)
  - 시세(현재가): 종가/등락률/거래량 + 외국인 보유율(hts_frgn_ehrt)
      → endpoint /uapi/domestic-stock/v1/quotations/inquire-price      (tr_id FHKST01010100)
  ※ '여러 자료'로 쉽게 확장 가능하도록 ENDPOINTS 표 + _req() 헬퍼로 구조화했다(아래 [확장]).

[인증]  POST /oauth2/tokenP {grant_type:client_credentials, appkey, appsecret} → access_token
        토큰은 약 24시간 유효 + 발급 rate-limit 이 있어 cache/kis_token.json 에 캐시·재사용한다.

[설계 원칙]  dart_collect/market_collect 와 동일: 독립 실행, 기존 파일 무수정, 종목별 try/except,
  콘솔 print ASCII 태그([kis])만(이모지 금지), UTF-8 IO, json ensure_ascii=False, 원자적 저장.
  부분 실패해도 exit 0(파이프라인 무중단). 키 없으면 즉시·정상 종료.

[사용법]
  python kis_collect.py                     # watch_tickers 전체 → 오늘 세션/kis_data.json
  python kis_collect.py --tickers 005930    # 특정 종목(테스트)
  python kis_collect.py --limit 5 --out x.json
  python kis_collect.py --check             # 키/토큰 발급만 점검(데이터 수집 안 함)

[확장 — '여러 자료' 추가 방법]
  ENDPOINTS 에 (path, tr_id) 를 추가하고, 그 응답을 파싱하는 fetch_* 함수를 만들어
  process_one() 에서 호출하면 된다. (예: 호가, 일별시세, 외국인 추정집계, 잔고 등)
"""
import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
CACHE_DIR = os.path.join(HERE, "cache")
TICKERS_FILE = os.path.join(HERE, "watch_tickers.txt")
KIS_KEY_FILE = os.path.join(HERE, "kis_api.txt")
TOKEN_CACHE = os.path.join(CACHE_DIR, "kis_token.json")

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("kis")

HTTP_TIMEOUT = 15
MAX_WORKERS = 4          # KIS 호출 rate-limit(초당 제한) 고려해 보수적으로
INVESTOR_DAYS = 10       # 투자자 매매동향 최근 N일만 보관

DOMAIN = {"real": "https://openapi.koreainvestment.com:9443",
          "mock": "https://openapivts.koreainvestment.com:29443"}

# (path, tr_id) — '여러 자료' 확장 시 여기에 추가
ENDPOINTS = {
    "price":    ("/uapi/domestic-stock/v1/quotations/inquire-price",    "FHKST01010100"),
    "investor": ("/uapi/domestic-stock/v1/quotations/inquire-investor", "FHKST01010900"),
}

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
# 설정 / 종목풀
# =====================================================================
def load_config() -> dict:
    """kis_api.txt → {mode, appkey, appsecret, account}. 키 없으면 appkey=''. """
    cfg = {"mode": "real", "appkey": "", "appsecret": "", "account": ""}
    if not os.path.isfile(KIS_KEY_FILE):
        return cfg
    try:
        with open(KIS_KEY_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip().lower()
                v = v.strip().strip('"').strip("'")
                if k in cfg:
                    cfg[k] = v
    except Exception as e:
        log.warning("[kis] 설정 읽기 실패: %s", e)
    if cfg["mode"] not in DOMAIN:
        cfg["mode"] = "real"
    return cfg


def has_key(cfg) -> bool:
    return bool(cfg.get("appkey") and cfg.get("appsecret"))


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
        log.warning("[kis] watch_tickers 읽기 실패: %s", e)
    return out


# =====================================================================
# 토큰 (발급 + 캐시 재사용)
# =====================================================================
from common import save_json_atomic as _save_json_atomic  # 원자적 JSON 저장(common.py 통합)


def get_token(cfg) -> str:
    """access_token 반환(없으면 ''). 캐시가 유효하면 재사용(KIS 발급 rate-limit 회피)."""
    if not (requests and has_key(cfg)):
        return ""
    # 1) 캐시
    try:
        if os.path.isfile(TOKEN_CACHE):
            with open(TOKEN_CACHE, encoding="utf-8") as f:
                c = json.load(f)
            if (c.get("mode") == cfg["mode"] and c.get("appkey_tail") == cfg["appkey"][-6:]
                    and c.get("token") and float(c.get("exp", 0)) - time.time() > 600):
                return c["token"]
    except Exception:
        pass
    # 2) 신규 발급
    try:
        url = DOMAIN[cfg["mode"]] + "/oauth2/tokenP"
        r = requests.post(url, json={"grant_type": "client_credentials",
                                     "appkey": cfg["appkey"], "appsecret": cfg["appsecret"]},
                          headers={"content-type": "application/json"}, timeout=HTTP_TIMEOUT)
        j = r.json()
        tok = j.get("access_token")
        if not tok:
            log.warning("[kis] 토큰 발급 실패: %s", str(j)[:200])
            return ""
        exp = time.time() + int(j.get("expires_in", 86400))
        try:
            _save_json_atomic(TOKEN_CACHE, {"mode": cfg["mode"], "appkey_tail": cfg["appkey"][-6:],
                                            "token": tok, "exp": exp})
        except Exception:
            pass
        return tok
    except Exception as e:
        log.warning("[kis] 토큰 발급 예외: %s: %s", type(e).__name__, e)
        return ""


def _req(cfg, token, which, params) -> dict:
    """ENDPOINTS[which] 를 GET 호출 → JSON dict(실패 시 {})."""
    if not (requests and token):
        return {}
    path, tr_id = ENDPOINTS[which]
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": "Bearer " + token,
        "appkey": cfg["appkey"], "appsecret": cfg["appsecret"],
        "tr_id": tr_id, "custtype": "P",
    }
    try:
        r = requests.get(DOMAIN[cfg["mode"]] + path, headers=headers,
                         params=params, timeout=HTTP_TIMEOUT)
        return r.json()
    except Exception as e:
        return {"_error": "%s: %s" % (type(e).__name__, e)}


def _f(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


# =====================================================================
# 수집 (시세 + 수급)
# =====================================================================
def fetch_price(cfg, token, code) -> dict:
    j = _req(cfg, token, "price",
             {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
    o = j.get("output") if isinstance(j, dict) else None
    if not isinstance(o, dict):
        return {}
    return {
        "price": _f(o.get("stck_prpr")),            # 현재가
        "chg_pct": _f(o.get("prdy_ctrt")),          # 등락률
        "volume": _f(o.get("acml_vol")),            # 누적거래량
        "foreign_hold_pct": _f(o.get("hts_frgn_ehrt")),   # 외국인 보유율
        "per": _f(o.get("per")), "pbr": _f(o.get("pbr")),
        "w52_high": _f(o.get("w52_hgpr")), "w52_low": _f(o.get("w52_lwpr")),
    }


def fetch_investor(cfg, token, code) -> dict:
    """외국인/기관/개인 일별 순매수(최근 N일). 합계 + 최근 추이."""
    j = _req(cfg, token, "investor",
             {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
    rows = j.get("output") if isinstance(j, dict) else None
    if not isinstance(rows, list) or not rows:
        return {}
    recent = []
    fsum = osum = psum = 0.0
    for it in rows[:INVESTOR_DAYS]:
        frg = _f(it.get("frgn_ntby_qty"))     # 외국인 순매수 수량
        org = _f(it.get("orgn_ntby_qty"))     # 기관계 순매수 수량
        prs = _f(it.get("prsn_ntby_qty"))     # 개인 순매수 수량
        recent.append({"date": it.get("stck_bsop_date"),
                       "foreign": frg, "inst": org, "indiv": prs})
        fsum += frg or 0; osum += org or 0; psum += prs or 0
    return {
        "foreign_net_sum": round(fsum), "inst_net_sum": round(osum),
        "indiv_net_sum": round(psum), "days": len(recent), "recent": recent,
    }


def fetch_yf(code) -> dict:
    """yfinance 폴백 시세: 현재가/52주/거래량/PER/PBR. .KS 먼저, 비면 .KQ.
    외국인 보유율·투자자별 수급은 yfinance 미제공(None) → 수급은 force_scores.json 참조."""
    if not YF_OK:
        return {}
    for suf in (".KS", ".KQ"):
        try:
            t = yf.Ticker(code + suf)
            fi = getattr(t, "fast_info", None)
            price = hi = lo = vol = None
            if fi is not None:
                price = _f(getattr(fi, "last_price", None))
                hi = _f(getattr(fi, "year_high", None))
                lo = _f(getattr(fi, "year_low", None))
                vol = _f(getattr(fi, "last_volume", None))
            if price is None:
                continue
            # PER/PBR 은 yfinance .info 가 한국종목엔 느리고 대개 None 이라 호출하지 않는다
            # (밸류는 force_scores/웹검색으로 보강). 폴백은 fast_info 만으로 빠르게 처리.
            return {"price": price, "chg_pct": None, "volume": vol,
                    "foreign_hold_pct": None, "per": None, "pbr": None,
                    "w52_high": hi, "w52_low": lo, "yf_symbol": code + suf}
        except Exception:
            continue
    return {}


def process_one(cfg, token, code, name, use_kis) -> dict:
    rec = {"ticker": code, "name": name}
    if use_kis:
        rec["source"] = "kis"
        try:
            rec["price"] = fetch_price(cfg, token, code)
        except Exception as e:
            rec["price"] = {"_error": str(e)}
        try:
            rec["investor"] = fetch_investor(cfg, token, code)
        except Exception as e:
            rec["investor"] = {"_error": str(e)}
        time.sleep(0.06)   # rate-limit 완화
    else:
        rec["source"] = "yfinance"
        rec["price"] = fetch_yf(code)
        rec["investor"] = {}     # yfinance 미제공(수급은 force_scores.json)
    return rec


# =====================================================================
# 저장 위치
# =====================================================================
def _today_latest_session():
    today = datetime.now().strftime("%Y-%m-%d")
    if not os.path.isdir(OUTPUT_DIR):
        return None
    cands = []
    for nm in os.listdir(OUTPUT_DIR):
        if nm.startswith("_") or nm == "__pycache__" or not nm.startswith(today):
            continue
        p = os.path.join(OUTPUT_DIR, nm)
        if os.path.isdir(p):
            cands.append((os.path.getmtime(p), p))
    if not cands:
        return None
    cands.sort(reverse=True)
    return cands[0][1]


def _resolve_out(explicit):
    if explicit:
        return os.path.abspath(explicit)
    sess = _today_latest_session()
    return os.path.join(sess, "kis_data.json") if sess else os.path.join(HERE, "kis_data.json")


# =====================================================================
# 메인
# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="KIS 수급/시세 수집 → kis_data.json (키 없으면 무동작)")
    ap.add_argument("--tickers", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    ap.add_argument("--check", action="store_true", help="키/토큰 발급만 점검")
    args = ap.parse_args()

    cfg = load_config()
    token, use_kis = "", False
    if has_key(cfg) and requests is not None:
        token = get_token(cfg)
        if token:
            use_kis = True
            log.info("[kis] 토큰 OK (mode=%s) → KIS 수집", cfg["mode"])
        else:
            log.warning("[kis] 키는 있으나 토큰 발급 실패 → yfinance 폴백(시세)")
    else:
        log.info("[kis] KIS 키 없음 → yfinance 폴백(시세). "
                 "외국인/기관/개인 '수급'은 KIS/KRX 전용이라 폴백 불가 → force_scores.json 참조.")

    if not use_kis and not YF_OK:
        log.warning("[kis] KIS 미사용 + yfinance 미설치 — 수집 불가. 빈 산출물 저장.")
        try:
            _save_json_atomic(_resolve_out(args.out),
                              {"generated_at": datetime.now().isoformat(timespec="seconds"),
                               "key_present": has_key(cfg), "source": "none", "tickers": [],
                               "notes": ["KIS 키 없음 + yfinance 미설치"]})
        except Exception:
            pass
        return 0
    if args.check:
        log.info("[kis] --check: %s", "KIS 인증 OK" if use_kis else "KIS 미사용 → yfinance 폴백 가능")
        return 0

    universe = load_universe()
    if args.tickers:
        want = [t.strip() for t in args.tickers.split(",") if t.strip()]
        nm = {c: n for c, n in universe}
        universe = [(c, nm.get(c, "")) for c in want]
    if args.limit > 0:
        universe = universe[:args.limit]

    log.info("[kis] 수집 시작 — 종목 %d개 / 소스: %s", len(universe), "KIS" if use_kis else "yfinance")
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(process_one, cfg, token, c, n, use_kis): c for c, n in universe}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                r = fut.result()
                results[r["ticker"]] = r
            except Exception as e:
                log.warning("[kis] 처리 예외: %s", e)
            if done % 10 == 0:
                log.info("[kis] %d/%d", done, len(universe))

    ordered = [results[c] for c, _ in universe if c in results]
    src = "kis" if use_kis else "yfinance"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "key_present": has_key(cfg), "source": src, "mode": cfg["mode"],
        "universe": len(universe),
        "what": ("KIS 수급(외국인/기관/개인 일별 순매수) + 시세" if use_kis
                 else "yfinance 폴백 시세(현재가·52주·PER/PBR·거래량). 수급은 미제공 → force_scores.json 참조."),
        "supply_note": "외국인/기관/개인 일별 수급은 KIS/KRX 전용. yfinance 폴백 시 investor 는 비며, 수급은 force_scores.json 으로 본다.",
        "tickers": ordered,
        "disclaimer": "KIS Open API / yfinance 데이터. 투자자문이 아니다.",
    }
    try:
        out_path = _resolve_out(args.out)
        _save_json_atomic(out_path, payload)
        log.info("[kis] 저장 완료: %s", out_path)
    except Exception as e:
        log.warning("[kis] 저장 실패: %s", e)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        log.warning("[kis] 치명적 예외(무시): %s: %s", type(e).__name__, e)
        sys.exit(0)
