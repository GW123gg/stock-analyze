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
    적중 = (긍정 태그 픽이고 수익률>0) 또는 (숏이고 수익률<0)
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

# market_call neutral 판정 임계(|구간 등락률| < 0.5% 면 횡보로 간주)
NEUTRAL_BAND_PCT = 0.5

# scorecard 요약 대상 최근 예측일 수
RECENT_DAYS = 20

# calibration 구간 (하한 미포함, 상한 포함). 단 첫 구간은 0 포함.
CALIB_BUCKETS = [
    ("[0.00-0.50]", 0.0, 0.50),
    ("(0.50-0.65]", 0.50, 0.65),
    ("(0.65-0.80]", 0.65, 0.80),
    ("(0.80-1.00]", 0.80, 1.00),
]

# 긍정(상승 기대) 픽 태그 — 이들은 수익률>0 일 때 적중
POSITIVE_TAGS = {"단기스윙", "장투가능", "장전선취매"}

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


def load_predictions():
    """
    모든 predictions.json 로드. (date, session) 기준 중복 제거 — 같은 날짜는
    파일 mtime 이 가장 최신인 1개만 채택.
    Returns: list[dict] (predictions, 각 dict 에 '_src' 경로 주입), 날짜 오름차순.
    """
    by_date = {}  # date -> (mtime, pred_dict)
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
            by_date[date] = (mtime, data)
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


