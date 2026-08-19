#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
accuracy_tracker.py — 과거 예측 사후 채점 & 정확도 점수표 생성 (독립 호스트 스크립트)

[목적]
  분석가(Cowork)가 매일 세션 폴더에 저장한 predictions.json 을 모아, 만기가 도달한
  예측만 골라 실제 주가 결과로 "사후 채점"한다. 그 결과를 누적(accuracy_log.json)하고
  사람·Cowork 가 읽을 점수표(scorecard.md)를 만든다.
  다음 분석에서 Cowork 가 scorecard.md 를 읽어 자기보정(과신 구간 축소·약세콜 신중 등)
  하게 하는 것이 이 시스템의 정확도 향상 엔진이다.

  research_agent.py / supervisor.py / force_analysis.py 등 기존 코드는 일절 수정하지 않고
  결과 파일(predictions.json)만 읽는다.

[입력]  predictions.json  (output/*/  및  output/_archive/*/  를 모두 스캔)
  스키마(요약):
    {"date","session","data_basis_date","regime",
     "market_call":{"kospi":{"dir","conviction","target_pct","invalidation"},"kosdaq":{...}},
     "picks":[{"ticker","name","tag","entry_ref","horizon_days","conviction","thesis","preprice"}],
     "shorts":[{"ticker","name","entry_ref","horizon_days","conviction","thesis"}],
     "key_risks":[...]}

[채점 로직 요약]
  - 만기 도달분만 채점: 예측일(거래일)로부터 horizon_days 거래일이 지난 픽/숏.
    market_call 은 T+1·T+5 거래일 경과분.
  - 픽 수익률 = (T+h 종가 - entry_ref) / entry_ref
    적중 = (픽이고 수익률>0 — 태그 무관, 전 픽=상승 기대) 또는 (숏이고 수익률<0)
  - alpha = 픽수익률 - 같은 구간 코스피(KS11) 수익률
  - market_call: 같은 구간 지수 등락 부호와 dir 일치 여부(neutral 은 |등락|<0.5% 면 적중)
  - calibration: conviction 구간별 실제 적중률
  - 멱등: 이미 채점·종가 확정된 건은 재계산하지 않는다(룩어헤드 금지).

[출력]
  accuracy_log.json (BASE_DIR) — (예측일, ticker, horizon) 단위 채점 결과 누적
  scorecard.md      (BASE_DIR) — 최근 ~20 예측일 요약 + 맨 위 한국어 권고 2~4줄

[제약]
  - 콘솔 print 이모지 금지(cp949 크래시) — ASCII 태그 [acc]. 파일은 UTF-8.
  - FDR 실패·상폐·데이터 없음 등 모두 try/except, 해당 건만 skip. 항상 exit 0.
  - predictions.json 이 하나도 없으면 안내용 빈 scorecard.md 생성 후 정상 종료.
  - 거래일 계산은 FDR 가 돌려주는 실제 거래일 인덱스를 사용(주말·휴장 자동 처리).

[사용법]
  python accuracy_tracker.py
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta

# ── 경로 상수 ───────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
ARCHIVE_DIR = os.path.join(OUTPUT_DIR, "_archive")
LOG_PATH = os.path.join(BASE_DIR, "accuracy_log.json")
SCORECARD_PATH = os.path.join(BASE_DIR, "scorecard.md")

KOSPI_SYMBOL = "KS11"
KOSDAQ_SYMBOL = "KQ11"

# ★v11.23 해외 픽 알파용 벤치마크(common 단일 출처 — 지시서·수집기와 같은 표를 쓴다)
from common import COUNTRY_BENCH   # noqa: E402  (순수 모듈 — import 부작용 없음)

# market_call neutral 판정 임계 — v9.6: 지평별 스케일(변동성 ~sqrt(t) 근사).
#   구 밴드(전 지평 0.5%)는 T+5 에서 기저율이 0%에 수렴해 neutral 을 '이길 수 없는 콜'로 만들었다
#   (23일 실측: neutral T+5 적중 0/15). 이미 채점된 과거 엔트리는 멱등이라 재채점되지 않는다 —
#   scorecard 각주에 밴드 변경일을 명시해 회차 간 비교 시 참고.
NEUTRAL_BAND_BY_H = {1: 0.5, 5: 1.2}
NEUTRAL_BAND_PCT = 0.5   # 폴백(미정의 지평)

# scorecard 요약 대상 최근 예측일 수
RECENT_DAYS = 20

# calibration 구간 (하한 미포함, 상한 포함). 단 첫 구간은 0 포함.
CALIB_BUCKETS = [
    ("[0.00-0.50]", 0.0, 0.50),
    ("(0.50-0.65]", 0.50, 0.65),
    ("(0.65-0.80]", 0.65, 0.80),
    ("(0.80-1.00]", 0.80, 1.00),
]

# (★v11.6 죽은 상수 POSITIVE_TAGS 삭제 — 정의만 있고 사용처 0. 실제 채점은 태그 무관
#  전 픽=상승 기대(grade_pick)라, '태그 기반 방향판정이 있다'는 착시가 방향중립 태그 신설 시
#  무음 오채점을 부를 수 있었다. 방향중립 태그를 만들면 grade_pick 의 hit 분기를 함께 고쳐라.)

# Windows 콘솔 UTF-8 (이모지는 쓰지 않지만 한글 안전 출력)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ── 선택적 의존성 ───────────────────────────────────────────────
try:
    import FinanceDataReader as fdr
    FDR_AVAILABLE = True
except Exception:
    fdr = None
    FDR_AVAILABLE = False

try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except Exception:
    pd = None
    PANDAS_AVAILABLE = False


# ── 로깅 ────────────────────────────────────────────────────────
def _setup_logger() -> logging.Logger:
    lg = logging.getLogger("accuracy_tracker")
    if lg.handlers:
        return lg
    lg.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%H:%M:%S"))
    lg.addHandler(sh)
    return lg


log = _setup_logger()


# =====================================================================
# 1. predictions.json 스캔 (중복 날짜는 최신 1개)
# =====================================================================
def _iter_prediction_files():
    """output/*/predictions.json + output/_archive/*/predictions.json 경로 yield."""
    for base in (OUTPUT_DIR, ARCHIVE_DIR):
        if not os.path.isdir(base):
            continue
        try:
            names = os.listdir(base)
        except Exception as e:
            log.warning(f"[acc] 폴더 목록 실패 ({base}): {type(e).__name__}: {e}")
            continue
        for name in names:
            if name.startswith("_") or name == "__pycache__":
                continue
            sess = os.path.join(base, name)
            if not os.path.isdir(sess):
                continue
            p = os.path.join(sess, "predictions.json")
            if os.path.isfile(p):
                yield p


def _was_sent(session_dir):
    """세션이 실제 메일 발송됐다는 증거 — sent_index.json 등재 또는 MAIL_DONE.flag (★v11.7 A38)."""
    name = os.path.basename(session_dir)
    try:
        si_path = os.path.join(BASE_DIR, "sent_index.json")
        if os.path.isfile(si_path):
            with open(si_path, encoding="utf-8", errors="replace") as f:
                if name in (json.load(f) or {}):
                    return True
    except Exception:
        pass
    return os.path.isfile(os.path.join(session_dir, "MAIL_DONE.flag"))


def load_predictions():
    """
    모든 predictions.json 로드. (date, session) 기준 중복 제거 — 같은 날짜는
    파일 mtime 이 가장 최신인 1개만 채택.
    ★v11.7(A38): 단 **실발송 증거가 있는 구세션**의 픽/숏 중 최신본에 없는
    (kind,ticker,horizon) 항목은 병합한다. 실측 2026-07-06: 11:27 발송(픽6·숏3) 뒤
    16:14 재실행 발송(픽8·숏2)이 최신본이 되며 11:27 에만 있던 픽3·숏3이 6회차 동안
    무채점이었다 — 발송된 예측은 전부 채점 대상이다(같은 항목의 수정 재발송은 최신본 우선).
    Returns: list[dict] (predictions, 각 dict 에 '_src' 경로 주입), 날짜 오름차순.
    """
    by_date = {}    # date -> (mtime, pred_dict)
    losers = {}     # date -> [(mtime, pred_dict), ...] (같은 날짜의 비최신본)
    for path in _iter_prediction_files():
        try:
            mtime = os.path.getmtime(path)
            with open(path, encoding="utf-8", errors="replace") as f:
                data = json.load(f)
        except Exception as e:
            log.warning(f"[acc] predictions 로드 실패 ({path}): {type(e).__name__}: {e}")
            continue
        if not isinstance(data, dict):
            continue
        date = str(data.get("date") or "").strip()
        if not date:
            # 날짜 없으면 폴더명 앞 10자(YYYY-MM-DD)로 보정 시도
            folder = os.path.basename(os.path.dirname(path))
            date = folder[:10]
            if not date:
                continue
            data["date"] = date
        data["_src"] = path
        prev = by_date.get(date)
        if prev is None or mtime > prev[0]:
            if prev is not None:
                losers.setdefault(date, []).append(prev)
            by_date[date] = (mtime, data)
        else:
            losers.setdefault(date, []).append((mtime, data))
    # ★v11.7(A38): 실발송 구세션의 고유 항목 병합
    for date, lst in losers.items():
        winner = by_date.get(date)
        if winner is None:
            continue
        wdata = winner[1]
        have = set()
        for kind, key in (("pick", "picks"), ("short", "shorts")):
            for it in (wdata.get(key) or []):
                if isinstance(it, dict):
                    have.add((kind, str(it.get("ticker") or ""), str(it.get("horizon_days") or "")))
        for _mt, ldata in sorted(lst, key=lambda x: -x[0]):   # 최신 구본부터
            sess_dir = os.path.dirname(str(ldata.get("_src") or ""))
            if not sess_dir or not _was_sent(sess_dir):
                continue   # 발송 안 된 재실행·테스트본은 병합 금지(비발행 예측 유입 차단)
            merged = 0
            for kind, key in (("pick", "picks"), ("short", "shorts")):
                for it in (ldata.get(key) or []):
                    if not isinstance(it, dict):
                        continue
                    k = (kind, str(it.get("ticker") or ""), str(it.get("horizon_days") or ""))
                    if k in have or not k[1]:
                        continue
                    it2 = dict(it)
                    it2["_merged_from"] = os.path.basename(sess_dir)
                    wdata.setdefault(key, []).append(it2)
                    have.add(k)
                    merged += 1
            if merged:
                log.info(f"[acc] {date}: 실발송 구세션 {os.path.basename(sess_dir)} 의 "
                         f"고유 예측 {merged}건 병합(A38)")
    preds = [v[1] for v in by_date.values()]
    preds.sort(key=lambda d: str(d.get("date") or ""))
    return preds


