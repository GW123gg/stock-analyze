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
import json


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
def resolve_session(output_dir, fallback_age_h=6):
    """오늘 날짜 세션 중 최신 폴더 경로. 없으면 '최근 fallback_age_h 시간 내' 최신 세션(자정 경계 완화).

    자정 함정(문서화된 지뢰): collect 를 23:50 에 돌리고 신호를 00:10 에 돌리면 날짜가 어긋나
    '오늘 세션 없음'이 되던 문제 — 6시간 폴백 창이 자정을 건너는 연속 실행만 허용한다.
    창을 크게 잡지 않는 이유: 아침 06:30 에 어제 저녁(14h 전) 세션을 잡으면 어제 세션의 신호 파일을
    오늘 데이터로 '덮어써' 회고 스냅샷 무결성(룩어헤드)을 오염시킨다 — 6h 는 그 사고를 막는 상한이다.
    '_' 시작 폴더(_archive/_designtest)는 항상 제외. 없으면 None(수집기는 루트 폴백 또는 생략).
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
        if nm.startswith(today):
            cands.append((mt, p))
        elif now - mt <= fallback_age_h * 3600:
            recent.append((mt, p))
    pool = cands or recent
    if not pool:
        return None
    pool.sort(reverse=True)
    return pool[0][1]


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
PRED_REQUIRED_PICK = ("ticker", "timing", "conviction", "preprice", "entry_ref", "horizon_days")
PRED_REQUIRED_SHORT = ("ticker", "timing", "conviction", "entry_ref", "horizon_days")
PRED_TIMINGS = ("임박", "단기", "중기")
PRED_PREPRICES = ("강함", "부분", "미반영")


def validate_predictions(payload):
    """predictions.json(dict) → 계약 위반 메시지 리스트(빈 리스트=통과).

    검사(계약=[7.5] 스키마): picks/shorts 각 항목의 필수 필드 null/공백 금지 + 타입·범위.
      - timing: 임박/단기/중기 중 하나 (픽·숏 공통 필수 — 회고가 6회 요청한 축)
      - conviction: 숫자 0~1
      - entry_ref: 양수(예측 시점 가격 — 채점 기준)
      - horizon_days: 양의 정수
      - preprice: 픽만 필수(강함/부분/미반영)
    payload 가 dict 가 아니거나 picks/shorts 가 모두 비면 그 사실을 오류로 본다.
    """
    errs = []
    if not isinstance(payload, dict):
        return ["predictions.json 이 dict 가 아님(파싱 실패 또는 형식 오류)"]
    picks = payload.get("picks") or []
    shorts = payload.get("shorts") or []
    # 픽·숏 0건은 '오류가 아니다' — 국면 게이트([3-차익실현](5)·F1·F8)가 롱을 전면 보류시킨
    # 관망일에는 추천 없이 시장 방향(market_call)만 내는 게 정상이고, 그날도 메일은 나가야 한다.
    # (여기서 막으면 게이트를 잘 지킨 날일수록 메일이 안 나가는 역설이 생긴다.)
    if not picks and not shorts:
        return []
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
                errs.append(f"{kind}[{i}] {tag}: timing '{t}' 은 임박/단기/중기 중 하나여야 함")
            if kind == "pick":
                pp = it.get("preprice")
                if pp is not None and str(pp).strip() and str(pp).strip() not in PRED_PREPRICES:
                    errs.append(f"{kind}[{i}] {tag}: preprice '{pp}' 은 강함/부분/미반영 중 하나여야 함")
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
                    if int(h) <= 0:
                        errs.append(f"{kind}[{i}] {tag}: horizon_days {h} 이 양의 정수가 아님")
                except (TypeError, ValueError):
                    errs.append(f"{kind}[{i}] {tag}: horizon_days '{h}' 이 정수가 아님")
    return errs
