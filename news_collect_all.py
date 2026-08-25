#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""news_collect_all.py — 뉴스 5종을 한 번에 돌리고 **종목별로 묶어** 세션에 저장한다.

[왜 이 파일이 생겼나 — 2026-08-25 실측]
  뉴스 수집기 5종(naver_stock_news · news_rss_collect · media_rss_collect ·
  yahoo_news · gdelt_collect)은 전부 **멀쩡히 동작**하는데, 호출부가
  **지시문 문서에만** 있었다. run_signals.py 에도 research_agent.py 에도 없다.
  그래서 아침 Cowork 가 멈춘 뒤로 아무도 안 돌렸고, 루트 산출물이 이렇게 늙었다:
      news_rss 08-03 · naver_stock_news 07-25 · media_rss/yahoo/gdelt 06-25~26
  분석가는 "각 회사의 호재·악재"를 쓰라는 지시([7] 확실한 호재·악재 섹션)를 받지만
  **입력이 한 달 넘게 낡은 파일**이었다. 차단당한 게 아니라 아무도 안 돌린 것이다.

[무엇을 하나]
  1) 종목 목록 = 등록된 사람들의 국내 보유 + watch_tickers.txt (중복 제거)
  2) 수집기 5종을 각각 **별도 프로세스**로 실행(하나가 죽어도 나머지는 산다)
  3) 결과를 읽어 **종목별로 묶어** 세션에 news_bundle.json 저장
     - 종목별 기사: naver(종목코드 직결) + yahoo + 제목에 종목명이 들어간 rss/media
     - 시장 전반: media_rss / gdelt / 종목에 안 붙은 rss
  4) 종목 커버리지가 낮으면 **Gemini 폴백**(api_collect.py --no-naver)을 돌려
     세션의 광역수집 마크다운을 보강한다(크롤링 차단 대비 경로).

[안 하는 것]
  ★호재/악재 판정은 하지 않는다. 그건 분석가가 기사를 읽고 쓴다 —
    여기서 기계가 감성분류를 하면 근거 없는 라벨이 리포트에 실린다.

[사용법]
  python news_collect_all.py                     # 오늘 세션에 수집
  python news_collect_all.py --session output\2026-08-25_0620
  python news_collect_all.py --n 10 --no-gemini  # 종목당 기사 수 / Gemini 생략
  python news_collect_all.py --check             # 실행 없이 현재 상태만
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime

from common import save_json_atomic, resolve_session, suppress_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
PY = sys.executable

OUT_NAME = "news_bundle.json"

# 종목별 뉴스가 이 비율에 못 미치면 Gemini 폴백을 돌린다.
#   ★"기사 0건"이 곧 "뉴스 없음"은 아니다 — 차단·개편으로 조용히 0 이 될 수 있다.
COVERAGE_FLOOR = 0.60

# 야후·GDELT 는 느리다(야후=종목당 왕복, GDELT=5.3s rate-limit).
#   전 종목에 돌리면 아침 예산을 다 먹는다 — 대표 종목만.
YAHOO_MAX = 6
GDELT_MAX = 3


# ────────────────────────────── 종목·이름 ──────────────────────────────
def _watch_map():
    """watch_tickers.txt → {코드: 이름}. 이름은 '# 뒤 주석'에서 공짜로 얻는다."""
    out = {}
    p = os.path.join(HERE, "watch_tickers.txt")
    if not os.path.isfile(p):
        return out
    with io.open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            code, _, comment = line.partition("#")
            code = code.strip()
            if len(code) == 6 and code.isdigit() and code != "000000":
                out.setdefault(code, comment.strip() or code)
    return out


