#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
short_collect.py — 공매도 잔고/추세 수집기 (T2: 차익실현/되돌림 압력 보강)

[목적]
  공매도 잔고(비중)와 그 추세는 '하락 베팅·되돌림 압력'의 직접 신호다. 빚투(신용)·차익실현과
  함께 단기 매물 위험을 키운다. watch_tickers 풀에 대해 KRX 공매도 잔고를 수집해 오늘자
  세션 폴더에 short.json 으로 저장한다(분석가가 [3-차익실현] 보강 판단에 사용).

[수집] pykrx(KRX 공개데이터, 키/로그인 불필요. 단 KRX 공매도 잔고는 T+1~2 지연 공시)
  - short_balance_ratio : 공매도 잔고 비중(%)  (= 공매도잔고/상장주식수)
  - balance_change_10d  : 최근 약 10거래일 잔고 증감률(%)  (+면 공매도 누적↑ = 압력↑)
  - trend               : 증가 / 감소 / 혼조
  → short_pressure_score(0~100) + label(높음/주의/낮음) + signals

  ※ 신용융자 잔고(빚투)는 pykrx 미제공이라 여기서 수집하지 않는다. 필요하면 KRX 정보데이터
    시스템(별도 소스)으로 추후 보강. (관련 매물 위험은 disclosures/overheat 와 함께 본다.)

[설계] 독립 실행, 기존 파일 무수정, 종목별 try/except, ASCII 콘솔 태그([short]),
  UTF-8 IO, json ensure_ascii=False, 원자적 저장, 부분 실패해도 exit 0.

[사용법]
  python short_collect.py                      # 전체 → 오늘 세션/short.json
  python short_collect.py --tickers 005930 --out x.json
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
log = logging.getLogger("short")

MAX_WORKERS = 4         # KRX rate-limit 고려 보수적
LOOKBACK_DAYS = 40

# 이 환경의 pykrx 는 import 시점에 KRX_ID/KRX_PW 환경변수로 KRX 에 자동 로그인하는 포크다
# (force_analysis 와 동일). 따라서 krx_account.txt 값을 'pykrx import 전에' os.environ 에 주입한다.
def _load_krx_account_into_env():
    path = os.path.join(HERE, "krx_account.txt")
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip().lower()
                val = val.strip()
                if key in ("krx_id", "id") and not os.getenv("KRX_ID"):
                    os.environ["KRX_ID"] = val
                elif key in ("krx_pw", "pw", "password") and not os.getenv("KRX_PW"):
                    os.environ["KRX_PW"] = val
    except Exception:
        pass


_load_krx_account_into_env()
try:
    from common import suppress_stdout as _suppress_stdout
    with _suppress_stdout():                 # pykrx 포크의 import-시 계정 ID 콘솔 노출 억제
        from pykrx import stock as _krx
except Exception:
    _krx = None


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
                if code.isdigit() and len(code) == 6:
                    name = line.split("#", 1)[1].strip() if "#" in line else ""
                    out.append((code, name))
    except Exception as e:
        log.warning("[short] watch_tickers 읽기 실패: %s", e)
    return out


def _col(df, *names):
    for n in names:
        if n in df.columns:
            return df[n]
    return None


_SHORT_ASOF_CACHE = {}


# ★공매도 잔고 '공표지연'(회고 A25, 2026-07-26): KRX 잔고는 거래일 기준 T+2~3 뒤에 공표된다.
#   추천일 아침(06:30)에 실제로 공개돼 있던 값은 asof-1 거래일이 아니라 asof-3 거래일 근처다.
#   거래일 기준으로만 컷하면 '아직 공개되지 않은 잔고'가 진입 피처에 들어간다(A22 와 같은 룩어헤드).
PUB_LAG_ROWS = 3          # 보수적 기본값 — 실측 지연 3~4영업일을 덮는다


