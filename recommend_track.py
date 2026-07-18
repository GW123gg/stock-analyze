#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recommend_track.py — 추천 종목 추적기: 메일 발송된 추천을 '명시적 이력'으로 저장 + 회고 폴더로 전달

[왜 필요한가]
  - 2시(precollect)·6:30 수집기(fsc_collect/flow_collect)는 watch_tickers '풀 전체'를 모은다.
    추천 종목이 풀 안이면 자동 포함되지만, 중소형 추천(풀 미포함)은 빠진다.
  - 또 회고(3:30) Cowork 가 '무엇을 추천했나'를 retro_dataset 안에서만 보는데, 명시적 목록이 없다.
  → 이 모듈이 predictions.json 들에서 추천 종목을 뽑아 (a) recommended_history.json(이력) +
    (b) recommended_universe.txt(최근 추천 종목코드 → fsc/flow 가 watch 와 합산해 수집) 로 저장하고,
    회고 폴더(stock_retro/inbox, stock_retro_manual)로 복사한다. '메일 발송 후' 호출된다.

[설계] 독립 모듈, 기존 파일 무수정, 멱등(매번 전체 재집계), 콘솔 ASCII([rec]), 원자적 저장.
"""
import os
import sys
import json
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
ARCHIVE_DIR = os.path.join(OUTPUT_DIR, "_archive")
HISTORY_FILE = os.path.join(HERE, "recommended_history.json")
UNIVERSE_FILE = os.path.join(HERE, "recommended_universe.txt")
LOG_FILE = os.path.join(HERE, "logs", "recommend_track.log")
RETRO_CONFIG = os.path.join(HERE, "retro_config.txt")
MANUAL_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "stock_retro_manual")

UNIVERSE_DAYS = 60   # 최근 60일 내 추천 종목만 수집 유니버스에 합산

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg):
    line = "[%s] [rec] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _iter_pred_files():
    for base in (OUTPUT_DIR, ARCHIVE_DIR):
        if not os.path.isdir(base):
            continue
        try:
            names = os.listdir(base)
        except Exception:
            continue
        for name in names:
            if name.startswith("_") or name == "__pycache__":
                continue
            p = os.path.join(base, name, "predictions.json")
            if os.path.isfile(p):
                yield name[:10], p


def collect():
    """모든 predictions.json → 종목별 추천 이력 집계. {ticker: {...}} 반환."""
    rec = {}
    for date, path in _iter_pred_files():
        try:
            d = json.load(open(path, encoding="utf-8", errors="replace"))
        except Exception:
            continue
        for kind, key in (("pick", "picks"), ("short", "shorts")):
            for it in (d.get(key) or []):
                code = str(it.get("ticker") or "").strip()
                if not (code.isdigit() and len(code) == 6):
                    continue
                r = rec.setdefault(code, {"ticker": code, "name": it.get("name") or code,
                                          "dates": [], "tags": [], "kinds": [], "count": 0})
                if it.get("name"):
                    r["name"] = it.get("name")
                if date not in r["dates"]:
                    r["dates"].append(date)
                tag = it.get("tag") or ("숏" if kind == "short" else "")
                if tag and tag not in r["tags"]:
                    r["tags"].append(tag)
                if kind not in r["kinds"]:
                    r["kinds"].append(kind)
                r["count"] += 1
    for code, r in rec.items():
        r["dates"].sort()
        r["first_rec"] = r["dates"][0] if r["dates"] else ""
        r["last_rec"] = r["dates"][-1] if r["dates"] else ""
    return rec


from common import atomic_write_text as _atomic_write  # 원자적 텍스트 저장(common.py 통합)


def _retro_folder():
    folder = ""
    if os.path.isfile(RETRO_CONFIG):
        try:
            for ln in open(RETRO_CONFIG, encoding="utf-8"):
                ln = ln.strip()
                if ln.lower().startswith("folder") and "=" in ln:
                    folder = ln.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return folder


def track():
    rec = collect()
    if not rec:
        log("predictions.json 없음 — 추천 이력 비어있음")
        return 0
    items = sorted(rec.values(), key=lambda r: (r.get("last_rec", ""), r.get("count", 0)), reverse=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "what": "지금까지 메일로 추천한 종목 이력(최신 추천일 순). 회고·수급추적용 명시 목록.",
        "n_tickers": len(items),
        "tickers": items,
    }
    try:
        _atomic_write(HISTORY_FILE, json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception as e:
        log("history 저장 실패: %s" % e)

    # 최근 UNIVERSE_DAYS 내 추천 종목코드 → fsc/flow 가 watch 와 합산해 수집
    cutoff = (datetime.now() - timedelta(days=UNIVERSE_DAYS)).strftime("%Y-%m-%d")
    recent = [r for r in items if r.get("last_rec", "") >= cutoff]
    lines = ["# 최근 %d일 내 추천 종목(메일 발송분). fsc/flow 가 watch_tickers 와 합산 수집." % UNIVERSE_DAYS,
             "# recommend_track.py 가 자동 생성 — 직접 편집 불필요."]
    for r in recent:
        lines.append("%s # %s" % (r["ticker"], r.get("name", "")))
    try:
        _atomic_write(UNIVERSE_FILE, "\n".join(lines) + "\n")
    except Exception as e:
        log("universe 저장 실패: %s" % e)

    # 회고 폴더로 복사(자동 inbox + 수동 루트) — 회고 Cowork 가 '추천 이력'을 바로 보게
    import shutil
    dests = []
    rf = _retro_folder()
    if rf:
        dests.append(os.path.join(rf, "inbox"))
    dests.append(MANUAL_DIR)
    for dd in dests:
        try:
            os.makedirs(dd, exist_ok=True)
            dst = os.path.join(dd, "recommended_history.json")
            shutil.copy2(HISTORY_FILE, dst)
            # #A7 사본 검증(+1회 재복사): inbox 사본은 Cowork 세션 되돌림 경합으로 손상된 전례가
            # 있는 경로다(retro_dataset 07-06·07-12). 크기·해시 불일치면 한 번 다시 복사하고 기록.
            import hashlib

            def _sha(p):
                h = hashlib.sha256()
                with open(p, "rb") as f:
                    for ch in iter(lambda: f.read(65536), b""):
                        h.update(ch)
                return h.hexdigest()[:16]
            if os.path.getsize(HISTORY_FILE) != os.path.getsize(dst) or _sha(HISTORY_FILE) != _sha(dst):
                log("사본 검증 실패(%s) — 재복사" % dd)
                shutil.copy2(HISTORY_FILE, dst)
                if os.path.getsize(HISTORY_FILE) != os.path.getsize(dst):
                    log("재복사도 불일치(%s) — 수동 확인 필요" % dd)
        except Exception as e:
            log("복사 실패(%s): %s" % (dd, e))

    today = datetime.now().strftime("%Y-%m-%d")
    today_recs = [r["ticker"] + "(" + r.get("name", "") + ")" for r in items if today in r.get("dates", [])]
    log("추천 이력 갱신: 누적 %d종목 / 최근%d일 수집유니버스 %d종목 / 오늘(%s) 추천: %s"
        % (len(items), UNIVERSE_DAYS, len(recent), today, ", ".join(today_recs) or "(없음)"))
    return len(items)


def load_recommended_codes():
    """fsc_collect/flow_collect 가 watch 와 합산할 '최근 추천 종목' (code, name) 리스트. 파일 없으면 []."""
    out = []
    if not os.path.isfile(UNIVERSE_FILE):
        return out
    try:
        for ln in open(UNIVERSE_FILE, encoding="utf-8"):
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            code = ln.split("#")[0].strip()
            if code.isdigit() and len(code) == 6:
                name = ln.split("#", 1)[1].strip() if "#" in ln else ""
                out.append((code, name))
    except Exception:
        pass
    return out


if __name__ == "__main__":
    track()
