#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
retro_label.py ─ 회고분석(PART C) 학습 데이터셋 빌더

[목적]
  과거에 우리 분석 Cowork 가 추천한 종목(predictions.json 의 picks/shorts)이 '예상대로 왜 안
  올랐는지'를 학습하기 위한 데이터셋을 만든다. 각 추천에 대해
    (A) 추천 시점의 피처 스냅샷 : 그날 세션의 force_scores/overheat/disclosures/short/mirae_data,
                                  시장국면(market_context.regime)
    (B) 추천 이후 실제 결과 라벨 : 만기(horizon) 수익률, '고점까지 일수(days_to_peak)',
                                  '고점 후 되돌림폭(post_peak_drawdown)', 차익실현형 고점 플래그
  를 한 행으로 묶어 retro_dataset.json (+ 사람이 보는 retro_dataset.csv) 으로 저장한다.
  이 데이터셋을 회고 Cowork 가 읽고 '큰손이 언제 차익실현하는가 / 추천을 어떻게 고칠까'를 학습한다.

[재사용·독립성]
  - accuracy_tracker 를 import 해 FDR 가격조회·거래일·predictions 로더를 그대로 쓴다(기존 파일 무수정).
  - 룩어헤드 금지: 피처는 '추천 당일 세션'에서만, 라벨은 '추천일 이후' 가격에서만.
  - 만기 미도달 행도 기록하되 matured=false 로 표시(라벨은 부분만). 부분 실패/예외는 행 단위로 흡수, exit 0.
  - 콘솔 ASCII 태그([retro]) + 한글. 이모지 금지. UTF-8 IO, json ensure_ascii=False, 원자적 저장.

[사용법]
  python retro_label.py                  # _archive 전체 → retro_dataset.json/csv (BASE_DIR)
  python retro_label.py --out x.json      # 출력 경로 지정