def get_short_asof(ticker, asof, pub_lag_rows=PUB_LAG_ROWS):
    """진입시점(asof, YYYYMMDD)에 **실제로 공개돼 있던** 공매도 잔고비중(%) + 직전 약10거래일 증감률(%).

    retro_label 진입 피처용 — 만기행의 short feature 를 추천일 기준으로 소급 채운다. 실패 시 빈 dict.
    pub_lag_rows: 공표지연 보정(최근 N개 거래일 행 제외). 0 이면 지연 보정 없음(옛 동작).
    ※ 아침 수집 경로(compute_short)는 '최신 공표분'이 맞으므로 이 함수와 무관하다 — 손대지 않는다.
    """
    if _krx is None:
        return {}
    # 캐시 키에 지연 파라미터 포함 — 안 하면 첫 호출 결과가 다른 설정으로 전파된다.
    key = (str(ticker), str(asof), int(pub_lag_rows or 0))
    if key in _SHORT_ASOF_CACHE:
        return _SHORT_ASOF_CACHE[key]
    out = {}
    try:
        cut = datetime.strptime(str(asof), "%Y%m%d")
        bgn = (cut - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
        df = _krx.get_shorting_balance_by_date(bgn, str(asof), ticker)
        # 1차: 추천일 당일·이후 행 제거(달력 -1 은 휴일에 부정확하므로 인덱스로 컷).
        if df is not None and len(df) > 0:
            try:
                df = df[df.index < cut]
            except Exception:
                pass
        # 2차: 공표지연 보정 — 최근 N행은 추천 시점에 아직 공개 전이었다.
        #      단 남는 행이 2개 미만이면 증감률 계산이 불가하므로 보정을 포기한다(결측보다 낫다).
        if df is not None and len(df) > (pub_lag_rows or 0) + 1 and pub_lag_rows:
            try:
                df = df.iloc[:-int(pub_lag_rows)]
            except Exception:
                pass
        if df is not None and len(df) > 0:
            ratio = _col(df, "비중")
            bal = _col(df, "공매도잔고")
            lr = round(float(ratio.iloc[-1]), 2) if ratio is not None else None
            chg10 = None
            if bal is not None and len(bal) >= 2:
                n = min(10, len(bal) - 1)
                bp = float(bal.iloc[-1 - n])
                if bp > 0:
                    chg10 = round((float(bal.iloc[-1]) / bp - 1.0) * 100, 1)
            out = {"pre_short_balance_ratio": lr, "pre_short_change_10d": chg10}
    except Exception:
        out = {}
    _SHORT_ASOF_CACHE[key] = out
    return out


def compute_short(code, name):
    if _krx is None:
        return {"ticker": code, "name": name, "source": "none",
                "notes": ["pykrx 미설치"]}
    try:
        end = datetime.now().strftime("%Y%m%d")
        bgn = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
        df = _krx.get_shorting_balance_by_date(bgn, end, code)
        if df is None or len(df) == 0:
            return {"ticker": code, "name": name, "source": "none",
                    "notes": ["공매도 잔고 데이터 없음(지연/비대상)"]}
        ratio = _col(df, "비중")
        bal = _col(df, "공매도잔고")
        latest_ratio = round(float(ratio.iloc[-1]), 2) if ratio is not None else None
        # 10거래일 전 대비 잔고 증감률
        chg10 = None
        trend = None
        if bal is not None and len(bal) >= 2:
            n = min(10, len(bal) - 1)
            b_now = float(bal.iloc[-1]); b_prev = float(bal.iloc[-1 - n])
            if b_prev > 0:
                chg10 = round((b_now / b_prev - 1.0) * 100, 1)
                trend = "증가" if chg10 > 10 else ("감소" if chg10 < -10 else "혼조")

        score = 0
        sig = []
        if latest_ratio is not None:
            if latest_ratio >= 5:
                score += 25; sig.append("공매도잔고비중 %.1f%%(높음)" % latest_ratio)
            elif latest_ratio >= 3:
                score += 15; sig.append("공매도잔고비중 %.1f%%" % latest_ratio)
            elif latest_ratio >= 1.5:
                score += 8; sig.append("공매도잔고비중 %.1f%%" % latest_ratio)
        # 잔고 비중이 유의미할 때만(>=0.5%) 증감 추세를 점수화 — 0%대 미미한 잔고의 %변동 노이즈 제외
        if chg10 is not None and latest_ratio is not None and latest_ratio >= 0.5:
            if chg10 >= 30:
                score += 20; sig.append("잔고 10일 +%.0f%% 급증" % chg10)
            elif chg10 >= 10:
                score += 10; sig.append("잔고 10일 +%.0f%% 증가" % chg10)
        score = min(100, score)
        label = "높음" if score >= 35 else ("주의" if score >= 15 else "낮음")
        return {"ticker": code, "name": name, "source": "krx",
                "short_balance_ratio": latest_ratio,
                "balance_change_10d": chg10, "trend": trend,
                "short_pressure_score": score, "short_pressure_label": label,
                "signals": sig, "asof": str(df.index[-1])[:10]}
    except Exception as e:
        return {"ticker": code, "name": name, "source": "none",
                "notes": ["%s: %s" % (type(e).__name__, e)]}


from common import save_json_atomic as _save_json_atomic  # 원자적 JSON 저장(common.py 통합)


def _today_latest_session():
    """H-3: common.resolve_session 위임 - 자정 경계 완화(6h 폴백) + 복제 제거."""
    from common import resolve_session
    return resolve_session(OUTPUT_DIR)


def main():
    ap = argparse.ArgumentParser(description="공매도 잔고/추세 → short.json")
    ap.add_argument("--tickers", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if _krx is None:
        log.warning("[short] pykrx 미설치 — 공매도 수집 불가")

    universe = load_universe()
    if args.tickers:
        want = [t.strip() for t in args.tickers.split(",") if t.strip()]
        nm = {c: n for c, n in universe}
        universe = [(c, nm.get(c, "")) for c in want]
    if args.limit > 0:
        universe = universe[:args.limit]

    log.info("[short] 시작 — 종목 %d개", len(universe))
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(compute_short, c, n): c for c, n in universe}
        done = 0
        for fut in as_completed(futs):
            done += 1
            r = fut.result()
            results[r["ticker"]] = r
            if r.get("short_pressure_score"):
                log.info("[short] (%d/%d) %s ratio=%s score=%s %s", done, len(universe),
                         r["ticker"], r.get("short_balance_ratio"),
                         r.get("short_pressure_score"), r.get("short_pressure_label"))

    ordered = [results[c] for c, _ in universe if c in results]
    hot = sorted([r for r in ordered if r.get("short_pressure_score")],
                 key=lambda r: r["short_pressure_score"], reverse=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "what": "공매도 잔고/추세 — 하락 베팅·되돌림 압력(차익실현 위험 보강). KRX 공시는 T+1~2 지연.",
        "score_guide": "short_pressure_score 높을수록 공매도(하락베팅) 압력 큼. 잔고 급증은 단기 매물 위험.",
        "universe": len(universe),
        "top_short_pressure": [{"ticker": r["ticker"], "name": r["name"],
                                "ratio": r.get("short_balance_ratio"),
                                "score": r["short_pressure_score"]} for r in hot[:10]],
        "tickers": ordered,
        "disclaimer": "KRX 공매도 공개데이터(지연). 투자자문이 아니다.",
    }

    out_path = os.path.abspath(args.out) if args.out else None
    if not out_path:
        sess = _today_latest_session()
        out_path = os.path.join(sess, "short.json") if sess else os.path.join(HERE, "short.json")
    try:
        _save_json_atomic(out_path, payload)
        log.info("[short] 저장 완료: %s (압력 감지 %d종목)", out_path, len(hot))
    except Exception as e:
        log.warning("[short] 저장 실패: %s", e)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        log.warning("[short] 치명적 예외(무시): %s: %s", type(e).__name__, e)
        sys.exit(0)
