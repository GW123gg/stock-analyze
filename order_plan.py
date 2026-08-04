#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
order_plan.py — 분석 결과(predictions/holding_review) → **주문 제안** 생성 [v11.9 신규]

[무엇을 하나]
  아침 분석이 낸 픽과 보유 재평가를 읽어, 노트북 계좌의 **실제 예수금·현재가·호가**를 조회해
  "무엇을 · 몇 주 · 얼마에" 낼지 계산해 `order_plan.json` 으로 남긴다.

[★★무엇을 하지 않나 — 설계상 금지]
  - **주문을 체결시키지 않는다.** 이 스크립트는 `arm`(폼 채우기)까지만 하고, 그마저도
    `--arm N` 으로 사람이 한 건씩 지정해야 한다. `fire`(가격 입력·확인패널)는 호출하지 않는다.
  - `live_enabled`·`enforce_cash` 를 건드리지 않는다. 서버가 DRY-RUN 이면 DRY-RUN 인 채로 둔다.
  - 실패해도 재시도하지 않는다(중복 주문 방지).
  → **최종 확인 버튼은 사람이 카이로스 앞에서 누른다.** 이 파일은 그 앞까지만 준비한다.

[왜 필요한가]
  지금까지 분석은 "셀트리온 182,200 에 8% 비중"까지 말했지만, 그걸 **실제 주문 수량·호가로
  옮기는 계산은 사람 머릿속**에 있었다. 예수금이 얼마인지, 한도(10주·200만원)에 걸리는지,
  현재 호가가 진입가에서 얼마나 벌어졌는지를 매번 손으로 맞추면 틀린다.

[사용법]
  python order_plan.py                    # 오늘 세션 기준 제안 생성(계좌 조회 포함)
  python order_plan.py --dry              # 계좌·시세 조회 없이 predictions 만으로 초안
  python order_plan.py --arm 2            # 2번 제안을 폼에 채운다(★확인은 사람이)
  python order_plan.py --session output\2026-08-05_063000

