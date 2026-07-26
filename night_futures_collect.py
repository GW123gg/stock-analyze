# -*- coding: utf-8 -*-
"""
night_futures_collect.py — 코스피200 선물(야간 세션 포함) → 루트 night_futures.json [v10.1 신규]

[왜] 한국 야간선물은 18:00~익일 06:00 에 거래되고, 06:30 분석 시점엔 **그날 개장 갭의 가장 직접적인
  힌트**다. 기존 간밤 신호(ES/NQ 미국선물·EWY)는 '미국 시장의 한국물 대리지표'인 반면, 이건
  한국 지수 자체의 야간 호가다. 개별종목은 야간 거래가 없으므로 **지수 방향 추정에만** 쓴다.

[출처] kr.investing.com 코스피200 선물(F). KRX 정보데이터시스템 JSON 엔드포인트는 외부 호출을
  차단하고(2026-07-26 실측: Error 페이지), pykrx 선물 API 는 KRX 로그인 차단 시 JSONDecodeError 로
  죽는다 → 실적 캘린더에서 검증된 investing 경로(requests → curl 폴백)를 재사용한다.

[★자기검증 설계 — 이 수집기의 핵심]
  investing 이 '야간 세션'을 실제로 반영하는지는 주말/장중에는 확인할 수 없다. 그래서 매 수집마다
    · session_text: 페이지가 표시하는 상태·날짜("닫음 · 24/07" 등)
    · ref_close: 같은 시점 KOSPI200 지수 종가(FDR)
    · vs_index_pct: 선물 - 지수 괴리(%)
  를 함께 남긴다. **여러 날 vs_index_pct 가 정확히 0 이면 야간 반영이 없는 것**이고, 값이 흔들리면
  야간·베이시스가 반영되는 것이다. 판정 전까지 분석 지시문은 이 값을 '참고'로만 쓰게 한다.

[출력] 루트 night_futures.json (deriv/ecos/vkospi/credit 과 같은 '루트 국면신호' 컨벤션)

[설계] 독립 실행·graceful(실패 시 exit 0·파일 미생성)·ASCII 로그 태그·원자적 저장·이모지 금지.

[사용법]
  python night_futures_collect.py            # → 루트 night_futures.json
  python night_futures_collect.py --check    # 통신·파싱만 검증(저장 안 함)
"""
import os
import re
import sys
import json
import html
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

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DEFAULT = os.path.join(HERE, "night_futures.json")
URL_K200 = "https://kr.investing.com/indices/korea-200-futures"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

logging.basicConfig(level=logging.INFO, format="[nightfut] %(message)s")
log = logging.getLogger("nightfut")

from common import save_json_atomic


# ── 페이지 취득: requests → curl 폴백(investing 이 python TLS 지문을 403 하는 경우가 있다) ──
def _fetch(url):
    if requests is not None:
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": UA})
            if r.status_code == 200 and len(r.text) > 5000:
                return r.text
            log.info("requests HTTP %s — curl 폴백", r.status_code)
        except Exception as e:
            log.info("requests 실패(%s) — curl 폴백", type(e).__name__)
    try:
        import subprocess
        p = subprocess.run(["curl", "-s", "-m", "30", url, "-H", f"User-Agent: {UA}"],
                           capture_output=True, timeout=45)
        if p.returncode == 0 and p.stdout:
            return p.stdout.decode("utf-8", "replace")
        log.info("curl 실패(rc=%s)", p.returncode)
    except Exception as e:
        log.info("curl 예외(%s)", type(e).__name__)
    return ""


def _num(s):
    try:
        return float(str(s).replace(",", "").replace("%", "").replace("+", "").strip())
    except (TypeError, ValueError):
        return None


def parse_quote(page):
    """investing 상품 페이지 → {last, change, change_pct, session_text}. 실패 필드는 None.

    순수함수(하네스 테스트 대상). 셀렉터가 바뀌면 값이 None 이 되고 호출부가 생략한다.
    """
    out = {"last": None, "change": None, "change_pct": None,
           "session_text": None, "last_trade_utc": None}
    if not page:
        return out
    pats = {
        "last": r'instrument-price-last[^>]*>([^<]+)',
        "change": r'instrument-price-change[^>]*>([^<]+)',
        "change_pct": r'instrument-price-change-percent[^>]*>\(?([^<)]+)',
    }
    for k, p in pats.items():
        m = re.search(p, page)
        if m:
            out[k] = _num(html.unescape(m.group(1)))
    # 세션 상태·최종체결 시각 — 야간 반영 판정의 핵심 근거.
    #  ★텍스트 패턴이 아니라 data-test 속성으로 잡는다(텍스트 매칭은 다른 위젯 '실시간 환율 시세'를
    #    오탐했다 — 2026-07-26 실측). dateTime 속성의 ISO(UTC)가 가장 확실한 판정 재료다:
    #    UTC 21:00(=KST 06:00) 근처면 야간 세션 종료가, UTC 06:45(=KST 15:45) 근처면 주간 종가가 잡힌 것.
    st = re.search(r'data-test="trading-state-label"[^>]*>([^<]+)<', page)
    tm = re.search(r'data-test="trading-time-label"[^>]*>([^<]+)<', page)
    if st or tm:
        out["session_text"] = " · ".join(
            html.unescape(x.group(1)).strip() for x in (st, tm) if x)
    iso = re.search(r'<time[^>]*dateTime="([^"]+)"[^>]*data-test="trading-time-label"', page)
    if not iso:
        iso = re.search(r'data-test="trading-time-label"[^>]*dateTime="([^"]+)"', page)
    if iso:
        out["last_trade_utc"] = iso.group(1).strip()
    return out


