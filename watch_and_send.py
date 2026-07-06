#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch_and_send.py ─ 리포트 완료 신호를 감시해 즉시 메일을 발송하는 워처
                    (발송 방식: Google Apps Script 웹앱 HTTPS POST)

[동작]
  output/ 아래 세션 폴더들을 주기적으로(기본 30초) 확인하다가
  REPORT_DONE.flag 가 있는 폴더를 발견하면, 그 즉시
    1) 03_final_report.html(없으면 .md→html 변환, 그래도 없으면 .md 원문)을 본문으로
    2) Apps Script 웹앱 URL 로 HTTPS POST (제목/본문/수신자/비밀키)
    3) 응답 ok:true 면 성공 → 세션 폴더를 _archive 로 이동 + REPORT_DONE.flag 제거
       + sent_index.json 에 발송 기록(재부팅 후에도 중복 발송 방지)

[Cowork와의 약속]
  Cowork는 리포트 작성을 끝내면:
      python research_agent.py report-done --session "세션폴더"
  를 호출해 REPORT_DONE.flag 를 남긴다. (발송은 하지 않는다)
  → 이 워처가 그 신호를 보고 Apps Script 로 발송한다.

[설정 — appscript_config.txt (BASE_DIR)]
  appscript_url    = https://script.google.com/macros/s/.../exec
  appscript_secret = (Apps Script 의 SECRET_TOKEN 과 동일한 값)
  (mail_config.txt 의 to 값을 수신자로 사용)

[실행]
  python watch_and_send.py                 # 기본 30초 간격, 무한 감시
  python watch_and_send.py --interval 15   # 15초 간격
  python watch_and_send.py --once          # 1회만 스캔하고 종료(테스트용)

[변경 이력]
  v2: Gmail API(OAuth, 7일 만료) → Apps Script 웹앱(만료 없음)로 발송 전환.
      첨부 제거(HTML 본문만). sent_index.json 영구 중복방지 추가.

[원칙]
  research_agent.py 는 수정하지 않는다(report-done / render_report_html 재사용만).
"""

import os
import re
import sys
import json
import time
import shutil
import argparse
import urllib.request
import urllib.error
from datetime import datetime

# Windows 콘솔 UTF-8 (한글 깨짐/ cp949 문제 방지)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR  = os.path.join(BASE_DIR, "output")
ARCHIVE_DIR = os.path.join(OUTPUT_DIR, "_archive")
LOG_DIR     = os.path.join(BASE_DIR, "logs")
APPSCRIPT_CONFIG = os.path.join(BASE_DIR, "appscript_config.txt")
MAIL_CONFIG      = os.path.join(BASE_DIR, "mail_config.txt")
SENT_INDEX  = os.path.join(BASE_DIR, "sent_index.json")
LOCK_FILE   = os.path.join(BASE_DIR, "watch_and_send.lock")
os.makedirs(LOG_DIR, exist_ok=True)

POST_TIMEOUT_SEC = 60

# 이번 프로세스에서 처리(시도)한 폴더 (보조 — 영구기록은 sent_index.json)
_handled = set()


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(LOG_DIR, "watch.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# =====================================================================
# 설정 로드
# =====================================================================
def _parse_kv_file(path) -> dict:
    """key = value 형식 파일 파싱(# 주석/빈 줄 무시). 소문자 키."""
    cfg = {}
    if not os.path.exists(path):
        return cfg
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"\s*([A-Za-z_]+)\s*[=:]\s*(.+)", line)
                if m:
                    cfg[m.group(1).strip().lower()] = m.group(2).strip()
    except Exception as e:
        log(f"⚠️ 설정 파일 읽기 실패({os.path.basename(path)}): {e}")
    return cfg


def load_appscript_config() -> tuple:
    """(url, secret) 반환. 없거나 비면 ('','')."""
    cfg = _parse_kv_file(APPSCRIPT_CONFIG)
    url = cfg.get("appscript_url", "").strip()
    secret = cfg.get("appscript_secret", "").strip()
    return url, secret


