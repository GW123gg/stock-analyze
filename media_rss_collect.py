# -*- coding: utf-8 -*-
r"""
media_rss_collect.py — 한국 매체 직접 RSS 전체 피드 수집 (무료·무키·무셀레늄)

[목적] 연합·한경·매경·이데일리 등 국내 매체의 '경제/증권 전체 피드'를 통째로 받아 시장 흐름·테마 뉴스를 모은다.
  구글뉴스RSS(키워드 기반)·네이버 종목뉴스(종목별)를 보완하는 '시장 전반 국문 뉴스 스트림'([0]/[2] 테마 점검).

[피드] media_rss_feeds.txt 에서 '매체명 | URL' 로 읽는다(URL 바뀌면 그 파일만 수정). SSL 문제 매체는 자동 우회.
[설계] requests + 표준 xml.etree. 매체별 try/except, 링크 중복제거, 원자적 저장, ASCII 태그([media]),
  한글 OK·이모지 금지, UTF-8 ensure_ascii=False. 비밀키 미취급.

[사용법] python media_rss_collect.py --n 30
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
    try:
        import urllib3
        urllib3.disable_warnings()
    except Exception:
        pass
except Exception:
    requests = None

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("media")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DEFAULT = os.path.join(HERE, "media_rss.json")
FEEDS_FILE = os.path.join(HERE, "media_rss_feeds.txt")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def load_feeds():
    out = []
    if not os.path.isfile(FEEDS_FILE):
        return out
    try:
        with open(FEEDS_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "|" not in line:
                    continue
                nm, url = line.split("|", 1)
                nm, url = nm.strip(), url.strip()
                if nm and url:
                    out.append((nm, url))
    except Exception:
        pass
    return out


def fetch_feed(url, n):
    if requests is None:
        return []
    for verify in (True, False):   # SSL 문제 매체는 verify=False 로 재시도
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=15, verify=verify)
            if r.status_code != 200:
                return []
            items = ET.fromstring(r.content).findall(".//item")
            break
        except requests.exceptions.SSLError:
            continue
        except Exception:
            return []
    else:
        return []
    out, seen = [], set()
    for it in items:
        link = (it.findtext("link") or "").strip()
        if not link or link in seen:
            continue
        seen.add(link)
        out.append({"title": (it.findtext("title") or "").strip(),
                    "link": link, "date": (it.findtext("pubDate") or "").strip()})
        if len(out) >= n:
            break
    return out


def _save(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def collect(n, out_path):
    feeds = load_feeds()
    by, total = {}, 0
    for nm, url in feeds:
        arts = fetch_feed(url, n)
        by[nm] = arts
        total += len(arts)
        log.info("[media] %-12s %d건", nm, len(arts))
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "source": "한국 매체 직접 RSS (무료·국문)", "feeds": [nm for nm, _ in feeds],
               "total_articles": total, "by_source": by,
               "_note": "시장 전반 국문 뉴스 스트림([0]/[2] 테마). 피드 URL 은 media_rss_feeds.txt 에서 수정."}
    _save(out_path, payload)
    log.info("[media] 저장: %s (매체 %d · 기사 %d)", out_path, len(feeds), total)
    return payload


def main():
    ap = argparse.ArgumentParser(description="한국 매체 직접 RSS 수집")
    ap.add_argument("--n", type=int, default=30, help="매체당 기사 수(기본 30)")
    ap.add_argument("--out", default=OUT_DEFAULT)
    args = ap.parse_args()
    if requests is None:
        log.error("[media] requests 미설치")
        sys.exit(1)
    collect(max(1, args.n), os.path.abspath(args.out))


if __name__ == "__main__":
    main()
