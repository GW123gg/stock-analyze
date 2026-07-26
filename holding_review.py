# -*- coding: utf-8 -*-
"""
holding_review.py — 이전에 추천한 종목의 '오늘 시점' 재평가 → 세션 holding_review.json [v10.4 신규]

[왜] 이 시스템은 매일 새 픽을 내지만 **낸 픽을 다시 들여다보는 경로가 없었다**. 독자는
  "어제 산 걸 계속 들고 있어도 되나"를 가장 궁금해하는데, 리포트에는 그 답이 없었다
  (섹션 0·1·1.9·2·3·4·5 어디에도 기존 픽 재평가가 없다 — 2026-07-27 확인).
  회고(retro_label)는 **만기 후 사후채점**이라 '지금 보유 중'인 픽에는 답을 못 준다.

[★설계 원칙 — 수집기는 사실만, 판정은 분석가가]
  이 파일은 매도/보유를 **판정하지 않는다**. 새 임계값도 만들지 않는다(과적합 금지 — [F2] 철학).
  대신 픽이 **스스로 선언했던 계약**(target_pct·stop_pct·partial_take_pct·trailing_stop_pct·
  horizon_days·expected_peak_days·path_view)에 비추어 **지금 어디에 와 있는지**를 측정한다.
  즉 "새 규칙으로 자르자"가 아니라 "네가 한 말을 지키고 있나"를 보여준다.
  최종 판단(계속보유/축소/청산)은 분석 지시문 [5.16]에 따라 Cowork 분석가가 쓴다.

[★룩어헤드 금지] 분석 시점은 06:30 KST — 장 시작 전이다. 따라서 **직전 거래일 종가까지만** 쓴다.
  당일 봉은 존재해도 배제한다(retro_label·fsc_collect 와 같은 규율).

[출력] 세션폴더/holding_review.json
[사용법]
  python holding_review.py                    # 오늘 세션에 저장
  python holding_review.py --check            # 산출만 하고 저장 안 함(요약 출력)
  python holding_review.py --out tmp.json --lookback-days 45
"""
import os
import sys
import json
import argparse
import logging
from datetime import datetime, timedelta

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

logging.basicConfig(level=logging.INFO, format="[holdrev] %(message)s")
log = logging.getLogger("holdrev")

from common import save_json_atomic, resolve_session

try:
    import accuracy_tracker as acc
    ACC_OK = True
except Exception:
    acc = None
    ACC_OK = False

# 만기가 지난 픽도 이 기간까지는 '최근 만기'로 함께 보여준다(무언 이탈 방지 — 정리 판단을 남기게).
GRACE_BDAYS = 3
# 조회 대상 픽의 최대 소급 일수(캘린더). horizon 40(장기)+여유를 덮는다.
DEFAULT_LOOKBACK_DAYS = 90

_PX_CACHE = {}
_KOSPI_CACHE = {}


# =====================================================================
# 가격 취득 (FDR — KRX 차단 상태에서도 동작 확인됨. 직전 거래일까지만)
# =====================================================================
def _closes(code, start_date, end_date):
    """[start, end] 종가 시계열 → [(date, close), ...]. 실패 시 []. (code,start,end) 캐시."""
    if not FDR_OK:
        return []
    key = (str(code).zfill(6), str(start_date), str(end_date))
    if key in _PX_CACHE:
        return _PX_CACHE[key]
    out = []
    try:
        df = fdr.DataReader(str(code).zfill(6), str(start_date), str(end_date))
        for idx, row in df.iterrows():
            try:
                c = float(row["Close"])
                if c > 0:
                    out.append((idx.date(), c))
            except Exception:
                continue
    except Exception:
        out = []
    _PX_CACHE[key] = out
    return out


def _kospi_closes(start_date, end_date):
    key = (str(start_date), str(end_date))
    if key in _KOSPI_CACHE:
        return _KOSPI_CACHE[key]
    out = []
    if FDR_OK:
        try:
            df = fdr.DataReader("KS11", str(start_date), str(end_date))
            for idx, row in df.iterrows():
                try:
                    c = float(row["Close"])
                    if c > 0:
                        out.append((idx.date(), c))
                except Exception:
                    continue
        except Exception:
            out = []
    _KOSPI_CACHE[key] = out
    return out


