# -*- coding: utf-8 -*-
r"""
gdelt_collect.py — GDELT 2.0 DOC API 글로벌 뉴스 수집 (무료·키 불필요)

[목적] 전 세계 매체가 한국 종목/이슈를 어떻게 다루는지(외신 포함) + 톤/이벤트를 무료로 수집.
  구글뉴스 RSS(국문)·네이버(국내)를 보완하는 '글로벌 시각'. Reuters/Bloomberg 등 외신 커버리지.

[키] GDELT DOC 2.0 API 는 키 없이 동작한다. gdelt_api.txt 가 있으면 로드하지만 DOC 엔 미사용.
[레이트리밋] GDELT 는 1요청/5초 제한. 수집기가 자동으로 간격(>=5.2초)을 두고, 429 면 백오프 재시도.
[설계] requests, 표준 라이브러리. 쿼리별 try/except, 링크 중복제거, 원자적 저장, ASCII 태그([gdelt]),
  한글 OK·이모지 금지, UTF-8 ensure_ascii=False. 비밀키 미취급.

[사용법]
  python gdelt_collect.py --queries "Samsung Electronics,SK Hynix,Hanwha Aerospace" --n 15 --timespan 3d
  python gdelt_collect.py --queries "Samsung" --lang korean    # 한국어 소스만(sourcelang:korean)
"""
import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import requests
except Exception:
    requests = None

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("gdelt")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DEFAULT = os.path.join(HERE, "gdelt_news.json")
KEY_FILE = os.path.join(HERE, "gdelt_api.txt")
DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
MIN_INTERVAL = 5.3          # GDELT 1req/5초 → 안전 간격
_LAST_REQ = [0.0]


def load_gdelt_key():
    """gdelt_api.txt 의 키(선택). DOC API 엔 불필요하지만 향후 대비 로드. 없으면 ''."""
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                tok = line.split("#")[0].strip()
                if tok and tok != "PASTE_YOUR_GDELT_KEY_HERE":
                    return tok
    except Exception:
        pass
    return ""


def _throttle():
    """직전 요청과 >=MIN_INTERVAL 초 간격 보장(스크립트 내부 sleep)."""
    wait = MIN_INTERVAL - (time.monotonic() - _LAST_REQ[0])
    if wait > 0:
        time.sleep(wait)
    _LAST_REQ[0] = time.monotonic()


def fetch_gdelt(query, n=15, timespan="3d", lang=None, retries=2):
    """쿼리 1개 → 기사 [{title, domain, language, country, date, link}]. 실패 시 []."""
    if requests is None:
        return []
    # 짧은 토큰(예: 'SK')은 GDELT 가 'keyword too short' 로 거부 → 따옴표 구문검색으로 감싼다.
    qbase = query.strip()
    if '"' not in qbase and any(len(t) < 3 for t in qbase.split()):
        qbase = '"%s"' % qbase
    q = qbase if not lang else "%s sourcelang:%s" % (qbase, lang)
    params = {"query": q, "mode": "ArtList", "format": "json",
              "maxrecords": str(max(1, min(250, n))), "timespan": timespan, "sort": "datedesc"}
    for attempt in range(retries + 1):
        _throttle()
        try:
            r = requests.get(DOC_URL, params=params, headers={"User-Agent": UA}, timeout=40)
        except Exception as e:
            log.warning("[gdelt] %s 요청 실패: %s", query, type(e).__name__)
            return []
        if r.status_code == 429:
            log.info("[gdelt] 429 레이트리밋 — %ds 후 재시도(%d/%d)", 6, attempt + 1, retries)
            time.sleep(6.0)
            continue
        if r.status_code != 200:
            log.warning("[gdelt] %s HTTP %d", query, r.status_code)
            return []
        ct = r.headers.get("content-type", "")
        if "json" not in ct and not r.text.strip().startswith("{"):
            # 가끔 평문 에러/빈 응답
            log.warning("[gdelt] %s 비JSON 응답: %s", query, r.text[:80])
            return []
        try:
            arts = r.json().get("articles", []) or []
        except Exception:
            return []
        out, seen = [], set()
        for a in arts:
            try:
                link = (a.get("url") or "").strip()
                if not link or link in seen:
                    continue
                seen.add(link)
                out.append({
                    "title": (a.get("title") or "").strip(),
                    "domain": a.get("domain", ""),
                    "language": a.get("language", ""),
                    "country": a.get("sourcecountry", ""),
                    "date": a.get("seendate", ""),
                    "link": link,
                })
            except Exception:
                continue
        return out
    log.warning("[gdelt] %s 429 재시도 소진", query)
    return []


def _save_atomic(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def collect(queries, n, timespan, lang, out_path):
    by_q, total = {}, 0
    for q in queries:
        arts = fetch_gdelt(q, n=n, timespan=timespan, lang=lang)
        by_q[q] = arts
        total += len(arts)
        log.info("[gdelt] %-26s %d건", q, len(arts))
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "GDELT 2.0 DOC API (무료·키불필요)",
        "timespan": timespan, "lang": lang or "all",
        "queries": queries, "total_articles": total, "by_query": by_q,
        "_note": "글로벌/외신 커버리지·이벤트. 영문 쿼리(영문 회사명)가 매칭 잘 됨. 링크 열어 교차검증.",
    }
    _save_atomic(out_path, payload)
    log.info("[gdelt] 저장: %s (쿼리 %d · 기사 %d)", out_path, len(queries), total)
    return payload


def main():
    ap = argparse.ArgumentParser(description="GDELT 2.0 글로벌 뉴스 수집(무료)")
    ap.add_argument("--queries", default="Samsung Electronics,SK Hynix",
                    help="쉼표구분 검색어(영문 회사명 권장)")
    ap.add_argument("--n", type=int, default=15, help="쿼리당 기사 수(기본 15, 최대 250)")
    ap.add_argument("--timespan", default="3d", help="기간(예: 24h, 3d, 1w)")
    ap.add_argument("--lang", default="", help="소스 언어 제한(예: korean, english). 비우면 전체")
    ap.add_argument("--out", default=OUT_DEFAULT)
    args = ap.parse_args()

    if requests is None:
        log.error("[gdelt] requests 미설치 — pip install requests 후 재시도")
        sys.exit(1)

    qs = [q.strip() for q in args.queries.split(",") if q.strip()]
    if not qs:
        log.error("[gdelt] 쿼리가 없습니다(--queries)")
        sys.exit(2)
    collect(qs, max(1, args.n), args.timespan, (args.lang or None), os.path.abspath(args.out))


if __name__ == "__main__":
    main()
