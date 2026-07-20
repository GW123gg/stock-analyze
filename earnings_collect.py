# -*- coding: utf-8 -*-
"""
earnings_collect.py — 한국 실적발표 캘린더 수집 → 세션 earnings_calendar.json

[왜] 회고 원장 잔존 '크롤 3종' 중 실적캘린더. v9.7 [4.8](5) 이벤트 경로 체크가 "픽의 지평 안
  실적 발표"를 웹검색으로 확인하라고 하는데, 구조화 캘린더가 있으면 검색 의존을 줄이고
  픽별 대조가 기계적으로 가능하다(수집 실패 시엔 기존 웹검색 폴백 그대로).

[출처] kr.investing.com 실적 캘린더 AJAX(공개, 키 불필요 — 2026-07-20 라이브 검증:
  POST /earnings-calendar/Service/getCalendarFilteredData, country[]=11(한국) → HTML rows).
  파싱은 정규식(stdlib) — 사이트 구조 변경 시 빈 결과 → graceful 생략(수집 실패=정상 경로).

[출력] 오늘 세션 earnings_calendar.json (세션 파일 — 루트 국면신호 아님, 종목 이벤트 데이터):
  {asof_date, window{from,to}, events:[{date, name, slug}...], count}
  (eps 실적치/예상치는 발표 캘린더 목록엔 없다 — 종목명·발표일 대조가 목적이라 3필드로 충분.)

[설계] 독립 실행·graceful(실패 시 exit 0, 파일 미생성)·ASCII 로그 [earnings]·
  원자적 저장(common.save_json_atomic)·resolve_session 위임·이모지 금지.

[사용법]
  python earnings_collect.py               # 오늘~+14일 → 세션 earnings_calendar.json
  python earnings_collect.py --check       # 통신/파싱만 검증
  python earnings_collect.py --days 7 --out x.json
"""
import os
import re
import sys
import html
import argparse
import logging
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import requests
except Exception:
    requests = None

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
URL = "https://kr.investing.com/earnings-calendar/Service/getCalendarFilteredData"

logging.basicConfig(level=logging.INFO, format="[earnings] %(message)s")
log = logging.getLogger("earnings")

from common import save_json_atomic, resolve_session

# ── HTML 파싱(순수함수 — 하네스 테스트 대상) ──────────────────────────
# 날짜 구분행:  <td colspan="9" class="theDay">2026년 7월 20일 월요일</td>
_RE_DAY = re.compile(r'class="theDay">\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일')
# 종목행: <td ... class="left noWrap earnCalCompany" title="기아" _p_pid="43460" ...>
#         ... <a href="/equities/kia-mot..." ...>
_RE_COMPANY = re.compile(
    r'earnCalCompany"\s+title="([^"]+)"[^>]*>.*?href="/equities/([^"?]+)', re.S)


def parse_calendar(html_text):
    """AJAX 응답의 data(HTML) → [{date, name, slug}...]. 구조 변경 시 빈 리스트(graceful)."""
    events = []
    cur_date = None
    # 행 단위로 순회: theDay 가 나오면 날짜 갱신, 회사행이 나오면 이벤트 추가
    for chunk in re.split(r"<tr\b", html_text):
        m = _RE_DAY.search(chunk)
        if m:
            y, mo, d = m.groups()
            cur_date = "%s-%02d-%02d" % (y, int(mo), int(d))
            continue
        c = _RE_COMPANY.search(chunk)
        if c and cur_date:
            name = html.unescape(c.group(1)).strip()
            slug = c.group(2).strip()
            if name:
                events.append({"date": cur_date, "name": name, "slug": slug})
    return events


