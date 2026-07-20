#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
supervisor.py ─ 통합 상시 감시·명령 데몬 (이것 하나만 켜두면 됨)

[목적]
  작업 스케줄러 3개(MorningAuto / Analyzer / Watcher)를 하나의 파이썬 프로세스로
  통합한다. 24시간 켜둔 PC에서 이 프로그램 하나만 돌리면:

    1) 매일 정해진 시각(MORNING_TIME, 기본 06:30) → 광역수집 자동 실행
       (.bat 없이 파이썬이 직접: research_agent collect → postprocess
        → 빈약 시 api_collect 폴백 → collection_report. run_morning_pipeline() 참조)
    2) COLLECT_DONE.flag 감지 → force_analysis.py 실행 → force_scores.json 생성
       (watch_and_analyze.cycle 재사용)
    2.5) commands.txt(Cowork 발행) 감지 → research_agent deep 자동 실행 → DEEP_DONE.flag
    3) REPORT_DONE.flag 감지 → Apps Script 웹앱으로 메일 발송
       (watch_and_send.scan_once 재사용)

  → flag 감지(감시)와 시각 기반 수집(명령)을 한 루프에서 처리. 30초 주기.

[원칙]
  - research_agent.py 는 수정하지 않는다. (호출/ import 만)
  - 메일·force_analysis 로직은 watch_and_send.py / watch_and_analyze.py 를 재사용
    (코드 중복 없음). 이 두 파일이 같은 폴더에 있어야 한다.

[실행]
  python supervisor.py            # 상시 실행 (보통 start_supervisor_visible.bat 이 띄움)
  python supervisor.py --once     # 1회만(테스트): 시각/플래그 한 번 처리
  python supervisor.py --no-morning   # 자동 수집 비활성(감시만)
  ※ 수집 시각은 위의 MORNING_TIME 변수로 지정한다(인자 --morning-time 도 가능).

[자동 시작]
  보이는 콘솔 창으로 로그온 시 자동 시작 — Windows 시작프로그램 폴더의
  바로가기(StockResearchSupervisor.lnk) → start_supervisor_visible.bat → 이 파일.
  죽거나 얼면 StockResearchWatchdog(5분 주기) → watchdog.py 가 보이는 창으로 재가동.
