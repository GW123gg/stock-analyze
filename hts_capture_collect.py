# -*- coding: utf-8 -*-
"""
hts_capture_collect.py — 카이로스(HTS) 화면 캡처 수집기 [v10.3 신규]

[왜] 2026-07-26 `data.krx.co.kr` 이 403 으로 차단되면서(브라우저로도 동일 → 헤더·pykrx 문제가
  아님) **공매도 잔고·대차잔고는 대체 소스가 아예 없어졌다**(FSC 에 해당 API 없음 — 404 실측,
  short.krx 도 동반 차단). 원격 노트북에 상시 로그인된 카이로스 HTS 화면을 캡처해 그 공백을 메운다.

[구조 — pull]
  이 PC 가 tailnet 사설망으로 노트북의 캡처 에이전트를 호출한다(노트북이 보내는 push 아님).
  이유: 캡처 시각을 이 PC 가 통제해야 06:30 룩어헤드 규율에 맞출 수 있고, 실패 시 즉시 재요청이
  되며, 이 PC 의 기존 Tailscale Funnel 설정을 건드리지 않는다.
  ★Funnel(공개 인터넷) 금지 — 공개 엔드포인트로 두면 외부인이 가짜 스크린샷을 주입할 수 있고
   그 이미지가 실제 발송 메일의 추천 근거가 된다.

[★자기검증 — 이 수집기의 핵심]
  HTS 자동화의 지배적 실패는 '접속 실패'가 아니라 **엉뚱한 화면이 조용히 찍히는 것**이다
  (로그인 세션 만료 → 로그인창, 공지 팝업, 화면 전환 지연, 창 위치·DPI 변경).
  이때 캡처는 "성공"하고 파일도 멀쩡하며 분석가만 엉뚱한 숫자를 읽는다. 그래서:
    · 에이전트가 화면 제목/화면번호 영역을 함께 찍고 marker_text 로 보고
    · 이 수집기가 기대 화면번호·화면명과 대조 → 불일치면 그 캡처를 **폐기**
    · 캡처 시각(KST)·조회 기준일자를 함께 기록해 stale 을 잡는다
  night_futures_collect 의 session_guess 와 같은 설계다.

[★실패는 소리나게] 오늘 KRX 가 403 인데 pykrx 는 예외 없이 rows=0 을 돌려줘 '차단'이 '데이터
  없음'으로 조용히 둔갑하는 것을 확인했다. 여기서는 status 를 명시하고, ok 가 아니면 값을
  **아예 산출하지 않는다**(0 으로 채우지 않는다 — retro_label dist_disc_count 와 같은 계약).

[출력] 세션폴더/hts_capture.json + 세션폴더/hts_captures/<screen>_<시각>.png
  ※ 이미지는 로그(세션)에 그대로 보존된다 — 회고가 나중에 '그날 분석가가 본 화면'을 재확인할 수 있다.

[설정] hts_capture_config.txt (gitignore 대상 — 토큰 포함). 없으면 무동작 exit 0.
    agent_url=http://100.x.x.x:8710
    token=<공유 시크릿>
    screens=short_lend,foreign_inst,investor_daily,night_fut_investor
    timeout=45

[사용법]
  python hts_capture_collect.py                 # 오늘 세션에 캡처 수집
  python hts_capture_collect.py --check         # 에이전트 연결·인증만 점검(저장 안 함)
  python hts_capture_collect.py --screens short_lend --out tmp.json
"""
import os
import sys
import json
import time
import base64
import hashlib
import argparse
import logging
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import requests
except Exception:
    requests = None

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "hts_capture_config.txt")
OUTPUT_DIR = os.path.join(HERE, "output")

logging.basicConfig(level=logging.INFO, format="[htscap] %(message)s")
log = logging.getLogger("htscap")

from common import save_json_atomic, resolve_session


