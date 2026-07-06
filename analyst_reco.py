# -*- coding: utf-8 -*-
r"""
analyst_reco.py — 증권사 애널리스트 투자의견·목표주가 컨센서스 + 리포트 수집 (무료·무키)

[목적] "전문가(애널리스트)의 매수/매도 추천"을 종목별로 모은다. 순수 수급/뉴스가 못 주는 '펀더 전문가 view'.
  - 컨센서스: 평균 투자의견(매수/중립/매도) + 평균 목표주가 + 현재가 대비 상승여력(upside).
  - 증권사 리포트 목록: 어느 증권사가 무슨 제목으로 언제 냈나.

[데이터] 네이버 모바일 종목 통합 API(m.stock.naver.com/api/stock/<code>/integration) 의 consensusInfo + researches.
  현재가는 .../basic 의 closePrice. 셀레늄 불필요(깨끗한 JSON).

[투자의견 척도] recommMean 1~5 (1=강력매도 … 3=중립 … 5=강력매수). 5점 만점 평균.
[설계] requests. 종목별 try/except, 원자적 저장, ASCII 태그([reco]), 한글 OK·이모지 금지, UTF-8. 비밀키 미취급.

[사용법]
  python analyst_reco.py --tickers 005930,000660,012450
  python analyst_reco.py            # watch_tickers 상위 종목
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
log = logging.getLogger("reco")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DEFAULT = os.path.join(HERE, "analyst_reco.json")
TICKERS_FILE = os.path.join(HERE, "watch_tickers.txt")
API = "https://m.stock.naver.com/api/stock/"
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://m.stock.naver.com/"}


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


def _label(mean):
    if mean is None:
        return None
    if mean >= 4.5:
        return "강력매수"
    if mean >= 3.5:
        return "매수"
    if mean >= 2.5:
        return "중립"
    if mean >= 1.5:
        return "매도"
    return "강력매도"


def fetch_ticker(code):
    if requests is None:
        return None
    code = str(code).zfill(6)
    try:
        j = requests.get(API + code + "/integration", headers=H, timeout=15).json()
    except Exception:
        return None
    ci = j.get("consensusInfo") or {}
    mean = _num(ci.get("recommMean"))
    target = _num(ci.get("priceTargetMean"))
    name = j.get("stockName") or code
    reports = []
    for r in (j.get("researches") or []):
        reports.append({"broker": r.get("bnm", ""), "title": (r.get("tit") or "").strip(),
                        "date": r.get("wdt", "")})
    # 현재가 → 상승여력
    price = None
    try:
        b = requests.get(API + code + "/basic", headers=H, timeout=15).json()
        price = _num(b.get("closePrice"))
    except Exception:
        price = None
    upside = round((target / price - 1.0) * 100, 1) if (target and price) else None
    return {
        "name": name,
        "consensus": {"opinion_mean": mean, "opinion_label": _label(mean),
                      "target_price": int(target) if target else None,
                      "current_price": int(price) if price else None,
                      "upside_pct": upside, "date": ci.get("createDate")},
        "reports": reports,
    }


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


def collect(tickers, out_path):
    by, n_reco = {}, 0
    for c in tickers:
        d = fetch_ticker(c)
        if d is None:
            continue
        by[str(c).zfill(6)] = d
        cons = d["consensus"]
        if cons.get("opinion_label"):
            n_reco += 1
        log.info("[reco] %s %s | 의견=%s 목표=%s 상승여력=%s%% | 리포트 %d",
                 str(c).zfill(6), d["name"][:8], cons.get("opinion_label"),
                 cons.get("target_price"), cons.get("upside_pct"), len(d["reports"]))
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "source": "네이버 금융 애널리스트 컨센서스+리포트(무료·국문)",
               "tickers": tickers, "n_with_consensus": n_reco, "by_ticker": by,
               "_note": ("opinion_label(매수/중립/매도)·target_price·upside_pct(목표가/현재가-1)·증권사 리포트. "
                         "매도·중립 의견이나 upside 음수(목표가<현재가)면 [6] 역검증에서 강한 경고. "
                         "전문가도 매수면 보강, 의견 갈리면 thesis 에 양면 명시.")}
    _save(out_path, payload)
    log.info("[reco] 저장: %s (종목 %d · 컨센서스 %d)", out_path, len(by), n_reco)
    return payload


def main():
    ap = argparse.ArgumentParser(description="증권사 애널리스트 투자의견·목표주가 수집")
    ap.add_argument("--tickers", default="")
    ap.add_argument("--out", default=OUT_DEFAULT)
    args = ap.parse_args()
    if requests is None:
        log.error("[reco] requests 미설치")
        sys.exit(1)
    ts = [t.strip() for t in args.tickers.split(",") if t.strip()] or _load_default_tickers()
    if not ts:
        log.error("[reco] 종목 없음")
        sys.exit(2)
    collect(ts, os.path.abspath(args.out))


if __name__ == "__main__":
    main()
