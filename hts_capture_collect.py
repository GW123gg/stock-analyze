# -*- coding: utf-8 -*-
"""
hts_capture_collect.py — 카이로스(HTS) 화면 캡처 수집기 → 세션 hts_capture.json
  [v10.8 재작성 — 실제 에이전트 계약(포트 8788·captures 배열·variant·watchlist)에 맞춤]

[왜] 2026-07-26 `data.krx.co.kr` 403 차단으로 **공매도 잔고·대차잔고는 대체 소스가 전무**하다
  (FSC 에 해당 API 없음 — 404 실측, short.krx 도 동반 차단). 노트북에 상시 로그인된 카이로스
  화면을 캡처해 그 공백을 메운다.

[구조] pc21(여기)이 호출, 노트북(desktop-psk2gpr)이 피코(실물 USB HID)로 조작·캡처.
  전송은 Tailscale 테일넷 내부(공개 아님). 자세한 계약은 kairos_client.py 도크 참조.

[★조용한 오류 차단 — 이 수집기의 핵심]
  HTS 자동화의 지배적 실패는 '접속 실패'가 아니라 **엉뚱한 화면이 조용히 찍히는 것**이다.
  캡처는 "성공"하고 파일도 멀쩡하며 분석가만 엉뚱한 숫자를 읽는다. 그래서 3중 검증
  (marker_text 화면번호 · sha256 · settled)을 통과한 장만 저장하고, 나머지는 사유와 함께
  status 로 남긴다. **status != ok 인 화면의 값은 아예 산출하지 않는다**(0 으로 채우지 않는다 —
  retro_label dist_disc_count 와 같은 계약: 0 = 확인된 값, 부재 = 확인 불가).

[★watchlist 생애주기] 0231·0261 은 관심종목이 비면 빈 화면이다.
  set → capture → **reset** 을 try/finally 로 묶어 예외가 나도 사용자 관심종목을 되돌린다.

[출력] 세션폴더/hts_capture.json + 세션폴더/hts_captures/<screen>[_<variant>]_<시각>.png
  ※ 이미지가 세션에 그대로 보존돼, 회고가 나중에 '그날 분석가가 본 화면'을 재확인할 수 있다.

[설정] kairos_api.txt 에 토큰(=*.txt 이므로 .gitignore 차단). 없으면 무동작 exit 0.

[사용법]
  python hts_capture_collect.py                      # 기본 화면 세트 수집
  python hts_capture_collect.py --check              # 상태·목록만 점검(저장 안 함)
  python hts_capture_collect.py --list               # 카탈로그 출력
  python hts_capture_collect.py --screens short_lend,foreign_inst --tickers 005930,000660
"""
import os
import sys
import json
import argparse
import logging
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")

logging.basicConfig(level=logging.INFO, format="[htscap] %(message)s")
log = logging.getLogger("htscap")

from common import save_json_atomic, resolve_session
import kairos_client as kc


