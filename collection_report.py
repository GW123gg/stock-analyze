#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collection_report.py — 수집 결과 검증용 요약 txt 저장

[목적]
  매일 아침 자동수집(run_morning_auto.bat)이 끝난 뒤, 사람이 빠르게
  "수집이 정상적으로 됐는지" 확인할 수 있는 요약 txt 를 별도 폴더에 저장한다.
  research_agent.py / supervisor.py 는 일절 수정하지 않는다 — 결과 파일만 읽는다.

[저장 위치]
  collection_check/ 폴더 (없으면 자동 생성)
    파일명: 수집점검_YYYY-MM-DD_HHMM.txt

[내용]
  1) 판정(정상/주의/실패) + 한 줄 사유
  2) 수집 통계: 총 기사 수, 태그 분포(원문/요약본/AI복구/실패), 원문 비율
  3) force_scores 요약: 종목 수, 수급출처 분포(krx/naver/결측), 시장지수
  4) 전체 헤드라인 목록

[사용법]
  python collection_report.py                 # 오늘자 최신 세션 자동탐지(없으면 전체 최신)
  python collection_report.py --session DIR    # 세션 직접 지정
  python collection_report.py --latest         # output 전체에서 최신(날짜 무관)
"""

import os
import sys
import re
import json
import argparse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
CHECK_DIR = os.path.join(HERE, "collection_check")

# research_agent.parse_articles_from_text 와 동일한 기사 헤더 패턴
#   [임의텍스트] (🟢|🔵|🟡|🔴) [태그] 제목
_ARTICLE_RE = re.compile(
    r"\[[^\]]+\]\s*(\U0001F7E2|\U0001F535|\U0001F7E1|\U0001F534)\s*\[([^\]]+)\]\s*(.*)"
)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _scan_dir_for_sessions(base, today, today_only):
    """base 폴더에서 01_broad 가 있는 세션 후보 [(mtime, path), ...] 수집."""
    cands = []
    if not os.path.isdir(base):
        return cands
    for name in os.listdir(base):
        if name.startswith("_") or name == "__pycache__":
            continue
        sess = os.path.join(base, name)
        if not os.path.isdir(sess):
            continue
        broad = os.path.join(sess, "01_broad_collection.md")
        if not os.path.isfile(broad):
            continue
        if today_only and not name.startswith(today):
            continue
        cands.append((os.path.getmtime(broad), sess))
    return cands


def _latest_session(today_only=False):
    """
    최신 세션 폴더 경로. 1순위 output/, 못 찾으면 output/_archive/ 도 탐색.
    (수집 직후 점검이 정상 경로지만, 세션이 막 _archive 로 이동된 타이밍도 대비)
    """
    today = datetime.now().strftime("%Y-%m-%d")
    cands = _scan_dir_for_sessions(OUTPUT_DIR, today, today_only)
    if not cands:
        cands = _scan_dir_for_sessions(os.path.join(OUTPUT_DIR, "_archive"),
                                       today, today_only)
    if not cands:
        return None
    cands.sort(reverse=True)
    return cands[0][1]


def parse_broad(session_dir):
    """01_broad_collection.md 파싱 → dict(헤드라인/태그분포/메타)."""
    path = os.path.join(session_dir, "01_broad_collection.md")
    result = {"exists": False, "total": 0, "tags": {}, "headlines": [],
              "meta_lines": [], "collected_at": ""}
    if not os.path.isfile(path):
        return result
    result["exists"] = True
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        return result
    lines = text.splitlines()
    for ln in lines[:25]:
        s = ln.strip()
        if s.startswith("#") and "광역 수집 데이터" in s:
            m = re.search(r"—\s*(.+)$", s)
            if m:
                result["collected_at"] = m.group(1).strip()
        elif s.startswith("- "):
            result["meta_lines"].append(s[2:].strip())
    for ln in lines:
        m = _ARTICLE_RE.search(ln)
        if not m:
            continue
        tag = m.group(2).strip()
        title = (m.group(3) or "").strip()
        result["tags"][tag] = result["tags"].get(tag, 0) + 1
        result["headlines"].append((tag, title))
    result["total"] = len(result["headlines"])
    return result


def parse_force(session_dir):
    """force_scores.json 요약 → dict(종목수/수급출처분포/시장)."""
    path = os.path.join(session_dir, "force_scores.json")
    res = {"exists": False, "tickers": 0, "src": {}, "markets": [], "generated_at": ""}
    if not os.path.isfile(path):
        return res
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            d = json.load(f)
    except Exception:
        return res
    res["exists"] = True
    res["generated_at"] = d.get("generated_at", "")
    ts = d.get("tickers", []) or []
    res["tickers"] = len(ts)
    for t in ts:
        src = (t.get("detail") or {}).get("supply_source")
        key = str(src) if src else "결측"
        res["src"][key] = res["src"].get(key, 0) + 1
    for m in (d.get("markets", []) or []):
        res["markets"].append({
            "market": m.get("market"),
            "force": m.get("force_score"),
            "rsi": (m.get("detail") or {}).get("rsi"),
        })
    return res


def verdict(broad, force):
    """수집 건강 판정 → (라벨, 사유)."""
    n = broad["total"]
    if not broad["exists"]:
        return "실패", "01_broad_collection.md 가 없음 (수집 자체가 안 됨)"
    if n == 0:
        return "실패", "수집 기사 0건 — 네트워크/프록시 차단 또는 전 소스 실패 의심"
    if n < 10:
        return "주의", f"수집 {n}건 (10건 미만) — 부분 실패 가능, 본문 확인 권장"
    won = broad["tags"].get("원문", 0)
    ratio = (won / n * 100) if n else 0
    notes = []
    if ratio < 30:
        notes.append(f"원문 비율 {ratio:.0f}% 낮음(요약/복구 위주)")
    fail = broad["tags"].get("실패", 0)
    if fail > n * 0.3:
        notes.append(f"실패 태그 {fail}건 많음")
    if force["exists"] and force["tickers"] > 0 and force["src"].get("krx", 0) == 0:
        notes.append("force 수급 KRX 인증 0건(naver/결측) — KRX 로그인 확인 권장")
    if notes:
        return "주의", "; ".join(notes)
    return "정상", f"수집 {n}건, 원문 비율 {ratio:.0f}%, 양호"


def build_report(session_dir, broad, force, vd):
    label, reason = vd
    name = os.path.basename(session_dir.rstrip("\\/"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bar = "=" * 60
    L = []
    L.append(bar)
    L.append(f" 수집 점검 리포트   [판정: {label}]")
    L.append(bar)
    L.append(f"  점검 시각   : {now} KST")
    L.append(f"  세션 폴더   : {name}")
    L.append(f"  수집 시각   : {broad.get('collected_at') or '(미상)'}")
    L.append(f"  판정 사유   : {reason}")
    L.append("")
    L.append("-" * 60)
    L.append("[1] 수집 통계")
    L.append("-" * 60)
    L.append(f"  총 기사 수  : {broad['total']} 건")
    if broad["tags"]:
        L.append("  태그 분포   :")
        order = ["원문", "요약본", "AI복구", "실패"]
        seen = set()
        for tg in order:
            if tg in broad["tags"]:
                L.append(f"      - {tg:6s}: {broad['tags'][tg]} 건")
                seen.add(tg)
        for tg, c in sorted(broad["tags"].items(), key=lambda x: -x[1]):
            if tg not in seen:
                L.append(f"      - {tg:6s}: {c} 건")
        won = broad["tags"].get("원문", 0)
        if broad["total"]:
            L.append(f"  원문 비율   : {won / broad['total'] * 100:.0f}%  "
                     f"(높을수록 본문 풍부 = 좋음)")
    if broad["meta_lines"]:
        L.append("  수집 메타   :")
        for ml in broad["meta_lines"][:8]:
            L.append(f"      - {ml}")
    L.append("")
    L.append("-" * 60)
    L.append("[2] 세력강도(force_scores) 요약")
    L.append("-" * 60)
    if not force["exists"]:
        L.append("  force_scores.json 없음 — watch_and_analyze 미동작 또는 분석 전")
    else:
        L.append(f"  생성 시각   : {force['generated_at'] or '(미상)'}")
        L.append(f"  종목 수     : {force['tickers']} 개")
        if force["src"]:
            parts = []
            for k in ("krx", "naver", "결측"):
                if k in force["src"]:
                    parts.append(f"{k} {force['src'][k]}")
            for k, v in force["src"].items():
                if k not in ("krx", "naver", "결측"):
                    parts.append(f"{k} {v}")
            L.append(f"  수급 출처   : {', '.join(parts)}  "
                     f"(krx=KRX인증 정밀 / naver=폴백 / 결측=수급없음)")
        for m in force["markets"]:
            L.append(f"  시장 {str(m['market']):6s}: force {m['force']}  (RSI {m['rsi']})")
    L.append("")
    L.append("-" * 60)
    L.append(f"[3] 수집 헤드라인 전체 ({broad['total']}건)")
    L.append("-" * 60)
    if broad["headlines"]:
        for i, (tag, title) in enumerate(broad["headlines"], 1):
            L.append(f"  {i:3d}. [{tag}] {title if title else '(제목 없음)'}")
    else:
        L.append("  (헤드라인 없음)")
    L.append("")
    L.append(bar)
    L.append(" 참고: 검증용 요약입니다. 전체 원문은 세션 폴더의")
    L.append("       01_broad_collection.md / 02_deep_collection.md 를 보세요.")
    L.append(bar)
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="수집 결과 검증용 요약 txt 저장")
    ap.add_argument("--session", default="", help="세션 폴더 직접 지정")
    ap.add_argument("--latest", action="store_true", help="output 전체 최신(날짜 무관)")
    args = ap.parse_args()

    if args.session and os.path.isdir(args.session):
        sess = args.session
    elif args.latest:
        sess = _latest_session(today_only=False)
    else:
        sess = _latest_session(today_only=True) or _latest_session(today_only=False)

    os.makedirs(CHECK_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    out_path = os.path.join(CHECK_DIR, f"수집점검_{stamp}.txt")

    if not sess:
        report = (f"[판정: 실패] {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                  f"오늘자 세션 폴더가 없습니다. 수집이 실행되지 않았거나 "
                  f"세션 생성 전에 점검이 돌았습니다.\n")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"[collection_report] 판정=실패(세션없음) 저장: {out_path}")
        sys.exit(3)

    broad = parse_broad(sess)
    force = parse_force(sess)
    vd = verdict(broad, force)
    report = build_report(sess, broad, force, vd)

    tmp = out_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(report)
        os.replace(tmp, out_path)
    except Exception as e:
        print(f"[collection_report] 저장 실패: {type(e).__name__}: {e}")
        sys.exit(1)

    print(f"[collection_report] 판정={vd[0]} | 기사 {broad['total']}건 | "
          f"force {force['tickers']}종목 | 저장: {out_path}")


if __name__ == "__main__":
    main()
