#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
overheat_collect.py — '차익실현/되돌림(과열) 압력' 지표 수집기 (A)

[목적]
  좋은 기업이라도 '차익실현 매물'은 펀더멘털이 아니라 수급·심리·과열에서 나온다.
  이 모듈은 watch_tickers 풀의 가격 시계열로 '과열/되돌림 압력' 신호를 정량화해
  오늘자 세션 폴더에 overheat.json 으로 저장한다. 분석(Cowork)이 [3] 선반영/되돌림
  판단에 활용한다(force_score 의 RSI/OBV 를 보완 — 이격도·연속상승·52주고가·급등률 추가).

[산출 지표] (FinanceDataReader 일봉 약 1년)
  - 이격도(disparity) : 종가/MA20*100, 종가/MA60*100  (>110~120% = 단기 과열·되돌림 위험)
  - 연속상승일 up_streak                                (길수록 차익실현 빌미)
  - 52주 고가 대비 위치 dist_52w_high_pct               (0% 근처 = 신고가권, 매물대·심리적 차익실현)
  - 20일 수익률 ret_20d_pct                            (급등 폭 — 차익실현 연료)
  - RSI(14)                                            (70/80+ 과열)
  - OBV 다이버전스 obv_divergence                       (주가↑인데 OBV 정체/하락 = 분배 신호)
  - 거래량비율 vol_ratio (최근5일 평균/60일 평균)
  → 종합 overheat_score(0~100) + overheat_label(정상/주의/과열) + signals(근거 목록)

[설계 원칙] (dart_collect / market_collect 와 동일)
  - 종목별 try/except, 콘솔 print 는 ASCII 태그([overheat])만(이모지 금지),
    파일 IO UTF-8, json ensure_ascii=False, 원자적 저장(.tmp→replace). 부분 실패해도 exit 0.
  - 기존 파일 무수정. 독립 실행. overheat.json 만 남긴다.

[사용법]
  python overheat_collect.py                       # watch_tickers 전체 → 오늘 세션/overheat.json
  python overheat_collect.py --tickers 005930,000660   # 특정 종목(테스트)
  python overheat_collect.py --limit 5 --out x.json    # 앞 N개 / 출력경로 지정(테스트)
