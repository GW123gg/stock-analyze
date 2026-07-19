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
from datetime import date, timedelta

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
    ok_pick = {"ticker": "005930", "timing": "단기", "conviction": 0.5,
               "preprice": "부분", "entry_ref": 70000, "horizon_days": 5}
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
    check("validate: 비dict 차단", v("깨짐") != [])
    # ── v9.6 확률 예보(월가식 개편) — 있을 때만 검사(하위호환) ──
    base_mc = {"picks": [], "shorts": [ok_short]}
    good_mc = dict(base_mc, market_call={"kospi": {"dir": "down", "conviction": 0.6,
                   "prob_up": 0.2, "prob_flat": 0.2, "prob_down": 0.6}})
    check("v9.6: 정상 확률(합1·argmax=dir) 통과", v(good_mc) == [], str(v(good_mc)))
    check("v9.6: prob 없는 구식 market_call 통과(하위호환)",
          v(dict(base_mc, market_call={"kospi": {"dir": "up", "conviction": 0.5}})) == [])
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
        # KOSPI: 진입일 100 -> T+5 102 (지수 +2%) -> alpha = -5 - 2 = -7
        rl._KS11_SERIES = [(d, c) for d, c in zip(days, [100.0, 101.0, 100.5, 99.0, 101.5, 102.0])]

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

        # 익절 먼저 닿는 경로: D+1 +13% -> tp12 룰이면 D+1 실제 종가 +13 반환
        closes2 = [100.0, 113.0, 108.0, 105.0, 104.0, 103.0]
        _FscStub.get_ohlcv_series = staticmethod(
            lambda t, b: list(zip(days, closes2, vols)))
        lab2 = rl.compute_labels("TEST", base, 100.0, 5)
        check("labels: 익절 반사실 = 실제 종가 +13.0", lab2["ret_if_stop8_tp12_pct"] == 13.0,
              str(lab2["ret_if_stop8_tp12_pct"]))
        check("labels: 손절 미터치 시 반사실 = 만기수익", lab2["ret_if_stop8_pct"] == lab2["ret_h_pct"])
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
def main():
    print("=" * 60)
    print("stock_research 골든 테스트 (네트워크 0 · 라이브 파일 무수정)")
    print("=" * 60)
    for fn in (test_validate_predictions, test_norm_tag, test_compute_labels_golden,
               test_pre_entry_snapshot_first, test_retro_forward_helpers, test_snapshot_signals):
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
