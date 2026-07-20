#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
flow_collect.py — 투자자별 순매수(개인/기관/외국인) 수집·집계 (KRX/pykrx, krx_account.txt 로그인)

[두 용도]
  1) 회고용 집계: get_investor_flow(ticker, begin, end) — 보유기간 동안 누가 사고 팔았나(retro_label).
  2) 추천시점 수집(신규, --collect): watch_tickers 의 외국인/기관/개인 5d·20d 순매수 + '외국인 연속
     순매도 일수' + 시장(KOSPI/KOSDAQ) 연속순매도·risk-off 신호 → fsc 같은 세션 파일 flow_data.json.
     → force_scores(KRX 수급) 가 자주 결측되는 문제(회고 17일 반복 지적)를 보강하는 '제2 수급원'.

[데이터] pykrx.stock.get_market_trading_value_by_date(begin, end, 종목/시장)
  - 일별 행, 컬럼=[기관합계, 기타법인, 개인, 외국인합계, 전체] (원). KRX 로그인 필요.
  - 외국인합계<0 인 날이 끝에서 몇 일 연속인지 = '외국인 연속 순매도 일수'(F1/F3 핵심 매크로 신호).

[설계] 독립 모듈, 종목별 try/except, ASCII 태그([flow]), 캐시, pykrx 실패 시 graceful(None).
  단위: 억원(_eok) / 조원(_jo). 비밀키 미취급.

[사용법]
  python flow_collect.py --collect          # 전체 → 오늘 세션/flow_data.json
  python flow_collect.py --check
  python flow_collect.py --tickers 005930 --out x.json
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
log = logging.getLogger("flow")

MAX_WORKERS = 4
LOOKBACK_DAYS = 38   # 20거래일 + 여유


# pykrx 는 import 시점에 KRX_ID/KRX_PW 로 자동 로그인(force_analysis/short_collect 동일).
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

_FLOW_CACHE = {}   # 회고 집계용
_DAILY_CACHE = {}  # 일별 df 캐시


# =====================================================================
# 회고용: 보유기간 동안 투자자별 순매수(억원)
# =====================================================================
def _net_eok(df, names):
    if df is None or "순매수" not in getattr(df, "columns", []):
        return None
    for n in names:
        if n in df.index:
            try:
                return int(round(float(df.loc[n, "순매수"]) / 1e8))
            except Exception:
                return None
    return None


def get_investor_flow(ticker, begin, end):
    """[begin,end] 동안 외국인/기관/개인 순매수(억원). retro_label 용. 실패 시 모두 None."""
    out = {"flow_foreign_eok": None, "flow_inst_eok": None,
           "flow_indiv_eok": None, "flow_source": "none"}
    if _krx is None:
        out["flow_source"] = "no_pykrx"
        return out
    key = (str(ticker), str(begin), str(end))
    if key in _FLOW_CACHE:
        return _FLOW_CACHE[key]
    try:
        df = _krx.get_market_trading_value_by_investor(begin, end, ticker)
        if df is not None and len(df) > 0:
            out = {"flow_foreign_eok": _net_eok(df, ["외국인"]),
                   "flow_inst_eok": _net_eok(df, ["기관합계", "기관"]),
                   "flow_indiv_eok": _net_eok(df, ["개인"]),
                   "flow_source": "krx"}
    except Exception:
        pass
    _FLOW_CACHE[key] = out
    return out


# =====================================================================
# 추천시점 수집용: 일별 투자자 순매수 → 5d/20d net + 연속순매도일수
# =====================================================================
def _daily_df(code_or_market, days=LOOKBACK_DAYS):
    if _krx is None:
        return None
    key = str(code_or_market)
    if key in _DAILY_CACHE:
        return _DAILY_CACHE[key]
    df = None
    try:
        end = (datetime.now() + timedelta(days=1)).strftime("%Y%m%d")
        begin = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        df = _krx.get_market_trading_value_by_date(begin, end, code_or_market)
        if df is None or len(df) == 0:
            df = None
    except Exception:
        df = None
    _DAILY_CACHE[key] = df
    return df


def _fcol(df):
    for c in ("외국인합계", "외국인"):
        if c in df.columns:
            return c
    return None


def _streak_neg(series):
    """series(일별) 끝에서부터 음수(<0, 순매도)가 몇 일 연속인지."""
    n = 0
    for v in reversed(list(series)):
        try:
            v = float(v)
        except Exception:
            break
        if v < 0:
            n += 1
        else:
            break
    return n


