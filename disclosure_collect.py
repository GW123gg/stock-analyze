#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
disclosure_collect.py — DART 공시 기반 '물량 오버행' 수집기 (C)

[목적]
  차익실현 매물 중 '예측 가능한' 부분 = 공시로 정해진 물량 이벤트(증자·전환사채·자사주처분·
  대주주 변동 등). 이 모듈은 watch_tickers 풀에 대해 OpenDART 공시목록을 훑어 '오버행 신호'를
  추려 오늘자 세션 폴더에 disclosures.json 으로 저장한다. 분석(Cowork)이 되돌림/차익실현
  판단([3])에 활용한다.

[필요]  dart_api.txt 의 OpenDART 키(=dart_collect 와 동일 키)가 있어야 동작.
        키가 없으면 조용히 빈 산출물(dart_key_present=false)만 남기고 종료한다.
        (dart_collect 의 load_dart_key / load_corp_map 를 그대로 재사용 — 중복 없음.)

[오버행 분류] (공시명 report_nm 키워드)
  - 증자(희석)        : 유상증자/무상증자/증자
  - 메자닌(전환물량)  : 전환사채/신주인수권부사채/교환사채(CB·BW)
  - 전환청구(출회)    : 전환청구권행사/전환가액조정
  - 자사주 처분(매물) : 자기주식 처분
  - 자사주 취득(흡수+): 자기주식 취득/신탁  (← 매물 흡수, 긍정 신호로 표시)
  - 대주주/대량보유   : 최대주주변경/주식등의대량보유/임원ㆍ주요주주 특정증권
  - 감자/기타         : 감자 등
  → overhang_score(0~100, 자사주 취득은 감점) + flags + 최근 공시 목록

[설계 원칙]  독립 실행, 기존 파일 무수정, 종목별 try/except, ASCII 콘솔 태그([disc]),
  UTF-8 IO, json ensure_ascii=False, 원자적 저장, 부분 실패해도 exit 0.

[사용법]
  python disclosure_collect.py                     # 전체 → 오늘 세션/disclosures.json
  python disclosure_collect.py --tickers 005930 --days 120 --out x.json
