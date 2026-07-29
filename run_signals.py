# -*- coding: utf-8 -*-
"""
run_signals.py — 아침 신호 수집 '한 방' 러너 (실행 순서를 코드에 고정)

[왜 만들었나 — 2026-07-29 실사고]
  이 날 예정작업의 SKILL.md 가 마스터 지시서(코워크_통합지시_최종.md)보다 낡아 신호 표가
  12종에 머물러 있었다. 분석가는 그 표를 그대로 베껴 hts_capture / earnings / holding_review /
  night_futures / taildrop / snapshot 을 통째로 빠뜨렸고, 이미 삭제된 kis_collect.py 를 돌렸다.
  게다가 개별 스크립트 로그가 리다이렉트 환경에서 깨져 '파일이 있으면 성공'으로 판정했다 —
  short.json 은 5일 전(07-24) 파일이 그대로 남아 있었는데도 통과했다.

  => 그래서 이 러너는 두 가지만 한다.
     (1) **실행 순서를 문서가 아니라 코드에 둔다.** 문서가 낡아도 단계가 빠지지 않는다.
     (2) **'파일 존재'가 아니라 '이번 실행으로 갱신됐는가 + 기준일이 언제인가'로 판정한다.**

[사용법]
  python run_signals.py                     # 오늘 세션 자동탐지 후 전 단계 실행
  python run_signals.py --check             # 실행 없이 계획·현재 산출물 상태만 출력
  python run_signals.py --session output\2026-07-29_063612
  python run_signals.py --only force,market # 일부만
  python run_signals.py --skip hts,taildrop # 일부 제외

[주의]
  - 이미 분석이 끝난 세션(03_final_report.md 존재)에는 기본적으로 실행을 거부한다(회고 스냅샷 오염 방지).
    강행하려면 --allow-analyzed 를 명시하라.
  - 개별 스크립트는 실패해도 exit 0 로 graceful 하게 끝나는 설계다. 이 러너도 마찬가지로
    끝까지 돌고, 마지막 요약표에서 실패·미갱신을 드러낸다. 종료코드는 필수 단계 실패 시 1.
"""
import os
import sys
import glob
import json
import time
import argparse
import subprocess
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE, "output")
PY = sys.executable  # PowerShell 에는 python 이 PATH 에 없다 — 절대 경로로 고정

# (key, 설명, argv, 산출물스펙, 필수여부, 타임아웃초)
#   산출물스펙: "session:파일명" | "root:파일명" | "session_glob:패턴"
STEPS = [
    ("force",      "세력강도(force_scores)",     ["watch_and_analyze.py", "--once"],
     "session:force_scores.json",        True,  900),
    ("market",     "시장컨텍스트/breadth",        ["market_collect.py"],
     "session:market_context.json",      True,  600),
    ("fsc",        "공식 시세",                   ["fsc_collect.py"],
     "session:fsc_prices.json",          True,  600),
    ("flow",       "투자자별 수급",               ["flow_collect.py"],
     "session:flow_data.json",           True,  900),
    ("overheat",   "과열/되돌림",                 ["overheat_collect.py"],
     "session:overheat.json",            True,  600),
    ("deriv",      "옵션 PCR",                    ["deriv_collect.py"],
     "root:deriv_sentiment.json",        True,  600),
    ("ecos",       "거시(한국은행)",              ["ecos_collect.py"],
     "root:ecos_macro.json",             True,  600),
    ("dart",       "장투 펀더멘털",               ["dart_collect.py"],
     "session:fundamentals.json",        True,  900),
    ("disclosure", "공시 오버행",                 ["disclosure_collect.py"],
     "session:disclosures.json",         True,  900),
    ("mirae",      "미래에셋 수급(교차검증)",     ["mirae_collect.py"],
     "root:mirae_data.json",             False, 600),
    ("short",      "공매도 잔고",                 ["short_collect.py"],
     "session:short.json",               True,  900),
    ("hts",        "카이로스 HTS 캡처",           ["hts_capture_collect.py"],
     "session:hts_capture.json",         False, 900),
    ("taildrop",   "노트북 전송분 이관",          ["taildrop_receive.py"],
     None,                               False, 300),
    ("vkospi",     "변동성지수",                  ["vkospi_collect.py"],
     "root:vkospi.json",                 False, 300),
    ("nightfut",   "야간선물",                    ["night_futures_collect.py"],
     "root:night_futures.json",          False, 300),
    ("credit",     "신용잔고(빚투)",              ["credit_collect.py"],
     "root:credit_balance.json",         False, 300),
    ("earnings",   "실적 캘린더",                 ["earnings_collect.py"],
     "session:earnings_calendar.json",   False, 600),
    ("caution",    "국면 종합게이트(반드시 뒤)",  ["market_caution.py"],
     "root:market_caution.json",         True,  600),
    ("snapshot",   "루트 신호 세션 동결",         ["snapshot_signals.py"],
     "session_glob:signals_snapshot_*.json", True, 300),
    ("holdrev",    "보유 픽 재평가",              ["holding_review.py"],
     "session:holding_review.json",      True,  900),
]

