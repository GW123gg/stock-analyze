#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
retro_manual_refresh.py — '수동 회고' 채널용 데이터 갱신기 (3:30 자동 루프와 별개)

[목적]
  사용자가 '수동으로' 과거 추천을 복기하고 싶을 때, 최신 데이터셋을 만들어
  바탕화면의 별도 폴더(stock_retro_manual)로 복사해 둔다. 그 폴더를 작업폴더로 한
  '별도 Claude Desktop(Cowork)'이 회고분석_수동_지시사항.md 를 따라 분석한다.

  ※ 호스트 자동화(supervisor)·03:30 회고 Cowork(stock_retro)와 완전히 분리되어 있다.
    이 스크립트는 트리거/플래그가 없고, 사용자가 원할 때만 직접 실행한다.

[동작]
  1) retro_label.py 를 1회 실행해 retro_dataset.json/csv 를 최신화(FSC 정산 종가 기반).
  2) retro_dataset.json/csv + scorecard.md 를 stock_retro_manual 폴더로 복사.
     (지시문 회고분석_수동_지시사항.md 는 그 폴더에 상주 — 덮어쓰지 않는다.)

[실행]
  python retro_manual_refresh.py           # 또는 stock_retro_manual\데이터_갱신.bat 더블클릭
  python retro_manual_refresh.py --dest "다른경로"
"""
import os
import sys
import shutil
import argparse
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DEST = os.path.join(os.path.expanduser("~"), "Desktop", "stock_retro_manual")
COPY_FILES = ["retro_dataset.json", "retro_dataset.csv", "scorecard.md"]

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="수동 회고 데이터 갱신")
    ap.add_argument("--dest", default=DEFAULT_DEST, help="복사할 폴더(기본 바탕화면\\stock_retro_manual)")
    ap.add_argument("--no-rebuild", action="store_true", help="데이터셋 재생성 생략(기존 파일만 복사)")
    args = ap.parse_args()

    if not args.no_rebuild:
        print("[1] retro_label.py 실행 — 데이터셋 최신화(FSC 정산 종가)")
        try:
            subprocess.run([sys.executable, os.path.join(HERE, "retro_label.py")],
                           cwd=HERE, timeout=1200)
        except Exception as e:
            print("    경고: retro_label 실행 실패(기존 데이터셋으로 진행):", type(e).__name__, e)

    os.makedirs(args.dest, exist_ok=True)
    print("[2] stock_retro_manual 로 복사 →", args.dest)
    n = 0
    for f in COPY_FILES:
        src = os.path.join(HERE, f)
        if os.path.isfile(src):
            try:
                shutil.copy2(src, os.path.join(args.dest, f))
                print("    복사:", f)
                n += 1
            except Exception as e:
                print("    복사 실패", f, ":", type(e).__name__, e)
        else:
            print("    (없음, 건너뜀):", f)
    print("[완료] %d개 파일 갱신. 이제 별도 Claude Desktop 을 '%s' 폴더로 열어 "
          "회고분석_수동_지시사항.md 대로 분석하세요." % (n, args.dest))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
