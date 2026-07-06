#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch_and_analyze.py — 세력 강도 자동 분석 워처

[목적]
  research_agent.py collect 가 새 세션 폴더 + COLLECT_DONE.flag 를 만들면,
  자동으로 force_analysis.py 를 실행해 세션 폴더에 force_scores.json 을 저장한다.
  Cowork(샌드박스)는 그 json 을 읽기만 하면 된다 — Cowork 가 force_analysis 를
  직접 실행할 필요가 없다(샌드박스에서는 pykrx 설치/실행 불가).

[동작]
  주기적으로(기본 30초) output/ 디렉토리를 스캔.
  COLLECT_DONE.flag 가 있고 force_scores.json 이 아직 없는 세션 폴더를 찾으면:
    1) watch_tickers.txt 에서 6자리 종목 코드 로드 (# 주석 허용)
    2) python force_analysis.py --ticker A,B,C,... --market --json 실행
    3) 결과를 세션폴더/force_scores.json 로 원자적 저장
    4) logs/watch_analyzer.log 에 기록
  이미 force_scores.json 이 있으면 건너뜀(중복 실행 방지).

[등록]
  register_analyzer.bat 을 관리자 권한으로 실행 → 작업 스케줄러에 로그온 시 자동 시작.

[중지]
  schtasks /end /tn "StockResearchAnalyzer"
  또는 실행 중인 파이썬 프로세스 종료(Ctrl+C 가능한 콘솔이면).

[원칙]
  - 기존 코드(research_agent.py / watch_and_send.py / force_analysis.py)는
    일절 호출만 하고 절대 수정하지 않는다.
  - force_analysis.py 실패해도 다음 세션은 계속 감시. 워처 자체는 죽지 않는다.
"""

import os
import re
import sys
import json
import time
import argparse
import subprocess
from datetime import datetime
from pathlib import Path


# ── 경로 / 상수 ────────────────────────────────────────────────
BASE_DIR     = Path(__file__).resolve().parent
OUTPUT_DIR   = BASE_DIR / "output"
TICKERS_FILE = BASE_DIR / "watch_tickers.txt"
FORCE_PY     = BASE_DIR / "force_analysis.py"
LOG_DIR      = BASE_DIR / "logs"
LOG_FILE     = LOG_DIR / "watch_analyzer.log"
RESULT_NAME  = "force_scores.json"
TRIGGER_FLAG = "COLLECT_DONE.flag"

# force_analysis.py 한 번 실행에 줄 최대 시간(초). 종목 수×수급조회 시간 고려.
FORCE_TIMEOUT_SEC = 600

# 한 사이클에서 처리할 세션 폴더 최대 개수(과부하 방지)
MAX_PER_CYCLE = 3


# Windows 콘솔 UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [analyzer] {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_tickers() -> list:
    """watch_tickers.txt 에서 6자리 종목코드 추출. # 주석 / 공백 무시."""
    if not TICKERS_FILE.exists():
        log(f"🟡 {TICKERS_FILE.name} 없음 — 빈 풀로 진행(분석 스킵됨)")
        return []
    tickers = []
    seen = set()
    try:
        for raw in TICKERS_FILE.read_text(encoding="utf-8").splitlines():
            code = raw.split("#")[0].strip()
            if code and code.isdigit() and len(code) == 6:
                if code not in seen:
                    tickers.append(code)
                    seen.add(code)
    except Exception as e:
        log(f"🔴 {TICKERS_FILE.name} 읽기 실패: {e}")
        return []
    return tickers


SKIP_MARKER = "force_scores.SKIPPED"   # recover.py 가 남기는 '재시도 중단' 마커
MAX_FAIL_PER_SESSION = 3               # 세션당 연속 실패 이 횟수 넘으면 자동 SKIP(무한재시도 방지)
_fail_counts = {}                      # {session_name: 연속 실패 횟수}