def load_recipients() -> str:
    """mail_config.txt 의 to 값(콤마구분 그대로) 반환."""
    cfg = _parse_kv_file(MAIL_CONFIG)
    return cfg.get("to", "").strip()


# =====================================================================
# 영구 중복방지: sent_index.json
# =====================================================================
def load_sent_index() -> dict:
    """발송 완료 세션 기록 로드. 손상 시 .corrupt 격리 후 빈 dict."""
    if not os.path.exists(SENT_INDEX):
        return {}
    try:
        with open(SENT_INDEX, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError as e:
        corrupt = SENT_INDEX + ".corrupt"
        try:
            if os.path.exists(corrupt):
                os.remove(corrupt)
            os.replace(SENT_INDEX, corrupt)
            log(f"⚠️ sent_index.json 손상 → '{os.path.basename(corrupt)}' 로 격리: {e}")
        except Exception:
            pass
        return {}
    except Exception as e:
        log(f"⚠️ sent_index.json 로드 실패: {e}")
        return {}


def mark_sent(session_name: str, extra: dict = None):
    """발송 성공 기록을 원자적으로 저장(temp→os.replace)."""
    idx = load_sent_index()
    rec = {"at": datetime.now().isoformat()}
    if extra:
        rec.update(extra)
    idx[session_name] = rec
    tmp = SENT_INDEX + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SENT_INDEX)
    except Exception as e:
        log(f"⚠️ sent_index.json 기록 실패: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def already_sent(session_name: str) -> bool:
    return session_name in load_sent_index()


# =====================================================================
# 본문 / 제목 / 수신자
# =====================================================================
def _strip_non_bmp(text: str) -> str:
    """
    BMP(기본다국어평면, U+0000~U+FFFF) 밖의 4바이트 문자(대부분의 이모지:
    🔥 🌐 🚨 🌱 🔔 🎯 등)를 제거한다. Apps Script/Gmail 전송 시 surrogate pair
    (4바이트 UTF-8)가 깨지는 문제(������)를 원천 방어한다.
    ⚠(U+26A0)·⚡(U+26A1) 등 BMP 내 기호(3바이트)는 정상 전송되므로 보존.
    """
    if not text:
        return text
    return "".join(ch for ch in text if ord(ch) < 0x10000)


def get_html_body(sess: str) -> str:
    """
    발송 본문 확보. 우선순위:
      1) 03_final_report.html (이미 변환됨)
      2) research_agent.render_report_html(sess) 로 생성 후 읽기
      3) 03_final_report.md 원문(HTML 변환 실패 시 그대로 본문)
    실패 시 "" 반환.
    """
    html_path = os.path.join(sess, "03_final_report.html")
    if os.path.exists(html_path):
        try:
            with open(html_path, encoding="utf-8") as f:
                t = f.read().strip()
            if t:
                return t
        except Exception as e:
            log(f"⚠️ HTML 읽기 실패: {e}")

    # render_report_html 재사용 시도 (research_agent.py 무수정 import)
    try:
        import research_agent as ra
        p = ra.render_report_html(sess)
        if p and os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                t = f.read().strip()
            if t:
                return t
    except Exception as e:
        log(f"⚠️ render_report_html 재사용 실패(무시): {type(e).__name__}: {e}")

    # md 원문 fallback
    md_path = os.path.join(sess, "03_final_report.md")
    if os.path.exists(md_path):
        try:
            with open(md_path, encoding="utf-8") as f:
                t = f.read().strip()
            if t:
                # md 원문을 최소한의 <pre> 로 감싸 가독성 확보
                import html as _h
                return ("<pre style=\"white-space:pre-wrap;font-family:"
                        "'Apple SD Gothic Neo','Malgun Gothic',sans-serif;"
                        "font-size:14px;line-height:1.6;\">"
                        + _h.escape(t) + "</pre>")
        except Exception as e:
            log(f"⚠️ md 읽기 실패: {e}")
    return ""


def parse_subject(session_name: str) -> str:
    """폴더명(2026-05-24_151326)에서 날짜 파싱 → 제목. 실패 시 오늘 날짜."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", session_name)
    date_str = m.group(1) if m else datetime.now().strftime("%Y-%m-%d")
    return f"[데일리 리서치] {date_str} 모멘텀 투자 리포트"


# =====================================================================
# Apps Script POST
# =====================================================================
def post_to_appscript(url: str, payload: dict, timeout: int = POST_TIMEOUT_SEC) -> tuple:
    """
    Apps Script 웹앱에 JSON POST. Returns (ok: bool, info: str).
    requests 가 있으면 사용(리다이렉트 자동), 없으면 urllib.
    Apps Script 는 POST→302→googleusercontent 리다이렉트 후 JSON 반환하므로
    리다이렉트를 따라가야 한다(urllib/requests 둘 다 기본 따라감).
    """
    # ensure_ascii=True: 한글·이모지(4바이트 surrogate 포함)를 모두 \uXXXX 로 이스케이프해
    # 순수 ASCII 바이트로 전송한다. 전송 중 4바이트 UTF-8 문자가 깨지는 문제(이모지 �)를
    # 원천 차단하며, Apps Script 의 JSON.parse 가 원래 문자(이모지 포함)로 정확히 복원한다.
    data = json.dumps(payload, ensure_ascii=True).encode("ascii")
    headers = {"Content-Type": "application/json; charset=utf-8"}

    # 1) requests 우선
    try:
        import requests
        try:
            r = requests.post(url, data=data, headers=headers,
                              timeout=timeout, allow_redirects=True)
            body = r.text
            return _interpret_response(body, r.status_code)
        except Exception as e:
            return False, f"requests_error:{type(e).__name__}:{e}"
    except ImportError:
        pass

    # 2) urllib fallback
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.getcode()
        body = raw.decode("utf-8", errors="replace")
        return _interpret_response(body, status)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return _interpret_response(body, e.code)
    except Exception as e:
        return False, f"urllib_error:{type(e).__name__}:{e}"


def _interpret_response(body: str, status: int) -> tuple:
    """응답 본문(JSON 기대)을 해석해 (ok, info)."""
    body = (body or "").strip()
    try:
        obj = json.loads(body)
        if isinstance(obj, dict) and obj.get("ok") is True:
            return True, f"ok (status={status})"
        err = obj.get("error") if isinstance(obj, dict) else None
        return False, f"ok=false err={err} status={status} body={body[:200]}"
    except Exception:
        # JSON 이 아니면(로그인 페이지 HTML 등) 실패로 간주
        return False, f"non_json status={status} body={body[:200]}"


# =====================================================================
# 세션 보관 이동
# =====================================================================
def archive_session(sess: str) -> str:
    """세션 폴더를 output/_archive/ 로 이동. 실패 시 원본 경로 반환."""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    base = os.path.basename(sess.rstrip("/\\"))
    dest = os.path.join(ARCHIVE_DIR, base)
    if os.path.exists(dest):
        dest = dest + "_" + datetime.now().strftime("%H%M%S")
    try:
        shutil.move(sess, dest)
        return dest
    except Exception as e:
        log(f"⚠️ 보관 이동 실패(원본 유지): {e}")
        return sess


# =====================================================================
# 스캔 / 발송
# =====================================================================
def scan_once(url: str, secret: str, recipients: str) -> int:
    """output/ 세션을 훑어 REPORT_DONE.flag 발송. 처리한(성공) 개수 반환."""
    if not os.path.isdir(OUTPUT_DIR):
        return 0
    sent = 0
    for name in sorted(os.listdir(OUTPUT_DIR)):
        if name.startswith("_"):              # _archive 등 제외
            continue
        sess = os.path.join(OUTPUT_DIR, name)
        if not os.path.isdir(sess):
            continue
        flag = os.path.join(sess, "REPORT_DONE.flag")
        if not os.path.exists(flag):
            continue

        # ── 영구 중복방지: 이미 발송된 세션이면 남은 flag 만 정리 ──
        if already_sent(name):
            try:
                os.remove(flag)
                log(f"♻️ 이미 발송된 세션의 잔여 flag 제거: {name}")
            except Exception:
                pass
            continue

        if sess in _handled:                  # 이번 프로세스에서 이미 시도
            continue

        log(f"🔔 발송 신호 감지 → {name}")
        _handled.add(sess)

        # 본문 확보
        html_body = get_html_body(sess)
        if not html_body:
            _handled.discard(sess)
            log(f"❌ 본문 없음(03_final_report.html/.md 모두 비어있음): {name} — 다음 스캔 재시도")
            continue

        subject = parse_subject(name)
        payload = {
            "secret": secret,
            "to": recipients,
            # 발송 직전 4바이트 이모지 제거 (메일 깨짐 방어). Cowork가 실수로 넣어도 안전.
            "subject": _strip_non_bmp(subject),
            "htmlBody": _strip_non_bmp(html_body),
        }

        ok, info = post_to_appscript(url, payload)
        if ok:
            log(f"✅ 발송 성공: {name} → {recipients}")
            mark_sent(name, {"to": recipients, "subject": subject})
            # flag 제거
            try:
                os.remove(flag)
            except Exception:
                pass
            # 보관 이동
            dest = archive_session(sess)
            log(f"📦 세션 보관: {os.path.basename(dest)}")
            sent += 1
        else:
            _handled.discard(sess)            # 다음 스캔 재시도 허용
            log(f"❌ 발송 실패: {name} | {info}")
    return sent


# =====================================================================
# 단일 인스턴스 락
# =====================================================================
def acquire_lock() -> bool:
    """워처 중복 실행 방지. 이미 살아있는 PID 가 잡고 있으면 False."""
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE, encoding="utf-8") as f:
                old = f.read().strip()
            if old.isdigit() and _pid_alive(int(old)):
                log(f"⚠️ 이미 워처가 실행 중(PID {old}) — 이 인스턴스는 종료")
                return False
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return True  # 락 실패해도 동작은 계속


def release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        if sys.platform.startswith("win"):
            out = os.popen(f'tasklist /fi "PID eq {pid}" /nh').read()
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:
        return False


# =====================================================================
# 메인
# =====================================================================
def main():
    ap = argparse.ArgumentParser(
        description="리포트 완료 신호 감시 → Apps Script 웹앱으로 자동 메일 발송")
    ap.add_argument("--interval", type=int, default=30, help="스캔 간격(초), 기본 30")
    ap.add_argument("--once", action="store_true", help="1회만 스캔 후 종료(테스트)")
    args = ap.parse_args()

    log(f"=== watch_and_send (Apps Script 발송) 시작 "
        f"(간격 {args.interval}s, output={OUTPUT_DIR}) ===")

    url, secret = load_appscript_config()
    recipients = load_recipients()

    # 설정 검증 — 없으면 발송 시도하지 않고 graceful 종료/대기
    cfg_ok = True
    if not url or not secret:
        log("🛑 appscript_config.txt 미설정 (appscript_url / appscript_secret 필요) "
            "— 발송 불가. Apps Script 배포 후 설정 파일을 채우세요.")
        cfg_ok = False
    if not recipients:
        log("🛑 mail_config.txt 의 to (수신자) 미설정 — 발송 불가.")
        cfg_ok = False

    if args.once:
        n = scan_once(url, secret, recipients) if cfg_ok else 0
        log(f"=== 1회 스캔 종료 (발송 {n}건) ===")
        return

    if not acquire_lock():
        return
    try:
        # 설정이 비어 있어도 워처는 살아서 대기(설정 채워지면 다음 스캔부터 발송).
        last_cfg_check = 0
        while True:
            # 30초마다 설정 재로딩(배포 후 설정 채우면 자동 반영)
            now = time.time()
            if now - last_cfg_check > 25:
                url, secret = load_appscript_config()
                recipients = load_recipients()
                cfg_ok = bool(url and secret and recipients)
                last_cfg_check = now
            if cfg_ok:
                scan_once(url, secret, recipients)
            time.sleep(max(5, args.interval))
    except KeyboardInterrupt:
        log("=== 사용자 중단(Ctrl+C) — 워처 종료 ===")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
