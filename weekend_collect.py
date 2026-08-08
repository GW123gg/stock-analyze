#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weekend_collect.py — 주말·휴장일 전용 이슈 수집 + 포트폴리오 [v11.22 신규]

[왜 따로 만들었나 — 실측된 오염]
  예전엔 주말에도 아침 파이프라인이 통째로 돌았다(2026-08-08 토요일에 수집→분석→발송 완료).
  주말 수집기는 **실패하지 않는다** — 금요일 값을 새 파일로 다시 구워 신선도 게이트를
  통과한다(verdict 가 mtime 기준이라 내용이 같아도 OK). 그 결과:
    · 토요일 predictions.json 발행 → 다음 거래일 발행분과 **같은 정산 창**을 본다.
      accuracy_tracker 주석 실측: 중복 그룹 14개·초과 엔트리 18건, entry_ref 최대 -8.5% 괴리.
      accuracy_log 는 append-only 라 한 번 들어간 오염은 뺄 수 없다.
    · intraday_review 가 토요일 11:01 에 "KRX 정규장 개장, 주문 가능"을 출력했다.
    · 루트 국면 신호가 금요일 값으로 덮이며 generated_at 만 토요일 → 회고가 잘못된 빈티지를 본다.
    · 포트폴리오 메일이 금요일과 **똑같은 내용**으로 또 나갔다.

[그래서 이 스크립트가 안 건드리는 것 — 설계의 핵심]
  · `predictions.json`  ★picks/shorts/market_call 키를 **쓰지 않는다**. 산출은
    `weekend_issues.json` 이고 스키마가 달라서 accuracy_tracker/retro_label 이 집어가지 않는다.
  · `output/<날짜>_<시각>/`  → **`output/_weekend_<날짜>/`** 를 쓴다. 언더스코어 접두라
    common.resolve_session 이 후보에서 자동 제외한다(평일 세션과 절대 안 섞인다).
  · 루트 국면 신호 6종(deriv_sentiment·ecos_macro·market_caution·vkospi·credit_balance·
    night_futures) — 재수집하지 않는다.
  · `accuracy_log.json`·`night_calls.jsonl`·`recommended_history.json`·`trade_plan.json`
  · `intraday_review.py` (요일을 봐도 주말엔 장 자체가 없다)
  · 9명 리서치 메일 경로와 dedup 인덱스

[하는 것]
  1. 이슈 수집 — RSS·네이버 종목뉴스·해외매체·GDELT (시장 휴장과 무관하게 들어온다)
  2. 실적 캘린더 — earnings 는 **미래 일정**이라 주말에 특히 쓸모 있다
  3. 포트폴리오 — sync → review → enrich (세션을 명시적으로 고정)
  분석과 발송은 코워크(분석가)가 이 산출물을 읽고 한다.

[사용]
  python weekend_collect.py               # 전 과정
  python weekend_collect.py --check       # 계획만
  python weekend_collect.py --skip-portfolio
  python weekend_collect.py --allow-trading-day   # 거래일에 강행(테스트용)
