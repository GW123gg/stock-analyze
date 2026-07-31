# -*- coding: utf-8 -*-
"""
common.py — 여러 스크립트가 복붙하던 '원자적 저장' 원시함수를 한 곳으로 통합.

[원칙]
  - **부작용 없음**: import 해도 stdout.reconfigure·전역 초기화 등 아무 부작용이 없다(순수 함수만).
    (count_articles 류가 겪던 'import 시 stdout 오염'을 절대 만들지 않는다.)
  - **기존 동작 100% 보존**: 각 호출부의 기존 동작을 플래그(fsync/ensure_dir/ensure_ascii/indent)로
    그대로 재현한다. 기본값은 수집기 8종의 복붙 원본과 바이트 동일:
      makedirs(dirname or ".") → tmp(.tmp) 쓰기 → json.dump(ensure_ascii=False, indent=2) → os.replace.
  - 여기엔 '진짜로 동일한' 원시함수만 둔다. RSI/OBV·숫자변환·키로더처럼 스크립트별로 미묘히 다른
    헬퍼는 통합하지 않는다(값·부작용이 달라질 수 있음).
"""
import os
import sys
import io as _io
import json
import contextlib


@contextlib.contextmanager
def suppress_stdout():
    """with 블록 동안 stdout 을 버린다(정의만으로는 stdout 무접촉 — import 부작용 없음).

    용도: pykrx 포크가 import(KRX 로그인) 시점에 계정 ID를 stdout 으로 찍는다
    ('로그인 ID: ...'). 그 노출만 억제한다 — 네트워크 로그인 자체는 그대로 수행되어 동작 무변경.
    stderr 는 건드리지 않아 실제 오류 로그는 보존된다.
    """
    _saved = sys.stdout
    try:
        sys.stdout = _io.StringIO()
        yield
    finally:
        sys.stdout = _saved