# ── 캡처 대상 카탈로그 ────────────────────────────────────────────────────────
# 화면번호는 미래에셋 공식 신구맵핑표(Maps.pdf / kairosmapping4.pdf)의 **카이로스(신) 열**에서
# 확인한 값이다. 표 헤더가 "맵스플러스 | 카이로스" 이므로 오른쪽 열이 현행 번호다.
# expect 는 marker 검증에 쓰는 부분일치 키워드(화면 제목에 반드시 있어야 하는 말).
SCREENS = {
    # ── P0: KRX 차단으로 대체 소스가 전혀 없는 것 ──
    "short_lend": {
        "no": "0231", "name": "관심종목 신용/공매도/대차 현황",
        "expect": ["관심종목", "공매도"],
        "why": "공매도잔고+대차잔고+신용을 관심종목(워치리스트) 전체로 한 장에. KRX 차단분의 유일한 대체",
        "fields": ["공매도잔고", "공매도비중", "대차잔고", "신용잔고"],
    },
    "foreign_inst": {
        "no": "0261", "name": "관심종목 외국인/기관 매매현황",
        "expect": ["관심종목", "외국인"],
        "why": "종목별 외인/기관 순매수를 워치리스트 전체로 한 장에(메일 '전일 투자자별 수급' 표 입력)",
        "fields": ["외국인순매수", "기관순매수"],
    },
    # ── P1: 시장 전체 수급(전일 확정치 — 06:30 룩어헤드 적합) ──
    "investor_daily": {
        "no": "0254", "name": "투자자 일별 매매현황",
        "expect": ["투자자", "일별"],
        "why": "시장 전체 개인/외국인/기관 일별 순매수. flow_collect 공백 대체",
        "fields": ["개인", "외국인", "기관"],
    },
    # ── P1: 야간선물 — 무료 대체 소스가 없는 고유 정보 ──
    "night_fut_investor": {
        "no": "9314", "name": "야간선물 투자자 일별 매매현황",
        "expect": ["야간선물", "투자자"],
        "why": "★간밤 야간선물에서 외국인이 무엇을 했나. night_futures_collect 는 가격만 본다",
        "fields": ["외국인", "기관", "개인"],
    },
    "night_fut_quote": {
        "no": "9308", "name": "야간선물옵션 종합시세",
        "expect": ["야간선물"],
        "why": "야간 선물 종합시세(가격·미결제). investing 폴백의 교차검증용",
        "fields": ["현재가", "등락률", "미결제"],
    },
    # ── P2: 국면 보조 ──
    "basis": {
        "no": "0313", "name": "선물 베이시스/스프레드",
        "expect": ["베이시스"],
        "why": "베이시스 = 프로그램 차익 매수/매도 압력의 선행 지표",
        "fields": ["베이시스", "이론가", "괴리"],
    },
    "program_daily": {
        "no": "0273", "name": "프로그램매매 일별현황",
        "expect": ["프로그램"],
        "why": "차익/비차익 프로그램 순매수 — 외국인 수급과 교차",
        "fields": ["차익", "비차익"],
    },
    "broker_3d": {
        "no": "0214", "name": "전체거래원 연속 3일 순매매상위종목",
        "expect": ["거래원"],
        "why": "3일 연속 순매수 창구 = 지속 매집 신호(단발 매수와 구분)",
        "fields": ["종목", "거래원", "순매수"],
    },
    "short_top": {
        "no": "0235", "name": "공매도상위종목분석",
        "expect": ["공매도"],
        "why": "공매도 급증 상위 — 숏 후보 발굴([6.7] 숏 3중정렬 입력)",
        "fields": ["종목", "공매도거래량", "비중"],
    },
    "lend_top": {
        "no": "0238", "name": "대차잔고 상위종목 분석",
        "expect": ["대차"],
        "why": "대차잔고 급증 = 공매도 대기물량. 잔고 자체보다 '증가'가 신호",
        "fields": ["종목", "대차잔고", "증감"],
    },
    "afterhours": {
        "no": "0147", "name": "시간외단일가 종목등락현황",
        "expect": ["시간외"],
        "why": "전일 시간외 흐름 = 당일 갭 예측 보조",
        "fields": ["종목", "등락률", "거래량"],
    },
}

DEFAULT_SCREENS = ["short_lend", "foreign_inst", "investor_daily", "night_fut_investor"]

# 캡처가 이 시간보다 오래됐으면 stale (에이전트가 캐시를 돌려주는 사고 방지)
MAX_CAPTURE_AGE_MIN = 30


def load_config(path=CONFIG):
    """hts_capture_config.txt → dict. 없으면 {} (무동작). ★토큰은 절대 로그에 찍지 않는다."""
    cfg = {}
    if not os.path.exists(path):
        return cfg
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip().lower()] = v.strip()
    except Exception as e:
        log.warning("설정 읽기 실패(무시): %s", type(e).__name__)
    return cfg


def kst_now():
    """이 PC 는 KST 로컬이라고 가정하되, 표기를 명시해 회고가 시점을 오해하지 않게 한다."""
    return datetime.now()


