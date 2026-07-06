#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
precollect.py — 새벽 1차 수집(precollect).

[목적]
  새벽 2시(supervisor 의 PRECOLLECT_TIME)에 '기존 뉴스 수집'을 미리 1회 돌려
  precollect/<날짜>/precollect_<HHMM>.md 로 저장한다.
  output 세션 폴더나 COLLECT_DONE.flag 를 만들지 않으므로,
  아침 분석(Cowork)을 새벽 2시에 조기 트리거하지 않는다.

  아침 본수집(supervisor.run_morning_pipeline) 중 `python precollect.py --merge` 가
  호출되어, 오늘자 1차 수집분을 방금 만들어진 아침 세션에 00_precollect.md 로 합친다.
  그러면 Cowork 가 1차+본수집을 함께 읽고 분석한다(중복 기사는 Cowork 가 한 번만 셈).

[사용법]
  python precollect.py            # 1차 수집 1회 -> precollect/<오늘>/precollect_<HHMM>.md
  python precollect.py --merge    # 오늘자 1차 수집분을 최근 아침 세션 00_precollect.md 로 합침
  python precollect.py --limit 8  # 소스당 기사 수 조절(기본 research_agent.ARTICLES_PER_SOURCE)

[주의]
  - research_agent.py 는 수정 금지. import 하여 run_broad_collection() 만 호출한다.
  - 콘솔 print() 는 ASCII only(cp949 안전). 파일 IO 는 UTF-8.
  - 환경변수 DISABLE_GEMINI=1 이면 Gemini 본문복구 끔(키 비움 -> 분석은 Cowork 담당).
    SELENIUM_USE 는 research_agent import 시점에 읽힘(supervisor 가 자식 환경에 설정).