def get_recent_flow(code, name=""):
    """종목의 외국인/기관/개인 5d·20d 순매수(억원) + 외국인 연속순매도일수."""
    df = _daily_df(code)
    if df is None or len(df) == 0:
        return {"ticker": code, "name": name, "source": "none"}
    fcol = _fcol(df)
    icol = "기관합계" if "기관합계" in df.columns else None
    pcol = "개인" if "개인" in df.columns else None

    def neok(col, n):
        if col is None:
            return None
        try:
            return int(round(float(df[col].iloc[-n:].sum()) / 1e8))
        except Exception:
            return None

    return {
        "ticker": code, "name": name, "source": "krx",
        "foreign_net_5d_eok": neok(fcol, 5), "foreign_net_20d_eok": neok(fcol, 20),
        "inst_net_5d_eok": neok(icol, 5), "indiv_net_5d_eok": neok(pcol, 5),
        "foreign_sell_streak": _streak_neg(df[fcol]) if fcol else None,
        "asof": str(df.index[-1])[:10],
    }


_ASOF_CACHE = {}


def get_flow_asof(ticker, asof, lookback=20):
    """진입시점(pred_date) '이전~당일' 구간의 외국인/기관/개인 순매수(억원) 5d/20d + 외국인 연속순매도일수.
    asof: 'YYYYMMDD'(추천일). 룩어헤드 없음 — asof 까지의 데이터만 사용한다(진입 전 큰손 분배 여부 = 예측 피처).
    실패 시 빈 dict."""
    if _krx is None:
        return {}
    key = (str(ticker), str(asof))
    if key in _ASOF_CACHE:
        return _ASOF_CACHE[key]
    out = {}
    try:
        begin = (datetime.strptime(str(asof), "%Y%m%d") - timedelta(days=lookback + 18)).strftime("%Y%m%d")
        df = _krx.get_market_trading_value_by_date(begin, str(asof), ticker)
        if df is not None and len(df) > 0:
            fcol = _fcol(df)
            icol = "기관합계" if "기관합계" in df.columns else None
            pcol = "개인" if "개인" in df.columns else None

            def neok(col, n):
                if col is None:
                    return None
                try:
                    return int(round(float(df[col].iloc[-n:].sum()) / 1e8))
                except Exception:
                    return None

            out = {
                "pre_foreign_5d_eok": neok(fcol, 5), "pre_foreign_20d_eok": neok(fcol, 20),
                "pre_inst_5d_eok": neok(icol, 5), "pre_indiv_5d_eok": neok(pcol, 5),
                "pre_foreign_sell_streak": (_streak_neg(df[fcol]) if fcol else None),
            }
    except Exception:
        out = {}
    _ASOF_CACHE[key] = out
    return out


def get_market_flow(market):
    """시장(KOSPI/KOSDAQ) 외국인 연속순매도일수 + 5d/20d 누적(조원)."""
    df = _daily_df(market)
    if df is None or len(df) == 0:
        return {"market": market, "source": "none"}
    fcol = _fcol(df)
    if not fcol:
        return {"market": market, "source": "none"}

    def njo(n):
        try:
            return round(float(df[fcol].iloc[-n:].sum()) / 1e12, 2)
        except Exception:
            return None

    return {
        "market": market, "source": "krx",
        "foreign_sell_streak": _streak_neg(df[fcol]),
        "foreign_net_5d_jo": njo(5), "foreign_net_20d_jo": njo(20),
        "asof": str(df.index[-1])[:10],
    }


def _risk_off(markets):
    """시장 외국인 수급으로 risk-off 점수(0~100)+라벨+드라이버. PART A 가 환율/breadth 와 결합."""
    # ★KRX 다운 시 markets 가 전부 source='none'(결측)이면 streak/cum20 이 0 으로 뭉개져 '낮음'이라는
    #   거짓 안도가 나온다 → 시장 수급이 전량 결측이면 점수 대신 명시적 결측을 반환한다.
    if not any((m or {}).get("source") == "krx" for m in markets.values()):
        return {"risk_off_score": None, "risk_off_label": "결측(KRX 미수집)", "drivers": [],
                "note": "시장 수급 전량 결측(KRX 다운 등) — score 산출 불가. 낮은 값이 아니라 미상이다."}
    score = 0
    drv = []
    streak = max([m.get("foreign_sell_streak") or 0 for m in markets.values()] or [0])
    cum20 = min([m.get("foreign_net_20d_jo") for m in markets.values()
                 if m.get("foreign_net_20d_jo") is not None] or [0])
    if streak >= 15:
        score += 45; drv.append("외국인 %d일 연속 순매도(대량 분배)" % streak)
    elif streak >= 10:
        score += 30; drv.append("외국인 %d일 연속 순매도" % streak)
    elif streak >= 5:
        score += 15; drv.append("외국인 %d일 연속 순매도" % streak)
    if cum20 <= -10:
        score += 30; drv.append("외국인 20일 누적 %.1f조 순매도" % cum20)
    elif cum20 <= -5:
        score += 18; drv.append("외국인 20일 누적 %.1f조 순매도" % cum20)
    elif cum20 <= -2:
        score += 8; drv.append("외국인 20일 누적 %.1f조 순매도" % cum20)
    score = min(100, score)
    label = "높음" if score >= 45 else ("주의" if score >= 20 else "낮음")
    return {"risk_off_score": score, "risk_off_label": label, "drivers": drv,
            "note": "외국인 수급 기반. 환율 급등·breadth 붕괴(market_context)와 결합해 최종 판단."}


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
        log.warning("[flow] watch_tickers 읽기 실패: %s", e)
    # 최근 추천 종목(watch 풀 밖이어도) 합산 — recommend_track 가 만든 recommended_universe.txt
    try:
        import recommend_track
        have = {c for c, _ in out}
        for c, n in recommend_track.load_recommended_codes():
            if c not in have:
                out.append((c, n)); have.add(c)
    except Exception:
        pass
    return out


