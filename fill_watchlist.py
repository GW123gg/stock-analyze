#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""카이로스 관심종목을 아침 캡처가 쓸 목록으로 채운다.

왜 필요한가
  0231(신용/공매도/대차)·0261(외국인/기관)은 **관심종목에 올라간 종목만** 찍힌다.
  목록이 비어 있으면 두 화면이 통째로 빈 표가 되고, 그날 리포트의 공매도잔고·대차잔고·
  종목별 수급이 전부 공백이 된다(2026-08-19·08-20 실사고: `watchlist_used=['000000']`).

  아침 캡처(`hts_capture_collect.py`)가 알아서 채우지만, 그 전에 사람이나 점검 작업이
  "지금 목록이 비었는지" 보고 미리 채워두고 싶을 때가 있다. 그 한 줄을 위한 스크립트다.

무엇을 넣나
  `hts_capture_collect._tickers_from_session()` 이 만드는 목록을 그대로 쓴다 —
  **등록자들의 국내 보유 종목이 앞**, 감시 종목(watch_tickers.txt)이 뒤, 상한 18종.
  18칸인 이유: 0231·0261 은 스크롤 없이 보이는 만큼만 캡처되고, 실측상 20종이면
  마지막 줄이 표 하단에 걸쳐 잘리기 직전이었다.

쓰는 법
  python fill_watchlist.py            현재 목록과 다르면 채운다(같으면 건드리지 않는다)
  python fill_watchlist.py --check    무엇이 들어갈지만 보여준다(조작 없음)
  python fill_watchlist.py --force    같아도 다시 넣는다

★삭제가 종목당 8초라 18종 교체에 2~3분 걸린다. 같으면 건너뛰는 것이 기본값인 이유다.
"""
from __future__ import annotations

import argparse
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main() -> int:
    ap = argparse.ArgumentParser(description="카이로스 관심종목 채우기(아침 캡처용 18종)")
    ap.add_argument("--check", action="store_true", help="계획만 출력(조작 없음)")
    ap.add_argument("--force", action="store_true", help="현재 목록과 같아도 다시 넣는다")
    a = ap.parse_args()

    try:
        import hts_capture_collect as h
        import kairos_client as kc
    except Exception as exc:                       # noqa: BLE001
        print("[fill] 모듈 로드 실패: %s" % type(exc).__name__)
        return 1

    want = h._tickers_from_session("output/_none_")   # 세션 없음 → 보유+감시 폴백
    if not want:
        print("[fill] 넣을 종목을 만들지 못했다 — watch_tickers.txt 와 portfolios/ 를 확인하라.")
        return 1

    try:
        cur = [str(t) for t in ((kc.get_watchlist() or {}).get("tickers") or [])]
    except Exception as exc:                       # noqa: BLE001
        print("[fill] 카이로스에 연결하지 못했다(%s) — 노트북이 꺼져 있거나 토큰이 없다."
              % type(exc).__name__)
        return 4

    usable = [t for t in cur if t != "000000"]
    print("[fill] 현재 %d종%s / 넣을 %d종"
          % (len(cur), " (★유효 0 — 빈 표가 찍힌다)" if not usable else "", len(want)))
    print("[fill] 넣을 목록: %s" % ", ".join(want))

    if a.check:
        print("[fill] --check: 조작하지 않았다.")
        return 0

    if cur == want and not a.force:
        print("[fill] 이미 같은 목록이다 — 건드리지 않는다(교체에 2~3분 걸리므로).")
        return 0

    t0 = time.time()
    try:
        r = kc.set_watchlist(want)
    except Exception as exc:                       # noqa: BLE001
        print("[fill] 설정 실패: %s" % type(exc).__name__)
        return 1
    print("[fill] 완료 %.1f초 — 지움 %s / 넣음 %s"
          % (time.time() - t0, r.get("removed"), r.get("added")))
    if not r.get("ok"):
        print("[fill] ★서버가 실패로 응답했다: %s" % (r.get("error") or "사유 없음"))
        return 1
    print("[fill] ★0231·0261 을 열어 표가 채워졌는지 눈으로 확인하라"
          " (python kairos_client.py --walk --walk-screens short_lend --dwell 5).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
