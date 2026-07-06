#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watchdog.py — supervisor 자가복구 감시자 (작업 스케줄러가 5분마다 실행)

[배경]
  supervisor 프로세스가 '살아있지만 루프가 얼어붙는'(alive-but-frozen) 가능성이
  있다(파이프 버퍼 고갈/경쟁 등). 프로세스가 안 죽으니 단일 인스턴스 락만으로는
  복구가 안 된다. 이 watchdog 이 하트비트로 프리즈를 판정해 자동으로 supervisor 를
  재가동한다.

[동작] 5분마다(작업 스케줄러) 1회 실행:
  - 락 PID 살아있고 + 하트비트 신선(<STALE)  → 정상 → 아무것도 안 함(조용히 종료)
  - 락 없음 / PID 죽음 / 하트비트 오래됨(프리즈) → supervisor 재가동
    (재가동된 supervisor 의 acquire_lock 이 프리즈된 옛 인스턴스를 강제종료 후 인수)
  → 정상일 땐 비용 거의 0, 프리즈/중단 시 최대 5분 내 자동 복구.

[원칙]
  - supervisor.py 의 상수/헬퍼(LOCK_FILE, HEARTBEAT_*, heartbeat_age, _pid_alive)를
    재사용. import 해도 supervisor.main() 은 실행되지 않는다(__main__ 가드).
  - 메일/수집 로직은 건드리지 않는다.
"""

import os
import sys
import subprocess
from datetime import datetime

import supervisor as sv

LOG_FILE = os.path.join(sv.LOG_DIR, "watchdog.log")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [watchdog] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _read_lock_pid():
    try:
        with open(sv.LOCK_FILE, encoding="utf-8") as f:
            s = f.read().strip()
        return int(s) if s.isdigit() else None
    except Exception:
        return None


def supervisor_healthy():
    """(healthy: bool, reason: str)."""
    pid = _read_lock_pid()
    if pid is None:
        return False, "락 파일 없음/손상"
    if not sv._pid_alive(pid):
        return False, f"PID {pid} 죽음"
    age = sv.heartbeat_age()
    if age >= sv.HEARTBEAT_STALE_SEC:
        return False, (f"PID {pid} 살아있으나 하트비트 {age:.0f}s "
                       f"(>={sv.HEARTBEAT_STALE_SEC}s) = 프리즈")
    return True, f"정상 (PID {pid}, 하트비트 {age:.0f}s 전)"


def _pythonw_path() -> str:
    """콘솔창 없는 pythonw.exe 경로. 없으면 현재 인터프리터."""
    d = os.path.dirname(sys.executable)
    cand = os.path.join(d, "pythonw.exe")
    return cand if os.path.exists(cand) else sys.executable


def launch_supervisor() -> bool:
    """
    supervisor 를 '보이는 콘솔 창'으로 재가동.
    start_supervisor_visible.bat 을 CREATE_NEW_CONSOLE 로 띄워, 사용자가
    supervisor 활동(06:30 수집 등)을 눈으로 볼 수 있게 한다.
    (창 모드로 운영하기로 했으므로 watchdog 도 보이는 창으로 살린다.)
    bat 이 없으면 안전하게 pythonw 백그라운드로 폴백.
    """
    bat = os.path.join(sv.BASE_DIR, "start_supervisor_visible.bat")
    try:
        if sys.platform.startswith("win") and os.path.isfile(bat):
            # cmd /c start "" cmd /k ... → 새 콘솔 창에서 bat 실행 (보임)
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen(["cmd", "/c", "start", "",
                              "cmd", "/k", bat],
                             cwd=sv.BASE_DIR, creationflags=flags, close_fds=True)
            log(f"supervisor 재가동(보이는 창) 요청 완료: {os.path.basename(bat)}")
            return True
        # 폴백: bat 없거나 비윈도우 → 숨김 백그라운드
        py = _pythonw_path()
        # --morning-time 을 주지 않는다 → supervisor.py 의 MORNING_TIME 변수 사용
        args = [py, os.path.join(sv.BASE_DIR, "supervisor.py"), "--interval", "30"]
        subprocess.Popen(args, cwd=sv.BASE_DIR,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, close_fds=True)
        log(f"supervisor 재가동(폴백 백그라운드) 완료: {py} supervisor.py")
        return True
    except Exception as e:
        log(f"supervisor 재가동 실패: {type(e).__name__}: {e}")
        return False


def main():
    healthy, reason = supervisor_healthy()
    if healthy:
        # 정상일 땐 로그 스팸 방지 위해 조용히 종료
        return
    log(f"비정상 감지 → {reason} → supervisor 재가동 시도")
    launch_supervisor()


if __name__ == "__main__":
    main()
