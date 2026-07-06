#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
retro_label.py ─ 회고분석(PART C) 학습 데이터셋 빌더

[목적]
  과거에 우리 분석 Cowork 가 추천한 종목(predictions.json 의 picks/shorts)이 '예상대로 왜 안
  올랐는지'를 학습하기 위한 데이터셋을 만든다. 각 추천에 대해
    (A) 추천 시점의 피처 스냅샷 : 그날 세션의 force_scores/overheat/disclosures/short/kis_data,
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
    """추천일까지 KOSPI(KS11) 직전 5거래일 수익률(%) — 진입시점 국면 프록시(룩어헤드 없음)."""
    if not FDR_OK or base_date is None:
        return None
    key = base_date.strftime("%Y%m%d")
    if key in _KOSPI_CACHE:
        return _KOSPI_CACHE[key]
    val = None
    try:
        df = fdr.DataReader("KS11", (base_date - timedelta(days=16)).strftime("%Y-%m-%d"),
                            base_date.strftime("%Y-%m-%d"))
        c = [float(x) for x in df["Close"].tolist() if x == x]
        if len(c) >= 6:
            val = round((c[-1] / c[-6] - 1.0) * 100, 2)
    except Exception:
        val = None
    _KOSPI_CACHE[key] = val
    return val


def pre_entry_features(ticker, base_date):
    """진입시점(추천일까지) 피처 — 룩어헤드 없음. '진입 전부터 큰손이 분배 중이었나'를 예측 피처로.
      pre_foreign_5d_eok/20d_eok·pre_inst_5d_eok·pre_indiv_5d_eok·pre_foreign_sell_streak(KRX 일별 수급) +
      pre_short_balance_ratio·pre_short_change_10d(공매도 잔고) + pre_kospi_ret5d(국면)."""
    out = {}
    if base_date is None:
        return out
    asof = base_date.strftime("%Y%m%d")
    if FLOW_OK:
        try:
            out.update(flow.get_flow_asof(ticker, asof) or {})
        except Exception:
            pass
    if SHORT_OK:
        try:
            out.update(short.get_short_asof(ticker, asof) or {})
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