"""

import os
import re
import sys
import json
import time
import argparse
import subprocess
import threading
from datetime import datetime

# Windows 콘솔 UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
LOG_DIR     = os.path.join(BASE_DIR, "logs")
STATE_FILE  = os.path.join(BASE_DIR, "supervisor_state.json")
LOCK_FILE   = os.path.join(BASE_DIR, "supervisor.lock")
HEARTBEAT_FILE = os.path.join(BASE_DIR, "supervisor_heartbeat.txt")
# 하트비트가 이 시간(초)보다 오래되면 '살아있지만 얼어붙은' 것으로 간주.
# watchdog.py 와 acquire_lock 이 이 값으로 프리즈를 판정해 자동 복구한다.
# 주의: force_analysis 한 사이클이 정상적으로 최대 600초까지 걸릴 수 있으므로
#       그보다 충분히 크게 둔다(오탐 방지). 900초면 정상작업은 통과, 진짜 프리즈는 복구.
HEARTBEAT_STALE_SEC = 900

# ── ★ 테스트용: 수집 시작 시각 하드코딩 ─────────────────────────────
# 매일 이 시각이 지나면 collect(광역수집)부터 파이프라인을 1회 트리거한다.
# 평소엔 "06:30". 테스트할 땐 이 값만 바꿔 저장하면 supervisor 가 25초 내
# 자동 반영(재시작 불필요)해 그 시각에 수집을 돌린다. 예: "14:05"
# (명령행에서 --morning-time 을 주면 그 값이 이 기본값을 덮어쓴다.)
MORNING_TIME = "06:30"

# ── 새벽 1차 수집(precollect) 시각 ───────────────────────────────────
# 이 시각이 지나면 '기존 뉴스 수집'을 미리 1회 돌려 precollect/<날짜>/ 에만 저장한다.
# (output 세션이나 COLLECT_DONE.flag 를 만들지 않으므로 아침 분석을 조기 트리거하지 않음.)
# 아침 MORNING_TIME 본수집 때 이 1차 수집분을 세션에 00_precollect.md 로 합쳐
# Cowork 가 1차+본수집을 함께 분석한다(중복 기사는 Cowork 가 한 번만 셈).
# MORNING_TIME 과 마찬가지로 이 값만 바꿔 저장하면 25초 내 자동 반영(재시작 불필요).
PRECOLLECT_TIME = "02:00"

# ── 회고분석(PART C) 시각 ────────────────────────────────────────────
# 이 시각이 지나면 retro_label.py 로 '과거 추천 → 실제결과' 학습 데이터셋을 만들고,
# retro_forward.py --push 로 바탕화면 회고 Cowork 폴더(inbox)로 전달한다(RETRO_GO.flag).
# 아침 본수집(06:30)보다 일찍 돌려, 회고 Cowork 가 만든 'PART A 추가지시'가 그날 분석에 반영되게 한다.
# PRECOLLECT_TIME 과 마찬가지로 이 값만 바꿔 저장하면 25초 내 자동 반영(재시작 불필요).
RETRO_TIME = "03:30"

os.makedirs(LOG_DIR, exist_ok=True)

# 메일/force_analysis 로직 재사용 (같은 폴더의 워처 모듈)
import watch_and_send as wsend
import watch_and_analyze as wanalyze
# 분석 완료(REPORT_DONE) 리포트를 모의투자 Cowork 폴더로 전달하는 독립 모듈(선택 기능).
# 임포트 실패해도 supervisor 본체는 계속 동작하도록 soft import.
try:
    import mock_forward as mforward
except Exception:
    mforward = None
# 회고분석(PART C) 양방향 브리지(데이터셋 push + 피드백 회수). soft import.
try:
    import retro_forward as rforward
except Exception:
    rforward = None
# 추천 종목 추적(메일 발송 후 이력·수집유니버스 저장 + 회고 폴더 전달). soft import.
try:
    import recommend_track as rtrack
except Exception:
    rtrack = None


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [supervisor] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(LOG_DIR, "supervisor.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# =====================================================================
# 상태 파일 (마지막 morning 수집 실행일)
# =====================================================================
def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        # 손상 시 격리 후 빈 상태
        try:
            os.replace(STATE_FILE, STATE_FILE + ".corrupt")
        except Exception:
            pass
        return {}


def save_state(state: dict):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"⚠️ 상태 저장 실패: {e}")


# =====================================================================
# 단일 인스턴스 락
# =====================================================================
def write_heartbeat():
    """현재 시각(epoch)을 하트비트 파일에 원자적으로 기록 — 루프가 살아있다는 증거."""
    try:
        tmp = HEARTBEAT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
        os.replace(tmp, HEARTBEAT_FILE)
    except Exception:
        pass


def heartbeat_age() -> float:
    """마지막 하트비트로부터 경과 초. 파일 없거나 손상 시 매우 큰 값(=오래됨)."""
    try:
        with open(HEARTBEAT_FILE, encoding="utf-8") as f:
            ts = int(f.read().strip())
        return time.time() - ts
    except Exception:
        return 1e9


def _kill_pid(pid: int) -> bool:
    """프리즈된 옛 인스턴스 강제 종료. 성공 시 True."""
    try:
        if sys.platform.startswith("win"):
            os.system(f'taskkill /F /PID {pid} >nul 2>&1')
        else:
            os.kill(pid, 9)
        return True
    except Exception:
        return False


def acquire_lock() -> bool:
    """
    단일 인스턴스 보장 + '살아있지만 얼어붙은' 인스턴스 자동 인수.
      - 락 PID 가 살아있고 하트비트가 신선(<STALE) → 정상 가동 중 → 이 인스턴스 종료(False)
      - 락 PID 가 살아있지만 하트비트가 오래됨(>=STALE) → 프리즈로 판정 → 강제종료 후 인수
      - 락 PID 가 죽음 → 인수
    """
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE, encoding="utf-8") as f:
                old = f.read().strip()
            if old.isdigit() and _pid_alive(int(old)):
                age = heartbeat_age()
                if age < HEARTBEAT_STALE_SEC:
                    log(f"⚠️ supervisor 정상 가동 중(PID {old}, 하트비트 {age:.0f}s 전) "
                        f"— 이 인스턴스 종료")
                    return False
                log(f"🩺 프리즈 감지: 기존 PID {old} 가 살아있으나 하트비트 {age:.0f}s "
                    f"(>={HEARTBEAT_STALE_SEC}s) — 강제 종료 후 인수")
                _kill_pid(int(old))
                time.sleep(2)
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        write_heartbeat()
        return True
    except Exception:
        return True


def release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        if sys.platform.startswith("win"):
            out = os.popen(f'tasklist /fi "PID eq {pid}" /nh').read()
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:
        return False


# =====================================================================
# 매일 수집 트리거 (시각 기반)
# =====================================================================
def _reload_morning_time() -> str:
    """supervisor.py 파일에서 MORNING_TIME 변수 값을 다시 읽는다(테스트 중 즉시 반영용).
    실패 시 현재 메모리의 MORNING_TIME 사용."""
    try:
        with open(os.path.abspath(__file__), encoding="utf-8") as f:
            for line in f:
                m = re.match(r'\s*MORNING_TIME\s*=\s*["\']([0-9]{1,2}:[0-9]{2})["\']', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return MORNING_TIME


def _reload_precollect_time() -> str:
    """supervisor.py 파일에서 PRECOLLECT_TIME 변수 값을 다시 읽는다(즉시 반영용).
    실패 시 현재 메모리의 PRECOLLECT_TIME 사용."""
    try:
        with open(os.path.abspath(__file__), encoding="utf-8") as f:
            for line in f:
                m = re.match(r'\s*PRECOLLECT_TIME\s*=\s*["\']([0-9]{1,2}:[0-9]{2})["\']', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return PRECOLLECT_TIME


def _reload_retro_time() -> str:
    """supervisor.py 파일에서 RETRO_TIME 변수 값을 다시 읽는다(즉시 반영용).
    실패 시 현재 메모리의 RETRO_TIME 사용."""
    try:
        with open(os.path.abspath(__file__), encoding="utf-8") as f:
            for line in f:
                m = re.match(r'\s*RETRO_TIME\s*=\s*["\']([0-9]{1,2}:[0-9]{2})["\']', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return RETRO_TIME


def _parse_hhmm(s: str) -> tuple:
    m = re.match(r"^\s*(\d{1,2})\s*:\s*(\d{2})\s*$", s or "")
    if not m:
        return 6, 30
    hh = max(0, min(23, int(m.group(1))))
    mm = max(0, min(59, int(m.group(2))))
    return hh, mm


MORNING_LOG = os.path.join(LOG_DIR, "morning_auto.log")
_morning_thread = None   # 현재 실행 중인 morning 파이프라인 스레드(중복 실행 방지)

# ── 수동 즉시 실행(RUN_NOW) ────────────────────────────────────────────
# Cowork(또는 사용자)가 BASE_DIR 에 RUN_NOW.flag 를 만들면, 시각과 무관하게 06:30 과
# '동일한' 수집 파이프라인(run_morning_pipeline)을 즉시 1회 실행한다. 파일의 '존재' 자체가
# 신호이며(내용은 읽지 않음), 감지 즉시 삭제해 중복 트리거를 막는다. last_morning_run 은
# 건드리지 않으므로 정규 06:30 수집과 독립적이다(필요하면 하루에 여러 번 새로고침 가능).
RUN_NOW_FLAG = os.path.join(BASE_DIR, "RUN_NOW.flag")
RUN_NOW_MAX_AGE_SEC = 3600   # 1시간 넘은 잔여 RUN_NOW.flag 는 무시(오래된 요청 오발 방지)
_manual_thread = None        # 현재 실행 중인 수동 파이프라인 스레드(중복 실행 방지)


def _mlog(msg: str):
    """morning 파이프라인 진행을 morning_auto.log 와 supervisor 콘솔에 함께 기록."""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [morning] {msg}"
    print(line, flush=True)
    try:
        with open(MORNING_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _kill_proc_tree(pid: int):
    """자식·손자까지 프로세스 트리를 통째로 강제 종료.
    Windows: taskkill /F /T (자식 트리 포함), POSIX: 프로세스 그룹에 SIGKILL.
    collect 가 남긴 셀레늄/크롬 손자 프로세스가 stdout 파이프를 붙잡아 communicate 가
    영구 블록되는 것을 끊기 위함."""
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            import signal as _sig
            os.killpg(os.getpgid(pid), _sig.SIGKILL)
    except Exception:
        pass


def _run_step(label: str, args: list, timeout: int, extra_env: dict = None) -> int:
    """research_agent 등 파이썬 스크립트를 직접 실행하고 출력을 로그에 남긴다. 반환=종료코드.

    [중요] subprocess.run(capture_output, timeout) 은 자식이 남긴 '손자 프로세스'(셀레늄/크롬 등)가
    stdout 파이프를 붙잡고 있으면 타임아웃 후에도 communicate 가 영구 블록될 수 있다(Windows).
    그러면 morning 파이프라인 스레드가 step1 collect 에서 멈춰 세션을 못 만든다(2026-06-22 사고).
    그래서 Popen + 타임아웃 시 프로세스 트리 강제종료(_kill_proc_tree)로 처리해, 멈춘 collect 가
    파이프라인 전체를 정지시키지 않게 한다."""
    _mlog(f"[{label}] 실행: {' '.join(args[1:])}")
    _t0 = time.time()                      # #L2 소요 측정
    child_env = dict(os.environ)
    if extra_env:
        child_env.update(extra_env)
    popen_kwargs = dict(
        cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=child_env,
    )
    if sys.platform.startswith("win"):
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        popen_kwargs["start_new_session"] = True   # 그룹 단위 종료 가능하게
    try:
        proc = subprocess.Popen(args, **popen_kwargs)
    except Exception as e:
        _mlog(f"[{label}] 실행 실패: {type(e).__name__}: {e}")
        return 1

    out, rc, status = "", 1, "종료코드 1"
    try:
        out, _ = proc.communicate(timeout=timeout)
        rc = proc.returncode
        status = f"종료코드 {rc}"
    except subprocess.TimeoutExpired:
        _mlog(f"[{label}] 타임아웃({timeout}s) — 프로세스 트리 강제 종료")
        _kill_proc_tree(proc.pid)
        # 트리를 죽였으니 파이프 writer 가 닫혀 communicate 가 곧 끝난다. 그래도 안전하게 짧게만.
        try:
            out, _ = proc.communicate(timeout=20)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            out = ""
        rc, status = 124, f"타임아웃({timeout}s) rc=124"
    except Exception as e:
        _mlog(f"[{label}] 예외: {type(e).__name__}: {e}")
        try:
            _kill_proc_tree(proc.pid)
        except Exception:
            pass
        return 1

    # 자식 출력(stdout+stderr 병합)은 파일에만 기록(콘솔 스팸 방지).
    try:
        if out:
            with open(MORNING_LOG, "a", encoding="utf-8") as f:
                f.write(out)
    except Exception:
        pass
    _mlog(f"[{label}] {status}")
    # #L2 구조화 헬스 로그(JSONL): 스텝별 성패·소요를 기계가 읽게 — 파이프라인 자체의 건강도 추적.
    try:
        with open(os.path.join(LOG_DIR, "pipeline_health.jsonl"), "a", encoding="utf-8") as _hf:
            _hf.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                                  "step": label, "rc": rc, "dur_s": round(time.time() - _t0, 1)},
                                 ensure_ascii=False) + chr(10))
    except Exception:
        pass
    return rc


def _notify_health(subject: str, body: str):
    """#K4 파이프라인 헬스 알림 — 검증된 Apps Script 채널 재사용. 실패는 조용히 무시(알림이 파이프라인을 못 깨게)."""
    try:
        import watch_and_send as _ws
        url, secret = _ws.load_appscript_config()
        to = _ws.load_recipients()
        if not (url and secret and to):
            return
        _ws.post_to_appscript(url, {"secret": secret, "to": to,
                                    "subject": "[헬스] " + subject,
                                    "htmlBody": "<pre>%s</pre>" % body})
        _mlog(f"[헬스알림] 발송: {subject}")
    except Exception as e:
        _mlog(f"[헬스알림] 실패(무시): {type(e).__name__}: {e}")