# =====================================================================
# 순수 계산 — 단독 테스트 가능
# =====================================================================
def _pct(a, b):
    """b 대비 a 의 변화율(%). b 가 0/None 이면 None."""
    try:
        if not b:
            return None
        return round((float(a) / float(b) - 1) * 100, 2)
    except Exception:
        return None


def review_one(pick, kind, pred_date, series, kospi_series, asof_date):
    """픽 1건 + 진입 후 종가열 → 재평가 사실 dict. 판정은 하지 않는다.

    series/kospi_series: [(date, close), ...] — **진입일 다음 거래일부터 직전 거래일까지**.
    """
    entry = pick.get("entry_ref")
    try:
        entry = float(entry) if entry else None
    except Exception:
        entry = None

    rec = {
        "ticker": str(pick.get("ticker") or "").zfill(6),
        "name": pick.get("name"),
        "kind": kind,                                   # pick | short
        "pred_date": pred_date,
        "tag": pick.get("tag"),
        "timing": pick.get("timing"),
        "horizon_days": pick.get("horizon_days"),
        "entry_ref": entry,
        "entry_ref_estimated": pick.get("entry_ref_estimated"),
        # 픽이 스스로 선언했던 계약(새 임계값이 아니다 — 그때 그가 한 말이다)
        "declared": {
            "target_pct": pick.get("target_pct"),
            "stop_pct": pick.get("stop_pct"),
            "partial_take_pct": pick.get("partial_take_pct"),
            "trailing_stop_pct": pick.get("trailing_stop_pct"),
            "conviction": pick.get("conviction"),
            "size_pct": pick.get("size_pct"),
            "path_view": pick.get("path_view"),                   # v10.1
            "expected_peak_days": pick.get("expected_peak_days"),  # v10.1
            "expected_gain_pct": pick.get("expected_gain_pct"),
            "expected_pullback_pct": pick.get("expected_pullback_pct"),
        },
        "asof_date": str(asof_date) if asof_date else None,
        "elapsed_bdays": len(series),
        "status": "ok",
    }

    if entry is None or not series:
        rec["status"] = "no_price" if entry is not None else "no_entry_ref"
        return rec

    closes = [c for _, c in series]
    last = closes[-1]
    peak = max(closes)
    trough = min(closes)
    peak_i = closes.index(peak)
    trough_i = closes.index(trough)

    ret = _pct(last, entry)
    rec["last_close"] = round(last, 2)
    rec["ret_pct"] = ret
    rec["peak_close"] = round(peak, 2)
    rec["peak_gain_pct"] = _pct(peak, entry)
    rec["bdays_to_peak"] = peak_i + 1
    rec["trough_close"] = round(trough, 2)
    rec["max_adverse_pct"] = _pct(trough, entry)          # 진입 후 최악의 순간
    rec["bdays_to_trough"] = trough_i + 1
    rec["drawdown_from_peak_pct"] = _pct(last, peak)      # 고점 대비 현재 반납폭(<=0)

    # alpha — 같은 창의 코스피 대비 초과. 손실이 '시장 베타'인지 '종목 선택'인지 가른다([0.5]/F8-b)
    # ★alpha_pct 는 **raw**(ret - kospi) 로 둔다 — retro_label.alpha_h_pct·accuracy_tracker 와
    #   같은 규약이어야 회고/채점/보유재평가의 숫자가 서로 대조된다. 숏에서는 **음수가 좋다**
    #   ("지수보다 더 빠진 폭" — accuracy_tracker:701 의 회고 9회 정정).
    if kospi_series:
        k0 = kospi_series[0][1]
        k1 = kospi_series[-1][1]
        kret = _pct(k1, k0)
        rec["kospi_ret_pct"] = kret
        if ret is not None and kret is not None:
            rec["alpha_pct"] = round(ret - kret, 2)

    # ★방향 보정 쌍 — 숏은 부호가 반대다. raw 와 보정본을 **쌍으로** 두지 않으면 정반대로 읽힌다.
    #   실사고(2026-07-27): 숏 엘앤에프가 -19.15% 하락(숏에겐 +19.15% 이익)했는데 alpha_pct 는
    #   raw -11.0 이라, 이 둘을 나란히 보면 "수익은 났는데 시장 대비 뒤졌다"로 **정반대 해석**된다.
    #   실제로는 지수(-8.15%)보다 11%p 더 빠져 숏이 이긴 것이다.
    #   → 분석·리포트는 반드시 `*_favorable` 쌍으로만 읽어라.
    if kind == "short":
        rec["favorable_pct"] = round(-ret, 2) if ret is not None else None
        rec["alpha_favorable_pct"] = (round(-rec["alpha_pct"], 2)
                                      if rec.get("alpha_pct") is not None else None)
    else:
        rec["favorable_pct"] = ret
        rec["alpha_favorable_pct"] = rec.get("alpha_pct")

    # ── 선언한 계약 대비 도달 플래그(사실) ──
    d = rec["declared"]
    flags = {}

    def _num(v):
        try:
            return float(v)
        except Exception:
            return None

    tp, sp = _num(d.get("target_pct")), _num(d.get("stop_pct"))
    pt, ts = _num(d.get("partial_take_pct")), _num(d.get("trailing_stop_pct"))
    # 롱 기준으로 계산하고, 숏은 부호를 뒤집어 같은 의미가 되게 한다
    gains = [_pct(c, entry) for c in closes]
    gains = [g for g in gains if g is not None]
    if kind == "short":
        gains = [-g for g in gains]
    best = max(gains) if gains else None
    worst = min(gains) if gains else None
    cur = gains[-1] if gains else None

    # ★계약값이 없으면 플래그를 False 로 채우지 마라 — '미이탈'로 오독된다.
    #   [F4] 익절/손절 의무화 이전 픽에는 target/stop 이 아예 없다(실측: 132건 중 64건만 보유).
    #   없는 것은 '아니다'가 아니라 '모른다'이므로 키 자체를 만들지 않고 아래 미선언 목록에 남긴다.
    if tp is not None and best is not None:
        flags["target_hit"] = best >= abs(tp)
    if sp is not None and worst is not None:
        flags["stop_hit"] = worst <= -abs(sp)
        if cur is not None:
            flags["currently_below_stop"] = cur <= -abs(sp)
    if pt is not None and best is not None:
        flags["partial_take_hit"] = best >= abs(pt)
    if ts is not None and best is not None and cur is not None:
        # 고점 대비 trailing_stop_pct 만큼 반납했는가(고점이 잡힌 뒤에만 의미)
        flags["trailing_hit"] = (best - cur) >= abs(ts)
    rec["level_flags"] = flags
    missing = [k for k, v in (("target_pct", tp), ("stop_pct", sp),
                              ("partial_take_pct", pt), ("trailing_stop_pct", ts)) if v is None]
    rec["contract_missing"] = missing
    rec["contract_complete"] = not missing

    # ── v10.1 시간축 전망 대비 경로 대조 ──
    epd = _num(d.get("expected_peak_days"))
    if epd is not None and epd > 0:
        overdue = rec["elapsed_bdays"] > epd
        rec["path_check"] = {
            "expected_peak_days": epd,
            "elapsed_bdays": rec["elapsed_bdays"],
            "peak_overdue": bool(overdue),
            # 예상 고점 시한이 지났는데 아직 플러스 전환도 못 했나(논지 훼손의 강한 신호)
            "overdue_and_negative": bool(overdue and (cur is not None and cur <= 0)),
        }
        eg = _num(d.get("expected_gain_pct"))
        if eg is not None and best is not None:
            rec["path_check"]["expected_gain_pct"] = eg
            rec["path_check"]["gain_vs_expected_pp"] = round(best - eg, 2)
        ep = _num(d.get("expected_pullback_pct"))
        if ep is not None and worst is not None:
            rec["path_check"]["expected_pullback_pct"] = ep
            # 예상보다 더 깊이 밀렸나(예상 되돌림은 음수로 적히거나 양수 크기로 적힐 수 있다)
            rec["path_check"]["deeper_than_expected"] = worst < -abs(ep)

    hz = _num(rec.get("horizon_days"))
    if hz is not None:
        rec["remaining_bdays"] = int(hz) - rec["elapsed_bdays"]
        rec["horizon_expired"] = rec["elapsed_bdays"] >= int(hz)
    return rec


