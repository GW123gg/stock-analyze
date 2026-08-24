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

from common import trading_day_status   # 순수 모듈(import 부작용 없음)

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
    # ★산출 위치는 세션이다(mirae_collect._resolve_out: 오늘 세션이 있으면 거기, 없으면 루트).
    #   판정만 root 를 보고 있어서 **매일 STALE 로 오판**했다(2026-08-21 발견).
    #   루트 파일이 2026-07-12 에 멈춰 있어 "asof 지연 40일"이 리포트에 실렸고,
    #   분석가는 그날 09:27 에 갓 받은 교차검증 수급을 매일 버리고 있었다.
    ("mirae",      "미래에셋 수급(교차검증)",     ["mirae_collect.py"],
     "session:mirae_data.json",          False, 600),
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


def _content_hts(d):
    """카이로스 캡처: 몇 종이 실제로 찍혔나. 문제면 사람이 읽을 사유를 돌려준다."""
    n_ok, n_req = d.get("n_ok"), d.get("n_requested")
    if not isinstance(n_ok, int) or not isinstance(n_req, int) or n_req <= 0:
        return None                      # 옛 스키마 — 판단하지 않는다
    if n_ok == 0:
        why = sorted({(c.get("status") or "") for c in (d.get("captures") or [])
                      if c.get("status") != "ok"} - {""})
        return "캡처 %d종 전부 실패(%s)" % (n_req, ", ".join(why) or "사유 미상")
    if n_ok < n_req:
        return "캡처 %d/%d 만 성공 — 나머지는 확인 불가" % (n_ok, n_req)
    return None


# ★내용 점검표. mtime 이 올라갔어도 알맹이가 비었으면 EMPTY 로 내린다.
#   ★여기 넣는 함수는 **문제일 때만** 문자열을 돌려줘라(정상이면 None).
#   ★과잉 판정 금지 — 스키마가 다르면 None 을 돌려 판단을 미뤄라.
CONTENT_CHECKS = {"hts": _content_hts}


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

    # ★새 파일이라고 성공이 아니다 — 알맹이를 들여다본다(2026-08-24 실사고).
    if verdict == "OK" and key in CONTENT_CHECKS and path:
        try:
            with open(path, encoding="utf-8") as fh:
                problem = CONTENT_CHECKS[key](json.load(fh))
        except Exception:                # noqa: BLE001  판정 때문에 러너가 죽으면 안 된다
            problem = None
        if problem:
            verdict = "EMPTY"
            note = problem

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
    ap.add_argument("--allow-nontrading", action="store_true",
                    help="주말·휴장일에도 강행(기본은 중단 — 아래 이유)")
    args = ap.parse_args()

    # ★[v11.22] 비거래일 차단. 수집기는 주말에 '실패'하지 않는다 — 금요일 값을 새 파일로
    #   다시 구워 신선도 게이트를 그냥 통과한다(verdict 가 mtime 기준이라 내용이 같아도 OK).
    #   그 상태로 분석이 돌면 주말 predictions 가 발행되고, 그건 다음 거래일 발행분과
    #   **같은 정산 창**을 봐서 채점 표본을 중복 계상한다(accuracy_log 는 영구 append-only).
    #   주말 작업은 별도 주말 코워크(weekend_collect.py)로 하라 — 루트 신호를 안 건드린다.
    _st = trading_day_status(datetime.now().date())
    if not _st["is_trading_day"] and not args.check and not args.allow_nontrading:
        print("[run_signals] %s" % _st["reason"])
        print("[run_signals] 아침 신호 수집은 거래일에만 한다 — 중단.")
        print("[run_signals]   · 주말 이슈 수집·포트폴리오는:  python weekend_collect.py")
        print("[run_signals]   · 그래도 강행하려면:            --allow-nontrading")
        return 1
    if not _st["is_trading_day"]:
        print("[run_signals] ※ %s (강행 중)" % _st["reason"])

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

    # ★v11.6(호스트 감사 leakage-3): asof 지연을 숫자로 병기 — '방금 재생성됐지만 내용은 낡은'
    #   파일(실측 07-29 short.json: generated_at 당일 06:42, 종목 asof=07-24)은 mtime 기반
    #   STALE 이 못 잡는다. 공매도 T+2 공표처럼 정당한 지연도 있으므로 판정은 유지하고
    #   지연 일수만 비고에 붙인다(CLAUDE.md '지연 거래일 수를 숫자로' 원칙).
    _today = datetime.now().date()
    _wd = _today.weekday()
    _allow = 3 if _wd == 0 else (2 if _wd == 6 else 1)   # 월=금요일까지 허용, 일=금, 그외=전일
    for r in results:
        a = str(r.get("asof") or "")[:10]
        if len(a) == 10 and a[:4].isdigit():
            try:
                _lag = (_today - datetime.strptime(a, "%Y-%m-%d").date()).days
            except ValueError:
                _lag = None
            if _lag is not None and _lag > _allow:
                r["asof_lag_days"] = _lag
                r["note"] = ((r["note"] + " | ") if r["note"] else "") + ("★asof 지연 %d일" % _lag)

    print("\n" + "=" * 100)
    print("%-11s %-8s %-7s %-19s %s" % ("KEY", "판정", "소요", "기준일/생성", "비고"))
    print("-" * 100)
    for r in results:
        print("%-11s %-8s %-7s %-19s %s" % (r["key"], r["verdict"], "%.0fs" % r["took"],
                                            r["asof"] or "-", r["note"] or ""))
    bad = [r for r in results
           if r["verdict"] in ("FAIL", "STALE", "EMPTY") and r["required"]]
    warn = [r for r in results
            if r["verdict"] in ("FAIL", "STALE", "EMPTY", "SKIP") and not r["required"]]
    print("-" * 100)
    print("필수 실패/미갱신: %d건 %s" % (len(bad), [r["key"] for r in bad] or ""))
    print("선택 실패/생략  : %d건 %s" % (len(warn), [r["key"] for r in warn] or ""))
    if bad:
        print("\n[run_signals] ★분석 전에 위 필수 항목을 먼저 보라. 'STALE' 은 과거 파일이"
              " 남아 있는 것이라 파일 존재만으로는 절대 성공이 아니다.")
    print("=" * 100)

    # ★v11.6(leakage-3): 판정 요약을 세션에 남긴다 — 지금은 콘솔에만 찍혀 회고·분석가가
    #   "그날 신호가 STALE/지연이었나"를 사후 참조할 방법이 없었다.
    try:
        from common import save_json_atomic
        save_json_atomic(os.path.join(session, "run_signals_summary.json"),
                         {"generated_at": datetime.now().isoformat(timespec="seconds"),
                          "what": ("run_signals 판정 요약 — 그날 신호 수집이 STALE/지연/실패였는지 "
                                   "사후 참조용(회고 국면입력 신뢰도 판단·분석가 근거 각주)"),
                          "n_required_bad": len(bad),
                          "results": [{k: r.get(k) for k in
                                       ("key", "desc", "verdict", "asof", "asof_lag_days",
                                        "note", "rc", "took")} for r in results]})
        print("[run_signals] 판정 요약 저장: run_signals_summary.json")
    except Exception as e:
        print("[run_signals] 요약 저장 실패(무시): %s" % type(e).__name__)
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
