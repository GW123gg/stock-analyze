#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
heartbeat.py — 장시간 코워크 세션 유지용 대기 타이머 [v11.12 신규]

[왜 필요한가]
  08:00 개장 자동매매 코워크는 감시 사이클(시세 확인 → 계획 대조)을 장중 내내 반복해야
  하는데, 코워크 세션은 **도구 호출 없이 놀면 종료**된다. 이 스크립트가 '다음 사이클까지
  대기'라는 무해한 작업이 되어 세션을 살아 있게 한다 — 대기 타이머 + 생존 신호 겸용.

[안전]
  - 아무것도 읽지도 쓰지도 않는다(heartbeat 로그 한 줄 제외). 시장·계좌에 부작용 0.
  - 최대 대기 600초로 강제 캡 — 잘못 불러도 10분이면 끝난다.
  - 매 60초마다 진행 상황을 출력한다(터미널이 살아있음을 보여준다).

[사용] python heartbeat.py --wait 300     # 300초 대기 후 종료(exit 0)
       python heartbeat.py --until 09:30  # 그 시각까지(최대 600초 단위로 끊어 호출할 것)
"""
from __future__ import annotations

import os
import sys
import time
import argparse
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_WAIT = 600          # ★강제 캡 — 이보다 길게는 못 기다린다(사이클을 더 자주 돌아라)


def main() -> int:
    ap = argparse.ArgumentParser(description="코워크 세션 유지용 대기 타이머")
    ap.add_argument("--wait", type=int, default=300, help="대기 초(최대 600)")
    ap.add_argument("--until", default=None, metavar="HH:MM",
                    help="이 시각까지 대기(역시 최대 600초 캡 — 넘으면 600초만 기다리고 종료)")
    args = ap.parse_args()

    sec = max(1, min(int(args.wait), MAX_WAIT))
    if args.until:
        try:
            h, m = args.until.split(":")
            tgt = datetime.now().replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            remain = (tgt - datetime.now()).total_seconds()
            if remain <= 0:
                print("[heartbeat] %s 은 이미 지났다 — 즉시 종료(다음 단계로 진행하라)" % args.until)
                return 0
            sec = max(1, min(int(remain), MAX_WAIT))
        except ValueError:
            print("[heartbeat] --until 형식 오류(HH:MM) — --wait 값으로 대기")

    print("[heartbeat] %d초 대기 시작 (%s)" % (sec, datetime.now().strftime("%H:%M:%S")))
    try:
        with open(os.path.join(HERE, "logs", "heartbeat.log"), "a", encoding="utf-8") as f:
            f.write("%s wait=%ds\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sec))
    except Exception:
        pass

    left = sec
    while left > 0:
        step = min(60, left)
        time.sleep(step)
        left -= step
        if left > 0:
            print("[heartbeat] ... %d초 남음 (%s)" % (left, datetime.now().strftime("%H:%M:%S")))
    print("[heartbeat] 대기 끝 (%s) — 다음 사이클을 진행하라" % datetime.now().strftime("%H:%M:%S"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("[heartbeat] 중단됨")
        sys.exit(0)
