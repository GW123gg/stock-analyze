#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
premarket_signals.py — 개장 전후 호가·가격 지표 계산 [v11.11 신규]

[무엇을 하나]
  카이로스 [0626] X-Ray 화면에서 읽은 **현재가 + 호가 20단**으로, 08:00 NXT 프리마켓부터
  쓸 수 있는 정량 지표를 계산한다. 08:00 자동매매 코워크가 매매계획의 조건과 대조하는 데 쓴다.

[★★지표를 맹신하지 마라 — 이 모듈의 설계 원칙]
  1) **호가 지표는 호가를 믿을 수 있을 때만 쓴다.** `ladder_ok=False` 면 전부 None 을 돌려준다
     (추측해서 채우지 않는다). OCR 이 `67,100` 을 `87,100` 으로 읽은 실측 사례가 있다.
  2) **얇은 호가에서 불균형은 노이즈다.** 프리마켓(08:00~08:50)은 정규장의 수십분의 일이라,
     몇백 주 주문 하나로 불균형이 ±0.9 까지 튄다. 두께가 기준 미만이면 불균형을 **무효화**한다.
  3) **어떤 지표도 단독으로 진입 근거가 될 수 없다.** `evaluate()` 는 계획(thesis)이 살아
     있을 때만 'go' 를 낼 수 있고, 지표는 그 계획을 **막는 쪽으로만** 강하게 작동한다.
     (지표가 좋다고 계획에 없는 종목을 사지 않는다 — 그건 그날 아침 분석이 안 본 종목이다.)
  4) 임계값은 전부 **어림값**이다. 데이터로 검증된 적 없다 — 회고가 표본을 쌓아 판정할 때까지
     '경계선'으로만 쓰고, 경계 ±20% 는 연속적으로 해석하라([4.8](7) 어림값 조항).

[입력] trade_client.quote() 응답 형식
  {"quote": {"price":..., "open":..., "high":..., "low":..., "volume":...},
   "ladder": {"best_ask":..., "best_bid":..., "levels":[{"price","ask_qty","bid_qty"}...]},
   "quote_ok": bool, "ladder_ok": bool}