"""
import os
import sys
import json
import argparse
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
TICKERS_FILE = os.path.join(HERE, "watch_tickers.txt")

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("overheat")

MAX_WORKERS = 6
LOOKBACK_DAYS = 420     # 52주(약 252거래일) 커버 위해 넉넉히

try:
    import FinanceDataReader as fdr
except Exception:
    fdr = None
try:
    import numpy as np
    import pandas as pd
except Exception:
    np = None
    pd = None


def load_universe():
    out = []
    if not os.path.isfile(TICKERS_FILE):
        return out
    try:
        with open(TICKERS_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                code = line.split("#")[0].strip()
                if not (code.isdigit() and len(code) == 6):
                    continue
                name = line.split("#", 1)[1].strip() if "#" in line else ""
                out.append((code, name))
    except Exception as e:
        log.warning("[overheat] watch_tickers 읽기 실패: %s", e)
    return out


def _rsi(closes, period=14):
    if np is None or len(closes) < period + 1:
        return None
    diff = np.diff(closes)
    gain = np.where(diff > 0, diff, 0.0)
    loss = np.where(diff < 0, -diff, 0.0)
    ag = gain[-period:].mean()
    al = loss[-period:].mean()
    if al == 0:
        return 100.0
    rs = ag / al
    return round(100.0 - 100.0 / (1.0 + rs), 1)


def _obv(closes, vols):
    if np is None or len(closes) < 2:
        return None
    obv = np.zeros(len(closes))
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv[i] = obv[i - 1] + vols[i]
        elif closes[i] < closes[i - 1]:
            obv[i] = obv[i - 1] - vols[i]
        else:
            obv[i] = obv[i - 1]
    return obv


def compute_overheat(code, name):
    """단일 종목 과열/되돌림 압력 지표. 실패 시 source=none."""
    if fdr is None or np is None:
        return {"ticker": code, "name": name, "source": "none",
                "notes": ["FinanceDataReader/numpy 미설치"]}
    try:
        d0 = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        df = fdr.DataReader(code, d0)
        if df is None or len(df) < 30:
            return {"ticker": code, "name": name, "source": "none",
                    "notes": ["가격데이터 부족"]}
        close = df["Close"].astype(float).values
        high = df["High"].astype(float).values
        vol = df["Volume"].astype(float).values
        c = float(close[-1])

        ma20 = float(np.mean(close[-20:])) if len(close) >= 20 else None
        ma60 = float(np.mean(close[-60:])) if len(close) >= 60 else None
        disp20 = round(c / ma20 * 100, 1) if ma20 else None
        disp60 = round(c / ma60 * 100, 1) if ma60 else None

        # 연속 상승일(끝에서부터)
        up_streak = 0
        for i in range(len(close) - 1, 0, -1):
            if close[i] > close[i - 1]:
                up_streak += 1
            else:
                break

        # 52주 고가/저가 대비
        hi52 = float(np.max(high[-252:])) if len(high) >= 1 else None
        dist_52w_high_pct = round((c / hi52 - 1.0) * 100, 1) if hi52 else None

        ret20 = round((c / float(close[-21]) - 1.0) * 100, 1) if len(close) >= 21 else None
        rsi14 = _rsi(close)

        # 거래량비율(최근5/최근60)
        v5 = float(np.mean(vol[-5:])) if len(vol) >= 5 else None
        v60 = float(np.mean(vol[-60:])) if len(vol) >= 60 else None
        vol_ratio = round(v5 / v60, 2) if (v5 and v60) else None

        # OBV 다이버전스: 최근 20일 주가는 상승(+)인데 OBV 가 정체/하락이면 분배 신호
        obv = _obv(close, vol)
        obv_div = False
        if obv is not None and len(obv) >= 21 and ret20 is not None:
            obv_now, obv_prev = obv[-1], obv[-21]
            if ret20 > 3 and obv_now <= obv_prev:
                obv_div = True

        # 종합 점수(0~100) + 근거
        score = 0
        sig = []
        if disp20 is not None:
            if disp20 >= 120:
                score += 25; sig.append("MA20 이격도 %.0f%%(매우 과열)" % disp20)
            elif disp20 >= 110:
                score += 15; sig.append("MA20 이격도 %.0f%%(과열)" % disp20)
        if rsi14 is not None:
            if rsi14 >= 80:
                score += 25; sig.append("RSI %.0f(극과열)" % rsi14)
            elif rsi14 >= 70:
                score += 15; sig.append("RSI %.0f(과열)" % rsi14)
        if up_streak >= 8:
            score += 20; sig.append("연속상승 %d일" % up_streak)
        elif up_streak >= 5:
            score += 10; sig.append("연속상승 %d일" % up_streak)
        if dist_52w_high_pct is not None and dist_52w_high_pct >= -3:
            score += 15; sig.append("52주 신고가권(고가대비 %+.1f%%)" % dist_52w_high_pct)
        if ret20 is not None:
            if ret20 >= 50:
                score += 25; sig.append("20일 +%.0f%% 급등" % ret20)
            elif ret20 >= 30:
                score += 15; sig.append("20일 +%.0f%% 상승" % ret20)
        if obv_div:
            score += 15; sig.append("OBV 다이버전스(주가↑ OBV 정체/하락=분배)")
        score = min(100, score)
        label = "과열" if score >= 60 else ("주의" if score >= 30 else "정상")

        return {
            "ticker": code, "name": name, "source": "fdr",
            "close": round(c, 2),
            "ma20": round(ma20, 2) if ma20 else None,
            "ma60": round(ma60, 2) if ma60 else None,
            "disparity20": disp20, "disparity60": disp60,
            "up_streak": up_streak,
            "dist_52w_high_pct": dist_52w_high_pct,
            "ret_20d_pct": ret20,
            "rsi14": rsi14,
            "vol_ratio": vol_ratio,
            "obv_divergence": obv_div,
            "overheat_score": score,
            "overheat_label": label,
            "signals": sig,
        }
    except Exception as e:
        return {"ticker": code, "name": name, "source": "none",
                "notes": ["%s: %s" % (type(e).__name__, e)]}


def _today_latest_session():
    """H-3: common.resolve_session 위임 — 자정 경계 완화(6h 폴백) + 11곳 복제 제거."""
    from common import resolve_session
    return resolve_session(OUTPUT_DIR)


def _save_atomic(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="과열/되돌림 압력 지표 → overheat.json")
    ap.add_argument("--tickers", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    universe = load_universe()
    if args.tickers:
        want = [t.strip() for t in args.tickers.split(",") if t.strip()]
        nm = {c: n for c, n in universe}
        universe = [(c, nm.get(c, "")) for c in want]
    if args.limit > 0:
        universe = universe[:args.limit]

    log.info("[overheat] 시작 — 종목 %d개", len(universe))
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(compute_overheat, c, n): c for c, n in universe}
        done = 0
        for fut in as_completed(futs):
            done += 1
            r = fut.result()
            results[r["ticker"]] = r
            log.info("[overheat] (%d/%d) %s score=%s %s", done, len(universe),
                     r["ticker"], r.get("overheat_score", "-"), r.get("overheat_label", ""))

    ordered = [results[c] for c, _ in universe if c in results]
    # 과열 상위 요약
    hot = sorted([r for r in ordered if r.get("overheat_score") is not None],
                 key=lambda r: r["overheat_score"], reverse=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "what": "차익실현/되돌림(과열) 압력 지표 — force_score 의 RSI/OBV 를 보완",
        "score_guide": "overheat_score 0~100: >=60 과열(차익실현 위험 큼), 30~60 주의, <30 정상",
        "fields": {
            "disparity20/60": "종가/MA20·MA60*100 (>110~120 과열)",
            "up_streak": "연속 상승 거래일수",
            "dist_52w_high_pct": "52주 고가 대비 위치(%, 0 근처=신고가권)",
            "ret_20d_pct": "최근 20거래일 수익률(%)",
            "obv_divergence": "주가 상승 중 OBV 정체/하락(분배 신호)",
        },
        "universe": len(universe),
        "top_overheated": [{"ticker": r["ticker"], "name": r["name"],
                            "score": r["overheat_score"], "label": r["overheat_label"]}
                           for r in hot[:10]],
        "tickers": ordered,
        "disclaimer": "공개 가격데이터 기반 추정 신호이며 투자자문이 아니다.",
    }

    out_path = os.path.abspath(args.out) if args.out else None
    if not out_path:
        sess = _today_latest_session()
        out_path = os.path.join(sess, "overheat.json") if sess else os.path.join(HERE, "overheat.json")
    try:
        _save_atomic(out_path, payload)
    except Exception as e:
        log.warning("[overheat] 저장 실패(%s) → BASE_DIR", e)
        out_path = os.path.join(HERE, "overheat.json")
        _save_atomic(out_path, payload)
    log.info("[overheat] 저장 완료: %s", out_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        log.warning("[overheat] 치명적 예외(무시): %s: %s", type(e).__name__, e)
        sys.exit(0)