# =====================================================================
# 2. FDR 시세 조회 (거래일 인덱스 기반, 캐시)
# =====================================================================
_PRICE_CACHE = {}  # symbol -> DataFrame (또는 None)


def _fetch_history(symbol, start_date_str):
    """
    symbol 의 일봉 DataFrame(거래일 인덱스)을 FDR 로 조회. 실패/빈 응답 시 None.
    start_date_str: 'YYYY-MM-DD'. 종료일은 미지정(최신까지).
    동일 symbol 은 캐시(여유 있게 과거부터 한 번만 조회).
    """
    if not FDR_AVAILABLE:
        return None
    if symbol in _PRICE_CACHE:
        return _PRICE_CACHE[symbol]
    df = None
    try:
        df = fdr.DataReader(symbol, start_date_str)
        if df is None or df.empty:
            df = None
    except Exception as e:
        log.warning(f"[acc] FDR 조회 실패 ({symbol}): {type(e).__name__}: {e}")
        df = None
    _PRICE_CACHE[symbol] = df
    return df


def _close_col(df):
    """종가 컬럼명 추출(FDR 영문 'Close' 우선, pykrx 한글 '종가' 폴백)."""
    cols = list(df.columns)
    if "Close" in cols:
        return "Close"
    if "종가" in cols:
        return "종가"
    return None


def _to_date(d):
    """문자열/Timestamp → date. 실패 시 None."""
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date()
    try:
        return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _trading_dates(df):
    """DataFrame 인덱스를 거래일 date 리스트(오름차순)로 변환."""
    out = []
    for ix in df.index:
        try:
            out.append(ix.date() if hasattr(ix, "date") else _to_date(ix))
        except Exception:
            out.append(None)
    return out


def _close_at_horizon(symbol, base_date, horizon_days):
    """
    base_date(예측 기준일) 이후 horizon_days 번째 거래일의 종가를 반환.
    Returns: (close_value, settle_date_str) 또는 (None, None) — 미만기/데이터 부족/실패 시.
      - base_date 가 거래일이면 그날을 0번째로 보고, 그 이후 horizon_days 번째 거래일.
      - base_date 가 거래일이 아니면 그 다음 첫 거래일을 0번째로 본다.
    만기 미도달(아직 미래)이면 (None, None) → 채점 보류.
    """
    df = _fetch_history(symbol, (base_date - timedelta(days=10)).strftime("%Y-%m-%d"))
    if df is None or df.empty:
        return None, None
    ccol = _close_col(df)
    if not ccol:
        return None, None
    dates = _trading_dates(df)
    # base_date 이상인 첫 거래일의 인덱스(0번째)
    start_idx = None
    for i, d in enumerate(dates):
        if d is not None and d >= base_date:
            start_idx = i
            break
    if start_idx is None:
        return None, None
    target_idx = start_idx + int(horizon_days)
    if target_idx >= len(dates):
        # 아직 그 거래일이 도래하지 않음 → 만기 미도달
        return None, None
    try:
        val = float(df[ccol].iloc[target_idx])
        sdate = dates[target_idx]
        sdate_str = sdate.strftime("%Y-%m-%d") if sdate else None
        if val <= 0:
            return None, None
        # ★v11.8(2026-08-04, A40): '오늘' 날짜 봉으로는 **아예 정산하지 않는다** — 다음 실행에서 채점.
        #   구 가드(v11.6: 15:40 이후 허용)가 뚫렸다. 실측: 16:01 채점에서 FDR 지수가 아직
        #   장중 스냅샷(KOSPI -1.13%)이었고 실제 종가는 +1.62% — 방향까지 반대라 down 콜이
        #   '적중'으로 오기록됐다(22건 오염, 당일 재베이스라인). FDR/KRX 의 일봉 확정 시점은
        #   종목·지수마다 달라 시각 기준(15:40)은 신뢰할 수 없다. 멱등 로그는 영구 동결이므로
        #   **하루 늦은 채점이 오염보다 낫다**(정상 흐름인 아침 06:35 채점엔 지연 비용 0).
        if sdate is not None and sdate >= datetime.now().date():
            return None, None
        return val, sdate_str
    except Exception:
        return None, None


def _close_on_or_after(symbol, base_date):
    """base_date 이상 첫 거래일의 종가(=0번째). entry 시점 기준 지수 산출용."""
    return _close_at_horizon(symbol, base_date, 0)


# =====================================================================
# 3. 멱등 로그 (accuracy_log.json)
# =====================================================================
def load_log():
    """accuracy_log.json 로드. 없거나 깨졌으면 빈 구조 반환."""
    base = {"version": 1, "updated": "", "entries": []}
    if not os.path.isfile(LOG_PATH):
        return base
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            return data
    except Exception as e:
        log.warning(f"[acc] accuracy_log 로드 실패(새로 시작): {type(e).__name__}: {e}")
    return base


def _entry_key(kind, pred_date, ticker, horizon):
    """멱등 키. (종류, 예측일, ticker, horizon)."""
    return f"{kind}|{pred_date}|{ticker}|{horizon}"


def save_log(logdata):
    """accuracy_log.json 원자적 저장(UTF-8, ensure_ascii=False)."""
    logdata["updated"] = datetime.now().isoformat()
    tmp = LOG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(logdata, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, LOG_PATH)
    except Exception as e:
        log.warning(f"[acc] accuracy_log 저장 실패: {type(e).__name__}: {e}")


# =====================================================================
# 4. 채점
# =====================================================================
def _safe_float(v):
    try:
        f = float(v)
        return f
    except Exception:
        return None


def _close_before(symbol, base_date):
    """base_date **미만** 마지막 거래일의 종가 — entry_ref(전일 종가)와 같은 빈티지."""
    df = _fetch_history(symbol, (base_date - timedelta(days=15)).strftime("%Y-%m-%d"))
    if df is None or df.empty:
        return None
    ccol = _close_col(df)
    if not ccol:
        return None
    dates = _trading_dates(df)
    last = None
    for i, d in enumerate(dates):
        if d is not None and d < base_date:
            last = i
        elif d is not None and d >= base_date:
            break
    if last is None:
        return None
    try:
        v = float(df[ccol].iloc[last])
        return v if v > 0 else None
    except Exception:
        return None


def _index_return(symbol, base_date, horizon_days, anchor_before=False):
    """지수 수익률(%). 실패 시 None.
    anchor_before=False: base_date(0번째 거래일) 종가 → T+h — **시장콜 채점용**(지수 자체가 예측 대상).
    anchor_before=True : base_date 미만 마지막 종가 → T+h — **alpha 차감용**.
    ★v10.5(2026-07-27 전면감사): 픽/숏의 종목 다리는 entry_ref=D-1 종가인데 지수 다리가 D 종가
      앵커라 추천일 하루치 지수 변동이 alpha 로 오귀속됐다(하락기엔 픽 alpha 과소·숏 alpha 과대의
      한 방향 편향). alpha 경로만 D-1 앵커로 교정 — 시장콜 정의는 불변(이력 채점 일관성)."""
    c0 = _close_before(symbol, base_date) if anchor_before else _close_on_or_after(symbol, base_date)[0]
    ch, sdate = _close_at_horizon(symbol, base_date, horizon_days)
    if c0 is None or ch is None or c0 <= 0:
        return None, None
    return (ch / c0 - 1.0) * 100.0, sdate