def _index_return(symbol, base_date, horizon_days):
    """base_date(0번째 거래일) 종가 대비 T+horizon 종가 수익률(%). 실패 시 None."""
    c0, _ = _close_on_or_after(symbol, base_date)
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

    ret_pct = (close_h / entry_ref - 1.0) * 100.0

    # 같은 구간 코스피 수익률 → alpha
    kospi_ret, _ = _index_return(KOSPI_SYMBOL, base_date, horizon)
    alpha = (ret_pct - kospi_ret) if kospi_ret is not None else None

    tag = str(item.get("tag") or "").strip()
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
        "kospi_return_pct": round(kospi_ret, 2) if kospi_ret is not None else None,
        "alpha_pct": round(alpha, 2) if alpha is not None else None,
        "conviction": conv,
        "hit": bool(hit),
        "graded_at": datetime.now().isoformat(),
    }


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

    if direction == "neutral":
        hit = abs(idx_ret) < NEUTRAL_BAND_PCT
    elif direction == "up":
        hit = idx_ret > 0
    else:  # down
        hit = idx_ret < 0

    conv = _safe_float(call.get("conviction"))
    return {
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
                key = _entry_key("market_" + market_name, pred_date,
                                 str(call.get("dir")), horizon)
                if key in existing:
                    continue
                try:
                    res = grade_market_call(pred_date, market_name, call, horizon)
                except Exception as e:
                    log.warning(f"[acc] market 채점 예외 ({market_name}@{pred_date}): "
                                f"{type(e).__name__}: {e}")
                    res = None
                if res:
                    logdata["entries"].append(res)
                    existing[key] = res
                    added += 1

    return added


# =====================================================================
# 5. 집계 & scorecard.md
# =====================================================================
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

    agg = {
        "n_pred_dates": len(recent),
        "date_range": (min(recent), max(recent)) if recent else (None, None),
        "market": {},      # horizon -> {hit, total}
        "picks": {"total": 0, "hit": 0, "ret_sum": 0.0, "ret_n": 0,
                  "alpha_sum": 0.0, "alpha_n": 0},
        "shorts": {"total": 0, "hit": 0},
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
            m = agg["market"].setdefault(h, {"total": 0, "hit": 0})
            m["total"] += 1
            if hit:
                m["hit"] += 1

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
            tag = e.get("tag") or "(태그없음)"
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
            s = agg["shorts"]
            s["total"] += 1
            if hit:
                s["hit"] += 1

    # 예시 종목(최근 픽/숏 중 수익률 극단 몇 개)
    scored = [e for e in rset if e.get("kind") in ("pick", "short")
              and e.get("return_pct") is not None]
    scored_sorted = sorted(scored, key=lambda e: e.get("return_pct"), reverse=True)
    hits = [e for e in scored_sorted if e.get("hit")]
    misses = [e for e in scored_sorted if not e.get("hit")]
    agg["examples_hit"] = hits[:3]
    # 오답은 손실 큰 순(픽) / 가장 안 맞은 순
    agg["examples_miss"] = sorted(misses, key=lambda e: e.get("return_pct"))[:3]
    return agg


def build_recommendations(agg):
    """집계 → 한국어 권고 2~4줄(휴리스틱 자동 생성). 이모지 금지."""
    recs = []
    n = agg["n_pred_dates"]

    # 표본 부족
    if n < RECENT_DAYS:
        recs.append(f"표본 N={n} (< {RECENT_DAYS}) — 통계 신뢰도 낮음, 참고용으로만 활용.")

    # 시장 방향(약세콜 포함) — T+5 우선, 없으면 T+1
    mkt = agg["market"]
    for h in (5, 1):
        if h in mkt and mkt[h]["total"] >= 3:
            rate = _pct(mkt[h]["hit"], mkt[h]["total"])
            if rate is not None and rate < 45:
                recs.append(f"시장 방향(T+{h}) 적중률 {rate:.0f}% 낮음 "
                            f"-> 방향성 콜 신중, conviction 하향 권장.")
            elif rate is not None and rate >= 65:
                recs.append(f"시장 방향(T+{h}) 적중률 {rate:.0f}% 양호 -> 현 판단 유지.")
            break

    # 태그별 성과
    for tag, t in sorted(agg["by_tag"].items(), key=lambda kv: -kv[1]["total"]):
        if t["total"] < 3:
            continue
        avg_ret = _pct_avg(t["ret_sum"], t["ret_n"])
        rate = _pct(t["hit"], t["total"])
        if avg_ret is None:
            continue
        if avg_ret >= 1.0 and (rate or 0) >= 55:
            recs.append(f"[{tag}] 평균 {avg_ret:+.1f}%, 적중 {rate:.0f}% 양호 -> 유지·비중 확대 가능.")
        elif avg_ret <= -1.0 or (rate is not None and rate < 40):
            recs.append(f"[{tag}] 평균 {avg_ret:+.1f}%, 적중 {_fmt_rate(rate)} 부진 "
                        f"-> 해당 태그 선별 강화 또는 비중 축소.")
        if len(recs) >= 4:
            break

    # calibration 과신 진단
    if len(recs) < 4:
        for label, lo, hi in reversed(CALIB_BUCKETS):
            c = agg["calib"].get(label, {})
            if c.get("total", 0) >= 3:
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
            L.append(f"- T+{h}: 적중 {m['hit']}/{m['total']} ({_fmt_rate(rate)})")
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
        L.append(f"- 채점 픽 수: {p['total']}건")
        L.append(f"- 적중률: {p['hit']}/{p['total']} ({_fmt_rate(rate)})")
        L.append(f"- 평균 수익률: {_fmt_pct(avg_ret)}")
        L.append(f"- 평균 alpha(코스피 대비): {_fmt_pct(avg_alpha)}")
    s = agg["shorts"]
    if s["total"] > 0:
        srate = _pct(s["hit"], s["total"])
        L.append(f"- 숏 적중률: {s['hit']}/{s['total']} ({_fmt_rate(srate)})")
    L.append("")

    # 태그별
    L.append("## 3. 태그별 성과 분해")
    if not agg["by_tag"]:
        L.append("- (태그별 데이터 없음)")
    else:
        L.append("| 태그 | 건수 | 적중률 | 평균수익률 | 평균alpha |")
        L.append("|---|---|---|---|---|")
        for tag, t in sorted(agg["by_tag"].items(), key=lambda kv: -kv[1]["total"]):
            rate = _pct(t["hit"], t["total"])
            ar = _pct_avg(t["ret_sum"], t["ret_n"])
            aa = _pct_avg(t["alpha_sum"], t["alpha_n"])
            L.append(f"| [{tag}] | {t['total']} | {_fmt_rate(rate)} | "
                     f"{_fmt_pct(ar)} | {_fmt_pct(aa)} |")
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
                L.append(f"- [{e.get('tag')}] {e.get('name')}({e.get('ticker')}) "
                         f"{e.get('pred_date')} T+{e.get('horizon')}: "
                         f"수익률 {_fmt_pct(e.get('return_pct'))}, "
                         f"alpha {_fmt_pct(e.get('alpha_pct'))}")
        if em:
            L.append("오답 사례:")
            for e in em:
                L.append(f"- [{e.get('tag')}] {e.get('name')}({e.get('ticker')}) "
                         f"{e.get('pred_date')} T+{e.get('horizon')}: "
                         f"수익률 {_fmt_pct(e.get('return_pct'))}, "
                         f"alpha {_fmt_pct(e.get('alpha_pct'))}")
    L.append("")
    L.append("---")
    L.append("주: 공개데이터(FinanceDataReader) 기반 사후 채점이며, entry_ref 는 "
             "예측 시점 값 그대로 사용(룩어헤드 없음). 만기 미도달 건은 자동 보류 후 "
             "다음 실행에서 재시도됩니다.")
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


def _atomic_write(path, text):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
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

    save_log(logdata)
    total = len(logdata["entries"])
    log.info(f"[acc] 채점 완료 — 누적 {total}건 (이번 추가 {n_added}건, 기존 {before}건)")

    try:
        agg = aggregate(logdata["entries"])
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
