#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
retro_archive_parse.py — 구조화 예측(predictions.json) 이전 발송 리포트(03_final_report.md) 파싱

[목적]
  6/18 이전 세션은 predictions.json 이 없고 발송 리포트(.md)만 있다. 그 리포트의 추천 표를 파싱해
  (날짜·종목·태그·롱/숏)를 뽑아 '예측 형식'으로 만들어, retro_label 이 FSC 정산가로 사후 채점하게 한다.
  → 회고 표본(N)을 옛 발송분까지 확장(5대 실패모드를 더 많은 데이터로 검증).

[주의·한계] 프로즈 표 파싱이라 100% 정확하지 않다(_src_kind='archive' 로 구분 표시). 잡주(매수금지)
  섹션은 제외. [테스트] 발송분 제외. 진입가(entry_ref)는 추천일 FSC 종가로 대체(원문 진입가 미보존).

[표 형식 가정] ## 2. 타점 진입…/2-중소형 = 롱 picks, ## 3 …숏 = shorts, ## 2-주의 잡주 = 제외.
  행: | … 종목명 (123456) | [장투가능]/[단기스윙]/[장전선취매] | … |
"""
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
ARCHIVE_DIR = os.path.join(OUTPUT_DIR, "_archive")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import fsc_collect as fsc
    FSC_OK = True
except Exception:
    fsc = None
    FSC_OK = False

TICK_RE = re.compile(r"\((\d{6})\)")
TAG_RE = re.compile(r"\[(단기스윙|장투가능|장전선취매)\]")
HORIZON_BY_TAG = {"장전선취매": 1, "단기스윙": 5, "장투가능": 20}


def _section_kind(header):
    h = header
    # ★'## 2-주의.' 섹션은 헤더 문구와 무관하게 전부 '추천 표 제외' 섹션이다(상단 [표 형식 가정]과 일치).
    #   구 구현은 '잡주' 리터럴에만 걸려 '강한 분산 경계(추천 표 제외)'·'매수 회피'·'관망/주의' 같은
    #   회피 경고 섹션이 롱 pick 으로 채점됐다(실측: 회피 헤더 4종이 pick 으로 분류되고 있었다).
    if ("잡주" in h or "매수 금지" in h or "매수금지" in h
            or re.search(r"##\s*2-\s*(주의|⚠)", h)):
        return "exclude"
    if "숏" in h or "short" in h.lower():
        return "short"
    # '## 2 …' 계열(타점/진입/중소형/주목/선취매)은 롱 picks
    if (any(k in h for k in ("타점", "진입", "중소형", "주목", "선취매")) or
            re.search(r"##\s*2", h)):
        return "pick"
    return None


def parse_report(md):
    """리포트 마크다운 → (picks[{ticker,name,tag}], shorts[{ticker,name}])."""
    picks, shorts, seen = [], [], set()
    cur = None
    for line in md.splitlines():
        if line.startswith("## "):
            cur = _section_kind(line)
            continue
        if cur not in ("pick", "short") or "|" not in line:
            continue
        if re.match(r"\s*\|[\s:\-|]+\|?\s*$", line):   # 구분선
            continue
        if ("종목명" in line and ("티커" in line or "순위" in line)):  # 헤더행
            continue
        m = TICK_RE.search(line)
        if not m:
            continue
        code = m.group(1)
        if code in seen:
            continue
        seen.add(code)
        name = ""
        for c in line.split("|"):
            if "(%s)" % code in c:
                name = c.split("(")[0].strip().strip("*").strip()
                break
        tagm = TAG_RE.search(line)
        tag = tagm.group(1) if tagm else ""
        if cur == "short":
            shorts.append({"ticker": code, "name": name})
        else:
            picks.append({"ticker": code, "name": name, "tag": tag})
    return picks, shorts


def _entry_close(ticker, date_obj):
    """★v11.6(호스트 감사 2026-08-01): 앵커를 **D-1 종가**(date_obj 미만 마지막 봉)로 교정.
    구현이 'd >= date_obj 첫 종가'(=추천일 D 종가, 주말 리포트면 다음 거래일 종가)라
    예측행(entry_ref=D-1 종가 계약)과 빈티지가 달랐고, 알파 지수다리(v10.5 에서 D-1 앵커로
    통일)와도 역방향 비대칭 — 추천일 하루치 종목 변동이 라벨에서 통째로 빠졌다
    (실측: 2026-05-30 005930 D 종가 349,000 vs D-1 317,000 — 첫날 +10.1% 소실).
    반영 시 archive 행(191행) ret/alpha 전량 리베이스라인 — 원장 A표 등재, 회고 통지."""
    if not FSC_OK:
        return None
    try:
        last = None
        for d, c in fsc.get_close_series(ticker, date_obj):
            if d < date_obj:
                last = c
            else:
                break
        return last
    except Exception:
        pass
    return None


def _fill(items, date_obj):
    out = []
    for it in items:
        it2 = dict(it)
        it2["horizon_days"] = HORIZON_BY_TAG.get(it.get("tag", ""), 5)
        it2["entry_ref"] = _entry_close(it["ticker"], date_obj)
        # ★v11.6: archive 앵커는 FSC 시세로 추정한 값 — 예측행과 구분되도록 명시
        #   (기존엔 미설정 → retro_label 이 bool(None)=False 로 기록해 전 행이 '실측'처럼 보였다)
        it2["entry_ref_estimated"] = True
        out.append(it2)
    return out


def _iter_report_sessions():
    """output/ 와 output/_archive/ 의 세션 폴더(name, path) yield (이름 _ 시작/__pycache__ 제외)."""
    for base in (OUTPUT_DIR, ARCHIVE_DIR):
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            if name.startswith("_") or name == "__pycache__":
                continue
            sess = os.path.join(base, name)
            if os.path.isdir(sess):
                yield name, sess


def load_archive_predictions(skip_dates=None):
    """predictions.json 없는 세션(output + _archive)의 리포트를 파싱 → 예측형 dict 리스트(날짜 오름차순).
    각 픽/숏에 entry_ref(추천일 FSC 종가)·horizon_days(태그 기반) 채워 반환.
    predictions.json 이 (어느 위치든) 있는 날짜는 통째로 제외(구조화 예측 우선)."""
    skip = set(skip_dates or [])
    # predictions.json 이 있는 날짜는 통째로 스킵
    for name, sess in _iter_report_sessions():
        if os.path.isfile(os.path.join(sess, "predictions.json")):
            skip.add(name[:10])
    by_date = {}
    for name, sess in _iter_report_sessions():
        date = name[:10]
        if date in skip:
            continue
        md_path = os.path.join(sess, "03_final_report.md")
        if not os.path.isfile(md_path):
            continue
        try:
            md = open(md_path, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        if "[테스트]" in md[:600] or "[TEST]" in md[:600]:
            continue
        picks, shorts = parse_report(md)
        if not picks and not shorts:
            continue
        rec = by_date.setdefault(date, {"date": date, "_src": md_path,
                                        "_src_kind": "archive", "picks": [], "shorts": []})
        have = {p["ticker"] for p in rec["picks"]} | {s["ticker"] for s in rec["shorts"]}
        for p in picks:
            if p["ticker"] not in have:
                rec["picks"].append(p); have.add(p["ticker"])
        for s in shorts:
            if s["ticker"] not in have:
                rec["shorts"].append(s); have.add(s["ticker"])
    out = []
    for date, rec in by_date.items():
        try:
            do = datetime.strptime(date, "%Y-%m-%d").date()
        except Exception:
            continue
        rec["picks"] = _fill(rec["picks"], do)
        rec["shorts"] = _fill(rec["shorts"], do)
        out.append(rec)
    out.sort(key=lambda r: r["date"])
    return out


if __name__ == "__main__":
    aps = load_archive_predictions()
    print("[archive] 파싱된 예측일: %d개" % len(aps))
    tot_p = tot_s = 0
    for ap in aps:
        tot_p += len(ap["picks"]); tot_s += len(ap["shorts"])
        ex = ", ".join("%s(%s)" % (p["ticker"], p.get("tag") or "?") for p in ap["picks"][:4])
        print("  %s  picks=%d shorts=%d  | %s" % (ap["date"], len(ap["picks"]), len(ap["shorts"]), ex))
    print("[archive] 합계 picks=%d shorts=%d" % (tot_p, tot_s))
