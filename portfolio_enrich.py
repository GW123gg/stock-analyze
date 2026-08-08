#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
portfolio_enrich.py — 보유 종목별 심층 자료 수집 → portfolio_review_<이메일>.json 에 주입 [v11.22 신규]

[왜 필요했나 — 실측]
  portfolio_review 는 손익·알파·우리추천이력만 준다. 그런데 [7.7] 은 분석가에게 종목마다
  "진단·논지·행동(가격조건)"을 쓰라고 시킨다. 실측 결과 **분석가가 가진 건 손익 숫자와
  사용자 메모뿐**이었다:
    · 보유 8종 중 6종이 watch_tickers 에 없어 force_scores·overheat·short·fundamentals·
      disclosures 세션 파일 **전부 미포함**(그 파일들은 watch 유니버스만 돈다).
    · analyst_reco.json 은 보유 종목 0개 포함, naver_stock_news.json 은 2주 낡음.
    · 가격 시계열이 없어 "45,000 이탈 시 축소" 같은 **가격 조건을 근거 있게 쓸 수 없었다**.
  그래서 보유 종목만 **온디맨드로** 다시 수집한다(세션 유니버스와 무관).

[★사실만 — 판정하지 않는다]
  portfolio_review·holding_review 와 같은 철학이다. 여기서 나오는 건 전부 측정값이고,
  "사라/팔아라"는 분석가가 쓴다. 실패한 소스는 **0 으로 채우지 않고 사유와 함께 비운다**
  (확인 불가 != 값 없음 — hts_capture 와 같은 계약).

[국가별]
  · 한국: 세력강도·과열·공매도잔고·컨센서스/목표가·최근뉴스·공시 오버행 + 가격구조
  · 미국/일본: **국내 전용 소스는 적용 불가**(pykrx/네이버는 국내 종목만) — 가격구조만
    산출하고 나머지는 사유를 남긴다. 분석가가 웹검색으로 메꾼다([7.7] 검색 규율).

[사용]
  python portfolio_enrich.py                    # 등록자 전원, 오늘 세션의 review 에 주입
  python portfolio_enrich.py --email a@b.com
  python portfolio_enrich.py --session output\2026-08-08_063507
  python portfolio_enrich.py --skip-slow        # 세력강도(가장 느림) 생략
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
from datetime import datetime, timedelta

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")

logging.basicConfig(level=logging.INFO, format="[pfenrich] %(message)s")
log = logging.getLogger("pfenrich")

NEWS_N = 6              # 종목당 최근 뉴스 건수
SLEEP_BETWEEN = 0.3     # 소스 호출 간 간격(과호출 방지)


# =====================================================================
# 순수 계산 — 가격 구조(국가 무관)
# =====================================================================
def _rsi14(closes):
    """종가 리스트 → RSI(14). 표본 부족이면 None. 순수함수."""
    if not closes or len(closes) < 15:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    ag = sum(gains[-14:]) / 14.0
    al = sum(losses[-14:]) / 14.0
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    rs = ag / al
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def price_structure(series):
    """[(date, close)] 오름차순 → 가격 구조 dict. 순수함수(네트워크 없음).

    ★'행동'에 가격 조건을 쓰려면 이게 있어야 한다 — 지지·저항 근사와 이동평균.
      마지막 봉은 호출자가 이미 '오늘 제외'로 걸러 넣는다(A40 원칙).
    """
    out = {"n_bars": len(series or [])}
    if not series or len(series) < 5:
        out["note"] = "표본 부족(5봉 미만) — 가격 구조 산출 불가"
        return out
    closes = [c for _d, c in series]
    last = closes[-1]
    out["last_close"] = round(last, 2)

    def ma(n):
        return round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None

    out["ma20"], out["ma60"] = ma(20), ma(60)
    for k in ("ma20", "ma60"):
        v = out.get(k)
        out[k + "_disparity_pct"] = round((last / v - 1.0) * 100.0, 2) if v else None
    win = closes[-252:] if len(closes) >= 60 else closes
    hi, lo = max(win), min(win)
    out["high_52w"], out["low_52w"] = round(hi, 2), round(lo, 2)
    out["dist_from_high_pct"] = round((last / hi - 1.0) * 100.0, 2) if hi else None
    out["dist_from_low_pct"] = round((last / lo - 1.0) * 100.0, 2) if lo else None
    # 최근 20봉 범위 = 단기 지지·저항 근사(분석가가 가격 조건을 잡을 앵커)
    r20 = closes[-20:]
    out["range20_low"], out["range20_high"] = round(min(r20), 2), round(max(r20), 2)
    out["rsi14"] = _rsi14(closes)
    for lbl, n in (("ret_5d_pct", 5), ("ret_20d_pct", 20), ("ret_60d_pct", 60)):
        out[lbl] = (round((last / closes[-n - 1] - 1.0) * 100.0, 2)
                    if len(closes) > n else None)
    return out