_status_lock = threading.Lock()   # #A9: morning·retro 스레드가 daily_status.json 을 동시 갱신할 때 유실 방지


def _record_daily_status(status: str, **extra):
    """#9 일일 상태 로그(daily_status.json): 날짜별 파이프라인 실행 상태를 누적 기록한다.
    회고 Cowork 가 '평일인데 추천 없음 vs 수집 실패 vs 휴장'을 구분하도록(요청서 #9). 회고 폴더에도 복사됨."""
    p = os.path.join(BASE_DIR, "daily_status.json")
    with _status_lock:                     # #A9
        return _record_daily_status_locked(p, status, extra)


def _record_daily_status_locked(p, status, extra):
    try:
        data = {}
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        ent = data.get(today, {})
        ent.update({"status": status, "weekday": now.strftime("%a"),
                    "is_weekend": now.weekday() >= 5,
                    "updated": now.isoformat(timespec="seconds")})
        ent.update(extra)
        data[today] = ent
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception as e:
        _mlog(f"[daily_status] 기록 실패: {type(e).__name__}")


def run_morning_pipeline():
    """
    .bat 없이 파이썬이 직접 수집 파이프라인을 순차 실행한다(별도 스레드에서 호출됨).
      step1) research_agent.py collect --no-playwright   (광역수집)
      step2) morning_postprocess.py --check-only          (폴백 필요 판단, 종료코드)
      step3) 종료코드!=0 이면 api_collect.py               (폴백 수집)
      step4) collection_report.py                          (검증 txt 저장)
    이후 핸드셰이크: COLLECT_DONE → (Cowork commands.txt) → supervisor deep 워처 → ...
    """
    py = sys.executable
    ra = os.path.join(BASE_DIR, "research_agent.py")
    _mlog("=" * 50)
    _mlog("morning 파이프라인 시작 (파이썬 직접 실행, bat 미사용)")

    # step0: 과거 예측 사후채점 → scorecard.md 갱신 (정확도 자기보정 루프).
    #   어제까지의 predictions.json 중 만기 도달분을 실제 주가로 채점해 점수표 갱신.
    #   분석가(Cowork)가 오늘 분석 시작 시 scorecard.md 를 읽어 자기보정한다.
    _run_step("step0 accuracy_tracker",
              [py, os.path.join(BASE_DIR, "accuracy_tracker.py")], timeout=600)

    # step1: 광역 수집
    # collect 는 광역수집(RSS/Naver)만. Gemini 본문복구는 끔(429 병목 회피, 분석은 Cowork 담당).
    # [내구성 — 2026-06-22/24 collect 동결 재발 방지]
    #   SELENIUM_USE=0: 셀레늄(undetected-chromedriver)은 드라이버 init/JS챌린지에서 워커
    #     스레드가 안 죽어 collect 를 30분 영구 동결시킨 주범. 시각 임계인 '아침 본수집'에서는
    #     끄고, 대신 requests → curl_cffi(TLS 지문 위장, 비-크로뮴) → r.jina.ai 리더로 봇차단을
    #     우회한다(셀레늄 우회는 02:00 precollect·수동 실행에만 남김).
    #   BROAD_WALL_SEC=540: 한 소스가 멈춰도 9분이면 완료분만으로 반환(run_broad_collection 캡)
    #     → 01_broad + COLLECT_DONE 이 반드시 생성돼 force_analysis·Cowork 핸드셰이크가 진행됨.
    #   _run_step timeout 1800→900: 이중 안전망(월타임 캡이 먼저 작동하므로 사실상 도달 안 함).
    _run_step("step1 collect", [py, ra, "collect", "--no-playwright"],
              timeout=900,
              extra_env={"DISABLE_GEMINI": "1", "SELENIUM_USE": "0",
                         "CFFI_USE": "1", "READER_USE": "1", "BROAD_WALL_SEC": "540"})

    # step1.5: 새벽 1차 수집(precollect)분을 방금 만들어진 아침 세션에 합치기.
    #   precollect/<오늘>/*.md → 세션 폴더의 00_precollect.md 로 복사(있을 때만).
    #   Cowork 가 1차+본수집을 함께 읽고 중복 기사는 한 번만 센다. 1차 수집이 없으면 무동작.
    _run_step("step1.5 precollect_merge",
              [py, os.path.join(BASE_DIR, "precollect.py"), "--merge"], timeout=180)

    # step2: 폴백 필요 여부 판단
    rc = _run_step("step2 postprocess",
                   [py, os.path.join(BASE_DIR, "morning_postprocess.py"), "--check-only"],
                   timeout=120)

    # step3: 수집이 빈약하면 api_collect 폴백
    if rc == 0:
        _mlog("step3: collect 충분 — API 폴백 스킵")
    else:
        _mlog(f"step3: collect 빈약(rc={rc}) — api_collect.py 폴백 실행")
        _run_step("step3 api_collect",
                  [py, os.path.join(BASE_DIR, "api_collect.py")], timeout=1800)

    # step4: 검증 리포트 txt
    _run_step("step4 collection_report",
              [py, os.path.join(BASE_DIR, "collection_report.py")], timeout=120)

    # step5: 시장 컨텍스트 수집 → 세션폴더 market_context.json
    #   인터마켓(美지수·VIX·DXY·UST10Y·환율 등) + 한국 섹터 RS + breadth + 외국인/기관 흐름
    #   + 시장국면(regime) 점수. 분석가가 top-down 방향성 판단에 사용.
    _run_step("step5 market_collect",
              [py, os.path.join(BASE_DIR, "market_collect.py")], timeout=600)

    # step6: 장투 펀더멘털 수집 → 세션폴더 fundamentals.json
    #   watch_tickers 풀의 연간 재무: 매출/영업이익 + GPM/OPM/FCF + 추세.
    #   소스: DART(dart_api.txt 키 있으면 공식) 우선, 없으면 yfinance 폴백(키 불필요).
    #   분석가가 [장투가능] 판정(마진 체력·잉여현금흐름)에 사용.
    _run_step("step6 dart_collect",
              [py, os.path.join(BASE_DIR, "dart_collect.py")], timeout=900)

    # step7: 과열/되돌림(차익실현) 압력 지표 → 세션폴더 overheat.json
    #   이격도·연속상승·52주고가·RSI·OBV다이버전스·20일급등률. 가격데이터 기반(키 불필요).
    #   분석가가 [3] 선반영/차익실현 판단에 사용(force_score 보완).
    _run_step("step7 overheat",
              [py, os.path.join(BASE_DIR, "overheat_collect.py")], timeout=600)

    # step8: DART 공시 물량 오버행 → 세션폴더 disclosures.json
    #   증자/CB·BW/자사주처분/대주주변동 등 '예측 가능한' 매물 이벤트. dart_api.txt 키 있을 때.
    _run_step("step8 disclosure",
              [py, os.path.join(BASE_DIR, "disclosure_collect.py")], timeout=600)

    # step9: 미래에셋 수급/시세 → 세션폴더 mirae_data.json
    #   외국인/기관/개인 일별 순매수 + 현재가·외국인보유율. mirae_api.txt 키 있을 때만(없으면 무동작).
    _run_step("step9 mirae_collect",
              [py, os.path.join(BASE_DIR, "mirae_collect.py")], timeout=600)

    # step10: 공매도 잔고/추세 → 세션폴더 short.json (KRX, krx_account.txt 로그인 시)
    #   공매도 잔고 비중·증감 = 하락 베팅·되돌림 압력. 차익실현 위험 보강([3-차익실현]).
    _run_step("step10 short_collect",
              [py, os.path.join(BASE_DIR, "short_collect.py")], timeout=600)

    # step11: 금융위(FSC) 공식 시세 → 세션폴더 fsc_prices.json (종가/등락률/거래량)
    #   메일 리포트의 추천·주의 종목을 'FSC 공식 시세'로 분석하도록 PART A 에 제공([5.6]).
    #   fsc_api.txt 키 있으면 FSC, 없으면 FinanceDataReader 폴백(키 발급 전에도 동작).
    _run_step("step11 fsc_collect",
              [py, os.path.join(BASE_DIR, "fsc_collect.py")], timeout=600)

    # step12: 투자자별 순매수(외국인/기관/개인) 추천시점 수급 → 세션 flow_data.json
    #   force_scores(KRX 수급)가 자주 결측되는 문제(회고 17일 반복 지적)를 보강하는 제2 수급원.
    #   외국인 연속순매도일수·시장 risk-off 신호 포함(F1/F3/F5 정량 게이트 입력).
    _run_step("step12 flow_collect",
              [py, os.path.join(BASE_DIR, "flow_collect.py"), "--collect"], timeout=600)

    # step13: 한국은행 ECOS 거시지표(기준금리·환율·국고채·코스피) → ecos_macro.json
    #   국면(regime)/위험회피(F1) 보강 — 지금까지 외국인 수급만 보던 한계를 거시로 보완([5.9]).
    #   ecos_api.txt 키 있으면 수집, 없으면 graceful 스킵(exit 0, 파이프라인 무영향).
    _run_step("step13 ecos_collect",
              [py, os.path.join(BASE_DIR, "ecos_collect.py")], timeout=300)

    # step14: 파생(옵션) 헤지신호 — KOSPI200 풋콜비율(PCR) + 개별주식 풋콜 → deriv_sentiment.json
    #   '세력·외인이 현물은 사면서도 보호풋으로 헤지'(F3/F5) 경계 + 시장 헤지(F1). KRX 무료(로그인 세션).
    _run_step("step14 deriv_collect",
              [py, os.path.join(BASE_DIR, "deriv_collect.py")], timeout=300)

    # step14.5: VKOSPI(변동성지수) — 금융위 지수시세 API → 루트 vkospi.json (F1 '공포' 판별 데이터)
    #   키(vkospi_api.txt 또는 fsc_api.txt 활용신청) 없으면 조용히 생략(graceful).
    _run_step("step14.5 vkospi_collect",
              [py, os.path.join(BASE_DIR, "vkospi_collect.py")], timeout=120)

    # step14.6: 신용잔고(빚투)·증시자금 — 금투협 freesis 공개 JSON → 루트 credit_balance.json
    #   레버리지 과열/디레버리징 국면 신호([5.12]). 키 불필요, 실패 시 graceful.
    _run_step("step14.6 credit_collect",
              [py, os.path.join(BASE_DIR, "credit_collect.py")], timeout=120)

    # step14.7: 실적발표 캘린더(한국, 향후 2주) — investing.com → 세션 earnings_calendar.json
    #   [4.8](5) 이벤트 경로 체크·픽 실적일정 대조용. 실패 시 웹검색 폴백(graceful).
    _run_step("step14.7 earnings_collect",
              [py, os.path.join(BASE_DIR, "earnings_collect.py")], timeout=120)

    # step15: 시장 국면 복합 게이트 — KOSPI 5일 + 파생 PCR + 외인 risk_off + 환율 합성 → market_caution.json
    #   회고 최강 발견('추천일 시장 과열이 결과 좌우')을 운영화(F1/F6). step12~14 산출물을 읽으므로 맨 뒤.
    _run_step("step15 market_caution",
              [py, os.path.join(BASE_DIR, "market_caution.py")], timeout=120)

    # step15.5: 루트 신호 4종(deriv/ecos/vkospi/market_caution)을 '오늘 세션'에 동결 복사.
    #   루트 파일은 매일 덮어써져 회고가 '그날 분석가가 본 국면 입력'을 재현할 수 없었다
    #   (회고 사각지대: F1/F8 게이트의 1차 입력이 학습에서 통째로 누락). market_caution 다음이어야
    #   13종 신호가 모두 확정된 상태를 찍고, step16(회고 폴더 복사) 앞이어야 같은 회차에 전달된다.
    _run_step("step15.5 snapshot_signals",
              [py, os.path.join(BASE_DIR, "snapshot_signals.py")], timeout=60)

    # step16: 아침 수집결과(수급/거시/파생/국면)도 회고 폴더 collections/<날짜>/ 로 복사 + 일일상태 기록(#9).
    _run_step("step16 push_collections",
              [py, os.path.join(BASE_DIR, "retro_forward.py"), "--push-collections"], timeout=180)
    _record_daily_status("morning_collected")

    _mlog("morning 파이프라인 종료 (이후 Cowork 핸드셰이크 대기)")
    _mlog("=" * 50)