"""
from __future__ import annotations

import os
import sys
import json
import time
import argparse
import subprocess
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from common import save_json_atomic, trading_day_status

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
PY = sys.executable

# ★언더스코어 접두 — common.resolve_session 이 자동 제외한다. 평일 세션과 절대 안 섞인다.
SESSION_PREFIX = "_weekend_"

# (key, 설명, argv, 산출 파일명, 필수)
#   전부 --out 으로 주말 폴더에 쓴다 — 루트 파일을 덮지 않는다.
STEPS = [
    ("rss",      "국내외 RSS 뉴스",     ["news_rss_collect.py"],   "news_rss.json",         True),
    ("media",    "해외 매체 RSS",       ["media_rss_collect.py"],  "media_rss.json",        False),
    ("gdelt",    "GDELT 글로벌 이슈",   ["gdelt_collect.py"],      "gdelt_news.json",       False),
    ("stocknews", "보유·관심 종목 뉴스", ["naver_stock_news.py"],   "naver_stock_news.json", False),
    ("reco",     "애널리스트 컨센서스", ["analyst_reco.py"],       "analyst_reco.json",     False),
    ("earnings", "실적 캘린더(미래)",   ["earnings_collect.py"],   "earnings.json",         False),
]

PORTFOLIO_STEPS = [
    ("pf_sync",   "웹앱 → CSV 동기화",  ["portfolio_sync.py"],                 False),
    ("pf_review", "손익·상대강도 산출", ["portfolio_review.py", "--all"],      True),
    ("pf_enrich", "종목별 심층 수집",   ["portfolio_enrich.py"],               False),
]


def session_dir(today=None):
    d = (today or datetime.now().date()).strftime("%Y-%m-%d")
    return os.path.join(OUTPUT_DIR, SESSION_PREFIX + d)


def run(argv, timeout=900):
    """서브프로세스 1회. (rc, 소요초). 예외를 던지지 않는다."""
    t0 = time.time()
    try:
        p = subprocess.run([PY] + argv, cwd=HERE, timeout=timeout,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        return p.returncode, time.time() - t0, (p.stdout or "")[-400:]
    except subprocess.TimeoutExpired:
        return 124, time.time() - t0, "시간 초과"
    except Exception as e:
        return 1, time.time() - t0, "%s: %s" % (type(e).__name__, e)


def verdict(path, t_start):
    """★'파일 존재'는 성공이 아니다 — 이번 실행으로 갱신됐는지를 본다(run_signals 와 같은 계약)."""
    if not os.path.exists(path):
        return "없음", None
    mt = os.path.getmtime(path)
    if mt <= t_start:
        return "STALE", datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M")
    return "OK", datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M")


def main():
    ap = argparse.ArgumentParser(description="주말·휴장일 이슈 수집 + 포트폴리오(★예측 발행 없음)")
    ap.add_argument("--check", action="store_true", help="실행 없이 계획만")
    ap.add_argument("--skip-portfolio", action="store_true")
    ap.add_argument("--skip-news", action="store_true")
    ap.add_argument("--allow-trading-day", action="store_true",
                    help="거래일에도 강행(테스트용 — 평소엔 아침 파이프라인을 써라)")
    args = ap.parse_args()

    st = trading_day_status(datetime.now().date())
    if st["is_trading_day"] and not args.allow_trading_day:
        print("[weekend] %s" % st["reason"])
        print("[weekend] 거래일에는 아침 파이프라인을 써라 — 중단.")
        print("[weekend]   · 거래일 신호 수집:  python run_signals.py")
        print("[weekend]   · 그래도 강행하려면: --allow-trading-day")
        return 1

    sess = session_dir()
    print("[weekend] %s" % st["reason"])
    print("[weekend] 세션: %s" % sess)
    print("[weekend] ★이 실행은 predictions.json·루트 국면신호·accuracy_log·trade_plan 을")
    print("[weekend]   건드리지 않는다. 산출은 weekend_issues.json 이다.")

    steps = ([] if args.skip_news else STEPS)
    if args.check:
        print("\n%-11s %-22s %s" % ("KEY", "설명", "산출"))
        print("-" * 62)
        for k, d, _a, out, req in steps:
            print("%-11s %-22s %s%s" % (k, d, out, "  (필수)" if req else ""))
        if not args.skip_portfolio:
            for k, d, _a, req in PORTFOLIO_STEPS:
                print("%-11s %-22s %s" % (k, d, "-"))
        return 0

    os.makedirs(sess, exist_ok=True)
    results, n_bad = [], 0

    for key, desc, argv, out, required in steps:
        path = os.path.join(sess, out)
        t0 = time.time()
        rc, took, tail = run(argv + ["--out", path])
        v, mt = verdict(path, t0)
        # 필수 단계만 실패로 센다 — 무료 소스는 주말에 조용해질 수 있다(정상)
        bad = required and (v != "OK")
        n_bad += bad
        results.append({"key": key, "desc": desc, "rc": rc, "took": round(took, 1),
                        "verdict": v, "mtime": mt, "required": required})
        print("[weekend] %-10s %-22s rc=%-3s %-6s %5.1fs%s"
              % (key, desc, rc, v, took, "  ★필수 실패" if bad else ""))
        if bad and tail.strip():
            print("[weekend]    %s" % tail.strip().replace("\n", " ")[:160])

    if not args.skip_portfolio:
        for key, desc, argv, required in PORTFOLIO_STEPS:
            # ★review 와 enrich 에 **같은 세션**을 명시적으로 넘긴다.
            #   각자 glob(오늘날짜_*) 을 돌리면 주말엔 서로 다른 폴더를 잡아 전원 스킵된다.
            a = list(argv)
            if key in ("pf_review", "pf_enrich"):
                a += ["--session", sess]
            rc, took, tail = run(a)
            ok = (rc == 0)
            n_bad += (required and not ok)
            results.append({"key": key, "desc": desc, "rc": rc, "took": round(took, 1),
                            "verdict": "OK" if ok else "실패", "required": required})
            print("[weekend] %-10s %-22s rc=%-3s %-6s %5.1fs"
                  % (key, desc, rc, "OK" if ok else "실패", took))
            if not ok and tail.strip():
                print("[weekend]    %s" % tail.strip().replace("\n", " ")[:160])

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": "weekend",
        "trading_day": st,
        "session": sess,
        "steps": results,
        "n_required_bad": n_bad,
        "what": ("주말·휴장일 이슈 수집 산출물. ★예측(predictions)이 아니다 — "
                 "picks/shorts/market_call 키를 의도적으로 쓰지 않는다."),
        "contract": (
            "이 파일과 같은 폴더의 산출물은 **채점 대상이 아니다**. 주말 발행 예측은 다음 "
            "거래일 발행분과 같은 정산 창을 봐서 표본을 중복 계상하기 때문이다(실측: 중복 "
            "그룹 14개). 주말 코워크는 (1) 이슈 정리 (2) 등록자 포트폴리오 판단 두 가지만 한다. "
            "종목 추천을 쓰더라도 predictions.json 으로 저장하지 마라."),
    }
    save_json_atomic(os.path.join(sess, "weekend_issues.json"), payload)

    print("\n[weekend] 완료 — 필수 실패 %d건" % n_bad)
    print("[weekend] 산출: %s" % os.path.join(sess, "weekend_issues.json"))
    print("[weekend] 다음: 코워크가 이 폴더를 읽고 주말 이슈 브리핑 + 포트폴리오 판단을 쓴다.")
    return 1 if n_bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