def find_pending_sessions() -> list:
    """
    COLLECT_DONE.flag 있고 force_scores.json 없는 세션 폴더 목록.
    단, force_scores.SKIPPED 마커가 있으면 제외(이전에 끝내 실패해 재시도 포기한 세션).
    → KRX 403 등으로 force_analysis 가 계속 실패할 때 30초마다 영원히 재시도하는 사태 방지.
    """
    if not OUTPUT_DIR.exists():
        return []
    pending = []
    try:
        for d in sorted(OUTPUT_DIR.iterdir(), key=lambda p: p.stat().st_mtime):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            if (d / SKIP_MARKER).exists():
                continue   # 재시도 포기된 세션 — 건너뜀
            if (d / TRIGGER_FLAG).exists() and not (d / RESULT_NAME).exists():
                pending.append(d)
    except Exception as e:
        log(f"🔴 output/ 스캔 실패: {e}")
    return pending


def run_force_analysis(session_dir: Path, tickers: list) -> bool:
    """
    force_analysis.py 를 subprocess 로 실행, 결과를 세션폴더/force_scores.json 으로 저장.
    원자적 저장(.tmp → rename) 으로 중도 중단 시 부분 파일 남지 않게.
    """
    if not FORCE_PY.exists():
        log(f"🔴 {FORCE_PY.name} 없음 — 분석 스킵")
        return False
    if not tickers:
        log(f"🟡 {session_dir.name}: 종목 풀 비어있음 — 스킵 "
            f"(watch_tickers.txt 채우세요)")
        return False

    ticker_str = ",".join(tickers)
    tmp_path = session_dir / (RESULT_NAME + ".tmp")
    out_path = session_dir / RESULT_NAME

    cmd = [sys.executable, str(FORCE_PY),
           "--ticker", ticker_str, "--market", "--json"]

    log(f"▶ {session_dir.name} 분석 시작 ({len(tickers)}종목, "
        f"timeout={FORCE_TIMEOUT_SEC}s)")
    t0 = time.time()
    try:
        result = subprocess.run(
            cmd, cwd=str(BASE_DIR), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=FORCE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        log(f"🔴 {session_dir.name} 타임아웃 ({FORCE_TIMEOUT_SEC}s)")
        return False
    except Exception as e:
        log(f"🔴 {session_dir.name} subprocess 예외: {type(e).__name__}: {e}")
        return False

    dt = time.time() - t0
    if result.returncode != 0:
        log(f"🔴 {session_dir.name} 비정상 종료 rc={result.returncode} "
            f"({dt:.1f}s) stderr={(result.stderr or '')[:300]}")
        return False
    if not result.stdout or not result.stdout.strip():
        log(f"🔴 {session_dir.name} 빈 출력 ({dt:.1f}s)")
        return False

    # pykrx 라이브러리가 stdout으로 직접 print 하는 진단 메시지("KRX 로그인 실패",
    # "Error occurred in ..." 등)가 JSON 앞뒤에 섞일 수 있다. 첫 '{'부터
    # 마지막 '}' 까지만 추출해 정제한다.
    raw = result.stdout
    m = re.search(r"(\{[\s\S]*\})\s*\Z", raw)
    if not m:
        # 끝에 garbage가 있으면 처음 {부터 마지막 }까지 탐욕적으로
        m = re.search(r"(\{[\s\S]*\})", raw)
    if not m:
        log(f"🔴 {session_dir.name} stdout 에서 JSON 추출 실패 "
            f"(head={raw[:150]!r})")
        return False
    json_text = m.group(1)
    try:
        json.loads(json_text)   # 형식 검증
    except json.JSONDecodeError as e:
        log(f"🔴 {session_dir.name} JSON 파싱 실패: {e} "
            f"(head={json_text[:150]!r})")
        return False

    # 원자적 저장 (정제된 JSON만)
    try:
        tmp_path.write_text(json_text, encoding="utf-8")
        os.replace(tmp_path, out_path)
    except Exception as e:
        log(f"🔴 {session_dir.name} 파일 저장 실패: {e}")
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        return False

    log(f"🟢 {session_dir.name} 완료 ({dt:.1f}s) → {out_path.name} "
        f"({out_path.stat().st_size:,} bytes)")
    return True


def _mark_skip(sess, reason: str):
    """세션에 SKIP 마커를 남겨 더는 재시도하지 않게 한다."""
    try:
        with open(sess / SKIP_MARKER, "w", encoding="utf-8") as f:
            f.write(f"auto-skipped at {datetime.now().isoformat()}\n{reason}\n")
        log(f"🟡 {sess.name}: 연속 실패 {MAX_FAIL_PER_SESSION}회 초과 → {SKIP_MARKER} "
            f"생성(재시도 중단). 복구하려면 마커 삭제 후 recover.py 실행")
    except Exception:
        pass


def cycle(tickers: list) -> int:
    """한 폴링 사이클. 처리한 세션 수 반환."""
    pending = find_pending_sessions()
    if not pending:
        return 0
    processed = 0
    for sess in pending[:MAX_PER_CYCLE]:
        try:
            if run_force_analysis(sess, tickers):
                processed += 1
                _fail_counts.pop(sess.name, None)   # 성공 시 실패카운트 리셋
            else:
                # 연속 실패 누적 → 한도 넘으면 SKIP 마커(무한재시도 방지)
                _fail_counts[sess.name] = _fail_counts.get(sess.name, 0) + 1
                if _fail_counts[sess.name] >= MAX_FAIL_PER_SESSION:
                    _mark_skip(sess, f"force_analysis {MAX_FAIL_PER_SESSION}회 연속 실패")
        except Exception as e:
            log(f"🔴 {sess.name} 사이클 예외(워처는 계속): {type(e).__name__}: {e}")
            _fail_counts[sess.name] = _fail_counts.get(sess.name, 0) + 1
            if _fail_counts[sess.name] >= MAX_FAIL_PER_SESSION:
                _mark_skip(sess, f"사이클 예외 {MAX_FAIL_PER_SESSION}회: {type(e).__name__}")
    return processed


def main():
    ap = argparse.ArgumentParser(
        description="force_analysis 자동 실행 워처 — COLLECT_DONE.flag 감지 시 동작.")
    ap.add_argument("--interval", type=int, default=30,
                    help="폴링 간격(초). 기본 30")
    ap.add_argument("--once", action="store_true",
                    help="한 사이클만 돌고 종료(디버그용)")
    args = ap.parse_args()

    interval = max(5, args.interval)
    log("=" * 60)
    log("watch_and_analyze 시작")
    tickers = load_tickers()
    log(f"종목 풀: {len(tickers)}개 (출처: {TICKERS_FILE.name})")
    log(f"폴링 간격: {interval}s | once={args.once} | "
        f"force_timeout={FORCE_TIMEOUT_SEC}s | cycle_max={MAX_PER_CYCLE}")
    log(f"감시 폴더: {OUTPUT_DIR}")
    log("=" * 60)

    if args.once:
        n = cycle(tickers)
        log(f"--once 모드: {n}개 처리 후 종료")
        return

    last_tickers_mtime = TICKERS_FILE.stat().st_mtime if TICKERS_FILE.exists() else 0
    while True:
        try:
            # watch_tickers.txt 가 변경되면 재로딩(편집 즉시 반영)
            if TICKERS_FILE.exists():
                m = TICKERS_FILE.stat().st_mtime
                if m != last_tickers_mtime:
                    tickers = load_tickers()
                    last_tickers_mtime = m
                    log(f"🔄 종목 풀 갱신: {len(tickers)}개")
            cycle(tickers)
        except KeyboardInterrupt:
            log("종료 요청(Ctrl+C)")
            break
        except Exception as e:
            log(f"🔴 메인 루프 예외(계속): {type(e).__name__}: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