def maybe_run_morning(morning_time: str, state: dict) -> bool:
    """
    오늘 아직 morning 수집을 안 했고 현재 시각이 morning_time 을 지났으면,
    run_morning_pipeline() 을 '별도 스레드'로 직접 실행(논블로킹). 실행했으면 True.
    .bat 을 거치지 않고 supervisor 파이썬이 research_agent 등을 바로 호출한다.
    """
    global _morning_thread
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if state.get("last_morning_run") == today:
        return False
    hh, mm = _parse_hhmm(morning_time)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < target:
        return False  # 아직 시각 안 됨

    # 이전 morning/수동 파이프라인이 아직 돌고 있으면 중복 실행 방지
    if (_morning_thread is not None and _morning_thread.is_alive()) or \
       (_manual_thread is not None and _manual_thread.is_alive()):
        return False

    log(f"🌅 morning 수집 트리거 (시각 {morning_time} 경과) → 파이썬 직접 실행(스레드)")
    try:
        _morning_thread = threading.Thread(
            target=run_morning_pipeline, name="morning-pipeline", daemon=True)
        _morning_thread.start()
        state["last_morning_run"] = today
        state["last_morning_started_at"] = now.isoformat()
        save_state(state)
        return True
    except Exception as e:
        log(f"🚨 morning 수집 실행 실패: {type(e).__name__}: {e}")
        return False


