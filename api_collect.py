#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
api_collect.py — API 기반 광역수집 (Gemini + Naver API 전용)

[목적]
  Cowork 샌드박스나 학교·회사 프록시에서 RSS·뉴스 페이지(news.naver.com, mk.co.kr 등)가
  403 으로 막힐 때 대안. HTTPS 기반 API 만 사용해 광역수집을 수행한다.

  - Gemini API (검색 그라운딩) → 주제별 핵심 사실/뉴스 정리
  - Naver 검색 API → 한국 매체 키워드 검색

[기존 시스템과의 호환]
  research_agent.py 의 collect 와 동일한 세션 폴더 구조로 결과를 저장:
    output/YYYY-MM-DD_HHMMSS/
      ├─ 01_broad_collection.md
      ├─ INSTRUCTIONS.md
      ├─ 02_deep_collection.md   (API only 모드에서는 01과 동일/요약 마커 포함)
      ├─ COLLECT_DONE.flag
      ├─ DEEP_DONE.flag
      ├─ status.json
      └─ YYYY-MM-DD_1차수집.txt

  Cowork 는 평소대로 세션폴더만 읽으면 된다. cowork_instructions.md 수정 불필요.
  watch_and_analyze.py 도 COLLECT_DONE.flag 를 감지해 force_scores.json 자동 생성.

[사용법]
  python api_collect.py                          # 새 세션 폴더 + 일괄 수집
  python api_collect.py --session "기존세션"     # 기존 세션 폴더에 보강
  python api_collect.py --gemini-queries 6 --naver-queries 10
  python api_collect.py --no-naver               # Gemini 만
  python api_collect.py --no-gemini              # Naver 만

[원칙]
  research_agent.py 는 일절 수정하지 않고 import 만 한다.
