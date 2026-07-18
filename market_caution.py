# -*- coding: utf-8 -*-
r"""
market_caution.py — 시장 국면(regime) 복합 게이트 (회고 P2a)

[목적] 회고 최강 발견("결과는 종목보다 '추천한 날의 시장 과열'이 좌우")을 운영화한다.
  단일 신호가 아니라 4개를 합성해 'market_caution'(0~100) 점수를 만든다:
    ① KOSPI 직전 5일 수익률(과열 추격 = 회고 1순위, pre_kospi_ret5d>=+5% → 적중 24%)
    ② 파생 PCR(deriv_sentiment.json: 풋 우위 = 헤지/위험회피)
    ③ 외국인 수급 risk_off(flow_data.json)
    ④ 원/달러 5일 변화(ecos_macro.json: 원화 약세 = 외인 이탈 맥락)
  → market_caution.json. 높을수록 그날 신규 롱은 보수적(분할·보류·즉시익절·추천수 축소).

[설계] 이미 만든 세션 산출물을 읽어 합성(추가 수집 최소). 파일 없으면 그 항목만 건너뜀(graceful).
  requests/FDR 선택. 원자적 저장, ASCII 태그([caution]), 이모지 금지, UTF-8.

[사용법] python market_caution.py
"""
import os
import sys
import json
import glob
import logging
from datetime import datetime, timedelta, date

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("caution")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DEFAULT = os.path.join(HERE, "market_caution.json")

try:
    import FinanceDataReader as fdr
    FDR_OK = True
except Exception:
    fdr = None
    FDR_OK = False


