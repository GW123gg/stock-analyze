#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/run_tests.py ─ 순수함수 골든 테스트 하네스 (H-4)

[왜] 2026-07-17 감사에서 CRITICAL 2건(LABEL_COLS 미등록·스냅샷 피처 미전달)이 '컴파일 통과 후
  라이브 실행'에서야 잡혔다. 순수함수는 테스트 비용이 거의 0인데 리스크는 최대다(회고 데이터셋·발송 게이트).
[원칙]
  - 의존성 0(pytest 불필요), 네트워크 0(무거운 외부호출은 몽키패치), 라이브 파일 무수정(tmp만).
  - 실패 시 exit 1 — 검증 게이트 0단계로 py_compile 앞에 실행한다.
  - 콘솔 ASCII 태그만(cp949), 한글 메시지 OK(UTF-8 reconfigure).
[사용법]  python tests/run_tests.py        (~5초, KRX 로그인 출력은 flow_collect import 부수효과 — 무해)
[추가 규칙] 새 라벨/피처를 추가하면 반드시: (1) LABEL_COLS/FEATURE_COLS/PRE_COLS 등록
  (2) 여기 test_label_cols_complete 가 자동 검증하니 실행만 하면 된다.
"""
import io
import os
import sys
import json
import shutil
import tempfile
from datetime import date, datetime, timedelta

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

_FAILS = []
_N = [0]


def check(name, cond, detail=""):
    _N[0] += 1
    if cond:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name} {detail}")
        _FAILS.append(name)


# =====================================================================
# 1. common.validate_predictions — 발송 게이트 계약([7.5])
# =====================================================================
def test_validate_predictions():
    from common import validate_predictions as v
    ok_pick = {"ticker": "005930", "tag": "단기스윙", "timing": "단기", "conviction": 0.5,
               "preprice": "부분", "entry_ref": 70000, "horizon_days": 5,
               # v10.1 시간축 전망(픽 필수)
               "path_view": "눌림후상승", "expected_peak_days": 4,
               # v11.3 진입 시점(픽 필수 — '못 사는 추천' 방지)
               "entry_window": "당일눌림"}
    ok_short = {"ticker": "000660", "timing": "단기", "conviction": 0.5,
                "entry_ref": 100000, "horizon_days": 5}
    check("validate: 정상 픽+숏 통과", v({"picks": [ok_pick], "shorts": [ok_short]}) == [])
    check("validate: 관망일(픽0·숏0) 통과 — 라이브 발송 보존",
          v({"picks": [], "shorts": [], "market_call": {}}) == [])
    check("validate: 숏은 preprice 없어도 통과([7.5] 계약)",
          v({"picks": [], "shorts": [ok_short]}) == [])
    bad = dict(ok_pick, timing=None)
    check("validate: timing null 차단", any("timing" in e for e in v({"picks": [bad]})))
    bad = dict(ok_pick, timing="곧")
    check("validate: timing enum 차단", any("임박/단기/중기" in e for e in v({"picks": [bad]})))
    bad = dict(ok_pick, conviction="높음")
    check("validate: conviction 비숫자 차단", any("숫자가 아님" in e for e in v({"picks": [bad]})))
    bad = dict(ok_pick, conviction=1.5)
    check("validate: conviction 범위 차단", any("범위 밖" in e for e in v({"picks": [bad]})))
    bad = dict(ok_pick, entry_ref=-1)
    check("validate: entry_ref 음수 차단", any("양수가 아님" in e for e in v({"picks": [bad]})))
    bad = dict(ok_pick, preprice="조금")
    check("validate: preprice enum 차단", any("강함/부분/미반영" in e for e in v({"picks": [bad]})))
    bad = dict(ok_pick, tag=None)
    check("v9.7: tag null 차단(무태그 7회 관찰 근절)", any("'tag'" in e for e in v({"picks": [bad]})))
    bad = dict(ok_pick, tag="스윙")
    check("v9.7: tag enum 차단", any("단기스윙/장투가능/장전선취매" in e for e in v({"picks": [bad]})))
    check("v9.7: 숏은 tag 없어도 통과(픽 전용)", v({"picks": [], "shorts": [ok_short]}) == [])
    check("validate: 비dict 차단", v("깨짐") != [])
    # ── v9.6 확률 예보(월가식 개편) + v9.7 게이트 강화(리뷰 [4]) ──
    base_mc = {"picks": [], "shorts": [ok_short]}
    good_mc = dict(base_mc, market_call={"kospi": {"dir": "down", "conviction": 0.6,
                   "prob_up": 0.2, "prob_flat": 0.2, "prob_down": 0.6}})
    check("v9.6: 정상 확률(합1·argmax=dir) 통과", v(good_mc) == [], str(v(good_mc)))
    check("v9.7: prob 없는 콜 차단(필수화 — 구 하위호환 폐지)",
          any("prob_up/flat/down 누락" in e
              for e in v(dict(base_mc, market_call={"kospi": {"dir": "up", "conviction": 0.5}}))))
    check("v9.7: market_call 자체가 없으면 통과(관망일 호환)",
          v(dict(base_mc, market_call={})) == [])
    bad = dict(base_mc, market_call={"kospi": {"dir": "down", "conviction": 0.75,
               "prob_up": 0.2, "prob_flat": 0.2, "prob_down": 0.6}})
    check("v9.7: conviction!=max(prob) 차단(±0.05)", any("max(prob)" in e for e in v(bad)))

    # ── v11.4 파생상품([6.8]) — 레버리지 상품이라 게이트를 픽보다 엄격히 ──
    _dv = {"kind": "futures", "underlying": "KOSPI200", "contract": "2026-09", "side": "short",
           "entry_ref": 956.75, "target_pct": 3, "stop_pct": -2, "conviction": 0.55,
           "horizon_days": 5, "max_loss_krw": 1200000,
           "leverage_note": "정규 선물 1계약=지수x25만원", "thesis": "x"}
    check("v11.4: 정상 선물 통과", v({"derivatives": [_dv]}) == [], str(v({"derivatives": [_dv]})))
    check("v11.4: derivatives 없거나 비면 통과(파생 안 하는 게 기본)",
          v({"derivatives": []}) == [] and v({}) == [])
    # ★★옵션 매도 금지 — 손실 무한이라 설정으로도 못 푼다
    check("v11.4: 콜 매도 차단",
          any("옵션 매도 금지" in e
              for e in v({"derivatives": [dict(_dv, kind="call", side="short", strike=1000)]})))
    check("v11.4: 풋 매도 차단",
          any("옵션 매도 금지" in e
              for e in v({"derivatives": [dict(_dv, kind="put", side="short", strike=900)]})))
    check("v11.4: 콜 매수는 허용",
          v({"derivatives": [dict(_dv, kind="call", side="long", strike=1000)]}) == [])
    check("v11.4: 옵션 strike 누락 차단",
          any("strike" in e for e in v({"derivatives": [dict(_dv, kind="put", side="long")]})))
    # 레버리지 인지 강제
    check("v11.4: max_loss_krw 누락 차단",
          any("max_loss_krw 필수" in e for e in v({"derivatives": [
              {k: x for k, x in _dv.items() if k != "max_loss_krw"}]})))
    check("v11.4: leverage_note 누락 차단",
          any("leverage_note" in e for e in v({"derivatives": [dict(_dv, leverage_note="")]})))
    check("v11.4: kind enum 차단",
          any("kind" in e for e in v({"derivatives": [dict(_dv, kind="stock_futures")]})))
    check("v11.4: 파생 horizon 40 차단(만기가 있다)",
          any("1|5|20" in e for e in v({"derivatives": [dict(_dv, horizon_days=40)]})))
    check("v11.4: conviction 0.8 초과 차단",
          any("conviction" in e for e in v({"derivatives": [dict(_dv, conviction=0.9)]})))
    check("v11.4: stop_pct 양수 차단",
          any("stop_pct" in e for e in v({"derivatives": [dict(_dv, stop_pct=2)]})))
    check("v11.4: 선물 short 는 허용(헤지·하락 베팅)",
          v({"derivatives": [dict(_dv, kind="mini_futures", side="short")]}) == [])

    # ── v11.3 진입 시점 + 픽 익일 전망([6.5++]) — '추천했는데 못 사는' 문제 ──
    #   회고 실측: 픽의 절반이 D+1~2 즉시고점(적중 19%), 손실 주범은 '진입가가 곧 고점'형.
    check("v11.3: entry_window 필수(누락 차단)",
          any("entry_window" in e for e in v({"picks": [
              {k: val for k, val in ok_pick.items() if k != "entry_window"}]})))
    check("v11.3: entry_window enum 차단",
          any("entry_window" in e and "중 하나" in e
              for e in v({"picks": [dict(ok_pick, entry_window="아무때나")]})))
    for _w in ("당일시가", "당일눌림", "당일종가", "익일이후"):
        check("v11.3: entry_window '%s' 허용" % _w,
              v({"picks": [dict(ok_pick, entry_window=_w)]}) == [])
    # ★정합성: 즉시상승인데 익일이후 진입은 모순
    check("v11.3: 즉시상승+익일이후 모순 차단",
          any("모순" in e for e in v({"picks": [
              dict(ok_pick, path_view="즉시상승", entry_window="익일이후")]})))
    check("v11.3: 즉시상승+당일시가는 정상",
          v({"picks": [dict(ok_pick, path_view="즉시상승", entry_window="당일시가")]}) == [])

    _pnd = {"dir": "up", "prob_up": 0.55, "prob_flat": 0.25, "prob_down": 0.20,
            "expected_pct": 2.5, "reason": "장 마감 강세 + 외인 순매수 전환"}
    check("v11.3: 픽 next_day 정상 통과",
          v({"picks": [dict(ok_pick, next_day=_pnd)]}) == [],
          str(v({"picks": [dict(ok_pick, next_day=_pnd)]})))
    check("v11.3: 픽 next_day 없어도 통과(선택)", v({"picks": [ok_pick]}) == [])
    _drop2 = {k: val for k, val in _pnd.items() if k != "prob_down"}
    check("v11.3: 픽 next_day prob 일부 누락 차단",
          any("next_day 를 넣었으면" in e for e in v({"picks": [dict(ok_pick, next_day=_drop2)]})))
    check("v11.3: 픽 next_day prob 합!=1 차단",
          any("next_day prob 합" in e
              for e in v({"picks": [dict(ok_pick, next_day=dict(_pnd, prob_up=0.9))]})))
    check("v11.3: 픽 next_day dir!=argmax 차단",
          any("argmax" in e
              for e in v({"picks": [dict(ok_pick, next_day=dict(_pnd, dir="down"))]})))
    check("v11.3: 픽 next_day 가격제한폭(±30%) 밖 차단",
          any("가격제한폭" in e
              for e in v({"picks": [dict(ok_pick, next_day=dict(_pnd, expected_pct=45))]})))
    check("v11.3: 상한가 수준(+29%)은 허용(물리적으로 가능)",
          v({"picks": [dict(ok_pick, next_day=dict(_pnd, expected_pct=29))]}) == [])

    # ── v11.1 익일(T+1) 지수 예측(사용자 요청: 하루 뒤 코스피·코스닥이 어떻게 될지) ──
    #   [왜] 기존엔 확률 1세트를 T+1·T+5 양쪽에 채점해 둘 다 놓쳤다(실측 25%/27%).
    #   next_day 는 선택이지만 넣었으면 형식은 엄격히 — 반쯤 채운 예측이 영구 미채점되는 걸 막는다.
    _nd_ok = {"dir": "down", "prob_up": 0.25, "prob_flat": 0.20, "prob_down": 0.55,
              "expected_pct": -0.8, "range_low": 6400, "range_high": 6700, "driver": "야간선물 -0.9%"}
    _mc_nd = dict(base_mc, market_call={"kospi": dict(
        {"dir": "down", "conviction": 0.6, "prob_up": 0.2, "prob_flat": 0.2, "prob_down": 0.6},
        next_day=_nd_ok)})
    check("v11.1: 정상 next_day 통과", v(_mc_nd) == [], str(v(_mc_nd)))
    check("v11.1: next_day 없어도 통과(도입 전 세션 호환)", v(good_mc) == [])

    def _nd(**kw):
        return dict(base_mc, market_call={"kospi": dict(
            {"dir": "down", "conviction": 0.6, "prob_up": 0.2, "prob_flat": 0.2, "prob_down": 0.6},
            next_day=dict(_nd_ok, **kw))})
    _drop = dict(_nd_ok)
    _drop.pop("prob_down")
    _mc_drop = dict(base_mc, market_call={"kospi": dict(
        {"dir": "down", "conviction": 0.6, "prob_up": 0.2, "prob_flat": 0.2, "prob_down": 0.6},
        next_day=_drop)})
    check("v11.1: next_day prob 일부 누락 차단",
          any("next_day: prob_up/flat/down 누락" in e for e in v(_mc_drop)))
    check("v11.1: next_day prob 합!=1 차단",
          any("next_day: prob 합" in e for e in v(_nd(prob_up=0.5))))
    check("v11.1: next_day 확률 상한 0.75 차단",
          any("next_day: 확률 상한" in e for e in v(_nd(prob_up=0.05, prob_flat=0.10, prob_down=0.85))))
    check("v11.1: next_day dir enum 차단",
          any("next_day: dir" in e for e in v(_nd(dir="상승"))))
    check("v11.1: next_day dir!=argmax 차단",
          any("argmax" in e for e in v(_nd(dir="up"))))
    check("v11.1: expected_pct 비현실값(±15% 초과) 차단",
          any("expected_pct" in e and "비현실" in e for e in v(_nd(expected_pct=-40))))
    check("v11.1: expected_pct 숫자 아니면 차단",
          any("expected_pct" in e and "숫자" in e for e in v(_nd(expected_pct="많이"))))
    check("v11.1: expected_pct 없어도 통과(선택 필드)",
          v(dict(base_mc, market_call={"kospi": dict(
              {"dir": "down", "conviction": 0.6, "prob_up": 0.2, "prob_flat": 0.2, "prob_down": 0.6},
              next_day={"dir": "down", "prob_up": 0.25, "prob_flat": 0.2, "prob_down": 0.55})})) == [])
    bad_h = dict(ok_pick, horizon_days=10)
    check("v9.7: horizon_days enum 차단", any("1|5|20|40" in e for e in v({"picks": [bad_h]})))
    # ── v10.1 시간축 전망(사용자 요청: 얼마 뒤에 오를까 / 단기 조정 / 1~2달) ──
    check("v10.1: horizon 40(장기 2개월) 허용",
          v({"picks": [dict(ok_pick, timing="장기", horizon_days=40, expected_peak_days=30)]}) == [])
    check("v10.1: timing '장기' 허용", v({"picks": [dict(ok_pick, timing="장기")]}) == [])
    check("v10.1: path_view 누락 차단(픽 필수)",
          any("'path_view'" in e for e in v({"picks": [{k: x for k, x in ok_pick.items()
                                                        if k != "path_view"}]})))
    check("v10.1: expected_peak_days 누락 차단(픽 필수)",
          any("expected_peak_days" in e for e in v({"picks": [{k: x for k, x in ok_pick.items()
                                                               if k != "expected_peak_days"}]})))
    check("v10.1: path_view enum 차단",
          any("즉시상승" in e for e in v({"picks": [dict(ok_pick, path_view="급등")]})))
    check("v10.1: expected_peak_days > horizon 차단(채점 창 밖)",
          any("초과" in e for e in v({"picks": [dict(ok_pick, expected_peak_days=9)]})))
    check("v10.1: expected_gain_pct 음수 차단",
          any("양수" in e for e in v({"picks": [dict(ok_pick, expected_gain_pct=-3)]})))
    check("v10.1: expected_pullback_pct 양수 차단",
          any("음수" in e for e in v({"picks": [dict(ok_pick, expected_pullback_pct=5)]})))
    check("v10.1: 정상 경로 필드 전체 통과",
          v({"picks": [dict(ok_pick, expected_gain_pct=8.5, expected_pullback_pct=-4.0)]}) == [])
    check("v10.1: 숏은 경로 필드 없어도 통과(선택)", v({"picks": [], "shorts": [ok_short]}) == [])
    # v9.8 감사 반영 — dir enum·존재 검사, 관망일 market_call 위반 보존
    bad = dict(base_mc, market_call={"kospi": {"dir": "flat",  # 오타(neutral 이어야)
               "prob_up": 0.2, "prob_flat": 0.6, "prob_down": 0.2}})
    check("v9.8: dir enum 오타 차단", any("up/down/neutral" in e for e in v(bad)))
    bad = dict(base_mc, market_call={"kospi": {"prob_up": 0.2, "prob_flat": 0.6, "prob_down": 0.2}})
    check("v9.8: dir 누락 차단", any("up/down/neutral" in e for e in v(bad)))
    watch_bad = {"picks": [], "shorts": [], "market_call": {"kospi": {"dir": "up", "conviction": 0.5}}}
    check("v9.8: 관망일(픽·숏0)에도 market_call prob 누락 차단(구 return [] 폐기 버그)",
          any("prob_up/flat/down 누락" in e for e in v(watch_bad)))
    check("v9.8: 관망일+정상 콜은 통과(발송 보존)",
          v({"picks": [], "shorts": [], "market_call": {"kospi": {"dir": "up", "conviction": 0.6,
             "prob_up": 0.6, "prob_flat": 0.25, "prob_down": 0.15}}}) == [])
    bad = dict(base_mc, market_call={"kospi": {"dir": "down", "prob_up": 0.5, "prob_flat": 0.3, "prob_down": 0.3}})
    check("v9.6: prob 합!=1 차단", any("합" in e for e in v(bad)))
    bad = dict(base_mc, market_call={"kospi": {"dir": "up", "prob_up": 0.2, "prob_flat": 0.2, "prob_down": 0.6}})
    check("v9.6: dir!=argmax(prob) 차단", any("argmax" in e for e in v(bad)))
    bad = dict(base_mc, market_call={"kospi": {"dir": "down", "prob_up": 0.1, "prob_flat": 0.1, "prob_down": 0.8}})
    check("v9.6: 확률 상한 0.75 초과 차단(겸손 규칙)", any("0.75" in e for e in v(bad)))
    ok_rated = dict(ok_pick, rating="매수", rating_action="신규커버", target_price=85000)
    check("v9.6: 커버리지 필드 정상 통과", v({"picks": [ok_rated], "shorts": []}) == [])
    bad_rated = dict(ok_pick, rating="적극매수")
    check("v9.6: rating enum 차단", any("rating" in e for e in v({"picks": [bad_rated], "shorts": []})))


# =====================================================================
# 2. accuracy_tracker._norm_tag — 태그 분할 집계 방지
# =====================================================================
def test_norm_tag():
    from accuracy_tracker import _norm_tag as n
    check("norm_tag: 대괄호 제거", n("[단기스윙]") == "단기스윙")
    check("norm_tag: 무괄호 보존", n("단기스윙") == "단기스윙")
    check("norm_tag: 이중괄호", n("[[장투가능]]") == "장투가능")
    check("norm_tag: 공백+괄호", n("  [장전선취매] ") == "장전선취매")
    check("norm_tag: None -> 빈문자열", n(None) == "")


# =====================================================================
# 3. retro_label.compute_labels — 몽키패치 골든(알파·경로·손절 반사실)
# =====================================================================
def test_compute_labels_golden():
    import retro_label as rl

    # 합성 가격 경로(진입 100): D+1 110(고점) D+2 96 D+3 88(저점, -8% 손절 터치=실제 -12%)
    # D+4 92  D+5 95(만기 -5%)
    base = date(2026, 6, 2)   # 월요일
    days = [base + timedelta(days=i) for i in range(6)]
    closes = [100.0, 110.0, 96.0, 88.0, 92.0, 95.0]
    vols = [1000, 5000, 1200, 3000, 900, 800]

    class _FscStub:
        @staticmethod
        def get_ohlcv_series(ticker, base_date):
            return list(zip(days, closes, vols))

    old_fsc, old_ok = rl.fsc, rl.FSC_OK
    old_ks = rl._KS11_SERIES
    old_flow_ok = rl.FLOW_OK
    try:
        rl.fsc, rl.FSC_OK = _FscStub, True
        rl.FLOW_OK = False                      # 보유기간 수급 라이브 호출 차단(네트워크 0)
        # KOSPI: D-1 종가 100 -> T+5 102 (지수 +2%) -> alpha = -5 - 2 = -7
        # ★v10.5 앵커 교정 반영: 지수 다리는 이제 D-1 종가(종목 entry_ref 와 같은 빈티지)에서
        #   시작한다. 직전 봉이 없으면 None 이 정답이므로 픽스처에 D-1 봉을 명시한다.
        rl._KS11_SERIES = ([(base - timedelta(days=1), 100.0)] +
                           [(d, c) for d, c in zip(days, [100.0, 101.0, 100.5, 99.0, 101.5, 102.0])])

        lab = rl.compute_labels("TEST", base, 100.0, 5)
        check("labels: matured", lab["matured"] is True)
        check("labels: ret_h = -5.0", lab["ret_h_pct"] == -5.0, str(lab["ret_h_pct"]))
        check("labels: peak_gain=+10 / days_to_peak=1",
              lab["peak_gain_pct"] == 10.0 and lab["days_to_peak"] == 1)
        check("labels: days_to_trough=3(저점 88)", lab["days_to_trough"] == 3, str(lab["days_to_trough"]))
        # 저점 후 되돌림: 88 -> max(88,92,95)=95 -> +7.95%
        check("labels: post_trough_rebound ~ +7.95",
              abs(lab["post_trough_rebound_pct"] - 7.95) < 0.02, str(lab["post_trough_rebound_pct"]))
        check("labels: kospi_ret_h=+2 / alpha=-7",
              lab["kospi_ret_h_pct"] == 2.0 and lab["alpha_h_pct"] == -7.0,
              f"{lab['kospi_ret_h_pct']}/{lab['alpha_h_pct']}")
        # 손절 반사실: D+3 -12%가 -8% 를 뚫음 -> '실제 그날 종가' -12.0 반환(이상적 -8 금지 — 과대평가 방지)
        check("labels: ret_if_stop8 = 실제 종가 -12.0(이상적 -8 아님)",
              lab["ret_if_stop8_pct"] == -12.0, str(lab["ret_if_stop8_pct"]))
        # -8/+12 룰: D+1 +10 은 +12 미달 -> D+3 -12 손절
        check("labels: ret_if_stop8_tp12 = -12.0", lab["ret_if_stop8_tp12_pct"] == -12.0,
              str(lab["ret_if_stop8_tp12_pct"]))
        # LABEL_COLS 완전성: compute_labels 반환 키 중 '행에 실려야 할 것'이 등록됐는지 (CRITICAL 재발 방지)
        internal = {"note"}
        missing = [k for k in lab if k not in rl.LABEL_COLS and k not in internal]
        check("labels: 반환키 전부 LABEL_COLS 등록(미등록=조용한 무효화)",
              not missing, str(missing))
        # v9.8 계약: SNAPSHOT_MARKET_COLS ⊆ FEATURE_COLS (여기 없으면 _row_for 루프가 컬럼을
        #   아예 안 실어 무음 no-op — pre_margin_* 3종이 이 방식으로 사라졌던 회귀 재발 차단).
        snap_missing = [c for c in rl.SNAPSHOT_MARKET_COLS if c not in set(rl.FEATURE_COLS)]
        check("labels: SNAPSHOT_MARKET_COLS 전부 FEATURE_COLS 등록(무음 no-op 방지)",
              not snap_missing, str(snap_missing))
        # v10.0 국면 스냅샷 커버리지: 도입일 이전 결측은 '정상', 이후 결측만 '결함'
        cov = rl._snapshot_coverage([
            {"pred_date": "2026-07-15", "pre_regime_kind": None},     # 도입 전 = 정상
            {"pred_date": "2026-07-18", "pre_regime_kind": "공포"},
            {"pred_date": "2026-07-19", "pre_regime_kind": None},     # 도입 후 결측 = 결함
            {"pred_date": "2026-07-20", "pre_regime_kind": "공포"},
        ])
        check("labels: 스냅샷 커버리지 — 정상일 목록",
              cov["dates_with_snapshot"] == ["2026-07-18", "2026-07-20"],
              str(cov["dates_with_snapshot"]))
        check("labels: 스냅샷 커버리지 — 도입 후 누락만 결함으로",
              cov["dates_missing_after_start"] == ["2026-07-19"],
              str(cov["dates_missing_after_start"]))
        check("labels: 스냅샷 커버리지 — 전부 정상이면 빈 목록",
              rl._snapshot_coverage([{"pred_date": "2026-07-20",
                                      "pre_regime_kind": "x"}])["dates_missing_after_start"] == [])
        # ★A19: 메타에 싣는 허용값 enum 이 실제 함수 반환값과 어긋나면 회고가 또 오집계한다.
        _produced = {rl._cap_bucket(v) for v in (200000, 50000, 5000, 100)}
        check("labels: CAP_BUCKETS enum 이 _cap_bucket 실제 반환과 일치",
              _produced == set(rl.CAP_BUCKETS), f"{sorted(_produced)} vs {sorted(rl.CAP_BUCKETS)}")
        # ★A19 v10.0: KOSDAQ GLOBAL 은 코스닥 세그먼트 → 정규화로 분할 집계를 원천 차단
        check("norm_market: KOSDAQ GLOBAL -> KOSDAQ", rl._norm_market("KOSDAQ GLOBAL") == "KOSDAQ")
        check("norm_market: KOSDAQ 유지", rl._norm_market("KOSDAQ") == "KOSDAQ")
        check("norm_market: KOSPI 유지", rl._norm_market("KOSPI") == "KOSPI")
        check("norm_market: KONEX 는 별도 시장이라 보존", rl._norm_market("KONEX") == "KONEX")
        check("norm_market: 공백·None -> None",
              rl._norm_market(None) is None and rl._norm_market("  ") is None)
        check("labels: EXCHANGES enum 이 정규화 결과와 일치(GLOBAL 제외)",
              set(rl.EXCHANGES) == {"KOSPI", "KOSDAQ", "KONEX"}, str(rl.EXCHANGES))

        # 익절 먼저 닿는 경로: D+1 +13% -> tp12 룰이면 D+1 실제 종가 +13 반환
        closes2 = [100.0, 113.0, 108.0, 105.0, 104.0, 103.0]
        _FscStub.get_ohlcv_series = staticmethod(
            lambda t, b: list(zip(days, closes2, vols)))
        lab2 = rl.compute_labels("TEST", base, 100.0, 5)
        check("labels: 익절 반사실 = 실제 종가 +13.0", lab2["ret_if_stop8_tp12_pct"] == 13.0,
              str(lab2["ret_if_stop8_tp12_pct"]))
        check("labels: 손절 미터치 시 반사실 = 만기수익", lab2["ret_if_stop8_pct"] == lab2["ret_h_pct"])
        # A15 캡판: 익절 갭상승(+13)은 +12 로 캡, 손절 경로(-12)는 캡 무관(하방 정직 유지)
        check("labels: A15 캡판 익절 = +12.0(갭상승 +13 캡)", lab2["ret_if_stop8_tp12_cap_pct"] == 12.0,
              str(lab2["ret_if_stop8_tp12_cap_pct"]))
        check("labels: A15 캡판 손절 경로 = 실제 종가 -12.0(캡 미적용)",
              lab["ret_if_stop8_tp12_cap_pct"] == -12.0, str(lab["ret_if_stop8_tp12_cap_pct"]))
    finally:
        rl.fsc, rl.FSC_OK, rl._KS11_SERIES, rl.FLOW_OK = old_fsc, old_ok, old_ks, old_flow_ok


# =====================================================================
# 4. retro_label.pre_entry_features — 세션 스냅샷 우선(#C1)
# =====================================================================
def test_pre_entry_snapshot_first():
    import retro_label as rl
    snap = {"_snap_pre_foreign_5d_eok": -500, "_snap_pre_foreign_20d_eok": -1200,
            "_snap_pre_inst_5d_eok": 30, "_snap_pre_indiv_5d_eok": 470,
            "_snap_pre_foreign_sell_streak": 4,
            "_snap_pre_short_balance_ratio": 2.1, "_snap_pre_short_change_10d": 15.0}
    old_flow, old_short = rl.FLOW_OK, rl.SHORT_OK
    try:
        rl.FLOW_OK = rl.SHORT_OK = False       # 스냅샷이 전부면 라이브 불필요함을 검증
        out = rl.pre_entry_features("005930", date(2026, 7, 1), snap)
        check("pre: 스냅샷 우선 채움", out.get("pre_foreign_5d_eok") == -500
              and out.get("pre_short_change_10d") == 15.0)
        check("pre: streak 전달", out.get("pre_foreign_sell_streak") == 4)
    finally:
        rl.FLOW_OK, rl.SHORT_OK = old_flow, old_short


# =====================================================================
# 5. retro_forward — 복사 검증·버전 정리(tmpdir)
# =====================================================================
def test_retro_forward_helpers():
    import retro_forward as rf
    t = tempfile.mkdtemp(prefix="rt_")
    try:
        src = os.path.join(t, "a.json")
        dst = os.path.join(t, "b.json")
        with open(src, "w", encoding="utf-8") as f:
            f.write('{"x": 1}')
        shutil.copy2(src, dst)
        check("verify_copy: 동일 파일 True", rf._verify_copy(src, dst) is True)
        with open(dst, "ab") as f:
            f.write(b"\x00" * 10)              # NUL 패딩(07-06·07-12 손상 시그니처)
        check("verify_copy: NUL패딩 감지 False", rf._verify_copy(src, dst) is False)

        inbox = os.path.join(t, "inbox")
        os.makedirs(inbox)
        for d in ("2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04",
                  "2026-07-05", "2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09"):
            for fn in rf.PUSH_FILES:
                stem, ext = os.path.splitext(fn)
                open(os.path.join(inbox, f"{stem}_{d}{ext}"), "w").close()
        rf._prune_versions(inbox, keep=7)
        left = sorted(n for n in os.listdir(inbox) if n.startswith("retro_dataset_") and n.endswith(".json"))
        check("prune: 역할별 최근 7개 유지", len(left) == 7 and left[0].endswith("2026-07-03.json"),
              str(left[:2]))
    finally:
        shutil.rmtree(t, ignore_errors=True)


# =====================================================================
# 6. snapshot_signals — 세션 동결(라이브 루트 파일 read-only 사용)
# =====================================================================
def test_snapshot_signals():
    import snapshot_signals as ss
    have = [f for f in ss.ROOT_SIGNALS if os.path.isfile(os.path.join(BASE, f))]
    if not have:
        print("[SKIP] snapshot: 루트 신호 없음(키 미설정 환경)")
        return
    t = tempfile.mkdtemp(prefix="ss_")
    try:
        done = ss.snapshot(t, check_only=False)
        check("snapshot: 존재 신호 전부 동결", len(done) == len(have), f"{len(done)}/{len(have)}")
        p = os.path.join(t, ss.PREFIX + have[0])
        d = json.load(open(p, encoding="utf-8"))
        check("snapshot: 메타(_snapshot_of/_snapshot_at) 부착",
              d.get("_snapshot_of") == have[0] and "_snapshot_at" in d)
    finally:
        shutil.rmtree(t, ignore_errors=True)


# =====================================================================
# 6.5 resolve_session — 같은 날 다중 세션(데몬+온디맨드) 안전 선택 (v9.9)
# =====================================================================
def test_resolve_session_multisession():
    import time
    from datetime import datetime
    from common import resolve_session
    t = tempfile.mkdtemp(prefix="rs_")
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        early = os.path.join(t, today + "_063115")   # 먼저 생성(채택 세션)
        late = os.path.join(t, today + "_063500")    # 나중 생성(orphan)
        os.makedirs(early); os.makedirs(late)
        # mtime 을 일부러 뒤집는다: 이름은 early 가 앞이지만 late 가 '더 최신 mtime'
        os.utime(early, (time.time() - 100, time.time() - 100))
        os.utime(late, (time.time(), time.time()))
        check("resolve: 기본은 mtime 최신(late) 반환(기존 동작 보존)",
              os.path.basename(resolve_session(t)) == today + "_063500")
        check("resolve: prefer_sameday_earliest 는 '먼저 생성된' early 반환(snapshot 오염 방지)",
              os.path.basename(resolve_session(t, prefer_sameday_earliest=True)) == today + "_063115")
        # 완료 세션(03_final_report) 제외 가드가 다중세션에서도 유지되는지
        open(os.path.join(early, "03_final_report.md"), "w").close()
        check("resolve: 완료 세션은 후보 제외(earliest 여도 late 선택)",
              os.path.basename(resolve_session(t, prefer_sameday_earliest=True)) == today + "_063500")
        # 단일 세션이면 두 모드 동일
        shutil.rmtree(late)
        os.remove(os.path.join(early, "03_final_report.md"))
        check("resolve: 단일 세션이면 모드 무관 동일",
              resolve_session(t) == resolve_session(t, prefer_sameday_earliest=True))
    finally:
        shutil.rmtree(t, ignore_errors=True)


# =====================================================================
# 7. v9.8 신규 수집기 순수함수 — credit(신용잔고)·earnings(실적캘린더)·vkospi 페이로드
# =====================================================================
def test_new_collectors_pure():
    import credit_collect as cc
    # 합성 신용공여 rows(십억 단위): 융자총 TMPV2, 유가 TMPV3, 코스닥 TMPV4, 대주 TMPV5, 담보 TMPV9
    credit_rows = [
        {"TMPV1": "20260701", "TMPV2": 36000, "TMPV3": 26000, "TMPV4": 10000, "TMPV5": 50, "TMPV9": 26000},
        {"TMPV1": "20260702", "TMPV2": 36500, "TMPV3": 26300, "TMPV4": 10200, "TMPV5": 51, "TMPV9": 26100},
        {"TMPV1": "20260703", "TMPV2": 36400, "TMPV3": 26200, "TMPV4": 10200, "TMPV5": 52, "TMPV9": 26200},
        {"TMPV1": "20260706", "TMPV2": 36200, "TMPV3": 26100, "TMPV4": 10100, "TMPV5": 52, "TMPV9": 26200},
        {"TMPV1": "20260707", "TMPV2": 35000, "TMPV3": 25200, "TMPV4": 9800, "TMPV5": 53, "TMPV9": 26300},
        {"TMPV1": "20260708", "TMPV2": 33362, "TMPV3": 26253, "TMPV4": 7109, "TMPV5": 24, "TMPV9": 25196},
    ]
    funds_rows = [
        {"TMPV1": "20260707", "TMPV2": 132860, "TMPV5": 1537, "TMPV6": 37, "TMPV7": 2.2},
        {"TMPV1": "20260708", "TMPV2": 108082, "TMPV5": 1145, "TMPV6": 15, "TMPV7": 1.1},
    ]
    p = cc.build_payload(credit_rows, funds_rows)
    check("credit: 십억→억 변환(총융자 333620)", p["margin_loan"]["total_eok"] == 333620.0,
          str(p["margin_loan"]["total_eok"]))
    check("credit: d5 변화율(-7.33% 근사)", abs(p["margin_loan"]["d5_chg_pct"] - (-7.33)) < 0.02,
          str(p["margin_loan"]["d5_chg_pct"]))
    check("credit: 예탁금 억 변환", p["deposit"]["total_eok"] == 1080820.0, str(p["deposit"]))
    check("credit: 반대매매 비중 전달", p["misu"]["rt_sell_ratio_pct"] == 1.1)
    check("credit: 급감 시 디레버리징 라벨", "디레버리징" in p["level_label"], p["level_label"])
    check("credit: 빈 입력 -> 빈 dict", cc.build_payload([], []) == {})
    # v9.8 감사: deposit.asof ISO 포맷(TMPV1 원문 YYYYMMDD 아님)
    check("credit: deposit.asof ISO 포맷", p["deposit"]["asof"] == "2026-07-08", str(p["deposit"]["asof"]))
    # v9.8 감사: 마지막 행 결측이면 d1/d5 위치기반이라 None(valid 압축으로 신장 금지)
    gap_rows = credit_rows[:-1] + [dict(credit_rows[-1], TMPV2=None)]
    pg = cc.build_payload(gap_rows, funds_rows)
    check("credit: 마지막행 결측 -> 빈 dict(총잔고 None)", pg == {}, str(pg)[:60])
    mid_gap = [dict(credit_rows[0], TMPV2=None)] + credit_rows[1:]
    pm = cc.build_payload(mid_gap, funds_rows)
    check("credit: 중간 결측 있어도 최신 유효 시 산출", pm.get("margin_loan", {}).get("total_eok") == 333620.0)

    import earnings_collect as ecal
    sample = ('<tr tablesorterdivider><td colspan="9" class="theDay">2026년 7월 20일 월요일</td></tr>'
              '<tr><td class="flag"></td>'
              '<td class="left noWrap earnCalCompany" title="기아" _p_pid="43460">'
              '<span class="earnCalCompanyName middle">기아</span>&nbsp;'
              '(<a href="/equities/kia-motors">000270</a>)</td></tr>'
              '<tr><td colspan="9" class="theDay">2026년 7월 21일 화요일</td></tr>'
              '<tr><td class="flag"></td>'
              '<td class="left noWrap earnCalCompany" title="POSCO홀딩스" _p_pid="43461">'
              '<a href="/equities/posco?cid=1">POSCO</a></td></tr>')
    ev = ecal.parse_calendar(sample)
    check("earnings: 2건 파싱", len(ev) == 2, str(ev))
    check("earnings: 날짜 구분 반영", ev[0]["date"] == "2026-07-20" and ev[1]["date"] == "2026-07-21")
    check("earnings: 이름·슬러그(쿼리 제거)", ev[0]["name"] == "기아" and ev[1]["slug"] == "posco", str(ev))
    check("earnings: 빈 입력 -> 빈 리스트", ecal.parse_calendar("") == [])

    # ── v10.5 알파 지수앵커 대칭(전면감사 P0) — 종목=D-1 종가인데 지수=D 종가면 추천일
    #    지수 변동이 통째로 '종목선택'으로 오귀속된다. 세 계산기 전부 D-1 앵커로 통일 검증. ──
    try:
        import retro_label as _rl5
        from datetime import date as _d5
        _saved_ser = _rl5._KS11_SERIES
        try:
            # 월 100 → 화 110 → 수 99 → 목 88 (기준일=화요일)
            _rl5._KS11_SERIES = [(_d5(2026, 1, 5), 100.0), (_d5(2026, 1, 6), 110.0),
                                 (_d5(2026, 1, 7), 99.0), (_d5(2026, 1, 8), 88.0)]
            _r = _rl5._kospi_ret_h(_d5(2026, 1, 6), 2)
            # D-1(월 100) → T+2(목 88) = -12%. 구버전(D 110 앵커)이면 -20% 였다.
            check("alpha앵커: retro_label D-1 종가 앵커(-12%)", _r == -12.0, str(_r))
            # 주말 추천(기준일=일요일): 종목 entry_ref=금요일 종가와 같은 봉이어야 한다
            _rl5._KS11_SERIES = [(_d5(2026, 1, 9), 100.0), (_d5(2026, 1, 12), 91.0),
                                 (_d5(2026, 1, 13), 90.0), (_d5(2026, 1, 14), 89.0)]
            _r2 = _rl5._kospi_ret_h(_d5(2026, 1, 11), 1)   # 일요일 추천, h=1
            # 금(100) → 월+1=화(90) = -10%. 구버전은 월(91) 앵커라 월요일 폭락 -9% 가 alpha 로 샜다.
            check("alpha앵커: 주말 추천도 직전 거래일(금) 앵커", _r2 == -10.0, str(_r2))
            # 앵커봉이 없으면(시계열 첫 봉이 기준일 이후) None — 0 이나 D 앵커로 조용히 대체 금지
            _r3 = _rl5._kospi_ret_h(_d5(2026, 1, 9), 1)
            check("alpha앵커: 직전 봉 없으면 None", _r3 is None, str(_r3))
        finally:
            _rl5._KS11_SERIES = _saved_ser
    except ImportError:
        print("[SKIP] alpha앵커: retro_label import 불가")

    try:
        import pandas as _pd5
        import accuracy_tracker as _at5
        from datetime import date as _d5b
        _idx = _pd5.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"])
        _dfK = _pd5.DataFrame({"Close": [100.0, 110.0, 99.0, 88.0]}, index=_idx)
        _old_fh = _at5._fetch_history
        try:
            _at5._fetch_history = lambda symbol, start: _dfK
            check("alpha앵커: tracker _close_before = D-1 종가",
                  _at5._close_before("KS11", _d5b(2026, 1, 6)) == 100.0)
            _rB, _ = _at5._index_return("KS11", _d5b(2026, 1, 6), 2, anchor_before=True)
            _rA, _ = _at5._index_return("KS11", _d5b(2026, 1, 6), 2, anchor_before=False)
            check("alpha앵커: tracker anchor_before=True → -12%", round(_rB, 1) == -12.0, str(_rB))
            check("alpha앵커: 시장콜 경로(False)는 종전 정의 유지(-20%)", round(_rA, 1) == -20.0, str(_rA))
        finally:
            _at5._fetch_history = _old_fh
    except ImportError:
        print("[SKIP] alpha앵커: pandas/accuracy_tracker 불가")

    try:
        import holding_review as _hr5
        from datetime import date as _d5c
        _ks = [(_d5c(2026, 1, 5), 100.0), (_d5c(2026, 1, 6), 110.0),
               (_d5c(2026, 1, 7), 99.0), (_d5c(2026, 1, 8), 88.0)]
        _w = _hr5._kospi_window(_ks, _d5c(2026, 1, 6))
        check("alpha앵커: holdrev 창 = [D-1] + [D초과]",
              _w == [(_d5c(2026, 1, 5), 100.0), (_d5c(2026, 1, 7), 99.0), (_d5c(2026, 1, 8), 88.0)],
              str(_w))
        check("alpha앵커: holdrev 앵커봉 없으면 빈 창(오귀속보다 결측)",
              _hr5._kospi_window(_ks[1:], _d5c(2026, 1, 6)) == [] or
              _hr5._kospi_window([(_d5c(2026, 1, 7), 99.0)], _d5c(2026, 1, 6)) == [],
              "before 없음 케이스")
    except ImportError:
        print("[SKIP] alpha앵커: holding_review 불가")

    # ── v10.5 pre_tech 당일 배제 + flow asof 미만 + fsc FDR end 존중(전면감사 P0/P1) ──
    try:
        import pandas as _pd6
        import flow_collect as _fc6
        from datetime import datetime as _DT6
        _fidx = _pd5.to_datetime(["2026-07-22", "2026-07-23", "2026-07-24"])
        _fdf = _pd6.DataFrame({"외국인합계": [1e8, 1e8, 1e8],
                               "기관합계": [2e8, 2e8, 2e8],
                               "개인": [-3e8, -3e8, -3e8]}, index=_fidx)

        class _KrxF:
            @staticmethod
            def get_market_trading_value_by_date(b, e, t):
                return _fdf.copy()

        _oldk, _oldc = _fc6._krx, dict(_fc6._ASOF_CACHE)
        try:
            _fc6._krx = _KrxF()
            _fc6._ASOF_CACHE.clear()
            _o = _fc6.get_flow_asof("005930", "20260724")
            # asof(07-24) 당일 행이 빠져야 한다: 외국인 5d 합 = 2일 x 1억 = 2억
            check("flowasof: 당일 수급 배제(외국인 2억)", _o.get("pre_foreign_5d_eok") == 2, str(_o))
            check("flowasof: 기관도 동일(4억)", _o.get("pre_inst_5d_eok") == 4, str(_o))
        finally:
            _fc6._krx = _oldk
            _fc6._ASOF_CACHE.clear()
            _fc6._ASOF_CACHE.update(_oldc)
    except ImportError:
        print("[SKIP] flowasof: pandas/flow_collect 불가")

    try:
        import pandas as _pd7
        import fsc_collect as _fs7
        from datetime import datetime as _DT7, timedelta as _TD7
        _t0 = _DT7.now().date()
        _days = [_t0 - _TD7(days=2), _t0 - _TD7(days=1), _t0]
        _fidx7 = _pd7.to_datetime([d.strftime("%Y-%m-%d") for d in _days])
        _df7 = _pd7.DataFrame({"Close": [10.0, 11.0, 12.0], "Change": [0.01, 0.02, 0.03],
                               "Volume": [1, 1, 1], "Open": [10, 11, 12],
                               "High": [10, 11, 12], "Low": [10, 11, 12]}, index=_fidx7)
        _calls = {}

        class _FdrStub:
            @staticmethod
            def DataReader(code, start, end=None):
                _calls["end"] = end
                return _df7.copy()

        _old_fdr, _old_ok = _fs7.fdr, _fs7.FDR_OK
        try:
            _fs7.fdr = _FdrStub()
            _fs7.FDR_OK = True
            _rows = _fs7.fetch_history_fdr("005930", _days[0].strftime("%Y%m%d"),
                                           _t0.strftime("%Y%m%d"))
            check("fscfdr: end 인자가 DataReader 에 전달된다", _calls.get("end") is not None,
                  str(_calls))
            check("fscfdr: 당일 부분봉 배제(2행만)", len(_rows) == 2 and
                  all(r["date"] < _t0.strftime("%Y-%m-%d") for r in _rows), str(len(_rows)))
        finally:
            _fs7.fdr = _old_fdr
            _fs7.FDR_OK = _old_ok
    except ImportError:
        print("[SKIP] fscfdr: pandas/fsc_collect 불가")

    # ── v10.4 보유 재평가 — 판정이 아니라 '선언한 계약 대비 현재 위치'를 정확히 재는가 ──
    try:
        import holding_review as _hr
        from datetime import date as _d

        def _ser(vals, start_day=2):
            """[(date, close)] — 2026-01-02 부터 연속 거래일이라 가정(순수 계산 테스트용)."""
            return [(_d(2026, 1, start_day + i), v) for i, v in enumerate(vals)]

        _pick = {"ticker": "005930", "name": "테스트", "entry_ref": 100.0, "horizon_days": 5,
                 "target_pct": 8, "stop_pct": -6, "partial_take_pct": 5, "trailing_stop_pct": 4}
        # 롱: 100 → 110 고점 → 105 마감. 목표(+8) 도달, 손절(-6) 미이탈, 트레일링(고점-4) 도달
        _r = _hr.review_one(_pick, "pick", "2026-01-01", _ser([102, 110, 105]), [], _d(2026, 1, 4))
        check("holdrev: 수익률", _r["ret_pct"] == 5.0, str(_r["ret_pct"]))
        check("holdrev: 고점수익", _r["peak_gain_pct"] == 10.0, str(_r["peak_gain_pct"]))
        check("holdrev: 고점대비 반납", _r["drawdown_from_peak_pct"] == -4.55,
              str(_r["drawdown_from_peak_pct"]))
        check("holdrev: 목표 도달", _r["level_flags"].get("target_hit") is True)
        check("holdrev: 손절 미이탈", _r["level_flags"].get("stop_hit") is False)
        check("holdrev: 트레일링 도달(고점10 - 현재5 = 5 >= 4)",
              _r["level_flags"].get("trailing_hit") is True)
        check("holdrev: 경과 거래일", _r["elapsed_bdays"] == 3, str(_r["elapsed_bdays"]))
        check("holdrev: 잔여 거래일", _r["remaining_bdays"] == 2, str(_r["remaining_bdays"]))
        check("holdrev: 만기 미도달", _r["horizon_expired"] is False)

        # 손절 이탈: 100 → 92 (-8% <= -6%)
        _r2 = _hr.review_one(_pick, "pick", "2026-01-01", _ser([97, 92]), [], _d(2026, 1, 3))
        check("holdrev: 손절 이탈 감지", _r2["level_flags"].get("stop_hit") is True)
        check("holdrev: 현재도 손절 아래", _r2["level_flags"].get("currently_below_stop") is True)
        # 이탈 후 회복 — stop_hit 은 True 지만 현재는 아래가 아니다(둘을 구분해야 판단이 갈린다)
        _r3 = _hr.review_one(_pick, "pick", "2026-01-01", _ser([92, 99]), [], _d(2026, 1, 3))
        check("holdrev: 이탈 이력은 남되", _r3["level_flags"].get("stop_hit") is True)
        check("holdrev: 현재는 손절 위(회복)", _r3["level_flags"].get("currently_below_stop") is False)

        # ★숏: 가격이 내려야 이익 — 부호 반전이 되는가
        _short = dict(_pick, target_pct=8, stop_pct=-6)
        _rs = _hr.review_one(_short, "short", "2026-01-01", _ser([95, 90]), [], _d(2026, 1, 3))
        check("holdrev: 숏 ret_pct 는 원가격 기준(-10)", _rs["ret_pct"] == -10.0, str(_rs["ret_pct"]))
        check("holdrev: 숏 favorable 은 +10", _rs["favorable_pct"] == 10.0, str(_rs["favorable_pct"]))
        check("holdrev: 숏 목표 도달(하락 10 >= 8)", _rs["level_flags"].get("target_hit") is True)
        check("holdrev: 숏은 손절 미이탈", _rs["level_flags"].get("stop_hit") is False)
        # ★숏 alpha 부호 — raw 는 retro_label 규약(음수가 좋음), favorable 은 방향 보정
        _rsa = _hr.review_one(_short, "short", "2026-01-01", _ser([95, 90]),
                              [(_d(2026, 1, 2), 100.0), (_d(2026, 1, 3), 95.0)], _d(2026, 1, 3))
        check("holdrev: 숏 alpha_pct 는 raw(-10 - (-5) = -5)",
              _rsa["alpha_pct"] == -5.0, str(_rsa["alpha_pct"]))
        check("holdrev: 숏 alpha_favorable 은 부호 반전(+5)",
              _rsa["alpha_favorable_pct"] == 5.0, str(_rsa["alpha_favorable_pct"]))
        _rla = _hr.review_one(_pick, "pick", "2026-01-01", _ser([95, 90]),
                              [(_d(2026, 1, 2), 100.0), (_d(2026, 1, 3), 95.0)], _d(2026, 1, 3))
        check("holdrev: 롱은 두 alpha 가 같다",
              _rla.get("alpha_pct") == _rla.get("alpha_favorable_pct") == -5.0,
              "%s vs %s" % (_rla.get("alpha_pct"), _rla.get("alpha_favorable_pct")))
        # 숏이 역행(가격 상승)하면 손절
        _rs2 = _hr.review_one(_short, "short", "2026-01-01", _ser([104, 108]), [], _d(2026, 1, 3))
        check("holdrev: 숏 역행 시 손절 감지", _rs2["level_flags"].get("stop_hit") is True)

        # ★계약 미선언 — 플래그를 False 로 채우면 '미이탈'로 오독된다
        _bare = {"ticker": "000660", "entry_ref": 100.0, "horizon_days": 5}
        _rb = _hr.review_one(_bare, "pick", "2026-01-01", _ser([70]), [], _d(2026, 1, 2))
        check("holdrev: 계약 미비면 플래그 키 없음", _rb["level_flags"] == {}, str(_rb["level_flags"]))
        check("holdrev: 계약 미비 표시", _rb["contract_complete"] is False)
        check("holdrev: 미비 항목 열거", set(_rb["contract_missing"]) ==
              {"target_pct", "stop_pct", "partial_take_pct", "trailing_stop_pct"},
              str(_rb["contract_missing"]))
        check("holdrev: 계약 없어도 수익률은 잰다", _rb["ret_pct"] == -30.0, str(_rb["ret_pct"]))

        # alpha — 손실이 시장 베타인지 종목 선택 실패인지 가른다
        _ra = _hr.review_one(_pick, "pick", "2026-01-01", _ser([95]),
                             _ser([2000.0, 1800.0])[:1] + [], _d(2026, 1, 2))
        _ra2 = _hr.review_one(_pick, "pick", "2026-01-01", _ser([95]),
                              [(_d(2026, 1, 2), 1800.0)], _d(2026, 1, 2))
        check("holdrev: 코스피 1점이면 지수수익 0 → alpha=ret",
              _ra2.get("alpha_pct") == -5.0, str(_ra2.get("alpha_pct")))
        _rk = _hr.review_one(_pick, "pick", "2026-01-01",
                             _ser([95, 90]), [(_d(2026, 1, 2), 100.0), (_d(2026, 1, 3), 80.0)],
                             _d(2026, 1, 3))
        check("holdrev: 지수 -20%, 종목 -10% → alpha +10",
              _rk.get("alpha_pct") == 10.0, str(_rk.get("alpha_pct")))

        # v10.1 시간축 연결 — 예상 고점 시한 경과 + 아직 마이너스
        _pv = dict(_pick, expected_peak_days=2, path_view="즉시상승", expected_gain_pct=10)
        _rp = _hr.review_one(_pv, "pick", "2026-01-01", _ser([99, 98, 97]), [], _d(2026, 1, 4))
        check("holdrev: 고점시한 경과 감지", _rp["path_check"]["peak_overdue"] is True)
        check("holdrev: 시한경과+마이너스 감지", _rp["path_check"]["overdue_and_negative"] is True)
        check("holdrev: 기대수익 대비 격차", _rp["path_check"]["gain_vs_expected_pp"] == -11.0,
              str(_rp["path_check"].get("gain_vs_expected_pp")))
        # entry_ref 없으면 조용히 0 이 아니라 상태로 남긴다
        _rn = _hr.review_one({"ticker": "1", "horizon_days": 5}, "pick", "2026-01-01",
                             _ser([100]), [], _d(2026, 1, 2))
        check("holdrev: entry_ref 없으면 no_entry_ref", _rn["status"] == "no_entry_ref", _rn["status"])
    except ImportError:
        print("[SKIP] holdrev: holding_review import 불가")

    # ── v10.3 VKOSPI investing 폴백(A13) — 엉뚱한 지수를 VKOSPI 로 착각하면 국면이 통째로 틀어진다 ──
    try:
        import vkospi_collect as _vk
        _good = ('<h1>KOSPI Volatility (KSVKOSPI)</h1>'
                 '<span data-test="instrument-price-last">78.65</span>'
                 '<time dateTime="2026-07-24T06:29:59.000Z"></time>' + "x" * 3000)
        _r = _vk.parse_investing(_good)
        check("vkfb: 정상 파싱(KST 날짜 환산)", _r == {"2026-07-24": 78.65}, str(_r))
        # ★다른 지수 페이지를 잘못 열었을 때 — 반드시 거부
        _wrong = _good.replace("KOSPI Volatility (KSVKOSPI)", "KOSPI 200 Futures")
        check("vkfb: 다른 지수 페이지 거부", _vk.parse_investing(_wrong) == {},
              str(_vk.parse_investing(_wrong)))
        # 값이 물리적 범위를 벗어나면 파싱 사고로 보고 거부
        _huge = _good.replace(">78.65<", ">2,654.30<")
        check("vkfb: 범위 이탈 값 거부(지수 오인)", _vk.parse_investing(_huge) == {})
        _zero = _good.replace(">78.65<", ">0<")
        check("vkfb: 0 이하 거부", _vk.parse_investing(_zero) == {})
        # 체결 시각이 없으면 '오늘'로 날조하지 말고 실패 — 시점 날조 금지
        _nots = _good.replace('<time dateTime="2026-07-24T06:29:59.000Z"></time>', "")
        check("vkfb: 체결시각 없으면 거부(날짜 날조 금지)", _vk.parse_investing(_nots) == {})
        check("vkfb: 빈 입력 거부", _vk.parse_investing("") == {})
        # 표본이 얕으면 백분위를 조용히 내놓지 않는다
        _p1 = _vk.build_payload([], rows={"2026-07-24": 78.65}, source="investing_fallback")
        check("vkfb: 1점이면 백분위 None", _p1.get("pct_rank_60d") is None, str(_p1.get("pct_rank_60d")))
        check("vkfb: 1점이면 d5 None", _p1["latest"]["d5_chg_pct"] is None)
        check("vkfb: source 가 payload 에 실린다", _p1.get("source") == "investing_fallback")
        check("vkfb: 78.65 는 공포 라벨", "공포" in _p1.get("level_label", ""), _p1.get("level_label"))
        # 충분한 표본이면 백분위가 다시 산출된다(누적 복구 경로)
        _many = {"2026-05-%02d" % (d + 1): 20.0 + d for d in range(25)}
        _p2 = _vk.build_payload([], rows=_many, source="investing_fallback")
        check("vkfb: 표본 20+ 면 백분위 산출", _p2.get("pct_rank_60d") is not None)
        # items 경로(FSC)는 기존 계약 그대로여야 한다(정확한 지수명 — 공백 포함)
        _p3 = _vk.build_payload([{"idxNm": "코스피 200 변동성지수", "basDt": "20260724", "clpr": "30.5"}])
        check("vkfb: FSC items 경로 불변", _p3.get("asof_date") == "2026-07-24"
              and _p3.get("source") == "fsc_index", str(_p3.get("source")))
        # ★실제 사고 재현(2026-07-26): likeIdxNm=변동성 조회는 이름에 '변동성'이 들어간 6종을 섞어
        #   돌려준다. 부분일치 필터였을 때 3903.23(현선물 목표변동성24%지수)이 VKOSPI 인 척 찍혔다.
        _mixed = [
            {"idxNm": "KRX 최소변동성지수", "basDt": "20260723", "clpr": "12905.04"},
            {"idxNm": "코스피 200 가치저변동성", "basDt": "20260723", "clpr": "13941.26"},
            {"idxNm": "코스피 200 변동성매칭 양매도지수", "basDt": "20260723", "clpr": "916.47"},
            {"idxNm": "코스피 200 변동성지수", "basDt": "20260723", "clpr": "80.46"},
            {"idxNm": "코스피 200 변동성추세 추종 양매도지수", "basDt": "20260723", "clpr": "729.67"},
            {"idxNm": "코스피 200 현선물 목표변동성 24% 지수", "basDt": "20260723", "clpr": "3903.23"},
        ]
        _rows_mixed = _vk.items_to_rows(_mixed)
        check("vkfb: 6종 혼재에서 진짜 VKOSPI만 선택(80.46)",
              _rows_mixed == {"2026-07-23": 80.46}, str(_rows_mixed))
        _p4 = _vk.build_payload(_mixed)
        check("vkfb: 혼재 응답으로도 엉뚱한 전략지수값(3903.23 등)이 안 들어간다",
              _p4.get("latest", {}).get("value") == 80.46, str(_p4.get("latest")))
    except ImportError:
        print("[SKIP] vkfb: vkospi_collect import 불가")

    # ── v10.8 토큰 생성기 — 강도·지문·파일형식(토큰 자체는 절대 출력하지 않는다) ──
    try:
        import gen_token as _gt

        _t1, _t2 = _gt.make_token(32), _gt.make_token(32)
        check("token: 매번 다른 값(CSPRNG)", _t1 != _t2)
        check("token: 256비트 → URL-safe 40자 이상", len(_t1) >= 40, str(len(_t1)))
        check("token: URL-safe 문자만(헤더에 그대로 실림)",
              all(c.isalnum() or c in "-_" for c in _t1), _t1[:0] or "비허용 문자")
        try:
            _gt.make_token(8)
            check("token: 16바이트 미만은 거부", False, "예외 미발생")
        except ValueError:
            check("token: 16바이트 미만은 거부", True)

        # 지문: 같은 토큰 → 같은 지문, 다른 토큰 → 다른 지문, 역산 불가(토큰 미포함)
        check("token: 같은 토큰은 같은 지문", _gt.fingerprint(_t1) == _gt.fingerprint(_t1))
        check("token: 다른 토큰은 다른 지문", _gt.fingerprint(_t1) != _gt.fingerprint(_t2))
        check("token: 지문 길이 12", len(_gt.fingerprint(_t1)) == 12, _gt.fingerprint(_t1))
        check("token: 지문에 토큰 원문이 없다(역산 불가)", _t1 not in _gt.fingerprint(_t1))
        check("token: 빈 토큰은 빈 지문(없는 걸 있는 척 금지)", _gt.fingerprint("") == "")

        # 파일 본문: 주석엔 지문만, 토큰은 token= 한 줄에만
        _body = _gt.render_file(_t1)
        check("token: 파일에 token= 라인 존재", ("token=" + _t1) in _body)
        check("token: 주석에 지문 기재", _gt.fingerprint(_t1) in _body)
        check("token: 토큰은 파일에 정확히 1회만 등장", _body.count(_t1) == 1, str(_body.count(_t1)))

        # 왕복: 쓴 파일을 kairos_client 로 읽어도 같은 값(두 로더 규약 일치)
        _td2 = tempfile.mkdtemp(prefix="gt_")
        _fp2 = os.path.join(_td2, "kairos_api.txt")
        io.open(_fp2, "w", encoding="utf-8", newline="").write(_body)
        check("token: gen_token 자체 로더 왕복", _gt.read_token(_fp2) == _t1)
        # --set 경로(save_token): 붙여넣은 토큰을 그대로 저장하고 지문을 돌려준다
        _fp3 = os.path.join(_td2, "set.txt")
        _got_fp = _gt.save_token("  " + _t2 + "  ", _fp3)          # 앞뒤 공백은 잘라낸다
        check("token: save_token 왕복", _gt.read_token(_fp3) == _t2)
        check("token: save_token 지문 일치", _got_fp == _gt.fingerprint(_t2))
        try:
            _gt.save_token("short", _fp3)
            check("token: 붙여넣기 잘림(16자 미만) 거부", False, "예외 미발생")
        except ValueError:
            check("token: 붙여넣기 잘림(16자 미만) 거부", True)
        check("token: 거부돼도 기존 파일 불변", _gt.read_token(_fp3) == _t2)
        try:
            import kairos_client as _kc2
            check("token: kairos_client 로더와 규약 일치", _kc2.load_token(_fp2) == _t1)
        except ImportError:
            pass
    except ImportError:
        print("[SKIP] token: gen_token import 불가")

    # ── v11.3 장중 재분석 — '아침에 말한 자리에서 실제로 살 수 있었나'를 재는가 ──
    try:
        import intraday_review as _ir
        for _t, _want in (("08:30", "pre_open"), ("09:00", "intraday"), ("13:00", "intraday"),
                          ("15:30", "intraday"), ("15:31", "after_close"), ("20:00", "after_close")):
            check("intraday: 장구간 %s → %s" % (_t, _want), _ir.session_phase(_t) == _want)

        _base = {"ticker": "005930", "entry_ref": 100, "target_pct": 8, "stop_pct": -5}
        # ★핵심: 갭상승하면 '당일시가' 추천은 실제로 못 산다
        _r = _ir.review_pick(dict(_base, entry_window="당일시가"),
                             {"open": 108, "high": 112, "low": 107, "last": 110}, "intraday")
        check("intraday: 갭 +8% 면 당일시가 진입 불가 판정",
              _r["entry_check"]["buyable"] is False and "못 산다" in _r["entry_check"]["note"])
        _r2 = _ir.review_pick(dict(_base, entry_window="당일시가"),
                              {"open": 101, "high": 104, "low": 100, "last": 103}, "intraday")
        check("intraday: 갭 +1% 면 진입 가능", _r2["entry_check"]["buyable"] is True)
        # 눌림 대기: 저가가 진입가 아래로 왔는지가 판정 기준
        _r3 = _ir.review_pick(dict(_base, entry_window="당일눌림"),
                              {"open": 102, "high": 105, "low": 98, "last": 104}, "intraday")
        check("intraday: 눌림 왔으면 진입 기회 있음", _r3["entry_check"]["buyable"] is True)
        _r4 = _ir.review_pick(dict(_base, entry_window="당일눌림"),
                              {"open": 105, "high": 109, "low": 103, "last": 108}, "intraday")
        check("intraday: 눌림 안 왔으면 진입 못 함", _r4["entry_check"]["buyable"] is False)
        # 종가·익일 진입은 장중에 판정하지 않는다(성급한 단정 금지)
        _r5 = _ir.review_pick(dict(_base, entry_window="당일종가"),
                              {"open": 101, "high": 104, "low": 100, "last": 103}, "intraday")
        check("intraday: 당일종가는 장중 미판정(None)", _r5["entry_check"]["buyable"] is None)
        _r6 = _ir.review_pick(dict(_base, entry_window="익일이후"),
                              {"open": 101, "high": 104, "low": 100, "last": 103}, "intraday")
        check("intraday: 익일이후도 장중 미판정(None)", _r6["entry_check"]["buyable"] is None)
        # 선언한 계약(target/stop) 터치
        check("intraday: 고가가 목표 도달", _r["level_flags"].get("target_touched") is True)
        _r7 = _ir.review_pick(dict(_base, entry_window="당일시가"),
                              {"open": 99, "high": 100, "low": 94, "last": 95}, "intraday")
        check("intraday: 저가가 손절 터치", _r7["level_flags"].get("stop_touched") is True)
        check("intraday: 현재도 손절 아래", _r7["level_flags"].get("below_stop_now") is True)
        # entry_window 미선언이면 판정 불가로 남긴다(False 로 채우지 않는다)
        _r8 = _ir.review_pick(dict(_base), {"open": 101, "high": 104, "low": 100, "last": 103},
                              "intraday")
        check("intraday: entry_window 없으면 판정 불가 명시",
              "판정 불가" in _r8["entry_check"]["note"] and "buyable" not in _r8["entry_check"])
        check("intraday: entry_ref 없으면 no_entry_ref",
              _ir.review_pick({"ticker": "1"}, {"open": 1}, "intraday")["status"] == "no_entry_ref")

        # ── v11.5 거래 가능 시장 판정 — 예정작업 ±5분 오차 + 분석 소요로 세션이 바뀔 수 있다 ──
        def _vn(t):
            return [x["venue"] for x in _ir.tradable_venues(t)["open_venues"]]
        check("venue: 08:30 은 NXT 프리마켓만", _vn("08:30") == ["NXT 프리마켓"], str(_vn("08:30")))
        check("venue: 10:00 은 KRX 정규장 + NXT 메인",
              _vn("10:00") == ["KRX 정규장", "NXT 메인마켓"], str(_vn("10:00")))
        check("venue: 15:25 는 KRX 종가단일가만(NXT 메인 15:20 마감)",
              _vn("15:25") == ["KRX 종가 단일가"], str(_vn("15:25")))
        check("venue: 15:35 는 NXT 애프터만(KRX 시간외는 15:40부터)",
              _vn("15:35") == ["NXT 애프터마켓"], str(_vn("15:35")))
        check("venue: 15:45 는 KRX 시간외종가 + NXT 애프터",
              _vn("15:45") == ["KRX 시간외 종가", "NXT 애프터마켓"], str(_vn("15:45")))
        check("venue: 16:10 은 KRX 시간외단일가 + NXT 애프터",
              _vn("16:10") == ["KRX 시간외 단일가", "NXT 애프터마켓"], str(_vn("16:10")))
        check("venue: 18:30 은 NXT 애프터만", _vn("18:30") == ["NXT 애프터마켓"], str(_vn("18:30")))
        check("venue: 20:10 은 전부 마감", _vn("20:10") == [], str(_vn("20:10")))
        check("venue: 마감이면 tradable_now=False",
              _ir.tradable_venues("20:10")["tradable_now"] is False)
        # ★NXT 종목은 KRX 시간외단일가 불가 — 한 종목에 두 시장을 제안하면 안 된다
        _v16 = _ir.tradable_venues("16:10")
        check("venue: 시간외단일가에 NXT 배타 경고",
              any("NXT" in x["note"] and "불가" in x["note"]
                  for x in _v16["open_venues"] if "단일가" in x["venue"]), str(_v16))
        check("venue: 안내문에 '한 시장만' 규율", "한 시장만" in _v16["guidance"])
        # 15:40 시간외 종가는 가격 지정이 안 된다(종가 고정) — 호가 제안 시 중요
        _v1545 = _ir.tradable_venues("15:45")
        # ── v11.12 포트폴리오 — 돈 계산이라 하네스로 고정 ──
        import portfolio_review as _pf
        _r, _w = _pf.parse_row({"매수일시": "2026-07-15", "종목코드": "5930",
                                "종목명": "삼성전자", "평단가": "71,500", "수량": "10"})
        check("pf: 6자리 zfill + 콤마 숫자 파싱",
              _r and _r["ticker"] == "005930" and _r["avg_price"] == 71500.0 and _r["qty"] == 10,
              str((_r, _w)))
        check("pf: 종목코드 없으면 거부", _pf.parse_row({"평단가": "100", "수량": "1"})[0] is None)
        # ★엑셀이 종목코드를 망가뜨리는 두 방식 — 선행 0 소실·텍스트서식. 전부 받아야 한다.
        check("pf: 엑셀 선행0 소실 복원(34020->034020)",
              _pf.normalize_ticker("34020") == ("034020", True))
        check("pf: 정상 6자리는 복원 표시 안 함",
              _pf.normalize_ticker("034020") == ("034020", False))
        check("pf: 엑셀 텍스트서식 =\"034020\" 수용",
              _pf.normalize_ticker('="034020"')[0] == "034020")
        check("pf: HTS 표기 A034020 수용", _pf.normalize_ticker("A034020")[0] == "034020")
        check("pf: 공백 포함 수용", _pf.normalize_ticker(" 34020 ")[0] == "034020")
        check("pf: 7자리 이상 거부", _pf.normalize_ticker("1234567")[0] is None)
        check("pf: 문자 거부", _pf.normalize_ticker("abc")[0] is None)
        check("pf: 빈 값 거부", _pf.normalize_ticker("")[0] is None)
        check("pf: 엑셀 안전 표기 생성", _pf.excel_safe_ticker("034020") == '="034020"')
        # 망가진 코드로도 파싱이 끝까지 성공해야(사용자가 고치기 전에도 동작)
        _rb, _ = _pf.parse_row({"종목코드": "34020", "종목명": "두산에너빌리티",
                                "평단가": "50000", "수량": "3"})
        check("pf: 망가진 코드로도 파싱 성공 + 복원 표시",
              _rb and _rb["ticker"] == "034020" and _rb["ticker_fixed"] is True, str(_rb))
        check("pf: 수량 0 거부",
              _pf.parse_row({"종목코드": "005930", "평단가": "100", "수량": "0"})[0] is None)
        check("pf: 숫자 아닌 평단가 거부",
              _pf.parse_row({"종목코드": "005930", "평단가": "비쌈", "수량": "1"})[0] is None)
        # 가중평균 — 여러 번 나눠 산 경우
        _m = _pf.merge_lots([
            {"ticker": "005930", "name": "삼성전자", "avg_price": 300000, "qty": 3,
             "buy_date": "2026-07-15", "memo": ""},
            {"ticker": "005930", "name": "삼성전자", "avg_price": 320000, "qty": 2,
             "buy_date": "2026-07-22", "memo": ""}])
        check("pf: 분할매수 가중평균 308,000 · 5주",
              len(_m) == 1 and _m[0]["avg_price"] == 308000.0 and _m[0]["qty"] == 5, str(_m))
        check("pf: 매수일은 가장 이른 날", _m[0]["buy_date"] == "2026-07-15")
        _c = _pf.compute_position({"ticker": "005930", "name": "삼성전자", "avg_price": 100000,
                                   "qty": 2, "buy_date": "2026-07-01"},
                                  110000, index_ret_pct=4.0,
                                  today=__import__("datetime").date(2026, 7, 31))
        check("pf: 손익 +20,000 / +10% / 알파 +6%",
              _c["pnl"] == 20000 and _c["pnl_pct"] == 10.0 and _c["alpha_pct"] == 6.0, str(_c))
        check("pf: 보유일수 30일", _c["held_days"] == 30, str(_c.get("held_days")))
        _cn = _pf.compute_position({"ticker": "005930", "name": "x", "avg_price": 100,
                                    "qty": 1, "buy_date": "2026-07-01"}, None)
        check("pf: 현재가 조회 실패면 손익 None(추측 금지)",
              _cn["pnl"] is None and "조회 실패" in _cn.get("_note", ""), str(_cn))
        _t = _pf.portfolio_totals([
            {"ticker": "A", "name": "가", "value": 700, "cost": 500},
            {"ticker": "B", "name": "나", "value": 300, "cost": 500}])
        check("pf: 합계·집중도", _t["total_pnl"] == 0 and _t["top_weight_pct"] == 70.0, str(_t))
        # ── v11.13 다중 사용자 + 증권사 구분 ──
        check("pf: 미래에셋은 자동매매 대상", _pf.is_auto_broker("미래에셋") is True)
        check("pf: 미래에셋도 대상", _pf.is_auto_broker("미래에셋") is True)
        check("pf: ★KB 는 참고만(자동매매 아님)", _pf.is_auto_broker("KB") is False)
        check("pf: 빈 증권사는 기본 대상", _pf.is_auto_broker("") is True)
        check("pf: 이메일 파일명 추출",
              _pf.email_from_path("portfolios/a@b.com.csv") == "a@b.com")
        # ── v11.17 2차 리포트 발송(장중·마감·전야) ──
        import report_mail as _rm
        # ★Gmail 은 <style> 블록을 지운다 — 태그마다 인라인이어야 표가 산다
        _ih = _rm.inline_styles("<table><tr><th>a</th><td>b</td></tr></table><hr><h2>t</h2>")
        check("rmail: 표·제목·구분선에 인라인 스타일",
              'border-collapse' in _ih and '<th style=' in _ih
              and '<td style=' in _ih and '<hr style=' in _ih and '<h2 style=' in _ih, _ih[:90])
        check("rmail: 이미 style 이 있으면 덮지 않는다",
              _rm.inline_styles('<td style="x">v</td>') == '<td style="x">v</td>')
        # 신선도 — '파일이 있다'는 신선함이 아니다
        _tdr = tempfile.mkdtemp(prefix="rmail_")
        try:
            _nmd = os.path.join(_tdr, "night_preview.md")
            _njs = os.path.join(_tdr, "night_preview.json")
            io.open(_nmd, "w", encoding="utf-8").write("## 전야")
            _mk = lambda d: json.dump({"for_date": d}, io.open(_njs, "w", encoding="utf-8"))
            _today = datetime.now().date()
            _mk((_today + timedelta(days=1)).strftime("%Y-%m-%d"))
            check("rmail: 전야가 내일 대상이면 통과",
                  _rm.freshness("night", _nmd, _njs)[0] is True)
            _mk((_today - timedelta(days=1)).strftime("%Y-%m-%d"))
            _ok, _why, _ = _rm.freshness("night", _nmd, _njs)
            check("rmail: ★지난밤 전야 파일은 막는다(23시 작업 실패 잔재)",
                  _ok is False and "지난 밤" in _why, _why)
            _imd = os.path.join(_tdr, "intraday_1100.md")
            _ijs = os.path.join(_tdr, "intraday_review.json")
            io.open(_imd, "w", encoding="utf-8").write("# 장중")
            json.dump({"trade_date": (_today - timedelta(days=1)).strftime("%Y-%m-%d")},
                      io.open(_ijs, "w", encoding="utf-8"))
            _ok2, _why2, _ = _rm.freshness("intraday", _imd, _ijs)
            check("rmail: ★어제 세션 장중 리포트는 막는다", _ok2 is False, _why2)
            json.dump({"trade_date": _today.strftime("%Y-%m-%d")},
                      io.open(_ijs, "w", encoding="utf-8"))
            check("rmail: 오늘 것이면 통과", _rm.freshness("intraday", _imd, _ijs)[0] is True)
        finally:
            shutil.rmtree(_tdr, ignore_errors=True)
        check("rmail: 종류 3종 정의", sorted(_rm.KINDS) == ["after_close", "intraday", "night"])

        # ── v11.15 해외 주식(미국·일본) ──
        # ★국가는 종목 식별의 일부다. 빼면 일본 7203(도요타)이 한국 007203 이 되고,
        #   증권사가 카이로스면 자동매매 대상으로까지 잡힌다.
        check("intl: 일본 4자리는 zfill 하지 않는다",
              _pf.normalize_ticker("7203", "JP")[0] == "7203")
        check("intl: 같은 문자열이 한국에선 6자리가 된다",
              _pf.normalize_ticker("7203", "KR")[0] == "007203")
        check("intl: 미국은 영문 티커", _pf.normalize_ticker("aapl", "US")[0] == "AAPL")
        check("intl: BRK-B -> BRK.B", _pf.normalize_ticker("BRK-B", "US")[0] == "BRK.B")
        check("intl: 국가가 틀리면 거절",
              _pf.normalize_ticker("AAPL", "KR")[0] is None
              and _pf.normalize_ticker("034020", "JP")[0] is None
              and _pf.normalize_ticker("7203", "US")[0] is None)
        check("intl: 국가 별칭", _pf.normalize_country("미국") == "US"
              and _pf.normalize_country("일본") == "JP"
              and _pf.normalize_country("") == "KR"
              and _pf.normalize_country("중국") is None)
        check("intl: 일본은 .T 접미사(7203 은 404, 7203.T 는 OK — 실측)",
              _pf.market_symbol("7203", "JP") == "7203.T"
              and _pf.market_symbol("AAPL", "US") == "AAPL")
        # ★★해외는 절대 자동매매 대상이 아니다 — 러너는 국내 HTS 만 조작한다
        _ru, _ = _pf.parse_row({"국가": "미국", "증권사": "카이로스", "종목코드": "AAPL",
                                "종목명": "애플", "평단가": "250", "수량": "1",
                                "매수환율": "1380"})
        check("intl: ★카이로스라도 해외면 자동매매 대상 아님",
              _ru and _ru["auto_tradable"] is False, str(_ru))
        _rk2, _ = _pf.parse_row({"국가": "한국", "증권사": "카이로스", "종목코드": "034020",
                                 "종목명": "두산", "평단가": "1", "수량": "1"})
        check("intl: 국내 카이로스는 자동매매 대상", _rk2 and _rk2["auto_tradable"] is True)
        # 환율 필수 + 단위 검사
        _rn2, _why2 = _pf.parse_row({"국가": "미국", "증권사": "KB", "종목코드": "AAPL",
                                     "종목명": "애플", "평단가": "250", "수량": "1"})
        check("intl: 해외인데 환율 없으면 거절",
              _rn2 is None and "매수환율" in _why2, str(_why2))
        _r100, _w100 = _pf.parse_row({"국가": "일본", "증권사": "KB", "종목코드": "7203",
                                      "종목명": "도요타", "평단가": "2800", "수량": "1",
                                      "매수환율": "910"})
        check("intl: ★'100엔당' 오기를 잡고 되돌릴 값을 알려준다",
              _r100 is None and "100엔당" in _w100 and "9.10" in _w100, str(_w100))
        check("intl: 환율 상식 범위",
              _pf.fx_sane("USD", 1380)[0] is True and _pf.fx_sane("JPY", 9.02)[0] is True
              and _pf.fx_sane("USD", 5)[0] is False and _pf.fx_sane("JPY", 900)[0] is False)
        # 국가가 다르면 합치지 않는다
        _mi = _pf.merge_lots([
            {"country": "JP", "ticker": "7203", "name": "도요타", "avg_price": 2800,
             "qty": 1, "buy_date": "", "memo": "", "broker": "KB",
             "auto_tradable": False, "buy_fx": 9.0},
            {"country": "KR", "ticker": "7203", "name": "다른회사", "avg_price": 100,
             "qty": 1, "buy_date": "", "memo": "", "broker": "KB",
             "auto_tradable": False, "buy_fx": None}])
        check("intl: ★국가가 다르면 같은 코드여도 분리", len(_mi) == 2, str(_mi))
        # 환율 가중평균 + 원화 원가
        _mf = _pf.merge_lots([
            {"country": "US", "ticker": "AAPL", "name": "애플", "avg_price": 100, "qty": 1,
             "buy_date": "", "memo": "", "broker": "KB", "auto_tradable": False, "buy_fx": 1000.0},
            {"country": "US", "ticker": "AAPL", "name": "애플", "avg_price": 100, "qty": 3,
             "buy_date": "", "memo": "", "broker": "KB", "auto_tradable": False, "buy_fx": 1400.0}])
        check("intl: 매수환율은 금액 가중평균(1000x1 + 1400x3 -> 1300)",
              len(_mf) == 1 and abs(_mf[0]["buy_fx"] - 1300.0) < 0.01, str(_mf))
        check("intl: 원화 원가 = 현지원가 x 매수환율",
              abs(_mf[0]["cost_krw"] - 400 * 1300.0) < 1, str(_mf[0].get("cost_krw")))
        # 현지 수익률 / 환차익 / 원화 수익률 분리
        _cp = _pf.compute_position(
            {"country": "US", "ticker": "AAPL", "name": "애플", "avg_price": 100.0,
             "qty": 1, "buy_date": "", "buy_fx": 1000.0},
            last_close=110.0, index_ret_pct=5.0, now_fx=1100.0)
        check("intl: 현지 수익률 +10%", abs(_cp["local_pnl_pct"] - 10.0) < 0.01)
        check("intl: 환차익 +10%", abs(_cp["fx_pnl_pct"] - 10.0) < 0.01)
        check("intl: 원화 수익률 +21%(복리)", abs(_cp["pnl_pct"] - 21.0) < 0.01, str(_cp["pnl_pct"]))
        check("intl: ★알파는 **현지** 수익률 - 현지 지수(환율은 종목선택과 무관)",
              abs(_cp["alpha_pct"] - 5.0) < 0.01, str(_cp["alpha_pct"]))
        _cp2 = _pf.compute_position(
            {"country": "US", "ticker": "AAPL", "name": "애플", "avg_price": 100.0,
             "qty": 1, "buy_date": "", "buy_fx": 1000.0}, last_close=110.0, now_fx=None)
        check("intl: 환율 조회 실패면 원화값을 지어내지 않는다",
              _cp2["pnl"] is None and _cp2["local_pnl_pct"] == 10.0)

        # ── v11.14 리포트 린트 ([7.0] 계약 기계 검사) ──
        import report_lint as _rl
        _NL = chr(10)
        _good = ("# 리서치" + _NL + "## 오늘의 픽" + _NL + "표" + _NL
                 + "## 내일 체크포인트" + _NL + "- a" + _NL + "---" + _NL
                 + "## 부록 — 분석 근거" + _NL + "상세")
        _iss, _m = _rl.lint_report(_good)
        check("lint: 깨끗한 리포트는 무경고", _iss == [], str(_iss))
        check("lint: 본문/부록 분리 측정",
              _m["body_b"] > 0 and _m["app_b"] > 0 and _m["md_b"] == _m["body_b"] + _m["app_b"])
        _iss2, _ = _rl.lint_report("## 오늘의 픽 F1 [5.9] force_scores T+1~2")
        check("lint: 금지토큰 4종 + 구분자·체크포인트 누락 = 6건", len(_iss2) == 6,
              "%d건: %s" % (len(_iss2), _iss2))
        _iss3, _m3 = _rl.lint_report(_good, html_bytes=150_000)
        check("lint: 실측 HTML 이 접힘 임계 초과면 경고",
              any("접힌다" in i for i in _iss3) and _m3["est_render_b"] == 150_000)
        _big = _good + "x" * 30_000
        _iss4, _ = _rl.lint_report(_big)
        check("lint: 추정(x4.1)으로도 크기 경고", any("접힌다" in i for i in _iss4))
        _iss5, _ = _rl.lint_report(_good + chr(0x1F600))
        check("lint: 4바이트 이모지 검출", any("4바이트" in i for i in _iss5))
        check("lint: 항상 자문(발송 차단 아님) — 상수 확인",
              _rl.GMAIL_CLIP_B == 102_400 and _rl.MD_BUDGET_B < _rl.GMAIL_CLIP_B / _rl.RENDER_RATIO)

        # ── v11.13 초대제 허용목록 (해시만 올린다) ──
        import portfolio_allowlist as _al
        check("allow: 이메일 정규화(대소문자)", _al.normalize(" A@B.COM ") == "a@b.com")
        check("allow: 형식 아닌 값 거절",
              _al.normalize("없음") is None and _al.normalize("") is None)
        # ★로그에 원문이 새면 안 된다 — mail_config 내용은 밖으로 못 나간다
        _mk = _al.mask("student01@example.kr")
        check("allow: 마스킹이 원문을 안 드러낸다",
              "2024010203" not in _mk and "ushs" not in _mk and _mk.endswith(".kr"), _mk)
        # ★Apps Script 의 hmacHex_ 가 내는 값과 **바이트 단위로 같아야** 한다.
        #   아래 기대값은 Apps Script 의 부호있는 바이트 변환을 Node 로 재현해 얻은 것이다.
        #   어긋나면 대조가 전부 실패해 **아무도 등록하지 못한다**(전원 차단).
        #   한글 주소까지 넣은 이유: UTF-8 인코딩이 양쪽에서 같아야 하기 때문이다.
        _VEC = [
            ("a@b.com",
             "a40b8e6864e2ef145080116b5cad980d41a90860f5d9f5011c0331daf89ed614"),
            ("student01@example.kr",
             "57e55129b434cf63abd69540954c8ea7784131cd1d7d1a84b0ff28f3403fdd4b"),
            ("한글@테스트.com",
             "332cf5d2cc62360f6a32ae11cee0c711aaa3c2debc75a3380f3cadfc60689db4"),
        ]
        for _em, _exp in _VEC:
            _got = _al.hash_all([_em], "test-secret-12345")[0]
            check("allow: ★HMAC 고정 벡터 %s" % _al.mask(_em), _got == _exp,
                  "got=%s exp=%s" % (_got, _exp))
        check("allow: 해시 길이·형식(64자 16진)",
              all(len(h) == 64 and all(c in "0123456789abcdef" for c in h)
                  for h in _al.hash_all(["a@b.com", "한글@테스트.com"], "k")))
        check("allow: SECRET 이 다르면 해시도 다르다",
              _al.hash_all(["a@b.com"], "k1") != _al.hash_all(["a@b.com"], "k2"))
        # ★수신자를 추가하면 허용목록 지문이 반드시 달라져야 한다.
        #   안 달라지면 --sync 가 "변경 없음"으로 넘겨 **새 수신자가 영영 막힌다**
        #   (2026-08-06 실사고: 10번째 수신자가 웹 폼에서 등록 불가였다).
        import hashlib as _hl
        def _fp(ems, sec):
            return _hl.sha256("".join(sorted(_al.hash_all(ems, sec))).encode()).hexdigest()
        _base = ["a@x.com", "b@x.com"]
        check("allow: ★수신자 추가 시 지문 변화",
              _fp(_base, "k") != _fp(_base + ["c@x.com"], "k"))
        check("allow: 순서만 다르면 지문 동일(불필요한 재전송 방지)",
              _fp(_base, "k") == _fp(list(reversed(_base)), "k"))
        check("allow: 지문에 주소 원문이 없다",
              all(e not in _fp(_base, "k") for e in _base))
        check("allow: 같은 입력은 항상 같은 해시",
              _al.hash_all(["a@b.com"], "k") == _al.hash_all(["a@b.com"], "k"))

        # ── v11.13 실사고 대응: 탭 CSV·증권사 추정·코드/이름 대조 ──
        check("pf: 구분자 자동판별(쉼표)", _pf.sniff_delimiter("a,b,c") == ",")
        check("pf: ★탭 구분 파일도 읽는다(엑셀 유니코드 텍스트 저장)",
              _pf.sniff_delimiter("증권사	종목코드	종목명") == "	")
        check("pf: 세미콜론 구분", _pf.sniff_delimiter("a;b;c;d") == ";")
        # ★기존 CSV 가 '카이로스'(노트북 옛 이름)로 적혀 있어도 계속 인식해야 한다.
        #   라벨만 미래에셋으로 바뀌었고 매칭 키워드는 그대로다.
        check("pf: 메모에서 증권사 추정(카이로스 표기도 인식)",
              _pf.broker_from_memo("카이로스(미래에셋)") == "미래에셋")
        check("pf: 옛 표기 '카이로스' 도 자동매매 대상 유지",
              _pf.is_auto_broker("카이로스") is True
              and _pf.is_auto_broker("미래에셋") is True)
        check("pf: 메모에서 증권사 추정(KB)", _pf.broker_from_memo("kb증권") == "KB")
        check("pf: 메모에 증권사 없으면 None", _pf.broker_from_memo("장기보유") is None)
        # ★증권사 열이 없는 옛 파일에서 KB 보유가 '자동매매 대상'으로 잡히면 안 된다
        _rk, _ = _pf.parse_row({"종목코드": "005930", "종목명": "삼성전자",
                                "평단가": "100", "수량": "1", "메모": "kb증권"})
        check("pf: ★메모가 KB 면 자동매매 대상 아님",
              _rk and _rk["broker"] == "KB" and _rk["auto_tradable"] is False, str(_rk))
        _re_, _why = _pf.parse_row({"종목코드": "005930", "종목명": "삼성전자",
                                    "평단가": "", "수량": "1"})
        check("pf: 평단가 빈칸은 0 으로 삼키지 않고 거절",
              _re_ is None and "비어" in _why, "%s / %s" % (_re_, _why))
        # 코드/이름 대조 — 네트워크 없이 조회부만 대체
        check("pf: 이름 정규화(공백·대소문자 무시)",
              _pf._norm_name("삼성 E&A") == _pf._norm_name("삼성E&A"))
        _orig_on = _pf.official_name
        try:
            _pf.official_name = lambda t: {"030420": "디패션", "034020": "두산에너빌리티"}.get(t)
            check("pf: ★코드가 다른 회사면 불일치(030420=디패션)",
                  _pf.verify_ticker_name("030420", "두산에너빌리티") == (False, "디패션"))
            check("pf: 맞으면 통과", _pf.verify_ticker_name("034020", "두산에너빌리티")[0] is True)
            check("pf: 이름을 안 적었으면 검사 안 함",
                  _pf.verify_ticker_name("030420", "")[0] is True)
            check("pf: 조회 불가는 불일치가 아니다(막지 않는다)",
                  _pf.verify_ticker_name("999999", "아무거나")[0] is True)
        finally:
            _pf.official_name = _orig_on
        check("pf: 이메일 아닌 파일명은 None",
              _pf.email_from_path("portfolios/_README.txt") is None
              and _pf.email_from_path("portfolios/notanemail.csv") is None)
        # ★증권사가 다르면 같은 종목이어도 합치지 않는다(자동매매 가능분이 틀어진다)
        _mb = _pf.merge_lots([
            {"ticker": "005930", "name": "삼성", "avg_price": 100, "qty": 1,
             "buy_date": "", "memo": "", "broker": "카이로스", "auto_tradable": True},
            {"ticker": "005930", "name": "삼성", "avg_price": 200, "qty": 1,
             "buy_date": "", "memo": "", "broker": "KB", "auto_tradable": False}])
        check("pf: ★증권사 다르면 분리 보관", len(_mb) == 2, str(_mb))
        # 매수일시 없어도 파싱 성공(불타기/물타기라 선택 필드)
        _rn, _ = _pf.parse_row({"증권사": "KB", "종목코드": "005930", "종목명": "삼성",
                                "평단가": "100", "수량": "1"})
        check("pf: 매수일시 없어도 통과 + 증권사 반영",
              _rn and _rn["buy_date"] == "" and _rn["auto_tradable"] is False, str(_rn))
        # 동기화: 이메일 대소문자 통합·불량행 격리
        import portfolio_sync as _ps
        _by, _bad = _ps.group_by_email([
            {"email": "A@Example.COM", "broker": "카이로스", "ticker": "34020",
             "name": "두산", "avg_price": "50,000", "qty": "3", "memo": ""},
            {"email": "a@example.com", "broker": "KB", "ticker": "005930",
             "name": "삼성", "avg_price": "300000", "qty": "2", "memo": ""},
            {"email": "없음", "broker": "카이로스", "ticker": "005930",
             "name": "x", "avg_price": "1", "qty": "1", "memo": ""},
            {"email": "b@x.com", "broker": "카이로스", "ticker": "abc",
             "name": "x", "avg_price": "1", "qty": "1", "memo": ""}])
        check("pfsync: 이메일 대소문자 통합(1명 2종)",
              list(_by) == ["a@example.com"] and len(_by["a@example.com"]) == 2, str(_by))
        check("pfsync: 선행 0 복원 + 엑셀 안전표기",
              _by["a@example.com"][0]["종목코드"] == '="034020"')
        check("pfsync: 불량행 2건 격리(조용히 넘기지 않음)", len(_bad) == 2, str(_bad))
        check("pfsync: 매수일시는 웹에서 안 받는다",
              _by["a@example.com"][0]["매수일시"] == "")

        # ★★유출 가드 — 여러 명이 등록됐을 때 이름 없는 산출물을 쓰면
        #   B 가 A 의 보유·전략 글을 그대로 받는다. 실제로 재현해서 막혔는지 본다.
        import portfolio_mail as _pm
        _tdp = tempfile.mkdtemp(prefix="pfleak_")
        try:
            _pfd, _sd = os.path.join(_tdp, "pf"), os.path.join(_tdp, "sess")
            os.makedirs(_pfd); os.makedirs(_sd)
            _csv = chr(10).join([
                "증권사,종목코드,종목명,평단가,수량,메모,매수일시",
                '카이로스,"=""005930""",삼성전자,100,1,,', ""])
            for _em in ("a@x.com", "b@y.com"):
                io.open(os.path.join(_pfd, _em + ".csv"), "w",
                        encoding="utf-8-sig").write(_csv)
            # A 것만 '이름 없는' 구버전 파일로 존재(email 필드 없음 = 소유자 검사 무력)
            json.dump({"holdings": [{"name": "삼성전자"}], "total": {}},
                      io.open(os.path.join(_sd, "portfolio_review.json"), "w",
                              encoding="utf-8"), ensure_ascii=False)
            io.open(os.path.join(_sd, "portfolio_strategy.md"), "w",
                    encoding="utf-8").write("A 의 계좌 이야기")

            _orig_dir, _orig_argv = _pm.PORTFOLIO_DIR, sys.argv
            try:
                _pm.PORTFOLIO_DIR = _pfd
                sys.argv = ["x", "--all", "--session", _sd]
                _rc2 = _pm.main()          # --send 없음 = 미리보기만
            finally:
                _pm.PORTFOLIO_DIR, sys.argv = _orig_dir, _orig_argv
            _leaked = [f for f in os.listdir(_sd) if f.endswith(".html")
                       and "A 의 계좌 이야기" in io.open(os.path.join(_sd, f),
                                                    encoding="utf-8").read()]
            check("pfmail: ★2명일 때 이름없는 산출물로 발송하지 않는다",
                  _rc2 != 0 and not _leaked, "rc=%s leaked=%s" % (_rc2, _leaked))

            # 1명이면 하위호환 — 이름 없는 파일을 본인에게 쓴다
            os.remove(os.path.join(_pfd, "b@y.com.csv"))
            for _f in os.listdir(_sd):
                if _f.endswith(".html"):
                    os.remove(os.path.join(_sd, _f))
            try:
                _pm.PORTFOLIO_DIR = _pfd
                sys.argv = ["x", "--all", "--session", _sd]
                _rc1 = _pm.main()
            finally:
                _pm.PORTFOLIO_DIR, sys.argv = _orig_dir, _orig_argv
            _used = [f for f in os.listdir(_sd) if f.endswith(".html")
                     and "A 의 계좌 이야기" in io.open(os.path.join(_sd, f),
                                                  encoding="utf-8").read()]
            check("pfmail: 1명이면 이름없는 파일 폴백은 유지(하위호환)",
                  _rc1 == 0 and len(_used) == 1, "rc=%s used=%s" % (_rc1, _used))
        finally:
            shutil.rmtree(_tdp, ignore_errors=True)

        # ── v11.11 프리마켓 지표 — '맹신 금지' 설계가 코드로 지켜지는지 고정 ──
        import premarket_signals as _ps
        _lv = [{"price": 1000 + i, "bid_qty": 100, "ask_qty": 50} for i in range(10)]
        _bi, _n = _ps.book_imbalance(_lv)          # 매수 1000 / 매도 500
        check("pms: 불균형 매수우위 산출", abs(_bi - 0.3333) < 0.001 and "매수 우위" in _n, str((_bi, _n)))
        _thin = [{"price": 1000, "bid_qty": 5, "ask_qty": 3}]
        _bi2, _n2 = _ps.book_imbalance(_thin)
        check("pms: ★얇은 호가면 불균형 무효화(None)", _bi2 is None and "얇다" in _n2, str((_bi2, _n2)))
        check("pms: 호가 없으면 None", _ps.book_imbalance(None)[0] is None)
        _sp, _spn = _ps.spread_pct(1000, 1005)
        check("pms: 스프레드 계산", abs(_sp - 0.498) < 0.01, str(_sp))
        check("pms: 역전 호가는 None", _ps.spread_pct(1005, 1000)[0] is None)
        _g, _gn = _ps.gap_pct(10600, 10000)
        check("pms: 갭 +6%는 추격 금지 경고", abs(_g - 6.0) < 0.01 and "추격 금지" in _gn, str((_g, _gn)))
        check("pms: 전일종가 0이면 None", _ps.gap_pct(1000, 0)[0] is None)
        # ★ladder_ok=False 면 호가 지표를 만들지 않는다(추측 금지)
        _sig = _ps.compute({"quote": {"price": 10200}, "ladder": {"levels": _lv},
                            "quote_ok": True, "ladder_ok": False}, prev_close=10000)
        check("pms: ★ladder_ok=false면 호가지표 전부 None",
              _sig["book_imbalance"] is None and _sig["spread_pct"] is None
              and _sig["gap_pct"] == 2.0, str(_sig)[:120])
        # evaluate: 지표는 '막는 쪽'으로만 강하게 작동한다
        _ok = _ps.compute({"quote": {"price": 10200},
                           "ladder": {"levels": _lv, "best_bid": 10200, "best_ask": 10205},
                           "quote_ok": True, "ladder_ok": True}, prev_close=10000)
        _r = _ps.evaluate({"require": {"book_imbalance_min": 0.15,
                                       "gap_between": [-1, 3]}}, _ok)
        check("pms: 조건 충족이면 go", _r["verdict"] == "go", str(_r))
        _hi = _ps.compute({"quote": {"price": 10800},
                           "ladder": {"levels": _lv, "best_bid": 10800, "best_ask": 10805},
                           "quote_ok": True, "ladder_ok": True}, prev_close=10000)
        _r2 = _ps.evaluate({"require": {"book_imbalance_min": 0.15}}, _hi)
        check("pms: ★갭 +8%면 지표가 좋아도 avoid",
              _r2["verdict"] == "avoid" and any("추격 금지" in b for b in _r2["blocks"]), str(_r2))
        _r3 = _ps.evaluate({}, _ok)
        check("pms: ★정량조건 없으면 지표만으로 go 안 함",
              _r3["verdict"] == "hold" and any("지표만으로는" in x for x in _r3["reasons"]), str(_r3))
        _bad = _ps.compute({"quote": {"price": 10200}, "ladder": {}, "quote_ok": False,
                            "ladder_ok": False}, prev_close=10000)
        _r4 = _ps.evaluate({"require": {"gap_between": [-1, 3]}}, _bad)
        check("pms: quote_ok=false면 avoid", _r4["verdict"] == "avoid", str(_r4))
        _r5 = _ps.evaluate({"require": {"book_imbalance_min": 0.1}}, _sig)   # ladder 실패
        check("pms: 불균형 못 구하면 판정 불가(hold)", _r5["verdict"] == "hold", str(_r5))

        # ── v11.9 주문 제안 순수계산 — 돈이 걸린 산수라 하네스로 고정한다 ──
        import order_plan as _op
        # 진입시점 규율: 선언가 위로 달아났으면 추격하지 않는다(즉시고점 실패의 원인)
        _p, _n = _op.plan_price("당일눌림", 10000, 9500)
        check("order: 눌림 도달이면 선언가 이하로 매수", _p == 10000 and "눌림 도달" in _n, str((_p, _n)))
        _p, _n = _op.plan_price("당일눌림", 10000, 10300)
        check("order: 선언가 +3%면 대기 지정가(밴드 내)", _p == 10000 and "대기" in _n, str((_p, _n)))
        _p, _n = _op.plan_price("당일눌림", 10000, 10600)
        check("order: 선언가 +6%면 추격 금지(None)", _p is None and "추격 금지" in _n, str((_p, _n)))
        check("order: 익일이후는 오늘 주문 아님",
              _op.plan_price("익일이후", 10000, 9000)[0] is None)
        check("order: 당일시가는 장중이면 시점 지남",
              _op.plan_price("당일시가", 10000, 9900, phase="intraday")[0] is None)
        check("order: 당일시가는 장전이면 유효",
              _op.plan_price("당일시가", 10000, None, phase="pre_open")[0] == 10000)
        # 수량 산정: 예수금·비중·한도(10주·200만원)·수수료
        _q, _nn = _op.plan_qty(1_000_000, 8, 10000)
        check("order: 예수금 100만·비중8%·1만원 -> 8주", _q == 8, str((_q, _nn)))
        _q, _ = _op.plan_qty(100_000_000, 50, 10000)
        check("order: 수량한도 10주 캡", _q == 10, str(_q))
        _q, _ = _op.plan_qty(100_000_000, 50, 500_000)
        check("order: 금액한도 200만원 캡(500,000원 -> 4주)", _q == 4, str(_q))
        _q, _nn = _op.plan_qty(593, 8, 10000)
        check("order: 예수금 593원이면 0주 + 사유", _q == 0 and "못 산다" in _nn, str((_q, _nn)))
        _q, _ = _op.plan_qty(10_000, 100, 10_000)
        check("order: 수수료 때문에 1주도 못 사면 0주", _q == 0, str(_q))
        # 매도 신호는 픽이 스스로 선언한 계약 기준
        check("order: 손절선 이탈 -> 손절",
              _op.sell_signal({"currently_below_stop": True})[0] == "손절")
        check("order: 목표 도달 -> 익절", _op.sell_signal({"target_hit": True})[0] == "익절")
        check("order: 신호 없으면 None", _op.sell_signal({})[0] is None)
        check("order: 만기 경과 -> 만기청산",
              _op.sell_signal({}, horizon_expired=True)[0] == "만기청산")
        # 자체 검증 — 어차피 서버가 422 낼 것을 사람에게 보이지 않는다
        check("order: last_price 없으면 밴드검사 불가로 문제 보고",
              any("가격밴드 검사 불가" in e for e in
                  _op.validate_proposal({"qty": 1, "price": 1000, "side": "buy"})))
        check("order: 금액한도 초과 검출",
              any("금액한도" in e for e in _op.validate_proposal(
                  {"qty": 10, "price": 500_000, "last_price": 500_000, "side": "buy"})))
        check("order: 정상 매수 제안은 문제 없음",
              _op.validate_proposal({"qty": 5, "price": 10_000,
                                     "last_price": 10_100, "side": "buy"}) == [])
        check("order: 보유 없는 매도는 거부",
              any("보유수량" in e for e in _op.validate_proposal(
                  {"qty": 1, "price": 1000, "last_price": 1000, "side": "sell"})))
        # ★fire 는 명시 승인 없이는 호출 자체가 막혀야 한다(주문 제출로 이어지는 유일 지점)
        import trade_client as _tc
        try:
            _tc.fire(10000, 10000)
            check("order: fire 는 명시 승인 없이 막힌다", False, "예외가 안 났다")
        except _tc.TradeError as _e:
            check("order: fire 는 명시 승인 없이 막힌다", "명시적 승인" in str(_e), str(_e))
        try:
            _tc.arm("005930", "buy", 0)
            check("order: arm 수량 0 거부", False, "예외가 안 났다")
        except _tc.TradeError:
            check("order: arm 수량 0 거부", True)
        try:
            _tc.arm("005930", "long", 1)
            check("order: arm side 검증", False, "예외가 안 났다")
        except _tc.TradeError:
            check("order: arm side 검증", True)

        check("venue: 시간외 종가는 가격 고정 명시",
              any("종가" in x["method"] for x in _v1545["open_venues"]
                  if x["venue"] == "KRX 시간외 종가"))
    except ImportError:
        print("[SKIP] intraday: intraday_review import 불가")

    # ── v11.1 익일 채점 배선 — T+1 은 next_day, T+5 는 본 콜로 갈라져 채점되는가 ──
    try:
        import pandas as _pdN
        import accuracy_tracker as _atN
        # ★픽스처 날짜는 항상 과거로(v11.8 가드: 오늘 이후 봉 정산 금지 — 실행일과 겹치면 보류됨.
        #   2026-08-04 실측: 구 픽스처 마지막 날짜가 실행일과 같아 T+5 가 보류돼 위양성 실패)
        _idxN = _pdN.to_datetime(["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04",
                                  "2026-06-05", "2026-06-08", "2026-06-09"])
        # T+1 -3%(하락) · T+5 +2%(상승) — 두 지평 방향이 반대인 케이스
        _dfN = _pdN.DataFrame({"Close": [100.0, 100.0, 97.0, 98.0, 99.0, 101.0, 102.0]}, index=_idxN)
        _oldN = _atN._fetch_history
        try:
            _atN._fetch_history = lambda s, start: _dfN
            _callN = {"dir": "up", "prob_up": 0.5, "prob_flat": 0.3, "prob_down": 0.2,
                      "next_day": {"dir": "down", "prob_up": 0.2, "prob_flat": 0.2, "prob_down": 0.6}}
            _r1 = _atN.grade_market_call("2026-06-02", "kospi", _callN["next_day"], 1)
            _r5 = _atN.grade_market_call("2026-06-02", "kospi", _callN, 5)
            check("nextday: T+1 은 next_day(down)로 적중", _r1 and _r1.get("hit") is True, str(_r1))
            check("nextday: T+5 는 본 콜(up)로 적중", _r5 and _r5.get("hit") is True, str(_r5))
            check("nextday: 두 지평이 서로 다른 방향을 각각 채점",
                  _r1.get("dir") == "down" and _r5.get("dir") == "up")
            # 본 콜을 T+1 에 쓰면 틀린다 — next_day 분리의 실익 확인
            _r1_old = _atN.grade_market_call("2026-06-02", "kospi", _callN, 1)
            check("nextday: 구 방식(본 콜을 T+1 에)이었다면 오답이었을 것",
                  _r1_old and _r1_old.get("hit") is False, str(_r1_old))
        finally:
            _atN._fetch_history = _oldN
    except ImportError:
        print("[SKIP] nextday: pandas/accuracy_tracker 불가")

    # ── v11.0 Taildrop 수신 검증 — 전송 손상·엉뚱한 화면·zip slip 을 전부 막는가 ──
    try:
        import zipfile as _zf
        import hashlib as _hl6
        import struct as _st6
        import zlib as _zl6
        import taildrop_receive as _tr

        def _png6():
            def _ck(t, d):
                c = t + d
                return _st6.pack(">I", len(d)) + c + _st6.pack(">I", _zl6.crc32(c) & 0xffffffff)
            return (b"\x89PNG\r\n\x1a\n"
                    + _ck(b"IHDR", _st6.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                    + _ck(b"IDAT", _zl6.compress(b"\x00\xff\xff\xff")) + _ck(b"IEND", b""))

        _P6 = _png6()
        _H6 = _hl6.sha256(_P6).hexdigest()
        _td6 = tempfile.mkdtemp(prefix="td6_")
        _zp6 = os.path.join(_td6, "kairos_test.zip")
        _man6 = {"sent_at_kst": "2026-07-29 01:00", "files": [
            {"file": "0231_a.png", "sha256": _H6, "marker_text": "[0231] 관심종목", "settled": True},
            {"file": "0261_b.png", "sha256": "0" * 64, "marker_text": "[0261] x", "settled": True},
            {"file": "0313_c.png", "sha256": _H6, "marker_text": "[0254] 엉뚱", "settled": True},
            {"file": "0235_d.png", "sha256": _H6, "marker_text": "[0235] 공매도", "settled": False},
        ]}
        with _zf.ZipFile(_zp6, "w") as _z:
            _z.writestr("manifest.json", json.dumps(_man6, ensure_ascii=False))
            for _n in ("0231_a.png", "0261_b.png", "0313_c.png", "0235_d.png"):
                _z.writestr(_n, _P6)
            _z.writestr("../evil.png", _P6)        # zip slip
            _z.writestr("notes.txt", b"x")         # 비허용 확장자
        _img6 = os.path.join(_td6, "imgs")
        os.makedirs(_img6)
        _f6, _r6, _m6 = _tr.process_zip(_zp6, _img6)
        _reasons = " | ".join(x["reason"] for x in _r6)
        check("taildrop: 검증 통과분만 저장(1장)", len(_f6) == 1 and _f6[0]["file"] == "0231_a.png",
              str([x["file"] for x in _f6]))
        check("taildrop: 실제 저장 파일도 1개", os.listdir(_img6) == ["0231_a.png"],
              str(os.listdir(_img6)))
        check("taildrop: sha256 불일치 차단", "sha256 불일치" in _reasons)
        check("taildrop: marker 불일치 차단", "marker 불일치" in _reasons)
        check("taildrop: settled=false 차단", "settled=false" in _reasons)
        check("taildrop: zip slip(../) 차단",
              any(x["file"].endswith("evil.png") for x in _r6), _reasons[:60])
        check("taildrop: 비허용 확장자 차단", any(x["file"] == "notes.txt" for x in _r6))
        check("taildrop: manifest 파싱", _m6.get("sent_at_kst") == "2026-07-29 01:00")
        # 파일명 규약이 없으면(화면번호 접두 없음) marker 대조를 건너뛰되 sha 는 본다
        _zp7 = os.path.join(_td6, "kairos_noprefix.zip")
        with _zf.ZipFile(_zp7, "w") as _z:
            _z.writestr("manifest.json", json.dumps(
                {"files": [{"file": "plain.png", "sha256": _H6, "settled": True}]}))
            _z.writestr("plain.png", _P6)
        _img7 = os.path.join(_td6, "imgs7")
        os.makedirs(_img7)
        _f7, _r7, _ = _tr.process_zip(_zp7, _img7)
        check("taildrop: 화면번호 접두 없으면 marker 대조 생략(sha 는 검사)",
              len(_f7) == 1 and not _r7, str(_r7))
        check("taildrop: zip 이름 패턴", bool(_tr.ZIP_PAT.match("kairos_20260729.zip"))
              and not _tr.ZIP_PAT.match("other.zip"))

        # ── v11.2 배치 알림(ALERT_*.txt) — zip 이 안 와도 '왜 없는지'를 알아야 한다 ──
        _tda = tempfile.mkdtemp(prefix="alr_")
        io.open(os.path.join(_tda, "ALERT_0700.txt"), "w", encoding="utf-8").write(
            "batch failed: login_screen")
        io.open(os.path.join(_tda, "ALERT_1630.txt"), "w", encoding="utf-8").write(
            "reason=partial 3/10 ok")
        io.open(os.path.join(_tda, "other.txt"), "w", encoding="utf-8").write("x")
        _al = _tr.read_alerts(_tda)
        check("alert: ALERT_*.txt 2건만 인식(다른 txt 무시)", len(_al) == 2, str([a["file"] for a in _al]))
        check("alert: login_screen 사유 해석",
              any(a["reason"] == "login_screen" and "로그인" in a["meaning"] for a in _al))
        check("alert: partial 사유 해석 — '나머지는 정상 도착'",
              any(a["reason"] == "partial" and "나머지" in a["meaning"] for a in _al))
        io.open(os.path.join(_tda, "ALERT_0800.txt"), "w", encoding="utf-8").write("무슨 소리인지 모름")
        check("alert: 미상 사유도 버리지 않고 unknown 으로 남긴다",
              any(a["reason"] == "unknown" for a in _tr.read_alerts(_tda)))

        # ── v11.2 공휴일 stale 가드 — 배치는 공휴일에도 돌고 낡은 zip 이 '정상처럼' 온다 ──
        from datetime import date as _dT
        _s0 = _tr.stale_check({"sent_at_kst": "2026-07-31 07:00"}, today=_dT(2026, 7, 31))
        check("stale: 당일 배치는 age 0", _s0["age_days"] == 0 and "당일" in _s0["note"])
        _s2 = _tr.stale_check({"sent_at_kst": "2026-07-29 07:00"}, today=_dT(2026, 7, 31))
        check("stale: 2일 전이면 경고", _s2["age_days"] == 2 and "공휴일" in _s2["note"])
        _sn = _tr.stale_check({}, today=_dT(2026, 7, 31))
        check("stale: 시각 미상이면 None + 직접확인 안내",
              _sn["age_days"] is None and "직접 확인" in _sn["note"])
        _sc = _tr.stale_check({"capture": {"captured_at_kst": "2026-07-30 16:30"}},
                              today=_dT(2026, 7, 31))
        check("stale: capture.captured_at_kst 우선 사용", _sc["age_days"] == 1, str(_sc))
    except ImportError:
        print("[SKIP] taildrop: taildrop_receive import 불가")

    # ── v10.8 카이로스 캡처 3중 검증 — '엉뚱한 화면이 조용히 통과'가 최대 위험 ──
    #   marker_text 는 노트북이 창 제목에서 '실제로 읽은' 값이다(요청값 반향 아님).
    try:
        import base64 as _b64
        import hashlib as _hl5
        import struct as _st5
        import zlib as _zl5
        import kairos_client as _kc

        def _png5():
            def _ck(t, d):
                c = t + d
                return _st5.pack(">I", len(d)) + c + _st5.pack(">I", _zl5.crc32(c) & 0xffffffff)
            return (b"\x89PNG\r\n\x1a\n"
                    + _ck(b"IHDR", _st5.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                    + _ck(b"IDAT", _zl5.compress(b"\x00\xff\xff\xff"))
                    + _ck(b"IEND", b""))

        _p5 = _png5()
        _b5 = _b64.b64encode(_p5).decode()
        _s5 = _hl5.sha256(_p5).hexdigest()
        _meta_ok = {"marker_text": "[0231] 관심종목 신용/공매도/대차 현황"}
        _cap_ok = {"png_b64": _b5, "sha256": _s5, "settled": True}

        ok, why, png = _kc.verify_capture("0231", _meta_ok, _cap_ok)
        check("kairos: 정상 캡처 통과", ok and png.startswith(b"\x89PNG"), why)
        # ★다른 화면번호 → 폐기(요청한 0231 이 아닌 0254 가 열림)
        ok2, why2, _ = _kc.verify_capture(
            "0231", {"marker_text": "[0254] 투자자 일별 매매현황"}, _cap_ok)
        check("kairos: 다른 화면번호 폐기", (not ok2) and "엉뚱한 화면" in why2, why2)
        # 로그인 화면이 찍힘
        ok3, why3, _ = _kc.verify_capture("0231", {"marker_text": "미래에셋 로그인"}, _cap_ok)
        check("kairos: 로그인 화면 폐기", not ok3, why3)
        # marker 없음
        ok4, why4, _ = _kc.verify_capture("0231", {}, _cap_ok)
        check("kairos: marker_text 없으면 폐기", (not ok4) and "marker" in why4, why4)
        # sha256 불일치(전송 손상·중간 교체)
        ok5, why5, _ = _kc.verify_capture(
            "0231", _meta_ok, dict(_cap_ok, sha256="0" * 64))
        check("kairos: sha256 불일치 폐기", (not ok5) and "sha256" in why5, why5)
        # settled=false → 폐기하되 png 는 돌려준다(재요청 판단용)
        ok6, why6, png6 = _kc.verify_capture("0231", _meta_ok, dict(_cap_ok, settled=False))
        check("kairos: settled=false 폐기", (not ok6) and "settled" in why6, why6)
        # ★v11.2 masked 의미 변경(Tesseract 설치 후): 0=정상(가린 개수), -1=마스킹 실패
        ok_m0, _, _ = _kc.verify_capture("0231", _meta_ok, dict(_cap_ok, masked=0))
        check("kairos: masked=0 은 정상 통과", ok_m0)
        ok_m3, _, _ = _kc.verify_capture("0231", _meta_ok, dict(_cap_ok, masked=3))
        check("kairos: masked=3(가린 개수) 통과", ok_m3)
        ok_mn, why_mn, png_mn = _kc.verify_capture("0231", _meta_ok, dict(_cap_ok, masked=-1))
        check("kairos: masked=-1(마스킹 실패) 폐기 — 계좌 노출 위험",
              (not ok_mn) and "masked=-1" in why_mn, why_mn)
        check("kairos: 마스킹 실패 시 png 도 반환하지 않는다", png_mn == b"")
        check("kairos: settled=false 여도 png 는 반환(재요청 판단)", png6 != b"")
        # PNG 아님
        _bad = _b64.b64encode(b"XX").decode()
        ok7, why7, _ = _kc.verify_capture(
            "0231", _meta_ok, {"png_b64": _bad, "sha256": _hl5.sha256(b"XX").hexdigest()})
        check("kairos: PNG 시그니처 아니면 폐기", (not ok7) and "PNG" in why7, why7)

        # 토큰 로더: 두 형식 + 없으면 ''
        _td = tempfile.mkdtemp(prefix="kt_")
        _f1 = os.path.join(_td, "t1.txt")
        io.open(_f1, "w", encoding="utf-8").write("# 주석\ntoken=abc123\n")
        check("kairos: token= 형식 로드", _kc.load_token(_f1) == "abc123", _kc.load_token(_f1))
        _f2 = os.path.join(_td, "t2.txt")
        io.open(_f2, "w", encoding="utf-8").write("plainTOKEN\n")
        check("kairos: 한 줄 형식 로드", _kc.load_token(_f2) == "plainTOKEN")
        check("kairos: 파일 없으면 빈 문자열", _kc.load_token(os.path.join(_td, "no.txt")) == "")

        # set_watchlist 종목코드 검증 — ★"12" 같은 짧은 오타를 zfill 로 채우면 000012 라는
        #   **다른 종목**이 조용히 캡처된다. 4자리 미만은 거부해야 한다.
        try:
            _kc.set_watchlist(["abc", "12", "종목"])
            check("kairos: 짧은 오타/문자는 전부 거부되어 예외", False, "예외 미발생")
        except _kc.KairosError:
            check("kairos: 짧은 오타/문자는 전부 거부되어 예외", True)
        _norm = []
        _old_call = _kc.call
        try:
            _kc.call = lambda path, **kw: _norm.append(kw.get("body")) or {"ok": True}
            _kc.set_watchlist(["5930", "000660", "68270"])
            check("kairos: 4~6자리는 zfill 로 정규화",
                  _norm and _norm[0]["tickers"] == ["005930", "000660", "068270"],
                  str(_norm))
        finally:
            _kc.call = _old_call

        import hts_capture_collect as _hc
        check("kairos: 카탈로그 필수필드", all(
            v.get("no") and v.get("name") and v.get("why") is not None
            and "needs_watchlist" in v for v in _hc.SCREENS.values()))
        check("kairos: 관심종목 필요 화면은 0231/0261",
              {k for k, v in _hc.SCREENS.items() if v["needs_watchlist"]} ==
              {"short_lend", "foreign_inst"})
        check("kairos: 9314 는 사용 불가로 분리(카이로스 미지원)",
              "night_fut_investor" in _hc.UNAVAILABLE
              and "night_fut_investor" not in _hc.SCREENS)
        check("kairos: 기본 화면 4종이 카탈로그에 존재",
              all(k in _hc.SCREENS for k in _hc.DEFAULT_SCREENS), str(_hc.DEFAULT_SCREENS))

        # ★0313 원월물 경고 — 실측 2026-07-29 화면이 '03월물(27)'(2027-03)이었다.
        #   원월물 베이시스는 유동성이 없어 프로그램 압력 신호로 쓸 수 없다.
        from datetime import date as _dK
        check("kairos: 최근월물 — 7월이면 9월물", _hc.front_futures_month(_dK(2026, 7, 29)) == "2026-09",
              str(_hc.front_futures_month(_dK(2026, 7, 29))))
        check("kairos: 만기 당일(9/10 둘째목)까지는 9월물",
              _hc.front_futures_month(_dK(2026, 9, 10)) == "2026-09")
        check("kairos: 만기 다음날 12월물로 롤오버",
              _hc.front_futures_month(_dK(2026, 9, 11)) == "2026-12")
        check("kairos: 12월 만기 후 이듬해 3월물",
              _hc.front_futures_month(_dK(2026, 12, 11)) == "2027-03")
        check("kairos: 연초는 3월물", _hc.front_futures_month(_dK(2027, 1, 5)) == "2027-03")
        # 점검창(02:30~05:10) — 이 안의 hts=false 는 정상이라 사람을 부르지 않는다
        check("kairos: 점검창 경계(02:30 포함)", _hc.in_maintenance("02:30") is True)
        check("kairos: 점검창 직전(02:29)은 정상", _hc.in_maintenance("02:29") is False)
        check("kairos: 점검창 끝(05:10 포함)", _hc.in_maintenance("05:10") is True)
        check("kairos: 05:11 은 정상", _hc.in_maintenance("05:11") is False)
        check("kairos: 06:30 분석시각은 정상", _hc.in_maintenance("06:30") is False)
        # 변형 카탈로그(생략 시 전 변형 순회 = 0254 는 15장)
        check("kairos: 0254 변형 15종 등재", len(_hc.VARIANTS["investor_daily"]) == 15,
              str(len(_hc.VARIANTS["investor_daily"])))
        check("kairos: 0273·0214 변형 2종씩",
              len(_hc.VARIANTS["program_daily"]) == 2 and len(_hc.VARIANTS["broker_3d"]) == 2)
    except ImportError:

        print("[SKIP] htscap: hts_capture_collect import 불가")

    # ── v10.2 아카이브 섹션 분류 — '회피 경고' 섹션이 롱 픽으로 채점되던 결함 고정 ──
    #   리포트가 "사지 마라"고 쓴 종목이 추천으로 집계되면 회고 전체가 오염된다.
    try:
        import retro_archive_parse as _rap
        _excl = [
            "## 2-주의. 관망/주의 종목 (실적·급등·분배 확인 전 보류)",
            "## 2-주의. 매수 회피 — 강한 분산(force <= -40) 종목",
            "## 2-주의. 강한 분산 경계 섹션 (force <= -40 또는 과열 — 추천 표 제외)",
            "## 2-주의. 주의 종목 (force_score <= -40 — 추천 표 제외)",
            "## 2-주의. 잡주 (매수 금지)",
        ]
        for _h in _excl:
            check("archsec: 회피 섹션 제외 — %s" % _h[:34],
                  _rap._section_kind(_h) == "exclude", str(_rap._section_kind(_h)))
        # 정상 추천 섹션은 pick 으로 남아야 한다(과잉 제외 방지 — 고위험이라고 추천이 아닌 건 아니다)
        _keep = [
            "## 2. 타점 진입 대기 종목 TOP",
            "## 2. 타점 진입 대기 종목 TOP (위험회피 국면 — 소수·소액·분할)",
            "## 2-중소형. 중소형 고변동성 모멘텀 (고위험 — [단기스윙] 위주)",
            "## 2-중소형. 중소형 고변동성 모멘텀 (관찰 — 고위험)",
        ]
        for _h in _keep:
            check("archsec: 정상 추천 섹션 유지 — %s" % _h[:34],
                  _rap._section_kind(_h) == "pick", str(_rap._section_kind(_h)))
        check("archsec: 숏 섹션은 short",
              _rap._section_kind("## 3. 투자 주의 & 숏(Short) 전략") == "short")
    except ImportError:
        print("[SKIP] archsec: retro_archive_parse import 불가")

    # ── v10.2 DART 포화 플래그 — 100건 상한에 잘린 건수가 '무공시'로 읽히는 것 차단 ──
    try:
        import sys as _sys
        import retro_label as _rl
        _saved = (_rl._DART_KEY, _rl._CORP_MAP, _rl._disc)

        class _Resp:
            def __init__(self, j): self._j = j
            def json(self): return self._j

        class _RqStub:
            payload = None
            @staticmethod
            def get(*a, **k): return _Resp(_RqStub.payload)

        class _DiscStub:
            @staticmethod
            def classify(nm): return ("증자(희석)", 1.0)

        _oldrq = _sys.modules.get("requests")
        try:
            _rl._DART_KEY = "k" * 40
            _rl._CORP_MAP = {"005930": "00126380"}
            _rl._disc = _DiscStub()
            _sys.modules["requests"] = _RqStub
            _base = date(2026, 1, 5)
            _item = {"report_nm": "유상증자결정", "rcept_dt": "20260106"}
            # 100건 꽉 참 → 포화
            _RqStub.payload = {"status": "000", "total_count": 250, "list": [_item] * 100}
            _r = _rl.holding_distribution("005930", _base, 5)
            check("darttrunc: 100건 포화 시 truncated=True",
                  _r.get("dist_disc_truncated") is True, str(_r)[:90])
            check("darttrunc: 포화여도 건수는 실린다(하한값)",
                  _r.get("dist_disc_count") == 100, str(_r.get("dist_disc_count")))
            # 소량 → 비포화
            _RqStub.payload = {"status": "000", "total_count": 3, "list": [_item] * 3}
            _r2 = _rl.holding_distribution("005930", _base, 5)
            check("darttrunc: 소량이면 truncated=False",
                  _r2.get("dist_disc_truncated") is False, str(_r2)[:90])
            # 013(무공시)은 0 — '확인 불가'(컬럼 부재)와 구분되는 기존 계약 유지
            _RqStub.payload = {"status": "013"}
            _r3 = _rl.holding_distribution("005930", _base, 5)
            check("darttrunc: 무공시는 0 유지(부재와 구분)",
                  _r3 == {"dist_disc_count": 0}, str(_r3))
            check("darttrunc: 포화 플래그가 ENRICH_COLS 에 등록됨",
                  "dist_disc_truncated" in _rl.ENRICH_COLS, str(_rl.ENRICH_COLS))
        finally:
            _rl._DART_KEY, _rl._CORP_MAP, _rl._disc = _saved
            if _oldrq is not None:
                _sys.modules["requests"] = _oldrq
            else:
                _sys.modules.pop("requests", None)
    except ImportError:
        print("[SKIP] darttrunc: retro_label import 불가")

    # ── v10.1 공매도 공표지연 컷(A25) — 룩어헤드 차단 로직을 스텁으로 실검증 ──
    #   라이브에서는 세션 스냅샷 우선(#C1) + KRX 차단이라 이 폴백 경로가 잘 안 타므로,
    #   합성 DataFrame 으로 '최근 N행 제거'가 실제로 동작하는지 못 박아 둔다.
    try:
        import pandas as _pd
        import short_collect as _sc
        _idx = _pd.to_datetime(["2026-07-13", "2026-07-14", "2026-07-15",
                                "2026-07-16", "2026-07-17", "2026-07-20"])
        _df = _pd.DataFrame({"비중": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
                             "공매도잔고": [100, 200, 300, 400, 500, 600]}, index=_idx)

        class _KrxStub:
            @staticmethod
            def get_shorting_balance_by_date(b, e, t):
                return _df.copy()

        _old_krx, _old_cache = _sc._krx, dict(_sc._SHORT_ASOF_CACHE)
        try:
            _sc._krx = _KrxStub()
            _sc._SHORT_ASOF_CACHE.clear()
            # asof=07-21 → 거래일 컷으로 6행 전부 생존 → 공표지연 3행 제거 → 마지막은 07-15(비중 3.0)
            _lag = _sc.get_short_asof("005930", "20260721", pub_lag_rows=3)
            _sc._SHORT_ASOF_CACHE.clear()
            _nolag = _sc.get_short_asof("005930", "20260721", pub_lag_rows=0)
            check("shortlag: 지연 보정 시 최근 3행 제외(07-15 값)",
                  _lag.get("pre_short_balance_ratio") == 3.0, str(_lag))
            check("shortlag: 보정 없으면 최신행(07-20 값)",
                  _nolag.get("pre_short_balance_ratio") == 6.0, str(_nolag))
            check("shortlag: 보정판이 미보정판과 다르다(룩어헤드 제거 증거)",
                  _lag.get("pre_short_balance_ratio") != _nolag.get("pre_short_balance_ratio"))
            # 캐시 키에 지연값이 포함돼야 서로 오염되지 않는다
            check("shortlag: 캐시 키에 지연 파라미터 포함",
                  any(len(k) == 3 for k in _sc._SHORT_ASOF_CACHE))
            # 행이 모자라면 보정을 포기(결측보다 낫다)
            _sc._SHORT_ASOF_CACHE.clear()
            _small = _df.iloc[:2]

            class _KrxSmall:
                @staticmethod
                def get_shorting_balance_by_date(b, e, t):
                    return _small.copy()
            _sc._krx = _KrxSmall()
            _r = _sc.get_short_asof("005930", "20260721", pub_lag_rows=3)
            check("shortlag: 표본 부족 시 보정 포기(값 유지)",
                  _r.get("pre_short_balance_ratio") == 2.0, str(_r))
        finally:
            _sc._krx = _old_krx
            _sc._SHORT_ASOF_CACHE.clear()
            _sc._SHORT_ASOF_CACHE.update(_old_cache)
    except ImportError:
        print("[SKIP] shortlag: pandas 없음")

    # ── v10.1 야간선물(코스피200 선물) — 파싱 + 세션 판정 ──
    import night_futures_collect as nfc
    _pg = ('<span data-test="instrument-price-last">1,034.05</span>'
           '<span data-test="instrument-price-change">-29.45</span>'
           '<span data-test="instrument-price-change-percent">(-2.77%)</span>'
           '<div><span data-test="trading-state-label">닫음</span>·'
           '<time dateTime="2026-07-24T05:00:00.000Z" data-test="trading-time-label">24/07</time></div>')
    q = nfc.parse_quote(_pg)
    check("nightfut: 가격·등락 파싱", q["last"] == 1034.05 and q["change_pct"] == -2.77, str(q))
    check("nightfut: 세션 라벨(data-test 기반)", q["session_text"] == "닫음 · 24/07", str(q.get("session_text")))
    check("nightfut: ISO 최종체결 시각", q["last_trade_utc"] == "2026-07-24T05:00:00.000Z")
    check("nightfut: 빈 페이지 -> 전부 None", nfc.parse_quote("")["last"] is None)
    check("nightfut: 구조 변경 시 조용히 None", nfc.parse_quote("<html>바뀜</html>")["last"] is None)
    # 세션 판정: 야간 18:00~06:00 / 주간 09:00~15:45 / 그 사이는 off
    for _iso, _exp in (("2026-07-27T21:10:00.000Z", "night"),   # KST 06:10 — 야간 종료권
                       ("2026-07-27T10:00:00.000Z", "night"),   # KST 19:00 — 야간 개시
                       ("2026-07-24T05:00:00.000Z", "day"),     # KST 14:00 — 주간
                       ("2026-07-24T23:30:00.000Z", "off")):    # KST 08:30 — 비거래 구간
        _k, _s = nfc.kst_session_info(_iso)
        check(f"nightfut: 세션 판정 {_iso[11:16]}Z -> {_exp}", _s == _exp, f"{_k} {_s}")
    check("nightfut: 시각 없으면 unknown", nfc.kst_session_info(None)[1] == "unknown")
    check("nightfut: last 없으면 페이로드 생략", nfc.build_payload({"last": None}, 1000) == {})
    _pl = nfc.build_payload(q, 1055.58)
    check("nightfut: 지수 대비 괴리 계산", _pl["vs_index_pct"] == -2.04, str(_pl.get("vs_index_pct")))
    check("nightfut: 세션 판정이 페이로드에 포함", _pl["session_guess"] == "day")

    # ── v10.1 메일 수급표 단위(원→억) — 10^8 배 부풀림 회귀 방지 ──
    #   force_scores.detail 의 순매수는 '원' 단위다(force_analysis.score_supply 도크).
    #   억원으로 오인해 그대로 표기하면 '+3803330조' 같은 값이 메일로 나간다(2026-07-26 실측 사고).
    import tempfile as _tf
    _sd = _tf.mkdtemp(prefix="pv_")
    try:
        json.dump({"picks": [{"ticker": "068270", "name": "셀트리온"}], "shorts": []},
                  open(os.path.join(_sd, "predictions.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
        json.dump({"tickers": [{"ticker": "068270", "detail": {
            "price_change_pct": -1.82,
            "foreign_5d": 38033303000.0,     # 380.3억원
            "inst_5d": 32082121000.0,        # 320.8억원
            "indiv_5d": None}}]},
            open(os.path.join(_sd, "force_scores.json"), "w", encoding="utf-8"),
            ensure_ascii=False)
        import research_agent as _ra
        _md = _ra._prev_day_change_md(_sd)
        check("메일수급: 원→억 환산(380억)", "+380억" in _md, _md[-200:])
        # ★데이터 행만 검사한다(산문의 '구조'에도 '조'가 들어가 오탐이 난다 — 2026-07-26)
        _datarow = [l for l in _md.splitlines() if "셀트리온" in l]
        check("메일수급: 데이터 행에 조 단위 폭주 없음",
              _datarow and "조" not in _datarow[0], str(_datarow))
        check("메일수급: 기관도 억 단위(320억)", _datarow and "+321억" in _datarow[0], str(_datarow))
        check("메일수급: 결측은 대시", "—" in _md)
        check("메일수급: 전일 등락률 유지", "-1.82%" in _md)
    finally:
        shutil.rmtree(_sd, ignore_errors=True)

    import vkospi_collect as vc
    items = [{"idxNm": "코스피 200 변동성지수", "basDt": "20260717", "clpr": "27.5"},
             {"idxNm": "코스피 200 변동성지수", "basDt": "20260716", "clpr": "21.0"},
             {"idxNm": "다른지수", "basDt": "20260717", "clpr": "999"}]
    vp = vc.build_payload(items)
    check("vkospi: 변동성 지수만 채택 + 최신값", vp["latest"]["value"] == 27.5, str(vp.get("latest")))
    check("vkospi: 레벨 25+ -> 공포 라벨", "공포" in vp["level_label"], vp["level_label"])
    check("vkospi: 빈 입력 -> 빈 dict", vc.build_payload([]) == {})


# =====================================================================
# 8. email_charts — 이메일 안전 차트(순수함수) · 결측 시 생략 계약 (v10.0)
# =====================================================================
def test_email_charts():
    import email_charts as ec
    # 확률 막대: 정상 / 결측 / 합 0 / 음수
    h = ec.prob_bar(0.38, 0.18, 0.44, title="방향 확률")
    check("charts: prob 정상 렌더", "방향 확률" in h and "상승 38%" in h, h[:80])
    check("charts: prob 0~100 스케일도 동일 처리",
          "상승 38%" in ec.prob_bar(38, 18, 44))
    check("charts: prob 결측이면 생략", ec.prob_bar(None, 0.2, 0.3) == "")
    check("charts: prob 합 0 이면 생략", ec.prob_bar(0, 0, 0) == "")
    check("charts: prob 음수면 생략", ec.prob_bar(-0.1, 0.5, 0.6) == "")
    # 반올림 오차가 나도 합은 항상 100%
    h2 = ec.prob_bar(1 / 3, 1 / 3, 1 / 3)
    import re as _re
    # 바깥 table 의 width="100%" 는 빼고 '세그먼트 td' 의 폭만 합산
    pcts = [int(x) for x in _re.findall(r'<td width="(\d+)%" bgcolor=', h2)]
    check("charts: prob 세그먼트 폭 합계 100%",
          len(pcts) == 3 and sum(pcts) == 100, str(pcts))
    # 게이지: 정상 / 역전 / 범위 밖(클램프)
    check("charts: gauge 정상", "현재" in ec.range_gauge(6000, 6516, 6820))
    check("charts: gauge low>=high 생략", ec.range_gauge(7000, 6516, 6000) == "")
    check("charts: gauge 결측 생략", ec.range_gauge(6000, None, 6820) == "")
    g = ec.range_gauge(6000, 9999, 6820)      # 범위 위로 벗어남 → 클램프되어 렌더
    check("charts: gauge 범위밖 클램프 렌더", g != "" and 'width="97%"' in g)
    # 비교 막대
    b = ec.compare_bars([("셀트리온", 0.5), ("NAVER", 0.42)], title="확신도")
    check("charts: bar 라벨·값 노출", "셀트리온" in b and "0.5" in b)
    check("charts: bar 빈 입력 생략", ec.compare_bars([]) == "")
    check("charts: bar 숫자 아닌 값 무시", ec.compare_bars([("A", "없음")]) == "")
    many = ec.compare_bars([(f"종목{i}", i) for i in range(30)])
    # 행마다 바깥 <tr> 1개 + 막대용 중첩 <tr> 1개 = 2개씩. 12행 상한이면 최대 24.
    check("charts: bar 행수 상한 12행", many.count("<tr>") <= 24, str(many.count("<tr>")))
    # ★침묵 절단 금지: 잘렸으면 몇 개 생략됐는지 반드시 밝힌다
    check("charts: bar 절단 시 생략 개수 명시", "외 18개 생략" in many, many[-160:])
    check("charts: bar 상한 이내면 절단 문구 없음",
          "생략" not in ec.compare_bars([("A", 1), ("B", 2)]))
    # ★고정 축: 촘촘한 값(확신도 0.42~0.50)을 상대 스케일로 그리면 차이가 과장된다.
    #   axis_max 를 주면 '가능 범위 대비 위치'를 보여줘 오독을 막는다(2026-07-25 조사 반영).
    _rel = _re.findall(r'width="(\d+)%" bgcolor', ec.compare_bars([("A", .5), ("B", .42)]))
    _fix = _re.findall(r'width="(\d+)%" bgcolor',
                       ec.compare_bars([("A", .5), ("B", .42)], axis_max=0.8))
    check("charts: 상대 스케일은 최대값이 100%", _rel[:1] == ["100"], str(_rel))
    check("charts: 고정 축이면 상한 대비 비율(<100%)",
          _fix and int(_fix[0]) < 80 and int(_fix[0]) > 50, str(_fix))
    check("charts: 고정 축이면 축 범위를 캡션에 명시",
          "축 0~0.8" in ec.compare_bars([("A", .5)], axis_max=0.8))
    # RR: 정상 / 순서 뒤집힘(숏 등) 생략
    r = ec.rr_bar(100, 112, 95)
    check("charts: rr 정상 + RR 표기", "RR" in r and "진입" in r)
    check("charts: rr 순서 뒤집히면 생략", ec.rr_bar(100, 95, 112) == "")
    # 스파크라인
    s = ec.sparkbars([1, 2, 3, 2, 4])
    check("charts: spark 정상", "현재" in s)
    check("charts: spark 1개면 생략", ec.sparkbars([1]) == "")
    check("charts: spark 전부 같은 값도 렌더(0나눗셈 방어)", ec.sparkbars([5, 5, 5]) != "")
    # 펜스 파서
    f = "type: bar\ntitle: T\ndata: A=1, B=2"
    check("charts: fence bar", "T" in ec.render_chart_fence(f))
    check("charts: fence prob(한글 키)",
          "상승" in ec.render_chart_fence("type: prob\ndata: 상승=0.4, 횡보=0.2, 하락=0.4"))
    check("charts: fence 미지원 타입 생략", ec.render_chart_fence("type: pie\ndata: A=1") == "")
    check("charts: fence 빈 입력 생략", ec.render_chart_fence("") == "")
    check("charts: fence 깨진 입력 생략", ec.render_chart_fence("!!!@@@###") == "")
    # 이메일 안전성: 위험 태그가 절대 나오면 안 된다
    allhtml = h + b + r + s + g
    for bad in ("<script", "<svg", "<img", "javascript:", "data:image"):
        check(f"charts: 위험요소 없음({bad})", bad not in allhtml.lower())
    # XSS/깨짐 방지: 라벨 이스케이프
    esc = ec.compare_bars([("<b>x</b>", 1)])
    check("charts: 라벨 HTML 이스케이프", "&lt;b&gt;" in esc and "<b>" not in esc)

    # ── v10.7 근거 시각화 3종 — '근거를 글이 아니라 그림으로'(사용자 요청) ──
    # diverging_bars: 부호가 의미의 전부인 값(수급). compare_bars 와 달리 좌우로 갈라져야 한다.
    dv = ec.diverging_bars([("외국인", -500), ("기관", 120), ("개인", 380)],
                           title="투자자별", value_suffix="억")
    check("evviz: diverge 렌더", "외국인" in dv and "기관" in dv and "개인" in dv)
    check("evviz: diverge 부호 텍스트 병기(-500억/+380억)",
          "-500억" in dv and "+380억" in dv, dv[:0] or "부호 누락")
    check("evviz: diverge 음수=청 / 양수=적 (색으로 방향 구분)",
          ec.C_DOWN in dv and ec.C_UP in dv)
    # ★핵심: 같은 크기의 +/- 가 시각적으로 반대편에 있어야 한다(compare_bars 의 결함 교정 확인)
    _neg = ec.diverging_bars([("A", -100)])
    _pos = ec.diverging_bars([("A", 100)])
    check("evviz: 같은 크기 +/- 가 서로 다른 HTML(방향 구분됨)", _neg != _pos)
    check("evviz: compare_bars 는 +/- 를 같게 그린다(그래서 수급엔 부적합)",
          ec.compare_bars([("A", -100)]).count(ec.C_BAR) ==
          ec.compare_bars([("A", 100)]).count(ec.C_BAR))
    check("evviz: diverge 빈 입력이면 생략", ec.diverging_bars([]) == "")
    check("evviz: diverge 결측값 행 제외", ec.diverging_bars([("A", None), ("B", 5)]).count("<tr>") >= 1)

    # evidence_table: 축·방향·강도·출처 분해
    ev = ec.evidence_table([
        ("촉매", "호재", "강", "4공장 가동률 70% 돌파", "2Q 실적"),
        ("리스크", "주의", "중", "섹터 과열", "지수 관찰"),
    ], title="근거 분해")
    check("evviz: evidence 표 렌더", "촉매" in ev and "4공장" in ev and "2Q 실적" in ev)
    check("evviz: evidence 방향 배지 색(호재=적/주의=호박)", ec.C_UP in ev and "#b7770d" in ev)
    check("evviz: evidence 강도 표기", "호재 강" in ev or "강" in ev)
    check("evviz: evidence 4칸 미만 행은 무시",
          ec.evidence_table([("축", "호재")]) == "")
    check("evviz: evidence 빈 입력이면 생략", ec.evidence_table([]) == "")
    check("evviz: evidence 본문 이스케이프",
          "&lt;script&gt;" in ec.evidence_table([("a", "호재", "강", "<script>", "s")]))

    # path_timeline: 고점 예상일이 보유기간 어디쯤인지
    pt = ec.path_timeline("눌림후상승", 12, 20, title="예상 경로")
    check("evviz: path 타임라인 렌더", "고점 예상 T+12" in pt and "만기 T+20" in pt)
    check("evviz: path 경로유형 캡션", "눌림후상승" in pt)
    check("evviz: path 결측이면 생략", ec.path_timeline("즉시상승", None, 20) == "")
    check("evviz: path horizon 0 이면 생략", ec.path_timeline("즉시상승", 3, 0) == "")
    check("evviz: path 고점>만기 면 만기로 클램프(막대가 100% 넘지 않음)",
          ec.path_timeline("계단식", 50, 20) != "" and "T+20" in ec.path_timeline("계단식", 50, 20))

    # 펜스 파서: 여러 행 데이터('- ' 연속행) + 신규 타입 3종
    f_ev = ec.render_chart_fence(
        "type: evidence\ntitle: T\ndata:\n- 촉매 | 호재 | 강 | 내용A | 출처A\n"
        "- 수급 | 악재 | 중 | 내용B | 출처B\n")
    check("evviz: 펜스 evidence 다중행 파싱", "내용A" in f_ev and "내용B" in f_ev)
    f_dv = ec.render_chart_fence("type: diverge\ndata: 외국인=-500, 개인=+380\nsuffix: 억\n")
    check("evviz: 펜스 diverge", "외국인" in f_dv and "-500억" in f_dv)
    f_pt = ec.render_chart_fence("type: path\npath_view: 계단식\ndata: peak=5, horizon=20\n")
    check("evviz: 펜스 path", "T+5" in f_pt and "계단식" in f_pt)
    # 기존 펜스가 회귀하지 않았는가('- ' 연속행 지원 추가의 부작용 확인)
    f_prob = ec.render_chart_fence("type: prob\ndata: up=0.3, flat=0.2, down=0.5\n")
    check("evviz: 기존 prob 펜스 불변", f_prob != "" and "30%" in f_prob)
    # 신규 3종도 이메일 안전성 통과
    newhtml = dv + ev + pt + f_ev + f_dv + f_pt
    for bad in ("<script", "<svg", "<img", "javascript:", "data:image"):
        check(f"evviz: 위험요소 없음({bad})", bad not in newhtml.lower())


# =====================================================================
def main():
    print("=" * 60)
    print("stock_research 골든 테스트 (네트워크 0 · 라이브 파일 무수정)")
    print("=" * 60)
    for fn in (test_validate_predictions, test_norm_tag, test_compute_labels_golden,
               test_pre_entry_snapshot_first, test_retro_forward_helpers, test_snapshot_signals,
               test_resolve_session_multisession, test_new_collectors_pure,
               test_email_charts):
        try:
            fn()
        except Exception as e:
            _N[0] += 1
            _FAILS.append(fn.__name__)
            print(f"[FAIL] {fn.__name__} 예외: {type(e).__name__}: {e}")
    print("-" * 60)
    print(f"결과: {_N[0] - len(_FAILS)}/{_N[0]} 통과" + (f" — 실패 {_FAILS}" if _FAILS else " — 전부 통과"))
    return 1 if _FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