def grade_pick(pred_date, item, kind):
    """
    한 개 픽/숏 채점. 만기 미도달/데이터부족 시 None 반환(보류).
    kind: 'pick' 또는 'short'.
    Returns: entry dict 또는 None.
    """
    ticker = str(item.get("ticker") or "").strip()
    if not ticker:
        return None
    horizon = item.get("horizon_days")
    try:
        horizon = int(horizon)
    except Exception:
        return None
    if horizon <= 0:
        return None
    entry_ref = _safe_float(item.get("entry_ref"))
    if entry_ref is None or entry_ref <= 0:
        return None

    base_date = _to_date(pred_date)
    if base_date is None:
        return None

    # 종목 T+h 종가
    close_h, settle_date = _close_at_horizon(ticker, base_date, horizon)
    if close_h is None:
        return None  # 만기 미도달 또는 데이터 부족 → 보류(멱등: 다음 실행에서 재시도)

    # ★v11.6(호스트 감사): entry_ref(계약상 예측 시점 D-1 종가)를 실측 D-1 종가와 대조.
    #   실측: 주말 세션 중복쌍 14그룹 중 8그룹이 서로 다른 entry_ref(예: 039030@06-27 -8.5% 괴리).
    #   (a) 배수 점프(0.5x 미만/2x 초과) = 액면분할·병합·오기록 의심 → 채점 보류
    #       (유령 수익률이 append-only 멱등 로그에 영구 오염되는 것을 원천 차단.
    #        predictions 의 entry_ref 를 사람이 고치면 다음 실행에서 정상 채점된다).
    #   (b) 5% 초과 괴리 = suspect 플래그만 병기 — ★자동 정정 금지(판정은 회고 몫, 원장 규약).
    actual_d1 = _close_before(ticker, base_date)
    ref_gap_pct = None
    if actual_d1 is not None and actual_d1 > 0:
        _ratio = entry_ref / actual_d1
        if _ratio < 0.5 or _ratio > 2.0:
            log.warning(f"[acc] {ticker}@{pred_date} entry_ref {entry_ref} vs 실측 D-1 종가 "
                        f"{actual_d1:.0f} 배수 점프({_ratio:.2f}x) — 기업행위/오기록 의심, 채점 보류")
            return None
        ref_gap_pct = (_ratio - 1.0) * 100.0

    ret_pct = (close_h / entry_ref - 1.0) * 100.0

    # 같은 구간 지수 수익률 → alpha (★anchor_before: 종목 다리 entry_ref=D-1 종가와 같은 빈티지)
    #   ★v11.23 국가별 벤치마크. 예전엔 무조건 코스피였다 — 해외 픽을 넣는 순간
    #   "미국 종목이 코스피를 이겼나"라는 무의미한 알파가 회고 표본에 섞여 **한국 규칙 도출을
    #   오염**시킨다. country 가 없으면 KR → KS11 이므로 기존 예측의 채점은 완전히 동일하다.
    _bench = COUNTRY_BENCH.get(str(item.get("country") or "KR").strip().upper(), KOSPI_SYMBOL)
    kospi_ret, _ = _index_return(_bench, base_date, horizon, anchor_before=True)
    alpha = (ret_pct - kospi_ret) if kospi_ret is not None else None

    tag = _norm_tag(item.get("tag"))
    if kind == "short":
        hit = ret_pct < 0
    else:
        # 긍정 태그(또는 태그 미상)는 상승 기대로 본다
        hit = ret_pct > 0

    conv = _safe_float(item.get("conviction"))
    return {
        "kind": kind,
        "pred_date": str(pred_date),
        "ticker": ticker,
        "name": item.get("name") or ticker,
        "tag": tag if kind == "pick" else "숏",
        "timing": item.get("timing"),
        "horizon": horizon,
        "entry_ref": entry_ref,
        "settle_date": settle_date,
        "close_h": round(close_h, 2),
        "return_pct": round(ret_pct, 2),
        # ★키 이름은 유지한다(append-only 원장의 과거 행·소비자와 호환). 해외 픽이면 값은
        #   코스피가 아니라 그 나라 지수다 — 아래 alpha_bench 로 어느 지수인지 남긴다.
        "kospi_return_pct": round(kospi_ret, 2) if kospi_ret is not None else None,
        "alpha_pct": round(alpha, 2) if alpha is not None else None,
        "country": str(item.get("country") or "KR").strip().upper(),
        "alpha_bench": _bench,
        "conviction": conv,
        "hit": bool(hit),
        "entry_ref_gap_pct": round(ref_gap_pct, 2) if ref_gap_pct is not None else None,
        "entry_ref_suspect": bool(ref_gap_pct is not None and abs(ref_gap_pct) > 5.0),
        "graded_at": datetime.now().isoformat(),
    }


def grade_pick_next_day(pred_date, item, nd):
    """★v11.6(loop-gaps-2): 픽 익일(T+1) 전망 채점 — kind='pick_nd1'.
    dir(up/down/neutral)을 T+1 종목 수익률로 판정(neutral 밴드는 시장콜 T+1 과 동일 0.5%).
    prob 3종이 있으면 Brier 병기(grade_market_call #WS 와 같은 규약). 만기 미도달 시 None."""
    ticker = str(item.get("ticker") or "").strip()
    direction = str(nd.get("dir") or "").strip().lower()
    if not ticker or direction not in ("up", "down", "neutral"):
        return None
    entry_ref = _safe_float(item.get("entry_ref"))
    if entry_ref is None or entry_ref <= 0:
        return None
    base_date = _to_date(pred_date)
    if base_date is None:
        return None
    close_1, settle_date = _close_at_horizon(ticker, base_date, 1)
    if close_1 is None:
        return None
    ret_1 = (close_1 / entry_ref - 1.0) * 100.0
    band = NEUTRAL_BAND_BY_H.get(1, NEUTRAL_BAND_PCT)
    if direction == "neutral":
        hit = abs(ret_1) < band
    elif direction == "up":
        hit = ret_1 > 0
    else:
        hit = ret_1 < 0
    entry = {
        "kind": "pick_nd1",
        "pred_date": str(pred_date),
        "ticker": ticker,
        "name": item.get("name") or ticker,
        "dir": direction,
        "horizon": 1,
        "entry_ref": entry_ref,
        "settle_date": settle_date,
        "close_h": round(close_1, 2),
        "return_pct": round(ret_1, 2),
        "expected_pct": _safe_float(nd.get("expected_pct")),
        "hit": bool(hit),
        "graded_at": datetime.now().isoformat(),
    }
    pu, pf, pdn = (_safe_float(nd.get("prob_up")), _safe_float(nd.get("prob_flat")),
                   _safe_float(nd.get("prob_down")))
    if None not in (pu, pf, pdn) and abs((pu + pf + pdn) - 1.0) < 0.05:
        if ret_1 > band:
            o = (1.0, 0.0, 0.0)
        elif ret_1 < -band:
            o = (0.0, 0.0, 1.0)
        else:
            o = (0.0, 1.0, 0.0)
        entry["prob_up"], entry["prob_flat"], entry["prob_down"] = pu, pf, pdn
        entry["brier"] = round((pu - o[0]) ** 2 + (pf - o[1]) ** 2 + (pdn - o[2]) ** 2, 4)
    return entry


def grade_market_call(pred_date, market_name, call, horizon):
    """
    market_call(kospi/kosdaq) 한 개를 T+horizon 으로 채점. 만기 미도달 시 None.
    dir: up/down/neutral. neutral 은 |구간등락|<NEUTRAL_BAND_PCT 면 적중.
    """
    if not isinstance(call, dict):
        return None
    direction = str(call.get("dir") or "").strip().lower()
    if direction not in ("up", "down", "neutral"):
        return None
    base_date = _to_date(pred_date)
    if base_date is None:
        return None
    symbol = KOSPI_SYMBOL if market_name == "kospi" else KOSDAQ_SYMBOL
    idx_ret, settle_date = _index_return(symbol, base_date, horizon)
    if idx_ret is None:
        return None  # 만기 미도달 또는 데이터 부족 → 보류

    band = NEUTRAL_BAND_BY_H.get(horizon, NEUTRAL_BAND_PCT)
    if direction == "neutral":
        hit = abs(idx_ret) < band
    elif direction == "up":
        hit = idx_ret > 0
    else:  # down
        hit = idx_ret < 0

    conv = _safe_float(call.get("conviction"))
    entry = {
        "kind": "market",
        "pred_date": str(pred_date),
        "market": market_name,
        "dir": direction,
        "horizon": horizon,
        "settle_date": settle_date,
        "index_return_pct": round(idx_ret, 2),
        "conviction": conv,
        "hit": bool(hit),
        "graded_at": datetime.now().isoformat(),
    }
    # #WS(월가식 확률예보 채점, v9.6 additive): prob_up/flat/down 이 있으면 Brier 점수(3분류, 낮을수록 좋음).
    # 방향 이진 적중(동전던지기 프레임)보다 '확률의 정직성'을 재는 proper scoring rule —
    # 균등확률(1/3,1/3,1/3)의 기대 Brier=0.667 이 무정보 기준선. 확률 필드 없으면 기존 채점 그대로.
    pu, pf, pd = (_safe_float(call.get("prob_up")), _safe_float(call.get("prob_flat")),
                  _safe_float(call.get("prob_down")))
    if None not in (pu, pf, pd) and abs((pu + pf + pd) - 1.0) < 0.05:
        if idx_ret > band:
            o = (1.0, 0.0, 0.0)
        elif idx_ret < -band:
            o = (0.0, 0.0, 1.0)
        else:
            o = (0.0, 1.0, 0.0)
        entry["prob_up"], entry["prob_flat"], entry["prob_down"] = pu, pf, pd
        entry["brier"] = round((pu - o[0]) ** 2 + (pf - o[1]) ** 2 + (pd - o[2]) ** 2, 4)
    return entry


