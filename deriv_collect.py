# -*- coding: utf-8 -*-
r"""
deriv_collect.py — 파생(옵션) 기반 헤지/위험회피 신호 수집 (KRX, 무료)

[목적] "세력·외인이 주식을 사면서도 불안하면 풋옵션으로 보호풋(헤지)한다"는 신호를 잡는다.
  현물 수급만 보면 놓치는 '사면서도 헤지하는' 스마트머니 경계 신호.
  1) 시장 전체: KOSPI200 옵션 풋콜비율(PCR, 거래량·미결제) → 시장 헤지/위험회피(F1).
  2) 개별주식: 옵션 상장 종목의 풋/콜 → 그 종목 보호풋(헤지) 강도.

[데이터] pykrx 의 전종목시세(KrxWebIo, bld MDCSTAT12501) + prodId.
  KRDRVOPK2I=KOSPI200 옵션 / KRDRVOPEQU=개별주식 옵션. ISU_NM 의 ' C '/' P ' 로 콜·풋 구분.
  ※ KRX getJsonData 는 '인증 세션'이 필요 → flow_collect import 로 KRX 로그인(세션 워밍업) 후 호출.

[설계] 행별 try/except, 원자적 저장, ASCII 태그([deriv]), 한글 OK·이모지 금지, UTF-8 ensure_ascii=False.
  개별주식옵션은 일부 대형주만 상장(대부분 중소형 추천엔 옵션 없음 → 그 종목은 신호 없음).
  ★상장 종목 수를 코드 주석의 숫자로 인용하지 마라 — 예전 주석은 '~40' 이었으나
    2026-08-07 실측은 **64종**이었다. 세는 곳은 산출물의 by_underlying 하나뿐이다.

[사용법]
  python deriv_collect.py                 # 최근 거래일 → deriv_sentiment.json
  python deriv_collect.py --date 20260625
"""
import os
import sys
import re
import json
import logging
import argparse
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("deriv")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DEFAULT = os.path.join(HERE, "deriv_sentiment.json")

PROD_KOSPI200_OPT = "KRDRVOPK2I"     # KOSPI200 옵션
PROD_SINGLE_OPT = "KRDRVOPEQU"       # 개별주식 옵션

# KRX 세션 워밍업(로그인) — flow_collect import 가 KRX 로그인 수행. 실패해도 시도.
_FETCH = None
try:
    from common import suppress_stdout as _suppress_stdout
    with _suppress_stdout():                 # pykrx 포크의 import-시 계정 ID 콘솔 노출 억제
        import flow_collect  # noqa: F401  (import 시 KRX 로그인 → pykrx 세션 인증)
        from pykrx.website.krx.future.core import 전종목시세 as _AllListing
    _FETCH = _AllListing
except Exception as e:
    log.warning("[deriv] pykrx/KRX 세션 준비 실패: %s", type(e).__name__)


def _num(v):
    try:
        s = str(v).replace(",", "").strip()
        if s in ("", "-"):
            return 0.0
        return float(s)
    except Exception:
        return 0.0


_CP_RE = re.compile(r"\s([CP])\s")


def _parse_cp(isu_nm):
    """ISU_NM → ('C'|'P'|None, underlying_name). 예: '삼성전자 C 202509 80,000 (주간)' → ('C','삼성전자')."""
    m = _CP_RE.search(" " + (isu_nm or "") + " ")
    if not m:
        return None, ""
    cp = m.group(1)
    under = (isu_nm[: m.start() - 1] if m.start() >= 1 else isu_nm[: m.start()]).strip()
    return cp, under


def fetch_options(trd, prod):
    if _FETCH is None:
        return None
    try:
        df = _FETCH().fetch(trd, prod)
        return df if (df is not None and len(df)) else None
    except Exception as e:
        log.warning("[deriv] %s %s 조회 실패: %s", prod, trd, type(e).__name__)
        return None


def _latest_trading_date(start):
    """start(YYYYMMDD)부터 거꾸로 데이터 있는 날을 찾는다(최대 6일). 반환: (date, df_kospi)."""
    d = datetime.strptime(start, "%Y%m%d")
    for _ in range(6):
        trd = d.strftime("%Y%m%d")
        df = fetch_options(trd, PROD_KOSPI200_OPT)
        if df is not None:
            return trd, df
        d -= timedelta(days=1)
    return None, None


def _hint(pcr_oi):
    if pcr_oi is None:
        return None
    if pcr_oi >= 1.2:
        return "풋 우위(헤지·약세 경계)"
    if pcr_oi <= 0.8:
        return "콜 우위(낙관)"
    return "중립"


