#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recover.py — 수집/분석 상태 자동 감지 + 빠진 단계 자동 복구 (수동 실행용 단일 진입점)

[목적]
  아침 자동수집이 일부 단계에서 실패(예: Gemini 429, KRX 403)했을 때,
  사용자가 이 스크립트 하나만 실행하면 현재 상태를 진단하고 빠진 산출물을
  자동으로 채운다. "어느 python 을 돌려야 하지?"를 고민할 필요 없이 recover.py 하나면 됨.

  python recover.py

[감지 → 복구 매핑]
  1) 오늘자 세션 폴더가 없음
       → api_collect.py 실행해 세션 생성(수집)
  2) 세션은 있는데 01_broad 기사 0/빈약
       → api_collect.py 로 보강 수집 (--session 지정)
  3) 세션·수집은 OK인데 force_scores.json 없음   ← 오늘 같은 케이스(KRX 403)
       → force_analysis.py 실행해 force_scores.json 생성
         (force_analysis 는 이제 KRX 막혀도 FDR·Naver 폴백으로 생성됨)
  4) 모두 있음
       → 할 일 없음(정상) 보고

[무한재시도 방지]
  force_analysis 가 끝내 실패하면(둘 다 폴백 실패 등) 세션에
  force_scores.SKIPPED 마커를 남긴다. watch_and_analyze 가 이 마커를 보면
  더는 재시도하지 않는다(오늘 720회 재시도 사태 방지).

[원칙]
  - research_agent.py / supervisor.py 는 수정하지 않는다(호출만).
  - 기존 watch_and_analyze.py 의 함수(run_force_analysis 등)를 재사용 — 중복 코드 없음.
  - 메일 발송은 절대 하지 않는다(수집·분석 복구 전용).
"""

import os
import sys
import subprocess
from datetime import datetime
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")

# 기존 워처 모듈 재사용 (force_analysis 실행/JSON 추출/저장 로직 공유)
import watch_and_analyze as wa
from count_articles import count_articles

MIN_ARTICLES = 10                    # 이 수 미만이면 '빈약 수집'으로 보고 api_collect 보강
SKIP_MARKER = "force_scores.SKIPPED"  # force 분석 끝내 실패 시 남기는 마커
API_TIMEOUT = 1200                    # api_collect 최대 대기(초)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] [recover] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
        with open(os.path.join(HERE, "logs", "recover.log"), "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def latest_today_session():
    """오늘 날짜로 시작하는 최신 세션 폴더(01_broad 존재). 없으면 None."""
    if not os.path.isdir(OUTPUT_DIR):
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    cands = []
    for name in os.listdir(OUTPUT_DIR):
        if name.startswith("_") or name == "__pycache__":
            continue
        if not name.startswith(today):
            continue
        d = os.path.join(OUTPUT_DIR, name)
        if os.path.isdir(d):
            cands.append((os.path.getmtime(d), d))
    if not cands:
        return None
    cands.sort(reverse=True)
    return cands[0][1]


def run_api_collect(session_dir=None):
    """api_collect.py 실행(수집 생성/보강). 성공 시 True."""
    api_py = os.path.join(HERE, "api_collect.py")
    if not os.path.isfile(api_py):
        log("🔴 api_collect.py 없음 — 수집 복구 불가")
        return False
    cmd = [sys.executable, api_py]
    if session_dir:
        cmd += ["--session", session_dir]
    log(f"▶ api_collect 실행 {'(보강: '+os.path.basename(session_dir)+')' if session_dir else '(새 세션)'}")
    try:
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=API_TIMEOUT)
    except subprocess.TimeoutExpired:
        log(f"🔴 api_collect 타임아웃({API_TIMEOUT}s)")
        return False
    except Exception as e:
        log(f"🔴 api_collect 예외: {type(e).__name__}: {e}")
        return False
    if r.returncode != 0:
        log(f"🔴 api_collect 비정상 종료 rc={r.returncode} stderr={(r.stderr or '')[:200]}")
        return False
    log("🟢 api_collect 완료")
    return True


def run_force(session_dir):
    """force_analysis 실행 → force_scores.json 생성. 실패 시 SKIP 마커 남김."""
    tickers = wa.load_tickers()
    if not tickers:
        log("🟡 watch_tickers.txt 비어있음 — force 분석 스킵")
        return False
    ok = wa.run_force_analysis(Path(session_dir), tickers)
    if ok:
        log(f"🟢 force_scores.json 생성 완료")
        return True
    # 끝내 실패 → 무한재시도 방지 마커
    try:
        with open(os.path.join(session_dir, SKIP_MARKER), "w", encoding="utf-8") as f:
            f.write(f"force_analysis failed at {datetime.now().isoformat()}\n"
                    f"watch_and_analyze should not retry this session.\n")
        log(f"🟡 force 분석 실패 → {SKIP_MARKER} 마커 생성(워처 재시도 중단)")
    except Exception:
        pass
    return False


def main():
    log("=" * 55)
    log("recover 시작 — 상태 진단 후 빠진 단계 자동 복구")

    sess = latest_today_session()

    # CASE 1: 오늘 세션 자체가 없음 → 수집부터
    if not sess:
        log("진단: 오늘자 세션 폴더 없음 → 수집(api_collect)부터 실행")
        if not run_api_collect():
            log("🔴 수집 복구 실패 — logs/recover.log 및 네트워크 확인 필요")
            return 1
        sess = latest_today_session()
        if not sess:
            log("🔴 수집 후에도 세션 폴더 없음 — 비정상")
            return 1

    name = os.path.basename(sess)
    log(f"대상 세션: {name}")

    # CASE 2: 수집 빈약 → api_collect 보강
    n = count_articles(sess)
    broad = os.path.join(sess, "01_broad_collection.md")
    if not os.path.isfile(broad) or n < MIN_ARTICLES:
        # api_collect 세션(헤더 형식이 달라 count=0)일 수 있으니 파일 크기로 2차 판정
        broad_ok = os.path.isfile(broad) and os.path.getsize(broad) > 3000
        if not broad_ok:
            log(f"진단: 수집 빈약(기사 {n}건/파일미달) → api_collect 보강")
            run_api_collect(sess)
        else:
            log(f"진단: 헤드라인 카운트 {n}이나 01_broad 본문 충분({os.path.getsize(broad)}B) — 수집 OK 간주")

    # CASE 3: force_scores 없음 → force_analysis (핵심: 오늘 케이스)
    fs = os.path.join(sess, "force_scores.json")
    skip = os.path.join(sess, SKIP_MARKER)
    if os.path.isfile(fs):
        log(f"진단: force_scores.json 이미 있음 — 분석 OK")
    elif os.path.isfile(skip):
        log(f"진단: {SKIP_MARKER} 마커 있음 — 이전 시도 실패 기록됨. 재시도하려면 마커 삭제 후 재실행")
    else:
        log("진단: force_scores.json 없음 → force_analysis 실행(복구)")
        run_force(sess)

    # 최종 상태 요약
    log("-" * 55)
    log("최종 상태:")
    for fn, label in [("01_broad_collection.md", "수집"),
                      ("COLLECT_DONE.flag", "수집완료신호"),
                      ("force_scores.json", "세력강도"),
                      ("03_final_report.md", "리포트(Cowork)")]:
        mark = "✅" if os.path.isfile(os.path.join(sess, fn)) else "❌"
        log(f"  {mark} {label} ({fn})")
    log("recover 완료. 필요 시 Cowork 로 분석을 이어가세요.")
    log("=" * 55)
    return 0


if __name__ == "__main__":
    sys.exit(main())