def count_stalled(preds, logdata):
    """★v11.6(scoring-7): 만기+여유(3주+)가 지났는데도 로그에 없는 픽/숏 수 — 무음 미채점.
    거래정지·상폐 종목이 표본에서 조용히 사라지는 생존편향(최악의 픽이 빠져 적중률 상방 편향)을
    scorecard 에 숫자로 노출한다(현재 실측 0건 — 예방 계측)."""
    existing = set()
    for e in logdata["entries"]:
        if e.get("kind") in ("pick", "short"):
            try:
                existing.add((e.get("kind"), str(e.get("pred_date")),
                              str(e.get("ticker")), int(e.get("horizon") or 0)))
            except Exception:
                continue
    today = datetime.now().date()
    n = 0
    for pred in preds:
        pd_ = _to_date(pred.get("date"))
        if pd_ is None:
            continue
        for kind, key_ in (("pick", "picks"), ("short", "shorts")):
            for item in (pred.get(key_) or []):
                if not isinstance(item, dict):
                    continue
                t = str(item.get("ticker") or "").strip()
                try:
                    hz = int(item.get("horizon_days"))
                except Exception:
                    continue
                if not t or hz <= 0:
                    continue
                if (kind, str(pred.get("date")), t, hz) in existing:
                    continue
                if (today - pd_).days > hz * 1.6 + 21:
                    n += 1
    return n


def backfill_missing_alpha(logdata):
    """★v11.6(호스트 감사 2026-08-01): 지수 다리 일시 실패로 alpha_pct=None 인 확정 엔트리의
    지수만 재계산해 백필한다. return_pct 등 확정 필드는 불변(멱등 원칙 유지) — 정산이 끝난
    과거 창의 지수 시세는 불변이므로 백필은 룩어헤드가 아니다.
    실측: 장중 런 2회(07-06·07-20)에서 KS11 당일 봉 부재로 30건(13.1%)이 영구 결측 상태였고
    scorecard 알파 평균이 무음으로 표본 축소된 채 보고되고 있었다."""
    fixed = 0
    today = datetime.now().date()
    for e in logdata["entries"]:
        if e.get("kind") not in ("pick", "short"):
            continue
        if e.get("alpha_pct") is not None or e.get("return_pct") is None:
            continue
        sd = _to_date(e.get("settle_date"))
        bd = _to_date(e.get("pred_date"))
        try:
            hz = int(e.get("horizon"))
        except Exception:
            continue
        if sd is None or bd is None or sd >= today:
            continue  # 정산 미확정 창은 건드리지 않는다
        try:
            kospi_ret, _ = _index_return(KOSPI_SYMBOL, bd, hz, anchor_before=True)
        except Exception:
            kospi_ret = None
        if kospi_ret is None:
            continue
        e["kospi_return_pct"] = round(kospi_ret, 2)
        e["alpha_pct"] = round(float(e["return_pct"]) - kospi_ret, 2)
        e["alpha_backfilled_at"] = datetime.now().isoformat()
        fixed += 1
    return fixed


def grade_all(preds, logdata):
    """
    모든 예측을 순회하며 만기 도달분 채점, accuracy_log 에 멱등 누적.
    Returns: (추가된 entry 수).
    """
    existing = {}
    for e in logdata["entries"]:
        if e.get("kind") == "market":
            k = _entry_key("market_" + str(e.get("market")), e.get("pred_date"),
                           str(e.get("dir")), e.get("horizon"))
        else:
            k = _entry_key(e.get("kind"), e.get("pred_date"),
                           e.get("ticker"), e.get("horizon"))
        existing[k] = e

    added = 0
    for pred in preds:
        pred_date = str(pred.get("date") or "").strip()
        if not pred_date:
            continue

        # ── picks ──
        for item in (pred.get("picks") or []):
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or "").strip()
            horizon = item.get("horizon_days")
            key = _entry_key("pick", pred_date, ticker, horizon)
            if key in existing:
                continue  # 이미 채점 확정 — 재계산 안 함(멱등)
            try:
                res = grade_pick(pred_date, item, "pick")
            except Exception as e:
                log.warning(f"[acc] pick 채점 예외 ({ticker}@{pred_date}): "
                            f"{type(e).__name__}: {e}")
                res = None
            if res:
                logdata["entries"].append(res)
                existing[key] = res
                added += 1
            # ★v11.6(loop-gaps-2): 픽 next_day(T+1 전망)를 별도 kind='pick_nd1' 로 채점.
            #   지시서 [6.5++]는 "T+1 로 별도 채점된다"고 약속했는데 코드가 없었다(문서-코드
            #   계약 위반). 표본 0건인 지금이 적기 — 멱등 로그에 무채점 이력이 쌓이기 전.
            #   dir 판정은 시장콜 T+1 과 같은 밴드(NEUTRAL_BAND_BY_H[1]) 재사용.
            _pnd = item.get("next_day")
            if isinstance(_pnd, dict) and str(_pnd.get("dir") or "").strip():
                nd_key = _entry_key("pick_nd1", pred_date, ticker, 1)
                if nd_key not in existing:
                    try:
                        nd_res = grade_pick_next_day(pred_date, item, _pnd)
                    except Exception as e:
                        log.warning(f"[acc] pick_nd1 채점 예외 ({ticker}@{pred_date}): "
                                    f"{type(e).__name__}: {e}")
                        nd_res = None
                    if nd_res:
                        logdata["entries"].append(nd_res)
                        existing[nd_key] = nd_res
                        added += 1

        # ── shorts ──
        for item in (pred.get("shorts") or []):
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or "").strip()
            horizon = item.get("horizon_days")
            key = _entry_key("short", pred_date, ticker, horizon)
            if key in existing:
                continue
            try:
                res = grade_pick(pred_date, item, "short")
            except Exception as e:
                log.warning(f"[acc] short 채점 예외 ({ticker}@{pred_date}): "
                            f"{type(e).__name__}: {e}")
                res = None
            if res:
                logdata["entries"].append(res)
                existing[key] = res
                added += 1

        # ── market_call (T+1, T+5) ──
        mc = pred.get("market_call") or {}
        for market_name in ("kospi", "kosdaq"):
            call = mc.get(market_name)
            if not isinstance(call, dict):
                continue
            for horizon in (1, 5):
                # ★v11.1: T+1 은 **익일 전용 예측(next_day)** 이 있으면 그걸로 채점한다.
                #   [왜] 확률 1세트를 T+1·T+5 양쪽에 채점해 둘 다 놓쳤다(실측 25%/27%).
                #   두 지평은 지배 요인이 다르다 — T+1 은 간밤 갭·수급, T+5 는 추세·국면.
                #   next_day 가 없는 과거 세션은 종전대로 본 콜을 양쪽에 쓴다(하위호환).
                _nd = call.get("next_day")
                _target = _nd if (horizon == 1 and isinstance(_nd, dict)) else call
                key = _entry_key("market_" + market_name, pred_date,
                                 str(_target.get("dir")), horizon)
                if key in existing:
                    continue
                try:
                    res = grade_market_call(pred_date, market_name, _target, horizon)
                except Exception as e:
                    log.warning(f"[acc] market 채점 예외 ({market_name}@{pred_date}): "
                                f"{type(e).__name__}: {e}")
                    res = None
                if res:
                    # ★v11.6(loop-gaps-6): 채점 출처 마커 — T+1 이 신방식(next_day 전용 예측)으로
                    #   채점됐는지 구방식(본 콜 이중사용, 실측 25%)인지 집계에서 분리 가능하게.
                    #   이게 없으면 v11.1 이 증명하려던 'T+1 개선'을 스스로 측정할 수 없다.
                    res["scored_from"] = ("next_day" if (horizon == 1 and isinstance(_nd, dict))
                                          else "main_call")
                    logdata["entries"].append(res)
                    existing[key] = res
                    added += 1

    return added