def _sector_of(code):
    """code → 업종(Sector) 문자열. FDR StockListing('KRX') 1회 로드 캐시. 없으면 None."""
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
    # 진입시점 기술피처(P1b 백필) — §3 과열/즉시고점 가설 검증용
    "pre_rsi14", "pre_up_streak", "pre_ret_20d_pct", "pre_dist_52w_high_pct", "pre_disparity20", "pre_overheat",
]
# 라벨side 보강 컬럼(보유 후 결과 — 진입규칙 사용 금지). cats/titles 도 CSV 에 포함(JSON 과 일관, 조용한 드롭 방지)
ENRICH_COLS = ["dist_disc_count", "dist_disc_cats", "dist_disc_titles"]
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
    # disclosures / short / kis
    "overhang_score", "short_balance_ratio", "short_pressure_score", "short_trend",
    "per", "pbr", "foreign_hold_pct",
    # market-level
    "regime_label", "regime_score",
]
LABEL_COLS = [
    "matured", "label_status", "fwd_days_avail", "ret_h_pct",
    # #5 미만기 부분수익(만기 ret_h 와 분리 — 오독 방지)
    "ret_partial_pct", "partial_asof_date", "last_close",
    "ret_1", "ret_3", "ret_5", "ret_10", "ret_20",
    "peak_gain_pct", "days_to_peak", "post_peak_drawdown_pct", "max_drawdown_pct",
    "profit_take_flag", "hit", "settle_close",
    # 거래량(차익실현·큰손 매도 신호)
    "entry_volume", "avg_volume_20d", "peak_day_vol_ratio", "trough_day_vol_ratio",
    # 투자자별 순매수(보유기간, 억원) — 누가 사고 팔았나
    "flow_foreign_eok", "flow_inst_eok", "flow_indiv_eok",
]
BASE_COLS = [
    # 식별자(#6/#8) — 중복·다중horizon·종목클러스터 인지용
    "rec_id", "parent_rec_id", "ticker_rec_seq",
    "pred_date", "kind", "ticker", "name", "tag", "timing", "horizon",
    "conviction", "entry_ref", "preprice", "flow_unit_check", "thesis",
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
    # short.json
    sh = _by_ticker(_load_json(os.path.join(session_dir, "short.json")))
    for code, t in sh.items():
        f = ensure(code)
        f.update({"short_balance_ratio": t.get("short_balance_ratio"),
                  "short_pressure_score": t.get("short_pressure_score"),
                  "short_trend": t.get("trend")})
    # kis_data.json
    ks = _by_ticker(_load_json(os.path.join(session_dir, "kis_data.json")))
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
    return feats, regime


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
    pre_vols = [x for x in vols[max(0, start - 20):start + 1] if x is not None]  # 진입 직전까지 ~20일
    avg_vol = (sum(pre_vols) / len(pre_vols)) if pre_vols else None
    entry_vol = _v(start)
    peak_vol = _v(start + peak_k)
    worst_k = min(range(1, len(rets)), key=lambda k: rets[k]) if len(rets) > 1 else 0
    trough_vol = _v(start + worst_k)

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
        "profit_take_flag": bool(profit_take),
        "settle_close": settle_close_v, "last_close": last_close_v,
        # 거래량
        "entry_volume": int(entry_vol) if entry_vol else None,
        "avg_volume_20d": int(avg_vol) if avg_vol else None,
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
        "tag": (item.get("tag") if kind == "pick" else "숏"),
        "timing": item.get("timing"), "horizon": horizon,
        "conviction": acc._safe_float(item.get("conviction")) if ACC_OK else item.get("conviction"),
        "entry_ref": entry_ref, "preprice": item.get("preprice"),
        "thesis": (str(item.get("thesis") or "")[:200]),
    }
    # 피처(추천 시점 스냅샷)
    f = feats.get(code, {})
    for col in FEATURE_COLS:
        if col == "regime_label":
            row[col] = regime.get("label") if isinstance(regime, dict) else None
        elif col == "regime_score":
            row[col] = regime.get("score") if isinstance(regime, dict) else None
        else:
            row[col] = f.get(col)
    # 진입시점 피처(룩어헤드 없음) — 추천일까지의 큰손 분배/공매도/국면(진입규칙용 예측 피처)
    pre = pre_entry_features(code, base_date)
    for col in PRE_COLS:
        row[col] = pre.get(col)
    # 라벨(이후 결과)
    if entry_ref and horizon and base_date is not None:
        lab = compute_labels(code, base_date, entry_ref, horizon)
    else:
        lab = {"matured": None, "note": "missing_entry_or_horizon"}
    for col in LABEL_COLS:
        row[col] = lab.get(col)
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
    if n_arch:
        log.info("[retro] 아카이브 리포트 %d일 추가 채점 포함", n_arch)
    # #8 ticker_rec_seq — 같은 종목 N번째 추천(표본 비독립성: 같은 종목 최대 9일 중복). 회고가 종목 클러스터/가중 집계.
    from collections import defaultdict
    _seq = defaultdict(int)
    for r in sorted(rows, key=lambda x: (str(x.get("ticker", "")), str(x.get("pred_date", "")))):
        _seq[r["ticker"]] += 1
        r["ticker_rec_seq"] = _seq[r["ticker"]]
    return rows, len(preds)


def _save_json_atomic(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())   # #1 전원/킬 시 빈·잘린 .tmp 방지(디스크 flush 후 rename)
    os.replace(tmp, path)


def _save_csv(path, rows):
    cols = BASE_COLS + FEATURE_COLS + PRE_COLS + LABEL_COLS + ENRICH_COLS + ["hit"]
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
        if not ok:
            log.warning("[retro] 저장 무결성 불일치 — 다음 push 가 검증에서 막을 수 있음")
    except Exception as e:
        log.warning("[retro] 저장 실패: %s", e)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        log.warning("[retro] 치명적 예외(무시): %s: %s", type(e).__name__, e)
        sys.exit(0)