def _verify(screen_key, meta, png_bytes):
    """캡처 1건 검증 → (status, reason). ★여기서 통과 못 하면 값을 산출하지 않는다."""
    spec = SCREENS.get(screen_key) or {}
    if not png_bytes:
        return "agent_error", "이미지 없음"
    if not png_bytes.startswith(b"\x89PNG"):
        return "agent_error", "PNG 시그니처 아님"

    # 무결성: 에이전트가 보고한 sha256 과 실제 바이트 대조(전송 중 손상·중간 교체 탐지)
    want = (meta.get("sha256") or "").lower()
    got = hashlib.sha256(png_bytes).hexdigest()
    if want and want != got:
        return "agent_error", "sha256 불일치"

    # ★화면 검증 마커 — 엉뚱한 화면(로그인창·팝업·이전 화면)이 조용히 통과하는 것을 막는다
    marker = str(meta.get("marker_text") or "")
    if not marker:
        return "marker_mismatch", "marker_text 없음(에이전트가 화면 제목을 못 읽음)"
    no = spec.get("no") or ""
    expects = spec.get("expect") or []
    hit_no = bool(no and no in marker)
    hit_kw = all(k in marker for k in expects) if expects else False
    if not (hit_no or hit_kw):
        return "marker_mismatch", "기대 화면(%s %s) 아님 — 실제 marker=%r" % (
            no, spec.get("name"), marker[:60])

    # 신선도: 에이전트가 캐시를 돌려주는 사고 방지
    ts = meta.get("captured_at_kst")
    if ts:
        try:
            t = datetime.strptime(str(ts)[:16], "%Y-%m-%d %H:%M")
            age = (kst_now() - t).total_seconds() / 60.0
            if age > MAX_CAPTURE_AGE_MIN:
                return "stale", "캡처가 %.0f분 전(임계 %d분)" % (age, MAX_CAPTURE_AGE_MIN)
        except Exception:
            pass
    return "ok", ""


def fetch_one(agent_url, token, screen_key, timeout=45):
    """에이전트에서 화면 1장 취득 → (status, meta, png_bytes, reason)."""
    if requests is None:
        return "agent_error", {}, b"", "requests 미설치"
    url = agent_url.rstrip("/") + "/capture"
    try:
        r = requests.get(url, params={"screen": screen_key},
                         headers={"X-Capture-Token": token}, timeout=timeout)
    except Exception as e:
        # 노트북 꺼짐·tailnet 미연결 등 — 조용한 결측이 아니라 명시적 상태로 남긴다
        return "unreachable", {}, b"", "%s" % type(e).__name__
    if r.status_code == 401 or r.status_code == 403:
        return "agent_error", {}, b"", "인증 거부 HTTP %s" % r.status_code
    if r.status_code != 200:
        return "agent_error", {}, b"", "HTTP %s" % r.status_code
    try:
        j = r.json()
    except Exception:
        return "agent_error", {}, b"", "JSON 아님"
    if not j.get("ok", True):
        return "agent_error", j.get("meta") or {}, b"", str(j.get("error"))[:80]
    meta = j.get("meta") or {}
    try:
        png = base64.b64decode(j.get("png_b64") or "")
    except Exception:
        return "agent_error", meta, b"", "base64 디코드 실패"
    st, why = _verify(screen_key, meta, png)
    return st, meta, (png if st == "ok" else b""), why