# =====================================================================
# 5. 집계 & scorecard.md
# =====================================================================
def _norm_tag(t):
    """태그 표기 정규화: '[단기스윙]' → '단기스윙'.

    accuracy_log.json 은 append-only 라, 과거에 대괄호를 포함해 기록된 엔트리가 영구히 남는다.
    정규화하지 않으면 같은 태그가 두 그룹으로 쪼개져 표본이 갈리고 태그 통계가 왜곡된다
    (실측 2026-07-17: '단기스윙' 52건 vs '[단기스윙]' 4건, '장전선취매' 2 vs 2 로 분할돼 있었다).
    채점(grade_pick)과 집계(aggregate) 양쪽에 적용해 과거 기록도 병합한다(로그 파일은 무수정).
    """
    s = str(t or "").strip()
    while len(s) > 2 and s.startswith("[") and s.endswith("]"):
        s = s[1:-1].strip()
    return s


def _pct(numer, denom):
    if not denom:
        return None
    return numer / denom * 100.0


def _fmt_pct(v, digits=1):
    if v is None:
        return "N/A"
    return f"{v:+.{digits}f}%" if digits else f"{v:+.0f}%"


def _fmt_rate(v):
    if v is None:
        return "N/A"
    return f"{v:.0f}%"


def _recent_pred_dates(entries, n=RECENT_DAYS):
    """엔트리에서 최근 n개 예측일 집합."""
    dates = sorted({str(e.get("pred_date") or "") for e in entries if e.get("pred_date")})
    return set(dates[-n:]) if dates else set()


def aggregate(entries):
    """엔트리 → scorecard 작성에 필요한 집계 dict."""
    recent = _recent_pred_dates(entries, RECENT_DAYS)
    rset = [e for e in entries if str(e.get("pred_date") or "") in recent]
    # #A14(회고 15회차): 주말·휴장일 발행 콜은 다음 거래일 콜과 '같은 채점 창'을 본다(같은 settle_date)
    # — 3콜이 같은 창을 3표로 계상하던 중복을 접는다: (market,horizon,settle_date)당 최신 pred_date 1건.
    _mkt_dedup = {}
    for e in rset:
        if e.get("kind") != "market":
            continue
        k = (e.get("market"), e.get("horizon"), str(e.get("settle_date") or ""))
        prev = _mkt_dedup.get(k)
        if prev is None or str(e.get("pred_date") or "") > str(prev.get("pred_date") or ""):
            _mkt_dedup[k] = e
    _mkt_keep = set(id(e) for e in _mkt_dedup.values())

    # ★v11.6(호스트 감사 2026-08-01): #A14 같은 창 접기를 **픽·숏에도 확장**.
    #   주말·휴장일 발행 픽은 다음 거래일 픽과 같은 정산 창을 본다 — 같은 결과가 2~3표로
    #   계상돼 유효 N 과대·오차 자기상관(실측: 중복 그룹 14개·초과 엔트리 18건, 예: 000270
    #   pred 07-17/18/19 3건 전부 ret -12.89 동일 창). (kind,ticker,horizon,settle_date)당
    #   최신 pred_date 1건만 집계(로그 파일은 무수정 — 집계 시점 접기라 멱등 원칙과 무충돌).
    _stk_dedup = {}
    for e in rset:
        if e.get("kind") not in ("pick", "short", "pick_nd1"):
            continue
        k = (e.get("kind"), e.get("ticker"), e.get("horizon"), str(e.get("settle_date") or ""))
        prev = _stk_dedup.get(k)
        if prev is None or str(e.get("pred_date") or "") > str(prev.get("pred_date") or ""):
            _stk_dedup[k] = e
    _stk_keep = set(id(e) for e in _stk_dedup.values())

    agg = {
        "n_pred_dates": len(recent),
        "date_range": (min(recent), max(recent)) if recent else (None, None),
        "market": {},      # horizon -> {hit, total}
        "picks": {"total": 0, "hit": 0, "ret_sum": 0.0, "ret_n": 0,
                  "alpha_sum": 0.0, "alpha_n": 0},
        "shorts": {"total": 0, "hit": 0, "ret_sum": 0.0, "ret_n": 0,
                   "alpha_sum": 0.0, "alpha_n": 0, "idx_down": 0, "idx_n": 0},
        "picks_nd1": {"total": 0, "hit": 0, "brier_sum": 0.0, "brier_n": 0},   # v11.6 픽 T+1 전망
        "by_tag": {},      # tag -> {total, hit, ret_sum, ret_n, alpha_sum, alpha_n}
        "by_timing": {},   # timing(임박/단기/중기) -> 동일 구조 (T1: 신호별 적중률)
        "calib": {b[0]: {"total": 0, "hit": 0} for b in CALIB_BUCKETS},
        "examples_hit": [],
        "examples_miss": [],
    }

    for e in rset:
        kind = e.get("kind")
        conv = e.get("conviction")
        hit = bool(e.get("hit"))

        # #A14 같은 창 중복 콜 접기 — ★calibration 집계보다 먼저 스킵해야 시장콜 중복이
        #   calib 버킷에 이중계상되지 않는다(시장콜은 conviction 을 가지므로 아래 calib 에 들어간다).
        if kind == "market" and id(e) not in _mkt_keep:
            continue
        # ★v11.6: 픽·숏도 동일 — 같은 창 중복은 calib·태그·타이밍 전 집계에서 1건으로.
        if kind in ("pick", "short", "pick_nd1") and id(e) not in _stk_keep:
            continue

        if kind == "pick_nd1":               # v11.6 픽 T+1 전망(별도 kind — 본 픽 집계 불오염)
            nd = agg["picks_nd1"]
            nd["total"] += 1
            if hit:
                nd["hit"] += 1
            _b = _safe_float(e.get("brier"))
            if _b is not None:
                nd["brier_sum"] += _b
                nd["brier_n"] += 1
            continue

        # calibration (모든 종류 공통; conviction 있는 것만)
        if conv is not None:
            for label, lo, hi in CALIB_BUCKETS:
                in_bucket = (conv <= hi) and (conv > lo or (lo == 0.0 and conv >= 0.0))
                if in_bucket:
                    agg["calib"][label]["total"] += 1
                    if hit:
                        agg["calib"][label]["hit"] += 1
                    break

        if kind == "market":
            h = e.get("horizon")
            m = agg["market"].setdefault(h, {"total": 0, "hit": 0,
                                             "brier_sum": 0.0, "brier_n": 0,
                                             "by_dir": {}, "by_src": {},
                                             "bench_down_hit": 0})
            m["total"] += 1
            if hit:
                m["hit"] += 1
            d = agg["market"][h]["by_dir"].setdefault(str(e.get("dir")), {"total": 0, "hit": 0})
            d["total"] += 1
            if hit:
                d["hit"] += 1
            # #A44(24회차): T+1 을 '무엇으로 채점했나' 분해 — next_day(v11.1 전용 예측) vs
            #   main_call(본 콜 이중사용) vs 미기록(v11.6 마커 이전 구건). 이게 없으면
            #   v11.1 의 'T+1 분리 개선'을 3회차째 아무도 측정하지 못한다.
            s = m["by_src"].setdefault(str(e.get("scored_from") or "미기록(구건)"),
                                       {"total": 0, "hit": 0})
            s["total"] += 1
            if hit:
                s["hit"] += 1
            ir = _safe_float(e.get("index_return_pct"))   # #A14 always-down 벤치마크
            if ir is not None and ir < 0:
                m["bench_down_hit"] = m.get("bench_down_hit", 0) + 1
            b = _safe_float(e.get("brier"))          # #WS 확률예보(있을 때만)
            if b is not None:
                m["brier_sum"] = m.get("brier_sum", 0.0) + b
                m["brier_n"] = m.get("brier_n", 0) + 1

        elif kind == "pick":
            p = agg["picks"]
            p["total"] += 1
            if hit:
                p["hit"] += 1
            r = e.get("return_pct")
            if r is not None:
                p["ret_sum"] += r
                p["ret_n"] += 1
            a = e.get("alpha_pct")
            if a is not None:
                p["alpha_sum"] += a
                p["alpha_n"] += 1
            tag = _norm_tag(e.get("tag")) or "(태그없음)"
            t = agg["by_tag"].setdefault(
                tag, {"total": 0, "hit": 0, "ret_sum": 0.0, "ret_n": 0,
                      "alpha_sum": 0.0, "alpha_n": 0})
            t["total"] += 1
            if hit:
                t["hit"] += 1
            if r is not None:
                t["ret_sum"] += r
                t["ret_n"] += 1
            if a is not None:
                t["alpha_sum"] += a
                t["alpha_n"] += 1
            # 타이밍별(임박/단기/중기) 집계 — by_tag 와 동일 구조 (T1 신호별 적중률)
            tm = e.get("timing") or "(미지정)"
            ty = agg["by_timing"].setdefault(
                tm, {"total": 0, "hit": 0, "ret_sum": 0.0, "ret_n": 0,
                     "alpha_sum": 0.0, "alpha_n": 0})
            ty["total"] += 1
            if hit:
                ty["hit"] += 1
            if r is not None:
                ty["ret_sum"] += r
                ty["ret_n"] += 1
            if a is not None:
                ty["alpha_sum"] += a
                ty["alpha_n"] += 1

        elif kind == "short":
            # ★v10.0(회고 9회 반복 정정): 숏의 '적중률'은 하락장에서 지수 하락 기저율에 먹힌다
            #   (실측 적중 86% vs always-short 기저 ~91%). 우위는 alpha(지수보다 더 빠진 폭)다.
            #   → 시장콜(#A14)과 같은 규약으로 alpha·수익률·무정보 기저율을 함께 집계한다.
            s = agg["shorts"]
            s["total"] += 1
            if hit:
                s["hit"] += 1
            sr = _safe_float(e.get("return_pct"))
            if sr is not None:
                s["ret_sum"] = s.get("ret_sum", 0.0) + sr
                s["ret_n"] = s.get("ret_n", 0) + 1
            sa = _safe_float(e.get("alpha_pct"))
            if sa is not None:
                s["alpha_sum"] = s.get("alpha_sum", 0.0) + sa
                s["alpha_n"] = s.get("alpha_n", 0) + 1
            sk = _safe_float(e.get("kospi_return_pct"))   # always-short 벤치마크
            if sk is not None:
                s["idx_n"] = s.get("idx_n", 0) + 1
                if sk < 0:
                    s["idx_down"] = s.get("idx_down", 0) + 1

    # 예시 종목(최근 픽/숏 중 수익률 극단 몇 개) — 같은 창 중복 접기 후
    scored = [e for e in rset if e.get("kind") in ("pick", "short")
              and e.get("return_pct") is not None and id(e) in _stk_keep]
    scored_sorted = sorted(scored, key=lambda e: e.get("return_pct"), reverse=True)
    hits = [e for e in scored_sorted if e.get("hit")]
    misses = [e for e in scored_sorted if not e.get("hit")]
    agg["examples_hit"] = hits[:3]
    # 오답은 손실 큰 순(픽) / 가장 안 맞은 순
    agg["examples_miss"] = sorted(misses, key=lambda e: e.get("return_pct"))[:3]
    return agg


