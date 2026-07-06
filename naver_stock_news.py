# -*- coding: utf-8 -*-
r"""
naver_stock_news.py — 네이버 금융 종목별 뉴스 수집 (무료·무키·무셀레늄)

[목적] 종목별 국문 뉴스를 네이버 금융에서 직접 모은다(국내 커버리지 최강·종목 직결).
  구글RSS(키워드)·Yahoo(영문)·GDELT(글로벌)를 보완하는 '종목별 국문 뉴스'.

[방식] finance.naver.com 정적 HTML 은 JS 로 링크를 생성해 파싱이 취약 → 대신 네이버 모바일 종목뉴스
  JSON API(m.stock.naver.com/api/news/stock/<code>)를 쓴다(깨끗한 JSON, 셀레늄 불필요).

[설계] requests. 종목별 try/except, 중복제거, 원자적 저장, ASCII 태그([navfin]), 한글 OK·이모지 금지,
  UTF-8 ensure_ascii=False. 비밀키 미취급.

[사용법]
  python naver_stock_news.py --tickers 005930,000660 --n 10
  python naver_stock_news.py            # watch_tickers 상위 종목
"""
import os
import sys
import json
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
log = logging.getLogger("navfin")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DEFAULT = os.path.join(HERE, "naver_stock_news.json")
TICKERS_FILE = os.path.join(HERE, "watch_tickers.txt")
API = "https://m.stock.naver.com/api/news/stock/"
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://m.stock.naver.com/"}


def _fmt_date(s):
    s = str(s or "")
    if len(s) >= 12:   # YYYYMMDDHHMM
        return "%s-%s-%s %s:%s" % (s[0:4], s[4:6], s[6:8], s[8:10], s[10:12])
    return s


def fetch_ticker(code, n=10):
    if requests is None:
        return []
    code = str(code).zfill(6)
    try:
        r = requests.get(API + code, params={"pageSize": max(1, n), "page": 1}, headers=H, timeout=15)
        if r.status_code != 200:
            return []
        groups = r.json()
    except Exception:
        return []
    items = []
    for g in (groups or []):
        items += (g.get("items") or [])
    out, seen = [], set()
    for it in items:
        try:
            oid = str(it.get("officeId") or "")
            aid = str(it.get("articleId") or "")
            key = oid + aid
            if not aid or key in seen:
                continue
            seen.add(key)
            link = ("https://n.news.naver.com/article/%s/%s" % (oid, aid)) if (oid and aid) else it.get("mobileNewsUrl", "")
            out.append({"title": (it.get("titleFull") or it.get("title") or "").strip(),
                        "source": it.get("officeName", ""), "date": _fmt_date(it.get("datetime")),
                        "link": link})
            if len(out) >= n:
                break
        except Exception:
            continue
    return out


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
        log.info("[navfin] %s %d건", str(c).zfill(6), len(arts))
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "source": "네이버 금융 종목뉴스(m.stock.naver.com, 무료·국문)", "tickers": tickers,
               "total_articles": total, "by_ticker": by,
               "_note": "종목 직결 국문 뉴스. title/source/date/link. [6] 종목별 악재역검증에 활용."}
    _save(out_path, payload)
    log.info("[navfin] 저장: %s (종목 %d · 기사 %d)", out_path, len(tickers), total)
    return payload


def main():
    ap = argparse.ArgumentParser(description="네이버 금융 종목별 뉴스")
    ap.add_argument("--tickers", default="")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--out", default=OUT_DEFAULT)
    args = ap.parse_args()
    if requests is None:
        log.error("[navfin] requests 미설치")
        sys.exit(1)
    ts = [t.strip() for t in args.tickers.split(",") if t.strip()] or _load_default_tickers()
    if not ts:
        log.error("[navfin] 종목 없음")
        sys.exit(2)
    collect(ts, max(1, args.n), os.path.abspath(args.out))


if __name__ == "__main__":
    main()
