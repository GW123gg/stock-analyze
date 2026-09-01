#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
foreign_collect.py — 해외(미국·일본) 종목 사실 수집 [v11.23 신규]

[★이 파일의 존재 이유는 '보수적으로 하기 위해서'다]
  해외는 국내보다 **아는 게 적다.** 그래서 아는 것을 모으는 동시에 **모르는 것을 명시적으로
  기록**한다(`data_gaps`). 분석가가 "국내와 같은 수준으로 안다"고 착각하는 것을 막는 게
  이 수집기의 절반이다. 국내에 있고 해외에 없는 축:
    · 투자자별 일별 수급(외국인/기관/개인 순매수) — 미국은 이런 공시 자체가 없다
    · 일별 공매도 잔고 — 미국은 **월 2회**(FINRA 격주) 수준. 우리가 쓰는 건 yfinance 의
      shortRatio/shortPercentOfFloat 이고 기준일이 명시되지 않는다
    · 실시간 한국어 공시 대응 — SEC 는 영문이고 우리 RSS 커버리지가 국내보다 얕다
    · 세력강도(force_score) — 수급 데이터가 없어 **계산 자체가 불가능**하다
    · ★사용자가 미국 정규장 시간에 깨어 있지 않다 — 급변에 대응할 수 없다

[수집 — 전부 키 불필요. 2026-08-08 실측 확인]
  · FDR            : 미국·일본 일봉, 지수(US500/N225/VIX/IXIC/DJI/^SOX)
  · SEC EDGAR      : companyfacts XBRL(매출·영업이익) + 최근 공시 목록. User-Agent 만 필요
  · yfinance       : PER/목표가/애널리스트 수·투자의견 추이·공매도·실적일정·뉴스·기관보유
  ※ SEC 는 미국만. 일본은 EDINET(별도)이라 여기선 시세·yfinance 만 쓴다.

[★SEC 태그 함정 — 실측으로 두 번 틀렸다]
  (1) 'Revenue' 가 든 태그를 다 긁어 최신을 고르면 **이연매출 인식액**(ContractWith
      CustomerLiabilityRevenueRecognized, 애플 7.3B)을 총매출로 잡는다.
  (2) 화이트리스트를 '우선순위 고정'으로 쓰면 **태그를 갈아탄 기업**에서 낡은 값을 준다
      (NVIDIA 는 RevenueFromContract... 가 2022-01-30 에서 끊기고 이후 Revenues 를 쓴다
       — 4년 낡은 26.9B 를 최신인 양 돌려줬다).
  → 해법: **화이트리스트 안에서 최신 end 를 고른다.** 그리고 기간 길이(period_days)와
    지연 일수를 함께 남긴다 — 분기(81B)와 연간(331B)을 나란히 놓으면 그 자체가 오답이다.

[사용]
  python foreign_collect.py --tickers NVDA,AAPL --country US
  python foreign_collect.py --from-portfolio        # 등록자 보유 해외 종목
  python foreign_collect.py --out foreign_facts.json