"""
import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
TICKERS_FILE = os.path.join(HERE, "watch_tickers.txt")

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("disc")

DART_LIST = "https://opendart.fss.or.kr/api/list.json"
HTTP_TIMEOUT = 20
MAX_WORKERS = 5
DEFAULT_DAYS = 120

try:
    import requests
except Exception:
    requests = None

# dart_collect 재사용(키/corp매핑/종목풀). 실패해도 자체 폴백.
try:
    import dart_collect as dc
except Exception:
    dc = None

# (키워드, 분류, 가중치) — 가중치 음수=긍정(자사주 취득=매물 흡수)
_RULES = [
    ("유상증자", "증자(희석)", 25), ("무상증자", "증자(희석)", 8), ("증자", "증자(희석)", 15),
    ("전환사채", "메자닌(전환물량)", 18), ("신주인수권부사채", "메자닌(전환물량)", 18),
    ("교환사채", "메자닌(전환물량)", 15),
    ("전환청구권행사", "전환청구(출회)", 15), ("전환가액", "전환청구(출회)", 8),
    ("자기주식처분", "자사주 처분(매물)", 20), ("자기주식 처분", "자사주 처분(매물)", 20),
    ("자기주식취득", "자사주 취득(흡수)", -12), ("자기주식 취득", "자사주 취득(흡수)", -12),
    ("자기주식 신탁", "자사주 취득(흡수)", -8),
    ("최대주주변경", "대주주/대량보유", 18), ("최대주주 변경", "대주주/대량보유", 18),
    ("주식등의대량보유", "대주주/대량보유", 6), ("대량보유상황보고", "대주주/대량보유", 6),
    ("특정증권등소유", "대주주/대량보유", 4),
    ("감자", "감자/기타", 12),
]


def _norm(s):
    return (s or "").replace(" ", "")


def classify(report_nm):
    """공시명 → (분류, 가중치) 또는 None."""
    nm = _norm(report_nm)
    for kw, cat, w in _RULES:
        if _norm(kw) in nm:
            return cat, w
    return None


def load_universe():
    if dc is not None:
        try:
            return dc.load_universe()
        except Exception:
            pass
    out = []
    if not os.path.isfile(TICKERS_FILE):
        return out
    with open(TICKERS_FILE, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            code = line.split("#")[0].strip()
            if code.isdigit() and len(code) == 6:
                name = line.split("#", 1)[1].strip() if "#" in line else ""
                out.append((code, name))
    return out


def fetch_disclosures(key, corp, code, name, days):
    """corp_code 기준 최근 days 공시 → 오버행 추출."""
    if not (requests and key and corp):
        return None
    end = datetime.now().strftime("%Y%m%d")
    bgn = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    try:
        r = requests.get(DART_LIST, params={"crtfc_key": key, "corp_code": corp,
                                            "bgn_de": bgn, "end_de": end,
                                            "page_count": 100}, timeout=HTTP_TIMEOUT)
        j = r.json()
        if j.get("status") not in ("000", "013"):   # 013 = 조회데이터 없음
            return {"_status": j.get("status"), "_msg": j.get("message", "")[:80]}
        items = j.get("list", []) if j.get("status") == "000" else []
    except Exception as e:
        return {"_error": "%s: %s" % (type(e).__name__, e)}

    flags = {}
    recent = []
    cat_weight = {}
    for it in items:
        cl = classify(it.get("report_nm"))
        if not cl:
            continue
        cat, w = cl
        flags[cat] = flags.get(cat, 0) + 1
        # 카테고리별 '가장 심한' 가중치만 1회 반영(같은 종류 반복 공시로 점수 과포화 방지).
        # 자사주 취득(흡수, 음수)은 그대로 감점(긍정)으로 합산된다.
        if cat not in cat_weight or abs(w) > abs(cat_weight[cat]):
            cat_weight[cat] = w
        if len(recent) < 12:
            recent.append({"date": it.get("rcept_dt"), "title": it.get("report_nm"),
                           "category": cat})
    score = max(0, min(100, sum(cat_weight.values())))
    return {"overhang_score": score,
            "overhang_flags": [{"category": k, "count": v} for k, v in flags.items()],
            "recent": recent, "checked": len(items)}


from common import save_json_atomic as _save_json_atomic  # 원자적 JSON 저장(common.py 통합)


def _today_latest_session():
    """H-3: common.resolve_session 위임 — 자정 경계 완화(6h 폴백) + 11곳 복제 제거."""
    from common import resolve_session
    return resolve_session(OUTPUT_DIR)


def _resolve_out(explicit):
    if explicit:
        return os.path.abspath(explicit)
    sess = _today_latest_session()
    return os.path.join(sess, "disclosures.json") if sess else os.path.join(HERE, "disclosures.json")


def main():
    ap = argparse.ArgumentParser(description="DART 공시 오버행 → disclosures.json (키 없으면 무동작)")
    ap.add_argument("--tickers", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    key = dc.load_dart_key() if dc else ""
    if not key:
        log.info("[disc] OpenDART 키 없음(dart_api.txt) → 공시 오버행 수집 생략. "
                 "키를 넣으면 다음 실행부터 자동 동작.")
        payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
                   "dart_key_present": False, "tickers": [],
                   "notes": ["dart_api.txt 키 미설정 — DART 공시 오버행 생략"]}
        try:
            _save_json_atomic(_resolve_out(args.out), payload)
        except Exception:
            pass
        return 0
    if requests is None:
        log.warning("[disc] requests 미설치 — 수집 불가")
        return 0

    corp_map = dc.load_corp_map(key)
    universe = load_universe()
    if args.tickers:
        want = [t.strip() for t in args.tickers.split(",") if t.strip()]
        nm = {c: n for c, n in universe}
        universe = [(c, nm.get(c, "")) for c in want]
    if args.limit > 0:
        universe = universe[:args.limit]

    log.info("[disc] 시작 — 종목 %d개 / 최근 %d일 공시", len(universe), args.days)
    results = {}

    def work(code, name):
        corp = corp_map.get(code)
        rec = {"ticker": code, "name": name}
        d = fetch_disclosures(key, corp, code, name, args.days)
        if isinstance(d, dict) and "overhang_score" in d:
            rec.update(d)
        else:
            rec.update({"overhang_score": None, "overhang_flags": [], "recent": [],
                        "notes": [str(d)[:120] if d else "corp_code 없음/조회 실패"]})
        time.sleep(0.05)
        return rec

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(work, c, n): c for c, n in universe}
        done = 0
        for fut in as_completed(futs):
            done += 1
            r = fut.result()
            results[r["ticker"]] = r
            sc = r.get("overhang_score")
            if sc:
                log.info("[disc] (%d/%d) %s overhang=%s %s", done, len(universe),
                         r["ticker"], sc, [f["category"] for f in r.get("overhang_flags", [])])

    ordered = [results[c] for c, _ in universe if c in results]
    hot = sorted([r for r in ordered if r.get("overhang_score")],
                 key=lambda r: r["overhang_score"], reverse=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dart_key_present": True, "lookback_days": args.days, "universe": len(universe),
        "what": "DART 공시 기반 물량 오버행(증자/CB/자사주/대주주변동). 차익실현·되돌림 리스크의 '예측 가능' 부분.",
        "score_guide": "overhang_score 높을수록 향후 매물 출회 위험 큼(자사주 취득은 감점=긍정).",
        "top_overhang": [{"ticker": r["ticker"], "name": r["name"], "score": r["overhang_score"],
                          "flags": [f["category"] for f in r.get("overhang_flags", [])]}
                         for r in hot[:10]],
        "tickers": ordered,
        "disclaimer": "OpenDART 공시 기반. 투자자문이 아니다.",
    }
    try:
        out_path = _resolve_out(args.out)
        _save_json_atomic(out_path, payload)
        log.info("[disc] 저장 완료: %s (오버행 감지 %d종목)", out_path, len(hot))
    except Exception as e:
        log.warning("[disc] 저장 실패: %s", e)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        log.warning("[disc] 치명적 예외(무시): %s: %s", type(e).__name__, e)
        sys.exit(0)
