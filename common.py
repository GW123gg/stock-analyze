# -*- coding: utf-8 -*-
"""
common.py — 여러 스크립트가 복붙하던 '원자적 저장' 원시함수를 한 곳으로 통합.

[원칙]
  - **부작용 없음**: import 해도 stdout.reconfigure·전역 초기화 등 아무 부작용이 없다(순수 함수만).
    (count_articles 류가 겪던 'import 시 stdout 오염'을 절대 만들지 않는다.)
  - **기존 동작 100% 보존**: 각 호출부의 기존 동작을 플래그(fsync/ensure_dir/ensure_ascii/indent)로
    그대로 재현한다. 기본값은 수집기 8종의 복붙 원본과 바이트 동일:
      makedirs(dirname or ".") → tmp(.tmp) 쓰기 → json.dump(ensure_ascii=False, indent=2) → os.replace.
  - 여기엔 '진짜로 동일한' 원시함수만 둔다. RSI/OBV·숫자변환·키로더처럼 스크립트별로 미묘히 다른
    헬퍼는 통합하지 않는다(값·부작용이 달라질 수 있음).
"""
import os
import json


def save_json_atomic(path, obj, *, ensure_ascii=False, indent=2, fsync=False, ensure_dir=True):
    """obj 를 path 에 **원자적으로** JSON 저장한다(.tmp 에 쓰고 os.replace).

    - ensure_dir=True(기본): 상위 폴더를 makedirs(exist_ok). 수집기 8종 원본과 동일.
    - fsync=True: 디스크 flush 후 rename(전원/강제종료 시 빈·잘린 파일 방지). retro_dataset 등 무결성 필요분.
    - ensure_ascii/indent: json.dump 인자. 기본은 원본과 동일(ensure_ascii=False, indent=2).
    부분 실패 시 .tmp 만 남고 원본 path 는 보존된다(원자성).
    """
    if ensure_dir:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=ensure_ascii, indent=indent)
        if fsync:
            f.flush()
            os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_text(path, text):
    """text 를 path 에 **원자적으로** 저장한다(.tmp → os.replace).

    상위 폴더는 만들지 않는다(기존 텍스트 저장 호출부가 이미 존재하는 폴더에만 썼으므로 동작 보존).
    필요하면 호출부에서 폴더를 먼저 만든다.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