def maybe_run_manual(state: dict) -> bool:
    """
    Cowork(또는 사용자)가 BASE_DIR 에 RUN_NOW.flag 를 만들면, 시각과 무관하게
    run_morning_pipeline() 을 즉시 1회 실행한다(06:30 정규 수집과 완전히 동일한 파이프라인).
    이후 핸드셰이크(COLLECT_DONE -> commands.txt -> deep -> report-done -> mail)는
    기존 워처들(force_analysis/deep/메일)이 그대로 이어받으므로, 분석부터 메일 발송까지
    자동으로 진행된다.

    플래그는 '존재' 자체가 신호이며(내용은 보지 않음), 감지 즉시 삭제해 다음 사이클
    재실행을 막는다(요청 점유). last_morning_run 은 건드리지 않으므로 그날 06:30 정규
    수집과 독립적이다(필요하면 하루에 여러 번 새로고침 가능). 실행했으면 True.
    """
    global _manual_thread
    if not os.path.exists(RUN_NOW_FLAG):
        return False

    # 오래된(>1시간) 잔여 플래그는 무시·정리 — 예: 호스트가 며칠 꺼져 있던 사이 작성된 것.
    try:
        age = time.time() - os.path.getmtime(RUN_NOW_FLAG)
    except OSError:
        return False
    if age > RUN_NOW_MAX_AGE_SEC:
        try:
            os.remove(RUN_NOW_FLAG)
            log(f"[수동] 오래된 RUN_NOW.flag({age/60:.0f}분 전 작성) 무시·정리")
        except Exception:
            pass
        return False

    # 1) 먼저 플래그를 삭제해 '요청을 점유'한다(다음 사이클에서 또 잡지 않도록).
    try:
        os.remove(RUN_NOW_FLAG)
    except FileNotFoundError:
        return False
    except Exception as e:
        log(f"[수동] RUN_NOW.flag 삭제 실패 — 이번 사이클 건너뜀: {type(e).__name__}: {e}")
        return False

    # 2) 이미 수집 파이프라인(아침 정규 또는 직전 수동)이 돌고 있으면 중복 실행 금지.
    if (_morning_thread is not None and _morning_thread.is_alive()) or \
       (_manual_thread is not None and _manual_thread.is_alive()):
        log("[수동] RUN_NOW.flag 감지 — 그러나 이미 수집 파이프라인 진행 중이라 요청을 무시한다")
        return False

    # 3) 시각과 무관하게 즉시 수집 파이프라인 실행(별도 스레드, 논블로킹).
    log("[수동] RUN_NOW.flag 감지 -> 즉시 수집 파이프라인 실행(스레드). "
        "이후 COLLECT_DONE -> commands.txt -> deep -> report-done -> 메일까지 자동 진행.")
    try:
        _manual_thread = threading.Thread(
            target=run_morning_pipeline, name="manual-pipeline", daemon=True)
        _manual_thread.start()
        state["last_manual_run_at"] = datetime.now().isoformat()
        save_state(state)
        return True
    except Exception as e:
        log(f"[수동] 즉시 수집 실행 실패: {type(e).__name__}: {e}")
        return False


# =====================================================================
# 새벽 1차 수집(precollect) 트리거 — 기존 뉴스 수집을 미리 1회 돌려 저장
# =====================================================================
_precollect_thread = None


def run_precollect_pipeline():
    """새벽 1차 수집: 기존 뉴스 수집(research_agent broad)을 1회 돌려
    precollect/<날짜>/ 에만 저장한다(세션/COLLECT_DONE 미생성 → 아침 분석 조기 트리거 방지).
    별도 스레드에서 호출됨."""
    py = sys.executable
    _mlog("-" * 50)
    _mlog("precollect(새벽 1차 수집) 시작 — 기존 뉴스 수집 1회")
    _run_step("precollect", [py, os.path.join(BASE_DIR, "precollect.py")],
              timeout=1800, extra_env={"DISABLE_GEMINI": "1", "SELENIUM_USE": "1"})
    # 새벽(precollect 시각, 기본 02:00) 금융위(FSC) 공식 시세 수집 → fsc_prices.json.
    #   전 거래일까지의 종가/등락률/거래량을 받아, 03:30 회고(retro_label)의 '실제 등락률'
    #   채점과 당일 분석의 가격 근거로 쓴다. fsc_api.txt 키 없으면 FDR 폴백.
    _mlog("precollect FSC 시세 수집 시작 — fsc_prices.json")
    _run_step("precollect_fsc", [py, os.path.join(BASE_DIR, "fsc_collect.py")], timeout=600)
    # 02:00 종가수집 결과(+그 외 존재하는 수집)를 회고 폴더 collections/<날짜>/ 로 복사(사용자 요청).
    _run_step("precollect_collections",
              [py, os.path.join(BASE_DIR, "retro_forward.py"), "--push-collections"], timeout=180)
    _record_daily_status("precollect_done")
    _mlog("precollect 종료 (아침 본수집 때 00_precollect.md 로 합쳐짐)")
    _mlog("-" * 50)


def maybe_run_precollect(precollect_time: str, state: dict) -> bool:
    """오늘 아직 precollect 안 했고 현재 시각이 precollect_time 을 지났으면,
    run_precollect_pipeline() 을 '별도 스레드'로 실행(논블로킹). 실행했으면 True."""
    global _precollect_thread
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if state.get("last_precollect_run") == today:
        return False
    hh, mm = _parse_hhmm(precollect_time)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < target:
        return False  # 아직 시각 안 됨

    # 이전 precollect 스레드가 아직 돌고 있으면 중복 실행 방지
    if _precollect_thread is not None and _precollect_thread.is_alive():
        return False
    # morning/수동 파이프라인이 돌고 있으면(이미 본수집 중) 1차 수집은 굳이 돌리지 않음
    if (_morning_thread is not None and _morning_thread.is_alive()) or \
       (_manual_thread is not None and _manual_thread.is_alive()):
        state["last_precollect_run"] = today
        save_state(state)
        return False

    log(f"[precollect] 새벽 1차 수집 트리거 (시각 {precollect_time} 경과) → 파이썬 직접 실행(스레드)")
    try:
        _precollect_thread = threading.Thread(
            target=run_precollect_pipeline, name="precollect-pipeline", daemon=True)
        _precollect_thread.start()
        state["last_precollect_run"] = today
        state["last_precollect_started_at"] = now.isoformat()
        save_state(state)
        return True
    except Exception as e:
        log(f"[precollect] 새벽 1차 수집 실행 실패: {type(e).__name__}: {e}")
        return False


# =====================================================================
# 회고분석(PART C) 트리거 — 과거 추천 → 실제결과 학습 데이터셋 생성 후 회고 Cowork 로 전달
# =====================================================================
_retro_thread = None