def _next_bday_index(series, base_date):
    """base_date **다음** 거래일부터의 인덱스(진입 당일은 제외 — entry_ref 가 그날 값이므로)."""
    for i, (d, _c) in enumerate(series):
        if d > base_date:
            return i
    return len(series)


def _kospi_window(kser_all, pdate):
    """alpha 용 지수 창: [D-1 앵커봉] + [D 초과 봉들] (순수함수 — 단독 테스트 가능).
    ★v10.5 앵커 교정(2026-07-27 전면감사): 구 구현은 지수 다리를 'D 초과 첫 봉'(D+1 종가)에서
    시작시켜 종목 다리(entry_ref=D-1 종가)와 **2거래일** 어긋났다 — 추천일·익일 지수 변동이
    통째로 alpha 로 오귀속(07-12 추천 뒤 07-13 -8.95% 폭락이 전부 '종목 탓'으로 계산된 실측 사례).
    review_one 은 kospi_series[0]→[-1] 로 지수수익을 재므로, 첫 원소를 D-1 종가로 두면
    종목과 같은 창이 된다."""
    after = [x for x in kser_all if x[0] > pdate]
    before = [x for x in kser_all if x[0] < pdate]
    if not before or not after:
        return []          # 앵커 없이 D+1 시작으로 재던 구버전보다, 없는 게 낫다(오귀속 방지)
    return before[-1:] + after


