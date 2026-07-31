# -*- coding: utf-8 -*-
"""
intraday_review.py — 장중 재분석: 오늘 픽이 지금 어떻게 가고 있나 → 세션 intraday_review.json
  [v11.3 신규 — 사용자 요청: "장 열리고 중간에 등락을 보면서 추가 분석, 내일 장 대비"]

[왜] 아침 리포트는 06:30(장 시작 전)에 나가고 끝이다. 그런데 정작 중요한 건 그 뒤다:
  · 추천한 자리에서 **실제로 살 수 있었나**(갭상승·상한가로 못 샀나) — `entry_window` 의 사후 검증
  · 지금 어디까지 왔나(목표·손절 근접)
  · **내일은 어떻게 볼 것인가**(픽 `next_day` 를 장중 실측으로 갱신)
  아침 예측을 장중에 한 번 대조하면 다음날 판단이 훨씬 정확해진다.

[★룩어헤드 경계 — 반드시 지켜라]
  이 파일은 **당일 장중 가격**을 쓴다. 06:30 시점엔 존재하지 않던 값이다.
  · 장중 분석·내일 대비에는 써도 된다(그 시점엔 실제로 아는 값이니까).
  · ★**회고 진입피처(pre_*)로는 절대 쓰지 마라.** payload 에 `is_intraday=true` 와
    `retro_use="forbidden_as_pre_feature"` 를 박아 둔다. retro_label 은 이 파일을 읽지 않는다.

[출력] 세션폴더/intraday_review.json (아침 리포트·predictions 를 덮어쓰지 않는다 — 별도 파일)

[사용법]
  python intraday_review.py                    # 오늘 세션 픽 전부
  python intraday_review.py --session <경로>
  python intraday_review.py --check            # 저장 없이 표만 출력
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

try:
    import FinanceDataReader as fdr
    FDR_OK = True
except Exception:
    fdr = None
    FDR_OK = False

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")

logging.basicConfig(level=logging.INFO, format="[intraday] %(message)s")
log = logging.getLogger("intraday")

from common import save_json_atomic, resolve_session

# 장 구간(KST). 이 밖에서 실행하면 '장중'이 아님을 명시한다.
MARKET_OPEN, MARKET_CLOSE = "09:00", "15:30"


def session_phase(now_hhmm=None):
    """지금이 장중인가 → pre_open | intraday | after_close (순수함수)."""
    t = now_hhmm or datetime.now().strftime("%H:%M")
    if t < MARKET_OPEN:
        return "pre_open"
    if t <= MARKET_CLOSE:
        return "intraday"
    return "after_close"


def _today_prices(code, day):
    """당일 일봉(시가·고가·저가·현재가). 장중이면 현재가는 '지금까지의 종가'다."""
    if not FDR_OK:
        return {}
    try:
        df = fdr.DataReader(str(code).zfill(6), str(day), str(day))
        if df is None or len(df) == 0:
            return {}
        r = df.iloc[-1]
        out = {}
        for k, col in (("open", "Open"), ("high", "High"), ("low", "Low"), ("last", "Close")):
            try:
                v = float(r[col])
                if v > 0:
                    out[k] = round(v, 2)
            except Exception:
                pass
        return out
    except Exception:
        return {}


def _pct(a, b):
    try:
        if not b:
            return None
        return round((float(a) / float(b) - 1) * 100, 2)
    except Exception:
        return None


def review_pick(pick, px, phase):
    """픽 1건 + 당일 시고저종 → 장중 사실 dict. ★판정은 하지 않는다(분석가 몫)."""
    entry = pick.get("entry_ref")
    try:
        entry = float(entry) if entry else None
    except Exception:
        entry = None
    ew = str(pick.get("entry_window") or "").strip()
    rec = {
        "ticker": str(pick.get("ticker") or "").zfill(6),
        "name": pick.get("name"),
        "tag": pick.get("tag"), "timing": pick.get("timing"),
        "entry_ref": entry,
        "entry_window": ew or None,
        "path_view": pick.get("path_view"),
        "declared": {"target_pct": pick.get("target_pct"), "stop_pct": pick.get("stop_pct")},
        "morning_next_day": pick.get("next_day"),
        "phase": phase,
    }
    if entry is None or not px:
        rec["status"] = "no_price" if entry is not None else "no_entry_ref"
        return rec
    rec["status"] = "ok"
    rec.update({"open": px.get("open"), "high": px.get("high"),
                "low": px.get("low"), "last": px.get("last")})
    rec["gap_pct"] = _pct(px.get("open"), entry)          # 시가 갭(진입 가능성의 1차 지표)
    rec["ret_pct"] = _pct(px.get("last"), entry)
    rec["high_pct"] = _pct(px.get("high"), entry)
    rec["low_pct"] = _pct(px.get("low"), entry)

    # ★entry_window 사후 검증 — '아침에 말한 자리에서 실제로 살 수 있었나'
    #   [왜] 회고 실측 '픽의 절반이 D+1~2 즉시고점' = 못 사는 추천이 절반이었다.
    #   이걸 장중에 바로 재면 다음 회차 entry_window 판단이 교정된다.
    chk = {"entry_window": ew or None}
    g = rec.get("gap_pct")
    if ew == "당일시가":
        # 갭이 크면 '시가 진입'은 사실상 불가(추천가와 딴 세상 가격)
        chk["buyable"] = (g is not None and g <= 3.0)
        chk["note"] = ("시가 갭 %+.2f%% — 추천가 근처 진입 가능" % g if chk["buyable"]
                       else "시가 갭 %+.2f%% — **추천가로 못 산다**(갭상승)" % g) if g is not None else "갭 미상"
    elif ew == "당일눌림":
        # 저가가 진입가 이하로 내려왔으면 눌림 진입 기회가 실재했다
        lo = rec.get("low_pct")
        chk["buyable"] = (lo is not None and lo <= 0)
        chk["note"] = ("저가 %+.2f%% — 눌림 진입 기회 있었음" % lo if chk["buyable"]
                       else "저가 %+.2f%% — 눌림이 오지 않음(진입 못 함)" % lo) if lo is not None else "저가 미상"
    elif ew == "당일종가":
        chk["buyable"] = None
        chk["note"] = "종가 진입 — 마감 후 판정(장중엔 미정)"
    elif ew == "익일이후":
        chk["buyable"] = None
        chk["note"] = "익일 진입 예정 — 오늘 움직임은 참고만"
    else:
        chk["note"] = "entry_window 미선언 — 진입 가능성 판정 불가"
    rec["entry_check"] = chk

    # 목표·손절 근접(선언한 계약 기준. 새 임계값 없음)
    fl = {}
    try:
        tp = float(pick.get("target_pct")) if pick.get("target_pct") is not None else None
        sp = float(pick.get("stop_pct")) if pick.get("stop_pct") is not None else None
    except (TypeError, ValueError):
        tp = sp = None
    if tp is not None and rec.get("high_pct") is not None:
        fl["target_touched"] = rec["high_pct"] >= abs(tp)
    if sp is not None and rec.get("low_pct") is not None:
        fl["stop_touched"] = rec["low_pct"] <= -abs(sp)
    if sp is not None and rec.get("ret_pct") is not None:
        fl["below_stop_now"] = rec["ret_pct"] <= -abs(sp)
    rec["level_flags"] = fl
    return rec


def collect(session_dir, phase=None):
    ph = phase or session_phase()
    day = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(os.path.join(session_dir, "predictions.json"), encoding="utf-8") as f:
            pred = json.load(f)
    except Exception as e:
        log.warning("predictions.json 읽기 실패: %s", type(e).__name__)
        return {}
    rows = []
    for kind in ("picks", "shorts"):
        for it in (pred.get(kind) or []):
            px = _today_prices(it.get("ticker"), day)
            r = review_pick(it, px, ph)
            r["kind"] = "pick" if kind == "picks" else "short"
            rows.append(r)

    ok = [r for r in rows if r.get("status") == "ok"]
    nb = [r for r in ok if (r.get("entry_check") or {}).get("buyable") is False]
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "tz": "KST",
        "trade_date": day, "phase": ph,
        "pred_date": pred.get("date"),
        # ★룩어헤드 경계 — 회고가 이 파일을 진입피처로 쓰면 안 된다
        "is_intraday": True,
        "retro_use": "forbidden_as_pre_feature",
        "n_total": len(rows), "n_priced": len(ok),
        "n_not_buyable": len(nb),
        "not_buyable": [{"ticker": r["ticker"], "name": r.get("name"),
                         "entry_window": r.get("entry_window"),
                         "note": (r.get("entry_check") or {}).get("note")} for r in nb],
        "rows": rows,
        "note": ("장중 실측이다. **아침 predictions 를 덮어쓰지 않는다** — 별도 파일이다. "
                 "용도 두 가지: ①entry_window 사후 검증(아침에 말한 자리에서 실제로 살 수 있었나) "
                 "②내일 대비(픽 next_day 를 장중 흐름으로 갱신). "
                 "★당일 장중 가격은 06:30 시점에 없던 값이므로 **회고 진입피처(pre_*)로 쓰면 "
                 "룩어헤드다** — 그 용도로는 절대 쓰지 마라."),
    }


def main():
    ap = argparse.ArgumentParser(description="장중 재분석 → 세션 intraday_review.json")
    ap.add_argument("--session", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--check", action="store_true", help="저장 없이 출력만")
    args = ap.parse_args()

    if not FDR_OK:
        log.warning("FinanceDataReader 없음 — 종료")
        return 0
    session = args.session or resolve_session(OUTPUT_DIR)
    if not session or not os.path.isdir(session):
        log.warning("세션 폴더를 찾을 수 없음 → 종료")
        return 0

    p = collect(session)
    if not p:
        return 0
    log.info("장 구간: %s · 픽/숏 %d건(가격확보 %d) · ★진입 불가 %d건",
             p["phase"], p["n_total"], p["n_priced"], p["n_not_buyable"])
    for r in p["rows"]:
        if r.get("status") != "ok":
            log.info("  %-10s %s", (r.get("name") or "")[:10], r["status"])
            continue
        fl = r.get("level_flags") or {}
        hit = ",".join(k for k, v in fl.items() if v) or "-"
        log.info("  %-10s [%s] 갭 %+6s 현재 %+6s 고 %+6s 저 %+6s | %-12s %s",
                 (r.get("name") or "")[:10], r.get("entry_window") or "-",
                 r.get("gap_pct"), r.get("ret_pct"), r.get("high_pct"), r.get("low_pct"),
                 hit, (r.get("entry_check") or {}).get("note", "")[:34])
    if args.check:
        return 0
    out = args.out or os.path.join(session, "intraday_review.json")
    try:
        save_json_atomic(out, p)
        log.info("저장: %s", out)
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