"""

import os
import sys
import csv
import json
import argparse
import logging
from datetime import datetime, timedelta

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("retro")

HERE = os.path.dirname(os.path.abspath(__file__))
DATASET_JSON = os.path.join(HERE, "retro_dataset.json")
DATASET_CSV = os.path.join(HERE, "retro_dataset.csv")

# accuracy_tracker 재사용(predictions 로더·날짜·float 헬퍼). 실패 시 graceful.
try:
    import accuracy_tracker as acc
    ACC_OK = True
except Exception as e:
    acc = None
    ACC_OK = False
    log.warning("[retro] accuracy_tracker import 실패(predictions 로드 불가): %s", e)

# 가격 출처: 금융위(FSC) 공식 종가 우선, 실패 시 FinanceDataReader 폴백(fsc_collect.get_close_series).
try:
    import fsc_collect as fsc
    FSC_OK = True
except Exception as e:
    fsc = None
    FSC_OK = False
    log.warning("[retro] fsc_collect import 실패(가격 채점 불가): %s", e)

# 투자자별 순매수(개인/기관/외국인) 집계 — 보유기간 동안 누가 사고 팔았나(KRX/pykrx). 선택.
try:
    import flow_collect as flow
    FLOW_OK = True
except Exception as e:
    flow = None
    FLOW_OK = False
    log.warning("[retro] flow_collect import 실패(투자자 매매 집계 생략): %s", e)

# 6/18 이전 발송 리포트(predictions.json 없음)도 파싱해 채점 포함 — 표본 확대(_src_kind='archive').
try:
    import retro_archive_parse as archive
    ARCH_OK = True
except Exception as e:
    archive = None
    ARCH_OK = False
    log.warning("[retro] retro_archive_parse import 실패(옛 리포트 채점 생략): %s", e)

# 진입시점 공매도(룩어헤드 없음) — short_collect.get_short_asof. 선택.
try:
    import short_collect as short
    SHORT_OK = True
except Exception as e:
    short = None
    SHORT_OK = False
    log.warning("[retro] short_collect import 실패(진입시점 공매도 생략): %s", e)

# KOSPI 국면(추천일 직전 5일 수익률) — FDR. 선택.
try:
    import FinanceDataReader as fdr
    FDR_OK = True
except Exception:
    fdr = None
    FDR_OK = False

_KOSPI_CACHE = {}


def _kospi_ret5d(base_date):
    """추천일 '직전' 5거래일 KOSPI(KS11) 수익률(%) — 진입시점 국면 프록시.

    ★v10.0 룩어헤드 수정(회고 A22): 예전엔 base_date(추천일) 종가까지 포함해 계산했다.
    그 종가는 추천 시점(06:30)에 아직 존재하지 않는 미래값이라 CLAUDE.md 절대규칙
    '룩어헤드 금지: 회고 피처는 그 시점 이전 데이터만'을 위반했고, 분석가가 아침에 실제로 본
    market_context.kr_index.kospi_ret5d_pct(D-1 기준)와도 값이 달라 게이트 검증이 왜곡됐다.
    → base_date **미만** 봉만 사용한다(= D-1 종가로 끝나는 5거래일 수익률).
    """
    if not FDR_OK or base_date is None:
        return None
    key = base_date.strftime("%Y%m%d")
    if key in _KOSPI_CACHE:
        return _KOSPI_CACHE[key]
    val = None
    try:
        # 20일로 넓힌다: base_date 를 빼고도 6개 봉(D-1..D-6)을 확보해야 하므로(연휴 대비)
        df = fdr.DataReader("KS11", (base_date - timedelta(days=20)).strftime("%Y-%m-%d"),
                            base_date.strftime("%Y-%m-%d"))
        c = []
        for _idx, _close in zip(df.index, df["Close"].tolist()):
            try:
                _d = _idx.date()
            except Exception:
                continue
            if _d >= base_date:          # ★추천일 당일 이후는 진입 시점에 알 수 없다
                continue
            if _close == _close:         # NaN 제외
                c.append(float(_close))
        if len(c) >= 6:
            val = round((c[-1] / c[-6] - 1.0) * 100, 2)
    except Exception:
        val = None
    _KOSPI_CACHE[key] = val
    return val


# --- 알파(지수 차감) 라벨(#R1) — KOSPI 전 구간 1회 조회 캐시 ---
_KS11_SERIES = None


def _ks11_series():
    """[(date, close)] 오름차순 — 알파 라벨용 KOSPI 종가. 전 구간 1회 조회(행별 재조회 금지)."""
    global _KS11_SERIES
    if _KS11_SERIES is None:
        _KS11_SERIES = []
        if FDR_OK:
            try:
                df = fdr.DataReader("KS11", "2025-01-01")
                for idx, cl in zip(df.index, df["Close"].tolist()):
                    if cl == cl:  # NaN 제외
                        _KS11_SERIES.append((idx.date(), float(cl)))
            except Exception as e:
                log.warning("[retro] KOSPI 시계열 조회 실패(알파 라벨 생략): %s", type(e).__name__)
    return _KS11_SERIES


def _kospi_ret_h(base_date, horizon):
    """진입일 종가 -> T+horizon 종가의 KOSPI 수익률(%) — 알파(ret_h - kospi_ret_h) 라벨용(사후 라벨).
    '과열추격 -9%가 종목선택 실패인가, 그 뒤 지수가 빠진 것(베타)인가'를 분리한다. 미만기/결측이면 None."""
    if base_date is None or not horizon:
        return None
    ser = _ks11_series()
    if not ser:
        return None
    start = next((i for i, (d, _c) in enumerate(ser) if d >= base_date), None)
    if start is None or start + horizon >= len(ser):
        return None
    c0, c1 = ser[start][1], ser[start + horizon][1]
    if not c0:
        return None
    return round((c1 / c0 - 1.0) * 100.0, 2)


_PRE_FLOW_KEYS = ("pre_foreign_5d_eok", "pre_foreign_20d_eok", "pre_inst_5d_eok",
                  "pre_indiv_5d_eok", "pre_foreign_sell_streak")
_PRE_SHORT_KEYS = ("pre_short_balance_ratio", "pre_short_change_10d")


def pre_entry_features(ticker, base_date, snap=None):
    """진입시점(추천일까지) 피처 — 룩어헤드 없음. '진입 전부터 큰손이 분배 중이었나'를 예측 피처로.
      pre_foreign_5d_eok/20d_eok·pre_inst_5d_eok·pre_indiv_5d_eok·pre_foreign_sell_streak(KRX 일별 수급) +
      pre_short_balance_ratio·pre_short_change_10d(공매도 잔고) + pre_kospi_ret5d(국면).
    #C1(회고 07-06 백필 요청): 세션 스냅샷(snap=추천 아침의 flow_data/short.json 값) 우선, 없을 때만
      pykrx 라이브(get_*_asof) 폴백 — 회고가 새벽에 돌 때 KRX 간헐실패로 전량 null 되던 문제의 근본 수정.
      세션 파일은 추천 아침에 산출된 값이라 asof 의미가 동일(룩어헤드 없음)."""
    out = {}
    if base_date is None:
        return out
    snap = snap or {}
    for k in _PRE_FLOW_KEYS + _PRE_SHORT_KEYS:      # 1순위: 세션 스냅샷
        v = snap.get("_snap_" + k)
        if v is not None:
            out[k] = v
    asof = base_date.strftime("%Y%m%d")
    if FLOW_OK and any(out.get(k) is None for k in _PRE_FLOW_KEYS):   # 2순위: 라이브 폴백(빠진 키만)
        try:
            live = flow.get_flow_asof(ticker, asof) or {}
            for k in _PRE_FLOW_KEYS:
                if out.get(k) is None and live.get(k) is not None:
                    out[k] = live[k]
        except Exception:
            pass
    if SHORT_OK and any(out.get(k) is None for k in _PRE_SHORT_KEYS):
        try:
            live = short.get_short_asof(ticker, asof) or {}
            for k in _PRE_SHORT_KEYS:
                if out.get(k) is None and live.get(k) is not None:
                    out[k] = live[k]
        except Exception:
            pass
    out["pre_kospi_ret5d"] = _kospi_ret5d(base_date)
    out["sector"] = _sector_of(ticker)
    # 진입시점 기술피처 백필(P1b) — 가격에서 소급 계산해 §3 과열/즉시고점 가설을 만기행에서 바로 검증.
    try:
        out.update(pre_tech_features(ticker, base_date))
    except Exception:
        pass
    return out


# --- 진입시점 기술피처(룩어헤드 없음) — 추천일까지 가격으로 RSI·연속상승·52주고가이격·과열 소급 계산(P1b) ---
_TECH_CACHE = {}


def _rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(len(closes) - n, len(closes)):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return round(100 - 100 / (1 + rs), 1)


def _overheat_proxy(rsi, dist_52w, up_streak, ret_20d):
    """0~100 과열 프록시(overheat.json 부재 행 보강): RSI 고점·신고가근접·연속상승·단기급등 가중."""
    score = 0.0
    if rsi is not None:
        score += max(0.0, (rsi - 55) / 45.0) * 45      # RSI 55~100 → 0~45
    if dist_52w is not None:
        score += max(0.0, (dist_52w + 10) / 10.0) * 25  # 52주고가 -10%~0% → 0~25(근접할수록)
    if up_streak is not None:
        score += min(up_streak, 6) / 6.0 * 15           # 연속상승 0~6 → 0~15
    if ret_20d is not None:
        score += max(0.0, min(ret_20d, 40) / 40.0) * 15  # 20일 +0~40% → 0~15
    return round(max(0.0, min(100.0, score)), 1)


def pre_tech_features(code, base_date):
    """추천일까지의 기술적 진입피처: pre_rsi14·pre_up_streak·pre_ret_20d_pct·pre_dist_52w_high_pct·pre_overheat.
    가격은 FDR 종가(base_date 까지만). 룩어헤드 없음. 실패 시 빈 dict. (code,날짜) 캐시."""
    if not FDR_OK or base_date is None:
        return {}
    key = (str(code).zfill(6), base_date.strftime("%Y%m%d"))
    if key in _TECH_CACHE:
        return _TECH_CACHE[key]
    out = {}
    try:
        start = (base_date - timedelta(days=400)).strftime("%Y-%m-%d")
        end = base_date.strftime("%Y-%m-%d")
        df = fdr.DataReader(str(code).zfill(6), start, end)
        closes = [float(x) for x in df["Close"].tolist() if x == x and x > 0]
        if len(closes) >= 25:
            c = closes[-1]
            ret20 = round((c / closes[-21] - 1) * 100, 2) if len(closes) >= 21 else None
            win = closes[-252:] if len(closes) >= 252 else closes
            hi = max(win)
            dist52 = round((c / hi - 1) * 100, 2) if hi > 0 else None
            streak = 0
            for i in range(len(closes) - 1, 0, -1):
                if closes[i] > closes[i - 1]:
                    streak += 1
                else:
                    break
            rsi = _rsi(closes, 14)
            # 이격도20(#2 백필: 죽은 disparity20 컬럼을 pre_* 로 충당) = 종가/20일이평 -1
            disp20 = None
            if len(closes) >= 20:
                ma20 = sum(closes[-20:]) / 20.0
                disp20 = round((c / ma20 - 1) * 100, 2) if ma20 > 0 else None
            out = {
                "pre_ret_20d_pct": ret20, "pre_dist_52w_high_pct": dist52,
                "pre_up_streak": streak, "pre_rsi14": rsi, "pre_disparity20": disp20,
                "pre_overheat": _overheat_proxy(rsi, dist52, streak, ret20),
            }
    except Exception:
        out = {}
    _TECH_CACHE[key] = out
    return out


# --- 섹터(업종) 태그 — '방어/조선 등 테마 편중' 분리용(#5b). FDR 상장목록 1회 캐시 ---
_SECTOR_MAP = None


_SECTOR_CACHE_FILE = os.path.join(HERE, "cache", "sector_map.json")
_CAPMKT_CACHE_FILE = os.path.join(HERE, "cache", "cap_market_map.json")
_CAPMKT_MAP = None


def _cap_market_of(code):
    """code -> (시총 억원, KOSPI/KOSDAQ). FDR StockListing('KRX') 1회 로드 + 디스크 캐시 폴백.
    #E(주도주 예외 정량화): '반도체·바이오 대장 예외'가 12회차 내내 서사로만 존재해 정의 불가였다 —
    시총 버킷이 있어야 회고가 '대형은 강세추격에도 간다'를 수치로 검증/기각할 수 있다.
    ⚠️ 이 값은 '현재' 시총(조회 시점)이지 진입 시점 시총이 아니다(경미한 드리프트) — 버킷 분류용으로만
    쓰고 수익률 크기 회귀에 쓰지 마라(label_guide 에 동일 경고)."""
    global _CAPMKT_MAP
    if _CAPMKT_MAP is None:
        _CAPMKT_MAP = {}
        if FDR_OK:
            try:
                lst = fdr.StockListing("KRX")
                ccol = next((c for c in ("Code", "Symbol") if c in lst.columns), None)
                if ccol and "Marcap" in lst.columns:
                    mcol = "Market" if "Market" in lst.columns else None
                    for _, rr in lst.iterrows():
                        c = str(rr[ccol]).zfill(6)
                        try:
                            cap_eok = round(float(rr["Marcap"]) / 1e8)
                        except Exception:
                            continue
                        mk = str(rr[mcol]) if mcol else ""
                        _CAPMKT_MAP[c] = [cap_eok, mk]
            except Exception:
                pass
        if _CAPMKT_MAP:
            try:
                from common import save_json_atomic as _sj
                _sj(_CAPMKT_CACHE_FILE, _CAPMKT_MAP)
            except Exception:
                pass
        else:
            cached = _load_json(_CAPMKT_CACHE_FILE)
            if isinstance(cached, dict) and cached:
                _CAPMKT_MAP = {str(k): v for k, v in cached.items()}
                log.info("[retro] cap/market: FDR 실패 -> 디스크 캐시 사용(%d종목)", len(_CAPMKT_MAP))
    v = _CAPMKT_MAP.get(str(code).zfill(6))
    return (v[0], v[1]) if v else (None, None)


# ★A19: 범주형 컬럼의 '허용값 전체'. 회고가 문자열 접두 매칭으로 추측하다 집계를 틀린 사고가
#   있었다('메가' vs '메가(10조+)', KOSDAQ 완전일치로 'KOSDAQ GLOBAL' 53건 누락).
#   → dataset 메타에 그대로 실어 분석이 추측하지 않게 한다. _cap_bucket 리터럴과 동기 유지(하네스 검사).
CAP_BUCKETS = ["메가(10조+)", "대형(1조+)", "중형(3천억+)", "소형"]
# exchange 는 FDR StockListing 의 Market 원값을 가공 없이 싣는다(정규화하면 정보가 준다).
#   'KOSDAQ GLOBAL' 은 코스닥 소속이므로 **코스닥 집계 시 반드시 함께 세어라**.
EXCHANGES = ["KOSPI", "KOSDAQ", "KOSDAQ GLOBAL"]


def _cap_bucket(cap_eok):
    """시총(억원) -> 버킷. 메가(>=10조)/대형(>=1조)/중형(>=3천억)/소형."""
    if cap_eok is None:
        return None
    if cap_eok >= 100000:
        return "메가(10조+)"
    if cap_eok >= 10000:
        return "대형(1조+)"
    if cap_eok >= 3000:
        return "중형(3천억+)"
    return "소형"


def _sector_of(code):
    """code → 업종(Sector) 문자열. FDR StockListing('KRX') 1회 로드 캐시. 없으면 None.
    #C1: FDR 성공 시 디스크 캐시 갱신, 실패 시(새벽 간헐실패) 지난 캐시 폴백 — sector 전량 null 방지."""
    global _SECTOR_MAP
    if _SECTOR_MAP is None:
        _SECTOR_MAP = {}
        if FDR_OK:
            # KRX-DESC 에 Sector/Industry 컬럼이 있다(KRX 스냅샷에는 없음).
            # 대형주는 Sector 가 NaN 이고 Industry 에만 값이 있는 경우가 많아 행별로 폴백한다.
            try:
                lst = fdr.StockListing("KRX-DESC")
                ccol = next((c for c in ("Code", "Symbol") if c in lst.columns), None)
                scols = [c for c in ("Sector", "Industry") if c in lst.columns]
                if ccol and scols:
                    for _, rr in lst[[ccol] + scols].iterrows():
                        c = str(rr[ccol]).zfill(6)
                        if not c:
                            continue
                        for sc in scols:
                            s = rr[sc]
                            if isinstance(s, str) and s.strip():
                                _SECTOR_MAP[c] = s.strip()
                                break
            except Exception:
                pass
        if _SECTOR_MAP:
            try:                                   # 성공 → 디스크 캐시 갱신(다음 실패 대비)
                from common import save_json_atomic as _sj
                _sj(_SECTOR_CACHE_FILE, _SECTOR_MAP)
            except Exception:
                pass
        else:
            cached = _load_json(_SECTOR_CACHE_FILE)  # 실패 → 지난 캐시 폴백
            if isinstance(cached, dict) and cached:
                _SECTOR_MAP = {str(k): v for k, v in cached.items()}
                log.info("[retro] sector: FDR 실패 -> 디스크 캐시 사용(%d종목)", len(_SECTOR_MAP))
    return _SECTOR_MAP.get(str(code).zfill(6))


# --- DART 분배성 공시 매칭 — '거래량 클라이맥스=분배' 가설 검증(#4). 라벨side(보유 후) ---
try:
    import dart_collect as _dc
    import disclosure_collect as _disc
    DART_OK = True
