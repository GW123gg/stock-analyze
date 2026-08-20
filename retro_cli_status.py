#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""전야 CLI 실행 결과를 상태 파일로 남긴다 — 무인 실행의 실패를 사람이 알아채게.

왜 필요한가
  23시 작업은 사람이 없는 시간에 돈다. 실패해도 아무도 모르면 다음날 아침 리포트가
  전야 입력([5.17]) 없이 나가고, 그 사실조차 조용히 지나간다. 2026-08-13~18 에
  아침 리서치가 3거래일 미발행된 것을 24회차 회고(08-19)가 되어서야 발견한 것과 같은
  실패 유형이다(#A45). 그래서 결과를 기계가 읽을 수 있는 파일로 남기고, 아침 작업이
  이걸 먼저 본다.

산출: retro_cli_status.json (루트)
  run_at / rc / ok / model / log / summary(프롬프트가 찍은 RETRO_RESULT 블록)

  ★아침 작업이 이 파일을 읽고 "회고가 정상 완료됐는지"를 보고한다.
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS = os.path.join(HERE, "retro_cli_status.json")

# 프롬프트 마지막 보고 블록에서 뽑을 키(없으면 조용히 생략 — 억지로 채우지 않는다)
KEYS = ("RETRO_RESULT", "ROUND", "MATURED", "INTEGRITY",
        "LAG", "NEW_RULES", "SELFCHECK", "NOTES")


def tail_summary(log_path: str, tail_bytes: int = 20000) -> dict:
    """로그 꼬리에서 보고 블록을 회수한다. 실패해도 빈 dict(로그 파싱이 상태를 막지 않게)."""
    out = {}
    try:
        with io.open(log_path, encoding="utf-8", errors="replace") as f:
            try:
                f.seek(max(0, os.path.getsize(log_path) - tail_bytes))
            except OSError:
                pass
            text = f.read()
    except OSError:
        return out
    for k in KEYS:
        m = re.findall(r"^%s=(.*)$" % k, text, re.M)
        if m:
            out[k] = m[-1].strip()      # 마지막 것(재시도 시 최신)
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--rc", type=int, required=True)
    ap.add_argument("--log", default="")
    ap.add_argument("--model", default="")
    a = ap.parse_args()

    summary = tail_summary(a.log) if a.log else {}
    declared = (summary.get("RETRO_RESULT") or "").lower()

    # ★rc 와 자기신고를 둘 다 본다. rc=0 이어도 프롬프트가 failed 라고 하면 실패다
    #   (모델이 '완료했다'고 말하면서 실제로는 못 한 경우를 걸러낸다).
    ok = (a.rc == 0) and (declared in ("success", "partial", ""))
    if declared == "failed":
        ok = False

    payload = {
        "_what": "회고 분석 CLI 실행 결과. 아침 작업이 이 파일을 먼저 읽고 실패를 보고한다.",
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rc": a.rc,
        "ok": ok,
        "declared": declared or "(보고 블록 없음)",
        "model": a.model,
        "log": a.log,
        "summary": summary,
    }
    try:
        from common import save_json_atomic
        save_json_atomic(STATUS, payload)
    except Exception:
        import json
        with io.open(STATUS, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    print("[retro-status] ok=%s rc=%s declared=%s -> %s"
          % (ok, a.rc, payload["declared"], STATUS))
    if not ok:
        print("[retro-status] ★전야 리서치가 정상 완료되지 않았다. 아침 작업이 이 사실을 보고할 것.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