# 기준일 필드 후보(있는 것 하나를 읽어 신선도 판정)
ASOF_KEYS = ("asof_date", "asof", "latest_trade_date", "generated_at")


def find_session(explicit=None):
    if explicit:
        p = explicit if os.path.isabs(explicit) else os.path.join(BASE, explicit)
        return p if os.path.isdir(p) else None
    today = datetime.now().strftime("%Y-%m-%d")
    cands = []
    for d in glob.glob(os.path.join(OUTPUT_DIR, "*")):
        name = os.path.basename(d)
        if not os.path.isdir(d) or name.startswith("_"):
            continue
        if name.startswith(today):
            cands.append(d)
    if not cands:  # 자정 경계 6시간 폴백
        cutoff = time.time() - 6 * 3600
        for d in glob.glob(os.path.join(OUTPUT_DIR, "*")):
            name = os.path.basename(d)
            if os.path.isdir(d) and not name.startswith("_") and os.path.getmtime(d) >= cutoff:
                cands.append(d)
    return max(cands, key=os.path.getmtime) if cands else None


def resolve_out(spec, session):
    if not spec:
        return None, None
    kind, _, name = spec.partition(":")
    root = session if kind.startswith("session") else BASE
    return kind, os.path.join(root, name)


def peek_asof(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return ""
    if not isinstance(d, dict):
        return ""
    for k in ASOF_KEYS:
        v = d.get(k)
        if isinstance(v, str) and v[:4].isdigit():
            return v[:19]
    fr = d.get("freshness")
    if isinstance(fr, dict) and isinstance(fr.get("latest_trade_date"), str):
        return fr["latest_trade_date"]
    return ""


def newest(spec, session):
    """산출물의 (경로, mtime, asof). glob 이면 가장 최근 것."""
    kind, path = resolve_out(spec, session)
    if not path:
        return None, 0.0, ""
    if kind == "session_glob":
        hits = glob.glob(path)
        if not hits:
            return path, 0.0, ""
        p = max(hits, key=os.path.getmtime)
        return p, os.path.getmtime(p), peek_asof(p)
    if not os.path.exists(path):
        return path, 0.0, ""
    return path, os.path.getmtime(path), peek_asof(path)


def run_one(step, session, log_dir):
    key, desc, argv, spec, required, timeout = step
    _, before_mt, _ = newest(spec, session)
    t0 = time.time()
    logf = os.path.join(log_dir, "sig_%s.log" % key)
    try:
        with open(logf, "w", encoding="utf-8", errors="replace") as fh:
            r = subprocess.run([PY, "-X", "utf8"] + [os.path.join(BASE, argv[0])] + argv[1:],
                               cwd=BASE, stdout=fh, stderr=subprocess.STDOUT,
                               timeout=timeout)
        rc = r.returncode
        err = ""
    except subprocess.TimeoutExpired:
        rc, err = -1, "timeout"
    except Exception as e:
        rc, err = -1, type(e).__name__
    took = time.time() - t0

    path, after_mt, asof = newest(spec, session)
    if spec is None:
        verdict = "OK" if rc == 0 else "FAIL"
        note = err or "(산출물 없음 단계)"
    elif after_mt == 0.0:
        verdict = "FAIL" if required else "SKIP"
        note = err or "산출물 없음"
    elif after_mt <= before_mt + 0.5:
        verdict = "STALE"
        note = "이번 실행으로 갱신 안 됨 — 과거 파일이 남아있는 것"
    else:
        verdict = "OK"
        note = err
    return dict(key=key, desc=desc, rc=rc, took=took, verdict=verdict,
                note=note, path=path, asof=asof, required=required, log=logf)


def main():
    ap = argparse.ArgumentParser(description="아침 신호 수집 러너(순서 고정 + 산출물 검증)")
    ap.add_argument("--session", default=None)
    ap.add_argument("--check", action="store_true", help="실행 없이 계획·현재 상태만")
    ap.add_argument("--only", default=None, help="쉼표구분 key 만 실행")
    ap.add_argument("--skip", default=None, help="쉼표구분 key 제외")
    ap.add_argument("--allow-analyzed", action="store_true",
                    help="03_final_report.md 가 있는 세션에도 강행")
    args = ap.parse_args()

    session = find_session(args.session)
    if not session:
        print("[run_signals] 오늘 세션을 찾지 못했다 — research_agent.py collect 를 먼저 돌려라.")
        return 1
    print("[run_signals] 세션: %s" % session)
    print("[run_signals] python: %s" % PY)

    analyzed = os.path.exists(os.path.join(session, "03_final_report.md"))
    if analyzed and not args.check and not args.allow_analyzed:
        print("[run_signals] 이 세션은 이미 분석 완료(03_final_report.md 존재)다.")
        print("[run_signals] 신호를 재수집하면 회고 스냅샷이 오염된다 — 중단. 강행은 --allow-analyzed.")
        return 1

    only = {s.strip() for s in args.only.split(",")} if args.only else None
    skip = {s.strip() for s in args.skip.split(",")} if args.skip else set()
    steps = [s for s in STEPS if (only is None or s[0] in only) and s[0] not in skip]

    if args.check:
        print("\n%-11s %-26s %-9s %-19s %s" % ("KEY", "설명", "상태", "기준일/생성", "경로"))
        print("-" * 108)
        for st in steps:
            key, desc, _, spec, required, _ = st
            path, mt, asof = newest(spec, session)
            if spec is None:
                state = "-"
            elif mt == 0.0:
                state = "없음"
            else:
                age_h = (time.time() - mt) / 3600.0
                state = "%.1fh전" % age_h
            print("%-11s %-26s %-9s %-19s %s" % (key, desc, state, asof or "-",
                                                 os.path.basename(path) if path else "-"))
        print("\n필수 단계: %s" % ", ".join(s[0] for s in steps if s[4]))
        return 0

    log_dir = os.path.join(BASE, "logs")
    os.makedirs(log_dir, exist_ok=True)
    results = []
    for st in steps:
        print("[run_signals] >> %-11s %s" % (st[0], st[1]))
        res = run_one(st, session, log_dir)
        print("   %-6s rc=%s %.1fs %s" % (res["verdict"], res["rc"], res["took"],
                                          res["note"] or ""))
        results.append(res)

    print("\n" + "=" * 100)
    print("%-11s %-8s %-7s %-19s %s" % ("KEY", "판정", "소요", "기준일/생성", "비고"))
    print("-" * 100)
    for r in results:
        print("%-11s %-8s %-7s %-19s %s" % (r["key"], r["verdict"], "%.0fs" % r["took"],
                                            r["asof"] or "-", r["note"] or ""))
    bad = [r for r in results if r["verdict"] in ("FAIL", "STALE") and r["required"]]
    warn = [r for r in results if r["verdict"] in ("FAIL", "STALE", "SKIP") and not r["required"]]
    print("-" * 100)
    print("필수 실패/미갱신: %d건 %s" % (len(bad), [r["key"] for r in bad] or ""))
    print("선택 실패/생략  : %d건 %s" % (len(warn), [r["key"] for r in warn] or ""))
    if bad:
        print("\n[run_signals] ★분석 전에 위 필수 항목을 먼저 보라. 'STALE' 은 과거 파일이"
              " 남아 있는 것이라 파일 존재만으로는 절대 성공이 아니다.")
    print("=" * 100)
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
