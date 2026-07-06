# -*- coding: utf-8 -*-
r"""
ecos_collect.py — 한국은행 ECOS 거시지표 수집 (무료 OpenAPI, ecos_api.txt 키)

[목적] 기준금리·원/달러 환율·국고채/회사채 금리·코스피 등 거시지표를 수집해, 아침 리포트의
  국면(regime)/위험회피(risk-off, F1) 판단을 보강한다. 지금은 외국인 수급만 보는 한계를 거시로 보완.

[설계 핵심] 통계코드 추측 위험을 피하려고 'KeyStatisticList(100대 지표)' 를 주력으로 쓴다 — 지표를
  '이름'으로 받으므로 코드가 필요 없다(안정적). 환율은 추세(5일)까지 보려 검증된 코드(731Y001/0000001/D)
  로 시계열도 추가하되, 실패해도 스냅샷은 그대로 동작한다(graceful).

[설계] requests, 표준 라이브러리. 지표별 try/except, 원자적 저장, ASCII 태그([ecos]),
  한글 OK·이모지 금지, UTF-8 ensure_ascii=False. 키는 접두 일부만 표시(전체 미출력).

[사용법]
  python ecos_collect.py                 # ecos_macro.json 생성
  python ecos_collect.py --check         # 키/연결만 점검
  python ecos_collect.py --tables 환율   # 통계표 코드 검색(StatisticTableList)
"""
import os
import sys
import json
import argparse
import logging
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import requests
except Exception:
    requests = None

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("ecos")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DEFAULT = os.path.join(HERE, "ecos_macro.json")
KEY_FILE = os.path.join(HERE, "ecos_api.txt")
BASE = "https://ecos.bok.or.kr/api"
PLACEHOLDER = "PASTE_YOUR_ECOS_KEY_HERE"

# KeyStatisticList(100대 지표) 에서 남길 거시지표(이름에 아래 키워드 포함 시 채택) — 코드 불필요
KEEP_KEYWORDS = ["기준금리", "콜금리", "CD", "국고채", "회사채", "환율", "코스피", "KOSPI",
                 "코스닥", "소비자물가", "생산자물가", "실업", "경상수지", "수출", "외환보유"]
# 환율 시계열(추세용) — 검증된 코드. 실패해도 스냅샷은 유지.
FX_STAT, FX_ITEM, FX_CYCLE = "731Y001", "0000001", "D"   # 원/달러(매매기준율), 일별


def load_ecos_key():
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                tok = line.split("#")[0].strip()
                if tok and tok != PLACEHOLDER:
                    return tok
    except Exception:
        pass
    return ""


def mask(k):
    return (k[:4] + "***") if k else "(없음)"


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return None


def _get(url):
    """ECOS GET → (data_dict 또는 None, error_msg). RESULT 에러 처리."""
    if requests is None:
        return None, "requests 미설치"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)
    if r.status_code != 200:
        return None, "HTTP %d" % r.status_code
    try:
        j = r.json()
    except Exception:
        return None, "비JSON: %s" % (r.text or "")[:80]
    if "RESULT" in j:   # 에러(INFO-200=데이터없음, ERROR-xxx=키/요청 오류)
        res = j.get("RESULT", {})
        return None, "%s %s" % (res.get("CODE", ""), str(res.get("MESSAGE", ""))[:80])
    return j, ""


def keystat_snapshot(key):
    """100대 지표에서 거시지표만 추려 [{name, value, unit, cycle, time, class}]. 실패 시 []."""
    j, err = _get("%s/KeyStatisticList/%s/json/kr/1/100" % (BASE, key))
    if j is None:
        log.warning("[ecos] KeyStatisticList 실패: %s", err)
        return []
    rows = (j.get("KeyStatisticList", {}) or {}).get("row", []) or []
    out = []
    for it in rows:
        try:
            name = (it.get("KEYSTAT_NAME") or "").strip()
            if not any(kw in name for kw in KEEP_KEYWORDS):
                continue
            out.append({
                "name": name, "value": _num(it.get("DATA_VALUE")),
                "raw_value": it.get("DATA_VALUE"), "unit": it.get("UNIT_NAME", ""),
                "cycle": it.get("CYCLE", ""), "time": it.get("TIME", ""),
                "class": it.get("CLASS_NAME", ""),
            })
        except Exception:
            continue
    return out