def _wilson_ci(hit, n, z=1.96):
    """Wilson 95% CI (하한%, 상한%) — ★v11.6 소표본 권고 가드. n=0 이면 (None, None)."""
    if not n:
        return None, None
    p = hit / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    hw = (z / den) * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return (center - hw) * 100.0, (center + hw) * 100.0


def build_recommendations(agg):
    """집계 → 한국어 권고 2~4줄(휴리스틱 자동 생성). 이모지 금지.
    ★v11.6(호스트 감사): 최소표본 3→10 상향 + Wilson 95% CI 병기 + CI 반폭 >15%p 면 판단 보류.
    N=3~5 에서 나온 방향 처방(conviction 하향 등)이 매일 [0.5] 자기보정 입력으로 들어가던
    노이즈→권고 경로를 차단한다(임계 45/65 자체는 회고 실측 재도출 전까지 잠정 유지)."""
    recs = []
    n = agg["n_pred_dates"]

    # 표본 부족
    if n < RECENT_DAYS:
        recs.append(f"표본 N={n} (< {RECENT_DAYS}) — 통계 신뢰도 낮음, 참고용으로만 활용.")

    # 시장 방향(약세콜 포함) — T+5 우선, 없으면 T+1
    mkt = agg["market"]
    for h in (5, 1):
        if h in mkt and mkt[h]["total"] >= 10:
            rate = _pct(mkt[h]["hit"], mkt[h]["total"])
            lo95, hi95 = _wilson_ci(mkt[h]["hit"], mkt[h]["total"])
            ci_txt = f" [95%CI {lo95:.0f}~{hi95:.0f}%]" if lo95 is not None else ""
            if lo95 is not None and (hi95 - lo95) / 2 > 15:
                recs.append(f"시장 방향(T+{h}) 적중률 {rate:.0f}%{ci_txt} — CI 반폭 >15%p, "
                            f"표본부족: 방향 처방 보류.")
            elif rate is not None and rate < 45:
                recs.append(f"시장 방향(T+{h}) 적중률 {rate:.0f}%{ci_txt} 낮음 "
                            f"-> 방향성 콜 신중, conviction 하향 권장.")
            elif rate is not None and rate >= 65:
                recs.append(f"시장 방향(T+{h}) 적중률 {rate:.0f}%{ci_txt} 양호 -> 현 판단 유지.")
            break

    # 태그별 성과 — ★서술만, 처방 금지(회고 6회 연속 기각: 2026-06-27~07-12)
    #   이유: 이 집계는 '최근 RECENT_DAYS 예측일' 창이라 국면(강세추격장)에 편중될 수 있고,
    #   엔트리에 국면·출처 정보가 없어(grade_pick 스키마에 _src_kind 없음) 태그 효과를 분리할 수 없다.
    #   실제로 회고가 완전표본으로 6회 재검한 결과 '단기스윙 부진'은 태그가 아니라 국면·출처 귀속이었고,
    #   태그 축소 처방은 매번 기각됐다. 그래서 여기서는 수치만 보고하고 판단은 회고/[0.5]에 넘긴다.
    for tag, t in sorted(agg["by_tag"].items(), key=lambda kv: -kv[1]["total"]):
        if t["total"] < 10:      # v11.6: 3→10
            continue
        avg_ret = _pct_avg(t["ret_sum"], t["ret_n"])
        rate = _pct(t["hit"], t["total"])
        if avg_ret is None:
            continue
        if avg_ret >= 1.0 and (rate or 0) >= 55:
            recs.append(f"[{tag}] 평균 {avg_ret:+.1f}%, 적중 {rate:.0f}% 양호(참고) "
                        f"-> 태그 단독 비중 조정 금지, 국면·종목 근거로 판단.")
        elif avg_ret <= -1.0 or (rate is not None and rate < 40):
            recs.append(f"[{tag}] 평균 {avg_ret:+.1f}%, 적중 {_fmt_rate(rate)} 부진(참고) "
                        f"-> ★태그 탓으로 단정·축소 금지(최근 {RECENT_DAYS}일 국면 편중 가능). "
                        f"회고가 6회 기각한 처방이다. 원인은 retro_dataset 의 국면(pre_kospi_ret5d)·"
                        f"출처(_src_kind)로 대조하라.")
        if len(recs) >= 4:
            break

    # calibration 과신 진단
    if len(recs) < 4:
        for label, lo, hi in reversed(CALIB_BUCKETS):
            c = agg["calib"].get(label, {})
            if c.get("total", 0) >= 10:      # v11.6: 3→10
                rate = _pct(c["hit"], c["total"])
                mid = (lo + hi) / 2 * 100
                if rate is not None and rate < mid - 15:
                    recs.append(f"고확신 구간 {label} 실제적중 {rate:.0f}% "
                                f"<< 기대 {mid:.0f}% -> 과신 경향, conviction 보정 필요.")
                    break

    if not recs:
        recs.append("최근 채점 결과 특이사항 없음 -> 현 전략 유지.")
    return recs[:4]


def _pct_avg(s, n):
    if not n:
        return None
    return s / n


def missing_pred_days(entries):
    """#A45(24회차): 마지막 예측일 이후 '예측이 발행되지 않은 거래일' 목록.

    2026-08-13·14·18 3거래일 미발행을 아무도 몰랐다(계정 전환으로 예정작업 소실) — 회고가
    2026-08-19 에야 발견했다. 다음 실행이 과거 공백을 소급 감지해 사람에게 보인다.

    거래일 판정은 달력·휴일표가 아니라 **실제 KS11 봉 존재**로 한다 — krx_holidays.json 은
    과거(2026-08-07까지)만 알아서 광복절 대체휴일 같은 날을 거래일로 오판해 오경보를 낸다.
    범위는 어제까지(오늘은 발행 진행 중일 수 있음). FDR 실패 시 None(경고 생략 — fail-open).
    ★입력은 predictions(발행분 전체)이어야 한다 — accuracy_log entries 는 '만기 채점분'만
      담아서, 오늘 발행이 있어도 최근 3~20일 미만기 구간이 통째로 '미발행'으로 오경보난다
      (구현 직후 실측: 08-19 발행이 있는데 last_pred=08-12 로 나옴).
    """
    try:
        dates = sorted({str(e.get("pred_date") or e.get("date") or "")[:10]
                        for e in entries if e.get("pred_date") or e.get("date")})
        if not dates:
            return None
        last = datetime.strptime(dates[-1], "%Y-%m-%d").date()
        yday = datetime.now().date() - timedelta(days=1)
        if last >= yday or not FDR_AVAILABLE:
            return None
        df = fdr.DataReader(KOSPI_SYMBOL,
                            (last + timedelta(days=1)).isoformat(), yday.isoformat())
        if df is None or df.empty:
            return None
        traded = [d.strftime("%Y-%m-%d") for d in df.index]
        have = set(dates)
        gap = [d for d in traded if d not in have]
        return {"last_pred": dates[-1], "missing": gap} if gap else None
    except Exception:
        return None