# ── 캡처 대상 카탈로그 ────────────────────────────────────────────────────────
#   화면번호는 미래에셋 공식 신구맵핑표의 '카이로스(신)' 열 기준이며, 노트북 에이전트의
#   화이트리스트와 일치해야 한다(불일치 시 403 forbidden_screen = 정상 차단).
#   needs_watchlist=True 인 화면은 관심종목이 비면 **빈 화면**이 나온다.
SCREENS = {
    "short_lend": {
        "no": "0231", "name": "관심종목 신용/공매도/대차 현황", "needs_watchlist": True,
        "why": "공매도잔고+대차잔고+신용을 관심종목 전체로 한 장에 — KRX 차단분의 유일한 대체",
    },
    "foreign_inst": {
        "no": "0261", "name": "관심종목 외국인/기관 매매현황", "needs_watchlist": True,
        "why": "종목별 외인/기관 순매수(메일 '전일 투자자별 수급' 표 보강)",
    },
    "investor_daily": {
        "no": "0254", "name": "투자자 일별 매매현황", "needs_watchlist": False,
        "why": "시장 전체 개인/외국인/기관 일별 순매수. flow_collect 공백 대체(변형 15종)",
    },
    "program_daily": {
        "no": "0273", "name": "프로그램매매 일별현황", "needs_watchlist": False,
        "why": "차익/비차익 순매수 — 외국인 수급과 교차(변형 kospi/kosdaq)",
    },
    "broker_3d": {
        "no": "0214", "name": "전체거래원 연속 3일 순매매상위종목", "needs_watchlist": False,
        "why": "3일 연속 순매수 창구 = 지속 매집(변형 net_buy/net_sell)",
    },
    "basis": {
        "no": "0313", "name": "선물 베이시스/스프레드", "needs_watchlist": False,
        "why": "베이시스 = 프로그램 차익 매수/매도 압력의 선행 지표",
    },
    "night_fut_quote": {
        "no": "9308", "name": "야간선물옵션 종합시세", "needs_watchlist": False,
        "why": "야간 선물 가격·미결제. investing 폴백의 교차검증용",
    },
    "short_top": {
        "no": "0235", "name": "공매도상위종목분석", "needs_watchlist": False,
        "why": "공매도 급증 상위 — 숏 후보 발굴([6.7] 숏 3중정렬 입력)",
    },
    "lend_top": {
        "no": "0238", "name": "대차잔고 상위종목 분석", "needs_watchlist": False,
        "why": "대차잔고 급증 = 공매도 대기물량(잔고 자체보다 '증가'가 신호)",
    },
    "afterhours": {
        "no": "0147", "name": "시간외단일가 종목등락현황", "needs_watchlist": False,
        "why": "전일 시간외 흐름 → 당일 갭 예측 보조",
    },
}

# ★9314(야간선물 투자자별)는 카이로스에서 쓸 수 없다 — CME/EUREX 연계 시절 화면이라
#   KRX 자체 야간거래(2025-06-09~) 데이터가 들어오지 않는다(노트북 가이드 §9 실측).
#   대안: KRX 정보데이터시스템 → 파생상품 → 투자자별 거래실적 → 시장구분 '야간'
#         (조회일자 = 야간거래 종료일 = T+1). 단 현재 data.krx.co.kr 은 403 차단 상태다.
UNAVAILABLE = {
    "night_fut_investor": ("9314", "CME 연계 화면이라 KRX 자체 야간거래 데이터 미포함 — "
                                   "KRX 정보데이터시스템 '야간' 시장구분 사용(현재 403 차단)"),
}

DEFAULT_SCREENS = ["short_lend", "foreign_inst", "investor_daily", "night_fut_quote"]