def fx_trend(key, days=20):
    """원/달러 환율 시계열(최근 days) → {latest, prev5, change_5d_pct, series:[(time,value)]}. 실패 시 None."""
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    url = "%s/StatisticSearch/%s/json/kr/1/100/%s/%s/%s/%s/%s" % (
        BASE, key, FX_STAT, FX_CYCLE, start, end, FX_ITEM)
    j, err = _get(url)
    if j is None:
        log.info("[ecos] 환율 시계열 생략(%s) — 스냅샷의 환율값은 유효", err)
        return None
    rows = (j.get("StatisticSearch", {}) or {}).get("row", []) or []
    series = []
    for it in rows:
        v = _num(it.get("DATA_VALUE"))
        t = it.get("TIME", "")
        if v is not None and t:
            series.append((t, v))
    series.sort()
    if not series:
        return None
    latest = series[-1][1]
    chg = None
    if len(series) >= 6:
        prev = series[-6][1]
        if prev:
            chg = round((latest / prev - 1.0) * 100, 2)
    return {"latest": latest, "asof": series[-1][0],
            "change_5d_pct": chg, "series": series[-10:]}


def _pick(snapshot, *keywords):
    for s in snapshot:
        if all(kw in s["name"] for kw in keywords):
            return s
    return None


def collect(key, out_path):
    snap = keystat_snapshot(key)
    fx = fx_trend(key)
    # 파생: 핵심값 + 거친 risk-off 힌트(환율 상승=원화약세=외인 위험회피 맥락)
    base_rate = _pick(snap, "기준금리")
    usdkrw = _pick(snap, "환율")
    ktb3 = _pick(snap, "국고채")
    kospi = _pick(snap, "코스피") or _pick(snap, "KOSPI")
    derived = {
        "base_rate": base_rate["value"] if base_rate else None,
        "usdkrw": (fx["latest"] if fx else (usdkrw["value"] if usdkrw else None)),
        "usdkrw_change_5d_pct": fx["change_5d_pct"] if fx else None,
        "ktb3y": ktb3["value"] if ktb3 else None,
        "kospi": kospi["value"] if kospi else None,
        "risk_off_hint": None,
    }
    # 아주 단순한 힌트(코워크가 최종 해석): 환율 5일 +1% 이상이면 '원화약세=주의'
    if derived["usdkrw_change_5d_pct"] is not None:
        c = derived["usdkrw_change_5d_pct"]
        derived["risk_off_hint"] = ("원화약세(주의)" if c >= 1.0 else
                                    ("원화강세(우호)" if c <= -1.0 else "중립"))
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "한국은행 ECOS OpenAPI (무료)",
        "snapshot": snap, "fx_trend": fx, "derived": derived,
        "_note": ("거시 국면/위험회피(F1) 보강. derived 의 환율 5일변화·기준금리·국고채로 risk-on/off "
                  "맥락 판단. snapshot 은 100대 지표 중 금리/환율/지수/물가."),
    }
    _save_json(out_path, payload)
    log.info("[ecos] 저장: %s (지표 %d · 환율추세 %s · risk_off=%s)",
             out_path, len(snap), "O" if fx else "X", derived["risk_off_hint"])
    return payload


def _save_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def discover_tables(key, keyword):
    """StatisticTableList 에서 keyword 포함 통계표(STAT_CODE/STAT_NAME) 검색 — 코드 찾기용."""
    j, err = _get("%s/StatisticTableList/%s/json/kr/1/1000" % (BASE, key))
    if j is None:
        log.error("[ecos] StatisticTableList 실패: %s", err)
        return
    rows = (j.get("StatisticTableList", {}) or {}).get("row", []) or []
    hit = [(it.get("STAT_CODE"), it.get("STAT_NAME")) for it in rows
           if keyword in (it.get("STAT_NAME") or "")]
    print("[ecos] '%s' 포함 통계표 %d개:" % (keyword, len(hit)))
    for code, name in hit[:40]:
        print("  %s  %s" % (code, name))


def main():
    ap = argparse.ArgumentParser(description="한국은행 ECOS 거시지표 수집(무료)")
    ap.add_argument("--check", action="store_true", help="키/연결만 점검")
    ap.add_argument("--tables", default="", help="통계표 코드 검색(키워드)")
    ap.add_argument("--out", default=OUT_DEFAULT)
    args = ap.parse_args()

    key = load_ecos_key()
    if not key:
        # 키 미입력은 '오류'가 아니라 '스킵'(파이프라인 친화적 exit 0). 키 넣으면 자동 동작.
        log.info("[ecos] ecos_api.txt 키 없음(자리표시자) — ECOS 수집 스킵. (다른 수집엔 영향 없음)")
        sys.exit(0)
    log.info("[ecos] 키 로드 prefix=%s", mask(key))

    if args.tables:
        discover_tables(key, args.tables)
        return

    if args.check:
        snap = keystat_snapshot(key)
        if snap:
            log.info("[ecos] 연결 OK — 거시지표 %d개 예: %s",
                     len(snap), ", ".join("%s=%s" % (s["name"][:10], s["raw_value"]) for s in snap[:4]))
        else:
            log.error("[ecos] 연결 실패 또는 데이터 없음(키 확인)")
            sys.exit(3)
        return

    collect(key, os.path.abspath(args.out))


if __name__ == "__main__":
    main()
