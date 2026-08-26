#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cowork_skill_check.py — 예정작업 이름과 내용물이 어긋났는지 대조한다.

[왜 이 파일이 생겼나 — 2026-08-13~26 실사고]
  Cowork 예정작업 `stock-research`(평일 06:20, "장 시작전 분석")의 SKILL.md 본문이
  **회고 지시문**이었다. 프론트매터는 `name: stock-research / description: 장 시작전 분석`
  인데, 5번째 줄부터 `night-research`(회고)와 **바이트 단위로 동일**했다
  ("너는 이 시스템의 사후평가(회고) 분석가다", "■ 왜 17:30 인가"까지 그대로).

  그래서 매일 아침 06:20 에 깨어난 작업은 회고 지시문을 읽고, 그 안의 자기보호 규칙
  (06:00~08:00 아침 슬롯 보호)에 걸려 스스로 멈췄다. **회고로서는 옳게 멈췄지만
  아침 리서치로서는 시작조차 못 했다.** 아침 메일이 2주 넘게 안 나간 진짜 이유다.

  회고는 매 회차 원인을 정확히 짚어 원장에 적었지만(A53 -> A56), 이름과 내용물이
  어긋났다는 사실 자체를 **기계가 볼 방법이 없어서** 같은 지적이 반복 발행됐다.

[무엇을 보나 — 추측이 아니라 대조]
  1) **본문 중복** : 서로 다른 두 작업의 본문이 동일하다 → 거의 확실히 복사 사고.
     이번 사고의 정확한 형태이고, 판정에 해석이 끼지 않는다(가장 강한 신호).
  2) **이름 불일치** : 프론트매터 `name` 이 폴더 이름과 다르다.
  3) **성격 모순**  : description 은 '아침/장 시작전'인데 본문 첫 줄은 '회고/사후평가'
     (또는 그 반대). 키워드 대조라 약한 신호 → 경고로만 쓴다.

[쓰는 법]
  python cowork_skill_check.py            # 사람이 읽는 표
  python cowork_skill_check.py --json     # 기계용
  python cowork_skill_check.py --quiet    # 종료코드만 (러너용)

  종료코드 0=이상 없음 / 1=어긋남 발견 / 2=점검 불가(폴더 없음)
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys

DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), "Documents", "Claude", "Scheduled")

# 성격 키워드 — 서로 섞이면 안 되는 짝
_KINDS = {
    "회고": ("사후평가", "회고 분석가", "retro"),
    "아침": ("장 시작전", "아침 리서치", "리서치 데스크 분석가"),
    "전야": ("전야", "지수 콜"),
}


def _split_front(text):
    """(프론트매터 dict, 본문). 프론트매터가 없으면 ({}, 전체)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    front, body = text[3:end], text[end + 4:]
    meta = {}
    for line in front.splitlines():
        k, _, v = line.partition(":")
        if _ and k.strip():
            meta[k.strip()] = v.strip()
    return meta, body


def _first_line(body):
    for ln in body.splitlines():
        s = ln.strip()
        if s and not s.startswith(("#", ">", "-", "*", "|")):
            return s
    return ""


def _kind_of(text):
    hits = [k for k, words in _KINDS.items() if any(w in text for w in words)]
    return hits[0] if len(hits) == 1 else ""


def scan(root):
    tasks = []
    if not os.path.isdir(root):
        return tasks
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name, "SKILL.md")
        if not os.path.isfile(p):
            continue
        try:
            text = io.open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        meta, body = _split_front(text)
        norm = re.sub(r"\s+", " ", body).strip()
        tasks.append({
            "task": name,
            "path": p,
            "name": meta.get("name", ""),
            "description": meta.get("description", ""),
            "first_line": _first_line(body)[:120],
            "body_hash": hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16],
            "body_kind": _kind_of(body[:600]),
            "desc_kind": _kind_of(meta.get("description", "")),
            "bytes": len(text),
        })
    return tasks


def findings(tasks):
    out = []

    # 1) 본문 중복 — 가장 강한 신호. 해석이 끼지 않는다.
    by_hash = {}
    for t in tasks:
        by_hash.setdefault(t["body_hash"], []).append(t["task"])
    for h, names in by_hash.items():
        if len(names) > 1:
            out.append({
                "level": "ERROR", "kind": "본문 중복", "tasks": names,
                "detail": "서로 다른 작업 %s 의 본문이 동일하다 — 복사 사고일 가능성이 높다"
                          % " · ".join(names),
            })

    for t in tasks:
        # 2) 이름 불일치
        if t["name"] and t["name"] != t["task"]:
            out.append({
                "level": "ERROR", "kind": "이름 불일치", "tasks": [t["task"]],
                "detail": "폴더는 '%s' 인데 프론트매터 name 은 '%s'" % (t["task"], t["name"]),
            })
        # 3) 성격 모순 — 약한 신호(키워드 대조)라 경고로만
        if t["desc_kind"] and t["body_kind"] and t["desc_kind"] != t["body_kind"]:
            out.append({
                "level": "WARN", "kind": "성격 모순", "tasks": [t["task"]],
                "detail": "description 은 '%s' 인데 본문은 '%s' 로 읽힌다 — 첫 줄: %s"
                          % (t["desc_kind"], t["body_kind"], t["first_line"][:70]),
            })
    return out


def main():
    ap = argparse.ArgumentParser(description="예정작업 이름·내용물 대조")
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    tasks = scan(a.root)
    if not tasks:
        if not a.quiet:
            print("[skillchk] 점검 불가 — SKILL.md 를 찾지 못했다: %s" % a.root)
        return 2

    fs = findings(tasks)
    if a.json:
        print(json.dumps({"root": a.root, "n_tasks": len(tasks), "findings": fs},
                         ensure_ascii=False, indent=2))
        return 1 if any(f["level"] == "ERROR" for f in fs) else 0

    if not a.quiet:
        print("[skillchk] 작업 %d개 점검: %s" % (len(tasks), a.root))
        if not fs:
            print("[skillchk] 어긋남 없음.")
        for f in fs:
            print("  [%-5s] %-8s %s" % (f["level"], f["kind"], f["detail"]))
        if fs:
            print("[skillchk] ★이름과 내용물이 어긋난 작업은 '이름대로 동작하지 않는다'.")
            print("[skillchk]   2026-08-13~26 에 아침 메일이 2주 넘게 안 나간 원인이 이것이었다.")
    return 1 if any(f["level"] == "ERROR" for f in fs) else 0


if __name__ == "__main__":
    sys.exit(main())