def save_json_atomic(path, obj, *, ensure_ascii=False, indent=2, fsync=False, ensure_dir=True):
    """obj 를 path 에 **원자적으로** JSON 저장한다(.tmp 에 쓰고 os.replace).

    - ensure_dir=True(기본): 상위 폴더를 makedirs(exist_ok). 수집기 8종 원본과 동일.
    - fsync=True: 디스크 flush 후 rename(전원/강제종료 시 빈·잘린 파일 방지). retro_dataset 등 무결성 필요분.
    - ensure_ascii/indent: json.dump 인자. 기본은 원본과 동일(ensure_ascii=False, indent=2).
    부분 실패 시 .tmp 만 남고 원본 path 는 보존된다(원자성).
    """
    if ensure_dir:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=ensure_ascii, indent=indent)
        if fsync:
            f.flush()
            os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_text(path, text):
    """text 를 path 에 **원자적으로** 저장한다(.tmp → os.replace).

    상위 폴더는 만들지 않는다(기존 텍스트 저장 호출부가 이미 존재하는 폴더에만 썼으므로 동작 보존).
    필요하면 호출부에서 폴더를 먼저 만든다.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ─────────────────────────────────────────────────────────────────────
# 세션 해석 (H-3 — 12개 수집기의 _today_latest_session 복제 통합)
# ─────────────────────────────────────────────────────────────────────
def resolve_session(output_dir, fallback_age_h=6, prefer_sameday_earliest=False):
    """오늘 날짜 세션 중 최신 폴더 경로. 없으면 '최근 fallback_age_h 시간 내' 최신 세션(자정 경계 완화).

    자정 함정(문서화된 지뢰): collect 를 23:50 에 돌리고 신호를 00:10 에 돌리면 날짜가 어긋나
    '오늘 세션 없음'이 되던 문제 — 6시간 폴백 창이 자정을 건너는 연속 실행만 허용한다.
    창을 크게 잡지 않는 이유: 아침 06:30 에 어제 저녁(14h 전) 세션을 잡으면 어제 세션의 신호 파일을
    오늘 데이터로 '덮어써' 회고 스냅샷 무결성(룩어헤드)을 오염시킨다 — 6h 는 그 사고를 막는 상한이다.
    '_' 시작 폴더(_archive/_designtest)는 항상 제외. 없으면 None(수집기는 루트 폴백 또는 생략).

    ★분석 완료 세션 보호(2026-07-20): 03_final_report.md 가 이미 있는 세션은 후보에서 제외한다.
    신호 수집기는 이 함수로 '쓰기 대상'을 찾으므로, 분석이 끝난 세션에 재실행하면 그날 분석가가
    본 입력이 새 시각 데이터로 덮여 회고 스냅샷이 오염된다(실제 사고 2회: 07-18·07-20 —
    문서 경고로는 재발을 못 막아 코드로 차단). 완료 세션뿐이면 None → 루트 폴백(무해).
    """
    import time as _time
    from datetime import datetime as _dt
    if not os.path.isdir(output_dir):
        return None
    today = _dt.now().strftime("%Y-%m-%d")
    cands, recent = [], []
    now = _time.time()
    for nm in os.listdir(output_dir):
        if nm.startswith("_") or nm == "__pycache__":
            continue
        p = os.path.join(output_dir, nm)
        if not os.path.isdir(p):
            continue
        try:
            mt = os.path.getmtime(p)
        except Exception:
            continue
        if os.path.isfile(os.path.join(p, "03_final_report.md")):
            continue      # 분석 완료 세션 — 쓰기 대상 아님(스냅샷 오염 방지, 위 도크 참조)
        if nm.startswith(today):
            cands.append((mt, p))
        elif now - mt <= fallback_age_h * 3600:
            recent.append((mt, p))
    if cands:
        # ★같은 날 세션이 2개 이상일 때(데몬 morning + 온디맨드 collect 동시 실행 등):
        #   prefer_sameday_earliest 이면 '가장 먼저 생성된' 세션(=분석·predictions 가 붙는 채택 세션)을
        #   고른다. 폴더명이 %Y-%m-%d_%H%M%S 라 이름 오름차순 = 생성순(디렉터리 mtime 은 이후 쓰기로
        #   뒤집혀 신뢰 불가). 이 옵션 없이는 mtime 최신=늦게 생긴 orphan 세션을 골라 snapshot 이
        #   엉뚱한 세션에 동결돼 회고 국면입력(F1/F8)이 공백이 된다(2026-07-21·22 실사고).
        if prefer_sameday_earliest and len(cands) > 1:
            return min(cands, key=lambda t: os.path.basename(t[1]))[1]
        cands.sort(reverse=True)
        return cands[0][1]
    if not recent:
        return None
    recent.sort(reverse=True)          # 자정 폴백은 '최신' 유지(23:50→00:10 연속 실행 완화)
    return recent[0][1]


# ─────────────────────────────────────────────────────────────────────
# predictions.json 계약 검증 (순수함수 — 부작용·IO 없음)
# ─────────────────────────────────────────────────────────────────────
# 왜: timing·conviction·preprice 필수는 지금까지 '지시문'에만 있어 강제력이 없었고,
#     회고가 6회 연속(07-01~07-12) "timing 공백으로 '단기 vs 임박' 검증 영구 불가"를 요청했다.
#     발송 전에 코드로 막아야 회고 데이터셋 오염이 근본 차단된다(cmd_mail 이 호출).
# 필수 필드는 kind 별로 다르다 — cowork_instructions [7.5] 스키마 원문 기준:
#   "모든 픽에 timing·conviction·preprice·horizon_days·entry_ref 를 반드시" / "숏도 timing·conviction 필수".
#   preprice(강함|부분|미반영)는 '선반영' 개념이라 픽 전용이고 숏 스키마엔 아예 없다.
#   (실측: 2026-07-11·07-16 세션의 숏은 preprice 가 없는 게 계약상 정상 — 여기서 요구하면 정상 발송을 오차단한다.)
# ★v10.1 시간축 전망(사용자 요청 2026-07-26): "얼마 뒤에 오를까 / 단기 조정이 있을까 / 1~2달 내에는".
#   회고는 이미 경로를 '측정'하고 있었다(days_to_peak·peak_gain_pct·max_drawdown_pct)는데 예측 쪽
#   대응 필드가 0개였다 — 즉 언제 오를지는 한 번도 예측·채점된 적이 없다. 그 공백을 메운다.
#   path_view·expected_peak_days 를 픽 필수로 둔 이유: timing 이 8회차 동안 '선택'이라 방치됐다가
#   게이트로 강제한 뒤에야 채워진 전례(A4)가 있다.
PRED_PATH_VIEWS = ("즉시상승", "눌림후상승", "계단식", "횡보후상승", "이벤트대기")
# ★v11.3 진입 시점([6.5++]): 리포트는 장 시작 전에 나가는데 추천 당일 갭상승·상한가로 뛰면
#   독자는 못 산다. 회고 실측 '픽의 절반이 D+1~2 즉시고점(적중 19%)'이 그 문제였다.
PRED_ENTRY_WINDOWS = ("당일시가", "당일눌림", "당일종가", "익일이후")
PRED_REQUIRED_PICK = ("ticker", "tag", "timing", "conviction", "preprice", "entry_ref",
                      "horizon_days", "path_view", "expected_peak_days", "entry_window")
PRED_REQUIRED_SHORT = ("ticker", "timing", "conviction", "entry_ref", "horizon_days")
PRED_TIMINGS = ("임박", "단기", "중기", "장기")     # v10.1: 장기(2개월·T+40) 추가
PRED_PREPRICES = ("강함", "부분", "미반영")
PRED_HORIZONS = (1, 5, 20, 40)                      # v10.1: 40거래일(약 2개월) 추가


def validate_predictions(payload):
    """predictions.json(dict) → 계약 위반 메시지 리스트(빈 리스트=통과).

    검사(계약=[7.5] 스키마): picks/shorts 각 항목의 필수 필드 null/공백 금지 + 타입·범위.
      - timing: 임박/단기/중기 중 하나 (픽·숏 공통 필수 — 회고가 6회 요청한 축)
      - conviction: 숫자 0~1
      - entry_ref: 양수(예측 시점 가격 — 채점 기준)
      - horizon_days: 1|5|20 ([6.5] 타이밍 enum — v9.7 강화)
      - preprice: 픽만 필수(강함/부분/미반영)
      - market_call: 콜이 있으면 prob 3종 필수 + dir enum + conviction=max(prob)(±0.05) — v9.7~9.8
    payload 가 dict 가 아니면 오류. 픽·숏 0건(관망일) 자체는 오류가 아니되, market_call 위반은
    그 경우에도 보존해 반환한다(v9.8 — 관망일에 콜만 내는 날의 게이트 구멍 차단).
    """
    errs = []
    if not isinstance(payload, dict):
        return ["predictions.json 이 dict 가 아님(파싱 실패 또는 형식 오류)"]
    picks = payload.get("picks") or []
    shorts = payload.get("shorts") or []
    # ★v9.6 확률 예보 검사(market_call). v9.7: 콜(dict)이 있으면 prob 3종은 '필수'다 —
    #   [7.5]가 필수라 말하면서 게이트가 안 보면 도피성 콜 방지라는 취지에 구멍이 난다(리뷰 [4]).
    #   각 0~1 + 합=1.00(±0.03) + 상한 0.75 + dir=argmax(prob) + conviction=max(prob)(±0.05).
    mc = payload.get("market_call")
    if isinstance(mc, dict):
        for mkt in ("kospi", "kosdaq"):
            c = mc.get(mkt)
            if not isinstance(c, dict):
                continue
            probs = [c.get("prob_up"), c.get("prob_flat"), c.get("prob_down")]
            if all(p is None for p in probs):
                errs.append(f"market_call.{mkt}: prob_up/flat/down 누락 — v9.7 필수([7.5] 확률 예보)")
                continue
            try:
                pu, pf, pd = (float(probs[0]), float(probs[1]), float(probs[2]))
            except (TypeError, ValueError):
                errs.append(f"market_call.{mkt}: prob_up/flat/down 3종 모두 숫자로 채워야 함")
                continue
            if not all(0.0 <= x <= 1.0 for x in (pu, pf, pd)):
                errs.append(f"market_call.{mkt}: prob 값이 0~1 범위 밖")
            if abs((pu + pf + pd) - 1.0) > 0.03:
                errs.append(f"market_call.{mkt}: prob 합 {pu + pf + pd:.2f} != 1.00(±0.03)")
            if max(pu, pf, pd) > 0.75 + 1e-9:
                errs.append(f"market_call.{mkt}: 확률 상한 0.75 초과(겸손 규칙 — [4.7] 규칙 3)")
            _amax = {"up": pu, "flat": pf, "down": pd}
            _dir = str(c.get("dir") or "").strip().lower()
            _map = {"up": "up", "neutral": "flat", "down": "down"}
            # dir 자체의 존재·enum 검사(v9.8): 오타·누락 dir 은 argmax 검사를 조용히 건너뛰고
            #   통과한 뒤 accuracy_tracker.grade_market_call 에서 무음 채점 스킵된다(영구 미채점).
            if _dir not in _map:
                errs.append(f"market_call.{mkt}: dir '{_dir or '누락'}' 은 up/down/neutral 중 하나여야 함([7.5])")
            elif _amax[_map[_dir]] < max(pu, pf, pd) - 1e-9:
                errs.append(f"market_call.{mkt}: dir '{_dir}' 이 argmax(prob)와 불일치")
            cv = c.get("conviction")
            if cv is not None:
                try:
                    if abs(float(cv) - max(pu, pf, pd)) > 0.05 + 1e-9:
                        errs.append(
                            f"market_call.{mkt}: conviction {cv} 이 max(prob)={max(pu, pf, pd):.2f} 와"
                            f" 불일치([4.7] 규칙 3 — 허용오차 0.05)")
                except (TypeError, ValueError):
                    errs.append(f"market_call.{mkt}: conviction '{cv}' 이 숫자가 아님")

            # ★v11.1 익일(T+1) 예측 검증 — 있으면 본 콜과 같은 규율을 적용한다.
            #   [왜] 기존엔 확률 1세트를 T+1·T+5 양쪽에 채점해 둘 다 놓쳤다(실측 25%/27%).
            #   next_day 는 **선택**이지만(도입 전 세션 호환), 넣었으면 형식은 엄격히 본다 —
            #   반쯤 채운 예측이 조용히 통과해 영구 미채점되는 것을 막는다(dir enum 전례).
            nd = c.get("next_day")
            if isinstance(nd, dict):
                nps = [nd.get("prob_up"), nd.get("prob_flat"), nd.get("prob_down")]
                if any(x is None for x in nps):
                    errs.append(f"market_call.{mkt}.next_day: prob_up/flat/down 누락"
                                f" — 익일 예측을 넣었으면 3종 다 채워라([7.5])")
                else:
                    try:
                        npu, npf, npd = (float(x) for x in nps)
                    except (TypeError, ValueError):
                        errs.append(f"market_call.{mkt}.next_day: prob 3종이 숫자가 아님")
                    else:
                        if not all(0.0 <= x <= 1.0 for x in (npu, npf, npd)):
                            errs.append(f"market_call.{mkt}.next_day: prob 값이 0~1 범위 밖")
                        elif abs((npu + npf + npd) - 1.0) > 0.03:
                            errs.append(f"market_call.{mkt}.next_day: prob 합"
                                        f" {npu + npf + npd:.2f} != 1.00(±0.03)")
                        if max(npu, npf, npd) > 0.75 + 1e-9:
                            errs.append(f"market_call.{mkt}.next_day: 확률 상한 0.75 초과"
                                        f"(겸손 규칙 — [4.7] 규칙 3)")
                        _nd_dir = str(nd.get("dir") or "").strip().lower()
                        if _nd_dir not in _map:
                            errs.append(f"market_call.{mkt}.next_day: dir '{_nd_dir or '누락'}' 은"
                                        f" up/down/neutral 중 하나여야 함")
                        else:
                            _namax = {"up": npu, "flat": npf, "down": npd}
                            if _namax[_map[_nd_dir]] < max(npu, npf, npd) - 1e-9:
                                errs.append(f"market_call.{mkt}.next_day: dir '{_nd_dir}' 이"
                                            f" argmax(prob)와 불일치")
                _ep = nd.get("expected_pct")
                if _ep is not None:
                    try:
                        _epf = float(_ep)
                        # 익일 등락률은 대개 ±0.5~2%. ±15% 밖이면 단위 착오(비율 vs 배수) 의심
                        if abs(_epf) > 15.0:
                            errs.append(f"market_call.{mkt}.next_day: expected_pct {_ep}"
                                        f" 가 비현실적(±15% 초과) — 단위 확인")
                    except (TypeError, ValueError):
                        errs.append(f"market_call.{mkt}.next_day: expected_pct '{_ep}' 이 숫자가 아님")
    PRED_RATINGS = ("강력매수", "매수", "중립", "비중축소")
    PRED_ACTIONS = ("신규커버", "재확인", "유지", "상향", "하향", "커버종료")
    # 픽·숏 0건은 '오류가 아니다' — 국면 게이트([3-차익실현](5)·F1·F8)가 롱을 전면 보류시킨
    # 관망일에는 추천 없이 시장 방향(market_call)만 내는 게 정상이고, 그날도 메일은 나가야 한다.
    # (여기서 막으면 게이트를 잘 지킨 날일수록 메일이 안 나가는 역설이 생긴다.)
    # ★단 errs 를 버리지 마라(v9.8 수정): market_call 이 유일한 예측인 관망일에 위 prob/dir
    #   위반이 잡혔으면 그날이야말로 게이트가 작동해야 하는 날이다(예전엔 return [] 로 폐기됐다).
    if not picks and not shorts:
        return errs
    for kind, items in (("pick", picks), ("short", shorts)):
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                errs.append(f"{kind}[{i}]: 항목이 dict 가 아님")
                continue
            tag = str(it.get("ticker") or "?")
            required = PRED_REQUIRED_PICK if kind == "pick" else PRED_REQUIRED_SHORT
            for k in required:
                v = it.get(k)
                if v is None or (isinstance(v, str) and not v.strip()):
                    errs.append(f"{kind}[{i}] {tag}: '{k}' 누락/null")
            t = it.get("timing")
            if t is not None and str(t).strip() and str(t).strip() not in PRED_TIMINGS:
                errs.append(f"{kind}[{i}] {tag}: timing '{t}' 은 "
                            f"{'/'.join(PRED_TIMINGS)} 중 하나여야 함")
            # ★v10.1 시간축 전망(픽 필수·숏 선택) — 회고의 경로 실측 라벨과 1:1 대응해 채점된다.
            pv = it.get("path_view")
            if pv is not None and str(pv).strip() and str(pv).strip() not in PRED_PATH_VIEWS:
                errs.append(f"{kind}[{i}] {tag}: path_view '{pv}' 은 "
                            f"{'/'.join(PRED_PATH_VIEWS)} 중 하나여야 함")
            epd = it.get("expected_peak_days")     # 채점 대상: 실측 days_to_peak
            if epd is not None:
                try:
                    _e = int(epd)
                    _hz = int(it.get("horizon_days") or 0)
                    if _e < 1:
                        errs.append(f"{kind}[{i}] {tag}: expected_peak_days {epd} 는 1 이상이어야 함")
                    elif _hz and _e > _hz:
                        errs.append(f"{kind}[{i}] {tag}: expected_peak_days {epd} 가 "
                                    f"horizon_days {_hz} 를 초과(채점 창 밖 — 예측이 검증 불가)")
                except (TypeError, ValueError):
                    errs.append(f"{kind}[{i}] {tag}: expected_peak_days '{epd}' 이 정수가 아님")
            # ★v11.3 진입 시점 + 익일 전망([6.5++]) — '못 사는 추천'을 줄이기 위한 필드
            ew = it.get("entry_window")
            if ew is not None and str(ew).strip() and str(ew).strip() not in PRED_ENTRY_WINDOWS:
                errs.append(f"{kind}[{i}] {tag}: entry_window '{ew}' 은 "
                            f"{'/'.join(PRED_ENTRY_WINDOWS)} 중 하나여야 함")
            pnd = it.get("next_day")
            if isinstance(pnd, dict):
                _ps = [pnd.get("prob_up"), pnd.get("prob_flat"), pnd.get("prob_down")]
                if any(x is None for x in _ps):
                    errs.append(f"{kind}[{i}] {tag}: next_day 를 넣었으면 prob 3종을 다 채워라")
                else:
                    try:
                        _pu, _pf, _pd2 = (float(x) for x in _ps)
                    except (TypeError, ValueError):
                        errs.append(f"{kind}[{i}] {tag}: next_day prob 3종이 숫자가 아님")
                    else:
                        if not all(0.0 <= x <= 1.0 for x in (_pu, _pf, _pd2)):
                            errs.append(f"{kind}[{i}] {tag}: next_day prob 값이 0~1 범위 밖")
                        elif abs((_pu + _pf + _pd2) - 1.0) > 0.03:
                            errs.append(f"{kind}[{i}] {tag}: next_day prob 합 "
                                        f"{_pu + _pf + _pd2:.2f} != 1.00(±0.03)")
                        if max(_pu, _pf, _pd2) > 0.75 + 1e-9:
                            errs.append(f"{kind}[{i}] {tag}: next_day 확률 상한 0.75 초과")
                        _nd2 = str(pnd.get("dir") or "").strip().lower()
                        _m2 = {"up": _pu, "neutral": _pf, "down": _pd2}
                        if _nd2 not in _m2:
                            errs.append(f"{kind}[{i}] {tag}: next_day dir "
                                        f"'{_nd2 or '누락'}' 은 up/down/neutral 중 하나여야 함")
                        elif _m2[_nd2] < max(_pu, _pf, _pd2) - 1e-9:
                            errs.append(f"{kind}[{i}] {tag}: next_day dir '{_nd2}' 이 argmax(prob)와 불일치")
                _pep = pnd.get("expected_pct")
                if _pep is not None:
                    try:
                        if abs(float(_pep)) > 30.0:      # 상하한가 ±30% 밖은 물리적으로 불가
                            errs.append(f"{kind}[{i}] {tag}: next_day expected_pct {_pep} 가 "
                                        f"가격제한폭(±30%) 밖 — 단위 확인")
                    except (TypeError, ValueError):
                        errs.append(f"{kind}[{i}] {tag}: next_day expected_pct '{_pep}' 이 숫자가 아님")
            # ★정합성: 즉시상승인데 '익일이후' 진입은 모순(이미 올랐는데 내일 사라?)
            if (str(it.get("path_view") or "").strip() == "즉시상승"
                    and str(ew or "").strip() == "익일이후"):
                errs.append(f"{kind}[{i}] {tag}: path_view=즉시상승 과 entry_window=익일이후 는 "
                            f"모순([6.5++]) — 둘 중 하나를 고쳐라")
            eg = it.get("expected_gain_pct")       # 채점 대상: 실측 peak_gain_pct
            if eg is not None:
                try:
                    if float(eg) <= 0:
                        errs.append(f"{kind}[{i}] {tag}: expected_gain_pct {eg} 는 양수여야 함"
                                    f"(숏도 '유리한 방향 폭'을 양수로 적는다)")
                except (TypeError, ValueError):
                    errs.append(f"{kind}[{i}] {tag}: expected_gain_pct '{eg}' 이 숫자가 아님")
            ep = it.get("expected_pullback_pct")   # 채점 대상: 실측 max_drawdown_pct
            if ep is not None:
                try:
                    if float(ep) > 0:
                        errs.append(f"{kind}[{i}] {tag}: expected_pullback_pct {ep} 는 "
                                    f"0 이하(되돌림 폭은 음수)여야 함")
                except (TypeError, ValueError):
                    errs.append(f"{kind}[{i}] {tag}: expected_pullback_pct '{ep}' 이 숫자가 아님")
            if kind == "pick":
                # v9.7(회고 07-16, 무태그 7회 관찰): tag 는 필수 + enum — 무태그 행은 태그별
                # 회고 분석에서 영구 제외되므로 발송 전에 막는다(숏은 tag 없음 — 픽 전용).
                tg = it.get("tag")
                if tg is not None and str(tg).strip() and str(tg).strip() not in ("단기스윙", "장투가능", "장전선취매"):
                    errs.append(f"{kind}[{i}] {tag}: tag '{tg}' 은 단기스윙/장투가능/장전선취매 중 하나여야 함")
                pp = it.get("preprice")
                if pp is not None and str(pp).strip() and str(pp).strip() not in PRED_PREPRICES:
                    errs.append(f"{kind}[{i}] {tag}: preprice '{pp}' 은 강함/부분/미반영 중 하나여야 함")
                # v9.6 커버리지 필드(있을 때만 enum 검사 — 하위호환)
                rt = it.get("rating")
                if rt is not None and str(rt).strip() and str(rt).strip() not in PRED_RATINGS:
                    errs.append(f"{kind}[{i}] {tag}: rating '{rt}' 은 {'/'.join(PRED_RATINGS)} 중 하나여야 함")
                ra = it.get("rating_action")
                if ra is not None and str(ra).strip() and str(ra).strip() not in PRED_ACTIONS:
                    errs.append(f"{kind}[{i}] {tag}: rating_action '{ra}' 은 {'/'.join(PRED_ACTIONS)} 중 하나여야 함")
            c = it.get("conviction")
            if c is not None:
                try:
                    cf = float(c)
                    if not (0.0 <= cf <= 1.0):
                        errs.append(f"{kind}[{i}] {tag}: conviction {c} 이 0~1 범위 밖")
                except (TypeError, ValueError):
                    errs.append(f"{kind}[{i}] {tag}: conviction '{c}' 이 숫자가 아님")
            e = it.get("entry_ref")
            if e is not None:
                try:
                    if float(e) <= 0:
                        errs.append(f"{kind}[{i}] {tag}: entry_ref {e} 이 양수가 아님")
                except (TypeError, ValueError):
                    errs.append(f"{kind}[{i}] {tag}: entry_ref '{e}' 이 숫자가 아님")
            h = it.get("horizon_days")
            if h is not None:
                try:
                    if int(h) not in PRED_HORIZONS:
                        errs.append(f"{kind}[{i}] {tag}: horizon_days {h} 은 "
                                    f"{'|'.join(str(x) for x in PRED_HORIZONS)} 중 하나여야 함([6.5] 타이밍)")
                except (TypeError, ValueError):
                    errs.append(f"{kind}[{i}] {tag}: horizon_days '{h}' 이 정수가 아님")
    return errs
