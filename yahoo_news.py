# -*- coding: utf-8 -*-
r"""
yahoo_news.py — Yahoo Finance 종목별 뉴스 수집 (무료 RSS, 셀레늄 불필요)

[목적] 한국 종목에 대한 '외신/글로벌 영문' 뉴스를 Yahoo Finance 종목 헤드라인 RSS 로 수집.
  국문(네이버/구글RSS)·글로벌(GDELT)을 보완하는 영문 시각. 외국인이 보는 관점 파악에 유용.

[방식·중요] Yahoo 는 종목별 RSS(feeds.finance.yahoo.com/rss/2.0/headline?s=005930.KS)가 동작하므로
  **셀레늄 없이 RSS** 로 받는다 → 6/24 아침수집 멈춤(셀레늄 스레드 행) 위험이 전혀 없다. KOSPI=.KS / KOSDAQ=.KQ
  (.KS 먼저, 결과 0이면 .KQ 재시도). 셀레늄이 필요한 진짜 막힌 사이트는 fetch_html.py(별도)가 담당.

[설계] requests + 표준 xml.etree. 종목별 try/except, 링크 중복제거, 원자적 저장, ASCII 태그([yahoo]),
  한글 OK·이모지 금지, UTF-8 ensure_ascii=False. 비밀키 미취급.

[사용법]
  python yahoo_news.py --tickers 005930,000660,012450 --n 10
  python yahoo_news.py            # 키워드 생략 시 watch_tickers.txt 상위 종목
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
log = logging.getLogger("yahoo")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DEFAULT = os.path.join(HERE, "yahoo_news.json")
TICKERS_FILE = os.path.join(HERE, "watch_tickers.txt")
RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def _fetch(symbol, n):
    try:
        r = requests.get(RSS, params={"s": symbol, "region": "US", "lang": "en-US"},
                         headers={"User-Agent": UA}, timeout=15)
        if r.status_code != 200:
            return []
        items = ET.fromstring(r.content).findall(".//item")
    except Exception:
        return []
    out, seen = [], set()
    for it in items:
        link = (it.findtext("link") or "").strip()
        if not link or link in seen:
            continue
        seen.add(link)
        out.append({"title": (it.findtext("title") or "").strip(),
                    "link": link, "date": (it.findtext("pubDate") or "").strip(),
                    "summary": (it.findtext("description") or "").strip()[:300]})
        if len(out) >= n:
            break
    return out


def fetch_ticker(code, n=10):
    """code(6자리) → Yahoo 뉴스. .KS 먼저, 0이면 .KQ 재시도."""
    if requests is None:
        return []
    code = str(code).zfill(6)
    arts = _fetch(code + ".KS", n)
    if not arts:
        arts = _fetch(code + ".KQ", n)
    return arts


def _load_default_tickers(limit=12):
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
                    out.append(code)
                if len(out) >= limit:
                    break
    except Exception:
        pass
    return out


def _save(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def collect(tickers, n, out_path):
    by, total = {}, 0
    for c in tickers:
        arts = fetch_ticker(c, n)
        by[str(c).zfill(6)] = arts
        total += len(arts)
        log.info("[yahoo] %s %d건", str(c).zfill(6), len(arts))
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "source": "Yahoo Finance 종목 RSS (무료·영문)", "tickers": tickers,
               "total_articles": total, "by_ticker": by,
               "_note": "한국 종목의 영문/외신 시각. 링크 열어 교차검증. .KS=KOSPI/.KQ=KOSDAQ."}
    _save(out_path, payload)
    log.info("[yahoo] 저장: %s (종목 %d · 기사 %d)", out_path, len(tickers), total)
    return payload


def main():
    ap = argparse.ArgumentParser(description="Yahoo Finance 종목 뉴스(RSS)")
    ap.add_argument("--tickers", default="", help="쉼표구분 6자리(미지정 시 watch_tickers 상위)")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--out", default=OUT_DEFAULT)
    args = ap.parse_args()
    if requests is None:
        log.error("[yahoo] requests 미설치")
        sys.exit(1)
    ts = [t.strip() for t in args.tickers.split(",") if t.strip()] or _load_default_tickers()
    if not ts:
        log.error("[yahoo] 종목 없음(--tickers 또는 watch_tickers.txt)")
        sys.exit(2)
    collect(ts, max(1, args.n), os.path.abspath(args.out))


if __name__ == "__main__":
    main()