def _kospi200_close():
    """KOSPI200 지수 최근 종가(FDR). 실패 시 None — 괴리 계산만 생략된다."""
    try:
        import FinanceDataReader as fdr
        from datetime import timedelta
        df = fdr.DataReader("KS200", (datetime.now() - timedelta(days=12)).strftime("%Y-%m-%d"))
        c = [float(x) for x in df["Close"].tolist() if x == x]
        return round(c[-1], 2) if c else None
    except Exception:
        return None


def kst_session_info(iso_utc):
    """ISO(UTC) 최종체결 시각 → (KST 문자열, 세션 추정). 파싱 실패 시 (None, 'unknown').

    한국 파생 시간대: 주간 09:00~15:45, 야간 18:00~익일 06:00(KST).
    → KST 시각이 16:00~익일 07:00 구간이면 '야간 세션'으로 본다. 이 판정이 있어야
      06:30 수집분이 '간밤 호가'인지 '어제 주간 종가'인지 분석가가 즉시 안다.
    """
    if not iso_utc:
        return None, "unknown"
    try:
        from datetime import timedelta
        t = datetime.strptime(str(iso_utc).replace("Z", "")[:19], "%Y-%m-%dT%H:%M:%S")
        k = t + timedelta(hours=9)
    except Exception:
        return None, "unknown"
    h = k.hour
    sess = "night" if (h >= 16 or h < 7) else ("day" if 9 <= h < 16 else "off")
    return k.strftime("%Y-%m-%d %H:%M"), sess


def build_payload(quote, ref_close):
    """파싱 결과 + 지수 종가 → 저장 페이로드. last 가 없으면 {}(생략)."""
    last = quote.get("last")
    if last is None:
        return {}
    kst, sess = kst_session_info(quote.get("last_trade_utc"))
    vs_idx = None
    if ref_close:
        try:
            vs_idx = round((last / ref_close - 1.0) * 100.0, 2)
        except (TypeError, ZeroDivisionError):
            vs_idx = None
    return {
        "asof": datetime.now().isoformat(timespec="seconds"),
        "source": "kr.investing.com KOSPI200 futures",
        "what": ("코스피200 선물 최종 체결가(야간 세션 18:00~익일 06:00 포함 가능). "
                 "개별종목은 야간 거래가 없어 **지수 방향 추정 전용**."),
        "last": last,
        "change": quote.get("change"),
        "change_pct": quote.get("change_pct"),
        "session_text": quote.get("session_text"),
        "last_trade_utc": quote.get("last_trade_utc"),
        "last_trade_kst": kst,
        "session_guess": sess,          # night / day / off / unknown ← 야간 반영 여부 자동 판정
        "kospi200_ref_close": ref_close,
        "vs_index_pct": vs_idx,
        "verification_note": (
            "★사용 전 `session_guess` 를 먼저 보라(2026-07-26 도입, 야간 반영 여부 자동 판정). "
            "'night' = 간밤 야간 세션 호가가 잡힌 것(당일 갭 힌트로 쓸 수 있음). "
            "'day' = 어제 주간 종가만 잡힌 것(간밤 정보 없음 — 갭 근거로 쓰지 마라). "
            "'unknown/off' = 판정 불가. 또 `last_trade_kst` 가 직전 거래일보다 낡으면 그날은 무시하라. "
            "vs_index_pct(선물-지수 괴리)는 베이시스라 0 이 아닌 게 정상이며, 급락장에선 "
            "백워데이션으로 크게 벌어질 수 있다(2026-07-24 실측 -2.04%)."),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def main():
    ap = argparse.ArgumentParser(description="코스피200 선물(야간 포함) → night_futures.json")
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--check", action="store_true", help="통신·파싱만 검증(저장 안 함)")
    args = ap.parse_args()

    page = _fetch(URL_K200)
    q = parse_quote(page)
    if q.get("last") is None:
        log.info("시세 파싱 실패(차단/구조 변경) — 파일 미생성, 정상 종료. "
                 "분석은 기존 간밤 신호(ES/NQ 선물·EWY)로 진행")
        return 0
    ref = _kospi200_close()
    payload = build_payload(q, ref)
    if args.check:
        log.info("check: last=%s chg=%s(%s%%) session=%s ref=%s vs_index=%s%%",
                 q["last"], q["change"], q["change_pct"], q.get("session_text"),
                 ref, payload.get("vs_index_pct"))
        return 0
    save_json_atomic(args.out, payload)
    log.info("저장: %s (last=%s %s%% | %s | 지수대비 %s%%)",
             args.out, q["last"], q["change_pct"], q.get("session_text") or "-",
             payload.get("vs_index_pct"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log.info("최상위 예외 흡수(%s: %s) — exit 0", type(e).__name__, str(e)[:120])
        sys.exit(0)