from common import save_json_atomic as _save_json_atomic  # 원자적 JSON 저장(common.py 통합)


def _today_latest_session():
    """H-3: common.resolve_session 위임 — 자정 경계 완화(6h 폴백) + 11곳 복제 제거."""
    from common import resolve_session
    return resolve_session(OUTPUT_DIR)


def collect(tickers=None, limit=0, out=""):
    universe = load_universe()
    if tickers:
        want = [t.strip() for t in tickers.split(",") if t.strip()]
        nm = {c: n for c, n in universe}
        universe = [(c, nm.get(c, "")) for c in want]
    if limit > 0:
        universe = universe[:limit]

    log.info("[flow] 추천시점 수급 수집 시작 — 종목 %d개", len(universe))
    if _krx is None:
        log.warning("[flow] pykrx 미설치 — 수집 불가")

    # 시장 단위
    markets = {}
    for m in ("KOSPI", "KOSDAQ"):
        markets[m] = get_market_flow(m)
    risk = _risk_off(markets)
    log.info("[flow] 시장 risk-off=%s(%s) %s", risk["risk_off_label"],
             risk["risk_off_score"], "; ".join(risk["drivers"])[:80])

    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(get_recent_flow, c, n): c for c, n in universe}
        done = 0
        for fut in as_completed(futs):
            done += 1
            r = fut.result()
            results[r["ticker"]] = r
            if done % 15 == 0:
                log.info("[flow] %d/%d", done, len(universe))

    ordered = [results[c] for c, _ in universe if c in results]
    by_src = {}
    for r in ordered:
        by_src[r.get("source", "none")] = by_src.get(r.get("source", "none"), 0) + 1
    # 외국인 순매도 강한 종목(추천 회피 후보)
    sold = sorted([r for r in ordered if r.get("foreign_net_20d_eok") is not None],
                  key=lambda r: r.get("foreign_net_20d_eok"))[:10]
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "what": ("투자자별 순매수(외국인/기관/개인) 추천시점 수급 — KRX 일별. force_scores 결측 보강용 제2 수급원. "
                 "외국인 연속순매도일수=분배 강도."),
        "field_guide": {
            "foreign_net_5d_eok": "외국인 5거래일 순매수(억원, -면 순매도)",
            "foreign_net_20d_eok": "외국인 20거래일 순매수(억원)",
            "foreign_sell_streak": "외국인 연속 순매도 일수(클수록 분배 지속)",
            "inst_net_5d_eok": "기관 5일 순매수(억원)", "indiv_net_5d_eok": "개인 5일 순매수(억원)",
        },
        "key_present": _krx is not None,
        "market": markets,
        "risk_off": risk,
        "universe": len(universe),
        "by_source": by_src,
        "top_foreign_sold_20d": [{"ticker": r["ticker"], "name": r["name"],
                                  "foreign_net_20d_eok": r.get("foreign_net_20d_eok"),
                                  "streak": r.get("foreign_sell_streak")} for r in sold],
        "tickers": ordered,
        "disclaimer": "KRX 공개데이터(투자자별 매매). 투자자문 아님.",
    }

    out_path = os.path.abspath(out) if out else None
    if not out_path:
        sess = _today_latest_session()
        out_path = os.path.join(sess, "flow_data.json") if sess else os.path.join(HERE, "flow_data.json")
    try:
        _save_json_atomic(out_path, payload)
        log.info("[flow] 저장 완료: %s (krx=%d, risk-off=%s)",
                 out_path, by_src.get("krx", 0), risk["risk_off_label"])
    except Exception as e:
        log.warning("[flow] 저장 실패: %s", e)
    return 0


def main():
    ap = argparse.ArgumentParser(description="투자자별 순매수 수집/집계")
    ap.add_argument("--collect", action="store_true", help="추천시점 수급 수집 → flow_data.json")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--tickers", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    if args.check:
        log.info("[flow] pykrx: %s", "있음" if _krx else "없음")
        if _krx:
            print("  KOSPI:", get_market_flow("KOSPI"))
            print("  005930:", get_recent_flow("005930", "삼성전자"))
        return 0
    # 기본 동작 = 수집
    return collect(tickers=args.tickers, limit=args.limit, out=args.out)


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        log.warning("[flow] 치명적 예외(무시): %s: %s", type(e).__name__, e)
        sys.exit(0)
