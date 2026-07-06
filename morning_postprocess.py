#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
morning_postprocess.py — step1(auto) 수집 후처리 (run_morning_auto.bat 헬퍼)

[배경 — 왜 필요한가]
  research_agent.py 의 `auto` 는 세션 폴더(01_broad / 02_deep / INSTRUCTIONS)만
  만들고 COLLECT_DONE.flag / DEEP_DONE.flag 는 만들지 않는다(그 플래그 생성은
  `collect` 명령에만 있음). 그래서 auto 세션은 watch_and_analyze(force_scores
  생성)와 Cowork 가 집어가지 못한다.

  과거에는 .bat 이 step1 의 ARTICLE_COUNT 를 못 읽어(0 으로 오인) 매번
  api_collect 폴백을 돌렸고, 그 폴백이 우연히 플래그를 만들어줬다. 그 결과
  '정상 82건 세션' 위에 'Gemini 쿼터 소진 24건 세션'이 새로 덮여 Cowork 가
  빈약한 세션을 분석하는 사고가 났다.

[이 헬퍼가 하는 일]
  step1 직후 .bat 이 호출한다:
    1) output/ 의 '오늘자' 최신 세션 폴더를 찾는다(01_broad 존재 + 오늘 날짜).
    2) 광역수집 기사 수를 센다(count_articles).
    3) 충분하면(>= --min, 기본 10) COLLECT_DONE.flag + DEEP_DONE.flag 를
       그 세션에 만들고 종료코드 0 ("강함 — 폴백 불필요").
    4) 부족하면 플래그를 만들지 않고 종료코드 2 ("약함 — .bat 이 api_collect 폴백").

  research_agent.py 는 일절 수정하지 않는다(결과 폴더에 신호 파일만 쓴다).
  플래그 내용은 타임스탬프(신호용)일 뿐, watch_and_analyze 는 '존재 여부'만 본다.

[종료코드]
  0 = 강함 (플래그 생성 완료, 폴백 불필요)
  2 = 약함 / 플래그 생성 실패 (.bat 이 api_collect.py 폴백 실행)
  3 = 오늘자 세션 없음 (.bat 이 api_collect.py 폴백 실행)

[사용법]
  python morning_postprocess.py                  # 오늘자 최신 세션 자동탐지
  python morning_postprocess.py --session DIR     # 세션 직접 지정
  python morning_postprocess.py --min 10          # 강함 기준 기사 수(기본 10)
  python morning_postprocess.py --no-date-check   # 오늘날짜 검증 생략(테스트용)
"""

import os
import sys
import argparse
from datetime import datetime

from count_articles import count_articles

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
MIN_ARTICLES_DEFAULT = 10

# 종료코드
RC_STRONG = 0
RC_WEAK = 2
RC_NOSESSION = 3

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def latest_session(output_dir: str):
    """output/ 에서 _archive/__pycache__ 제외, 01_broad 가 있는 최신 mtime 세션 경로."""
    if not os.path.isdir(output_dir):
        return None
    cands = []
    for name in os.listdir(output_dir):
        if name in ("_archive", "__pycache__"):
            continue
        sess = os.path.join(output_dir, name)
        if not os.path.isdir(sess):
            continue
        broad = os.path.join(sess, "01_broad_collection.md")
        if os.path.isfile(broad):
            cands.append((os.path.getmtime(broad), sess))
    if not cands:
        return None
    cands.sort(reverse=True)
    return cands[0][1]


def create_flags(session_dir: str) -> bool:
    """COLLECT_DONE.flag + DEEP_DONE.flag 생성. 성공 시 True."""
    stamp = datetime.now().isoformat()
    for flag in ("COLLECT_DONE.flag", "DEEP_DONE.flag"):
        try:
            with open(os.path.join(session_dir, flag), "w", encoding="utf-8") as f:
                f.write(stamp)
        except Exception as e:
            print(f"[postprocess] 🔴 flag 생성 실패 {flag}: {type(e).__name__}: {e}")
            return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="", help="세션 폴더 직접 지정")
    ap.add_argument("--min", type=int, default=MIN_ARTICLES_DEFAULT,
                    help="강함 기준 기사 수(기본 10)")
    ap.add_argument("--no-date-check", action="store_true",
                    help="오늘 날짜 검증 생략(테스트용)")
    ap.add_argument("--check-only", action="store_true",
                    help="핸드셰이크 모드: 플래그 생성하지 않고 폴백 필요 여부만 판단 "
                         "(collect 가 이미 COLLECT_DONE 을 만들었으므로)")
    args = ap.parse_args()

    # 세션 결정
    if args.session and os.path.isdir(args.session):
        sess = args.session
    else:
        sess = latest_session(OUTPUT_DIR)

    if not sess:
        print("[postprocess] RESULT=NOSESSION — output/ 에 세션 폴더 없음 → api_collect 폴백")
        sys.exit(RC_NOSESSION)

    name = os.path.basename(sess.rstrip("\\/"))

    # 오늘자 세션인지 확인 (step1 이 크래시하면 어제 세션이 최신일 수 있음)
    if not args.no_date_check:
        today = datetime.now().strftime("%Y-%m-%d")
        if not name.startswith(today):
            print(f"[postprocess] RESULT=STALE 최신 세션({name})이 오늘({today}) 것이 "
                  f"아님 → api_collect 폴백")
            sys.exit(RC_NOSESSION)

    n = count_articles(sess)

    # ── 핸드셰이크 모드(--check-only): 플래그 생성 안 함, 폴백 여부만 판단 ──
    # collect 가 이미 COLLECT_DONE.flag 를 만들었고, DEEP_DONE 은 Cowork→deep
    # 핸드셰이크가 만들어야 하므로 여기서 플래그를 만들면 안 된다.
    if args.check_only:
        # 01_broad 본문이 충분하면(api_collect 형식은 헤드라인 카운트가 0이어도
        # 본문이 큼) OK 로 본다. count 와 본문 크기 둘 다 고려.
        broad = os.path.join(sess, "01_broad_collection.md")
        broad_ok = os.path.isfile(broad) and os.path.getsize(broad) > 3000
        if n >= args.min or broad_ok:
            print(f"[postprocess] RESULT=OK count={n} session={name} "
                  f"(broad_ok={broad_ok}) → 폴백 불필요")
            sys.exit(RC_STRONG)
        print(f"[postprocess] RESULT=WEAK count={n} session={name} "
              f"(<{args.min}, 본문부족) → api_collect 폴백")
        sys.exit(RC_WEAK)

    # ── 레거시(auto) 모드: 플래그 직접 생성 (호환 유지) ──
    if n >= args.min:
        if create_flags(sess):
            print(f"[postprocess] RESULT=STRONG count={n} session={name} "
                  f"→ COLLECT_DONE.flag/DEEP_DONE.flag 생성 완료, 폴백 불필요")
            sys.exit(RC_STRONG)
        print(f"[postprocess] RESULT=FLAGFAIL count={n} session={name} → api_collect 폴백")
        sys.exit(RC_WEAK)

    print(f"[postprocess] RESULT=WEAK count={n} session={name} (<{args.min}) "
          f"→ api_collect 폴백")
    sys.exit(RC_WEAK)


if __name__ == "__main__":
    main()