"""
import os
import sys
import argparse
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

OUTPUT_DIR = os.path.join(BASE_DIR, "output")
PRECOLLECT_DIR = os.path.join(BASE_DIR, "precollect")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _log(msg: str):
    # ASCII only — cp949 콘솔에서 안전(이모지 금지)
    try:
        print(f"[precollect] {msg}", flush=True)
    except Exception:
        pass


def _disable_gemini() -> bool:
    v = (os.environ.get("DISABLE_GEMINI") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


# =====================================================================
# 1차 수집: 기존 뉴스 수집(research_agent.run_broad_collection)을 1회 실행
# =====================================================================
def do_collect(limit_arg) -> int:
    # research_agent 는 무거우니 필요 시점에만 import (이 파일의 단순 import 는 가볍게).
    import research_agent as ra

    # 소스당 기사 수 — 기본은 research_agent.ARTICLES_PER_SOURCE
    try:
        default_limit = int(getattr(ra, "ARTICLES_PER_SOURCE", 12))
    except Exception:
        default_limit = 12
    limit = int(limit_arg) if limit_arg else default_limit

    # Gemini 키 — DISABLE_GEMINI 면 비움(본문복구 생략, 본문 분석은 Cowork 담당)
    if _disable_gemini():
        keys = []
        _log("DISABLE_GEMINI=on -> Gemini bonmun fallback off (keys empty)")
    else:
        try:
            keys = ra.load_gemini_keys()
        except Exception as e:
            keys = []
            _log(f"load_gemini_keys failed ({type(e).__name__}) -> keys empty")

    sel = (os.environ.get("SELENIUM_USE") or "").strip()
    _log(f"start broad collection (limit={limit}, gemini_keys={len(keys)}, "
         f"selenium_env={sel or 'unset'})")

    # 기존 광역 수집 그대로 사용. use_pw=False(아침 step1 의 --no-playwright 와 동일).
    md = ""
    try:
        md = ra.run_broad_collection(limit, keys, use_pw=False)
    except Exception as e:
        _log(f"run_broad_collection error: {type(e).__name__}: {e}")
        md = ""
    md = md or ""

    # 저장 (수집 0건이어도 파일은 남겨 추적 가능)
    day = _today()
    day_dir = os.path.join(PRECOLLECT_DIR, day)
    try:
        os.makedirs(day_dir, exist_ok=True)
    except Exception as e:
        _log(f"mkdir failed: {type(e).__name__}: {e}")
        return 0
    hhmm = datetime.now().strftime("%H%M")
    out_path = os.path.join(day_dir, f"precollect_{hhmm}.md")
    header = (f"# [새벽 1차 수집] {datetime.now().isoformat(timespec='seconds')} "
              f"(limit={limit}, chars={len(md)})\n\n")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(header)
            f.write(md)
        _log(f"saved -> {out_path} (chars={len(md)})")
    except Exception as e:
        _log(f"save failed: {type(e).__name__}: {e}")
        return 0
    return 0


# =====================================================================
# --merge: 오늘자 1차 수집분을 최근 아침 세션에 00_precollect.md 로 합침
# =====================================================================
def _today_precollect_files(day: str):
    day_dir = os.path.join(PRECOLLECT_DIR, day)
    if not os.path.isdir(day_dir):
        return []
    files = [os.path.join(day_dir, n) for n in os.listdir(day_dir)
             if n.lower().endswith(".md") and
             os.path.isfile(os.path.join(day_dir, n))]
    files.sort()  # precollect_HHMM.md -> 시각순
    return files


def _latest_today_session(day: str) -> str:
    """output/<오늘>_* 중 가장 최근 세션 폴더(이름 정렬 최신). 없으면 ''.
    세션 폴더명은 research_agent.new_session_dir() 규칙: YYYY-MM-DD_HHMMSS[_n].
    _ 로 시작하는 폴더(_archive 등)는 제외."""
    if not os.path.isdir(OUTPUT_DIR):
        return ""
    prefix = day + "_"
    dirs = [n for n in os.listdir(OUTPUT_DIR)
            if (not n.startswith("_"))
            and n.startswith(prefix)
            and os.path.isdir(os.path.join(OUTPUT_DIR, n))]
    if not dirs:
        return ""
    dirs.sort()  # YYYY-MM-DD_HHMMSS[_n] -> 사전식 정렬 = 시각순
    return os.path.join(OUTPUT_DIR, dirs[-1])


def do_merge() -> int:
    day = _today()
    files = _today_precollect_files(day)
    if not files:
        _log(f"no precollect for today ({day}) -> skip merge")
        return 0
    sess = _latest_today_session(day)
    if not sess:
        _log(f"no today session (output/{day}_*) -> skip merge")
        return 0

    parts = [
        "# 00 새벽 1차 수집(precollect) — 본수집(01_broad 등)과 함께 분석할 것.\n"
        "# 같은 기사/사건이 본수집과 중복되면 한 번만 셀 것(중복 가중 금지).\n\n"
    ]
    used = 0
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                body = f.read()
        except Exception as e:
            _log(f"read failed {os.path.basename(fp)}: {type(e).__name__}")
            continue
        parts.append(f"\n--- ({os.path.basename(fp)}) ---\n\n")
        parts.append(body)
        used += 1

    if used == 0:
        _log("all precollect files unreadable -> skip merge")
        return 0

    out_path = os.path.join(sess, "00_precollect.md")
    merged = "".join(parts)
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(merged)
    except Exception as e:
        _log(f"write 00_precollect.md failed: {type(e).__name__}: {e}")
        return 0
    _log(f"merged {used} file(s) -> {out_path} "
         f"(bytes={len(merged.encode('utf-8'))})")
    _log(f"session = {sess}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="새벽 1차 수집(precollect): 기존 뉴스 수집을 미리 1회 저장 / "
                    "--merge 로 아침 세션 00_precollect.md 에 합침")
    ap.add_argument("--merge", action="store_true",
                    help="오늘자 1차 수집분을 최근 아침 세션 00_precollect.md 로 합침")
    ap.add_argument("--limit", type=int, default=None,
                    help="소스당 기사 수(기본 research_agent.ARTICLES_PER_SOURCE)")
    args = ap.parse_args()

    if args.merge:
        return do_merge()
    return do_collect(args.limit)


if __name__ == "__main__":
    sys.exit(main() or 0)