[출력] 세션폴더/order_plan.json + 콘솔 표(이모지 금지 — cp949)
"""
import os
import sys
import json
import glob
import argparse
import logging
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")

logging.basicConfig(level=logging.INFO, format="[plan] %(message)s")
log = logging.getLogger("plan")

# 서버 한도와 동일해야 의미가 있다(노트북 config.toml). 서버가 최종 판정하지만,
# 여기서 미리 걸러 '어차피 거부될 제안'을 사람에게 보이지 않는다.
MAX_ORDER_QTY = 10
MAX_NOTIONAL_KRW = 2_000_000
PRICE_BAND_PCT = 5.0


# =====================================================================
# 순수 계산 (네트워크 없음 — 하네스가 이 함수들을 검증한다)
# =====================================================================
def plan_price(entry_window, entry_ref, last_price, best_bid=None, best_ask=None,
               phase="intraday"):
    """진입 시점 선언(entry_window)에 맞는 지정가를 정한다.

    반환: (price:int|None, note:str)  — price None 이면 '오늘 낼 주문이 아님'.

    [규율] entry_ref 는 분석가가 "여기서 사겠다"고 **선언한 가격**이다. 시장이 그 아래로
      오면 그 가격에 사는 것이고, 이미 위로 달아났으면 추격하지 않는다(즉시고점 실패의 원인).
    """
    ew = (entry_window or "").strip()
    er = int(entry_ref) if entry_ref else None
    lp = int(last_price) if last_price else None

    if ew == "익일이후":
        return None, "익일이후 — 오늘 주문 대상 아님"
    if er is None:
        return None, "entry_ref 없음 — 가격 산출 불가"

    if ew == "당일시가":
        if phase != "pre_open":
            return None, "당일시가 선언인데 장이 이미 열렸다 — 시점 지남(추격 금지)"
        return er, "시가 지정가 = 선언가"

    if ew == "당일종가":
        if phase != "after_close" and phase != "close_auction":
            return None, "당일종가 선언 — 종가 단일가 구간(15:20~)에만 유효"
        return er, "종가 지정가 = 선언가"

    if ew == "당일눌림":
        if lp is None:
            return er, "현재가 미확인 — 선언가로 지정가 대기"
        if lp <= er:
            # 이미 눌림이 왔다 — 선언가 이하이므로 그 자리에서 산다.
            price = min(er, best_ask) if best_ask else er
            return int(price), "눌림 도달(현재 %d <= 선언 %d) — 지정가 매수" % (lp, er)
        gap = (lp / er - 1.0) * 100.0
        if gap > PRICE_BAND_PCT:
            return None, ("현재가가 선언가보다 %+.1f%% 위 — 가격밴드(±%.0f%%) 밖, "
                          "추격 금지" % (gap, PRICE_BAND_PCT))
        return er, "눌림 대기 지정가(현재 %d, 선언 %d, %+.1f%%)" % (lp, er, gap)

    return er, "entry_window 미지정 — 선언가로 지정가"


def plan_qty(cash, size_pct, price, max_qty=MAX_ORDER_QTY,
             max_notional=MAX_NOTIONAL_KRW, fee_rate=0.00015):
    """예수금·비중·한도로 주문 수량을 정한다.

    반환: (qty:int, note:str) — qty 0 이면 못 낸다(사유는 note).
    ★보수적으로: 수수료를 얹은 필요금액이 예수금을 넘지 않을 때까지 줄인다.
    """
    if not price or price <= 0:
        return 0, "가격 없음"
    if cash is None:
        return 0, "예수금 미확인"
    budget = float(cash) * (float(size_pct or 0) / 100.0)
    if budget <= 0:
        return 0, "size_pct 0 또는 미지정"

    qty = int(budget // price)
    caps = []
    if qty > max_qty:
        qty, _ = max_qty, caps.append("수량한도 %d주" % max_qty)
    while qty > 0 and qty * price > max_notional:
        qty -= 1
        if "금액한도" not in "".join(caps):
            caps.append("금액한도 %s원" % format(max_notional, ","))
    # 수수료 포함 실제 필요금액이 예수금을 넘지 않게
    while qty > 0 and qty * price * (1.0 + fee_rate) > float(cash):
        qty -= 1
        if "예수금" not in "".join(caps):
            caps.append("예수금")

    if qty <= 0:
        return 0, ("예수금 %s원으로 %s원짜리 1주도 못 산다"
                   % (format(int(cash), ","), format(int(price), ",")))
    note = "비중 %.0f%% -> %d주" % (float(size_pct or 0), qty)
    if caps:
        note += " (제한: %s)" % ", ".join(caps)
    return qty, note


def sell_signal(level_flags, ret_pct=None, horizon_expired=False):
    """보유 재평가의 level_flags → 매도 사유. 반환 (action:str|None, reason:str).

    ★판단 근거는 **픽이 스스로 선언한 계약**(목표·손절)이지 새 임계값이 아니다.
    """
    lf = level_flags or {}
    if lf.get("currently_below_stop"):
        return "손절", "현재가가 손절선 아래(계약 위반 상태)"
    if lf.get("target_hit"):
        return "익절", "목표가 도달"
    if lf.get("trailing_hit"):
        return "트레일링청산", "고점 대비 트레일링 스탑 도달"
    if lf.get("partial_take_hit"):
        return "분할익절", "분할익절 지점 도달"
    if horizon_expired:
        return "만기청산", "보유기간(horizon) 경과"
    return None, ""


def validate_proposal(p, mode="DRYRUN"):
    """제안 1건의 자체 검증. 반환: 문제 문자열 리스트(비면 통과).
    ★서버가 최종 판정하지만, 어차피 422 날 것을 사람에게 보여주지 않으려는 사전 필터."""
    errs = []
    qty, price = p.get("qty") or 0, p.get("price") or 0
    if qty <= 0:
        errs.append("수량 0")
    if qty > MAX_ORDER_QTY:
        errs.append("수량한도 초과(%d > %d)" % (qty, MAX_ORDER_QTY))
    if price <= 0:
        errs.append("가격 0")
    if qty * price > MAX_NOTIONAL_KRW:
        errs.append("금액한도 초과(%s > %s)"
                    % (format(qty * price, ","), format(MAX_NOTIONAL_KRW, ",")))
    lp = p.get("last_price")
    if lp:
        gap = abs(price / float(lp) - 1.0) * 100.0
        if gap > PRICE_BAND_PCT:
            errs.append("가격밴드 초과(현재가 대비 %+.1f%%)" % gap)
    else:
        errs.append("last_price 없음 — 가격밴드 검사 불가(서버도 건너뛴다)")
    if p.get("side") == "sell" and not p.get("sellable_qty"):
        errs.append("보유수량 미확인 — 매도 불가")
    return errs


# =====================================================================
# 세션·입력
# =====================================================================
def find_session(explicit=None):
    if explicit:
        return explicit if os.path.isdir(explicit) else None
    today = datetime.now().strftime("%Y-%m-%d")
    cands = sorted(glob.glob(os.path.join(OUTPUT_DIR, today + "_*")))
    return cands[-1] if cands else None


def _load(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        return None


def session_phase(now_hhmm=None):
    """장 국면 — intraday_review 와 같은 정의를 재사용(중복 구현 금지)."""
    try:
        import intraday_review as ir
        return ir.session_phase(now_hhmm)
    except Exception:
        hm = now_hhmm or datetime.now().strftime("%H:%M")
        if hm < "09:00":
            return "pre_open"
        if hm < "15:30":
            return "intraday"
        return "after_close"


# =====================================================================
# 제안 생성
# =====================================================================
def build_proposals(preds, holding, account, quotes, phase="intraday"):
    """순수 조립 — 네트워크 호출 없음(quotes 는 미리 받아둔 dict).

    quotes: {ticker: {"price":int, "best_bid":int|None, "best_ask":int|None,
                      "quote_ok":bool, "ladder_ok":bool}}
    account: {"cash": int, "holdings": [{"ticker","qty"}...]}
    """
    cash = (account or {}).get("cash")
    # ★'보유를 아는가'와 '보유가 없다'를 구분한다 — 계좌를 못 읽었는데 '없음'으로 처리하면
    #   실제 보유 중인 종목의 손절 신호를 조용히 삼킨다(이 시스템에서 가장 비싼 침묵).
    held_known = isinstance(account, dict) and "holdings" in account
    held = {}
    for h in (account or {}).get("holdings") or []:
        t = str(h.get("ticker") or h.get("symbol") or "")
        if t:
            held[t] = int(h.get("qty") or h.get("quantity") or 0)

    out = []
    unheld_signals = []          # 신호는 났으나 계좌에 없어 낼 수 없는 매도

    # ── 매수: 오늘 픽 ──
    for it in (preds or {}).get("picks") or []:
        tk = str(it.get("ticker") or "")
        if not tk:
            continue
        q = (quotes or {}).get(tk) or {}
        lp = q.get("price")
        price, pnote = plan_price(it.get("entry_window"), it.get("entry_ref"), lp,
                                  q.get("best_bid"), q.get("best_ask"), phase=phase)
        if price is None:
            out.append({"side": "buy", "ticker": tk, "name": it.get("name"),
                        "qty": 0, "price": None, "last_price": lp,
                        "skip": True, "note": pnote, "source": "pick"})
            continue
        qty, qnote = plan_qty(cash, it.get("size_pct"), price)
        p = {"side": "buy", "ticker": tk, "name": it.get("name"),
             "qty": qty, "price": int(price), "last_price": lp,
             "entry_window": it.get("entry_window"), "entry_ref": it.get("entry_ref"),
             "target_pct": it.get("target_pct"), "stop_pct": it.get("stop_pct"),
             "size_pct": it.get("size_pct"), "conviction": it.get("conviction"),
             "quote_ok": q.get("quote_ok"), "ladder_ok": q.get("ladder_ok"),
             "note": "%s / %s" % (pnote, qnote), "source": "pick", "skip": qty <= 0}
        p["problems"] = validate_proposal(p)
        out.append(p)

    # ── 매도: 보유 재평가 신호 ──
    seen = set()
    for h in (holding or {}).get("holdings") or []:
        tk = str(h.get("ticker") or "")
        if not tk or tk in seen:
            continue
        action, reason = sell_signal(h.get("level_flags"), h.get("ret_pct"),
                                     h.get("horizon_expired"))
        if not action:
            continue
        seen.add(tk)
        sellable = held.get(tk, 0)
        # 계좌를 읽었고 그 종목을 안 갖고 있으면 '낼 수 없는 주문'이다 —
        # 제안 목록에 넣지 않고 아래 요약 한 줄로만 센다(만기 지난 옛 추천이 수십 건이라
        # 전부 나열하면 정작 실행 가능한 제안이 묻힌다).
        if held_known and sellable <= 0:
            unheld_signals.append({"ticker": tk, "name": h.get("name"),
                                   "action": action, "reason": reason})
            continue
        q = (quotes or {}).get(tk) or {}
        lp = q.get("price") or h.get("last_close")
        # 매도 지정가: 최우선 매수호가(즉시 체결 쪽). 호가 판독 실패면 현재가.
        price = q.get("best_bid") if q.get("ladder_ok") else lp
        p = {"side": "sell", "ticker": tk, "name": h.get("name"),
             "qty": int(sellable), "price": int(price) if price else None,
             "last_price": lp, "sellable_qty": sellable,
             "action": action, "reason": reason, "ret_pct": h.get("ret_pct"),
             "quote_ok": q.get("quote_ok"), "ladder_ok": q.get("ladder_ok"),
             "note": "%s — %s" % (action, reason), "source": "holding",
             "skip": sellable <= 0}
        if sellable <= 0:
            p["note"] += " (계좌에 보유 없음 — 추천 이력상 신호일 뿐)"
        p["problems"] = validate_proposal(p)
        out.append(p)

    return out, unheld_signals


# =====================================================================
# 실행
# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="주문 제안 생성(★체결하지 않음)")
    ap.add_argument("--session", default=None)
    ap.add_argument("--dry", action="store_true",
                    help="계좌·시세 조회 없이 predictions 만으로 초안(네트워크 없음)")
    ap.add_argument("--arm", type=int, default=None, metavar="N",
                    help="N번 제안을 폼에 채운다(★가격 입력·확인은 하지 않는다)")
    args = ap.parse_args()

    sess = find_session(args.session)
    if not sess:
        log.error("오늘 세션을 찾지 못했다 — --session 으로 지정하라.")
        return 1
    log.info("세션: %s", sess)

    preds = _load(os.path.join(sess, "predictions.json")) or {}
    holding = _load(os.path.join(sess, "holding_review.json")) or {}
    phase = session_phase()
    log.info("국면: %s | 픽 %d건 | 보유 재평가 %d행",
             phase, len(preds.get("picks") or []), len((holding.get("holdings") or [])))

    account, quotes, st = {}, {}, None
    if not args.dry:
        try:
            import trade_client as tc
        except Exception as e:
            log.error("trade_client import 실패: %s", e)
            return 1
        if not tc.load_token():
            log.error("kairos_trade_api.txt 에 토큰이 없다 — 계좌·시세를 못 읽는다.")
            log.error("  --dry 로 초안만 만들거나, 토큰 파일을 먼저 채워라.")
            return 1
        try:
            st = tc.status()
            log.info("서버: %s", tc.describe_status(st))
        except Exception as e:
            log.error("서버 상태 조회 실패: %s", e)
            return 1
        # ★안전 게이트: 킬스위치·긴급정지 중이면 제안 자체를 만들지 않는다.
        if st.get("killswitch") or st.get("emergency_stop"):
            log.error("킬스위치/긴급정지 상태 — 제안을 만들지 않는다(해제는 사용자가).")
            return 1
        try:
            account = tc.account() or {}
        except Exception as e:
            log.warning("계좌 조회 실패(예수금 미확인으로 진행): %s", e)
        tickers = [str(p.get("ticker")) for p in (preds.get("picks") or []) if p.get("ticker")]
        for tk in tickers:
            try:
                q = tc.quote(tk)
                qq = q.get("quote") or {}
                ld = q.get("ladder") or {}
                quotes[tk] = {"price": qq.get("price"),
                              "best_bid": ld.get("best_bid"), "best_ask": ld.get("best_ask"),
                              "quote_ok": q.get("quote_ok"), "ladder_ok": q.get("ladder_ok")}
                log.info("  시세 %s: %s (quote_ok=%s ladder_ok=%s)",
                         tk, qq.get("price"), q.get("quote_ok"), q.get("ladder_ok"))
            except Exception as e:
                log.warning("  시세 %s 실패: %s", tk, e)

    props, unheld = build_proposals(preds, holding, account, quotes, phase=phase)

    # ── 출력 ──
    print("")
    print("=" * 96)
    print("주문 제안 (%s, 국면=%s) — ★체결하지 않음. 확인 버튼은 사람이 누른다."
          % (datetime.now().strftime("%Y-%m-%d %H:%M"), phase))
    print("=" * 96)
    print("%-3s %-5s %-7s %-12s %8s %10s %12s  %s"
          % ("#", "구분", "티커", "종목", "수량", "지정가", "금액", "비고"))
    print("-" * 96)
    for i, p in enumerate(props, 1):
        amt = (p.get("qty") or 0) * (p.get("price") or 0)
        mark = "  " if not p.get("skip") else "--"
        print("%-3s %-5s %-7s %-12s %8s %10s %12s  %s"
              % (str(i) + mark, "매수" if p["side"] == "buy" else "매도",
                 p["ticker"], (p.get("name") or "")[:11],
                 p.get("qty") or 0, format(p.get("price") or 0, ","),
                 format(amt, ","), p.get("note", "")[:44]))
        if p.get("problems"):
            print("      ! " + " / ".join(p["problems"]))
    print("-" * 96)
    live = [p for p in props if not p.get("skip") and not p.get("problems")]
    print("실행 가능 제안: %d건 / 전체 %d건" % (len(live), len(props)))
    if unheld:
        print("매도 신호 %d건은 계좌에 보유가 없어 제외: %s"
              % (len(unheld), ", ".join("%s(%s)" % (u["name"] or u["ticker"], u["action"])
                                        for u in unheld[:6])
                 + (" 외" if len(unheld) > 6 else "")))
    if st:
        print("서버 모드: %s  (DRYRUN 이면 폼만 채우고 버튼은 안 눌린다)"
              % (st.get("mode") or ("LIVE" if st.get("live_enabled") else "DRYRUN")))
    print("※ 다음 단계는 `--arm N` — 폼에 채우기까지만 한다. 가격 입력·확인은 사람이.")
    print("")

    # ── 저장 ──
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "session": os.path.basename(sess),
        "phase": phase,
        "what": ("주문 제안. ★실행 기록이 아니다 — 체결 여부는 카이로스 주문내역이 전거."
                 " 확인 버튼은 사람이 누른다."),
        "server_mode": (st or {}).get("mode"),
        "cash": (account or {}).get("cash"),
        "n_proposals": len(props),
        "n_actionable": len(live),
        "proposals": props,
        "unheld_sell_signals": unheld,
        "retro_use": "forbidden_as_pre_feature",   # 회고가 진입 피처로 쓰지 못하게
    }
    try:
        from common import save_json_atomic
        save_json_atomic(os.path.join(sess, "order_plan.json"), payload)
        log.info("저장: %s", os.path.join(sess, "order_plan.json"))
    except Exception as e:
        log.warning("저장 실패: %s", e)

    # ── arm(폼 채우기) ──
    if args.arm is not None:
        if args.dry:
            log.error("--dry 에서는 arm 할 수 없다(계좌·시세 미확인).")
            return 1
        if not (1 <= args.arm <= len(props)):
            log.error("제안 번호 범위 밖: %d (1~%d)", args.arm, len(props))
            return 1
        p = props[args.arm - 1]
        if p.get("skip") or p.get("problems"):
            log.error("이 제안은 실행 대상이 아니다: %s / %s",
                      p.get("note"), p.get("problems"))
            return 1
        import trade_client as tc
        log.info("arm: %s %s %d주 (가격 %s 는 폼에 넣지 않는다)",
                 p["side"], p["ticker"], p["qty"], format(p["price"], ","))
        try:
            r = tc.arm(p["ticker"], p["side"], p["qty"])
        except Exception as e:
            log.error("arm 실패: %s", e)
            log.error("★재시도하지 마라 — 원인을 확인하고 사람이 판단할 것.")
            return 1
        print(json.dumps(r, ensure_ascii=False, indent=2)[:800])
        print("")
        print("다음: 카이로스 화면에서 폼을 눈으로 확인하라(/snapshot.jpg).")
        print("      가격 입력·주문 확인은 **사람이** 한다. 이 스크립트는 여기서 끝난다.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
