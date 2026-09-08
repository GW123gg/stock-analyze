#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
retro_forward.py ─ 회고분석(PART C) Cowork 와의 양방향 브리지

[목적]
  새벽(RETRO_TIME, 기본 03:30) 호스트가 retro_label.py 로 '학습 데이터셋'을 만든 뒤,
  이 스크립트가 그 데이터셋을 바탕화면의 '회고분석 Cowork' 폴더(inbox)로 밀어 넣고(push),
  회고 Cowork 가 분석을 끝내면(outbox/RETRO_DONE.flag) 그 산출물 중
  'PART_A_추가지시.md'(기존 분석 Cowork 에게 줄 추가 지시)를 호스트로 되가져온다(scan_back).
  => stock_research/retro_feedback.md 로 저장 → 아침 분석 Cowork 가 [0.5] 에서 읽어 반영.

[독립 모듈 원칙] mock_forward.py 와 동일.
  - 기존 파일을 수정하지 않는다(import 만 재사용).
  - supervisor.py 의 one_cycle 에 scan_back() 호출 1줄, run_retro_pipeline 에서 --push 1회.
  - 비밀키 미취급. 콘솔 ASCII 태그([retro_forward]) + 한글. 이모지 금지. 원자적 저장.

[설정 — retro_config.txt (BASE_DIR, 저장 시 ~30초 내 자동 반영, 재시작 불필요)]
  enabled = 1                                  # 1=동작, 0=중지(데이터셋만 만들고 전달 안 함)
  folder  = C:\\Users\\USER\\Desktop\\stock_retro   # 회고 Cowork 작업 폴더(없으면 자동 생성)

[폴더 구조 (folder 기준)]
  folder/회고분석_지시사항.md   ← 호스트가 1회 복사(회고 Cowork 가 따르는 지시문)
  folder/inbox/                 ← 호스트가 push. 데이터셋은 **날짜가 붙은 불변 파일명**으로 드롭한다
                                  (retro_dataset_<날짜>.json/csv, scorecard_<날짜>.md) + RETRO_GO.flag + latest.json.
                                  회고는 latest.json 의 roles(역할→파일명)로 이번 회차 파일을 찾는다.
                                  ※ 고정 이름 덮어쓰기는 2026-07-06·07-12 사본 손상(잘림·NUL패딩)의 원인이라 폐지.
  folder/outbox/                ← 회고 Cowork 가 작성 (회고리포트_*.md, PART_A_추가지시.md, RETRO_DONE.flag)

[실행]
  python retro_forward.py --push        # 데이터셋을 inbox 로 전달 + RETRO_GO.flag (보통 supervisor 가 새벽 1회)
  python retro_forward.py --scan-back   # outbox 의 피드백을 호스트로 회수 (보통 supervisor 가 매 사이클)
  (인자 없으면 --push 후 --scan-back 둘 다 1회)
