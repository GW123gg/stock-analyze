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
    ok_pick = {"ticker": "005930", "tag": "단기스윙", "timing": "단기", "conviction": 0.5,
               "preprice": "부분", "entry_ref": 70000, "horizon_days": 5,
               # v10.1 시간축 전망(픽 필수)
               "path_view": "눌림후상승", "expected_peak_days": 4}
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