"""
from __future__ import annotations

import os
import re
import sys
import json
import glob
import time
import argparse
import logging
import urllib.request
from datetime import datetime, date, timedelta

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")

logging.basicConfig(level=logging.INFO, format="[foreign] %(message)s")
log = logging.getLogger("foreign")

# SEC 는 User-Agent 로 연락처를 요구한다(없으면 403). 키는 필요 없다.
#   ★연락처를 코드에 박지 않는다 — 저장소를 공개하면 그대로 노출된다.
#   환경변수 SEC_CONTACT 에 본인 주소를 넣어라. 없으면 프로젝트명만 보낸다
#   (저빈도 조회에서는 통과하지만, SEC 정책상 연락처를 넣는 편이 옳다).
_SEC_CONTACT = os.environ.get("SEC_CONTACT", "").strip()
SEC_UA = {"User-Agent": ("stock_research (%s)" % _SEC_CONTACT) if _SEC_CONTACT
                        else "stock_research (educational research project)"}
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK%010d.json"

# ★화이트리스트. 이 밖의 'Revenue*' 태그는 곁가지다(위 함정 (1)).
REV_TAGS = {"RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "Revenues", "SalesRevenueNet"}
OP_TAGS = {"OperatingIncomeLoss"}
NI_TAGS = {"NetIncomeLoss"}

# 국내에 있고 해외에 없는 것 — 분석가가 반드시 보게 만든다
DATA_GAPS = [
    "투자자별 일별 수급(외국인·기관·개인 순매수)이 없다 — 세력강도(force_score) 계산 불가",
    "일별 공매도 잔고가 없다 — yfinance shortRatio 는 기준일이 불명확하고 갱신이 느리다",
    "한국어 실시간 공시·뉴스 커버리지가 국내보다 얕다(SEC 는 영문·분기 단위)",
    "장 시간이 한국 심야다 — 사용자가 급변에 대응할 수 없다(갭·실적 발표 반응 포함)",
    "환율이 수익률에 섞인다 — 종목 판단과 환 판단을 분리해야 한다",
]


# =====================================================================
# 순수 계산
# =====================================================================
def pick_fact(facts_us_gaap, tag_whitelist):
    """XBRL facts → (태그, 행). ★화이트리스트 안에서 **최신 end** 를 고른다. 순수함수.

    우선순위 고정이 아니라 최신 선택인 이유는 위 [SEC 태그 함정] (2) 참조.
    10-K/10-Q 만 본다(8-K 등의 잠정치·정정 혼입 방지).
    """
    best = None
    for t in (tag_whitelist & set(facts_us_gaap or {})):
        for unit in facts_us_gaap[t].get("units", {}).values():
            for x in unit:
                if x.get("form") not in ("10-K", "10-Q"):
                    continue
                if not x.get("end"):
                    continue
                if best is None or x["end"] > best[1]["end"]:
                    best = (t, x)
    return best if best else (None, None)


def describe_period(row, today=None):
    """XBRL 행 → 기간 성격과 지연. ★분기와 연간을 섞어 비교하는 사고를 막는다. 순수함수."""
    out = {}
    try:
        d1 = date.fromisoformat(row["end"])
        out["end"] = row["end"]
        out["lag_days"] = ((today or date.today()) - d1).days
    except Exception:
        return {"note": "기간 파싱 실패"}
    if row.get("start"):
        try:
            n = (d1 - date.fromisoformat(row["start"])).days
            out["period_days"] = n
            out["period_kind"] = ("연간" if n > 300 else
                                  "분기" if n < 120 else
                                  "누적%d일" % n)
        except Exception:
            pass
    return out


# =====================================================================
# 수집 (네트워크)
# =====================================================================
_CIK_MAP = None


def _fail(reason):
    return {"ok": False, "reason": reason}


def _get_json(url, timeout=30):
    req = urllib.request.Request(url, headers=SEC_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def cik_for(ticker):
    """티커 → CIK. SEC 공식 매핑 1회 로드 후 캐시."""
    global _CIK_MAP
    if _CIK_MAP is None:
        try:
            raw = _get_json(SEC_TICKERS)
            _CIK_MAP = {v["ticker"].upper(): int(v["cik_str"]) for v in raw.values()}
            log.info("SEC 티커 매핑 %d건 로드", len(_CIK_MAP))
        except Exception as e:
            log.warning("SEC 티커 매핑 실패: %s", e)
            _CIK_MAP = {}
    return _CIK_MAP.get(ticker.upper())


def sec_fundamentals(ticker):
    """SEC XBRL 재무. 미국 상장사만. 실패는 사유와 함께 비운다."""
    cik = cik_for(ticker)
    if not cik:
        return _fail("SEC 티커 매핑에 없다(미국 상장사가 아니거나 티커 상이)")
    try:
        g = _get_json(SEC_FACTS % cik).get("facts", {}).get("us-gaap", {})
    except Exception as e:
        return _fail("SEC companyfacts 조회 실패: %s" % e)
    if not g:
        return _fail("us-gaap facts 없음(외국 발행사는 ifrs-full 일 수 있다)")
    out = {"ok": True, "cik": cik}
    for lbl, wl in (("revenue", REV_TAGS), ("operating_income", OP_TAGS), ("net_income", NI_TAGS)):
        t, row = pick_fact(g, wl)
        if not row:
            out[lbl] = _fail("해당 태그 없음")
            continue
        d = {"ok": True, "tag": t, "value_usd": row.get("val"),
             "value_bn": round((row.get("val") or 0) / 1e9, 2), "fp": row.get("fp"),
             "form": row.get("form")}
        d.update(describe_period(row))
        out[lbl] = d
    # ★가장 흔한 오독 방지: 매출 기간이 분기인지 연간인지를 최상단에 복창한다
    rv = out.get("revenue") or {}
    if rv.get("ok"):
        out["revenue_caveat"] = (
            "매출 %.2fB 는 **%s** 수치이고 %s 종료분이다(%d일 지연). "
            "다른 회사·다른 기간과 그대로 비교하지 마라."
            % (rv.get("value_bn") or 0, rv.get("period_kind") or "기간 미상",
               rv.get("end"), rv.get("lag_days") or 0))
    return out


def yf_facts(ticker):
    """yfinance — 밸류에이션·투자의견·공매도·실적일정·뉴스. 항목별로 독립 실패."""
    out = {}
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
    except Exception as e:
        return {"_all": _fail("yfinance 사용 불가: %s" % e)}
    try:
        i = t.info or {}
        out["valuation"] = {
            "ok": True, "name": i.get("shortName"), "currency": i.get("currency"),
            "market_cap": i.get("marketCap"), "per_trailing": i.get("trailingPE"),
            "per_forward": i.get("forwardPE"), "pbr": i.get("priceToBook"),
            "target_mean": i.get("targetMeanPrice"), "target_low": i.get("targetLowPrice"),
            "target_high": i.get("targetHighPrice"),
            "n_analysts": i.get("numberOfAnalystOpinions"),
            "avg_volume": i.get("averageVolume"),
            "beta": i.get("beta"),
        }
        # ★공매도 — 기준일이 불명확하다. 국내처럼 '며칠 지연'을 못 쓴다는 사실을 같이 남긴다.
        out["short"] = {"ok": True, "short_ratio": i.get("shortRatio"),
                        "pct_of_float": i.get("shortPercentOfFloat"),
                        "shares_short": i.get("sharesShort"),
                        "caveat": ("미국 공매도 공시는 격주 단위이고 이 값의 기준일이 "
                                   "명시되지 않는다 — 국내처럼 '며칠 지연'을 쓸 수 없다.")}
    except Exception as e:
        out["valuation"] = _fail("info 조회 실패: %s" % e)
    try:
        r = t.recommendations
        if r is not None and len(r):
            row = r.iloc[0].to_dict()
            tot = sum(int(row.get(k) or 0) for k in
                      ("strongBuy", "buy", "hold", "sell", "strongSell"))
            buy = int(row.get("strongBuy") or 0) + int(row.get("buy") or 0)
            out["reco"] = {"ok": True, "period": row.get("period"), "n": tot,
                           "buy_share_pct": round(buy / tot * 100.0, 1) if tot else None,
                           "detail": {k: row.get(k) for k in
                                      ("strongBuy", "buy", "hold", "sell", "strongSell")}}
        else:
            out["reco"] = _fail("투자의견 추이 없음")
    except Exception as e:
        out["reco"] = _fail("recommendations 실패: %s" % e)
    try:
        c = t.calendar or {}
        ed = c.get("Earnings Date")
        out["earnings"] = {"ok": True, "next_earnings": [str(x) for x in ed] if ed else None,
                           "note": "실적 발표 전후는 갭 위험이 크다(한국 심야에 발생한다)"}
    except Exception as e:
        out["earnings"] = _fail("calendar 실패: %s" % e)
    try:
        n = t.news or []
        items = []
        for a in n[:6]:
            c = a.get("content") or a
            items.append({"title": (c.get("title") or "")[:110],
                          "publisher": ((c.get("provider") or {}).get("displayName")
                                        if isinstance(c.get("provider"), dict)
                                        else a.get("publisher")),
                          "date": c.get("pubDate") or a.get("providerPublishTime")})
        out["news"] = {"ok": True, "n": len(items), "items": items} if items \
            else _fail("뉴스 없음")
    except Exception as e:
        out["news"] = _fail("news 실패: %s" % e)
    return out


def collect_one(ticker, country="US", today=None):
    """해외 1종목 → 사실 dict. ★판정하지 않는다."""
    import portfolio_enrich as pe
    e = {"ticker": ticker, "country": country,
         "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    ser = pe._series_for(ticker, country, today=today)
    e["price"] = pe.price_structure(ser) if ser else _fail("시세 시계열 조회 실패")
    e["relative"] = pe.relative_strength(ser, country)
    e.update(yf_facts(ticker))
    e["fundamentals"] = (sec_fundamentals(ticker) if country == "US"
                         else _fail("SEC 는 미국 상장사만 — 일본은 EDINET(미연동)"))
    e["data_gaps"] = DATA_GAPS
    return e


def market_context():
    """해외 시장 국면 — 국내 VKOSPI 에 대응하는 축(VIX)과 주요 지수."""
    out = {}
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import FinanceDataReader as fdr
        start = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        t = datetime.now().date()
        for lbl, sym in (("vix", "VIX"), ("sp500", "US500"), ("nasdaq", "IXIC"),
                         ("dow", "DJI"), ("sox", "^SOX"), ("nikkei", "N225")):
            try:
                df = fdr.DataReader(sym, start)
                rows = [(ix.date(), float(c)) for ix, c in zip(df.index, df["Close"])
                        if c == c and hasattr(ix, "date") and ix.date() < t]
                if len(rows) < 6:
                    out[lbl] = _fail("표본 부족")
                    continue
                last = rows[-1][1]
                out[lbl] = {"ok": True, "symbol": sym, "asof": str(rows[-1][0]),
                            "close": round(last, 2),
                            "chg_5d_pct": round((last / rows[-6][1] - 1.0) * 100.0, 2),
                            "chg_20d_pct": (round((last / rows[-21][1] - 1.0) * 100.0, 2)
                                            if len(rows) > 21 else None)}
            except Exception as ex:
                out[lbl] = _fail(str(ex)[:60])
    except Exception as e:
        return _fail("FDR 사용 불가: %s" % e)
    return out


def portfolio_foreign_tickers():
    """등록자 보유 중 해외 종목 (티커, 국가) 목록."""
    out = []
    for f in sorted(glob.glob(os.path.join(HERE, "portfolios", "*.csv"))):
        try:
            import portfolio_review as pr
            rows, _w = pr.load_portfolio(f)
        except Exception:
            continue
        for r in rows:
            c = (r.get("country") or "KR").upper()
            if c != "KR" and (r.get("ticker"), c) not in out:
                out.append((r["ticker"], c))
    return out


def main():
    ap = argparse.ArgumentParser(description="해외 종목 사실 수집(★판정하지 않는다)")
    ap.add_argument("--tickers", default=None, help="쉼표구분 (예: NVDA,AAPL)")
    ap.add_argument("--country", default="US", choices=["US", "JP"])
    ap.add_argument("--from-portfolio", action="store_true", help="등록자 보유 해외 종목")
    ap.add_argument("--out", default=None)
    ap.add_argument("--session", default=None)
    args = ap.parse_args()

    targets = []
    if args.tickers:
        targets = [(t.strip().upper(), args.country) for t in args.tickers.split(",") if t.strip()]
    elif args.from_portfolio:
        targets = portfolio_foreign_tickers()
    if not targets:
        log.error("대상이 없다 — --tickers 또는 --from-portfolio 를 써라.")
        return 1

    log.info("대상 %d종: %s", len(targets), ", ".join("%s(%s)" % t for t in targets))
    rows = []
    for i, (tk, c) in enumerate(targets, 1):
        r = collect_one(tk, c)
        # price_structure 는 ok 키를 두지 않는다(실패는 note/ok:false 로 표시) — 둘 다 본다
        oks = [k for k in ("price", "relative", "valuation", "reco", "short",
                           "earnings", "news", "fundamentals")
               if isinstance(r.get(k), dict) and r[k].get("ok") is not False]
        log.info("  %2d/%d %-6s %s", i, len(targets), tk,
                 "성공 " + "·".join(oks) if oks else "전부 실패")
        rows.append(r)
        time.sleep(0.4)     # SEC·yfinance 예의

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "what": "해외(미국·일본) 종목 사실. ★판정 없음 — 추천 규율은 [6.10] 을 따르라.",
        "n": len(rows), "tickers": rows, "market": market_context(),
        "data_gaps": DATA_GAPS,
        "disclaimer": ("무료 공개 데이터(FDR·SEC EDGAR·yfinance) 기반이며 투자자문이 아니다. "
                       "국내 종목보다 확인할 수 있는 축이 적다는 사실 자체가 판단의 입력이다."),
    }
    out = args.out
    if not out:
        sess = args.session
        if not sess:
            c = sorted(p for p in glob.glob(os.path.join(OUTPUT_DIR, "20??-??-??_*"))
                       if os.path.isdir(p) and not os.path.basename(p).startswith("_"))
            sess = c[-1] if c else HERE
        out = os.path.join(sess, "foreign_facts.json")
    from common import save_json_atomic
    save_json_atomic(out, payload)
    log.info("저장: %s", out)
    print("FOREIGN_COLLECT=%d" % len(rows))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