def _fail(reason):
    """실패를 값으로 남긴다 — 0 으로 채우지 않는다."""
    return {"ok": False, "reason": reason}


def match_underlying(name, keys):
    """보유 종목명 → deriv_sentiment.by_underlying 의 키. 못 찾으면 None. 순수함수.

    ★KRX 는 기초자산명을 5~6자로 자른다('두산에너빌리티'→'두산에너빌'). 그래서 접두 매칭이
      필요한데, 순진하게 하면 **다른 회사를 붙인다**('한국전력' 키가 '한국전력기술' 보유에
      매칭되는 식). 그래서 두 가지만 인정한다:
        (1) 완전일치
        (2) 키가 이름의 접두이면서 **키가 5자 이상**(=잘린 흔적)이고 길이차 4자 이내
      '한국전력'(4자)은 잘린 게 아니라 온전한 이름이므로 (2)에 안 걸린다 — 의도한 것이다.
    """
    if not name:
        return None
    for k in keys:
        if k == name:
            return k
    for k in keys:
        if len(k) >= 5 and name.startswith(k) and 0 < len(name) - len(k) <= 4:
            return k
    return None


def enrich_options(name, deriv=None):
    """개별주식옵션 수급 [v11.22] — ★남의 포지션을 읽는 지표이지 매매 권유가 아니다.

    보유 종목에 개별주식옵션이 상장돼 있으면 콜/풋 거래량·미결제와 PCR 을 붙인다.
    풋 거래가 급증했다면 '누군가 이 종목의 하락에 돈을 걸거나 헤지하고 있다'는 뜻이다.

    ★한계(반드시 함께 적어야 한다): 이 수치는 **기초자산 합산**이다. 행사가·월물별 호가는
      알 수 없으므로 "살 수 있다"의 근거가 되지 못한다.
    """
    d = deriv
    if d is None:
        try:
            with open(os.path.join(HERE, "deriv_sentiment.json"), encoding="utf-8-sig") as f:
                d = json.load(f)
        except Exception as e:
            return _fail("deriv_sentiment.json 없음/오류: %s" % e)
    bu = (d or {}).get("by_underlying") or {}
    if not bu:
        return _fail("개별주식옵션 데이터 없음")
    k = match_underlying(name, list(bu))
    if not k:
        return {"ok": False, "listed": False,
                "reason": "개별주식옵션 미상장(기초자산 %d종에 없다)" % len(bu)}
    v = dict(bu[k])
    v.update({"ok": True, "listed": True, "underlying_key": k,
              "trade_date": (d or {}).get("trade_date"),
              "caveat": ("기초자산 합산 수치다. 행사가·월물별 호가는 확인하지 못했으므로 "
                         "체결 가능성의 근거로 쓰지 마라. 종목명 접두 매칭('%s'←'%s')이므로 "
                         "동명이형 오인 가능성을 한 번 확인하라." % (k, name))})
    return v