# =====================================================================
def collect(lookback_days=DEFAULT_LOOKBACK_DAYS, asof=None):
    """열려 있는(만기 전 + 최근 만기) 픽을 모아 재평가. → payload dict."""
    if not ACC_OK:
        log.warning("accuracy_tracker import 불가 — 중단")
        return {}
    preds = acc.load_predictions()
    today = asof or datetime.now().date()
    cutoff = today - timedelta(days=lookback_days)

    rows = []
    for p in preds:
        try:
            pdate = datetime.strptime(str(p.get("date"))[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        if pdate < cutoff or pdate >= today:
            continue                     # 미래·너무 오래된 것 제외(당일 픽은 재평가 대상 아님)
        for kind, items in (("pick", p.get("picks") or []), ("short", p.get("shorts") or [])):
            for it in items:
                code = str(it.get("ticker") or "").zfill(6)
                if not code or len(code) != 6:
                    continue
                # 직전 거래일까지만(룩어헤드 금지) — end 를 어제로 둔다
                # D-1 앵커봉 확보용 여유(연휴 최장 대비 10일 — v10.5 지수 앵커 교정과 세트)
                start = (pdate - timedelta(days=10)).strftime("%Y-%m-%d")
                end = (today - timedelta(days=1)).strftime("%Y-%m-%d")
                ser_all = _closes(code, start, end)
                i0 = _next_bday_index(ser_all, pdate)
                ser = ser_all[i0:]
                kser_all = _kospi_closes(start, end)
                kser = _kospi_window(kser_all, pdate)   # ★D-1 앵커(종목 entry_ref 와 같은 빈티지)
                asof_d = ser[-1][0] if ser else None
                rec = review_one(it, kind, pdate.strftime("%Y-%m-%d"), ser, kser, asof_d)
                # 만기 + 유예 넘긴 것은 제외(이미 회고가 채점한다)
                hz = rec.get("horizon_days")
                try:
                    if hz and rec["elapsed_bdays"] > int(hz) + GRACE_BDAYS:
                        continue
                except Exception:
                    pass
                rows.append(rec)

    # 같은 종목이 여러 날 추천된 경우 — 분석가가 클러스터를 보게 묶어 둔다
    by_ticker = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)
    clusters = [{"ticker": t, "name": v[0].get("name"), "n_entries": len(v),
                 "pred_dates": [x["pred_date"] for x in v]}
                for t, v in by_ticker.items() if len(v) > 1]

    # ── 주의 사유(사실 나열 — 판정 아님) + 심각도. 분석가가 어디부터 볼지 정하는 데만 쓴다.
    #   priority: 1=가장 급함 … 5=평온. '팔아라'가 아니라 '먼저 들여다봐라'의 순서다.
    for r in rows:
        att, prio = [], 5
        fl = r.get("level_flags") or {}
        pc = r.get("path_check") or {}
        if fl.get("currently_below_stop"):
            att.append("선언한 손절선 아래에 현재 위치")
            prio = min(prio, 1)
        elif fl.get("stop_hit"):
            att.append("보유 중 손절선을 이탈한 적 있음(현재는 회복)")
            prio = min(prio, 3)
        if fl.get("trailing_hit"):
            att.append("고점 대비 선언한 트레일링 폭만큼 반납")
            prio = min(prio, 2)
        if fl.get("target_hit"):
            att.append("목표가 도달")
            prio = min(prio, 2)
        elif fl.get("partial_take_hit"):
            att.append("분할익절 구간 도달")
            prio = min(prio, 3)
        if pc.get("overdue_and_negative"):
            att.append("예상 고점 시한 경과했는데 아직 마이너스(논지 훼손 의심)")
            prio = min(prio, 2)
        elif pc.get("peak_overdue"):
            att.append("예상 고점 시한 경과")
            prio = min(prio, 4)
        if pc.get("deeper_than_expected"):
            att.append("예상 되돌림보다 깊게 밀림")
            prio = min(prio, 3)
        if r.get("horizon_expired"):
            att.append("보유기간(horizon) 만료 — 정리 판단 필요")
            prio = min(prio, 4)
        r["attention"] = att
        r["priority"] = prio

    ok = [r for r in rows if r.get("status") == "ok"]
    n_contract = sum(1 for r in rows if r.get("contract_complete"))
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tz": "KST",
        "asof_note": "직전 거래일 종가 기준(06:30 분석 시점 — 당일 봉 배제, 룩어헤드 없음)",
        "n_open": len(rows),
        "n_priced": len(ok),
        "n_expired_recent": sum(1 for r in rows if r.get("horizon_expired")),
        "n_attention": sum(1 for r in rows if r.get("attention")),
        # ★계약 커버리지 — 손절/목표가가 선언되지 않은 픽은 '이탈 여부를 알 수 없다'
        "n_contract_complete": n_contract,
        "n_contract_missing": len(rows) - n_contract,
        "contract_note": ("target/stop 이 선언되지 않은 픽은 level_flags 에 해당 키가 **없다**. "
                          "없는 것은 '미이탈'이 아니라 '확인 불가'다 — 0/False 로 읽지 마라."),
        "duplicate_clusters": clusters,
        "n_priority1": sum(1 for r in rows if r.get("priority") == 1),
        # 심각도 → 최신 추천 순. ★잘라내지 않고 전량 싣는다(메일에서 접더라도 '외 N건'을 밝힐 수 있게)
        "holdings": sorted(rows, key=lambda r: (r.get("priority", 5),
                                                r.get("pred_date") or "", r.get("ticker") or "")),
        "note": ("이 파일은 **판정하지 않는다** — 픽이 스스로 선언한 계약(target/stop/"
                 "partial/trailing/horizon/expected_peak_days) 대비 현재 위치를 측정만 한다. "
                 "계속보유/축소/청산 판단은 분석 지시문 [5.16]에 따라 분석가가 근거와 함께 쓴다. "
                 "손실이 alpha 로도 음수인지(종목 선택 실패) alpha>=0 인지(시장 베타)를 반드시 나눠 보라."),
    }
    return payload