except Exception:
    _dc = None
    _disc = None
    DART_OK = False

_DART_KEY = None
_CORP_MAP = None
# 분배(매물 출회)성 공시 분류만 매칭(자사주 취득=흡수는 제외)
_DIST_CATS = {"증자(희석)", "메자닌(전환물량)", "전환청구(출회)", "대주주/대량보유", "감자/기타"}


def _dart_ready():
    global _DART_KEY, _CORP_MAP
    if not DART_OK:
        return False
    if _DART_KEY is None:
        try:
            _DART_KEY = _dc.load_dart_key() or ""
        except Exception:
            _DART_KEY = ""
    if _DART_KEY and _CORP_MAP is None:
        try:
            _CORP_MAP = _dc.load_corp_map(_DART_KEY) or {}
        except Exception:
            _CORP_MAP = {}
    return bool(_DART_KEY and _CORP_MAP)


def holding_distribution(code, base_date, horizon):
    """보유기간 [추천일, 추천일+horizon+여유] 동안의 분배성 공시(증자/CB/대주주·대량보유 변동) 매칭.
    차익실현형(profit_take)이 실제 공시 물량과 동반했는지 검증하는 라벨side 증거. 실패 시 빈 dict."""
    if not _dart_ready() or base_date is None:
        return {}
    corp = _CORP_MAP.get(str(code).zfill(6)) or _CORP_MAP.get(str(code))
    if not corp:
        return {}
    try:
        import requests as _rq
        bgn = base_date.strftime("%Y%m%d")
        end = (base_date + timedelta(days=int(horizon or 10) + 14)).strftime("%Y%m%d")
        r = _rq.get("https://opendart.fss.or.kr/api/list.json",
                    params={"crtfc_key": _DART_KEY, "corp_code": corp,
                            "bgn_de": bgn, "end_de": end, "page_count": 100}, timeout=12)
        j = r.json()
        # 의도적 구분: 0 = '확인된 무공시'(컬럼 채움, 교차표의 진짜 0), 부재 = '확인 불가/미상'.
        # 013(조회데이터 없음)=성공·무공시 → 0. 그 외 상태/예외/키없음 → {} 부재(실패를 0으로 오집계 방지).
        if j.get("status") == "013":
            return {"dist_disc_count": 0}
        if j.get("status") != "000":
            return {}
        items = j.get("list", []) or []
    except Exception:
        return {}
    hits = []
    for it in items:
        cl = _disc.classify(it.get("report_nm")) if _disc else None
        if cl and cl[0] in _DIST_CATS:
            hits.append({"date": it.get("rcept_dt"), "cat": cl[0],
                         "title": (it.get("report_nm") or "").strip()[:40]})
    return {"dist_disc_count": len(hits),
            "dist_disc_cats": sorted({h["cat"] for h in hits}),
            "dist_disc_titles": [h["title"] for h in hits[:3]]}


# 진입시점 피처 컬럼(룩어헤드 없음 — 진입규칙에 쓸 수 있는 예측 피처)
PRE_COLS = [
    "pre_foreign_5d_eok", "pre_foreign_20d_eok", "pre_inst_5d_eok", "pre_indiv_5d_eok",
    "pre_foreign_sell_streak", "pre_short_balance_ratio", "pre_short_change_10d", "pre_kospi_ret5d",
    "sector",
    # #A6 유동성 정규화(메가캡 왜곡 제거) — 5일 순매수 / 20일 평균 일거래대금(배)
    "pre_foreign_5d_ratio", "pre_indiv_5d_ratio", "pre_avg_trade_value_20d_eok",
    # 진입시점 기술피처(P1b 백필) — §3 과열/즉시고점 가설 검증용
    "pre_rsi14", "pre_up_streak", "pre_ret_20d_pct", "pre_dist_52w_high_pct", "pre_disparity20", "pre_overheat",
]
# 라벨side 보강 컬럼(보유 후 결과 — 진입규칙 사용 금지). cats/titles 도 CSV 에 포함(JSON 과 일관, 조용한 드롭 방지)
ENRICH_COLS = ["dist_disc_count", "dist_disc_cats", "dist_disc_titles"]
# #E 메타 컬럼(현재값 프록시 — 진입시점 아님·버킷 분류 전용): 주도주 예외·KOSPI/KOSDAQ 분해용
META_COLS = ["market_cap_eok", "cap_bucket", "exchange"]
# DART 분배공시 매칭 토글(--no-dart 로 끔). 키 없으면 자동 graceful.
DART_ENRICH = True

# 차익실현형 고점 판정 임계치(튜닝 가능)
PROFIT_TAKE_MIN_PEAK = 7.0     # 고점까지 +7% 이상 올랐다가
PROFIT_TAKE_GIVEBACK = -5.0    # 고점 대비 -5% 이상 반납하면 '차익실현형'

# 한 행에 담을 피처 컬럼(없으면 None) — CSV 컬럼 순서이기도 함
FEATURE_COLS = [
    # force_scores
    "force_score", "force_label", "supply", "rsi", "ma_disparity_20", "vol_ratio",
    "foreign_5d", "foreign_20d", "supply_source",
    # overheat
    "overheat_score", "disparity20", "disparity60", "up_streak",
    "dist_52w_high_pct", "ret_20d_pct", "rsi14", "obv_divergence",
    # disclosures / short / mirae
    "overhang_score", "short_balance_ratio", "short_pressure_score", "short_trend",
    "per", "pbr", "foreign_hold_pct",
    # market-level
    "regime_label", "regime_score",
    # #S1 market-level(세션 동결 스냅샷 — 2026-07-17+ 세션만 값 있음). regime dict 에서 읽는다.
    "pre_caution_score", "pre_regime_kind", "pre_allow_market_up",
    "pre_pcr_oi", "pre_vkospi", "pre_vkospi_d5_chg", "pre_vkospi_pct_rank", "pre_vkospi_label",
    "pre_base_rate", "pre_usdkrw_chg5d",
    # v9.8 신용잔고(빚투) — 2026-07-21 이후 세션에만 값(그 전은 결측=정상). SNAPSHOT_MARKET_COLS 와
    # ★반드시 동기 유지(여기 없으면 _row_for 831행 루프가 컬럼을 아예 안 실어 무음 no-op — 하네스가 계약검사).
    "pre_margin_total_eok", "pre_margin_d5_chg_pct", "pre_margin_pct_rank",
]
# 위 중 '시장수준(그날 공통)' 컬럼 — _row_for 가 종목별 feats 가 아니라 regime 에서 읽어야 하는 것들.
SNAPSHOT_MARKET_COLS = {
    "pre_caution_score", "pre_regime_kind", "pre_allow_market_up",
    "pre_pcr_oi", "pre_vkospi", "pre_vkospi_d5_chg", "pre_vkospi_pct_rank", "pre_vkospi_label",
    "pre_base_rate", "pre_usdkrw_chg5d",
    # v9.8 신용잔고(빚투) — 2026-07-21 이후 세션에만 값(그 전은 결측=정상)
    "pre_margin_total_eok", "pre_margin_d5_chg_pct", "pre_margin_pct_rank",
}
LABEL_COLS = [
    "matured", "label_status", "fwd_days_avail", "ret_h_pct",
    # #5 미만기 부분수익(만기 ret_h 와 분리 — 오독 방지)
    "ret_partial_pct", "partial_asof_date", "last_close",
    "ret_1", "ret_3", "ret_5", "ret_10", "ret_20",
    "peak_gain_pct", "days_to_peak", "post_peak_drawdown_pct", "max_drawdown_pct",
    "days_to_trough", "post_trough_rebound_pct",   # #R2 숏 경로(익절 설계)
    "kospi_ret_h_pct", "alpha_h_pct",              # #R1 지수차감(베타/선택 분리)
    "ret_if_stop8_pct", "ret_if_stop8_tp12_pct",   # #S2 손절 반사실(규칙을 지켰다면)
    "ret_if_stop8_tp12_cap_pct",                   # #A15 익절 지정가 체결 가정(상방편향 보정판)
    "profit_take_flag", "hit", "settle_close",
    # 거래량(차익실현·큰손 매도 신호)
    "entry_volume", "avg_volume_20d", "avg_volume_20d_ex_entry", "peak_day_vol_ratio", "trough_day_vol_ratio",
    # 투자자별 순매수(보유기간, 억원) — 누가 사고 팔았나
    "flow_foreign_eok", "flow_inst_eok", "flow_indiv_eok",
]
SNAPSHOT_START_DATE = "2026-07-18"   # signals_snapshot_* 도입일. 그 이전 결측은 '정상'이다.


def _snapshot_coverage(rows):
    """pred_date 별 국면 스냅샷(pre_regime_kind) 커버리지 요약.

    회고가 '결측=결함'인지 '결측=정상(기능 도입 전)'인지 스스로 판정할 수 있게 만드는 메타.
    missing_dates 는 **도입일 이후인데도 비어 있는 날** — 이것만이 진짜 조사 대상이다.
    """
    by_date = {}
    for r in (rows or []):
        d = r.get("pred_date")
        if not d:
            continue
        has = r.get("pre_regime_kind") is not None
        slot = by_date.setdefault(d, [0, 0])
        slot[0] += 1
        if has:
            slot[1] += 1
    missing = sorted(d for d, (tot, hit) in by_date.items()
                     if hit == 0 and d >= SNAPSHOT_START_DATE)
    covered = sorted(d for d, (tot, hit) in by_date.items() if hit > 0)
    return {
        "snapshot_start_date": SNAPSHOT_START_DATE,
        "dates_with_snapshot": covered,
        "dates_missing_after_start": missing,
        "note": ("dates_missing_after_start 가 비어 있으면 국면 스냅샷은 정상이다. "
                 f"{SNAPSHOT_START_DATE} 이전 pred_date 의 pre_* 국면 결측은 기능 도입 전이라 "
                 "정상이며 결함으로 보고하지 마라. 목록에 날짜가 있으면 그날 snapshot_signals 가 "
                 "돌지 않았다는 뜻(소급 복구 불가 — 루트 신호는 이미 덮어써졌다)."),
    }


