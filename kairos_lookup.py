# -*- coding: utf-8 -*-
"""
kairos_lookup.py — 분석 중 '이 종목 데이터 좀 더 보자' 온디맨드 조회 [v10.9 신규]

[왜] 아침 파이프라인(step9.5)은 **미리 정한 종목**으로 한 번 캡처한다. 그런데 분석 도중
  "이 종목 공매도 잔고가 궁금하다"가 생기면 그때 다시 받아야 한다. 이 스크립트가 그 진입점이다.
  Cowork 분석가가 **한 줄로** 호출해 이미지 경로를 받고 바로 읽으면 된다.

[★안전] 관심종목 교체는 항상 set → capture → **reset** 을 try/finally 로 묶는다.
  예외·중단이 나도 사용자 관심종목을 원래대로 되돌린다(에이전트는 '넣은 개수만큼만' 지운다).

[★검증] kairos_client 의 3중 검증(marker 화면번호·sha256·settled)을 그대로 통과한 장만 저장한다.
  ⚠단 화면이 '안정적으로 틀린' 경우는 호스트가 못 잡는다 —
  **읽은 뒤 반드시 행 단위 산술 검증**을 하라([5.15]):
    ① 현재가 - 전일대비 == 외부 전일 종가(fsc_prices/FDR)  ← 주 검증
    ② 등락률 == 전일대비 / (현재가 - 전일대비) x 100
  ①통과·②불일치면 **등락률만 재계산해 쓰고** 그 사실을 밝혀라(행 폐기 아님).
  ※0231 '현재가'는 정규장 종가가 아니라 시간외 단일가다 — 종가는 fsc_prices 를 써라.

[사용법]
  python kairos_lookup.py 005930                      # 기본 2화면(공매도/대차 + 외인/기관)
  python kairos_lookup.py 005930,000660 --screens short_lend
  python kairos_lookup.py 005930 --out-dir output/2026-07-29_063000/hts_captures
  python kairos_lookup.py --market                    # 관심종목 무관 시장 화면(수급·베이시스 등)
"""
import os
import sys
import json
import argparse
import logging
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(level=logging.INFO, format="[lookup] %(message)s")
log = logging.getLogger("lookup")

import kairos_client as kc
from hts_capture_collect import SCREENS

# 종목별(관심종목 필요) 기본 세트 / 시장 전체(관심종목 무관) 기본 세트
TICKER_SCREENS = ["short_lend", "foreign_inst"]
MARKET_SCREENS = ["investor_daily", "basis", "program_daily", "broker_3d"]


def lookup(tickers, screen_keys, out_dir):
    """종목(선택) + 화면 목록 → 저장된 이미지 경로 목록. 관심종목은 항상 원복한다."""
    h = kc.health()
    if not h.get("hts") or h.get("hts_login_screen"):
        log.warning("HTS 사용 불가 — hts=%s login_screen=%s (사람 확인 필요)",
                    h.get("hts"), h.get("hts_login_screen"))
        return []

    os.makedirs(out_dir, exist_ok=True)
    need_wl = any((SCREENS.get(k) or {}).get("needs_watchlist") for k in screen_keys)
    did_set = False
    saved = []
    try:
        if need_wl:
            if not tickers:
                log.warning("관심종목 화면인데 종목이 없다 — 빈 화면이 나온다. 중단")
                return []
            r = kc.set_watchlist(tickers)
            did_set = True
            log.info("관심종목 %s 설정(removed=%s added=%s)",
                     ",".join(tickers), r.get("removed"), r.get("added"))

        for key in screen_keys:
            spec = SCREENS.get(key)
            if not spec:
                log.warning("%s: 미등록 화면 — 건너뜀", key)
                continue
            try:
                shots, meta = kc.capture(key, expect_no=spec["no"])
            except kc.KairosError as e:
                log.warning("%-14s 실패 — %s", key, e)
                continue
            stamp = datetime.now().strftime("%Y%m%d_%H%M")
            for i, s in enumerate(shots):
                if not s["ok"]:
                    log.warning("%-14s 폐기 — %s", key, s["reason"][:60])
                    continue
                suffix = ("_" + str(s.get("variant"))) if s.get("variant") else ""
                fp = os.path.join(out_dir, "%s%s_%s.png" % (key, suffix, stamp))
                with open(fp, "wb") as f:
                    f.write(s["png"])
                saved.append(fp)
                log.info("%-14s 저장 %s (%.1f KB) marker=%s",
                         key, os.path.basename(fp), s["bytes"] / 1024,
                         str(meta.get("marker_text"))[:40])
    finally:
        if did_set:
            try:
                r = kc.reset_watchlist()
                log.info("관심종목 복구(removed=%s)", r.get("removed"))
            except Exception as e:
                log.warning("★관심종목 복구 실패 — 사람이 확인해야 한다: %s", type(e).__name__)
    return saved


def main():
    ap = argparse.ArgumentParser(description="분석 중 카이로스 온디맨드 조회")
    ap.add_argument("tickers", nargs="?", default=None, help="쉼표구분 종목코드(6자리)")
    ap.add_argument("--screens", default=None, help="쉼표구분 화면 키(기본: 용도별 세트)")
    ap.add_argument("--market", action="store_true", help="관심종목 무관 시장 화면 세트")
    ap.add_argument("--out-dir", default=None, help="저장 폴더(기본 tmp_cap/)")
    ap.add_argument("--list", action="store_true", help="화면 카탈로그")
    args = ap.parse_args()

    if args.list:
        for k, v in SCREENS.items():
            print("%-16s %-5s %-32s %s" % (k, v["no"], v["name"],
                                           "관심종목필요" if v["needs_watchlist"] else ""))
        return 0
    if not kc.load_token():
        log.warning("kairos_api.txt 토큰 없음 — 종료")
        return 1

    tickers = [t.strip() for t in (args.tickers or "").split(",") if t.strip()]
    if args.screens:
        keys = [s.strip() for s in args.screens.split(",") if s.strip()]
    elif args.market or not tickers:
        keys = MARKET_SCREENS
    else:
        keys = TICKER_SCREENS

    out_dir = args.out_dir or os.path.join(HERE, "tmp_cap")
    saved = lookup(tickers, keys, out_dir)
    if not saved:
        log.warning("저장된 이미지 없음")
        return 1
    print()
    print("읽을 이미지 %d장:" % len(saved))
    for p in saved:
        print("  " + p)
    print()
    print("★읽은 뒤 반드시 행 단위 산술 검증([5.15]):")
    print("  ① 현재가 - 전일대비 == 외부 전일 종가(fsc_prices/FDR)")
    print("  ② 등락률 == 전일대비 / (현재가 - 전일대비) x 100")
    print("  ①통과·②불일치 → 등락률만 재계산해 쓰고 그 사실을 밝혀라")
    print("  ※0231 현재가는 시간외 단일가다 — 정규장 종가는 fsc_prices 를 써라")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