def main():
    ap = argparse.ArgumentParser(description="이전 추천 종목 재평가 → 세션 holding_review.json")
    ap.add_argument("--session", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    ap.add_argument("--check", action="store_true", help="산출만 하고 저장 안 함")
    args = ap.parse_args()

    if not FDR_OK:
        log.warning("FinanceDataReader 없음 — 가격 재평가 불가, 종료")
        return 0

    payload = collect(lookback_days=args.lookback_days)
    if not payload:
        log.info("산출 없음 — 종료")
        return 0

    log.info("열린 픽 %d건(가격확보 %d · 최근만기 %d) · 중복종목 %d",
             payload["n_open"], payload["n_priced"],
             payload["n_expired_recent"], len(payload["duplicate_clusters"]))
    for r in payload["holdings"][:40]:
        if r.get("status") != "ok":
            log.info("  %s %-10s [%s] %s", r["pred_date"], (r.get("name") or "")[:10],
                     r["kind"], r["status"])
            continue
        fl = r.get("level_flags") or {}
        hit = ",".join(k for k, v in fl.items() if v) or "-"
        log.info("  %s %-10s [%s] %+6.2f%% (alpha %s) 경과 %s/%s일 | %s",
                 r["pred_date"], (r.get("name") or "")[:10], r["kind"],
                 r.get("favorable_pct") if r.get("favorable_pct") is not None else 0.0,
                 r.get("alpha_pct"), r.get("elapsed_bdays"), r.get("horizon_days"), hit)

    if args.check:
        return 0
    session = args.session or resolve_session(OUTPUT_DIR)
    out = args.out or (os.path.join(session, "holding_review.json") if session else None)
    if not out:
        log.warning("세션 폴더를 찾을 수 없음 — 저장 생략")
        return 0
    save_json_atomic(out, payload)
    log.info("저장: %s", out)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        log.warning("예기치 못한 오류(무시): %s: %s", type(e).__name__, e)
        sys.exit(0)
