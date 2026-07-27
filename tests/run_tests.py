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