def build_scorecard(agg, total_entries, n_added):
    """집계 → scorecard.md 본문(한국어, 이모지 금지)."""
    L = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    d0, d1 = agg["date_range"]
    L.append("# 예측 정확도 점수표 (scorecard)")
    L.append("")
    L.append(f"- 생성 시각: {now} KST")
    L.append(f"- 집계 대상: 최근 {agg['n_pred_dates']} 예측일"
             + (f" ({d0} ~ {d1})" if d0 else ""))
    L.append(f"- 누적 채점 엔트리: {total_entries}건 (이번 실행 추가 {n_added}건)")
    L.append("- ★모집단 주의(회고 06-29 요청): 이 카드는 '최근 창(predictions.json 구조화 예측)' 기준이다."
             " 회고 retro_dataset(전 기간·archive 파싱 포함 완전표본)과 모집단이 달라 수치가 어긋날 수"
             " 있다 — 결론이 다르면 완전표본(회고) 쪽을 우선하라.")
    gap = agg.get("missing_pred_days")
    if gap:
        L.append(f"- ★★운영 경고(#A45): 마지막 예측일 {gap['last_pred']} 이후 **거래일 "
                 f"{len(gap['missing'])}일 미발행** — {', '.join(gap['missing'][:6])}"
                 + (" 외" if len(gap['missing']) > 6 else "")
                 + ". 회고 표본이 그만큼 늙는다. ※소급 발행 금지(사후 정보 오염) — 원인만 확인하라.")
    L.append("")

    # 권고(맨 위 강조)
    L.append("## 권고 (다음 분석 시 반영)")
    for r in build_recommendations(agg):
        L.append(f"- {r}")
    L.append("")

    # 시장 방향
    L.append("## 1. 시장 방향 적중률 (market_call)")
    mkt = agg["market"]
    if not mkt:
        L.append("- (만기 도달한 시장 방향 예측 없음)")
    else:
        for h in sorted(mkt.keys()):
            m = mkt[h]
            rate = _pct(m["hit"], m["total"])
            line = f"- T+{h}: 적중 {m['hit']}/{m['total']} ({_fmt_rate(rate)})"
            if m.get("total"):
                bench = _pct(m.get("bench_down_hit", 0), m["total"])
                line += f" | 벤치마크(always-down) {_fmt_rate(bench)}"    # #A14 정직 기준선
            if m.get("brier_n", 0) >= 3:            # #WS 확률예보 품질(낮을수록 좋음, 0.667=무정보)
                line += (f" | Brier {m['brier_sum'] / m['brier_n']:.3f}"
                         f" (N={m['brier_n']}, 무정보 기준선 0.667)")
            L.append(line)
            bd = m.get("by_dir") or {}
            if bd:
                parts = [f"{k} {v['hit']}/{v['total']}" for k, v in sorted(bd.items())]
                L.append(f"  · dir별: {' / '.join(parts)}  (주말·휴장 중복 콜은 같은 창 1건으로 접음)")
            # #A44: T+1 채점 출처 분해 — 회고가 scored_from 별 성적을 독립 검증할 수 있게 병기.
            bs = m.get("by_src") or {}
            if h == 1 and bs:
                parts = [f"{k} {v['hit']}/{v['total']} ({_fmt_rate(_pct(v['hit'], v['total']))})"
                         for k, v in sorted(bs.items())]
                L.append(f"  · 채점출처별(#A44): {' / '.join(parts)}"
                         "  — next_day=익일 전용 예측(v11.1) / main_call=본 콜 이중사용")
        L.append("  (neutral 밴드 v9.6: T+1 ±0.5% / T+5 ±1.2% — 2026-07-19 이후 채점분부터. "
                 "Brier 는 prob_up/flat/down 제출 콜만 집계)")
    L.append("")

    # 픽 성과
    L.append("## 2. 픽(매수 아이디어) 성과")
    p = agg["picks"]
    if p["total"] == 0:
        L.append("- (만기 도달한 픽 없음)")
    else:
        rate = _pct(p["hit"], p["total"])
        avg_ret = _pct_avg(p["ret_sum"], p["ret_n"])
        avg_alpha = _pct_avg(p["alpha_sum"], p["alpha_n"])
        L.append(f"- 채점 픽 수: {p['total']}건 (주말·휴장 중복 픽은 같은 정산 창 1건으로 접음 — "
                 "2026-08-01 집계분부터, #A14 확장)")
        L.append(f"- 적중률: {p['hit']}/{p['total']} ({_fmt_rate(rate)})")
        L.append(f"- 평균 수익률: {_fmt_pct(avg_ret)}")
        L.append(f"- 평균 alpha(코스피 대비): {_fmt_pct(avg_alpha)}")
        if p.get("alpha_n"):
            # 숏 주석(아래)과 동형 — '평균 수익률 음수 = 종목선별 실패'라는 오독을 매 회차 차단한다.
            L.append("  ※ 픽 평균 수익률이 음수여도 alpha>=0 이면 손실은 시장 베타다 —"
                     " 처방은 '픽 억제'가 아니라 순노출 축소(회고 3회 재현, [0.5]/F8-b).")
    nd = agg.get("picks_nd1") or {}
    if nd.get("total"):
        _ndr = _pct(nd["hit"], nd["total"])
        _l = f"- 픽 익일(T+1) 전망: 적중 {nd['hit']}/{nd['total']} ({_fmt_rate(_ndr)})"
        if nd.get("brier_n"):
            _l += f" | Brier {nd['brier_sum'] / nd['brier_n']:.3f} (무정보 0.667)"
        L.append(_l + "  (v11.6 신설 — 표본 30건+ 전까지 참고만)")
    s = agg["shorts"]
    if s["total"] > 0:
        srate = _pct(s["hit"], s["total"])
        _line = f"- 숏 적중률: {s['hit']}/{s['total']} ({_fmt_rate(srate)})"
        if s.get("idx_n", 0) >= 3:      # always-short 무정보 기저율(같은 창의 지수 하락 빈도)
            _line += f" | 벤치마크(always-short) {_fmt_rate(_pct(s['idx_down'], s['idx_n']))}"
        L.append(_line)
        L.append(f"- 숏 평균 수익률: {_fmt_pct(_pct_avg(s.get('ret_sum', 0.0), s.get('ret_n', 0)))}")
        L.append(f"- 숏 평균 alpha(코스피 대비): "
                 f"{_fmt_pct(_pct_avg(s.get('alpha_sum', 0.0), s.get('alpha_n', 0)))}")
        L.append("  ※ 하락장에선 숏 방향적중이 지수 하락 기저율에 먹힌다 — 숏의 우위는 '적중률'이"
                 " 아니라 alpha(지수보다 더 빠진 폭)로 판단하라(회고 9회 반복 정정).")
    L.append("")

    # 태그별
    L.append("## 3. 태그별 성과 분해")
    if not agg["by_tag"]:
        L.append("- (태그별 데이터 없음)")
    else:
        L.append("| 태그 | 건수 | 적중률 | 평균수익률 | 평균alpha |")
        L.append("|---|---|---|---|---|")
        _has_untagged = False
        for tag, t in sorted(agg["by_tag"].items(), key=lambda kv: -kv[1]["total"]):
            rate = _pct(t["hit"], t["total"])
            ar = _pct_avg(t["ret_sum"], t["ret_n"])
            aa = _pct_avg(t["alpha_sum"], t["alpha_n"])
            _mark = ""
            if tag == "(태그없음)":
                _has_untagged = True
                _mark = " ※"
            L.append(f"| [{tag}]{_mark} | {t['total']} | {_fmt_rate(rate)} | "
                     f"{_fmt_pct(ar)} | {_fmt_pct(aa)} |")
        if _has_untagged:
            L.append("")
            L.append("  ※ (태그없음) = tag 필드 결측 행(archive 파싱 유래 추정) — 신뢰 한 단계 하향해"
                     " 읽어라(회고 07-16 요청). 발송 게이트가 tag 를 필수화한 이후 신규 유입은 없어야"
                     " 정상이며, 이 행이 계속 늘면 게이트 우회 경로를 의심하라.")
    L.append("")

    # 타이밍별(임박/단기/중기) — '언제 오른다'는 콜이 실제로 맞았는지(T1 신호별 적중률)
    L.append("## 3.5 타이밍별 성과 분해 (임박/단기/중기)")
    if not agg.get("by_timing"):
        L.append("- (타이밍 데이터 없음 — predictions.json 에 timing 누적되면 채워짐)")
    else:
        L.append("| 예상시점 | 건수 | 적중률 | 평균수익률 | 평균alpha |")
        L.append("|---|---|---|---|---|")
        _torder = {"임박": 0, "단기": 1, "중기": 2}
        for tm, t in sorted(agg["by_timing"].items(),
                            key=lambda kv: _torder.get(kv[0], 9)):
            rate = _pct(t["hit"], t["total"])
            ar = _pct_avg(t["ret_sum"], t["ret_n"])
            aa = _pct_avg(t["alpha_sum"], t["alpha_n"])
            L.append(f"| {tm} | {t['total']} | {_fmt_rate(rate)} | "
                     f"{_fmt_pct(ar)} | {_fmt_pct(aa)} |")
    L.append("")

    # calibration
    L.append("## 4. Calibration (확신구간 vs 실제적중률)")
    L.append("확신(conviction)이 높을수록 실제 적중률도 높아야 정상. "
             "괴리가 크면 과신/과소.")
    L.append("")
    L.append("| 확신구간 | 표본 | 실제적중률 |")
    L.append("|---|---|---|")
    any_calib = False
    for label, lo, hi in CALIB_BUCKETS:
        c = agg["calib"].get(label, {"total": 0, "hit": 0})
        if c["total"] == 0:
            L.append(f"| {label} | 0 | N/A |")
            continue
        any_calib = True
        rate = _pct(c["hit"], c["total"])
        L.append(f"| {label} | {c['total']} | {_fmt_rate(rate)} |")
    if not any_calib:
        L.append("")
        L.append("- (아직 채점된 표본이 없어 calibration 산출 불가)")
    # ★표본 수가 줄어도 채점 오류가 아니다 — 회고가 '표본 감소 = 누락'으로 오독한 전례가 있어 명시한다.
    L.append("")
    L.append("  ※ 표본 계산은 §1 과 동일하게 '주말·휴장 중복 시장콜을 같은 창 1건으로 접은'"
             " 뒤 이뤄진다(2026-07-21 채점분부터 calibration 에도 적용). 따라서"
             f" 신규 채점이 0건이어도 이 규칙 적용·집계창(최근 {RECENT_DAYS} 예측일) 롤링으로"
             " 표본 수가 줄 수 있다 — 표본 감소만으로 채점 오류를 단정하지 마라.")
    L.append("")

    # 예시
    L.append("## 5. 최근 눈에 띄는 종목")
    eh = agg["examples_hit"]
    em = agg["examples_miss"]
    if not eh and not em:
        L.append("- (예시로 보일 채점 종목 없음)")
    else:
        if eh:
            L.append("적중 사례:")
            for e in eh:
                L.append(f"- [{_norm_tag(e.get('tag'))}] {e.get('name')}({e.get('ticker')}) "
                         f"{e.get('pred_date')} T+{e.get('horizon')}: "
                         f"수익률 {_fmt_pct(e.get('return_pct'))}, "
                         f"alpha {_fmt_pct(e.get('alpha_pct'))}")
        if em:
            L.append("오답 사례:")
            for e in em:
                L.append(f"- [{_norm_tag(e.get('tag'))}] {e.get('name')}({e.get('ticker')}) "
                         f"{e.get('pred_date')} T+{e.get('horizon')}: "
                         f"수익률 {_fmt_pct(e.get('return_pct'))}, "
                         f"alpha {_fmt_pct(e.get('alpha_pct'))}")
    L.append("")
    L.append("---")
    L.append("주: 공개데이터(FinanceDataReader) 기반 사후 채점이며, entry_ref 는 "
             "예측 시점 값 그대로 사용(룩어헤드 없음). 만기 미도달 건은 자동 보류 후 "
             "다음 실행에서 재시도됩니다.")
    L.append("주2(재채점 이력): 2026-08-01 장중 미완성 봉 43건(A36) + 2026-08-04 당일 미확정 봉"
             " 22건(A40 — 16:01 채점에서 FDR 지수가 -1.13% 장중값, 실제 종가 +1.62%로 방향까지"
             " 반대)을 재베이스라인 — 이전 scorecard 와 수치가 다르면 이것이 원인이다(원장 등재)."
             " v11.8 부터 **당일 날짜 봉으로는 아예 정산하지 않는다**(다음 실행에서 확정 종가로).")
    if agg.get("n_deriv_unscored"):
        L.append(f"주3(파생): 파생 추천 누적 {agg['n_deriv_unscored']}건은 현재 **무채점 채널**이다"
                 " — 성과 서사 인용 금지. 첫 표본 발생 시 선물 방향 채점 다리를 추가한다.")
    if agg.get("n_stalled") is not None:
        L.append(f"주4(미채점 잔존): 만기+3주 경과에도 채점되지 않은 픽/숏 {agg['n_stalled']}건"
                 " — 0 이 아니면 거래정지·상폐 의심(최악의 픽이 표본에서 빠지는 생존편향)."
                 " 적중률 해석 시 이 소실을 감안하라.")
    return "\n".join(L) + "\n"


