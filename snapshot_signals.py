#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
snapshot_signals.py ─ 루트 저장 신호 5종을 '오늘 세션'에 동결(freeze) 복사

[왜 필요한가 — 회고 12회차 감사 결과]
  deriv_sentiment/ecos_macro/vkospi/market_caution/credit_balance 5종은 루트에 저장되고 매일 덮어써진다.
  그래서 (a) 회고 시점에 '그날 분석가가 본 국면 입력'을 재현할 수 없고,
      (b) retro_label 이 이 5종을 피처로 쓸 수 없어(실측 0건) F1/F8 게이트의 1차 입력이
          회고 학습에서 통째로 빠져 있었다(사각지대 #10).
  이 스크립트가 신호 체인 '맨 마지막'(market_caution 다음)에 1회 돌면, 그날의 국면 입력이
  세션에 signals_snapshot_*.json 으로 남아 회고가 룩어헤드 없이 학습할 수 있다.

[설계]
  - 읽기·복사만 한다(원본 무수정). 기존 파일 스키마 변경 없음.
  - 오늘 세션이 없으면 아무것도 안 하고 exit 0(파이프라인 무중단).
  - 원자적 저장(common.save_json_atomic, fsync) — 회고가 읽는 영구 데이터라 무결성 우선.
  - 콘솔 ASCII 태그([snapshot])만, 이모지 금지. UTF-8 IO, ensure_ascii=False.

[사용법]
  python snapshot_signals.py            # 루트 5종 → 오늘 세션에 동결
  python snapshot_signals.py --check    # 무엇이 복사될지 점검만(쓰기 없음)
"""
import os
import sys
import json
import argparse
import logging
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("snapshot")

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")

# 루트에 저장되는 국면·거시 신호(세션 아님) — CLAUDE.md/CODEMAP.md 의 '루트 저장 5종'과 동일
ROOT_SIGNALS = ["deriv_sentiment.json", "ecos_macro.json", "vkospi.json", "market_caution.json",
                "credit_balance.json"]   # v9.8: 신용잔고(빚투) 국면 신호 — 회고 pre_margin_* 원천
PREFIX = "signals_snapshot_"

from common import save_json_atomic


def _today_latest_session():
    """H-3: common.resolve_session 위임 - 자정 경계 완화(6h 폴백) + 복제 제거.
    ★prefer_sameday_earliest=True: 같은 날 세션이 2개 이상이면(데몬+온디맨드 동시) '가장 먼저
    생성된' 세션(=predictions 가 붙는 채택 세션)에 동결한다. mtime 최신을 고르면 늦게 생긴 orphan
    세션에 동결돼 회고 국면입력이 공백이 된다(2026-07-21·22 실사고)."""
    from common import resolve_session
    return resolve_session(OUTPUT_DIR, prefer_sameday_earliest=True)


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("[snapshot] 읽기 실패 %s: %s", os.path.basename(path), type(e).__name__)
        return None


def snapshot(session_dir, check_only=False):
    """루트 5종 → session_dir/signals_snapshot_<name>.json. 복사한 파일명 리스트 반환."""
    done = []
    for fn in ROOT_SIGNALS:
        src = os.path.join(HERE, fn)
        if not os.path.isfile(src):
            log.info("[snapshot] 없음(생략): %s", fn)
            continue
        payload = _load(src)
        if payload is None:
            continue
        # 언제 어느 원본을 동결했는지 남긴다(회고가 신선도를 판정할 수 있게)
        meta = {
            "_snapshot_of": fn,
            "_snapshot_at": datetime.now().isoformat(timespec="seconds"),
            "_source_generated_at": (payload.get("generated_at")
                                     if isinstance(payload, dict) else None),
        }
        if isinstance(payload, dict):
            out = dict(payload)
            out.update(meta)
        else:
            out = {"data": payload}
            out.update(meta)
        dest = os.path.join(session_dir, PREFIX + fn)
        if check_only:
            log.info("[snapshot] (check) %s -> %s", fn, os.path.basename(dest))
            done.append(fn)
            continue
        try:
            save_json_atomic(dest, out, fsync=True)   # 영구 회고 입력 — 무결성 우선
            done.append(fn)
        except Exception as e:
            log.warning("[snapshot] 저장 실패 %s: %s", fn, type(e).__name__)
    return done


def main():
    ap = argparse.ArgumentParser(description="루트 신호 5종을 오늘 세션에 동결 복사")
    ap.add_argument("--check", action="store_true", help="복사 대상만 점검(쓰기 없음)")
    ap.add_argument("--session", default="", help="세션 폴더 직접 지정(기본: 오늘 최신)")
    args = ap.parse_args()

    sess = args.session or _today_latest_session()
    if not sess or not os.path.isdir(sess):
        log.info("[snapshot] 오늘 세션 없음 — 동결 생략(정상 종료)")
        return 0
    # 같은 날 세션이 2개 이상이면(데몬+온디맨드 이중 생성) 어디에 동결하는지 크게 경고 —
    # 의도와 다르면 --session 으로 명시하라(2026-07-21·22 실사고: orphan 세션에 동결됨).
    if not args.session:
        _today = datetime.now().strftime("%Y-%m-%d")
        try:
            _same = [n for n in os.listdir(OUTPUT_DIR)
                     if n.startswith(_today) and os.path.isdir(os.path.join(OUTPUT_DIR, n))]
            if len(_same) > 1:
                log.warning("[snapshot] ★같은 날 세션 %d개 감지: %s -> 동결 대상=%s "
                            "(의도와 다르면 --session 으로 지정하라)",
                            len(_same), ", ".join(sorted(_same)), os.path.basename(sess))
        except Exception:
            pass
    done = snapshot(sess, check_only=args.check)
    log.info("[snapshot] %s: %d/%d 동결 -> %s",
             "점검" if args.check else "완료", len(done), len(ROOT_SIGNALS), sess)
    return 0


if __name__ == "__main__":
    sys.exit(main())
