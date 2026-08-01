# -*- coding: utf-8 -*-
r"""
gen_scorecard.py — 예측 정답 채점 리포트 생성기 (종목별·지수별 맞춤/틀림)

[목적] stock_research 의 과거 분석 예측(predictions.json: picks·shorts·market_call)을
  **실제 등락률과 대조**해 "어떤 종목을 맞췄나/틀렸나, 코스피·코스닥 방향은 맞췄나"를
  **새 md 파일**로 정리한다. (기존 분석 md 에 덧붙이지 않고 별도 폴더에 생성.)

[입력] output/_archive/*/predictions.json + output/*/predictions.json (단 _designtest·_discarded 제외)
[가격] stock_backtest/price_cache (FSC공식종가→FDR, 캐시 재사용). 지수=KS11/KQ11.
[채점] 픽=진입후 horizon_days 상승이면 맞춤 / 숏=하락이면 맞춤 / 지수=dir 부호 일치하면 맞춤.
[출력] prediction_scorecard/예측채점_리포트.md  (누적 요약 + 날짜별 표)

[사용] python gen_scorecard.py
"""
import os
import re
import sys
import json
import glob
from datetime import datetime, timedelta

SR = os.getenv("STOCK_RESEARCH", r"C:\Users\USER\Desktop\stock_research")
SB = os.getenv("STOCK_BACKTEST", r"C:\Users\USER\Desktop\stock_backtest")
# stock_backtest 폴더가 backup 으로 이동돼(폴더 정리) import 가 조용히 깨졌었다(2026-07-20 발견).
# 원위치 우선, 없으면 backup 폴백 — price_cache 는 자기완결형(자체 cache/ 사용)이라 위치 무관.
if not os.path.isdir(SB):
    _bak = r"C:\Users\USER\Desktop\backup\stock_backtest"
    if os.path.isdir(_bak):
        SB = _bak
if SB not in sys.path:
    sys.path.insert(0, SB)
import price_cache as pc

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_MD = os.path.join(HERE, "예측채점_리포트.md")
INDEX = {"kospi": "KS11", "kosdaq": "KQ11"}


def _end(D, horizon):
    return (datetime.strptime(D, "%Y-%m-%d") + timedelta(days=horizon * 2 + 12)).strftime("%Y-%m-%d")


