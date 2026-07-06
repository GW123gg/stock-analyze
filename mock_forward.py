#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mock_forward.py ─ 분석 완료(REPORT_DONE) 리포트를 '모의투자 Cowork' 폴더로 전달하는 워처

[목적]
  매일 아침 자동 분석 또는 수동 분석이 끝나면(= 세션에 REPORT_DONE.flag 가 생기면),
  그 리포트를 별도의 '모의투자용 Claude Cowork' 가 읽을 폴더로 자동 복사한다.
  그 모의투자 Cowork 는 전달받은 분석(predictions.json + 03_final_report.md)을 바탕으로
  '가상 모의투자(페이퍼 트레이딩)' 를 운용한다. (실제 주문 아님)

[독립 모듈 원칙]
  - 기존 파일(research_agent.py / watch_and_send.py 등)을 수정하지 않는다(import 만 재사용).
  - supervisor.py 의 one_cycle 에 step 하나(scan_once 호출)만 추가한다.
  - 메일 발송/아카이브와 무관하게 동작한다(메일 off 여도 전달은 됨). 단 watch_and_send 가
    발송 성공 후 세션을 _archive 로 '옮기기 전' 에 복사해야 하므로, supervisor 에서 메일
    step '이전' 에 호출한다.

[설정 — mock_forward_config.txt (BASE_DIR, 저장 시 ~30초 내 자동 반영, 재시작 불필요)]
  enabled = 1            # 1=전달함, 0=전달 안 함 (on/off 스위치)
  folder  = C:\\Users\\USER\\Desktop\\mock_invest_inbox   # 떨어뜨릴 폴더(없으면 자동 생성)
  files   = predictions.json,03_final_report.md,03_final_report.html   # (선택) 전달 파일 목록

[동작]
  output/ 의 세션 중 REPORT_DONE.flag 가 있고 아직 전달 안 한 세션을 찾아,
  enabled=1 이면 folder/<세션명>/ 아래로 지정 파일을 복사하고 folder/latest.json 갱신.
  mock_forward_index.json 으로 영구 중복방지(재부팅/재시작 후에도 같은 세션은 1회만 전달).

[실행]
  python mock_forward.py     # 1회 스캔(테스트). 평소엔 supervisor 가 매 사이클 호출.