def run_retro_pipeline():
    """회고분석: retro_label.py 로 '과거 추천 → 실제결과' 학습 데이터셋을 만들고,
    retro_forward.py --push 로 회고 Cowork 폴더(inbox)에 전달 + RETRO_GO.flag. 별도 스레드 호출."""
    py = sys.executable
    _mlog("-" * 50)
    _mlog("retro(회고분석 데이터셋) 시작 — 과거 추천 라벨링")
    # timeout 상향(1500s): pre_* asof 수급/공매도 + DART 분배공시 매칭으로 느려져, kill 로 저장이 잘리지 않게.
    # ★ rc 확인(#A5): 예전엔 반환값을 버려서, retro_label 이 죽어도 push 가 그대로 돌아
    #   '낡은 데이터셋 + RETRO_GO'가 회고로 넘어갔다(사용자는 정상 회고로 오인). 실패면 push 를 건너뛴다.
    rc = _run_step("retro_label", [py, os.path.join(BASE_DIR, "retro_label.py")], timeout=1500)
    if rc != 0:
        _mlog(f"[retro] retro_label 실패(rc={rc}) — push 생략(낡은 데이터셋 전달 방지). "
              f"logs/morning_auto.log 확인 필요")
        _record_retro_status(ok=False, note=f"retro_label rc={rc}")
        _notify_health("회고 데이터셋 생성 실패", f"retro_label rc={rc} — push 생략(낡은 데이터 전달 방지). logs/morning_auto.log 확인")
        _mlog("-" * 50)
        return
    rc2 = _run_step("retro_push", [py, os.path.join(BASE_DIR, "retro_forward.py"), "--push"],
                    timeout=180)
    if rc2 != 0:
        _mlog(f"[retro] retro_push 실패(rc={rc2}) — 회고 inbox 미갱신(회고는 이전 회차 대기 상태)")
        _record_retro_status(ok=False, note=f"retro_push rc={rc2}")
        _notify_health("회고 push 실패", f"retro_forward --push rc={rc2} — inbox 미갱신(회고는 이전 회차 대기)")
        _mlog("-" * 50)
        return
    _record_retro_status(ok=True, note="dataset+push 완료")
    _mlog("retro 종료 (회고 Cowork inbox 로 전달, RETRO_GO.flag 생성)")
    _mlog("-" * 50)


def _record_retro_status(ok: bool, note: str = ""):
    """회고 실행 결과를 daily_status.json 에 남긴다(#A5 '무음 실패' 대책).
    예전엔 회고가 성공/실패 어느 쪽도 상태에 안 남아, 4일간 멈춰도 아무도 몰랐다.
    실패는 로그 + 이 상태로 드러나고, morning-research 스킬의 회고 신선도 가드가 사람에게 알린다."""
    with _status_lock:                     # #A9
        return _record_retro_status_locked(ok, note)


def _record_retro_status_locked(ok, note=""):
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(BASE_DIR, "daily_status.json")
        data = {}
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f) or {}
            except Exception:
                data = {}
        entry = data.get(today) or {}
        entry["retro"] = {"ok": bool(ok), "note": note,
                          "at": datetime.now().isoformat(timespec="seconds")}
        data[today] = entry
        from common import save_json_atomic
        save_json_atomic(path, data)
    except Exception as e:
        _mlog(f"[retro] 상태 기록 실패(무시): {type(e).__name__}: {e}")


def maybe_run_retro(retro_time: str, state: dict) -> bool:
    """오늘 아직 회고분석 안 했고 현재 시각이 retro_time 을 지났으면 run_retro_pipeline() 을
    별도 스레드로 실행(논블로킹). 실행했으면 True. 회고는 수집 파이프라인과 독립(가벼움)이라
    morning/manual 진행 여부와 무관하게 돌리되, 자기 자신 중복만 막는다."""
    global _retro_thread
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if state.get("last_retro_run") == today:
        return False
    hh, mm = _parse_hhmm(retro_time)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < target:
        return False
    if _retro_thread is not None and _retro_thread.is_alive():
        return False

    log(f"[retro] 회고분석 트리거 (시각 {retro_time} 경과) → retro_label + push(스레드)")
    try:
        _retro_thread = threading.Thread(
            target=run_retro_pipeline, name="retro-pipeline", daemon=True)
        _retro_thread.start()
        state["last_retro_run"] = today
        state["last_retro_started_at"] = now.isoformat()
        save_state(state)
        return True
    except Exception as e:
        log(f"[retro] 회고분석 실행 실패: {type(e).__name__}: {e}")
        return False


# =====================================================================
# 수동 회고 트리거 — '수동 회고 Cowork'(stock_retro_manual)가 RETRO_RUN_NOW.flag 를 만들면
#   호스트가 retro_label 재실행(최신 FSC 정산가)→그 폴더로 복사→RETRO_DATA_READY.flag 로 응답.
#   (사용자가 회고 Cowork 에서 '시작'을 누르면, 코워크가 flag 로 호스트(파이썬)에 데이터 준비를 명령.)
#   기존 RUN_NOW.flag(아침 수동 수집)와 동일한 패턴. 03:30 자동 회고와는 별개 채널.
# =====================================================================
RETRO_MANUAL_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "stock_retro_manual")
RETRO_MANUAL_FLAG = os.path.join(RETRO_MANUAL_DIR, "RETRO_RUN_NOW.flag")
RETRO_MANUAL_READY = os.path.join(RETRO_MANUAL_DIR, "RETRO_DATA_READY.flag")
RETRO_MANUAL_MAX_AGE_SEC = 1800   # 30분 넘은 잔여 flag 는 무시(오발 방지)
_retro_manual_thread = None


def run_retro_manual_pipeline():
    """수동 회고 데이터 준비: retro_manual_refresh.py(retro_label 재실행 + stock_retro_manual 복사)
    실행 후 RETRO_DATA_READY.flag 를 그 폴더에 남겨 코워크에 '최신 데이터 준비됨'을 알린다. 별도 스레드."""
    py = sys.executable
    _mlog("-" * 50)
    _mlog("retro_manual(수동 회고 데이터 준비) 시작 — 코워크 RETRO_RUN_NOW.flag 감지")
    _run_step("retro_manual", [py, os.path.join(BASE_DIR, "retro_manual_refresh.py")], timeout=1200)
    try:
        os.makedirs(RETRO_MANUAL_DIR, exist_ok=True)
        with open(RETRO_MANUAL_READY, "w", encoding="utf-8") as f:
            f.write(datetime.now().isoformat() + "\n준비 완료 — retro_dataset.json 최신화됨\n")
        _mlog("retro_manual 완료 → RETRO_DATA_READY.flag (코워크가 이제 분석 시작)")
    except Exception as e:
        _mlog(f"retro_manual READY 플래그 작성 실패: {type(e).__name__}: {e}")
    _mlog("-" * 50)


def maybe_run_retro_manual(state: dict) -> bool:
    """stock_retro_manual\\RETRO_RUN_NOW.flag 감지 시 데이터 준비를 스레드로 실행. 실행했으면 True.
    회고 Cowork(사용자 수동 시작)가 만든 flag → claim-by-delete 후 retro_label 재실행."""
    global _retro_manual_thread
    if not os.path.exists(RETRO_MANUAL_FLAG):
        return False
    try:
        age = time.time() - os.path.getmtime(RETRO_MANUAL_FLAG)
    except Exception:
        age = 0
    if age > RETRO_MANUAL_MAX_AGE_SEC:
        try:
            os.remove(RETRO_MANUAL_FLAG)
        except Exception:
            pass
        log(f"[retro수동] 오래된 RETRO_RUN_NOW.flag({age/60:.0f}분 전) 무시·정리")
        return False
    if _retro_manual_thread is not None and _retro_manual_thread.is_alive():
        return False   # 이미 준비 중
    # claim-by-delete: 같은 요청 중복 처리 방지
    try:
        os.remove(RETRO_MANUAL_FLAG)
    except Exception as e:
        log(f"[retro수동] flag 삭제 실패 — 이번 사이클 건너뜀: {type(e).__name__}: {e}")
        return False
    # 이전 READY 플래그 정리(코워크가 '이번' 준비 완료를 명확히 구분하도록)
    try:
        if os.path.exists(RETRO_MANUAL_READY):
            os.remove(RETRO_MANUAL_READY)
    except Exception:
        pass
    log("[retro수동] RETRO_RUN_NOW.flag 감지 -> retro_label 재실행(스레드)로 최신 데이터 준비")
    try:
        _retro_manual_thread = threading.Thread(
            target=run_retro_manual_pipeline, name="retro-manual", daemon=True)
        _retro_manual_thread.start()
        state["last_retro_manual_at"] = datetime.now().isoformat()
        save_state(state)
        return True
    except Exception as e:
        log(f"[retro수동] 실행 실패: {type(e).__name__}: {e}")
        return False