def _portfolio_map():
    """등록된 사람들의 **국내** 보유 {코드: 이름}.

    ★보유 종목이야말로 뉴스가 가장 급한 종목인데, 대부분 watch_tickers 밖이다
      (2026-08-08 실측: 보유 8종 중 6종이 세션 신호에 아예 없었다).
    ★남의 보유가 섞이지만 이 목록은 '무엇을 수집할까'에만 쓰인다 —
      리포트·메일에는 사람별로 분리된 자료만 나간다.
    """
    out = {}
    try:
        with suppress_stdout():          # pykrx 가 로그인 실패 문구를 stdout 에 뱉는다
            import portfolio_review as pr
            people = pr.list_people()
    except Exception:                    # noqa: BLE001
        return out
    for _email, path in people:
        try:
            with suppress_stdout():
                positions, _w = pr.load_portfolio(path)
        except Exception:                # noqa: BLE001
            continue
        for pos in positions:
            if (pos.get("country") or "KR") != "KR":
                continue                 # 해외는 naver 종목뉴스로 못 본다
            t = str(pos.get("ticker") or "").zfill(6)
            if len(t) == 6 and t.isdigit():
                out.setdefault(t, str(pos.get("name") or t))
    return out


def resolve_tickers(limit=28):
    """보유 먼저, 그다음 관심종목. 순서가 곧 우선순위다."""
    names = {}
    names.update(_portfolio_map())       # 보유가 앞
    for k, v in _watch_map().items():
        names.setdefault(k, v)
    order = list(names.keys())[:limit]
    return order, {k: names[k] for k in order}


