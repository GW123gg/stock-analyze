# -*- coding: utf-8 -*-
r"""
fetch_html.py — 막힌 기사 사이트의 전체 HTML 끌어오기 (코워크가 직접 파싱하는 최후 폴백)

[목적] 일반 수집(requests/RSS/API)이 막힌 사이트는, 사이트 HTML 을 통째로 끌어와 정리해서 저장한다.
  '코워크가 똑똑하니' 그 HTML 에서 기사 본문을 직접 읽어 추출하게 한다. r.jina.ai reader 보다 직접적.

[방식·안전] 기본은 **curl_cffi(TLS 위장, 셀레늄 불필요)** → requests 폴백. 진짜 JS 렌더가 필요한 사이트만
  `--selenium` 으로 옵트인(undetected-chromedriver). 셀레늄은 6/24 아침수집 멈춤의 원인이므로 **워치독 타이머가
  WALL 초 후 드라이버·크롬을 강제종료(taskkill)** 하고, 기본 경로(curl_cffi)는 셀레늄을 절대 띄우지 않는다.

[설계] script/style/head/svg/주석 제거 후 본문 위주로 정리, 최대 MAX_CHARS 로 절단. 원자적 저장.
  ASCII 태그([html]) + 한글, 이모지 금지, UTF-8. 비밀키 미취급.

[사용법]
  python fetch_html.py "https://example.com/article" --out raw_html.txt
  python fetch_html.py "https://example.com/article" --selenium     # JS 렌더 필요할 때만
"""
import os
import re
import sys
import argparse
import logging
import threading
import subprocess
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("html")

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
MAX_CHARS = 60000
SELENIUM_WALL = 40   # 셀레늄 하드 타임아웃(초)

try:
    from curl_cffi import requests as cffi
    CFFI = True
except Exception:
    CFFI = False
try:
    import requests as _rq
except Exception:
    _rq = None


def clean_html(html):
    """script/style/head/svg/noscript/주석 제거 + 공백 정리 + 절단(코워크가 읽기 좋게)."""
    if not html:
        return ""
    h = html
    for tag in ("script", "style", "head", "svg", "noscript", "iframe"):
        h = re.sub(r"<%s[^>]*>.*?</%s>" % (tag, tag), " ", h, flags=re.S | re.I)
    h = re.sub(r"<!--.*?-->", " ", h, flags=re.S)
    h = re.sub(r"[ \t\r\f\v]+", " ", h)
    h = re.sub(r"\n\s*\n+", "\n", h)
    return h.strip()[:MAX_CHARS]


def by_cffi(url):
    if not CFFI:
        return None
    try:
        r = cffi.get(url, impersonate="chrome", timeout=20,
                     headers={"User-Agent": UA, "Accept-Language": "ko,en;q=0.9"})
        if r.status_code == 200 and len(r.text) > 500:
            return r.text
    except Exception as e:
        log.info("[html] curl_cffi 실패: %s", type(e).__name__)
    return None


def by_requests(url):
    if _rq is None:
        return None
    try:
        r = _rq.get(url, headers={"User-Agent": UA, "Accept-Language": "ko,en;q=0.9"}, timeout=20)
        if r.status_code == 200 and len(r.text) > 500:
            return r.text
    except Exception as e:
        log.info("[html] requests 실패: %s", type(e).__name__)
    return None


def _kill_chrome():
    """행 방지: chromedriver/undetected chrome 잔존 프로세스 강제종료(Windows)."""
    for img in ("chromedriver.exe", "undetected_chromedriver.exe"):
        try:
            subprocess.run(["taskkill", "/F", "/IM", img, "/T"], capture_output=True, timeout=10)
        except Exception:
            pass


def by_selenium(url):
    """undetected-chromedriver 로 JS 렌더. 워치독이 WALL 초 후 강제종료(행 방지)."""
    driver = None
    timer = threading.Timer(SELENIUM_WALL, lambda: (_kill_chrome(), os._exit(124)))
    timer.daemon = True
    timer.start()
    try:
        try:
            import undetected_chromedriver as uc
            opts = uc.ChromeOptions()
        except Exception:
            from selenium import webdriver as _wd
            from selenium.webdriver.chrome.options import Options as _O
            uc = None
            opts = _O()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1280,1800")
        if uc is not None:
            driver = uc.Chrome(options=opts)
        else:
            from selenium import webdriver as _wd
            driver = _wd.Chrome(options=opts)
        driver.set_page_load_timeout(SELENIUM_WALL - 8)
        driver.get(url)
        html = driver.page_source
        return html if (html and len(html) > 500) else None
    except Exception as e:
        log.info("[html] selenium 실패: %s", type(e).__name__)
        return None
    finally:
        timer.cancel()
        try:
            if driver is not None:
                driver.quit()
        except Exception:
            pass
        _kill_chrome()


def fetch(url, use_selenium=False):
    """url → 정리된 HTML 문자열(+사용한 방법). 단계: curl_cffi → requests → (옵트인)selenium."""
    for name, fn in (("curl_cffi", by_cffi), ("requests", by_requests)):
        h = fn(url)
        if h:
            return clean_html(h), name
    if use_selenium:
        h = by_selenium(url)
        if h:
            return clean_html(h), "selenium"
    return "", "none"


def main():
    ap = argparse.ArgumentParser(description="막힌 사이트 전체 HTML 끌어오기(코워크 파싱용)")
    ap.add_argument("url")
    ap.add_argument("--selenium", action="store_true", help="JS 렌더 필요 시 셀레늄 사용(행-세이프)")
    ap.add_argument("--out", default="", help="저장 경로(미지정 시 raw_html.txt)")
    args = ap.parse_args()

    out = os.path.abspath(args.out) if args.out else os.path.join(HERE, "raw_html.txt")
    html, method = fetch(args.url, use_selenium=args.selenium)
    if not html:
        log.error("[html] 수집 실패(모든 방법). --selenium 을 시도해 보세요: %s", args.url)
        sys.exit(3)
    header = "<!-- fetched_at=%s method=%s url=%s len=%d -->\n" % (
        datetime.now().isoformat(timespec="seconds"), method, args.url, len(html))
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(header + html)
    os.replace(tmp, out)
    log.info("[html] 저장: %s (방법=%s, %d자) — 코워크가 이 파일에서 기사 본문을 추출", out, method, len(html))


if __name__ == "__main__":
    main()