"""
from __future__ import annotations

# ── 어림값 임계 (★데이터 검증 전 — 경계선으로만 쓸 것) ────────────────────
MIN_DEPTH_SHARES = 500        # 매수+매도 총잔량이 이 미만이면 '호가가 얇다' → 불균형 무효
WIDE_SPREAD_PCT = 1.0         # 스프레드가 이보다 넓으면 슬리피지 경고
IMBALANCE_STRONG = 0.15       # |불균형| 이 이상이면 '한쪽 우위'로 본다
GAP_CHASE_PCT = 5.0           # 갭이 이보다 크면 추격 금지(회고: 즉시고점 최대 실패모드)


# =====================================================================
# 순수 계산 (네트워크 없음 — 하네스가 검증한다)
# =====================================================================
def book_depth(levels):
    """호가 총잔량. 반환 (bid_total, ask_total). levels 가 없으면 (None, None)."""
    if not levels:
        return None, None
    b = a = 0
    for lv in levels:
        try:
            b += int(lv.get("bid_qty") or 0)
            a += int(lv.get("ask_qty") or 0)
        except (TypeError, ValueError):
            continue
    return b, a


def book_imbalance(levels, min_depth=MIN_DEPTH_SHARES):
    """호가 불균형 = (매수잔량 - 매도잔량) / 총잔량. 범위 -1.0 ~ +1.0.

    반환 (value:float|None, note:str)
    ★두께가 min_depth 미만이면 **None** 을 돌려준다 — 얇은 호가의 불균형은 신호가 아니라
      노이즈다(주문 하나로 튄다). 프리마켓에서 특히 그렇다.
    """
    b, a = book_depth(levels)
    if b is None or a is None:
        return None, "호가 없음"
    tot = b + a
    if tot <= 0:
        return None, "잔량 0"
    if tot < min_depth:
        return None, "호가가 얇다(총 %d주 < %d) — 불균형은 노이즈" % (tot, min_depth)
    v = (b - a) / float(tot)
    if v >= IMBALANCE_STRONG:
        s = "매수 우위"
    elif v <= -IMBALANCE_STRONG:
        s = "매도 우위"
    else:
        s = "균형"
    return round(v, 3), "%s (매수 %d / 매도 %d)" % (s, b, a)


def spread_pct(best_bid, best_ask):
    """스프레드 비율(%) = (매도호가 - 매수호가) / 중간가 × 100. 반환 (v|None, note)."""
    try:
        bb, ba = float(best_bid), float(best_ask)
    except (TypeError, ValueError):
        return None, "최우선 호가 없음"
    if bb <= 0 or ba <= 0 or ba < bb:
        return None, "호가 이상(매도 %s < 매수 %s)" % (best_ask, best_bid)
    mid = (bb + ba) / 2.0
    v = (ba - bb) / mid * 100.0
    note = "넓다 — 슬리피지 주의" if v > WIDE_SPREAD_PCT else "정상"
    return round(v, 3), note


def gap_pct(price, prev_close):
    """전일 종가 대비 갭(%). 반환 (v|None, note)."""
    try:
        p, pc = float(price), float(prev_close)
    except (TypeError, ValueError):
        return None, "가격 없음"
    if pc <= 0:
        return None, "전일 종가 없음"
    v = (p / pc - 1.0) * 100.0
    if v > GAP_CHASE_PCT:
        note = "급등 출발 — ★추격 금지 구간"
    elif v < -GAP_CHASE_PCT:
        note = "급락 출발 — 논지 훼손 여부 확인"
    else:
        note = "정상 범위"
    return round(v, 2), note


def volume_ratio(volume, avg_volume):
    """거래량 / 평소(20일 평균). 반환 (v|None, note)."""
    try:
        v, av = float(volume), float(avg_volume)
    except (TypeError, ValueError):
        return None, "거래량 없음"
    if av <= 0:
        return None, "평균 거래량 없음"
    r = v / av
    if r >= 2.5:
        note = "폭증 — ★F6 관찰: 극단 거래량일 때 적중 낮았다(소표본)"
    elif r >= 1.5:
        note = "증가"
    else:
        note = "평소 수준"
    return round(r, 2), note


def compute(quote_resp, prev_close=None, avg_volume=None):
    """quote 응답 → 지표 묶음. 못 구한 값은 None(추측 금지)."""
    q = (quote_resp or {}).get("quote") or {}
    ld = (quote_resp or {}).get("ladder") or {}
    ladder_ok = bool((quote_resp or {}).get("ladder_ok"))
    quote_ok = bool((quote_resp or {}).get("quote_ok"))

    out = {"quote_ok": quote_ok, "ladder_ok": ladder_ok,
           "price": q.get("price"), "volume": q.get("volume"),
           "high": q.get("high"), "low": q.get("low")}

    g, gn = gap_pct(q.get("price"), prev_close)
    out["gap_pct"], out["gap_note"] = g, gn

    vr, vrn = volume_ratio(q.get("volume"), avg_volume)
    out["volume_ratio"], out["volume_note"] = vr, vrn

    # ★호가 지표는 ladder_ok 일 때만. 아니면 전부 None(빈 값으로 두는 게 정직하다).
    if ladder_ok:
        bi, bin_ = book_imbalance(ld.get("levels"))
        sp, spn = spread_pct(ld.get("best_bid"), ld.get("best_ask"))
        b, a = book_depth(ld.get("levels"))
        out.update({"book_imbalance": bi, "book_imbalance_note": bin_,
                    "spread_pct": sp, "spread_note": spn,
                    "bid_depth": b, "ask_depth": a,
                    "best_bid": ld.get("best_bid"), "best_ask": ld.get("best_ask")})
    else:
        out.update({"book_imbalance": None,
                    "book_imbalance_note": "ladder_ok=false — 호가 판독 실패, 지표 산출 안 함",
                    "spread_pct": None, "spread_note": "판독 실패",
                    "bid_depth": None, "ask_depth": None,
                    "best_bid": None, "best_ask": None})
    return out


# =====================================================================
# 계획 대조 — ★지표는 '막는 쪽'으로만 강하게 쓴다
# =====================================================================
def evaluate(plan_item, sig):
    """매매계획 1건 + 지표 → 판정.

    반환 {"verdict": "go"|"hold"|"avoid", "reasons": [...], "blocks": [...]}

    [★설계 원칙 — 왜 지표가 'go' 를 만들지 못하나]
      지표가 좋다고 사는 것은 '오늘 아침 분석이 검토하지 않은 이유로 사는 것'이다.
      진입 근거는 언제나 **계획의 thesis** 이고, 지표는 그 계획을 **실행할 수 없는 상태인지**
      를 판별한다. 그래서 blocks 가 하나라도 있으면 go 가 될 수 없고,
      blocks 가 없어도 계획이 요구한 조건(require)을 못 채우면 hold 다.
    """
    reasons, blocks = [], []
    verdict = "hold"

    if not sig.get("quote_ok"):
        blocks.append("현재가 판독 실패(quote_ok=false) — 값을 믿을 수 없다")

    gap = sig.get("gap_pct")
    # 1) 추격 금지 — 회고 최대 실패모드(즉시고점)의 직접 방어
    limit = plan_item.get("avoid_gap_above_pct")
    limit = GAP_CHASE_PCT if limit is None else float(limit)
    if gap is not None and gap > limit:
        blocks.append("갭 %+.1f%% > 추격 한계 %.1f%% — 추격 금지" % (gap, limit))
    # 2) 급락 출발이면 논지부터 재확인
    down_limit = plan_item.get("recheck_gap_below_pct")
    down_limit = -GAP_CHASE_PCT if down_limit is None else float(down_limit)
    if gap is not None and gap < down_limit:
        blocks.append("갭 %+.1f%% < %.1f%% — 논지 훼손 여부를 먼저 확인해야 한다"
                      % (gap, down_limit))

    # 3) 호가가 얇거나 스프레드가 넓으면 체결 자체가 위험
    sp = sig.get("spread_pct")
    if sp is not None and sp > WIDE_SPREAD_PCT:
        blocks.append("스프레드 %.2f%% — 슬리피지 위험(얇은 호가)" % sp)
    bd, ad = sig.get("bid_depth"), sig.get("ask_depth")
    if bd is not None and ad is not None and (bd + ad) < MIN_DEPTH_SHARES:
        blocks.append("호가 총잔량 %d주 — 너무 얇다(체결·청산 곤란)" % (bd + ad))

    # 4) 계획이 명시적으로 요구한 조건(있을 때만 — 없으면 갭만으로 판단)
    req = plan_item.get("require") or {}
    bi = sig.get("book_imbalance")
    if "book_imbalance_min" in req:
        need = float(req["book_imbalance_min"])
        if bi is None:
            reasons.append("호가 불균형을 못 구했다(얇거나 판독 실패) — 이 조건은 판정 불가")
            verdict = "hold"
            return {"verdict": "hold", "reasons": reasons, "blocks": blocks}
        if bi < need:
            reasons.append("불균형 %+.3f < 요구 %+.3f" % (bi, need))
        else:
            reasons.append("불균형 %+.3f >= 요구 %+.3f (%s)"
                           % (bi, need, sig.get("book_imbalance_note") or ""))
    if "gap_between" in req:
        lo, hi = req["gap_between"]
        if gap is None:
            reasons.append("갭을 못 구했다 — 판정 불가")
            return {"verdict": "hold", "reasons": reasons, "blocks": blocks}
        if not (float(lo) <= gap <= float(hi)):
            reasons.append("갭 %+.1f%% 이 요구 구간 [%s, %s] 밖" % (gap, lo, hi))
        else:
            reasons.append("갭 %+.1f%% 이 요구 구간 안" % gap)

    if blocks:
        return {"verdict": "avoid", "reasons": reasons, "blocks": blocks}

    # 요구 조건이 하나라도 미충족이면 hold
    unmet = [r for r in reasons if "<" in r or "밖" in r]
    if unmet:
        return {"verdict": "hold", "reasons": reasons, "blocks": blocks}

    # 계획에 요구 조건이 아예 없으면 — 지표만으로 go 를 만들지 않는다
    if not req:
        reasons.append("계획에 정량 조건(require)이 없다 — 지표만으로는 진입하지 않는다"
                       "(사람/코워크가 thesis 로 판단할 것)")
        return {"verdict": "hold", "reasons": reasons, "blocks": blocks}

    return {"verdict": "go", "reasons": reasons, "blocks": blocks}


def describe(sig):
    """사람이 읽는 한 줄. 이모지 금지(cp949)."""
    bits = []
    if sig.get("price") is not None:
        bits.append("현재 %s" % format(int(sig["price"]), ","))
    if sig.get("gap_pct") is not None:
        bits.append("갭 %+.2f%%" % sig["gap_pct"])
    if sig.get("book_imbalance") is not None:
        bits.append("불균형 %+.3f" % sig["book_imbalance"])
    else:
        bits.append("불균형 N/A")
    if sig.get("spread_pct") is not None:
        bits.append("스프레드 %.2f%%" % sig["spread_pct"])
    if sig.get("volume_ratio") is not None:
        bits.append("거래량 %.1fx" % sig["volume_ratio"])
    return " | ".join(bits)