# =====================================================================
# deep 워처: Cowork 가 commands.txt 를 쓰면 호스트가 deep 를 자동 실행
# =====================================================================
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
DEEP_TIMEOUT_SEC = 1200          # deep 1회 최대 대기(초)
COMMANDS_MIN_AGE_SEC = 5         # commands.txt 작성 직후 너무 빨리 읽지 않게(쓰는 중 방지)
_deep_running = set()            # 현재 deep 실행 중인 세션명(중복 실행 방지)


def find_deep_pending():
    """commands.txt 가 있고 DEEP_DONE/DEEP_FAILED 가 아직 없는 오늘자 세션 목록."""
    if not os.path.isdir(OUTPUT_DIR):
        return []
    today = datetime.now().strftime("%Y-%m-%d")
    out = []
    try:
        for name in os.listdir(OUTPUT_DIR):
            if name.startswith("_") or not name.startswith(today):
                continue
            d = os.path.join(OUTPUT_DIR, name)
            if not os.path.isdir(d):
                continue
            cmds = os.path.join(d, "commands.txt")
            if not os.path.isfile(cmds):
                continue
            if os.path.exists(os.path.join(d, "DEEP_DONE.flag")) or \
               os.path.exists(os.path.join(d, "DEEP_FAILED.flag")):
                continue
            # commands.txt 가 막 쓰이는 중일 수 있으니 약간의 안정화 대기
            if (time.time() - os.path.getmtime(cmds)) < COMMANDS_MIN_AGE_SEC:
                continue
            out.append(d)
    except Exception as e:
        log(f"🚨 deep 스캔 예외: {type(e).__name__}: {e}")
    return out


def run_deep(session_dir: str) -> bool:
    """research_agent.py deep --session 실행. DEEP_DONE/DEEP_FAILED 는 deep 가 남긴다."""
    name = os.path.basename(session_dir)
    if name in _deep_running:
        return False
    _deep_running.add(name)
    log(f"🔬 deep 자동 실행: {name} (commands.txt 감지)")
    try:
        kwargs = {"cwd": BASE_DIR}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run(
            [sys.executable, os.path.join(BASE_DIR, "research_agent.py"),
             "deep", "--session", session_dir],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=DEEP_TIMEOUT_SEC, **kwargs)
        if r.returncode == 0:
            log(f"🔬 deep 완료: {name}")
            return True
        log(f"🚨 deep 비정상 종료 rc={r.returncode} ({name}) "
            f"stderr={(r.stderr or '')[:200]}")
        return False
    except subprocess.TimeoutExpired:
        log(f"🚨 deep 타임아웃({DEEP_TIMEOUT_SEC}s): {name}")
        return False
    except Exception as e:
        log(f"🚨 deep 실행 예외({name}): {type(e).__name__}: {e}")
        return False
    finally:
        _deep_running.discard(name)


# =====================================================================
# 메인 루프
# =====================================================================
def one_cycle(cfg: dict, state: dict):
    """한 사이클: 수집 트리거 → force_analysis → deep(핸드셰이크) → 메일발송."""
    # 1) 매일 수집 트리거
    if cfg["morning_enabled"]:
        # 1-a) 아침 본수집 파이프라인(1차 수집분을 00_precollect.md 로 합쳐 분석)
        #   morning 을 먼저 확인한다: 2시를 놓치고 6:30 이후 부팅한 경우, morning 이
        #   먼저 떠서 _morning_thread 가 살아있게 되고, 그러면 아래 precollect 는
        #   '이미 본수집 중'으로 보고 스킵된다(불필요한 이중 수집 방지).
        try:
            maybe_run_morning(cfg["morning_time"], state)
        except Exception as e:
            log(f"🚨 morning 트리거 예외(계속): {type(e).__name__}: {e}")
        # 1-b) 새벽 1차 수집(precollect): 기존 뉴스 수집을 미리 1회 → precollect/<날짜>/
        #   보통 새벽 2시에만 발동(아침 본수집보다 훨씬 이른 시각). morning 이 이미
        #   돌고 있으면 maybe_run_precollect 내부에서 today 처리 후 스킵한다.
        try:
            maybe_run_precollect(cfg.get("precollect_time", PRECOLLECT_TIME), state)
        except Exception as e:
            log(f"🚨 precollect 트리거 예외(계속): {type(e).__name__}: {e}")
        # 1-c) 회고분석(PART C): 과거 추천→실제결과 학습 데이터셋 생성 후 회고 Cowork 로 전달.
        #   보통 새벽 03:30(아침 본수집보다 일찍) 1회. 수집 파이프라인과 독립적으로 동작.
        try:
            maybe_run_retro(cfg.get("retro_time", RETRO_TIME), state)
        except Exception as e:
            log(f"🚨 retro 트리거 예외(계속): {type(e).__name__}: {e}")

    # 1.5) 수동 즉시 실행: BASE_DIR/RUN_NOW.flag 감지 → 시각과 무관하게 수집 1회.
    #   Cowork 를 정규 시각(06:30) 밖에 수동으로 켰을 때 호스트에 수집을 요청하는 통로.
    #   '명시적 요청'이므로 morning_enabled(--no-morning) 와 무관하게 항상 처리한다.
    try:
        maybe_run_manual(state)
    except Exception as e:
        log(f"🚨 수동 트리거 예외(계속): {type(e).__name__}: {e}")

    # 1.6) 수동 회고: 회고 Cowork(stock_retro_manual)가 RETRO_RUN_NOW.flag 를 만들면(사용자가
    #   '시작'을 누름) → 호스트가 retro_label 재실행해 최신 데이터 준비 + RETRO_DATA_READY.flag.
    #   '명시적 수동 시작'이라 morning_enabled 와 무관하게 항상 처리. 03:30 자동 회고와 별개.
    try:
        maybe_run_retro_manual(state)
    except Exception as e:
        log(f"🚨 수동 회고 트리거 예외(계속): {type(e).__name__}: {e}")

    # 2) COLLECT_DONE.flag → force_analysis (force_scores.json 생성)
    try:
        n = wanalyze.cycle(cfg["tickers"])
        if n:
            log(f"📊 force_analysis 처리: {n}건")
    except Exception as e:
        log(f"🚨 force_analysis 예외(계속): {type(e).__name__}: {e}")

    # 2.5) commands.txt(Cowork 발행) → deep 자동 실행 (핸드셰이크 연결고리)
    try:
        for sess in find_deep_pending():
            run_deep(sess)
    except Exception as e:
        log(f"🚨 deep 워처 예외(계속): {type(e).__name__}: {e}")

    # 2.7) REPORT_DONE.flag → 모의투자 Cowork 폴더로 리포트 전달 (mock_forward_config.txt 의
    #   enabled=1 일 때만). 메일/아카이브와 무관하게 동작하되, watch_and_send 가 발송 성공 후
    #   세션을 _archive 로 옮기기 '전'에 복사해야 하므로 아래 메일 step(3) '이전'에 호출한다.
    #   설정(on/off·폴더)은 mock_forward 가 매 호출마다 직접 읽어 ~즉시 반영된다.
    if mforward is not None:
        try:
            n = mforward.scan_once()
            if n:
                log(f"📤 모의투자 전달 처리: {n}건")
        except Exception as e:
            log(f"🚨 모의투자 전달 예외(계속): {type(e).__name__}: {e}")

    # 2.8) 회고분석 피드백 회수: 회고 Cowork 가 outbox/RETRO_DONE.flag 를 만들면, 그가 작성한
    #   'PART A 추가지시'(PART_A_추가지시.md)를 stock_research/retro_feedback.md 로 회수한다.
    #   다음 아침 분석 Cowork 가 [0.5] 에서 이 파일을 읽어 차익실현 회피 등 개선을 반영한다.
    if rforward is not None:
        try:
            n = rforward.scan_back()
            if n:
                log(f"🔁 회고 피드백 회수: {n}건 (retro_feedback.md 갱신)")
        except Exception as e:
            log(f"🚨 회고 피드백 회수 예외(계속): {type(e).__name__}: {e}")

    # 3) REPORT_DONE.flag → 메일 발송 (Apps Script)
    if cfg["mail_ready"]:
        try:
            n = wsend.scan_once(cfg["url"], cfg["secret"], cfg["recipients"])
            if n:
                log(f"📧 메일 발송 처리: {n}건")
                # 3.5) 메일 발송 후: 추천 종목을 이력(recommended_history.json)·수집유니버스
                #   (recommended_universe.txt)로 저장하고 회고 폴더로 전달. fsc/flow 가 다음 수집부터
                #   watch 풀 밖 추천 종목까지 합산하고, 회고 Cowork 가 '추천 이력'을 폴더에서 바로 본다.
                if rtrack is not None:
                    try:
                        rtrack.track()
                    except Exception as e:
                        log(f"🚨 추천 추적 예외(계속): {type(e).__name__}: {e}")
        except Exception as e:
            log(f"🚨 메일 발송 예외(계속): {type(e).__name__}: {e}")


