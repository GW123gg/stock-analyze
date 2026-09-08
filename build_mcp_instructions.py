#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_mcp_instructions.py — MCP판 아침 지시서 사본(analyze_instructions_mcp.md)을 원본에서 재생성 [v11.41]

[왜] setup_kit/02_코워크_예정작업/ 과 ..\\stock_research_mcp\\ 의 analyze_instructions_mcp.md 는 2026-07-12 판
  수동 사본(114,732B)이라 원본(cowork_instructions.md, 2026-08-30 v11.33 264KB)보다 7주 낡았고, 원장 A24 가
  철회한 '숏 86%(31/36) 최우선 신뢰'·'66%(12/18) 변곡점 동전던지기(neutral 허용)' 를 그대로 담고 있었다.
  게다가 그 사본의 [MCP 모드] 머리는 '수집기 13종을 한 줄씩 실행' 표를 담고 있어 CLAUDE.md 가 기록한
  07-29 사고(지시문 표가 마스터보다 낡아 삭제된 수집기를 실행)와 같은 지뢰였다.
  → 사본은 손으로 만들지 않는다. 이 스크립트가 **현행 원본 + 짧은 [MCP 모드] 머리**로 생성하고, 머리에
  원본 sha256 마커를 남겨 report_lint.py --rules 가 사본이 낡았는지 대조한다(DOC_COPY=stale).

[사용] python build_mcp_instructions.py            (두 위치 모두 재생성; stock_research_mcp 가 없으면 건너뜀)
       python build_mcp_instructions.py --check    (쓰지 않고 낡았는지만 판정; exit 1 이면 stale)
"""
from __future__ import annotations

import os
import sys
import hashlib
import argparse
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "cowork_instructions.md")
TARGETS = [
    os.path.join(HERE, "setup_kit", "02_코워크_예정작업", "analyze_instructions_mcp.md"),
    os.path.join(os.path.dirname(HERE), "stock_research_mcp", "analyze_instructions_mcp.md"),
]

HEADER = """# Cowork 지시사항 (MCP판·자동 생성 사본) — 한국 주식 리서치 데스크 분석가 (analyze/아침분석)
<!-- built by build_mcp_instructions.py from cowork_instructions.md sha256:{sha} at {ts} —
     이 파일을 손으로 고치지 마라(다음 빌드가 덮어쓴다). 판단 규칙은 원본 cowork_instructions.md 를 고쳐라. -->

================================================================
[MCP 모드] ★최우선★ — 이 섹션이 아래 본문의 flag/핸드셰이크·supervisor 서술을 '대체'한다
================================================================
- 이 파일은 `cowork_instructions.md`(판단 규칙 원본)의 **자동 생성 사본**이다. 절차의 정본은
  `코워크_통합지시_최종.md` **PART A** 이고 이 사본은 참고용 폴백이다 — 둘이 충돌하면 PART A 를 따른다.
- 본문의 `[0.7]/[0.8] RUN_NOW` · `[1] COLLECT_DONE 대기` · `[2]~[3] DEEP_DONE 대기` · `report-done 신호` ·
  "샌드박스라 직접 실행 금지" · "30초 간격 30분 대기" · "supervisor/호스트가 ~한다" 는 전부
  **데몬 시대(2026-07-25 이전) 문법**이다 — 무시하라. 지금은 네가 터미널 MCP(run_command / wait_job /
  read_file / list_directory)로 **직접 실행**한다(본문 [5.15] '온디맨드 정정'과 같은 뜻).
- 판단의 '내용'([분석가 역할] · [3]~[6.7] 게이트 · [7] 리포트 양식 · [7.5] predictions.json 스키마 ·
  판단 원칙 · 이모지 금지)은 **아래 본문 그대로** 따른다. 오케스트레이션만 MCP 로 바뀐 것이다.

[작업 폴더] WORK = 이 저장소 루트(cwd). run_command 는 shell:"cmd" 로(PowerShell 엔 python 이 PATH 에 없을 수 있다).
[경로 따옴표 금지] `--session "경로"` 는 따옴표가 인자에 포함돼 '세션 없음' 오류 — 따옴표 없이 준다.
[긴 명령] MCP 전송 타임아웃은 약 60초 — `wait_ms` 30000 으로 job_id 를 먼저 받고 `wait_job` 으로 폴링한다.
  끊겨도 명령은 계속 돈다(재실행 금지 — 산출물 파일이 생겼는지 list_directory 로 확인).

── 실행 순서(거래일 아침 — CLAUDE.md 'CLI 진실표'와 동일) ──
0) `python accuracy_tracker.py`                           → scorecard.md 갱신([0.5] 자기보정 입력)
1) 06:05 `python run_signals.py --stage early --make-session`  → 야간선물·카이로스 4단계
2) 06:20 `python run_signals.py --stage main`             → collect 포함 나머지 17단계(순서·기준일 검증이 코드에 고정)
   ★ **개별 수집기를 한 줄씩 베껴 실행하지 마라** — 2026-07-29 에 낡은 표를 따라 삭제된 수집기를 돌리고
     6단계를 통째로 빠뜨린 사고가 있다. 요약표의 OK / STALE / EMPTY / FAIL 을 읽어라. '파일 존재'는 성공이 아니다.
3) 세션 폴더(`output\\YYYY-MM-DD_HHMMSS` — `_archive`·`_designtest` 제외)의 산출물을 read_file 로 읽고
   본문 [2] 형식으로 `commands.txt` 작성
4) `python research_agent.py deep --session <세션>`       → wait_job; `02_deep_collection.md` 끝의 완료 마커 확인
5) 분석 → `03_final_report.md` + `predictions.json`([7]/[7.5]) → `python report_lint.py --session <세션>`
6) `python research_agent.py mail --session <세션> --method appscript` → MAIL_RESULT=success 확인(실패 시 재시도 금지·보고)
7) `python recommend_track.py`
※ 주말·휴장일은 run_signals 가 스스로 거부한다 — `python weekend_collect.py`(예측 발행 금지).
※ 이 요약이 낡았으면 `코워크_통합지시_최종.md` PART A 가 정본이다.

================================================================
아래부터 원본 `cowork_instructions.md` 전문(자동 삽입 — 절차 문장은 위 [MCP 모드] 가 대체한다)
================================================================

"""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def build(check_only: bool = False) -> int:
    with open(SRC, encoding="utf-8", errors="replace") as f:
        body = f.read()
    sha = _sha(body)
    out = HEADER.format(sha=sha, ts=datetime.now().strftime("%Y-%m-%d %H:%M")) + body
    stale = 0
    for t in TARGETS:
        if not os.path.isdir(os.path.dirname(t)):
            print("[build] 대상 폴더 없음 — 건너뜀: %s" % os.path.dirname(t))
            continue
        cur = ""
        if os.path.isfile(t):
            with open(t, encoding="utf-8", errors="replace") as f:
                cur = f.read(4000)
        is_fresh = ("sha256:%s" % sha) in cur
        if check_only:
            print("[check] %s — %s" % (t, "최신" if is_fresh else "낡음/마커 없음"))
            stale += 0 if is_fresh else 1
            continue
        tmp = t + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(out)
        os.replace(tmp, t)
        print("[build] 생성: %s (%d B, 원본 sha256:%s)" % (t, len(out.encode("utf-8")), sha))
    return 1 if (check_only and stale) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="MCP판 아침 지시서 사본 재생성")
    ap.add_argument("--check", action="store_true", help="쓰지 않고 낡았는지만 판정(stale 이면 exit 1)")
    args = ap.parse_args()
    return build(check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