def _entry_date(date, session_dir):
    """진입 기준일. 세션 폴더명 _HHMMSS 의 시(hour)가 9시 미만(아침 장전)=당일 진입,
    그 외(장중·저녁 작성)=다음 달력일(채점기가 그 이후 첫 거래일을 진입으로). 룩어헤드/엇갈림 방지."""
    m = re.search(r"_(\d{2})\d{4}(?:$|[\\/])", os.path.basename(session_dir or "") + "/")
    hour = int(m.group(1)) if m else 6   # 폴더 시각 못 읽으면 아침(06:xx) 가정
    if hour < 9:
        return date
    return (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _f(v, suffix=""):
    """None 을 '—' 로 표시(표 가독성)."""
    return "—" if v is None else f"{v}{suffix}"


def stock_ret(ticker, D, horizon):
    """진입(첫 거래일 종가) → +horizon 거래일 종가 등락률. (entry, close, ret%) 또는 미만기면 close=None."""
    s = pc.get_ohlcv(str(ticker).zfill(6), D, _end(D, horizon))
    idx = next((i for i, (d, _c, _v) in enumerate(s) if d >= D), None)
    if idx is None:
        return None, None, None
    entry = s[idx][1]
    j = idx + horizon
    if j >= len(s):
        return entry, None, None
    close = s[j][1]
    return entry, close, round((close / entry - 1.0) * 100, 2)


def index_ret(idx_code, D, horizon):
    s = pc.get_index_series(idx_code, D, _end(D, horizon))
    i0 = next((i for i, (d, _c) in enumerate(s) if d >= D), None)
    if i0 is None:
        return None
    j = i0 + horizon
    if j >= len(s):
        return None
    return round((s[j][1] / s[i0][1] - 1.0) * 100, 2)


def _mark(ok):
    return "✅ 맞춤" if ok else ("❌ 틀림" if ok is not None else "⏳ 미만기")


def load_predictions():
    # active(output/*) 를 _archive 보다 먼저 → 같은 날짜가 양쪽에 있으면 active(최신본) 우선.
    active = sorted(glob.glob(os.path.join(SR, "output", "*", "predictions.json")))
    arch = sorted(glob.glob(os.path.join(SR, "output", "_archive", "*", "predictions.json")))
    out = []
    seen = set()
    for f in active + arch:
        if "_designtest" in f or "_discarded" in f:
            continue
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        date = d.get("date")
        if not date or date in seen:
            continue
        seen.add(date)
        out.append((date, d, os.path.dirname(f)))   # 세션폴더 경로(진입일 시각 판정용)
    return out


def score_one(date, d, session_dir=None):
    """한 예측일 채점 → dict. 진입은 세션 작성시각 기준(아침=당일/저녁=익일, _entry_date)."""
    entry_d = _entry_date(date, session_dir)
    # 지수 (T+1, T+5)
    idx_rows = []
    mc = d.get("market_call") or {}
    for mk, code in INDEX.items():
        call = ((mc.get(mk) or {}).get("dir") or "").lower()
        r1 = index_ret(code, entry_d, 1)
        r5 = index_ret(code, entry_d, 5)
        def ok(call, r):
            if r is None or call not in ("up", "down"):
                return None
            return (call == "up" and r > 0) or (call == "down" and r < 0)
        idx_rows.append({"index": mk.upper(), "call": call or "—", "r1": r1, "r5": r5,
                         "ok1": ok(call, r1), "ok5": ok(call, r5)})
    # 종목
    def score_items(items, is_pick):
        rows = []
        for it in (items or []):
            tk = str(it.get("ticker") or "").zfill(6)
            hz = int(it.get("horizon_days") or 5)
            entry, close, ret = stock_ret(tk, entry_d, hz)     # 해당 horizon (진입일 기준)
            _e1, _c1, ret1 = stock_ret(tk, entry_d, 1)         # T+1 조기 결과
            ok = None
            if ret is not None:
                ok = (ret > 0) if is_pick else (ret < 0)
            ok1 = None
            if ret1 is not None:
                ok1 = (ret1 > 0) if is_pick else (ret1 < 0)
            rows.append({"ticker": tk, "name": it.get("name"), "hz": hz,
                         "entry_ref": it.get("entry_ref"), "entry": entry, "close": close,
                         "ret1": ret1, "ok1": ok1, "ret": ret, "ok": ok, "conv": it.get("conviction")})
        return rows
    picks = score_items(d.get("picks"), True)
    shorts = score_items(d.get("shorts"), False)
    return {"date": date, "regime": d.get("regime"), "index": idx_rows, "picks": picks, "shorts": shorts}


def render(scored):
    L = ["# 📊 예측 정답 채점 리포트 (자동 생성)\n",
         "> stock_research 의 과거 예측(picks·shorts·지수방향)을 실제 등락률과 대조. "
         "픽=상승이면 맞춤·숏=하락이면 맞춤·지수=방향 일치면 맞춤. 미만기는 ⏳.\n",
         "> ★채점 규약 주의(v11.6): 이 리포트는 scorecard.md 와 **규약이 다르다** — "
         "진입일(세션 9시 이후면 익일 진입 vs 항상 예측일), 수익 앵커(진입일 종가 vs "
         "entry_ref=D-1 종가), horizon 결측 처리(기본 5 채점 vs 스킵). "
         "**두 산출물의 적중률·수익률을 서로 비교하지 마라**(코드상 정상적으로 다른 값이다).\n"]
    # 누적 집계
    ki = {"k1": [0, 0], "k5": [0, 0]}
    di = {"d1": [0, 0], "d5": [0, 0]}
    pk = [0, 0]
    sh = [0, 0]
    pk1 = [0, 0]
    sh1 = [0, 0]
    for s in scored:
        for ir in s["index"]:
            tgt = ki if ir["index"] == "KOSPI" else di
            for h, key in ((ir["ok1"], "k1" if ir["index"] == "KOSPI" else "d1"),
                           (ir["ok5"], "k5" if ir["index"] == "KOSPI" else "d5")):
                if h is not None:
                    tgt[key][1] += 1
                    tgt[key][0] += 1 if h else 0
        for r in s["picks"]:
            if r["ok"] is not None:
                pk[1] += 1
                pk[0] += 1 if r["ok"] else 0
            if r["ok1"] is not None:
                pk1[1] += 1
                pk1[0] += 1 if r["ok1"] else 0
        for r in s["shorts"]:
            if r["ok"] is not None:
                sh[1] += 1
                sh[0] += 1 if r["ok"] else 0
            if r["ok1"] is not None:
                sh1[1] += 1
                sh1[0] += 1 if r["ok1"] else 0

    def pct(a):
        return f"{a[0]}/{a[1]} ({round(100*a[0]/a[1])}%)" if a[1] else "0/0"
    L.append("## ✅ 누적 적중률 (만기된 예측)\n")
    L.append(f"- **지수 KOSPI**: T+1 {pct(ki['k1'])} · T+5 {pct(ki['k5'])}")
    L.append(f"- **지수 KOSDAQ**: T+1 {pct(di['d1'])} · T+5 {pct(di['d5'])}")
    L.append(f"- **픽(상승 예상)**: T+1 {pct(pk1)} · 만기 {pct(pk)}")
    L.append(f"- **숏(하락 예상)**: T+1 {pct(sh1)} · 만기 {pct(sh)}\n")

    # 날짜별 (최신 먼저)
    L.append("## 📅 날짜별 채점\n")
    for s in sorted(scored, key=lambda x: x["date"], reverse=True):
        L.append(f"### {s['date']}  (regime: {s.get('regime') or '—'})")
        # 지수
        for ir in s["index"]:
            L.append(f"- **{ir['index']}** 예상 `{ir['call']}` → 실제 T+1 {_f(ir['r1'], '%')} / T+5 {_f(ir['r5'], '%')} "
                     f"→ T+5 {_mark(ir['ok5'])} (T+1 {_mark(ir['ok1'])})")
        # 종목 표 (T+1 조기결과 + 만기결과)
        L.append("\n| 종목 | 구분 | 예상 | 진입 | T+1 | (판정) | 만기(T+N) | (판정) |")
        L.append("|---|---|---|---|---|---|---|---|")

        def srow(r, kind, exp):
            return (f"| {r['name']}({r['ticker']}) | {kind} | {exp} | {_f(r['entry'])} | "
                    f"{_f(r['ret1'], '%')} | {_mark(r['ok1'])} | T+{r['hz']} {_f(r['ret'], '%')} | {_mark(r['ok'])} |")
        for r in s["picks"]:
            L.append(srow(r, "픽", "상승"))
        for r in s["shorts"]:
            L.append(srow(r, "숏", "하락"))
        # 날짜 요약 (T+1 / 만기)
        p1 = [r["ok1"] for r in s["picks"] if r["ok1"] is not None]
        s1 = [r["ok1"] for r in s["shorts"] if r["ok1"] is not None]
        ph = [r["ok"] for r in s["picks"] if r["ok"] is not None]
        sd = [r["ok"] for r in s["shorts"] if r["ok"] is not None]
        L.append(f"\n> 이날 T+1: 픽 {sum(p1)}/{len(p1)} · 숏 {sum(s1)}/{len(s1)} | 만기: 픽 {sum(ph)}/{len(ph)} · 숏 {sum(sd)}/{len(sd)}\n")
    return "\n".join(L)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    preds = load_predictions()
    scored = [score_one(date, d, sdir) for date, d, sdir in preds]
    md = render(scored)
    tmp = OUT_MD + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(md)
    os.replace(tmp, OUT_MD)
    # 콘솔 요약(ASCII)
    n_dates = len(scored)
    print("[scorecard] %d개 예측일 채점 -> %s" % (n_dates, OUT_MD))


if __name__ == "__main__":
    main()