def collect(session_dir, screen_keys, tickers=None, save_images=True):
    """화면 목록 수집 → payload. 부분 실패해도 계속한다(전부 실패해도 exit 0)."""
    h = kc.health()
    if not h.get("hts") or h.get("hts_login_screen"):
        # ★조용한 결측 금지: '캡처 0건'이 아니라 '왜 못 했는지'를 남긴다
        log.warning("HTS 상태 불가 — hts=%s login_screen=%s", h.get("hts"), h.get("hts_login_screen"))
        return {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "tz": "KST",
            "source": "kairos_hts_capture", "agent_reachable": True,
            "blocked": "login_screen" if h.get("hts_login_screen") else "hts_not_found",
            "health": h, "n_requested": len(screen_keys), "n_ok": 0, "captures": [],
            "note": "HTS 미로그인/미실행으로 수집 불가 — 값 없음이 아니라 '확인 불가'다. 사람 확인 필요.",
        }

    img_dir = os.path.join(session_dir, "hts_captures")
    if save_images:
        try:
            os.makedirs(img_dir, exist_ok=True)
        except Exception as e:
            log.warning("이미지 폴더 생성 실패: %s", type(e).__name__)
            save_images = False

    need_wl = any((SCREENS.get(k) or {}).get("needs_watchlist") for k in screen_keys)
    did_set = False
    results = []
    try:
        if need_wl and tickers:
            r = kc.set_watchlist(tickers)
            did_set = True
            log.info("관심종목 설정: removed=%s added=%s", r.get("removed"), r.get("added"))
        elif need_wl:
            log.warning("관심종목 화면이 포함됐는데 --tickers 가 없다 — 빈 화면이 찍힐 수 있다")

        for key in screen_keys:
            spec = SCREENS.get(key)
            if not spec:
                why = UNAVAILABLE.get(key)
                results.append({"screen": key, "status": "disabled",
                                "reason": (why[1] if why else "미등록 화면 키"),
                                "screen_no": (why[0] if why else None)})
                log.warning("%-18s disabled — %s", key, (why[1] if why else "미등록"))
                continue
            try:
                shots, meta = kc.capture(key, expect_no=spec["no"])
            except kc.KairosError as e:
                results.append({"screen": key, "screen_no": spec["no"],
                                "screen_name": spec["name"], "status": "agent_error",
                                "reason": str(e)})
                log.warning("%-18s agent_error — %s", key, e)
                continue

            stamp = str(meta.get("captured_at_kst") or
                        datetime.now().strftime("%Y-%m-%d %H:%M")
                        ).replace("-", "").replace(":", "").replace(" ", "_")[:13]
            files, bad = [], []
            for i, s in enumerate(shots):
                if not s["ok"]:
                    bad.append({"variant": s.get("variant"), "reason": s["reason"]})
                    continue
                rec = {"variant": s.get("variant"), "label": s.get("label"),
                       "bytes": s["bytes"], "image": None}
                if save_images and s["png"]:
                    suffix = ("_" + str(s.get("variant"))) if s.get("variant") else ""
                    fn = "%s%s_%s.png" % (key, suffix, stamp)
                    try:
                        with open(os.path.join(img_dir, fn), "wb") as f:
                            f.write(s["png"])
                        rec["image"] = ("hts_captures/" + fn)
                    except Exception as e:
                        rec["error"] = "저장 실패: %s" % type(e).__name__
                files.append(rec)

            status = "ok" if files else ("marker_mismatch" if bad else "agent_error")
            results.append({
                "screen": key, "screen_no": spec["no"], "screen_name": spec["name"],
                "status": status,
                "reason": (bad[0]["reason"] if (bad and not files) else None),
                "captured_at_kst": meta.get("captured_at_kst"),
                "marker_text": (str(meta.get("marker_text"))[:120]
                                if meta.get("marker_text") else None),
                "n_shots": len(shots), "n_ok": len(files),
                "files": files, "rejected": bad,
            })
            log.info("%-18s %-16s %d/%d장 %s", key, status, len(files), len(shots),
                     (bad[0]["reason"][:40] if bad else ""))
    finally:
        if did_set:
            try:
                r = kc.reset_watchlist()
                log.info("관심종목 복구: removed=%s", r.get("removed"))
            except Exception as e:
                log.warning("★관심종목 복구 실패 — 사람이 확인해야 한다: %s", type(e).__name__)

    ok_n = sum(1 for r in results if r.get("status") == "ok")
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tz": "KST", "source": "kairos_hts_capture", "agent_reachable": True,
        "health": {k: h.get(k) for k in ("hts", "hts_login_screen", "current_screen", "now_kst")},
        "watchlist_used": list(tickers or []) if did_set else None,
        "n_requested": len(results), "n_ok": ok_n,
        "n_rejected_shots": sum(len(r.get("rejected") or []) for r in results),
        "coverage": round(ok_n / len(results), 3) if results else 0.0,
        "captures": results,
        "note": ("status != ok 인 화면은 '데이터 없음'이 아니라 '확인 불가'다 — 값을 0 으로 "
                 "채우지 말고 그 화면 근거를 쓰지 마라. 이미지에서 읽은 값은 [캡처] 로 표기하고 "
                 "API 값과 섞지 마라(단위·시점이 다르다). rejected 는 3중 검증에 걸려 폐기된 "
                 "장이며, 그 사유가 곧 신뢰도 정보다. ※화면공유 알림 배너가 이미지 중앙에 "
                 "찍힐 수 있다 — 가려진 값은 읽지 마라."),
    }