def _index_series(country, today=None):
    """그 나라 벤치마크 지수 종가 [(date, close)] — 오늘 봉 제외. 프로세스 내 캐시."""
    if country in _IDX_CACHE:
        return _IDX_CACHE[country]
    out = []
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import FinanceDataReader as fdr
        import portfolio_review as pr
        bench = pr.country_meta(country)["bench"]
        df = fdr.DataReader(bench, (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d"))
        t = today or datetime.now().date()
        for ix, c in zip(df.index, df["Close"].tolist()):
            if c == c and c and hasattr(ix, "date") and ix.date() < t:
                out.append((ix.date(), float(c)))
    except Exception as e:
        log.debug("지수 조회 실패 %s: %s", country, e)
    _IDX_CACHE[country] = out
    return out


def relative_strength(stock_series, country):
    """★[v11.22] 매수일시 없이도 '종목 탓인가 시장 탓인가'를 답한다.

    [왜] portfolio_review 의 `alpha_pct` 는 매수일 이후 지수수익률이 있어야 계산되는데,
      [7.7] 은 "불타기·물타기 때문에 매수일시는 비는 게 정상"이라고 못박는다. 그래서
      **알파가 구조적으로 항상 null** 이었다(실측: 보유 8종 전부 null). 지시문이 "알파로
      판단하라"고 시키면서 알파를 못 만드는 자기모순이었다.
      보유기간 알파는 매수일 없이 만들 수 없다 — 대신 **고정 창(5·20·60거래일)** 의
      초과수익을 준다. "최근 한 달 이 종목이 시장을 이겼나"는 답할 수 있는 질문이다.
      ★이건 보유기간 알파가 아니다. 그렇게 부르지 마라.
    """
    idx = _index_series(country)
    out = {"bench_country": country, "window": "고정 거래일 창(보유기간 아님)"}
    if not idx or not stock_series or len(idx) < 61 or len(stock_series) < 6:
        out["ok"] = False
        out["reason"] = "지수 또는 종목 표본 부족 — 상대강도 산출 불가"
        return out
    # ★같은 '봉 번호'가 아니라 같은 **날짜**로 맞춘다. 거래정지·신규상장 종목은 봉 수가
    #   지수와 달라서, 위치로 맞추면 서로 다른 기간을 비교하게 된다(조용한 오답).
    imap = dict(idx)
    idates = [d for d, _c in idx]

    def _idx_on_or_before(d):
        lo, hi = 0, len(idates) - 1
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if idates[mid] <= d:
                best = idates[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        return imap.get(best) if best is not None else None

    s_last_d, s_last_c = stock_series[-1]
    i_last = _idx_on_or_before(s_last_d)
    if not i_last:
        out["ok"] = False
        out["reason"] = "종목 최신 봉 날짜(%s)에 대응하는 지수 봉이 없다" % s_last_d
        return out
    out["ok"] = True
    out["asof"] = str(s_last_d)
    for lbl, n in (("5d", 5), ("20d", 20), ("60d", 60)):
        if len(stock_series) > n:
            d0, c0 = stock_series[-n - 1]
            i0 = _idx_on_or_before(d0)
            if i0:
                s = (s_last_c / c0 - 1.0) * 100.0
                i = (i_last / i0 - 1.0) * 100.0
                out["stock_%s_pct" % lbl] = round(s, 2)
                out["index_%s_pct" % lbl] = round(i, 2)
                out["excess_%s_pct" % lbl] = round(s - i, 2)
                out["from_%s" % lbl] = str(d0)
                continue
        out["excess_%s_pct" % lbl] = None
    return out


_IDX_CACHE = {}


# =====================================================================
# 수집 (네트워크)
# =====================================================================
def _series_for(ticker, country, today=None):
    """[(date, close)] — 오늘 봉 제외. 국가별 심볼로 FDR 조회."""
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import FinanceDataReader as fdr
        import portfolio_review as pr
        sym = pr.market_symbol(ticker, country)
        start = (datetime.now() - timedelta(days=420)).strftime("%Y-%m-%d")
        df = fdr.DataReader(sym, start)
        t = (today or datetime.now().date())
        out = []
        for ix, c in zip(df.index, df["Close"].tolist()):
            if c == c and c and hasattr(ix, "date") and ix.date() < t:
                out.append((ix.date(), float(c)))
        return out
    except Exception as e:
        log.debug("시세 조회 실패 %s: %s", ticker, e)
        return []


def enrich_kr(ticker, name, skip_slow=False):
    """국내 종목 전용 소스. 각 항목은 독립 — 하나 실패해도 나머지는 채운다."""
    out = {}

    if not skip_slow:
        try:
            import force_analysis as fa
            r = fa.analyze_ticker(ticker)
            if r.get("error"):
                out["force"] = _fail(r["error"])
            else:
                d = r.get("detail") or {}
                out["force"] = {"ok": True, "score": r.get("force_score"),
                                "label": r.get("label"),
                                "supply": d.get("supply"), "obv": d.get("obv"),
                                "rsi": d.get("rsi"), "vol_ratio": d.get("vol_ratio"),
                                "foreign_5d": d.get("foreign_5d"),
                                "foreign_20d": d.get("foreign_20d"),
                                "inst_5d": d.get("inst_5d"),
                                "supply_source": d.get("supply_source")}
        except Exception as e:
            out["force"] = _fail("%s: %s" % (type(e).__name__, e))
        time.sleep(SLEEP_BETWEEN)

    try:
        import overheat_collect as oc
        r = oc.compute_overheat(ticker, name or "")
        out["overheat"] = ({"ok": True, "score": r.get("overheat_score"),
                            "label": r.get("overheat_label"),
                            "disparity20": r.get("disparity20"),
                            "up_streak": r.get("up_streak"),
                            "dist_52w_high_pct": r.get("dist_52w_high_pct"),
                            "obv_divergence": r.get("obv_divergence"),
                            "signals": r.get("signals")}
                           if r.get("source") not in (None, "none")
                           else _fail("과열 산출 실패: %s" % (r.get("notes") or "")))
    except Exception as e:
        out["overheat"] = _fail("%s: %s" % (type(e).__name__, e))
    time.sleep(SLEEP_BETWEEN)

    # ★공매도 — 사용자 요청(v11.22): 보유 종목의 공매도 압력을 설명 가능하게
    try:
        import short_collect as sc
        r = sc.compute_short(ticker, name or "")
        out["short"] = ({"ok": True,
                         "balance_ratio": r.get("short_balance_ratio"),
                         "change_10d": r.get("balance_change_10d"),
                         "trend": r.get("trend"),
                         "pressure_score": r.get("short_pressure_score"),
                         "pressure_label": r.get("short_pressure_label"),
                         "signals": r.get("signals"), "asof": r.get("asof")}
                        if r.get("source") not in (None, "none")
                        # ★'비대상'이라고 단정하면 안 된다 — KRX 차단·공표지연·실제 비대상이
                        #   전부 같은 빈 응답으로 온다(2026-08-08 실측: IP 1일 차단).
                        else _fail("KRX 공매도 잔고를 받지 못했다(차단·공표지연·비대상 구분 불가)"))
    except Exception as e:
        out["short"] = _fail("%s: %s" % (type(e).__name__, e))
    time.sleep(SLEEP_BETWEEN)

    try:
        import analyst_reco as ar
        r = ar.fetch_ticker(ticker)
        out["consensus"] = ({"ok": True, **(r.get("consensus") or {}),
                             "reports": (r.get("reports") or [])[:3]}
                            if r else _fail("컨센서스 조회 실패(비커버 종목일 수 있음)"))
    except Exception as e:
        out["consensus"] = _fail("%s: %s" % (type(e).__name__, e))
    time.sleep(SLEEP_BETWEEN)

    try:
        import naver_stock_news as nsn
        arts = nsn.fetch_ticker(ticker, NEWS_N)
        out["news"] = ({"ok": True, "n": len(arts), "items": arts}
                       if arts else _fail("최근 뉴스 없음/조회 실패"))
    except Exception as e:
        out["news"] = _fail("%s: %s" % (type(e).__name__, e))
    time.sleep(SLEEP_BETWEEN)
    return out


def enrich_disclosure(ticker, name, corp_map, key):
    """공시 오버행 — corp_map·DART 키가 있을 때만."""
    if not key or not corp_map:
        return _fail("DART 키/corp_map 없음 — 공시 확인 불가")
    try:
        import disclosure_collect as dc
        corp = corp_map.get(ticker)
        if not corp:
            return _fail("corp_code 미상(비상장·코드 불일치)")
        r = dc.fetch_disclosures(key, corp, ticker, name or "", 90)
        if not r or "overhang_score" not in r:
            return _fail("공시 조회 실패: %s" % str(r)[:60])
        return {"ok": True, "overhang_score": r.get("overhang_score"),
                "flags": r.get("overhang_flags"), "recent": (r.get("recent") or [])[:5]}
    except Exception as e:
        return _fail("%s: %s" % (type(e).__name__, e))


def enrich_position(pos, corp_map=None, dart_key=None, skip_slow=False, deriv=None):
    """보유 1건 → enrich dict. 국가별로 가능한 것만 채운다."""
    tk = pos.get("ticker")
    country = pos.get("country") or "KR"
    name = pos.get("name") or ""
    e = {"collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         "country": country}

    ser = _series_for(tk, country)
    e["price"] = price_structure(ser) if ser else _fail("시세 시계열 조회 실패")
    # 매수일시 없이도 답할 수 있는 '종목 탓 vs 시장 탓' — 보유기간 알파의 대체가 아니라 보완
    e["relative"] = relative_strength(ser, country)

    if country == "KR":
        e.update(enrich_kr(tk, name, skip_slow=skip_slow))
        e["disclosure"] = enrich_disclosure(tk, name, corp_map, dart_key)
        e["options"] = enrich_options(name, deriv)
    else:
        # ★국내 전용 소스는 해외 종목에 쓸 수 없다 — 조용히 비우지 말고 이유를 남긴다.
        why = ("%s 종목 — 국내 전용 소스(pykrx 수급·공매도잔고, 네이버 컨센서스·뉴스, "
               "DART 공시)는 적용 불가. 분석가가 웹검색으로 보강해야 한다."
               % (pos.get("country_name") or country))
        for k in ("force", "overheat", "short", "consensus", "news",
                  "disclosure", "options"):
            e[k] = _fail(why)
    return e


# =====================================================================
# IO
# =====================================================================
def _review_files(session, email=None):
    pat = "portfolio_review_%s.json" % (email or "*")
    return sorted(glob.glob(os.path.join(session, pat)))


def latest_session():
    c = sorted(p for p in glob.glob(os.path.join(OUTPUT_DIR, "20??-??-??_*"))
               if os.path.isdir(p) and not os.path.basename(p).startswith("_"))
    return c[-1] if c else None


def main():
    ap = argparse.ArgumentParser(description="보유 종목별 심층 자료 수집(★판정하지 않는다)")
    ap.add_argument("--session", default=None)
    ap.add_argument("--email", default=None, help="이 사람만")
    ap.add_argument("--skip-slow", action="store_true", help="세력강도 생략(가장 느림)")
    ap.add_argument("--limit", type=int, default=40, help="종목 수 상한(과호출 방지)")
    args = ap.parse_args()

    sess = args.session or latest_session()
    if not sess or not os.path.isdir(sess):
        log.error("세션을 찾지 못했다 — --session 으로 지정하라.")
        return 1
    files = _review_files(sess, args.email)
    if not files:
        log.error("%s 에 portfolio_review_*.json 이 없다 — "
                  "먼저 python portfolio_review.py --all 을 돌려라.", sess)
        return 1

    # DART 는 한 번만 준비(대용량 corp_map)
    # 개별주식옵션 수급은 루트 파일 하나를 전 종목이 공유한다 — 한 번만 읽는다
    deriv = None
    try:
        with open(os.path.join(HERE, "deriv_sentiment.json"), encoding="utf-8-sig") as fh:
            deriv = json.load(fh)
        log.info("개별주식옵션 기초자산 %d종 로드(trade_date=%s)",
                 len(deriv.get("by_underlying") or {}), deriv.get("trade_date"))
    except Exception as e:
        log.info("deriv_sentiment.json 없음 — 옵션 수급은 '확인 불가'로 남는다: %s", e)

    corp_map, dart_key = None, None
    try:
        import dart_collect as dcol
        dart_key = dcol.load_dart_key()
        if dart_key:
            corp_map = dcol.load_corp_map(dart_key)
            log.info("DART corp_map %d건 로드", len(corp_map or {}))
        else:
            log.info("DART 키 없음 — 공시 오버행은 '확인 불가'로 남는다(정상)")
    except Exception as e:
        log.warning("DART 준비 실패(공시 생략): %s", e)

    total = 0
    for f in files:
        try:
            with open(f, encoding="utf-8-sig") as fh:
                d = json.load(fh)
        except Exception as e:
            log.error("%s 읽기 실패: %s", os.path.basename(f), e)
            continue
        poss = d.get("positions") or []
        if not poss:
            log.info("%s — 보유 0종(건너뜀)", d.get("email"))
            continue
        log.info("%s — %d종 수집 시작%s", d.get("email"), len(poss),
                 " (세력강도 생략)" if args.skip_slow else "")
        for i, p in enumerate(poss[:args.limit], 1):
            e = enrich_position(p, corp_map, dart_key, args.skip_slow, deriv)
            p["enrich"] = e
            oks = [k for k in ("price", "relative", "force", "overheat", "short",
                               "consensus", "news", "disclosure", "options")
                   if isinstance(e.get(k), dict) and e[k].get("ok") is not False]
            log.info("  %2d/%d %-14s %s", i, len(poss), (p.get("name") or "")[:12],
                     "성공 " + "·".join(oks) if oks else "전부 실패")
            total += 1
        d["enriched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        d["enrich_note"] = (
            "보유 종목만 온디맨드로 수집한 **사실**이다(세션 watch 유니버스와 무관). "
            "판정은 분석가가 쓴다. ok=false 항목은 '확인 불가'이지 '값 없음'이 아니다 — "
            "0 으로 읽지 말고 그 근거를 쓰지 마라. 해외 종목은 국내 전용 소스가 "
            "구조적으로 불가하니 웹검색으로 보강하라.")
        try:
            from common import save_json_atomic
            save_json_atomic(f, d)
            log.info("  저장: %s", os.path.basename(f))
        except Exception as e:
            log.error("  저장 실패: %s", e)

    log.info("완료 — %d종목 수집", total)
    print("PORTFOLIO_ENRICH=%d" % total)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
