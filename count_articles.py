#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
count_articles.py — 세션 폴더의 광역수집 기사 수를 센다 (헬퍼).

[목적]
  run_morning_auto.bat 가 step1(research_agent.py auto) 의 수집 성공 여부를
  정확히 판단하기 위한 헬퍼.
  cmd_auto 는 ARTICLE_COUNT 를 stdout 에 찍지 않으므로, .bat 이 그 값을 0 으로
  오인해 불필요하게 api_collect 폴백을 돌리고 → 2중 세션이 생기던 문제를 막는다.
  (research_agent.py 는 일절 수정하지 않는다 — 이 헬퍼는 결과 파일만 읽는다.)

[방식]
  research_agent.parse_articles_from_text 가 한 기사로 인식하는 헤더와 동일한
  마커로 센다:
    [날짜] <상태이모지> [태그] 제목      (예: "[Recent] 🟢 [원문] ...")
  → 정상 RSS 세션 ≈ 82, 수집 실패 세션 = 0 으로 또렷이 구분된다.
  (검증: 2026-05-30 정상세션 82 / 실패세션 0 — research_agent 정식 파서와 일치)

[사용법]
  python count_articles.py "C:\\path\\to\\output\\YYYY-MM-DD_HHMMSS"
  → 정수 한 줄(기사 수)만 stdout 에 출력. 폴더·파일 없음/오류 시 0.
"""

import sys
import os
import re

# research_agent.parse_articles_from_text 가 한 기사로 인식하는 헤더 패턴과 동일.
#   [임의텍스트] (🟢|🔵|🟡|🔴) [임의텍스트]
_ARTICLE_RE = re.compile(r"\[[^\]]+\]\s*(?:\U0001F7E2|\U0001F535|\U0001F7E1|\U0001F534)\s*\[[^\]]+\]")


def count_articles(session_dir: str) -> int:
    """세션 폴더의 01_broad_collection.md 에서 기사 수를 센다. 실패 시 0."""
    if not session_dir:
        return 0
    path = os.path.join(session_dir, "01_broad_collection.md")
    if not os.path.isfile(path):
        return 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        return 0
    return sum(1 for line in text.splitlines() if _ARTICLE_RE.search(line))


def main():
    session_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    try:
        print(count_articles(session_dir))
    except Exception:
        print(0)


if __name__ == "__main__":
    main()
