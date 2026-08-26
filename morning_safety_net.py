#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""morning_safety_net.py — "오늘 아침 메일이 안 나갔으면 나가게 한다".

[왜 이 파일이 생겼나]
  아침 발송이 조용히 실패한 날이 반복됐다(2026-08-13~18 3거래일, 08-20·21 지각,
  08-24·25 미발행). 원인은 그때그때 달랐지만 **공통점은 하나** — 실패해도
  아무도 즉시 모른다는 것이다. 사람이 알아챈 건 며칠 뒤 회고에서였다.

  이 스크립트는 늦은 아침에 한 번 깨어나 **"오늘 메일이 나갔는가"만 보고**,
  안 나갔으면 가장 싼 방법부터 차례로 시도한다.

[단계 — 싼 것부터. 함부로 전체를 다시 돌리지 않는다]
  0. 거래일이 아니면 아무것도 안 한다.
  1. 오늘 이미 발송됐으면 아무것도 안 한다(sent_index.json).
  2. 오늘 세션에 03_final_report.md 가 **있으면** → 메일만 보낸다.
     (분석은 됐는데 발송에서 끊긴 경우. 가장 흔하고 가장 싸다)
  3. 리포트가 없으면 → CLI 대체 러너(run_morning_research.cmd)를 돌린다.
     ★--no-full 을 주면 여기서 멈추고 사유만 남긴다.

[안전장치]
  · 예측 계약 검증(blocked_schema)·중복 발송 방지는 research_agent 가 그대로 한다.
    여기서 --skip-pred-check 나 --force-resend 를 쓰지 않는다.
  · 이미 분석이 끝난 세션에 신호를 재수집하지 않는다(회고 스냅샷 보호).
  · 무엇을 했든 morning_safety_net.json 에 남긴다 — 조용히 지나가지 않는다.

[사용법]
  python morning_safety_net.py              # 판단 후 필요한 만큼만 실행
  python morning_safety_net.py --dry-run    # 무엇을 할지만 보고 아무것도 안 한다
  python morning_safety_net.py --no-full    # 발송까지만. 전체 재실행은 안 한다
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import subprocess
import sys
from datetime import datetime

from common import save_json_atomic, trading_day_status

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
PY = sys.executable
STATE = os.path.join(HERE, "morning_safety_net.json")


def sent_today(today):
    """오늘 발송 기록이 있나. (있음, 세션키)"""
    try:
        with io.open(os.path.join(HERE, "sent_index.json"), encoding="utf-8") as f:
            idx = json.load(f)
    except Exception:                                  # noqa: BLE001
        return False, ""
    if not isinstance(idx, dict):
        return False, ""
    for key, val in idx.items():
        at = (val or {}).get("at") if isinstance(val, dict) else None
        if isinstance(at, str) and at[:10] == today:
            return True, key
    return False, ""


def today_session(today):
    """오늘 날짜로 시작하는 세션 중 가장 최근 것."""
    cands = [d for d in glob.glob(os.path.join(OUTPUT_DIR, "*"))
             if os.path.isdir(d) and os.path.basename(d).startswith(today)]
    return max(cands, key=os.path.getmtime) if cands else None


def run(argv, timeout, label):
    print("[safety] 실행: %s" % label)
    try:
        r = subprocess.run(argv, cwd=HERE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        tail = [ln for ln in out.strip().splitlines() if ln.strip()][-6:]
        for ln in tail:
            print("   | %s" % ln[:150])
        return r.returncode, "\n".join(tail)[-1200:]
    except subprocess.TimeoutExpired:
        print("   | 시간초과")
        return -1, "timeout"
    except Exception as e:                             # noqa: BLE001
        print("   | %s" % type(e).__name__)
        return -1, type(e).__name__


def finish(action, detail, ok, skill_check=""):
    doc = {"run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "tz": "KST",
           "action": action, "ok": bool(ok), "detail": detail,
           "skill_check": skill_check,
           "_note": ("아침 발송 안전망의 마지막 실행 기록. action=none 이면 손댈 것이 "
                     "없었다는 뜻이다(이미 발송됐거나 비거래일).")}
    save_json_atomic(STATE, doc)
    print("[safety] %s — %s" % (action, "정상" if ok else "확인 필요"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="아침 메일 미발송 안전망")
    ap.add_argument("--dry-run", action="store_true", help="판단만 하고 실행하지 않는다")
    ap.add_argument("--no-full", action="store_true", help="전체 재실행까지는 하지 않는다")
    a = ap.parse_args()

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    print("[safety] %s %s" % (today, now.strftime("%H:%M")))

    # ★예정작업 이름과 내용물이 어긋나면 '이름대로 동작하지 않는다'.
    #   2026-08-13~26 아침 미발송의 진짜 원인이 그것이었다 — 그런데
    #   그 사실을 기계가 볼 방법이 없어 같은 지적이 회차마다 반복 발행됐다.
    #   메일이 나갔든 아니든 매일 한 번 대조해 둔다(실패해도 본 작업을 막지 않는다).
    try:
        rc_chk, chk = run([PY, "-X", "utf8", os.path.join(HERE, "cowork_skill_check.py")],
                          120, "cowork_skill_check")
        if rc_chk == 1:
            print("[safety] ★예정작업 이름·내용물 어긋남 — 위 목록을 보라")
    except Exception:                                  # noqa: BLE001
        chk = ""

    # 0) 거래일인가
    st = trading_day_status(now.date())
    if not st["is_trading_day"]:
        print("[safety] %s" % st["reason"])
        return finish("none", st["reason"], True, chk)
    if not st.get("holiday_checked"):
        print("[safety] ※ %s" % st["reason"])

    # 1) 이미 나갔나
    done, key = sent_today(today)
    if done:
        print("[safety] 오늘 이미 발송됨(세션 %s) — 할 일 없음" % key)
        return finish("none", "이미 발송: %s" % key, True, chk)

    # 2) 리포트는 있는데 발송만 안 됐나 — 가장 싼 복구
    sess = today_session(today)
    if sess and os.path.exists(os.path.join(sess, "03_final_report.md")):
        rel = os.path.relpath(sess, HERE)
        print("[safety] 리포트는 있는데 발송 기록이 없다 — 발송만 시도한다: %s" % rel)
        if a.dry_run:
            return finish("would_mail", rel, True)
        rc, tail = run([PY, "-X", "utf8", os.path.join(HERE, "research_agent.py"),
                        "mail", "--session", rel, "--method", "appscript"], 900,
                       "research_agent mail")
        ok = rc == 0 and "MAIL_RESULT=success" in tail
        return finish("mail_only", tail, ok)

    # 3) 리포트 자체가 없다 — 아침이 통째로 안 돌았다
    why = "오늘 세션 없음" if not sess else "세션은 있으나 03_final_report.md 없음"
    print("[safety] ★%s — 아침 파이프라인이 돌지 않았다" % why)
    if a.no_full or a.dry_run:
        return finish("would_run_full" if a.dry_run else "skipped_full", why, False)

    cmd = os.path.join(HERE, "run_morning_research.cmd")
    if not os.path.isfile(cmd):
        return finish("no_runner", "run_morning_research.cmd 없음", False)
    rc, tail = run(["cmd", "/c", cmd], 5400, "run_morning_research.cmd")

    done, key = sent_today(today)                      # 실제로 나갔는지로 판정한다
    if done:
        return finish("full_run", "발송 확인: %s" % key, True)
    return finish("full_run_failed", "돌렸으나 발송 기록이 없다\n" + tail, False)


if __name__ == "__main__":
    raise SystemExit(main())