"""

import os
import re
import sys
import csv
import glob
import json
import shutil
import hashlib
import argparse
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
LOG_DIR     = os.path.join(BASE_DIR, "logs")
CONFIG_FILE = os.path.join(BASE_DIR, "retro_config.txt")
GUIDE_FILE  = os.path.join(BASE_DIR, "회고분석_지시사항.md")
# 호스트가 inbox 로 밀어 넣을 데이터셋(없으면 그 파일만 건너뜀)
# ※ 실제 드롭 파일명은 회차마다 날짜가 붙는다(#P0-1 불변 드롭): retro_dataset_2026-07-17.json
#   회고는 inbox/latest.json 의 roles/files 로 실제 파일명을 찾는다.
PUSH_FILES  = ["retro_dataset.json", "retro_dataset.csv", "scorecard.md", "market_calls.json",
               # v11.20: 전야(23시) 콜 원장·성적 — ★market_calls(아침 콜)와 **합산 금지**.
               #   같은 거래일을 밤·아침 두 콜이 겨냥하므로 합치면 이중계상 + 유사복제
               #   표본으로 CI 가 거짓으로 좁아진다. 비교 전용(회고지시 §3.16).
               "night_calls.jsonl", "night_scorecard.md"]
# ★v11.6(호스트 감사 loop-gaps-4): 세션 산출 장중 실측 — '살 수 있었나 M/N' 성적표가
#   세션에서만 계산되고 증발하던 경로를 회고로 연결. 라벨·사후검증 전용(파일 안에
#   retro_use=forbidden_as_pre_feature 마커 — pre_* 피처 사용 금지는 회고분석_지시사항 참조).
SESSION_PUSH_FILES = ["intraday_review.json"]
KEEP_VERSIONS = 7          # 역할별 보관 회차 수(오래된 버전 자동 정리)
# 회고 Cowork 가 outbox 에 작성하는 '기존 분석 Cowork 용 추가 지시' → 호스트가 이 이름으로 회수
FEEDBACK_NAME = "PART_A_추가지시.md"
FEEDBACK_DEST = os.path.join(BASE_DIR, "retro_feedback.md")
REPORTS_DIR   = os.path.join(BASE_DIR, "retro_reports")
os.makedirs(LOG_DIR, exist_ok=True)


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [retro_forward] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(LOG_DIR, "retro_forward.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config() -> dict:
    """retro_config.txt → {enabled: bool, folder: str}. 없거나 비면 enabled=False(안전 기본값)."""
    cfg = {"enabled": False, "folder": ""}
    if not os.path.exists(CONFIG_FILE):
        return cfg
    try:
        kv = {}
        with open(CONFIG_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"\s*([A-Za-z_]+)\s*[=:]\s*(.+)", line)
                if m:
                    kv[m.group(1).strip().lower()] = m.group(2).strip()
        cfg["enabled"] = str(kv.get("enabled", "0")).strip().lower() in ("1", "true", "on", "yes", "y")
        cfg["folder"] = kv.get("folder", "").strip().strip('"').strip("'")
    except Exception as e:
        log(f"설정 읽기 실패: {type(e).__name__}: {e}")
    return cfg


from common import atomic_write_text as _atomic_write  # 원자적 텍스트 저장(common.py 통합)


def _latest_session_file(fname: str):
    """output/ 최신 세션(날짜 역순)의 fname 경로. 없으면 None(★v11.6 — _archive 제외)."""
    out_dir = os.path.join(BASE_DIR, "output")
    if not os.path.isdir(out_dir):
        return None
    try:
        names = sorted(os.listdir(out_dir), reverse=True)
    except Exception:
        return None
    for name in names:
        if name.startswith("_"):
            continue
        p = os.path.join(out_dir, name, fname)
        if os.path.isfile(p):
            return p
    return None


def _atomic_copy(src: str, dest: str):
    """반쪽 복사 방지: dest.tmp 로 복사 후 os.replace. 회고 Cowork 가 복사 중 파일을 읽지 않게."""
    tmp = dest + ".tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, dest)


def _verify_copy(src: str, dest: str) -> bool:
    """복사 검증(#R4, 회고 07-06 손상 대응): 원본-사본 바이트 수·sha256 일치 확인.
    2026-07-06 회고에 JSON 뒤잘림(24KB)·CSV 널패딩 사본이 도착한 재발 방지 — 검증 실패 시 호출부가 재복사."""
    try:
        if os.path.getsize(src) != os.path.getsize(dest):
            return False
        hs, hd = _sha256(src), _sha256(dest)
        return (hs is not None) and (hs == hd)
    except Exception:
        return False


def _validate_dataset_json(path: str):
    """retro_dataset.json 무결성: 파싱되고 len(rows)==n_rows 면 행수 반환, 아니면 None.
    파일이 없으면 None(없는 건 무결성 실패가 아니라 '없음' — 호출부에서 구분)."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        rows = d.get("rows", [])
        meta = d.get("n_rows")
        if meta is not None and len(rows) != meta:
            return None
        return len(rows)
    except Exception:
        return None


def _csv_rowcount(path: str):
    """CSV '논리적' 데이터행 수(헤더 제외). csv.reader 로 세서 따옴표 안 개행(thesis)에도 정확. 실패 시 None."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return sum(1 for _ in csv.reader(f)) - 1
    except Exception:
        return None


def _sha256(path: str):
    """파일 sha256(앞 16자). 회고가 무결성 대조용. 실패 시 None."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except Exception:
        return None


# 02:00 종가수집(+그 외)에서 회고 폴더로 넘길 수집결과 파일들(존재하는 것만 복사)
COLLECTION_FILES = ["fsc_prices.json", "flow_data.json", "deriv_sentiment.json", "ecos_macro.json",
                    "market_caution.json", "overheat.json", "short.json", "force_scores.json",
                    "analyst_reco.json", "yahoo_news.json", "naver_stock_news.json",
                    "media_rss.json", "gdelt_news.json", "fundamentals.json", "daily_status.json"]


def _latest_path(name: str):
    """루트 → output/<날짜>/ 에서 name 의 '최신'(세션날짜 우선, 동률 mtime) 경로. 없으면 None."""
    cands = []
    for pat in (os.path.join(BASE_DIR, name), os.path.join(BASE_DIR, "output", "*", name)):
        for p in glob.glob(pat):
            if "_discarded" in p:
                continue
            parent = os.path.basename(os.path.dirname(p))
            datekey = parent[:10] if (len(parent) >= 10 and parent[4:5] == "-") else ""
            try:
                mt = os.path.getmtime(p)
            except Exception:
                mt = 0
            cands.append((datekey, mt, p))
    if not cands:
        return None
    cands.sort(reverse=True)
    return cands[0][2]


def push_collections() -> int:
    """02:00 종가수집(+그 외) 결과를 회고 폴더 `collections/<날짜>/` 로 복사한다 — 회고 Cowork 가 종가·수급·
    거시·파생·뉴스를 과거 추천과 교차참조하도록. enabled=0/folder 미설정이면 스킵. 복사 파일 수 반환."""
    cfg = load_config()
    if not cfg["enabled"] or not cfg["folder"]:
        return 0
    today = datetime.now().strftime("%Y-%m-%d")
    dest_dir = os.path.join(cfg["folder"], "collections", today)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception as e:
        log(f"collections 폴더 생성 실패({dest_dir}): {type(e).__name__}: {e}")
        return 0
    n, names = 0, []
    for name in COLLECTION_FILES:
        src = _latest_path(name)
        if not src:
            continue
        try:
            _atomic_copy(src, os.path.join(dest_dir, name))
            n += 1
            names.append(name)
        except Exception as e:
            log(f"수집결과 복사 실패 {name}: {type(e).__name__}")
    if n:
        log(f"수집결과 복사: {dest_dir} ({n}개: {', '.join(names)})")
    else:
        log("복사할 수집결과 없음(아직 수집 전)")
    return n


def _copy_guide_once(folder: str):
    """회고분석_지시사항.md 를 folder 루트에 복사(원본이 더 최신이면 갱신)."""
    if not os.path.isfile(GUIDE_FILE):
        return
    dest = os.path.join(folder, "회고분석_지시사항.md")
    try:
        if (not os.path.exists(dest)) or os.path.getmtime(GUIDE_FILE) > os.path.getmtime(dest):
            shutil.copy2(GUIDE_FILE, dest)
            log(f"지시문 복사: {dest}")
    except Exception as e:
        log(f"지시문 복사 실패(무시): {type(e).__name__}: {e}")


def _maybe_refresh_scorecard(dataset_json: str, timeout_s: int = 900) -> dict:
    """★v11.41(호스트 감사 2026-09-08 — 원장 A65 재발 2회): 드롭될 scorecard.md 가 라벨(retro_dataset.json)보다
    낡으면 push 직전에 accuracy_tracker.py 를 1회 돌려 갱신한다(37회차 실측: scorecard 08:43 vs 라벨 18:04 =
    9시간 21분 낡음 → 회고가 아침 판 성적표로 저녁 라벨을 해석). 실패·타임아웃이면 낡은 판을 그대로 싣되
    latest.json 의 scorecard_refresh 에 사실을 남긴다(무음 실패 금지). 반환: {ran, reason, rc, ...}."""
    sc = os.path.join(BASE_DIR, "scorecard.md")
    info = {"ran": False, "reason": None, "rc": None,
            "dataset_mtime": None, "scorecard_mtime_before": None, "scorecard_mtime_after": None}

    def _iso(ts):
        try:
            return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None
        except Exception:
            return None
    try:
        ds_m = os.path.getmtime(dataset_json) if os.path.isfile(dataset_json) else None
        sc_m = os.path.getmtime(sc) if os.path.isfile(sc) else None
        info["dataset_mtime"], info["scorecard_mtime_before"] = _iso(ds_m), _iso(sc_m)
        if ds_m is None:
            info["reason"] = "no_dataset"
            return info
        if sc_m is not None and sc_m >= ds_m - 60:
            info["reason"] = "fresh"
            return info
        import subprocess
        log("scorecard.md 가 retro_dataset.json 보다 낡음(A65) — accuracy_tracker.py 1회 실행 후 push")
        r = subprocess.run([sys.executable, os.path.join(BASE_DIR, "accuracy_tracker.py")],
                           cwd=BASE_DIR, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout_s)
        info["ran"], info["rc"] = True, r.returncode
        sc_m2 = os.path.getmtime(sc) if os.path.isfile(sc) else None
        info["scorecard_mtime_after"] = _iso(sc_m2)
        info["reason"] = ("refreshed" if (sc_m2 and (sc_m is None or sc_m2 > sc_m)) else "ran_but_not_updated")
        log(f"scorecard 갱신 결과: {info['reason']} (rc={r.returncode})")
    except Exception as e:      # subprocess.TimeoutExpired 포함 — push 자체는 계속
        info["ran"] = info["ran"] or isinstance(e, getattr(__import__("subprocess"), "TimeoutExpired", ()))
        info["reason"] = f"error:{type(e).__name__}"
        log(f"scorecard 갱신 실패(낡은 판 그대로 전달): {type(e).__name__}: {e}")
    return info


def push(no_refresh: bool = False) -> bool:
    """retro_dataset.* + scorecard.md 를 folder/inbox/ 로 전달하고 RETRO_GO.flag + latest.json 작성.
    enabled=0/ folder 미설정이면 아무것도 안 함. 1개 이상 전달했으면 True.
    v11.41: scorecard.md 가 라벨보다 낡으면 accuracy_tracker 를 먼저 1회 실행(--no-refresh 로 생략)."""
    cfg = load_config()
    if not cfg["enabled"]:
        log("enabled=0 — push 보류")
        return False
    folder = cfg["folder"]
    if not folder:
        log("enabled=1 이나 folder 미설정 — push 보류(retro_config.txt 의 folder 확인)")
        return False
    inbox = os.path.join(folder, "inbox")
    outbox = os.path.join(folder, "outbox")
    try:
        os.makedirs(inbox, exist_ok=True)
        os.makedirs(outbox, exist_ok=True)
    except Exception as e:
        log(f"폴더 생성 실패({folder}): {type(e).__name__}: {e}")
        return False

    _copy_guide_once(folder)

    # 무결성 검증(P0): 핵심 데이터셋(retro_dataset.json)이 잘려 있으면 push 를 거부해
    # 회고 Cowork 가 깨진 데이터로 분석하지 않게 한다(이전 inbox 의 온전한 사본 유지).
    json_src = os.path.join(BASE_DIR, "retro_dataset.json")
    n_rows = _validate_dataset_json(json_src)
    if n_rows is None and os.path.isfile(json_src):
        log("retro_dataset.json 무결성 실패(파싱불가/행수불일치) — push 거부(이전 inbox 유지)")
        return False
    csv_src = os.path.join(BASE_DIR, "retro_dataset.csv")
    if n_rows is not None and os.path.isfile(csv_src):
        n_csv = _csv_rowcount(csv_src)
        if n_csv is not None and n_csv != n_rows:
            log(f"경고: csv 행수({n_csv}) != json 행수({n_rows}) — json 우선이나 csv 재생성 권장")

    # #P0-1 버저닝 드롭(불변 파일명) — 2026-07-06·07-12 손상의 근본 대책.
    #   두 사건 모두 '무정전 연속가동'이었고(이벤트로그 확인) 사본이 정확히 '이전 push 크기'까지
    #   잘리거나 NUL 패딩됐다 = 옛 크기를 아는 주체(열려있는 회고 Cowork 세션의 파일 되돌림)와
    #   호스트 push 가 같은 경로를 두고 경합했다는 뜻. 매 회차 '새 이름'으로 떨어뜨리면
    #   되돌림 대상 자체가 없어 경합이 성립하지 않는다. 회고는 latest.json 의 files 를 보고 읽는다.
    stamp = datetime.now().strftime("%Y-%m-%d")
    copied, name_map = [], {}
    # ★v11.41(A65): 낡은 scorecard 를 싣지 않는다 — 라벨보다 오래됐으면 여기서 1회 갱신
    _refresh = ({"ran": False, "reason": "no_refresh"} if no_refresh
                else _maybe_refresh_scorecard(json_src))
    # ★v11.6: 루트 파일 + 최신 세션의 장중 실측 파일을 같은 규약(버저닝·검증)으로 드롭
    _push_pairs = [(fn, os.path.join(BASE_DIR, fn)) for fn in PUSH_FILES]
    for fn in SESSION_PUSH_FILES:
        _sp = _latest_session_file(fn)
        if _sp:
            _push_pairs.append((fn, _sp))
    for fn, src in _push_pairs:
        if not os.path.isfile(src):
            continue
        stem, ext = os.path.splitext(fn)
        vname = f"{stem}_{stamp}{ext}"               # 예: retro_dataset_2026-07-17.json
        # 같은 날 두 번째 push 면 기존 파일을 덮어쓰게 되어 '불변 드롭'의 목적(경합 회피)이
        # 하루 안에서 무효가 된다 → 시각 접미사로 새 이름을 만든다(회고가 이미 열어둔 사본은 그대로 보존).
        if os.path.exists(os.path.join(inbox, vname)):
            vname = f"{stem}_{stamp}_{datetime.now().strftime('%H%M%S')}{ext}"
        try:
            dest = os.path.join(inbox, vname)
            _atomic_copy(src, dest)                      # 반쪽 복사 방지(.tmp -> rename)
            if not _verify_copy(src, dest):              # #R4 사본 검증(잘림·널패딩 감지) + 1회 재복사
                log(f"복사 검증 실패 {vname}(원본-사본 불일치) — 재복사 시도")
                _atomic_copy(src, dest)
                if not _verify_copy(src, dest):
                    log(f"재복사도 검증 실패 {vname} — 이 파일은 전달 목록에서 제외")
                    continue
            copied.append(vname)
            name_map[fn] = vname
        except Exception as e:
            log(f"복사 실패 {fn}: {type(e).__name__}: {e}")

    if not copied:
        log("전달할 데이터셋 없음(retro_label.py 를 먼저 실행) — push 스킵")
        return False
    # #R4: 핵심 데이터셋이 검증을 통과하지 못했으면 push 전체를 거부(이전 inbox 온전 사본 유지,
    # RETRO_GO 미발행) — 회고가 깨진 핵심 데이터로 분석하는 일 방지.
    if os.path.isfile(json_src) and "retro_dataset.json" not in name_map:
        log("핵심 retro_dataset.json 전달 실패 — push 중단(RETRO_GO 미발행, 이전 inbox 유지)")
        return False

    today = datetime.now().strftime("%Y-%m-%d")
    # #1 체크섬: 회고가 파일 무결성을 sha256·rowcount 로 대조할 수 있게(키=실제 드롭된 버전 파일명).
    checks = {}
    for orig, vname in name_map.items():
        p = os.path.join(inbox, vname)
        c = {"sha256": _sha256(p), "bytes": (os.path.getsize(p) if os.path.isfile(p) else None)}
        if orig == "retro_dataset.json":
            c["rowcount"] = n_rows
        elif orig == "retro_dataset.csv":
            c["rowcount"] = _csv_rowcount(p)
        checks[vname] = c
    latest = {"date": today, "pushed_at": datetime.now().isoformat(), "files": copied,
              # #P0-1: 회고가 '역할 → 실제 파일명'을 찾는 표. 파일명이 매 회차 달라지므로 이 표가 전거다.
              "roles": name_map,
              "checksums": checks,
              # v11.41(A65): scorecard 가 라벨과 같은 빈티지인지 — reason=fresh|refreshed 면 정상, 그 외는 낡은 판
              "scorecard_refresh": _refresh,
              "_note": ("파일명은 회차마다 날짜가 붙는다(불변 드롭 — 덮어쓰기 경합/되돌림 방지). "
                        "roles 로 실제 파일명을 찾고, checksums 로 무결성을 대조한 뒤 읽어라.")}
    try:
        _atomic_write(os.path.join(inbox, "latest.json"),
                      json.dumps(latest, ensure_ascii=False, indent=2))
    except Exception as e:
        log(f"latest.json 작성 실패(무시): {type(e).__name__}: {e}")
    # RETRO_GO.flag: 회고 Cowork 에게 '새 데이터 준비됨, 분석 시작' 신호
    try:
        _atomic_write(os.path.join(inbox, "RETRO_GO.flag"),
                      f"{datetime.now().isoformat()}\ndate={today}\nfiles={','.join(copied)}\n")
    except Exception as e:
        log(f"RETRO_GO.flag 작성 실패(무시): {type(e).__name__}: {e}")

    _prune_versions(inbox, keep=KEEP_VERSIONS)
    _drop_legacy_names(inbox, name_map)
    log(f"push 완료: {inbox} ({', '.join(copied)}) + RETRO_GO.flag")
    return True


def _drop_legacy_names(inbox: str, name_map: dict):
    """버저닝 이전의 고정 이름 사본(retro_dataset.json 등)을 제거한다.
    남겨두면 회고가 습관적으로 그 이름을 읽어 '낡은 회차 데이터'로 분석할 위험이 있다
    (이번 회차 파일은 버전명으로 이미 안전하게 드롭됨 — 원본은 BASE_DIR 에 그대로 있으니 무손실)."""
    # #A8: name_map(이번 회차 성공분)만 돌면 복사 실패한 역할의 낡은 고정이름이 영구 잔존 -> PUSH_FILES 전체 순회
    for orig in PUSH_FILES + SESSION_PUSH_FILES:
        legacy = os.path.join(inbox, orig)
        if not os.path.isfile(legacy):
            continue
        try:
            os.remove(legacy)
            log(f"구 고정이름 사본 제거(버저닝 전환): {orig}")
        except Exception:
            log(f"구 사본 제거 실패(무시) {orig}")


def _prune_versions(inbox: str, keep: int = 7):
    """버저닝 드롭이 무한 증식하지 않게 파일 역할별 최근 keep 개만 남긴다(오래된 것부터 삭제).
    latest.json 이 가리키는 현재 회차는 항상 최신이라 보존된다. 삭제 실패는 무시(무해)."""
    import re as _re
    for fn in PUSH_FILES + SESSION_PUSH_FILES:
        stem, ext = os.path.splitext(fn)
        pat = _re.compile(r"^%s_\d{4}-\d{2}-\d{2}%s$" % (_re.escape(stem), _re.escape(ext)))
        try:
            vs = sorted(n for n in os.listdir(inbox) if pat.match(n))
        except Exception:
            continue
        for old in vs[:-keep] if len(vs) > keep else []:
            try:
                os.remove(os.path.join(inbox, old))
                log(f"오래된 버전 정리: {old}")
            except Exception:
                pass


def scan_back() -> int:
    """folder/outbox/RETRO_DONE.flag 가 있으면 회고 Cowork 산출물을 호스트로 회수:
      - PART_A_추가지시.md → BASE_DIR/retro_feedback.md (아침 분석 Cowork 가 [0.5] 에서 읽음)
      - 회고리포트_*.md     → BASE_DIR/retro_reports/ 보관
    처리 후 RETRO_DONE.flag 를 소비(삭제)해 1회만 반영. 회수 건수 반환."""
    cfg = load_config()
    if not cfg["enabled"] or not cfg["folder"]:
        return 0
    outbox = os.path.join(cfg["folder"], "outbox")
    done_flag = os.path.join(outbox, "RETRO_DONE.flag")
    if not os.path.exists(done_flag):
        return 0

    n = 0
    # 1) 기존 분석 Cowork 용 추가 지시 회수(핵심)
    fb_src = os.path.join(outbox, FEEDBACK_NAME)
    if os.path.isfile(fb_src):
        try:
            txt = open(fb_src, encoding="utf-8", errors="replace").read()
            header = (f"<!-- 회고분석(PART C) Cowork 가 작성한 '기존 분석 Cowork 용 추가 지시'. "
                      f"호스트가 {datetime.now().strftime('%Y-%m-%d %H:%M')} 에 회수. "
                      f"아침 분석 Cowork 가 [0.5] 에서 읽는다. 사람이 검토 후 항구 규칙은 "
                      f"cowork_instructions.md 로 승격하라. -->\n\n")
            _atomic_write(FEEDBACK_DEST, header + txt)
            log(f"피드백 회수: {fb_src} -> {FEEDBACK_DEST}")
            n += 1
        except Exception as e:
            log(f"피드백 회수 실패: {type(e).__name__}: {e}")
    else:
        log("RETRO_DONE 인데 PART_A_추가지시.md 없음 — 리포트만 보관")

    # 2) 회고 리포트 보관(있으면)
    try:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        for fn in os.listdir(outbox):
            if fn.startswith("회고리포트") and fn.endswith(".md"):
                try:
                    shutil.copy2(os.path.join(outbox, fn), os.path.join(REPORTS_DIR, fn))
                except Exception:
                    pass
    except Exception as e:
        log(f"리포트 보관 실패(무시): {type(e).__name__}: {e}")

    # 3) 플래그 소비(claim-by-delete) — 다음 회차에 회고 Cowork 가 새로 만든다
    try:
        os.remove(done_flag)
    except Exception as e:
        log(f"RETRO_DONE.flag 삭제 실패(다음 사이클 재시도): {type(e).__name__}: {e}")
    return n


def main():
    ap = argparse.ArgumentParser(description="회고분석 Cowork 양방향 브리지")
    ap.add_argument("--push", action="store_true", help="데이터셋을 inbox 로 전달 + RETRO_GO.flag")
    ap.add_argument("--scan-back", action="store_true", help="outbox 피드백을 호스트로 회수")
    ap.add_argument("--push-collections", action="store_true",
                    help="수집결과(종가/수급/거시/파생/뉴스)를 회고 폴더 collections/<날짜>/ 로 복사")
    ap.add_argument("--no-refresh", action="store_true",
                    help="v11.41: push 전 scorecard.md 자동 갱신(accuracy_tracker 1회)을 생략")
    args = ap.parse_args()
    if args.push_collections:
        push_collections()
        return
    if not args.push and not args.scan_back:
        push(no_refresh=args.no_refresh)
        c = scan_back()
        log(f"기본 모드 종료 (회수 {c}건)")
        return
    if args.push:
        push(no_refresh=args.no_refresh)
    if args.scan_back:
        c = scan_back()
        log(f"scan-back 종료 (회수 {c}건)")


if __name__ == "__main__":
    main()
