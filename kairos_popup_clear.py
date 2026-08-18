"""카이로스 팝업 원격 정리 — pc21 에서 노드(카이로스 노트북)의 공지 팝업을 확인·해제한다.

왜 필요한가
  로그인 직후 공지/알림 팝업이 화면을 덮으면 `hts_capture_collect.py` 가 `popup_blocked`
  로 캡처를 거부한다(가려진 화면을 찍어 보내지 않기 위한 의도된 동작). 그러면 그날
  아침 리포트의 HTS 자료가 통째로 빈다. 지금까지는 사람이 노트북 앞에 가야 했다.

안전 경계 (노드의 `agent/popups.py` 가 서버 쪽에서도 다시 강제한다)
  - 클릭은 **지금 열거된 팝업 창 사각형 안**에서만 일어난다. 이 스크립트가 좌표를 줘도
    서버가 재검사하고, 벗어나면 `click_rejected` 로 거부한다.
  - **주문·인증 계열 팝업이 하나라도 있으면 아무것도 누르지 않고 중단**한다.
    ENTER 한 번이 주문 확정이 될 수 있기 때문이다 — 사람이 직접 확인해야 한다.
  - 로그인 화면이면 동작하지 않는다(로그인 스크립트의 구역).

쓰는 법
  python kairos_popup_clear.py --check        팝업이 있는지만 본다(누르지 않음)
  python kairos_popup_clear.py --shot         전체화면 PNG 를 받아 저장(판독용)
  python kairos_popup_clear.py --dry          무엇을 누를지 계획만 본다
  python kairos_popup_clear.py                자동 해제(ESC -> ENTER -> 우상단 X)
  python kairos_popup_clear.py --click 1520,412   캡처를 판독해 그 지점만 1회

종료 코드
  0 = 팝업 없음 또는 전부 해제 / 2 = 팝업이 남음 / 3 = 사람 확인 필요(주문 팝업·로그인 화면)
  4 = 노드에 연결 못 함(토큰 없음·오프라인 — 무동작으로 본다)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import datetime

import kairos_client as kc
from common import save_json_atomic

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "logs", "kairos_popup")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _prune(keep: int = 40) -> None:
    """오래된 타임스탬프 파일 정리 — logs\\ 가 무한히 커지지 않게. latest_* 는 건드리지 않는다."""
    try:
        files = [os.path.join(OUT_DIR, f) for f in os.listdir(OUT_DIR)
                 if not f.startswith("latest")]
        files.sort(key=os.path.getmtime, reverse=True)
        for p in files[keep:]:
            try:
                os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def _save_png(res: dict, tag: str) -> str | None:
    """타임스탬프본(이력) + latest_<tag>.png(코워크가 읽는 고정 경로) 둘 다 남긴다.

    ★고정 이름이 있어야 코워크 지시문이 '가장 최근 파일을 찾아라'가 아니라
      '이 경로를 읽어라'가 된다(파일명 추정 실수를 없앤다).
    """
    b64 = res.get("png_b64")
    if not b64:
        return None
    os.makedirs(OUT_DIR, exist_ok=True)
    raw = base64.b64decode(b64)
    path = os.path.join(OUT_DIR, "%s_%s.png" % (_stamp(), tag))
    with open(path, "wb") as f:
        f.write(raw)
    fixed = os.path.join(OUT_DIR, "latest_%s.png" % tag)
    try:
        with open(fixed, "wb") as f:
            f.write(raw)
    except OSError:
        fixed = None
    _prune()
    return fixed or path


_LAST_ORIGIN = {"x": 0, "y": 0}


def _shot(tag: str, region=None) -> str | None:
    """전체화면을 받아 저장하고 경로를 돌려준다. 실패는 조용히 None."""
    try:
        path = "/screenshot" + (("?region=%s" % region) if region else "")
        res = kc.call(path, timeout=90)
    except Exception as exc:                      # noqa: BLE001
        print("[popup] 화면 수신 실패(%s) — 진단 이미지 없이 진행" % exc)
        return None
    if not res.get("ok"):
        print("[popup] 화면 수신 거부: %s" % res.get("error"))
        return None
    p = _save_png(res, tag)
    if p:
        r = res.get("rect") or {}
        _LAST_ORIGIN.update({"x": int(r.get("x", 0)), "y": int(r.get("y", 0))})
        m = res.get("masked_tokens")
        note = "계좌 마스킹 %s" % ("OCR 불가(마스킹 안 됨)" if m == -1 else "%s곳" % m)
        print("[popup] 이미지 저장: %s (%s)" % (p, note))
        # ★좌표 변환 규약을 매번 찍는다 — 이미지 좌표를 그대로 --click 에 넣는 실수 방지.
        if _LAST_ORIGIN["x"] or _LAST_ORIGIN["y"]:
            print("[popup] ★화면좌표 = 이미지좌표 + (%d,%d) — 그대로 쓰지 마라."
                  % (_LAST_ORIGIN["x"], _LAST_ORIGIN["y"]))
        else:
            print("[popup] 이미지 좌표 = 화면 좌표(원점 0,0) — 읽은 값을 그대로 --click 에 쓰면 된다.")
    return p


def _print_popups(state: dict) -> None:
    for p in state.get("popups") or []:
        r = p["rect"]
        print("  - [%s/%s] %s  사각형 x %d~%d, y %d~%d"
              % (p["kind"], p.get("source", "?"), p["title"] or "(제목 없음)",
                 r["x"], r["x"] + r["w"], r["y"], r["y"] + r["h"]))
        for b in p.get("buttons") or []:
            print("      버튼 '%s' @ %d,%d" % (b["text"], b["x"], b["y"]))
        if not (p.get("buttons") or []):
            print("      버튼 없음 — 카이로스가 직접 그린 대화상자다."
                  " 화면을 읽고 --click 으로 좌표를 지정해야 한다.")


def main() -> int:
    ap = argparse.ArgumentParser(description="카이로스 공지 팝업 원격 정리")
    ap.add_argument("--check", action="store_true", help="조회만(누르지 않음)")
    ap.add_argument("--shot", action="store_true", help="전체화면 PNG 저장만")
    ap.add_argument("--region", default=None, help="부분 캡처 L,T,W,H")
    ap.add_argument("--dry", action="store_true", help="계획만 본다")
    ap.add_argument("--click", default=None, help="X,Y 지점만 1회 클릭")
    ap.add_argument("--max-rounds", type=int, default=4)
    ap.add_argument("--no-keys", action="store_true", help="ESC/ENTER 를 쓰지 않는다")
    ap.add_argument("--no-corner", action="store_true", help="우상단 X 추정 클릭 금지")
    a = ap.parse_args()

    if not kc.load_token():
        print("[popup] kairos_api.txt 에 토큰이 없다 — 무동작(정상).")
        return 4

    # 1) 지금 상태
    try:
        st = kc.call("/popups", timeout=60)
    except Exception as exc:                      # noqa: BLE001
        print("[popup] 노드 연결 실패: %s — 무동작." % exc)
        return 4

    if not st.get("ok"):
        err = st.get("error")
        print("[popup] 조회 거부: %s (%s)" % (err, st.get("detail", "")))
        if err == "not_found":
            print("[popup] ★노드 에이전트가 옛 버전이다 — /popups 가 없다."
                  " agent 폴더를 push 하고 에이전트를 재시작하라.")
        return 3

    blocked = bool(st.get("modal_blocked"))
    print("[popup] 막는 창 %d개 (주문계열=%s · 모달차단=%s)"
          % (st.get("count", 0), st.get("has_order_popup"), blocked))
    _print_popups(st)

    if a.shot:
        _shot("manual", a.region)
        return 0

    if st.get("has_order_popup"):
        print("[popup] ★주문·인증 계열 대화상자가 있다 — 자동으로 손대지 않는다. 사람이 확인하라.")
        _shot("order_popup")
        return 3

    if not st.get("count"):
        if blocked:
            # ★메인 창이 비활성인데 창을 못 찾은 경우. '없음'으로 넘기면 캡처가 전멸한다.
            print("[popup] ★막는 창을 못 찾았는데 메인 창이 비활성이다(모달 차단 상태).")
            print("        캡처는 modal_blocked 로 실패한다 — 화면을 저장하니 사람이 확인하라.")
            _shot("blocked_unknown")
            return 3
        print("[popup] 막는 창 없음 — 캡처를 그대로 진행하면 된다.")
        return 0

    if a.check:
        return 2

    before_png = _shot("before")

    body = {"max_rounds": a.max_rounds,
            "allow_keys": not a.no_keys,
            "allow_corner_click": not a.no_corner}
    if a.dry:
        body["dry_run"] = True
    if a.click:
        try:
            x, y = [int(v) for v in a.click.split(",")]
        except Exception:
            print("[popup] --click 은 X,Y 형식이다.")
            return 3
        body["click"] = {"x": x, "y": y}

    try:
        res = kc.call("/popups/close", body=body, timeout=180)
    except Exception as exc:                      # noqa: BLE001
        print("[popup] 해제 호출 실패: %s" % exc)
        return 3

    if a.dry:
        print("[popup] (dry) 계획:")
        for s in res.get("plan") or []:
            print("   %s %s %s" % (s.get("action"),
                                   s.get("title"),
                                   s.get("x") is not None and
                                   "(%s,%s)" % (s.get("x"), s.get("y")) or ""))
        return 0

    if not res.get("ok"):
        print("[popup] 해제 거부: %s (%s)" % (res.get("error"), res.get("detail", "")))
        _shot("rejected")
        return 3

    for s in res.get("steps") or []:
        pt = s.get("point")
        print("   %-6s %-28s %s%s"
              % (s.get("action"), (s.get("title") or "")[:28], s.get("result"),
                 (" @%d,%d" % (pt["x"], pt["y"])) if pt else ""))

    after_png = _shot("after")
    print("[popup] 닫음 %d개 / 남음 %d개 — %s"
          % (res.get("closed", 0), len(res.get("after") or []), res.get("note")))

    os.makedirs(OUT_DIR, exist_ok=True)
    record = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "before_png": before_png, "after_png": after_png,
        "image_origin": dict(_LAST_ORIGIN),   # 화면좌표 = 이미지좌표 + origin
        "closed": res.get("closed"), "cleared": res.get("cleared"),
        "before": res.get("before"), "after": res.get("after"),
        "steps": res.get("steps"),
    }
    save_json_atomic(os.path.join(OUT_DIR, "latest.json"), record)
    save_json_atomic(os.path.join(OUT_DIR, "%s_result.json" % _stamp()), record)

    if res.get("cleared"):
        return 0

    print("[popup] ★남은 팝업이 있다. 다음 순서로 처리하라:")
    print("        1) 이 이미지를 읽어라: %s" % (after_png or "(저장 실패)"))
    print("        2) 남은 팝업의 닫기(X)/확인 버튼 좌표를 이미지에서 읽어라")
    for p in res.get("after") or []:
        r = p["rect"]
        print("           - '%s' 사각형 x %d~%d, y %d~%d (이 안의 좌표만 허용된다)"
              % (p["title"], r["x"], r["x"] + r["w"], r["y"], r["y"] + r["h"]))
    print("        3) python kairos_popup_clear.py --click X,Y")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
