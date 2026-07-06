# -*- coding: utf-8 -*-
r"""
news_rss_collect.py — 무료 구글뉴스 RSS 수집기 (Apify 대체·폴백, 무제한·무과금·한국어)

[목적] Apify(유료·다계정 차단 위험) 없이 news.google.com 의 공개 RSS 로 종목/테마 뉴스를 직접 수집.
  비용·계정·약관 문제 전혀 없음. hl=ko&gl=KR 로 국문 기사(매경·한경·연합·이데일리…)를 받는다.
  결과를 news_rss.json 으로 저장 → 아침 PART A Cowork 가 flow_data/fsc_prices 처럼 읽는다.

[로케일] 기본 한국어(ko). --lang en 으로 영문도 가능.
[설계] requests + 표준 xml.etree(추가 의존성 없음). 키워드별 try/except, 링크 중복제거, 원자적 저장,
  ASCII 태그([rss]) + 한글, 이모지 금지, UTF-8 ensure_ascii=False. 비밀키 미취급.

[사용법]
  python news_rss_collect.py --keywords "삼성전자,SK하이닉스,한화에어로스페이스" --n 20
  python news_rss_collect.py --keywords "삼성전자 HBM" --n 15 --out news_rss.json
  python news_rss_collect.py            # 키워드 생략 시 watch_tickers.txt 상위 종목명 사용
"""
import os
import sys
import json
import argparse
import logging
from datetime import datetime
import xml.etree.ElementTree as ET

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
log = logging.getLogger("rss")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DEFAULT = os.path.join(HERE, "news_rss.json")
TICKERS_FILE = os.path.join(HERE, "watch_tickers.txt")
RSS_URL = "https://news.google.com/rss/search"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

LOCALES = {
    "ko": {"hl": "ko", "gl": "KR", "ceid": "KR:ko"},
    "en": {"hl": "en-US", "gl": "US", "ceid": "US:en"},
}


def _clean_title(title, source):
    """구글뉴스 제목 끝의 ' - 매체명' 접미를 제거(중복)."""
    if title and source and title.rstrip().endswith("- " + source):
        return title.rsplit("- " + source, 1)[0].strip()
    if title and source and title.rstrip().endswith("-" + source):
        return title.rsplit("-" + source, 1)[0].strip()
    return title


def fetch_news(keyword, n=20, lang="ko"):
    """키워드 1개 → 기사 [{title, source, link, date}] (최신순, 링크 중복제거). 실패 시 []."""
    if requests is None:
        return []
    loc = LOCALES.get(lang, LOCALES["ko"])
    params = {"q": keyword}
    params.update(loc)
    try:
        r = requests.get(RSS_URL, params=params, headers={"User-Agent": UA}, timeout=20)
        if r.status_code != 200:
            log.warning("[rss] %s HTTP %d", keyword, r.status_code)
            return []
        root = ET.fromstring(r.content)
    except Exception as e:
        log.warning("[rss] %s 수집 실패: %s", keyword, type(e).__name__)
        return []

    out, seen = [], set()
    for it in root.findall(".//item"):
        try:
            link = (it.findtext("link") or "").strip()
            if not link or link in seen:
                continue
            seen.add(link)
            src_el = it.find("source")
            source = (src_el.text.strip() if (src_el is not None and src_el.text) else "")
            title = (it.findtext("title") or "").strip()
            out.append({
                "title": _clean_title(title, source),
                "source": source,
                "link": link,
                "date": (it.findtext("pubDate") or "").strip(),
            })
            if len(out) >= n:
                break
        except Exception:
            continue
    return out


def _load_default_keywords(limit=12):
    """키워드 미지정 시 watch_tickers.txt 의 상위 종목명."""
    names = []
    if not os.path.isfile(TICKERS_FILE):
        return names
    try:
        with open(TICKERS_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "#" in line:
                    nm = line.split("#", 1)[1].strip()
                    if nm:
                        names.append(nm)
                if len(names) >= limit:
                    break
    except Exception:
        pass
    return names


def _save_atomic(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def collect(keywords, n, lang, out_path):
    by_kw, total = {}, 0
    for kw in keywords:
        arts = fetch_news(kw, n=n, lang=lang)
        by_kw[kw] = arts
        total += len(arts)
        log.info("[rss] %-20s %d건", kw, len(arts))
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "google_news_rss (무료·무과금)",
        "lang": lang,
        "keywords": keywords,
        "total_articles": total,
        "by_keyword": by_kw,
        "_note": "Apify 대체 무료 폴백. 기사 title/source/link/date 를 근거로 쓰되 중요한 건 링크 열어 교차검증.",
    }
    _save_atomic(out_path, payload)
    log.info("[rss] 저장: %s (키워드 %d · 기사 %d)", out_path, len(keywords), total)
    return payload


def main():
    ap = argparse.ArgumentParser(description="무료 구글뉴스 RSS 수집기")
    ap.add_argument("--keywords", default="", help="쉼표구분 검색어(미지정 시 watch_tickers 상위 종목명)")
    ap.add_argument("--n", type=int, default=20, help="키워드당 기사 수(기본 20)")
    ap.add_argument("--lang", default="ko", choices=["ko", "en"], help="ko(국문)/en(영문)")
    ap.add_argument("--out", default=OUT_DEFAULT)
    args = ap.parse_args()

    if requests is None:
        log.error("[rss] requests 미설치 — pip install requests 후 재시도")
        sys.exit(1)

    kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if not kws:
        kws = _load_default_keywords()
    if not kws:
        log.error("[rss] 키워드가 없습니다(--keywords 지정 또는 watch_tickers.txt 확인)")
        sys.exit(2)

    collect(kws, max(1, args.n), args.lang, os.path.abspath(args.out))


if __name__ == "__main__":
    main()