# ────────────────────────────── 실행 ──────────────────────────────
def run(script, argv, timeout, log):
    """수집기 하나를 별도 프로세스로. (ok, 초, 사유)"""
    t0 = time.time()
    try:
        r = subprocess.run([PY, "-X", "utf8", os.path.join(HERE, script)] + argv,
                           cwd=HERE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        took = time.time() - t0
        tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
        log.append({"script": script, "rc": r.returncode,
                    "sec": round(took, 1), "last": tail[0][:160]})
        return r.returncode == 0, took, tail[0][:160]
    except subprocess.TimeoutExpired:
        log.append({"script": script, "rc": -1, "sec": timeout, "last": "timeout"})
        return False, time.time() - t0, "timeout"
    except Exception as e:               # noqa: BLE001
        log.append({"script": script, "rc": -1, "sec": 0, "last": type(e).__name__})
        return False, time.time() - t0, type(e).__name__


def load_root(name):
    p = os.path.join(HERE, name)
    try:
        with io.open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d, os.path.getmtime(p)
    except Exception:                    # noqa: BLE001
        return {}, 0.0


def _fresh(mtime, started):
    """이번 실행으로 갱신됐나. ★'파일 존재'는 성공이 아니다(CLAUDE.md)."""
    return mtime >= started - 1.0


def _art(a, origin):
    return {"title": (a.get("title") or "").strip(),
            "source": (a.get("source") or "").strip(),
            "date": (a.get("date") or "").strip(),
            "link": (a.get("link") or "").strip(),
            "origin": origin}


def collect(session, n, tickers, names, use_gemini, log):
    started = time.time()
    ok_src, bad_src = [], []

    def mark(name, ok, why=""):
        (ok_src if ok else bad_src).append(name if ok else "%s(%s)" % (name, why))

    # 1) 네이버 종목뉴스 — 종목코드로 직접 붙는 유일한 소스. 가장 중요하다.
    ok, _, why = run("naver_stock_news.py",
                     ["--tickers", ",".join(tickers), "--n", str(n)], 600, log)
    mark("naver", ok, why)

    # 2) 구글뉴스 RSS — 종목명 키워드
    kw = [names[t] for t in tickers[:12] if names.get(t)]
    ok, _, why = run("news_rss_collect.py",
                     ["--keywords", ",".join(kw), "--n", str(n), "--lang", "ko"], 600, log)
    mark("google_rss", ok, why)

    # 3) 언론사 직접 RSS — 시장 전반(종목 무관)
    ok, _, why = run("media_rss_collect.py", ["--n", "30"], 600, log)
    mark("media_rss", ok, why)

    # 4) 야후 — 외신 시각. 느려서 대표 종목만.
    ok, _, why = run("yahoo_news.py",
                     ["--tickers", ",".join(tickers[:YAHOO_MAX]), "--n", "5"], 600, log)
    mark("yahoo", ok, why)

    # 5) GDELT — 글로벌. 429 는 정상 동작(부분 성공 허용).
    gq = [names[t] for t in tickers[:GDELT_MAX] if names.get(t)]
    ok, _, why = run("gdelt_collect.py",
                     ["--queries", ",".join(gq), "--n", "8", "--timespan", "2d"], 600, log)
    mark("gdelt", ok, why)

    # ── 읽어서 종목별로 묶는다 ─────────────────────────────────────
    nav, nav_mt = load_root("naver_stock_news.json")
    rss, rss_mt = load_root("news_rss.json")
    med, med_mt = load_root("media_rss.json")
    yah, yah_mt = load_root("yahoo_news.json")
    gde, gde_mt = load_root("gdelt_news.json")

    stale = [n_ for n_, mt in (("naver", nav_mt), ("google_rss", rss_mt),
                               ("media_rss", med_mt), ("yahoo", yah_mt),
                               ("gdelt", gde_mt)) if not _fresh(mt, started)]

    by_ticker = {}
    for t in tickers:
        by_ticker[t] = {"name": names.get(t, t), "articles": []}

    if _fresh(nav_mt, started):
        for t, arts in (nav.get("by_ticker") or {}).items():
            t = str(t).zfill(6)
            if t in by_ticker:
                by_ticker[t]["articles"] += [_art(a, "naver") for a in (arts or [])]
    if _fresh(yah_mt, started):
        for t, arts in (yah.get("by_ticker") or {}).items():
            t = str(t).zfill(6)
            if t in by_ticker:
                by_ticker[t]["articles"] += [_art(a, "yahoo") for a in (arts or [])]

    # 키워드/시장 스트림은 제목에 종목명이 있으면 그 종목에 붙인다.
    market = []
    name_pairs = [(t, names[t]) for t in tickers if names.get(t) and len(names[t]) >= 2]

    def spread(bucket, origin):
        for _k, arts in (bucket or {}).items():
            for a in (arts or []):
                item = _art(a, origin)
                hit = [t for t, nm in name_pairs if nm and nm in item["title"]]
                if hit:
                    for t in hit:
                        by_ticker[t]["articles"].append(item)
                else:
                    market.append(item)

    if _fresh(rss_mt, started):
        spread(rss.get("by_keyword"), "google_rss")
    if _fresh(med_mt, started):
        spread(med.get("by_source"), "media_rss")
    if _fresh(gde_mt, started):
        spread(gde.get("by_query"), "gdelt")

    # 같은 기사가 여러 소스로 들어온다 — 링크(없으면 제목)로 한 건 취급.
    #   ★같은 호재를 두 번 봤다고 강도를 두 배로 매기면 안 된다([4.x] 중복 규율).
    for t, blk in by_ticker.items():
        seen, uniq = set(), []
        for a in blk["articles"]:
            k = a["link"] or a["title"]
            if k and k not in seen:
                seen.add(k)
                uniq.append(a)
        blk["articles"] = uniq
        blk["n"] = len(uniq)
    seen, uniq = set(), []
    for a in market:
        k = a["link"] or a["title"]
        if k and k not in seen:
            seen.add(k)
            uniq.append(a)
    market = uniq

    n_with = sum(1 for b in by_ticker.values() if b["n"] > 0)
    cov = (n_with / len(tickers)) if tickers else 0.0

    # ── Gemini 폴백 ────────────────────────────────────────────────
    #   ★크롤링이 조용히 막히면 기사 수가 0 이 된다. 그때를 위한 다른 경로다.
    gem = {"ran": False, "reason": ""}
    if use_gemini and cov < COVERAGE_FLOOR:
        gem["reason"] = "종목 커버리지 %.0f%% < %.0f%%" % (cov * 100, COVERAGE_FLOOR * 100)
        ok, sec, why = run("api_collect.py",
                           ["--session", session, "--no-naver", "--no-deep"], 900, log)
        gem.update(ran=True, ok=ok, sec=round(sec, 1), detail=why,
                   note="Gemini 결과는 세션의 광역수집 마크다운(01_broad_collection.md)에 들어간다")
    elif use_gemini:
        gem["reason"] = "커버리지 %.0f%% — 폴백 불필요" % (cov * 100)
    else:
        gem["reason"] = "--no-gemini"

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tz": "KST",
        "source": "news_collect_all",
        "session": os.path.basename(session),
        "n_tickers": len(tickers),
        "n_tickers_with_news": n_with,
        "coverage_pct": round(cov * 100, 1),
        "total_articles": sum(b["n"] for b in by_ticker.values()) + len(market),
        "sources_ok": ok_src,
        "sources_failed": bad_src,
        "sources_stale": stale,
        "gemini_fallback": gem,
        "by_ticker": by_ticker,
        "market": market,
        "runs": log,
        "_note": ("종목별 기사 묶음. ★호재/악재 판정은 담기지 않는다 — 분석가가 "
                  "기사를 읽고 직접 쓴다. sources_stale 에 이름이 있으면 그 소스는 "
                  "이번 실행으로 갱신되지 않은 것이니 '뉴스 없음'으로 읽지 마라."),
    }