def _tickers_from_session(session_dir, limit=30):
    """세션 predictions.json 의 픽/숏 티커(없으면 watch_tickers 파일)."""
    out = []
    try:
        with open(os.path.join(session_dir, "predictions.json"), encoding="utf-8") as f:
            j = json.load(f)
        for kind in ("picks", "shorts"):
            for it in (j.get(kind) or []):
                t = str(it.get("ticker") or "").zfill(6)
                if len(t) == 6 and t.isdigit() and t not in out:
                    out.append(t)
    except Exception:
        pass
    if not out:
        for name in ("watch_tickers.txt", "watch_tickers"):
            p = os.path.join(HERE, name)
            if os.path.isfile(p):
                try:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        for line in f:
                            t = line.strip().split(",")[0].strip().zfill(6)
                            if len(t) == 6 and t.isdigit() and t not in out:
                                out.append(t)
                except Exception:
                    pass
                break
    return out[:limit]


def main():
    ap = argparse.ArgumentParser(description="카이로스 HTS 캡처 → 세션 hts_capture.json")
    ap.add_argument("--session", default=None)
    ap.add_argument("--screens", default=None, help="쉼표구분 화면 키(기본 4종)")
    ap.add_argument("--tickers", default=None, help="쉼표구분(미지정 시 세션 predictions 에서)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--check", action="store_true", help="상태·화면목록만 점검(저장 안 함)")
    ap.add_argument("--list", action="store_true", help="카탈로그 출력")
    args = ap.parse_args()

    if args.list:
        print("%-18s %-5s %-34s %s" % ("KEY", "화면", "화면명", "관심종목필요"))
        for k, v in SCREENS.items():
            print("%-18s %-5s %-34s %s" % (k, v["no"], v["name"],
                                           "Y" if v["needs_watchlist"] else "-"))
        for k, (no, why) in UNAVAILABLE.items():
            print("%-18s %-5s %-34s %s" % (k, no, "[사용 불가]", why[:40]))
        return 0

    if not kc.load_token():
        log.info("kairos_api.txt 에 토큰 없음 → 무동작 종료(노트북 연동 전 정상)")
        return 0

    keys = [s.strip() for s in (args.screens or ",".join(DEFAULT_SCREENS)).split(",") if s.strip()]

    if args.check:
        h = kc.health()
        log.info("health: hts=%s login_screen=%s screen=%s",
                 h.get("hts"), h.get("hts_login_screen"), h.get("current_screen"))
        try:
            d = kc.screens()
            avail = {s.get("key") for s in (d.get("screens") or [])}
            log.info("에이전트 제공 %d종 / 요청 %d종 · 미제공: %s",
                     len(avail), len(keys), sorted(set(keys) - avail) or "없음")
        except Exception as e:
            log.warning("screens 조회 실패: %s", type(e).__name__)
        return 0

    session = args.session or resolve_session(OUTPUT_DIR)
    if not session or not os.path.isdir(session):
        log.warning("세션 폴더를 찾을 수 없음 → 종료")
        return 0

    tickers = ([t.strip() for t in args.tickers.split(",") if t.strip()]
               if args.tickers else _tickers_from_session(session))
    payload = collect(session, keys, tickers=tickers)
    out = args.out or os.path.join(session, "hts_capture.json")
    try:
        save_json_atomic(out, payload)
        log.info("저장: %s (ok %d/%d · 폐기 %d장)", out, payload.get("n_ok", 0),
                 payload.get("n_requested", 0), payload.get("n_rejected_shots", 0))
    except Exception as e:
        log.warning("저장 실패: %s", type(e).__name__)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        log.warning("예기치 못한 오류(무시): %s: %s", type(e).__name__, e)
        sys.exit(0)