def _latest_json(name):
    """루트 → output/<날짜>/ → output/_archive/<날짜>/ 에서 name 의 '최신'을 로드. 없으면 {}."""
    cands = []
    for pat in (os.path.join(HERE, name),
                os.path.join(HERE, "output", "*", name),
                os.path.join(HERE, "output", "_archive", "*", name)):
        for p in glob.glob(pat):
            if "_discarded" in p:
                continue
            parent = os.path.basename(os.path.dirname(p))
            datekey = parent[:10] if (len(parent) >= 10 and parent[4:5] == "-") else ""
            try:
                mt = os.path.getmtime(p)
            except Exception:
                mt = 0
            cands.append((datekey, mt, p))
    if not cands:
        return {}
    cands.sort(reverse=True)
    try:
        with open(cands[0][2], encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        return {}


def kospi_ret5d():
    """KOSPI 직전 5거래일 수익률(%). 실패 시 None."""
    if not FDR_OK:
        return None
    try:
        end = datetime.now()
        df = fdr.DataReader("KS11", (end - timedelta(days=16)).strftime("%Y-%m-%d"),
                            end.strftime("%Y-%m-%d"))
        c = [float(x) for x in df["Close"].tolist() if x == x]
        if len(c) >= 6:
            return round((c[-1] / c[-6] - 1.0) * 100, 2)
    except Exception:
        pass
    return None


# KRX 세션(breadth용) — flow_collect import 가 KRX 로그인. 선택.
try:
    import flow_collect  # noqa: F401
    from pykrx import stock as _krx_stock
    _KRX = True
except Exception:
    _krx_stock = None
    _KRX = False


def breadth():
    """오늘(거꾸로 탐색) KOSPI 상승/하락 종목수 + 하락배수(declines/advances). 회고: 시장 콜이
    단일 촉매로 UP 되는 걸 막는 핵심 — breadth 붕괴면 공포 국면(롱 회피). 실패 시 None."""
    if not _KRX:
        return None
    for back in range(6):
        dd = (date.today() - timedelta(days=back)).strftime("%Y%m%d")
        try:
            df = _krx_stock.get_market_price_change(dd, dd, market="KOSPI")
        except Exception:
            continue
        if df is None or len(df) == 0:
            continue
        col = next((c for c in df.columns if "등락" in c or "변동" in c), None)
        if not col:
            return None
        up = int((df[col] > 0).sum())
        down = int((df[col] < 0).sum())
        ratio = round(down / up, 2) if up > 0 else None
        return {"date": dd, "advances": up, "declines": down, "decline_ratio": ratio}
    return None


def _age_h(payload):
    """입력 dict 의 generated_at 나이(시간). 결측·파싱 실패 None(graceful 관례 유지)."""
    try:
        g = payload.get("generated_at")
        if not g:
            return None
        return round((datetime.now() - datetime.fromisoformat(str(g)[:19])).total_seconds() / 3600.0, 1)
    except Exception:
        return None


def compute(out_path):
    flow = _latest_json("flow_data.json")
    deriv = _latest_json("deriv_sentiment.json")
    ecos = _latest_json("ecos_macro.json")
    # #M2 입력 신선도: _latest_json 은 (폴더날짜, mtime) 정렬이라 루트가 최신이어도 낡은 세션 사본이
    # 이길 수 있다 — 어느 입력이 하루 이상 묵었는지 산출물에 드러내(분석가가 해당 축 가중을 낮추게).
    inputs_age_h = {"flow_data": _age_h(flow), "deriv_sentiment": _age_h(deriv),
                    "ecos_macro": _age_h(ecos)}
    stale_inputs = [k for k, v in inputs_age_h.items() if v is not None and v > 24.0]

    k5 = kospi_ret5d()
    pcr_oi = ((deriv.get("market") or {}).get("pcr_oi"))
    risk_off = ((flow.get("risk_off") or {}).get("score"))
    usdkrw_5d = ((ecos.get("derived") or {}).get("usdkrw_change_5d_pct"))
    br = breadth()
    dratio = (br or {}).get("decline_ratio")

    score, drivers = 0.0, []
    # ⓪ ★breadth(하락배수) — 회고 핵심: 시장 콜이 단일 촉매로 UP 되는 걸 막는다. 하락종목이 상승의 2배↑면 공포.
    fear_breadth = False
    if dratio is not None:
        if dratio >= 2.0:
            score += 30
            fear_breadth = True
            drivers.append(f"breadth 붕괴(하락 {br['declines']} vs 상승 {br['advances']}, {dratio}배)=공포")
        elif dratio >= 1.5:
            score += 15
            drivers.append(f"breadth 약세(하락 {br['declines']} vs 상승 {br['advances']})")
        elif dratio <= 0.6:
            drivers.append(f"breadth 양호(상승 {br['advances']} vs 하락 {br['declines']})")
    # ① KOSPI 과열(회고 1순위): +5%↑ 강한 가중, 구간 비례
    if k5 is not None:
        if k5 >= 5:
            score += 35
            drivers.append(f"KOSPI5일 +{k5}%(과열 추격 위험)")
        elif k5 >= 3:
            score += 18
            drivers.append(f"KOSPI5일 +{k5}%(상승 추격 주의)")
        elif k5 <= 0 and not fear_breadth:
            drivers.append(f"KOSPI5일 {k5}%(완만한 눌림 — 역추세 진입 우호)")
        elif k5 <= 0 and fear_breadth:
            drivers.append(f"KOSPI5일 {k5}%(단, breadth 붕괴=공포 — 눌림 아님, 롱 회피)")
    # ② 파생 PCR(풋 우위 = 헤지)
    if pcr_oi is not None:
        if pcr_oi >= 1.5:
            score += 25
            drivers.append(f"옵션 PCR(OI) {pcr_oi}(풋 강우위=헤지)")
        elif pcr_oi >= 1.2:
            score += 15
            drivers.append(f"옵션 PCR(OI) {pcr_oi}(풋 우위)")
    # ③ 외국인 risk_off
    if isinstance(risk_off, (int, float)):
        score += float(risk_off) * 0.25
        if risk_off >= 60:
            drivers.append(f"외국인 risk_off {int(risk_off)}")
    # ④ 원화 약세
    if usdkrw_5d is not None and usdkrw_5d >= 1.0:
        score += 15
        drivers.append(f"원/달러 5일 +{usdkrw_5d}%(원화 약세)")

    score = round(max(0.0, min(100.0, score)), 1)
    if score >= 60:
        label = "경계(신규 롱 보수적)"
    elif score >= 35:
        label = "주의"
    else:
        label = "우호"
    # 국면 종류(회고 D): 공포(breadth붕괴/고PCR) vs 눌림목(완만한 하락) 구분 — 둘은 롱 대응이 정반대.
    if fear_breadth or (pcr_oi is not None and pcr_oi >= 1.5):
        regime_kind = "공포(롱 회피·falling knife)"
    elif (k5 is not None and k5 <= 0) and score < 35:
        regime_kind = "눌림목(역추세 롱 기회)"
    elif (k5 is not None and k5 >= 5):
        regime_kind = "과열(추격 위험)"
    else:
        regime_kind = "중립"
    # 시장 UP 콜 허용 여부(회고 A·C): breadth 붕괴/경계면 '단일 촉매 UP 콜' 금지 신호.
    allow_market_up = not (fear_breadth or score >= 60)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "market_caution_score": score,
        "label": label,
        "regime_kind": regime_kind,
        "allow_market_up_call": allow_market_up,
        "drivers": drivers,
        "inputs": {"kospi_ret5d": k5, "pcr_oi": pcr_oi, "risk_off_score": risk_off,
                   "usdkrw_change_5d_pct": usdkrw_5d, "breadth": br},
        "inputs_age_h": inputs_age_h,          # #M2 각 파일입력의 나이(시간). None=결측/라이브
        "stale_inputs": stale_inputs,          # #M2 24h 초과 입력 — 분석가는 해당 축 가중 하향([5.10])
        "_note": ("회고 운영화 복합 국면점수. score>=60 또는 allow_market_up_call=false 면 그날 신규 롱은 "
                  "분할/보류·추천수 축소, 시장 UP 콜 금지(단일 촉매로 올리지 말 것). regime_kind '공포'면 롱 회피·숏 우대, "
                  "'눌림목'이면 역추세 롱 기회. breadth(하락배수)가 단일 촉매 과대가중을 막는 핵심."),
    }
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, out_path)
    log.info("[caution] 저장: %s (score=%s %s | regime=%s up콜허용=%s | breadth=%s KOSPI5=%s PCR=%s)",
             out_path, score, label, regime_kind, allow_market_up,
             (f"{br['advances']}/{br['declines']}" if br else None), k5, pcr_oi)
    return payload


if __name__ == "__main__":
    compute(OUT_DEFAULT)