def collect(session_dir, agent_url, token, screens, timeout=45, save_images=True):
    """화면 목록 수집 → payload dict. 부분 실패해도 계속한다(전부 실패해도 exit 0)."""
    img_dir = os.path.join(session_dir, "hts_captures")
    if save_images:
        try:
            os.makedirs(img_dir, exist_ok=True)
        except Exception as e:
            log.warning("이미지 폴더 생성 실패: %s", type(e).__name__)
            save_images = False

    results = []
    for key in screens:
        spec = SCREENS.get(key)
        if not spec:
            log.warning("알 수 없는 화면 키: %s (건너뜀)", key)
            results.append({"screen": key, "status": "disabled", "reason": "미등록 화면 키"})
            continue
        st, meta, png, why = fetch_one(agent_url, token, key, timeout=timeout)
        rec = {
            "screen": key,
            "screen_no": spec["no"],
            "screen_name": spec["name"],
            "status": st,
            "reason": why or None,
            "captured_at_kst": meta.get("captured_at_kst"),
            "asof_date": meta.get("asof_date"),      # 화면이 표시하는 조회 기준일자
            "marker_text": (str(meta.get("marker_text"))[:120] if meta.get("marker_text") else None),
            "resolution": meta.get("resolution"),
            "sha256": meta.get("sha256"),
            "image": None,
            "fields_expected": spec.get("fields"),
        }
        if st == "ok" and png and save_images:
            stamp = (str(meta.get("captured_at_kst") or kst_now().strftime("%Y-%m-%d %H:%M"))
                     .replace("-", "").replace(":", "").replace(" ", "_"))[:13]
            fn = "%s_%s.png" % (key, stamp)
            fp = os.path.join(img_dir, fn)
            try:
                with open(fp, "wb") as f:
                    f.write(png)
                # 세션 상대경로로 남긴다(세션 폴더를 옮겨도 깨지지 않게)
                rec["image"] = os.path.join("hts_captures", fn).replace("\\", "/")
                rec["bytes"] = len(png)
            except Exception as e:
                rec["status"] = "agent_error"
                rec["reason"] = "이미지 저장 실패: %s" % type(e).__name__
        log.info("%-20s %-14s %s", key, rec["status"], rec["reason"] or "")
        results.append(rec)

    ok_n = sum(1 for r in results if r["status"] == "ok")
    payload = {
        "generated_at": kst_now().strftime("%Y-%m-%d %H:%M:%S"),
        "tz": "KST",
        "source": "kairos_hts_capture",
        "agent_reachable": any(r["status"] != "unreachable" for r in results),
        "n_requested": len(results),
        "n_ok": ok_n,
        "coverage": round(ok_n / len(results), 3) if results else 0.0,
        "captures": results,
        "note": ("status != ok 인 화면은 '데이터 없음'이 아니라 '확인 불가'다 — 값을 0 으로 "
                 "채우지 말고 그 화면 근거를 아예 쓰지 마라. 이미지에서 읽은 값은 [캡처] 로 "
                 "표기하고 API 값과 섞지 마라(단위·시점이 다르다)."),
    }
    return payload


def main():
    ap = argparse.ArgumentParser(description="카이로스 HTS 화면 캡처 수집 → 세션 hts_capture.json")
    ap.add_argument("--session", default=None, help="세션 폴더(미지정 시 오늘 세션 자동탐지)")
    ap.add_argument("--screens", default=None, help="쉼표구분 화면 키(미지정 시 기본 4종)")
    ap.add_argument("--out", default=None, help="출력 JSON 경로(테스트용)")
    ap.add_argument("--check", action="store_true", help="연결·인증만 점검(저장 안 함)")
    ap.add_argument("--list", action="store_true", help="수집 가능한 화면 카탈로그 출력")
    args = ap.parse_args()

    if args.list:
        print("%-20s %-6s %-34s %s" % ("KEY", "화면", "화면명", "용도"))
        for k, v in SCREENS.items():
            print("%-20s %-6s %-34s %s" % (k, v["no"], v["name"], v["why"][:60]))
        return 0

    cfg = load_config()
    agent_url = cfg.get("agent_url") or ""
    token = cfg.get("token") or ""
    if not agent_url or not token:
        # 키 없으면 즉시·정상 종료(mirae_collect 와 같은 규약) — 노트북 세팅 전에도 파이프라인 무중단
        log.info("hts_capture_config.txt 없음/불완전 → 무동작 종료(노트북 세팅 전 정상)")
        return 0

    screens = [s.strip() for s in (args.screens or cfg.get("screens") or
                                   ",".join(DEFAULT_SCREENS)).split(",") if s.strip()]
    try:
        timeout = int(cfg.get("timeout") or 45)
    except Exception:
        timeout = 45

    if args.check:
        st, meta, png, why = fetch_one(agent_url, token, screens[0], timeout=timeout)
        log.info("--check %s → %s %s (이미지 %dB)", screens[0], st, why or "", len(png))
        return 0

    session = args.session or resolve_session(OUTPUT_DIR)
    if not session or not os.path.isdir(session):
        log.warning("세션 폴더를 찾을 수 없음 → 종료")
        return 0

    payload = collect(session, agent_url, token, screens, timeout=timeout)
    out = args.out or os.path.join(session, "hts_capture.json")
    try:
        save_json_atomic(out, payload)
        log.info("저장: %s (ok %d/%d)", out, payload["n_ok"], payload["n_requested"])
    except Exception as e:
        log.warning("저장 실패: %s", type(e).__name__)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        # 파이프라인 무중단 원칙 — 실패해도 exit 0
        log.warning("예기치 못한 오류(무시): %s: %s", type(e).__name__, e)
        sys.exit(0)