def main():
    ap = argparse.ArgumentParser(description="뉴스 5종 일괄 수집 + 종목별 묶음")
    ap.add_argument("--session", default=None)
    ap.add_argument("--n", type=int, default=8, help="종목당 기사 수")
    ap.add_argument("--limit-tickers", type=int, default=28)
    ap.add_argument("--no-gemini", action="store_true", help="Gemini 폴백 생략")
    ap.add_argument("--check", action="store_true", help="실행 없이 상태만")
    a = ap.parse_args()

    session = a.session
    if session and not os.path.isabs(session):
        session = os.path.join(HERE, session)
    if not session:
        session = resolve_session(OUTPUT_DIR)
    if not session or not os.path.isdir(session):
        print("[news] 세션을 찾지 못했다 — research_agent.py collect 를 먼저 돌려라.")
        return 1

    tickers, names = resolve_tickers(a.limit_tickers)
    if not tickers:
        print("[news] 종목 목록이 비었다 — watch_tickers.txt 를 확인하라.")
        return 1

    print("[news] 세션: %s" % os.path.basename(session))
    print("[news] 종목 %d종(보유 우선) · 종목당 %d건" % (len(tickers), a.n))

    if a.check:
        for nm in ("naver_stock_news.json", "news_rss.json", "media_rss.json",
                   "yahoo_news.json", "gdelt_news.json"):
            _d, mt = load_root(nm)
            age = "(없음)" if not mt else datetime.fromtimestamp(mt).strftime("%m-%d %H:%M")
            print("   %-24s %s" % (nm, age))
        return 0

    log = []
    doc = collect(session, a.n, tickers, names, not a.no_gemini, log)
    save_json_atomic(os.path.join(session, OUT_NAME), doc)

    print("[news] 성공 소스: %s" % (", ".join(doc["sources_ok"]) or "없음"))
    if doc["sources_failed"]:
        print("[news] 실패 소스: %s" % ", ".join(doc["sources_failed"]))
    if doc["sources_stale"]:
        print("[news] ★갱신 안 됨(과거 파일): %s — '뉴스 없음'으로 읽지 마라"
              % ", ".join(doc["sources_stale"]))
    g = doc["gemini_fallback"]
    print("[news] Gemini 폴백: %s (%s)" % ("실행" if g.get("ran") else "생략", g.get("reason")))
    print("[news] 종목 커버리지 %d/%d (%.0f%%) · 기사 %d건"
          % (doc["n_tickers_with_news"], doc["n_tickers"],
             doc["coverage_pct"], doc["total_articles"]))
    print("[news] 저장: %s" % os.path.join(session, OUT_NAME))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