BASE_COLS = [
    # 식별자(#6/#8) — 중복·다중horizon·종목클러스터 인지용
    "rec_id", "parent_rec_id", "ticker_rec_seq",
    "pred_date", "kind", "ticker", "name", "tag", "timing", "horizon",
    "conviction", "entry_ref", "entry_ref_estimated", "preprice", "flow_unit_check", "thesis",
]


def _load_json(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        return None


def _by_ticker(payload):
    out = {}
    if isinstance(payload, dict):
        for t in payload.get("tickers", []) or []:
            code = str(t.get("ticker") or "").strip()
            if code:
                out[code] = t
    return out


def load_signal_snapshot(session_dir):
    """추천 당일 세션의 신호파일들을 읽어 {ticker: {피처...}} + 시장국면(dict) 반환.
    없는 파일/종목은 그냥 비워 둔다(None)."""
    feats = {}

    def ensure(code):
        return feats.setdefault(code, {})

    # force_scores.json
    fs = _by_ticker(_load_json(os.path.join(session_dir, "force_scores.json")))
    for code, t in fs.items():
        d = t.get("detail", {}) or {}
        f = ensure(code)
        f.update({
            "force_score": t.get("force_score"), "force_label": t.get("label"),
            "supply": d.get("supply"), "rsi": d.get("rsi"),
            "ma_disparity_20": d.get("ma_disparity_20"), "vol_ratio": d.get("vol_ratio"),
            "foreign_5d": d.get("foreign_5d"), "foreign_20d": d.get("foreign_20d"),
            "supply_source": d.get("supply_source"),
        })
    # overheat.json
    oh = _by_ticker(_load_json(os.path.join(session_dir, "overheat.json")))
    for code, t in oh.items():
        f = ensure(code)
        f.update({
            "overheat_score": t.get("overheat_score"), "disparity20": t.get("disparity20"),
            "disparity60": t.get("disparity60"), "up_streak": t.get("up_streak"),
            "dist_52w_high_pct": t.get("dist_52w_high_pct"), "ret_20d_pct": t.get("ret_20d_pct"),
            "rsi14": t.get("rsi14"), "obv_divergence": t.get("obv_divergence"),
        })
    # disclosures.json
    dc = _by_ticker(_load_json(os.path.join(session_dir, "disclosures.json")))
    for code, t in dc.items():
        ensure(code)["overhang_score"] = t.get("overhang_score")
    # short.json (+#C1: pre_short_* 백필 스냅샷 — 회고 새벽 pykrx 재조회 실패 대비)
    sh = _by_ticker(_load_json(os.path.join(session_dir, "short.json")))
    for code, t in sh.items():
        f = ensure(code)
        f.update({"short_balance_ratio": t.get("short_balance_ratio"),
                  "short_pressure_score": t.get("short_pressure_score"),
                  "short_trend": t.get("trend"),
                  "_snap_pre_short_balance_ratio": t.get("short_balance_ratio"),
                  "_snap_pre_short_change_10d": t.get("balance_change_10d")})
    # flow_data.json (#C1: pre_foreign_*/pre_inst_*/pre_indiv_* 백필 스냅샷 — 추천 아침 산출물이라 asof 동일)
    fl = _by_ticker(_load_json(os.path.join(session_dir, "flow_data.json")))
    for code, t in fl.items():
        f = ensure(code)
        f.update({
            "_snap_pre_foreign_5d_eok": t.get("foreign_net_5d_eok"),
            "_snap_pre_foreign_20d_eok": t.get("foreign_net_20d_eok"),
            "_snap_pre_inst_5d_eok": t.get("inst_net_5d_eok"),
            "_snap_pre_indiv_5d_eok": t.get("indiv_net_5d_eok"),
            "_snap_pre_foreign_sell_streak": t.get("foreign_sell_streak"),
        })
    # mirae_data.json
    ks = _by_ticker(_load_json(os.path.join(session_dir, "mirae_data.json")))
    for code, t in ks.items():
        p = t.get("price", {}) or {}
        f = ensure(code)
        f.update({"per": p.get("per"), "pbr": p.get("pbr"),
                  "foreign_hold_pct": p.get("foreign_hold_pct")})

    # market_context.json (시장 수준 — 그날 모든 픽 공통)
    regime = {}
    mc = _load_json(os.path.join(session_dir, "market_context.json"))
    if isinstance(mc, dict) and isinstance(mc.get("regime"), dict):
        regime = mc["regime"]
    # #S1(사각지대 #10): 루트 신호 4종의 세션 동결본(snapshot_signals.py 산출)을 시장수준 피처로.
    # 아침 [6.6]F1/F8 게이트의 1차 입력(국면·PCR·거시·공포)이 회고 학습에 전혀 없던 공백을 메운다.
    # 동결본이 없는 과거 세션은 전부 None(룩어헤드 없음 — 그날 존재하던 값만 씀).
    regime.update(_snapshot_market_feats(session_dir))
    return feats, regime


def _snapshot_market_feats(session_dir):
    """세션에 동결된 루트 신호 4종 → 시장수준 pre_* 피처(없으면 {} — 과거 세션은 결측 정상)."""
    out = {}
    mc = _load_json(os.path.join(session_dir, "signals_snapshot_market_caution.json"))
    if isinstance(mc, dict):
        out["pre_caution_score"] = mc.get("market_caution_score")
        out["pre_regime_kind"] = mc.get("regime_kind")
        out["pre_allow_market_up"] = mc.get("allow_market_up_call")
    dv = _load_json(os.path.join(session_dir, "signals_snapshot_deriv_sentiment.json"))
    if isinstance(dv, dict) and isinstance(dv.get("market"), dict):
        out["pre_pcr_oi"] = dv["market"].get("pcr_oi")
    ec = _load_json(os.path.join(session_dir, "signals_snapshot_ecos_macro.json"))
    if isinstance(ec, dict) and isinstance(ec.get("derived"), dict):
        out["pre_base_rate"] = ec["derived"].get("base_rate")
        out["pre_usdkrw_chg5d"] = ec["derived"].get("usdkrw_change_5d_pct")
    vk = _load_json(os.path.join(session_dir, "signals_snapshot_vkospi.json"))
    if isinstance(vk, dict):
        # ⚠️ vkospi.json 의 latest 는 스칼라가 아니라 {value, chg_pct, d5_chg_pct} 다(vkospi_collect.py:166).
        #    그대로 넣으면 dict 가 컬럼에 실려 CSV·집계가 깨진다 → value 만 꺼낸다.
        lt = vk.get("latest")
        out["pre_vkospi"] = lt.get("value") if isinstance(lt, dict) else lt
        out["pre_vkospi_d5_chg"] = lt.get("d5_chg_pct") if isinstance(lt, dict) else None
        out["pre_vkospi_pct_rank"] = vk.get("pct_rank_60d")
        out["pre_vkospi_label"] = vk.get("level_label")   # '공포' 판별(F1/F8 입력)
    cb = _load_json(os.path.join(session_dir, "signals_snapshot_credit_balance.json"))
    if isinstance(cb, dict) and isinstance(cb.get("margin_loan"), dict):
        ml = cb["margin_loan"]                            # v9.8 신용잔고(빚투) — [5.12]
        out["pre_margin_total_eok"] = ml.get("total_eok")
        out["pre_margin_d5_chg_pct"] = ml.get("d5_chg_pct")
        out["pre_margin_pct_rank"] = ml.get("pct_rank_60d")
    return out


def compute_labels(ticker, base_date, entry_ref, horizon):
    """추천일(base_date) 이후 horizon 거래일까지의 가격경로로 라벨 계산.
    가격 출처: 금융위(FSC) 공식 종가 우선, 실패 시 FDR 폴백(fsc_collect.get_close_series).
    핵심: peak_gain(고점 상승), days_to_peak(고점까지 일수), post_peak_drawdown(고점 후 반납)."""
    if not FSC_OK:
        return {"matured": None, "note": "no_fsc_collect"}
    try:
        series = fsc.get_ohlcv_series(ticker, base_date)   # [(date, close, volume)] 오름차순, FSC→FDR
    except Exception as e:
        return {"matured": None, "note": "fetch_err:%s" % type(e).__name__}
    if not series:
        return {"matured": None, "note": "no_price"}
    dates = [d for d, _c, _v in series]       # date 객체(오름차순)
    closes = [c for _d, c, _v in series]
    vols = [v for _d, _c, v in series]        # 거래량(차익실현·큰손 매도 신호)
    start = next((i for i, d in enumerate(dates) if d is not None and d >= base_date), None)
    if start is None:
        return {"matured": None, "note": "no_start"}

    avail = len(dates) - 1 - start            # 진입일 이후 사용가능한 거래일 수
    matured = avail >= horizon
    h = min(horizon, avail)
    if h < 1:
        return {"matured": False, "fwd_days_avail": avail, "note": "too_recent"}

    base_px = entry_ref if (entry_ref and entry_ref > 0) else closes[start]
    fwd = closes[start: start + h + 1]        # [진입, T+1, ..., T+h]
    rets = [(p / base_px - 1.0) * 100.0 for p in fwd]

    peak_k = max(range(1, len(rets)), key=lambda k: rets[k])
    peak_gain = rets[peak_k]
    peak_px = fwd[peak_k]
    trough_after = min(fwd[peak_k:])
    post_peak_dd = (trough_after / peak_px - 1.0) * 100.0 if peak_px > 0 else None
    max_dd = min(rets[1:])
    # #5: 만기/미만기 분리 — 만기만 ret_h(만기수익), 미만기는 ret_partial(부분수익·진입~최신가)로 분리해 오독 방지.
    last_idx = len(rets) - 1
    if matured:
        ret_h = rets[horizon]
        settle_close_v = round(fwd[horizon], 2)
        ret_partial = None
        partial_asof = None
        last_close_v = None
    else:
        ret_h = None                      # 미만기 = 만기수익 없음(null)
        settle_close_v = None
        ret_partial = round(rets[last_idx], 2)
        _di = start + last_idx
        partial_asof = dates[_di].strftime("%Y-%m-%d") if (_di < len(dates) and dates[_di]) else None
        last_close_v = round(fwd[last_idx], 2)

    def at(k):
        return round(rets[k], 2) if k < len(rets) else None

    profit_take = (peak_gain >= PROFIT_TAKE_MIN_PEAK and
                   post_peak_dd is not None and post_peak_dd <= PROFIT_TAKE_GIVEBACK)

    # ── 거래량(distribution) 지표 ── 고점/저점 당일 거래량이 평소보다 크면 '큰손 매도(차익실현)' 신호
    def _v(i):
        try:
            return float(vols[i]) if (0 <= i < len(vols) and vols[i] is not None) else None
        except Exception:
            return None
    # ★v10.0 (A18): 봉이 모자라면 '짧은 평균'을 20일 평균인 척 내보내지 않는다.
    #   예전엔 6봉만 있어도 그 평균을 avg_volume_20d 로 실었다(이름과 다른 값이 회고 임계의
    #   분모로 쓰임). 이제 20봉 미만이면 None — 결측이 조용한 오류보다 낫다.
    #   가격창 앵커 고정(fsc_collect.PRE_ENTRY_LOOKBACK_DAYS)으로 정상 데이터에선 발동하지 않는다.
    _MIN_VOL_BARS = 20
    pre_vols = [x for x in vols[max(0, start - 20):start + 1] if x is not None]  # 진입 직전까지 ~20일
    avg_vol = (sum(pre_vols) / len(pre_vols)) if len(pre_vols) >= _MIN_VOL_BARS else None
    # #A6: 비율(pre_*_ratio) 분모용 — 추천일 '당일' 거래량 제외(pre_ 명명 준수, 경미한 룩어헤드 제거).
    _prior = [x for x in vols[max(0, start - 20):start] if x is not None]
    avg_vol_ex = (sum(_prior) / len(_prior)) if len(_prior) >= _MIN_VOL_BARS else None
    entry_vol = _v(start)
    peak_vol = _v(start + peak_k)
    worst_k = min(range(1, len(rets)), key=lambda k: rets[k]) if len(rets) > 1 else 0
    trough_vol = _v(start + worst_k)

    # 숏 경로 라벨(#R2) — 저점까지 일수·저점 후 되돌림: 숏 익절 규칙("D+N / -X% 도달 시") 설계용.
    # (픽에는 '눌림 후 회복' 분석용. days_to_peak/peak_gain 의 하락 대칭.)
    trough_px = fwd[worst_k] if worst_k < len(fwd) else None
    post_trough_rb = ((max(fwd[worst_k:]) / trough_px - 1.0) * 100.0
                      if (trough_px and trough_px > 0) else None)

    # 알파 라벨(#R1) — 같은 창의 KOSPI 수익률 차감: '종목선택 실패 vs 시장베타' 분리(만기행만).
    kospi_rh = _kospi_ret_h(base_date, horizon) if matured else None
    alpha_h = (round(ret_h - kospi_rh, 2)
               if (ret_h is not None and kospi_rh is not None) else None)

    # 손절 반사실 라벨(#S2, 사각지대 #5) — "규칙을 지켰다면 결과가 얼마였나".
    # 즉시고점군 ret_h -13%를 손절선이 얼마나 줄였을지 회고가 정량 답하게 한다(경로 기반, 종가 근사).
    # 주의: 종가 기준이라 장중 터치는 반영 못 함(보수적 = 실제보다 손절이 덜 걸림). 진입규칙 아닌 사후 라벨.
    def _counterfactual(stop_pct, tp_pct=None, cap_tp=False):
        """진입 후 종가경로에서 stop/tp 에 처음 닿은 날 '그날 종가'로 청산했다면의 수익률(%).

        정직성: 손절선(-8%)을 그대로 반환하지 않고 **실제 그날 종가 수익률**을 반환한다.
        갭하락으로 -12% 마감했으면 -12% 로 기록 — 이상적 -8% 체결을 가정하면 손절의 효과를
        과대평가해 회고가 틀린 권고를 하게 된다(종가 기준이라 장중 터치는 애초에 못 잡는다).
        cap_tp(A15): 익절 쪽은 반대 방향의 편향이 있다 — 갭상승 종가(+20%)를 그대로 기록하면
        '+12% 지정가 익절 룰'의 성과를 과대계상한다(실전은 +12 근처 체결). cap_tp=True 면 익절
        도달 시 min(실제 종가, tp_pct) 로 캡. 손절 쪽은 그대로(하방 정직성 유지).
        """
        if not matured:
            return None
        for k in range(1, horizon + 1):
            if k >= len(rets):
                break
            if rets[k] <= stop_pct or (tp_pct is not None and rets[k] >= tp_pct):
                r = rets[k]                    # 이상적 체결가가 아니라 실제 종가
                if cap_tp and tp_pct is not None and r >= tp_pct:
                    r = min(r, tp_pct)         # A15: 익절만 지정가 체결 가정(상방 캡)
                return round(r, 2)
        return round(rets[horizon], 2) if horizon < len(rets) else None

    ret_stop8 = _counterfactual(-8.0)
    ret_stop8_tp12 = _counterfactual(-8.0, 12.0)   # auto stock strategy_config 의 실제 룰(-8/+12)
    ret_stop8_tp12_cap = _counterfactual(-8.0, 12.0, cap_tp=True)  # A15 상방편향 보정판(비교용 병존)

    def _ratio(x):
        return round(x / avg_vol, 2) if (x and avg_vol and avg_vol > 0) else None

    # ── 투자자별 순매수(억원) ── 보유기간 동안 외국인/기관/개인이 얼마나 사고 팔았나(=왜 떨어졌나)
    inv = {"flow_foreign_eok": None, "flow_inst_eok": None,
           "flow_indiv_eok": None, "flow_source": None}
    if FLOW_OK:
        try:
            begin_s = base_date.strftime("%Y%m%d")
            end_idx = min(start + horizon, len(dates) - 1)
            end_s = dates[end_idx].strftime("%Y%m%d")
            inv = flow.get_investor_flow(ticker, begin_s, end_s)
        except Exception:
            pass

    return {
        "matured": matured, "fwd_days_avail": avail,
        "ret_h_pct": (round(ret_h, 2) if ret_h is not None else None),     # 만기수익(만기행만)
        "ret_partial_pct": ret_partial, "partial_asof_date": partial_asof,  # #5 미만기 부분수익(오독 방지)
        "ret_1": at(1), "ret_3": at(3), "ret_5": at(5), "ret_10": at(10), "ret_20": at(20),
        "peak_gain_pct": round(peak_gain, 2), "days_to_peak": peak_k,
        "post_peak_drawdown_pct": round(post_peak_dd, 2) if post_peak_dd is not None else None,
        "max_drawdown_pct": round(max_dd, 2),
        # #R2 숏 경로 / #R1 알파(지수 차감) / #S2 손절 반사실
        "days_to_trough": worst_k,
        "post_trough_rebound_pct": round(post_trough_rb, 2) if post_trough_rb is not None else None,
        "kospi_ret_h_pct": kospi_rh, "alpha_h_pct": alpha_h,
        "ret_if_stop8_pct": ret_stop8, "ret_if_stop8_tp12_pct": ret_stop8_tp12,
        "ret_if_stop8_tp12_cap_pct": ret_stop8_tp12_cap,
        "profit_take_flag": bool(profit_take),
        "settle_close": settle_close_v, "last_close": last_close_v,
        # 거래량
        "entry_volume": int(entry_vol) if entry_vol else None,
        "avg_volume_20d": int(avg_vol) if avg_vol else None,
        "avg_volume_20d_ex_entry": int(avg_vol_ex) if avg_vol_ex else None,   # #A6 비율 분모(당일 제외)
        "peak_day_vol_ratio": _ratio(peak_vol),     # 고점일 거래량 / 평소(>1.5면 고점에 매물 집중)
        "trough_day_vol_ratio": _ratio(trough_vol),  # 최대낙폭일 거래량 / 평소(>1.5면 큰손 투매)
        # 투자자별 순매수(보유기간 동안, 억원. + 순매수 / - 순매도)
        "flow_foreign_eok": inv.get("flow_foreign_eok"),
        "flow_inst_eok": inv.get("flow_inst_eok"),
        "flow_indiv_eok": inv.get("flow_indiv_eok"),
    }


def _row_for(item, kind, pred_date, base_date, feats, regime):
    code = str(item.get("ticker") or "").strip()
    if not code:
        return None
    entry_ref = acc._safe_float(item.get("entry_ref")) if ACC_OK else None
    try:
        horizon = int(item.get("horizon_days"))
    except Exception:
        horizon = None
    row = {
        "pred_date": pred_date, "kind": kind, "ticker": code,
        "name": item.get("name") or code,
        # #A11(회고 07-17 요청): '[단기스윙]' 대괄호 잔재 정규화 — 분리 집계 방지(acc._norm_tag 재사용)
        "tag": ((acc._norm_tag(item.get("tag")) if (ACC_OK and hasattr(acc, "_norm_tag"))
                 else str(item.get("tag") or "").strip("[] ")) if kind == "pick" else "숏"),
        "timing": item.get("timing"), "horizon": horizon,
        "conviction": acc._safe_float(item.get("conviction")) if ACC_OK else item.get("conviction"),
        "entry_ref": entry_ref, "entry_ref_estimated": bool(item.get("entry_ref_estimated")),
        "preprice": item.get("preprice"),
        "thesis": (str(item.get("thesis") or "")[:200]),
    }
    # 피처(추천 시점 스냅샷)
    f = feats.get(code, {})
    for col in FEATURE_COLS:
        if col == "regime_label":
            row[col] = regime.get("label") if isinstance(regime, dict) else None
        elif col == "regime_score":
            row[col] = regime.get("score") if isinstance(regime, dict) else None
        elif col in SNAPSHOT_MARKET_COLS:
            # #S1 시장수준(그날 모든 픽 공통) — 세션 동결 스냅샷에서 온 값이라 regime 에서 읽는다.
            # (종목별 feats(f) 가 아니다 — 여기서 f.get 을 쓰면 전부 None 이 되어 조용히 무효화된다.)
            row[col] = regime.get(col) if isinstance(regime, dict) else None
        else:
            row[col] = f.get(col)
    # 진입시점 피처(룩어헤드 없음) — 추천일까지의 큰손 분배/공매도/국면(진입규칙용 예측 피처)
    # #C1: 세션 스냅샷(feats[code]의 _snap_pre_*) 우선, 없을 때만 pykrx 라이브 폴백
    pre = pre_entry_features(code, base_date, f)
    for col in PRE_COLS:
        row[col] = pre.get(col)
    # 라벨(이후 결과)
    if entry_ref and horizon and base_date is not None:
        lab = compute_labels(code, base_date, entry_ref, horizon)
    else:
        lab = {"matured": None, "note": "missing_entry_or_horizon"}
    for col in LABEL_COLS:
        row[col] = lab.get(col)
    # #A6(회고 07-16 요청): 외인/개인 수급을 '유동성 대비 비율'로 정규화.
    #   메가캡(삼성전자·SK하이닉스)은 5일 외인 -84,817억·개인 +80,173억 같은 규모가 상시 찍혀
    #   부호 기반 분배신호(foreign<0 & indiv>0)가 '거래 구조 그 자체'로 자동 참이 되어 신호가 희석된다
    #   (회고 실측: 메가캡 6행 alpha -1.00%=신호없음 vs ex-메가캡 4행 -11.57%=신호강함).
    #   분모 = 진입 직전 20일 평균 '일 거래대금'(억원) ≈ avg_volume_20d × entry_ref / 1e8.
    #   → 이 비율이 있어야 '분배 합류' 규칙을 메가캡 왜곡 없이 검증·승격할 수 있다(2회 좌절한 축).
    row["pre_avg_trade_value_20d_eok"] = None
    for col in ("pre_foreign_5d_ratio", "pre_indiv_5d_ratio"):
        row[col] = None
    try:
        _avgv, _px = lab.get("avg_volume_20d_ex_entry"), entry_ref   # #A6 당일 제외 분모
        if _avgv and _px and _px > 0:
            tv_eok = (float(_avgv) * float(_px)) / 1e8      # 20일 평균 일거래대금(억원)
            if tv_eok > 0:
                row["pre_avg_trade_value_20d_eok"] = round(tv_eok, 1)
                for col, src in (("pre_foreign_5d_ratio", "pre_foreign_5d_eok"),
                                 ("pre_indiv_5d_ratio", "pre_indiv_5d_eok")):
                    v = row.get(src)
                    if v is not None:
                        row[col] = round(float(v) / tv_eok, 2)   # 5일 순매수 / 평균 일거래대금(배)
    except Exception:
        pass
    # hit: 픽=상승, 숏=하락 (만기/부분 무관하게 ret_h 기준)
    rh = lab.get("ret_h_pct")
    if rh is None:
        row["hit"] = None
    else:
        row["hit"] = (rh < 0) if kind == "short" else (rh > 0)
    row["_note"] = lab.get("note")
    # 분배성 공시 매칭(#4) — 만기행만(DART 호출 절약), 토글 ON 일 때
    if DART_ENRICH and lab.get("matured") and base_date is not None:
        try:
            dd = holding_distribution(code, base_date, horizon)
            for k in ("dist_disc_count", "dist_disc_cats", "dist_disc_titles"):
                if k in dd:
                    row[k] = dd[k]
        except Exception:
            pass
    # #E 메타(현재 시총·시장 — 버킷 분류 전용, 수익률 회귀 금지)
    try:
        _cap, _mkt = _cap_market_of(code)
        row["market_cap_eok"] = _cap
        row["cap_bucket"] = _cap_bucket(_cap)
        row["exchange"] = (_mkt or None)
    except Exception:
        row["market_cap_eok"] = row["cap_bucket"] = row["exchange"] = None
    # #6 rec_id/parent — (ticker,date,kind)=parent(한 추천), +horizon=rec_id(행). 다중horizon·중복 인지용.
    row["parent_rec_id"] = "%s_%s_%s" % (code, pred_date, kind)
    row["rec_id"] = "%s_h%s" % (row["parent_rec_id"], horizon if horizon else "NA")
    # #10 label_status enum — matured 3값(True/False/None) 모호 해소.
    m = lab.get("matured")
    row["label_status"] = "matured" if m is True else ("maturing" if m is False else "no_label")
    # #7 외인수급 단위 sanity(억원 가정). 단일 종목 5일 |50조|·20일 |100조| 초과면 단위오류 의심 → flag(클리핑 아님).
    f5, f20 = row.get("pre_foreign_5d_eok"), row.get("pre_foreign_20d_eok")
    row["flow_unit_check"] = "suspect" if ((f5 is not None and abs(f5) > 500000)
                                           or (f20 is not None and abs(f20) > 1000000)) else "ok"
    return row


def build_rows():
    if not ACC_OK:
        return [], 0
    # #A5 캐리포워드: 이전 dataset 의 pre_* 값을 재사용할 사전(rec_id -> row).
    # pre_* 는 '그 시점의 사실'이라 한번 계산되면 불변 — 새벽(KRX 취약 시간) 재실행에서 라이브 폴백이
    # 실패해도 과거에 성공한 값을 잃지 않는다(archive 행 pre_* 커버리지가 회차마다 출렁이던 원인 제거).
    prev_pre = {}
    try:
        _prev = _load_json(DATASET_JSON)
        if isinstance(_prev, dict):
            for pr in _prev.get("rows", []) or []:
                rid = pr.get("rec_id")
                if rid:
                    prev_pre[rid] = pr
    except Exception:
        prev_pre = {}
    preds = acc.load_predictions()
    # 6/18 이전 발송 리포트(predictions.json 없음)도 파싱해 추가(표본 확대, _src_kind='archive')
    n_arch = 0
    if ARCH_OK:
        try:
            pred_dates = {str(p.get("date") or "") for p in preds}
            arch = archive.load_archive_predictions(skip_dates=pred_dates)
            preds = preds + arch
            n_arch = len(arch)
        except Exception as e:
            log.warning("[retro] 아카이브 파싱 실패(무시): %s", e)
    rows = []
    for pred in preds:
        pred_date = str(pred.get("date") or "")
        base_date = acc._to_date(pred_date)
        src = pred.get("_src") or ""
        session_dir = os.path.dirname(src) if src else ""
        feats, regime = load_signal_snapshot(session_dir) if session_dir else ({}, {})
        src_kind = pred.get("_src_kind", "prediction")
        for kind, key in (("pick", "picks"), ("short", "shorts")):
            for item in (pred.get(key) or []):
                try:
                    r = _row_for(item, kind, pred_date, base_date, feats, regime)
                    if r:
                        r["_src_kind"] = src_kind
                        rows.append(r)
                except Exception as e:
                    log.warning("[retro] 행 생성 예외(무시) %s: %s",
                                item.get("ticker"), type(e).__name__)
    # #A5: 이번 실행에서 None 인 pre_* 를 이전 dataset 값으로 복원(점시점 사실 — 룩어헤드 없음)
    carried = 0
    if prev_pre:
        for r in rows:
            old = prev_pre.get(r.get("rec_id"))
            if not old:
                continue
            for col in PRE_COLS:
                if r.get(col) is None and old.get(col) is not None:
                    r[col] = old[col]
                    carried += 1
    if carried:
        log.info("[retro] pre_* 캐리포워드: 이전 dataset 에서 %d개 값 복원", carried)
    if n_arch:
        log.info("[retro] 아카이브 리포트 %d일 추가 채점 포함", n_arch)
    # #A10(회고 07-17 요청): market_call 원본을 회고에 전달 — 12회 내내 미조명이던 최악 지표(T+5 23%)의
    # 원인 분석(§3.15) 입력. predictions 를 이미 전부 로드했으므로 여기서 부산물로 모아 별도 파일로 저장.
    try:
        calls = []
        for pred in preds:
            mc = pred.get("market_call")
            if isinstance(mc, dict):
                calls.append({"date": pred.get("date"), "_src_kind": pred.get("_src_kind", "prediction"),
                              "kospi": mc.get("kospi"), "kosdaq": mc.get("kosdaq"),
                              "regime": pred.get("regime")})
        if calls:
            _save_json_atomic(os.path.join(HERE, "market_calls.json"),
                              {"generated_at": datetime.now().isoformat(timespec="seconds"),
                               "what": "일자별 market_call 원본(kospi/kosdaq dir·conviction·invalidation) — 회고 §3.15 입력",
                               "n": len(calls), "calls": calls})
            log.info("[retro] market_calls.json 저장(%d일) — 회고 §3.15 입력", len(calls))
    except Exception as e:
        log.warning("[retro] market_calls 저장 실패(무시): %s", type(e).__name__)
    # #8 ticker_rec_seq — 같은 종목 N번째 추천(표본 비독립성: 같은 종목 최대 9일 중복). 회고가 종목 클러스터/가중 집계.
    from collections import defaultdict
    _seq = defaultdict(int)
    for r in sorted(rows, key=lambda x: (str(x.get("ticker", "")), str(x.get("pred_date", "")))):
        _seq[r["ticker"]] += 1
        r["ticker_rec_seq"] = _seq[r["ticker"]]
    return rows, len(preds)


from common import save_json_atomic as _cm_save_json  # common.py 통합


def _save_json_atomic(path, obj):
    _cm_save_json(path, obj, fsync=True)   # fsync: 전원/킬 시 빈·잘린 .tmp 방지(무결성 유지)


def _save_csv(path, rows):
    cols = BASE_COLS + FEATURE_COLS + PRE_COLS + META_COLS + LABEL_COLS + ENRICH_COLS + ["hit"]
    # 원자적 저장(.tmp -> os.replace): 쓰는 중 종료(타임아웃 taskkill 등)에도 부분 CSV 가 남지 않게.
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("[retro] csv 저장 실패(무시): %s", e)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


REQUIRED_COLS = ["pred_date", "ticker", "kind", "horizon", "entry_ref"]   # 필수 not-null(요청서 부록B)


def _verify_saved(json_path, csv_path, n_expected):
    """저장 무결성 게이트(P0, 요청서 부록B): JSON 재파싱·rows 수, CSV 논리행 수 == n_expected,
    필수컬럼 not-null 100% + entry_ref>0, matured⇒ret_h not-null/matured아님⇒ret_h null.
    모두 통과해야 True(→ 호출부가 RETRO_GO 생성). 하나라도 실패면 False."""
    try:
        with open(json_path, encoding="utf-8") as f:
            jd = json.load(f)
        rows = jd.get("rows", [])
        if len(rows) != n_expected:
            return False
    except Exception:
        return False
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            n_csv = sum(1 for _ in csv.reader(f)) - 1  # 헤더 제외(csv.reader=논리행)
        if n_csv != n_expected:
            return False
    except Exception:
        return False
    # 필수컬럼 not-null + entry_ref>0
    for r in rows:
        for c in REQUIRED_COLS:
            if r.get(c) in (None, ""):
                return False
        try:
            if float(r.get("entry_ref")) <= 0:
                return False
        except Exception:
            return False
        # 만기/수익 정합: matured==True ⇒ ret_h_pct not-null / matured!=True ⇒ ret_h_pct null
        if r.get("matured") is True and r.get("ret_h_pct") is None:
            return False
        if r.get("matured") is not True and r.get("ret_h_pct") is not None:
            return False
    return True


def main():
    ap = argparse.ArgumentParser(description="회고분석 학습 데이터셋 빌더")
    ap.add_argument("--out", default="", help="JSON 출력경로(기본 retro_dataset.json)")
    ap.add_argument("--no-dart", action="store_true",
                    help="DART 분배공시 매칭(#4) 생략 — 빠른 재생성용")
    args = ap.parse_args()

    global DART_ENRICH
    if args.no_dart:
        DART_ENRICH = False

    rows, n_pred = build_rows()
    n_matured = sum(1 for r in rows if r.get("matured") is True)
    n_pt = sum(1 for r in rows if r.get("profit_take_flag") is True)

    out_json = os.path.abspath(args.out) if args.out else DATASET_JSON
    out_csv = (os.path.splitext(out_json)[0] + ".csv") if args.out else DATASET_CSV

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "what": ("과거 추천(picks/shorts)의 '추천시점 피처 + 이후 실제결과' 학습 데이터셋. "
                 "회고 Cowork 가 '예상대로 왜 안 올랐는지/차익실현 타이밍'을 학습하는 입력."),
        "price_source": "금융위원회(FSC) 공식 종가 우선, 실패 시 FinanceDataReader 폴백",
        "label_guide": {
            "peak_gain_pct": "진입가 대비 기간 내 최대 상승률(%)",
            "days_to_peak": "고점까지 걸린 거래일 수(작을수록 차익실현 빠름)",
            "post_peak_drawdown_pct": "고점 이후 저점까지 되돌림(%) — 차익실현 매물 강도",
            "profit_take_flag": "고점 +%.0f%% 이상 후 -%.0f%% 이상 반납한 '차익실현형 고점'"
                                % (PROFIT_TAKE_MIN_PEAK, -PROFIT_TAKE_GIVEBACK),
            "matured": "horizon 만기 도달 여부(False/None 행은 라벨 부분/없음 — 참고만)",
            "entry_volume": "추천일 거래량",
            "avg_volume_20d": "진입 직전 ~20일 평균 거래량",
            "peak_day_vol_ratio": "고점일 거래량/평소(>1.5면 고점에 매물 집중=차익실현)",
            "trough_day_vol_ratio": "최대낙폭일 거래량/평소(>1.5면 큰손 투매)",
            "days_to_trough": "[라벨·#R2] 저점(최대낙폭)까지 거래일 수 — 숏 익절 타이밍('D+N') 설계용. 픽엔 눌림 깊이 시점",
            "post_trough_rebound_pct": "[라벨·#R2] 저점 이후 만기까지 최대 되돌림(%) — 숏이 익절 없이 버틸 때 반납하는 폭(스퀴즈 강도)",
            "ret_if_stop8_pct": "[라벨·#S2 반사실] -8% 손절을 지켰다면의 만기수익(%). ret_h 와 비교해 '손절이 얼마나 건졌나/승자를 잃었나' 판정(종가 근사 — 장중 터치 미반영)",
            "ret_if_stop8_tp12_pct": "[라벨·#S2 반사실] -8% 손절 + +12% 익절 룰(auto stock 실제 config)을 지켰다면의 수익(%). 익절일 갭상승 종가를 그대로 기록하므로 상방편향 있음(A15) — 룰 성과 평가에는 _cap 판을 쓰라",
            "ret_if_stop8_tp12_cap_pct": "[라벨·#A15] 위와 같되 익절 도달 시 min(실제 종가, +12%) 캡 — '+12% 지정가 체결' 가정. 익절 룰의 실전 성과 평가는 이 컬럼 기준(손절 쪽은 두 컬럼 모두 실제 종가 = 하방 정직)",
            "pre_margin_total_eok": "[진입피처·v9.8] 추천일 시점 신용거래융자 총잔고(억원, 금투협 — 세션 스냅샷). 레버리지 수준 = 반대매매 취약도. 2026-07-21 이후 세션에만 값",
            "pre_margin_d5_chg_pct": "[진입피처·v9.8] 신용융자 잔고 5거래일 변화율(%) — 급증=빚투 과열, 급감=디레버리징(투매 소화) 국면",
            "pre_margin_pct_rank": "[진입피처·v9.8] 신용융자 잔고의 60거래일 백분위(높을수록 역대급 레버리지)",
            "pre_foreign_5d_ratio": "[진입피처·#A6] 직전 5일 외국인 순매수 / 20일 평균 일거래대금(배). 메가캡은 금액이 상시 커서 부호만 보면 신호가 희석된다 → 종목 간 비교는 이 비율로",
            "pre_indiv_5d_ratio": "[진입피처·#A6] 직전 5일 개인 순매수 / 20일 평균 일거래대금(배)",
            "pre_avg_trade_value_20d_eok": "[진입피처·#A6] 진입 직전 20일 평균 일거래대금(억원) — 위 비율의 분모(유동성 규모)",
            "pre_caution_score": "[진입피처·시장] 세션 동결 market_caution 종합 국면점수(0~100, >=60 경계) — 2026-07-17+ 세션만",
            "pre_regime_kind": "[진입피처·시장] 동결 regime_kind(공포/눌림목/과열 등)",
            "pre_allow_market_up": "[진입피처·시장] 동결 allow_market_up_call(false=breadth 붕괴로 지수 up 콜 금지)",
            "pre_pcr_oi": "[진입피처·시장] 동결 KOSPI200 풋콜비율(미결제 기준)",
            "pre_vkospi": "[진입피처·시장] 동결 VKOSPI 수준(공포)",
            "pre_vkospi_pct_rank": "[진입피처·시장] 동결 VKOSPI 60일 백분위",
            "pre_base_rate": "[진입피처·거시] 동결 한국은행 기준금리(%)",
            "pre_usdkrw_chg5d": "[진입피처·거시] 동결 원/달러 5일 변화율(%, +면 원화약세=외인 위험회피)",
            "market_cap_eok": "[메타·#E] 현재 시총(억원) — ⚠️조회시점 값(진입시점 아님). 버킷 분류 전용, 수익률 크기 회귀 금지",
            "cap_bucket": "[메타·#E] 메가(10조+)/대형(1조+)/중형(3천억+)/소형 — '주도주 예외'(반도체·바이오 대장) 정량 검증용(§3.16)",
            "exchange": ("[메타·#E] KOSPI / KOSDAQ / **KOSDAQ GLOBAL**(3종 — categorical_values 참조) "
                         "— 시장별 분해(B6/B14: 대형주 레짐과 코스닥은 따로 논다 검증용). "
                         "★코스닥 집계는 KOSDAQ + KOSDAQ GLOBAL 을 합산하라"),
            "avg_volume_20d_ex_entry": "[라벨·#A6] 추천일 '당일 제외' 직전 20일 평균 거래량 — pre_*_ratio 의 분모(룩어헤드 제거판)",
            "kospi_ret_h_pct": "[라벨·#R1] 같은 보유창(진입일 종가->T+h)의 KOSPI 수익률(%) — 시장 기여분",
            "alpha_h_pct": "[라벨·#R1] ret_h - kospi_ret_h = 지수 차감 초과수익(%). 음수 크면 종목선택 실패, ret_h 음수인데 alpha>=0 이면 시장베타가 주범(처방: 픽 억제가 아니라 노출 축소/헤지)",
            "flow_foreign_eok": "[라벨·사후] 보유기간 동안 외국인 순매수(억원, -면 외국인 순매도=하락 압력). 진입규칙 사용 금지(룩어헤드)",
            "flow_inst_eok": "[라벨·사후] 보유기간 동안 기관 순매수(억원, -면 기관 순매도). 진입규칙 사용 금지",
            "flow_indiv_eok": "[라벨·사후] 보유기간 동안 개인 순매수(억원, +면 개인이 받아줌). 진입규칙 사용 금지",
            "pre_foreign_5d_eok": "[진입피처] 추천일까지 직전 5일 외국인 순매수(억원, -면 진입 전부터 외인 분배 중)",
            "pre_foreign_20d_eok": "[진입피처] 추천일까지 직전 20일 외국인 순매수(억원)",
            "pre_inst_5d_eok": "[진입피처] 추천일까지 직전 5일 기관 순매수(억원)",
            "pre_indiv_5d_eok": "[진입피처] 추천일까지 직전 5일 개인 순매수(억원, +면 개인이 받는 중=고점 신호일 수)",
            "pre_foreign_sell_streak": "[진입피처] 추천일까지 외국인 연속 순매도일수(≥3이면 진입 전 분배)",
            "pre_short_balance_ratio": "[진입피처] 추천일 기준 공매도 잔고비중(%)",
            "pre_short_change_10d": "[진입피처] 추천일까지 직전 10거래일 공매도 잔고 증감률(%, +면 공매도 증가)",
            "pre_kospi_ret5d": "[진입피처] 추천일까지 KOSPI 직전 5일 수익률(%) — 진입 국면(음수면 약세장 진입)",
            "sector": "[진입피처] 업종(FDR) — 방어/조선 등 테마 편중 분리·국면 의존성 분석용",
            "pre_rsi14": "[진입피처] 추천일까지 RSI14(>70 과열) — 즉시고점 시그니처 검증용(소급 계산)",
            "pre_up_streak": "[진입피처] 추천일까지 연속 상승일수(≥5 과열)",
            "pre_ret_20d_pct": "[진입피처] 추천일까지 직전 20일 수익률(%) — 단기 급등 추격 여부",
            "pre_dist_52w_high_pct": "[진입피처] 52주 고가 이격(%, 0 근접=신고가권=차익실현 매물 위)",
            "pre_overheat": "[진입피처] 과열 프록시 0~100(RSI·신고가근접·연속상승·급등 합성) — overheat.json 없는 행 보강",
            "pre_disparity20": "[진입피처] 20일 이평 이격도(%) — 단기 과열(죽은 disparity20 컬럼을 소급 백필, #2)",
            "label_status": "[상태·#10] matured(만기·ret_h 유효)/maturing(미만기·ret_partial 참조)/no_label(라벨없음) — matured 3값 모호 해소",
            "ret_partial_pct": "[미만기·#5] 진입~최신가 부분수익(%). 만기수익(ret_h) 아님 — T+N 만기로 오독 금지",
            "partial_asof_date": "[미만기·#5] ret_partial 기준 최신 종가 날짜",
            "last_close": "[미만기·#5] 최신 종가(만기 전). 만기행은 settle_close 사용",
            "rec_id": "[식별·#6] 행 고유키 ticker_date_kind_horizon",
            "parent_rec_id": "[식별·#6] ticker_date_kind — 같은 추천의 다중 horizon 행을 묶어 중복 인지",
            "ticker_rec_seq": "[식별·#8] 같은 종목 N번째 추천(같은 종목 최대 9일 중복 → 종목 클러스터로 가중집계)",
            "flow_unit_check": "[위생·#7] 외인수급 단위 sanity(ok/suspect). 억원 가정, 단일종목 5일|50조|·20일|100조| 초과면 suspect",
            "dist_disc_count": "[라벨·사후] 보유기간 중 분배성 공시(증자/CB/대주주·대량보유 변동) 건수 — '거래량 클라이맥스=분배' 가설 검증(차익실현형과 교차)",
        },
        "feature_cols": FEATURE_COLS + PRE_COLS,
        "pre_entry_feature_cols": PRE_COLS,
        "feature_note": ("pre_* = 진입시점에 알 수 있는 예측 피처(진입규칙 사용 가능). "
                         "flow_*_eok = 보유기간 사후 라벨(진입규칙 사용 금지=룩어헤드)."),
        "n_predictions": n_pred,
        "n_rows": len(rows),
        "n_matured": n_matured,
        "n_profit_take": n_pt,
        "n_archive_rows": sum(1 for r in rows if r.get("_src_kind") == "archive"),
        # ★국면 스냅샷 커버리지(v10.0): 세션에 signals_snapshot_* 이 동결되지 않으면 pre_regime_kind
        #   등 F1/F8 국면 입력이 통째로 빈다. 그 사실이 '조용히' 지나가면 회고가 결측을 결함으로
        #   오인하거나(2026-07-24 회차) 반대로 못 알아챈다(07-19 누락은 6일간 미발견).
        #   → pred_date 별 커버리지를 메타로 노출해 회고가 즉시 판정하게 한다.
        "snapshot_coverage": _snapshot_coverage(rows),
        # ★A19: 범주형 허용값 — 분석이 접두/부분 매칭으로 추측하지 않도록 명시.
        "categorical_values": {
            "cap_bucket": CAP_BUCKETS,
            "exchange": EXCHANGES,
            "timing": ["임박", "단기", "중기"],
            "kind": ["pick", "short"],
            "label_status": ["matured", "maturing", "no_label"],
            "_src_kind": ["prediction", "archive"],
            "note": ("문자열 완전일치로 집계하라. ★'KOSDAQ GLOBAL' 은 코스닥 소속이므로 "
                     "코스닥 집계에 반드시 포함시켜라(과거 회차가 이를 빠뜨려 코스닥 열위를 "
                     "과장했다). cap_bucket 도 '메가' 접두가 아니라 '메가(10조+)' 전체와 비교하라."),
        },
        # #8 종목 단위 유효표본(비독립성) — 명목 N(rows)보다 신뢰구간이 좁게 과대평가되지 않게.
        "n_unique_tickers": len({r.get("ticker") for r in rows if r.get("ticker")}),
        "src_kind_weight": {"prediction": 1.0, "archive": 0.6},   # #3 회고가 archive를 한 단계 낮춰 가중
        "src_kind_note": ("_src_kind='prediction'=구조화 예측(6/18+, 정밀, 가중 1.0). "
                          "_src_kind='archive'=6/18 이전 .md 파싱+FSC 채점(파싱·생존 편향 가능, 가중 0.6) — 표본 보강용."),
        # #2 죽은 컬럼 안내: 아래 옛 신호컬럼은 신호파일이 동반된 2026-06-23+ 추천에만 값이 있고 그 이전·archive 엔 null.
        #    분석 1차 피처는 pre_*(소급 백필, 만기행 100%)로 일원화하라. 옛 컬럼은 prediction 만기 표본이 쌓일 때 보조검증.
        "prediction_only_cols": ["overheat_score", "disparity20", "disparity60", "up_streak",
                                 "dist_52w_high_pct", "ret_20d_pct", "rsi14", "obv_divergence",
                                 "overhang_score", "short_balance_ratio", "short_pressure_score",
                                 "per", "pbr", "foreign_hold_pct"],
        "small_sample_warning": (
            "matured(label_status=matured) 행으로 패턴을 수치 검증하라. 같은 종목 중복추천이 많으니 n_unique_tickers·"
            "ticker_rec_seq 로 종목 클러스터 가중. archive 행은 가중 0.6. 미만기는 ret_partial(만기수익 아님)."),
        "rows": rows,
        "disclaimer": "내부 회고용 추정 데이터. 투자자문 아님.",
    }
    try:
        _save_json_atomic(out_json, payload)
        _save_csv(out_csv, rows)
        # 저장 후 무결성 검증(P0): 디스크의 JSON/CSV 행수가 메타와 일치하는지 확인 → push 전 truncation 조기 탐지.
        ok = _verify_saved(out_json, out_csv, len(rows))
        log.info("[retro] 저장: %s / %s", out_json, out_csv)
        log.info("[retro] 예측일 %d개 · 행 %d개(만기도달 %d · 차익실현형 %d) · 무결성=%s",
                 n_pred, len(rows), n_matured, n_pt, "OK" if ok else "불일치(경고)")
        # ★국면 스냅샷 누락을 '그날' 알린다(07-19 누락이 6일간 미발견됐던 사고 방지).
        _miss = (payload.get("snapshot_coverage") or {}).get("dates_missing_after_start") or []
        if _miss:
            log.warning("[retro] ★국면 스냅샷 누락 pred_date %d일: %s "
                        "— 그날 snapshot_signals 미실행(소급 복구 불가). "
                        "회고의 국면별 분해가 그만큼 빈다.", len(_miss), ", ".join(_miss))
        if not ok:
            log.warning("[retro] 저장 무결성 불일치 — 다음 push 가 검증에서 막을 수 있음")
    except Exception as e:
        # #A2: 저장 실패는 exit 3 — supervisor 의 rc 게이트가 push 를 생략하게(낡은 dataset 전달 방지).
        # 예전엔 여기서도 0을 반환해 '저장 실패 + 정상 push'라는 무음 실패가 가능했다.
        log.warning("[retro] 저장 실패(exit 3 — push 생략 유도): %s", e)
        return 3
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        # #A2: 치명 예외도 exit 3 — dataset 이 갱신되지 않았으므로 push 하면 안 된다.
        log.warning("[retro] 치명적 예외(exit 3): %s: %s", type(e).__name__, e)
        sys.exit(3)