"""

import os
import sys
import time
import inspect
import argparse
from datetime import datetime

# research_agent.py 의 유틸을 재사용 (import 만, 수정 X)
import research_agent as ra

# research_agent.py 버전 호환: gemini_call 이 purpose 인자를 지원하는 v2 와
# 지원하지 않는 v1 모두에서 동작하도록 래퍼 사용.
_GEMINI_CALL_SUPPORTS_PURPOSE = (
    "purpose" in inspect.signature(ra.gemini_call).parameters
)


def _gemini_call_compat(rotator, contents, **kwargs):
    """research_agent.py v1/v2 모두에서 동작하는 gemini_call 래퍼."""
    if not _GEMINI_CALL_SUPPORTS_PURPOSE:
        kwargs.pop("purpose", None)
    return ra.gemini_call(rotator, contents, **kwargs)


# Windows 콘솔 UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

log = ra.log   # 같은 로거 사용 (logs/run_YYYYMMDD.log 로 함께 기록)


# =====================================================================
# 검색 키워드 셋 — 분석가가 모멘텀 + 매크로 + 종목 단서를 모두 얻을 수 있게
# =====================================================================
GEMINI_QUERIES = [
    "오늘 한국 주식시장 코스피 코스닥 종가와 등락률, 거래대금 요약",
    "오늘 한국 외국인·기관 순매수/매도 상위 종목 (코스피·코스닥 각 5개씩)",
    "오늘 한국 주식 신고가 갱신 종목과 그 사유",
    "오늘 한국 정책 이슈 (정부 발표, 규제, 정책 수혜주)",
    "오늘 미국 증시 다우·나스닥·S&P500 종가 + 한국 증시에 미치는 영향",
    "오늘 한국 환율 원달러 환율, 미국 10년물 국채 금리, 유가 동향",
    "오늘 한국 반도체 HBM·AI 관련 주요 뉴스 및 종목",
    "오늘 한국 2차전지 (양극재·음극재·전해질) 주요 뉴스 및 종목",
    "오늘 한국 방산·조선·원전 수주 및 정책 수혜 종목",
    "오늘 한국 바이오 신약·임상·기술이전 뉴스 및 종목",
    "오늘 한국 어닝 서프라이즈·어닝쇼크 종목 (실적 발표)",
    "오늘 한국 코스닥 급등 종목과 사유 (테마·재료별)",
]

NAVER_QUERIES = [
    "코스피 오늘 시황",
    "코스닥 오늘 시황",
    "외국인 순매수",
    "기관 순매수",
    "외국인 매도 종목",
    "신고가 종목",
    "어닝 서프라이즈",
    "어닝쇼크 적자",
    "유상증자 공시",
    "관리종목 지정",
    "HBM 반도체 수주",
    "AI 인공지능 종목",
    "2차전지 수주",
    "방산 수주 계약",
    "원전 수주",
    "조선 수주",
    "정책 수혜주",
    "코스닥 급등",
]

GEMINI_SEARCH_PROMPT = (
    "너는 한국 주식 시장 정보 검색 에이전트야. 다음 주제에 대해 신뢰할 수 있는 한국 매체 "
    "(연합뉴스·매일경제·한국경제·머니투데이·이데일리·서울경제·파이낸셜뉴스·뉴스1·뉴시스 등) "
    "기사를 내장 구글 검색으로 찾아 핵심을 정리해줘.\n"
    "- 단순 요약/의견 금지. 사실(수치·종목명·일자·인용)만.\n"
    "- 종목명은 가능하면 6자리 종목코드와 함께 표기 (예: 삼성전자 005930).\n"
    "- 각 핵심 사실 끝에 [출처: 매체명, 날짜] 명시.\n"
    "- 출처를 찾지 못하면 그 항목은 적지 마.\n\n"
    "[주제]: {query}\n\n"
    "[출력 형식 — 정확히 이대로]\n"
    "### {query}\n"
    "- 핵심 사실 1 ... [출처: ..., YYYY-MM-DD]\n"
    "- 핵심 사실 2 ... [출처: ..., YYYY-MM-DD]\n"
    "- ...\n"
)


# =====================================================================
# 1. Gemini 검색 그라운딩 수집
# =====================================================================
def gemini_collect(queries, rotator, sleep_between=2.0) -> tuple:
    """
    각 query 에 대해 Gemini 검색 그라운딩으로 결과 수집.
    Returns: (combined_text, success_count, fail_count)
    """
    parts = ["## Gemini 검색 그라운딩 결과 (외부 RSS/페이지 차단 대안)\n\n"]
    ok = fail = 0
    for i, q in enumerate(queries, 1):
        log.info(f"[API/Gemini {i}/{len(queries)}] {q[:50]}")
        prompt = GEMINI_SEARCH_PROMPT.format(query=q)
        text, err = _gemini_call_compat(
            rotator, prompt,
            generation_config={"temperature": 0.2, "max_output_tokens": 2048},
            use_search_tool=True,
            timeout_sec=60,
            max_attempts=min(len(rotator), 3),
            purpose=f"api_collect:gemini[{i}/{len(queries)}]",
        )
        if text and text.strip():
            parts.append(text.strip() + "\n\n")
            ok += 1
        else:
            parts.append(f"### {q}\n- [Gemini 검색 실패: {err}]\n\n")
            fail += 1
        if i < len(queries) and sleep_between > 0:
            time.sleep(sleep_between)
    parts.append(f"\n_Gemini 결과: 성공 {ok} / 실패 {fail} / 총 {len(queries)}_\n\n")
    return "".join(parts), ok, fail


# =====================================================================
# 2. Naver API 수집
# =====================================================================
def naver_collect(queries, cid, csec, display=5, sleep_between=0.4) -> tuple:
    """Returns: (combined_text, success_count, fail_count)"""
    parts = ["## Naver 검색 API 결과\n\n"]
    ok = fail = 0
    for i, q in enumerate(queries, 1):
        log.info(f"[API/Naver {i}/{len(queries)}] {q}")
        try:
            res = ra.fetch_naver_news_api(q, cid, csec, display=display, sort="date")
            if res and "🚨" not in res and "오류" not in res[:60]:
                parts.append(res + "\n\n")
                ok += 1
            else:
                parts.append(res + "\n\n")  # 실패 메시지도 기록
                fail += 1
        except Exception as e:
            parts.append(f"=== [NAVER] '{q}' === [예외: {type(e).__name__}: {e}]\n\n")
            fail += 1
        if i < len(queries) and sleep_between > 0:
            time.sleep(sleep_between)
    parts.append(f"\n_Naver 결과: 성공 {ok} / 실패 {fail} / 총 {len(queries)}_\n\n")
    return "".join(parts), ok, fail


# =====================================================================
# 3. 02_deep_collection.md 호환 작성 (API only 모드는 broad 와 동일/마커만)
# =====================================================================
def write_deep_marker(session_dir: str, summary_text: str) -> str:
    """
    Cowork 가 평소대로 02_deep_collection.md 와 DEEP_COLLECTION_COMPLETE 마커를
    기대하므로, API only 모드에서는 broad 본문을 요약+포인터로 02 에 함께 저장.
    """
    sep = "=" * 70
    path = os.path.join(session_dir, "02_deep_collection.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# 심층 수집 결과 (API only 모드) — "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M')} KST\n\n")
        f.write("> API only 모드: 광역수집(01_broad_collection.md)이 이미 Gemini "
                "검색 그라운딩 + Naver API 로 충분히 풍부하므로, 별도 심층수집은 "
                "수행하지 않았다. Cowork 는 01_broad 와 본 파일을 합쳐 분석하라.\n\n")
        f.write(f"{sep}\n## API 수집 요약\n{sep}\n\n")
        f.write(summary_text + "\n\n")
        f.write(f"{sep}\n<<<DEEP_COLLECTION_COMPLETE "
                f"{datetime.now().strftime('%Y-%m-%d_%H%M%S')} api_only>>>\n")
    return path


# =====================================================================
# 4. 메인
# =====================================================================
def main():
    ap = argparse.ArgumentParser(
        description="API 기반 광역수집 (Gemini + Naver, RSS/페이지 차단 대안)")
    ap.add_argument("--session", default="",
                    help="기존 세션 폴더 경로(생략 시 새 세션 자동 생성)")
    ap.add_argument("--gemini-queries", type=int, default=len(GEMINI_QUERIES),
                    help=f"실행할 Gemini 검색 수 (1~{len(GEMINI_QUERIES)}, 기본 전체)")
    ap.add_argument("--naver-queries", type=int, default=len(NAVER_QUERIES),
                    help=f"실행할 Naver 검색 수 (1~{len(NAVER_QUERIES)}, 기본 전체)")
    ap.add_argument("--no-naver", action="store_true",
                    help="Naver API 생략 (Gemini 만)")
    ap.add_argument("--no-gemini", action="store_true",
                    help="Gemini 생략 (Naver 만)")
    ap.add_argument("--no-deep", action="store_true",
                    help="02_deep_collection.md 와 DEEP_DONE.flag 생성 생략")
    args = ap.parse_args()

    if args.no_gemini and args.no_naver:
        log.error("[API] --no-gemini 와 --no-naver 동시 사용 — 수집할 게 없음")
        sys.exit(2)

    # ── 세션 폴더 ─────────────────────────────────────────────
    if args.session and os.path.isdir(args.session):
        sess = args.session
        log.info(f"[API] 기존 세션 폴더에 보강: {sess}")
    else:
        sess = ra.new_session_dir()
        log.info(f"[API] 새 세션 폴더: {sess}")

    log.info("=" * 60)
    log.info("API only 광역수집 시작")
    log.info(f"세션: {sess}")
    log.info("=" * 60)

    parts = []
    gem_ok = gem_fail = nav_ok = nav_fail = 0

    # ── Gemini ────────────────────────────────────────────────
    if not args.no_gemini:
        gk = ra.load_gemini_keys()
        if not gk:
            log.warning("[API] Gemini 키 없음 — Gemini 단계 스킵")
            parts.append("## Gemini 검색 결과\n_(Gemini 키 없음 — 스킵됨)_\n\n")
        elif not ra.GENAI_AVAILABLE:
            log.warning("[API] google-genai SDK 미설치 — Gemini 단계 스킵")
            parts.append("## Gemini 검색 결과\n_(google-genai SDK 미설치 — 스킵됨)_\n\n")
        else:
            rotator = ra.KeyRotator(gk)
            queries = GEMINI_QUERIES[:max(1, args.gemini_queries)]
            text, gem_ok, gem_fail = gemini_collect(queries, rotator)
            parts.append(text)
    else:
        log.info("[API] --no-gemini 지정 — Gemini 단계 생략")

    # ── Naver API ─────────────────────────────────────────────
    if not args.no_naver:
        cid, csec = ra.load_naver_keys()
        ok, why = ra.validate_naver_credentials(cid, csec)
        if not ok:
            log.warning(f"[API] Naver 자격증명 오류: {why} — Naver 단계 스킵")
            parts.append(f"## Naver API 결과\n_(자격증명 오류: {why})_\n\n")
        else:
            queries = NAVER_QUERIES[:max(1, args.naver_queries)]
            text, nav_ok, nav_fail = naver_collect(queries, cid, csec)
            parts.append(text)
    else:
        log.info("[API] --no-naver 지정 — Naver 단계 생략")

    broad_text = "".join(parts)
    article_count = gem_ok + nav_ok

    # ── 결과 저장 ──────────────────────────────────────────────
    meta = {
        "수집 방식": "API only (Gemini 검색 그라운딩 + Naver API)",
        "Gemini 검색": f"성공 {gem_ok} / 실패 {gem_fail}",
        "Naver 검색": f"성공 {nav_ok} / 실패 {nav_fail}",
        "유효 수집 수": article_count,
        "참고": "RSS·뉴스 페이지 차단 환경 대안. Cowork 분석은 평소대로 진행.",
    }
    broad_path, instr_path = ra.write_broad_and_instructions(sess, broad_text, meta)

    # 메일 첨부용 1차수집 txt
    txt1 = ra._make_txt_attachment(sess, "01_broad_collection.md", "1차수집")

    # 02_deep_collection.md + DEEP_DONE.flag (Cowork 기존 흐름 호환)
    deep_flag = ""
    if not args.no_deep:
        # 02 본문엔 같은 broad 요약 사용 (API only 모드의 명시적 표시 포함)
        write_deep_marker(sess, broad_text)
        # 2차 첨부 사본 (1차와 동일 내용이지만 첨부 슬롯 채움)
        ra._make_txt_attachment(sess, "02_deep_collection.md", "2차심층수집")
        ra.update_status(sess, "deep_done",
                         {"mode": "api_only", "articles": article_count})
        deep_flag = ra._write_flag(sess, "deep", ok=True)

    # COLLECT_DONE.flag (워처 트리거 — watch_and_analyze.py 가 감지해 force_scores 자동 생성)
    ra.update_status(sess, "collected",
                     {"mode": "api_only", "articles_via_api": article_count})
    collect_flag = ra._write_flag(sess, "collect", ok=True)

    # ── 출력 (research_agent.py collect 와 동일한 토큰) ─────────
    log.info(f"✅ API 수집 완료 — Gemini {gem_ok} + Naver {nav_ok} = {article_count}건")
    log.info(f"   세션 폴더: {sess}")
    print(f"\nSESSION_DIR={sess}")
    print(f"BROAD_FILE={broad_path}")
    print(f"INSTRUCTIONS_FILE={instr_path}")
    print(f"COLLECT_DONE_FLAG={collect_flag}")
    print(f"ARTICLE_COUNT={article_count}")
    if txt1:
        print(f"ATTACH_TXT_1={txt1}")
    if deep_flag:
        print(f"DEEP_DONE_FLAG={deep_flag}")
    print(f"MODE=api_only")


if __name__ == "__main__":
    main()