def _fetch_via_requests(date_from, date_to):
    """requests 경로 — investing.com 이 python TLS 지문을 403 차단하는 경우가 있어(2026-07-20
    실측) 실패 시 curl 폴백으로 넘어간다."""
    if requests is None:
        return ""
    try:
        r = requests.post(
            URL, timeout=30,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                     "X-Requested-With": "XMLHttpRequest",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"country[]": "11", "dateFrom": date_from, "dateTo": date_to,
                  "currentTab": "custom", "limit_from": "0"})
        if r.status_code != 200:
            log.info("requests HTTP %s — curl 폴백 시도", r.status_code)
            return ""
        return (r.json() or {}).get("data") or ""
    except Exception as e:
        log.info("requests 실패(%s: %s) — curl 폴백 시도", type(e).__name__, str(e)[:120])
        return ""


def _fetch_via_curl(date_from, date_to):
    """curl.exe 폴백(Windows 10+ 기본 탑재) — TLS 지문이 브라우저와 달라 차단을 피한다
    (2026-07-20 라이브 검증: requests 403, curl 200)."""
    import subprocess
    import json as _json
    body = ("country[]=11&dateFrom=%s&dateTo=%s&currentTab=custom&limit_from=0"
            % (date_from, date_to))
    try:
        p = subprocess.run(
            ["curl", "-s", "-m", "30", "-X", "POST", URL,
             "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
             "-H", "X-Requested-With: XMLHttpRequest",
             "-H", "Content-Type: application/x-www-form-urlencoded",
             "-d", body],
            capture_output=True, timeout=40)
        if p.returncode != 0 or not p.stdout:
            log.info("curl 실패(rc=%s) — 생략", p.returncode)
            return ""
        return (_json.loads(p.stdout.decode("utf-8", "replace")) or {}).get("data") or ""
    except Exception as e:
        log.info("curl 폴백 실패(%s: %s) — 생략", type(e).__name__, str(e)[:120])
        return ""


def fetch_calendar(date_from, date_to):
    """investing.com 한국 실적 캘린더 조회 → HTML 문자열. requests → curl 순. 실패 시 ''."""
    return (_fetch_via_requests(date_from, date_to)
            or _fetch_via_curl(date_from, date_to))


def main():
    ap = argparse.ArgumentParser(description="한국 실적 캘린더 → earnings_calendar.json")
    ap.add_argument("--days", type=int, default=14, help="오늘부터 조회 일수(기본 14)")
    ap.add_argument("--out", default="")
    ap.add_argument("--check", action="store_true", help="통신/파싱만 검증(저장 안 함)")
    args = ap.parse_args()

    d_from = datetime.now().strftime("%Y-%m-%d")
    d_to = (datetime.now() + timedelta(days=args.days)).strftime("%Y-%m-%d")
    raw = fetch_calendar(d_from, d_to)
    events = parse_calendar(raw)
    if not events:
        log.info("이벤트 없음(통신 실패/구조 변경/휴장기) — 파일 미생성, 정상 종료. "
                 "분석은 기존 웹검색 폴백([4.8](5)) 사용")
        return 0
    if args.check:
        log.info("check: %s~%s 이벤트 %d건 (예: %s %s)", d_from, d_to, len(events),
                 events[0]["date"], events[0]["name"])
        return 0

    payload = {
        "asof_date": d_from,
        "source": "kr.investing.com earnings-calendar (country=KR)",
        "what": "향후 실적발표 예정(한국) — [4.8](5) 이벤트 경로 체크·픽 실적일정 대조용",
        "window": {"from": d_from, "to": d_to},
        "count": len(events),
        "events": events,
        "note": "슬러그는 investing.com 종목 경로(6자리 코드 아님) — 픽 대조는 종목명 기준. "
                "수집 실패 시 이 파일이 없는 게 정상(웹검색 폴백).",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    out_path = os.path.abspath(args.out) if args.out else None
    if not out_path:
        sess = resolve_session(OUTPUT_DIR)
        out_path = (os.path.join(sess, "earnings_calendar.json") if sess
                    else os.path.join(HERE, "earnings_calendar.json"))
    save_json_atomic(out_path, payload)
    log.info("저장: %s (%s~%s, %d건)", out_path, d_from, d_to, len(events))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log.info("최상위 예외 흡수(%s: %s) — exit 0", type(e).__name__, str(e)[:120])
        sys.exit(0)