def reload_config() -> dict:
    """워처 설정 재로딩 (파일 수정 시 자동 반영)."""
    url, secret = wsend.load_appscript_config()
    recipients = wsend.load_recipients()
    tickers = wanalyze.load_tickers()
    mail_ready = bool(url and secret and recipients)
    return {
        "url": url, "secret": secret, "recipients": recipients,
        "tickers": tickers, "mail_ready": mail_ready,
    }


def main():
    ap = argparse.ArgumentParser(
        description="통합 상시 데몬: 수집 트리거 + force_analysis + 메일 발송")
    ap.add_argument("--interval", type=int, default=30, help="스캔 간격(초), 기본 30")
    ap.add_argument("--morning-time", default=None,
                    help=f"매일 수집 시각 HH:MM. 생략 시 코드 상단 MORNING_TIME({MORNING_TIME}) 사용")
    ap.add_argument("--no-morning", action="store_true", help="자동 수집 비활성(감시만)")
    ap.add_argument("--once", action="store_true", help="1회만 처리 후 종료(테스트)")
    args = ap.parse_args()

    interval = max(5, args.interval)
    morning_enabled = not args.no_morning
    # 인자(--morning-time)가 있으면 그것을, 없으면 코드 상단 MORNING_TIME 변수를 쓴다.
    # 인자 없이 돌리면, MORNING_TIME 변수만 바꿔 저장해도 25초 내 자동 반영된다.
    arg_morning_time = args.morning_time

    log("=" * 60)
    log("supervisor 시작 (통합 상시 데몬)")
    log(f"  스캔 간격     : {interval}s")
    _eff_morning = arg_morning_time or MORNING_TIME
    log(f"  매일 수집     : {'ON @ ' + _eff_morning if morning_enabled else 'OFF'}"
        f"{'  (코드 MORNING_TIME 변수)' if not arg_morning_time else '  (--morning-time 인자)'}")
    log(f"  once          : {args.once}")
    log("=" * 60)

    state = load_state()
    base_cfg = reload_config()
    base_cfg["morning_enabled"] = morning_enabled
    base_cfg["morning_time"] = _eff_morning
    _eff_precollect = _reload_precollect_time()
    base_cfg["precollect_time"] = _eff_precollect
    log(f"  새벽 1차 수집 : {'ON @ ' + _eff_precollect if morning_enabled else 'OFF'}"
        f"  (코드 PRECOLLECT_TIME 변수, 25초 내 자동반영)")
    _eff_retro = _reload_retro_time()
    base_cfg["retro_time"] = _eff_retro
    log(f"  회고분석      : {'ON @ ' + _eff_retro if morning_enabled else 'OFF'}"
        f"  (코드 RETRO_TIME 변수, 25초 내 자동반영)")

    # 설정 상태 안내
    log(f"  종목 풀       : {len(base_cfg['tickers'])}개")
    if base_cfg["mail_ready"]:
        log(f"  메일 발송     : 준비됨 (수신: {base_cfg['recipients']})")
    else:
        log("  메일 발송     : 🛑 미설정 (appscript_config.txt 의 url/secret + "
            "mail_config.txt 의 to 필요) — 채워지면 자동 반영")

    if args.once:
        one_cycle(base_cfg, state)
        log("=== --once 처리 종료 ===")
        return

    if not acquire_lock():
        return

    last_cfg_reload = 0.0
    try:
        while True:
            now = time.time()
            # 25초마다 설정 재로딩(파일 수정 즉시 반영)
            if now - last_cfg_reload > 25:
                cfg = reload_config()
                cfg["morning_enabled"] = morning_enabled
                # 인자 없으면 코드의 MORNING_TIME 을 매번 파일에서 다시 읽어 반영
                # (테스트 중 MORNING_TIME 변수만 바꿔 저장해도 25초 내 적용됨)
                eff = arg_morning_time or _reload_morning_time()
                if eff != base_cfg.get("morning_time"):
                    log(f"🕢 수집 시각 변경 감지 → {eff}")
                cfg["morning_time"] = eff
                # 새벽 1차 수집 시각도 매번 파일에서 다시 읽어 반영
                pre_eff = _reload_precollect_time()
                if pre_eff != base_cfg.get("precollect_time"):
                    log(f"🕑 1차 수집 시각 변경 감지 → {pre_eff}")
                cfg["precollect_time"] = pre_eff
                # 회고분석 시각도 매번 파일에서 다시 읽어 반영
                retro_eff = _reload_retro_time()
                if retro_eff != base_cfg.get("retro_time"):
                    log(f"🕞 회고분석 시각 변경 감지 → {retro_eff}")
                cfg["retro_time"] = retro_eff
                # mail_ready 상태 변화 로깅
                if cfg["mail_ready"] != base_cfg.get("mail_ready"):
                    if cfg["mail_ready"]:
                        log(f"✅ 메일 설정 감지됨 — 발송 활성화 (수신: {cfg['recipients']})")
                    else:
                        log("🛑 메일 설정이 비었습니다 — 발송 비활성")
                base_cfg = cfg
                last_cfg_reload = now

            write_heartbeat()   # 긴 작업(force_analysis 등) 진입 전 갱신 — 오탐 방지
            one_cycle(base_cfg, state)
            write_heartbeat()   # 사이클 정상 완료 표시
            time.sleep(interval)
    except KeyboardInterrupt:
        log("=== 사용자 중단(Ctrl+C) — supervisor 종료 ===")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