def _agg_cp(df):
    """df → (call_vol, put_vol, call_oi, put_oi). ISU_NM 의 C/P 로 합산."""
    cv = pv = co = po = 0.0
    vcol = "ACC_TRDVOL" if "ACC_TRDVOL" in df.columns else None
    ocol = "ACC_OPNINT_QTY" if "ACC_OPNINT_QTY" in df.columns else None
    ncol = "ISU_NM" if "ISU_NM" in df.columns else None
    if not ncol:
        return cv, pv, co, po
    for _, row in df.iterrows():
        try:
            cp, _u = _parse_cp(row[ncol])
            if cp is None:
                continue
            v = _num(row[vcol]) if vcol else 0.0
            o = _num(row[ocol]) if ocol else 0.0
            if cp == "C":
                cv += v
                co += o
            else:
                pv += v
                po += o
        except Exception:
            continue
    return cv, pv, co, po


def _ratio(put, call):
    return round(put / call, 3) if call and call > 0 else None


def market_pcr(df):
    cv, pv, co, po = _agg_cp(df)
    pcr_oi = _ratio(po, co)
    return {
        "call_vol": int(cv), "put_vol": int(pv), "pcr_volume": _ratio(pv, cv),
        "call_oi": int(co), "put_oi": int(po), "pcr_oi": pcr_oi,
        "hedging_hint": _hint(pcr_oi),
    }


def single_stock_options(trd):
    """개별주식 옵션 → {underlying_name: {call_vol,put_vol,call_oi,put_oi,pcr_oi,pcr_volume}}."""
    df = fetch_options(trd, PROD_SINGLE_OPT)
    if df is None:
        return {}
    ncol = "ISU_NM" if "ISU_NM" in df.columns else None
    vcol = "ACC_TRDVOL" if "ACC_TRDVOL" in df.columns else None
    ocol = "ACC_OPNINT_QTY" if "ACC_OPNINT_QTY" in df.columns else None
    agg = {}
    for _, row in df.iterrows():
        try:
            cp, under = _parse_cp(row[ncol])
            if cp is None or not under:
                continue
            a = agg.setdefault(under, {"call_vol": 0.0, "put_vol": 0.0, "call_oi": 0.0, "put_oi": 0.0})
            v = _num(row[vcol]) if vcol else 0.0
            o = _num(row[ocol]) if ocol else 0.0
            if cp == "C":
                a["call_vol"] += v
                a["call_oi"] += o
            else:
                a["put_vol"] += v
                a["put_oi"] += o
        except Exception:
            continue
    out = {}
    for under, a in agg.items():
        pcr_oi = _ratio(a["put_oi"], a["call_oi"])
        out[under] = {
            "call_vol": int(a["call_vol"]), "put_vol": int(a["put_vol"]),
            "call_oi": int(a["call_oi"]), "put_oi": int(a["put_oi"]),
            "pcr_volume": _ratio(a["put_vol"], a["call_vol"]), "pcr_oi": pcr_oi,
            "hedging_hint": _hint(pcr_oi),
        }
    return out


from common import save_json_atomic as _save_json  # 원자적 JSON 저장(common.py 통합)


def collect(date, out_path):
    if _FETCH is None:
        log.info("[deriv] pykrx/KRX 세션 미준비 — 파생 수집 스킵")
        return None
    trd, df = _latest_trading_date(date)
    if df is None:
        log.warning("[deriv] %s 부근 거래일 데이터 없음 — 스킵", date)
        return None
    mkt = market_pcr(df)
    by_under = single_stock_options(trd)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": trd,
        "source": "KRX 파생 전종목시세(MDCSTAT12501) — 무료",
        "market": mkt,
        "by_underlying": by_under,
        "_note": ("market.pcr_oi(풋미결제/콜미결제)>1.2 = 시장 헤지/위험회피 강화(F1 경계). "
                  "by_underlying 은 옵션 상장 종목만(수는 이 dict 의 키 수를 세라 — "
                  "고정값이 아니다). 추천 종목이 그 안에 있고 풋이 급증/우위면 "
                  "'세력·외인이 그 종목을 사면서도 보호풋으로 헤지' = 추격 경계."),
    }
    _save_json(out_path, payload)
    log.info("[deriv] 저장: %s (%s | 시장PCR(OI)=%s %s · 개별옵션 %d종목)",
             out_path, trd, mkt["pcr_oi"], mkt["hedging_hint"], len(by_under))
    return payload


def main():
    ap = argparse.ArgumentParser(description="KRX 파생(옵션) 헤지신호 수집")
    ap.add_argument("--date", default=datetime.now().strftime("%Y%m%d"),
                    help="조회 시작일 YYYYMMDD(거꾸로 거래일 탐색). 기본 오늘")
    ap.add_argument("--out", default=OUT_DEFAULT)
    args = ap.parse_args()
    collect(args.date, os.path.abspath(args.out))


if __name__ == "__main__":
    main()