def write_empty_scorecard(reason):
    """채점할 예측이 없을 때 안내용 빈 scorecard.md 작성."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    L = [
        "# 예측 정확도 점수표 (scorecard)",
        "",
        f"- 생성 시각: {now} KST",
        f"- 상태: {reason}",
        "",
        "## 권고 (다음 분석 시 반영)",
        "- 아직 채점할 예측이 없습니다. predictions.json 이 세션 폴더에 쌓이고 "
        "만기(horizon_days)가 지나면 자동으로 채점됩니다.",
        "- 표본 부족 — 정확도 통계는 참고 불가 단계입니다.",
        "",
        "## 안내",
        "- 분석가(Cowork)가 매일 세션 폴더에 predictions.json 을 저장하면, 이 스크립트가 "
        "output/ 및 output/_archive/ 를 스캔해 만기 도달분을 사후 채점합니다.",
        "",
    ]
    _atomic_write(SCORECARD_PATH, "\n".join(L))


from common import atomic_write_text as _cm_atomic_write  # common.py 통합


def _atomic_write(path, text):
    try:
        _cm_atomic_write(path, text)
        return True
    except Exception as e:
        log.warning(f"[acc] 파일 저장 실패 ({path}): {type(e).__name__}: {e}")
        return False


# =====================================================================
# 6. main
# =====================================================================
def main():
    log.info("[acc] accuracy_tracker 시작")

    if not FDR_AVAILABLE:
        log.warning("[acc] FinanceDataReader 사용 불가 — 시세 조회 불가. "
                    "채점 없이 안내 scorecard 만 생성합니다.")
    if not PANDAS_AVAILABLE:
        log.warning("[acc] pandas 사용 불가 — 시세 처리 제한.")

    try:
        preds = load_predictions()
    except Exception as e:
        log.warning(f"[acc] predictions 스캔 예외: {type(e).__name__}: {e}")
        preds = []

    if not preds:
        log.info("[acc] 아직 채점할 예측 없음 (predictions.json 미발견) — "
                 "안내용 scorecard 생성")
        write_empty_scorecard("아직 채점할 예측이 없습니다 (predictions.json 미발견).")
        log.info(f"[acc] scorecard 작성: {SCORECARD_PATH}")
        log.info("[acc] 완료 (예측 0건)")
        return

    log.info(f"[acc] predictions {len(preds)}개 로드 (날짜 {preds[0].get('date')} ~ "
             f"{preds[-1].get('date')})")

    logdata = load_log()
    before = len(logdata["entries"])

    try:
        n_added = grade_all(preds, logdata)
    except Exception as e:
        log.warning(f"[acc] 채점 루프 예외: {type(e).__name__}: {e}")
        n_added = 0

    # ★v11.6: alpha 결측 백필(지수 다리만 재계산 — 확정 필드 불변)
    try:
        n_backfilled = backfill_missing_alpha(logdata)
        if n_backfilled:
            log.info(f"[acc] alpha 백필 {n_backfilled}건 (지수 다리 일시 실패분)")
    except Exception as e:
        log.warning(f"[acc] alpha 백필 예외: {type(e).__name__}: {e}")

    save_log(logdata)
    total = len(logdata["entries"])
    log.info(f"[acc] 채점 완료 — 누적 {total}건 (이번 추가 {n_added}건, 기존 {before}건)")

    try:
        agg = aggregate(logdata["entries"])
        # ★v11.6: 무채점 채널 가시화 — 파생(채점 다리 미구현)·미채점 잔존(정지·상폐 의심)
        try:
            agg["n_deriv_unscored"] = sum(len(p.get("derivatives") or []) for p in preds)
            agg["n_stalled"] = count_stalled(preds, logdata)
        except Exception:
            agg["n_deriv_unscored"] = agg["n_stalled"] = None
        agg["missing_pred_days"] = missing_pred_days(preds)   # #A45 — 발행분 전체 기준(만기 무관)
        text = build_scorecard(agg, total, n_added)
        _atomic_write(SCORECARD_PATH, text)
        log.info(f"[acc] scorecard 작성: {SCORECARD_PATH} "
                 f"(최근 {agg['n_pred_dates']} 예측일)")
    except Exception as e:
        log.warning(f"[acc] scorecard 작성 예외: {type(e).__name__}: {e}")
        # 그래도 안내용이라도 남긴다
        try:
            write_empty_scorecard("scorecard 집계 중 오류 — 로그는 정상 누적됨.")
        except Exception:
            pass

    log.info("[acc] 완료")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # 어떤 경우에도 exit 0 (제약)
        log.warning(f"[acc] 최상위 예외 흡수: {type(e).__name__}: {e}")
    finally:
        sys.exit(0)