"""

import os
import re
import sys
import json
import shutil
from datetime import datetime

# Windows 콘솔 UTF-8 (cp949 깨짐 방지)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR  = os.path.join(BASE_DIR, "output")
LOG_DIR     = os.path.join(BASE_DIR, "logs")
CONFIG_FILE = os.path.join(BASE_DIR, "mock_forward_config.txt")
INDEX_FILE  = os.path.join(BASE_DIR, "mock_forward_index.json")
GUIDE_FILE  = os.path.join(BASE_DIR, "mock_invest_cowork_guide.md")
os.makedirs(LOG_DIR, exist_ok=True)

# 설정에 files 가 없을 때 전달하는 기본 파일
DEFAULT_FILES = ["predictions.json", "03_final_report.md", "03_final_report.html"]


def log(msg):
    """진행 로그. 콘솔은 ASCII 태그([mock_forward]) + 한글(2바이트, cp949 OK). 이모지 금지."""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [mock_forward] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(LOG_DIR, "mock_forward.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# =====================================================================
# 설정
# =====================================================================
def load_config() -> dict:
    """mock_forward_config.txt 파싱 → {enabled: bool, folder: str, files: list}.
    파일이 없거나 비면 enabled=False(안전 기본값)."""
    cfg = {"enabled": False, "folder": "", "files": list(DEFAULT_FILES)}
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
        files = kv.get("files", "").strip()
        if files:
            cfg["files"] = [x.strip() for x in files.split(",") if x.strip()]
    except Exception as e:
        log(f"설정 읽기 실패: {type(e).__name__}: {e}")
    return cfg


# =====================================================================
# 영구 중복방지 인덱스 (watch_and_send 의 sent_index.json 패턴 미러)
# =====================================================================
def _load_index() -> dict:
    if not os.path.exists(INDEX_FILE):
        return {}
    try:
        with open(INDEX_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        try:
            os.replace(INDEX_FILE, INDEX_FILE + ".corrupt")
        except Exception:
            pass
        return {}


def _mark_forwarded(name: str, extra: dict = None):
    idx = _load_index()
    rec = {"at": datetime.now().isoformat()}
    if extra:
        rec.update(extra)
    idx[name] = rec
    tmp = INDEX_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False, indent=2)
        os.replace(tmp, INDEX_FILE)
    except Exception as e:
        log(f"인덱스 기록 실패: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _already_forwarded(name: str) -> bool:
    return name in _load_index()


# =====================================================================
# 전달
# =====================================================================
def _ensure_html(sess: str):
    """03_final_report.html 이 없으면 research_agent.render_report_html 로 생성(있으면 무시).
    research_agent import 는 무거우므로 '필요할 때만' 지연 import."""
    if os.path.exists(os.path.join(sess, "03_final_report.html")):
        return
    try:
        import research_agent as ra
        ra.render_report_html(sess)
    except Exception as e:
        log(f"html 생성 시도 실패(무시): {type(e).__name__}: {e}")


def _copy_guide_once(folder: str):
    """모의투자 Cowork 가이드(mock_invest_cowork_guide.md)를 전달 폴더 루트에 1회 복사
    (이미 있으면 무시). 모의투자 Cowork 가 데이터 옆에서 지시문을 바로 찾도록."""
    if not os.path.isfile(GUIDE_FILE):
        return
    dest = os.path.join(folder, "mock_invest_cowork_guide.md")
    if os.path.exists(dest):
        return
    try:
        shutil.copy2(GUIDE_FILE, dest)
        log(f"가이드 복사: {dest}")
    except Exception as e:
        log(f"가이드 복사 실패(무시): {type(e).__name__}: {e}")


def _forward_session(sess: str, name: str, folder: str, files: list) -> bool:
    """세션의 지정 파일을 folder/<name>/ 로 복사하고 folder/latest.json 을 갱신. 성공 시 True."""
    if "03_final_report.html" in files:
        _ensure_html(sess)

    dest_dir = os.path.join(folder, name)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception as e:
        log(f"대상 폴더 생성 실패({folder}): {type(e).__name__}: {e}")
        return False

    copied = []
    for fn in files:
        src = os.path.join(sess, fn)
        if not os.path.isfile(src):
            continue
        try:
            shutil.copy2(src, os.path.join(dest_dir, fn))
            copied.append(fn)
        except Exception as e:
            log(f"복사 실패 {fn}: {type(e).__name__}: {e}")

    if not copied:
        log(f"전달할 파일 없음(스킵): {name}")
        return False

    # 모의투자 Cowork 가 '가장 최근'을 쉽게 찾도록 latest.json 포인터 갱신(원자적)
    m = re.match(r"(\d{4}-\d{2}-\d{2})", name)
    latest = {
        "session": name,
        "date": m.group(1) if m else "",
        "forwarded_at": datetime.now().isoformat(),
        "folder": name,
        "files": copied,
    }
    tmp = os.path.join(folder, "latest.json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(latest, f, ensure_ascii=False, indent=2)
        os.replace(tmp, os.path.join(folder, "latest.json"))
    except Exception as e:
        log(f"latest.json 갱신 실패(무시): {type(e).__name__}: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass

    log(f"전달 완료: {name} -> {dest_dir} ({', '.join(copied)})")
    return True


def scan_once() -> int:
    """output/ 세션 중 REPORT_DONE.flag 가 있고 아직 전달 안 한 것을 folder 로 전달.
    enabled=0 또는 folder 미설정이면 아무것도 안 함. 전달한 개수 반환."""
    cfg = load_config()
    if not cfg["enabled"]:
        return 0
    folder = cfg["folder"]
    if not folder:
        log("enabled=1 이나 folder 미설정 — 전달 보류(mock_forward_config.txt 의 folder 확인)")
        return 0
    if not os.path.isdir(OUTPUT_DIR):
        return 0
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception as e:
        log(f"전달 폴더 생성 실패({folder}): {type(e).__name__}: {e}")
        return 0

    _copy_guide_once(folder)

    n = 0
    for name in sorted(os.listdir(OUTPUT_DIR)):
        if name.startswith("_"):              # _archive / _designtest 등 제외
            continue
        sess = os.path.join(OUTPUT_DIR, name)
        if not os.path.isdir(sess):
            continue
        if not os.path.exists(os.path.join(sess, "REPORT_DONE.flag")):
            continue
        if _already_forwarded(name):          # 영구 중복방지(재시작/메일 off 여도 1회만)
            continue
        try:
            if _forward_session(sess, name, folder, cfg["files"]):
                _mark_forwarded(name, {"folder": folder, "files": cfg["files"]})
                n += 1
            elif not any(os.path.isfile(os.path.join(sess, fn)) for fn in cfg["files"]):
                # 전달할 소스 파일이 아예 없는 세션은 1회만 표시(매 사이클 재시도/로그 스팸 방지).
                # 파일이 있는데 복사가 일시 실패한 경우는 표시 안 해서 다음 사이클에 재시도된다.
                _mark_forwarded(name, {"folder": folder, "skipped": "no_source_files"})
        except Exception as e:
            log(f"전달 예외({name}): {type(e).__name__}: {e}")
    return n


if __name__ == "__main__":
    c = scan_once()
    log(f"1회 스캔 종료 (전달 {c}건)")
