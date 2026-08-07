#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
night_track.py — 전야(23시) 지수 콜의 영속화 + 채점 [v11.20 신규]

[왜]
  night_preview.json 은 루트에서 **매일 밤 덮어써져** 전날 예측이 어디에도 안 남았다
  (실측: output/**·_archive·snapshot 전무). 예측을 내고 메일로 보내면서(v11.17)
  채점은 받지 않는 유일한 표면이었다 — 오답 흔적이 소멸하는 구조는 그 자체가 과적합
  경로다(자기 성적을 모르면 서사가 성적을 대신한다).

[무엇을]
  1) ingest: 루트 night_preview.json → night_calls.jsonl 에 원문 그대로 적재(append).
  2) score : 확정 종가가 나온 과거 콜을 채점 → night_scorecard.md.

[★채점 규약 — 적대 검증 3렌즈가 강제한 것들]
  · 정확 일치 매칭만: 채점 봉 날짜 == for_date. **backward 매칭 금지** — for_date 가
    휴장일이면 직전 봉(=콜 낼 때 이미 안 값, inputs.kr_close_confirmed)으로 채점되는
    룩어헤드가 생긴다. for_date 를 지난 확정 봉이 있는데 당일 봉이 없으면 void 로
    **명시 기록**(조용한 탈락 금지 — 연휴 전 고불확실성 콜만 사라지는 생존편향 차단).
  · A40 엄격: 오늘 날짜 봉은 절대 쓰지 않는다(bar_date < today, 부등호 엄격).
    2026-08-04 실사고(16:01 채점에서 장중 스냅샷 -1.13% vs 실제 종가 +1.62%) 재발 방지.
    '15:40 이후 허용' 류 완화를 재도입하지 마라 — 그게 뚫렸던 구가드다.
  · for_date 당 채점 1콜: 같은 for_date 에 콜이 여러 건이면(23시 재실행 등)
    **최신 generated_at 만** 채점하고 나머지는 superseded 로 보존·비채점.
  · 규약 정렬: 중립 밴드 ±0.5%(T+1, v9.6)·Brier 3분류·Wilson CI 를 accuracy_tracker 와
    동일 수식으로 쓴다(common.py 순수함수 — accuracy_tracker 는 import 하지 않는다).
    안 맞추면 "밤 콜 vs 아침 콜 어느 쪽이 맞나"([5.17]) 비교가 사과-배가 된다.
  · 기준가: date(generated_at) 이하의 마지막 확정 종가(=콜 낼 때 알던 값).

[★소비 금지 — 이 산출물은 자기보정 입력이 아니다]
  night_scorecard.md 는 [0.5] 아침 자기보정·23시 콜 작성의 입력이 **아니다**
  (머리 배너로 명시). 소표본(며칠치)으로 확률을 조정하는 경로를 막는다.
  규칙화는 회고 사전등록 경로만. 회고에는 market_calls 와 **합산 금지·비교 전용**으로 간다.

[사용]
  python night_track.py            # ingest + score + scorecard 갱신
  python night_track.py --ingest-only
  python night_track.py --check    # 상태만
"""
from __future__ import annotations

import os
import sys
import json
import hashlib
import argparse
import logging
from datetime import datetime, date

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
PREVIEW = os.path.join(HERE, "night_preview.json")
LEDGER = os.path.join(HERE, "night_calls.jsonl")
SCORECARD = os.path.join(HERE, "night_scorecard.md")

logging.basicConfig(level=logging.INFO, format="[night] %(message)s")
log = logging.getLogger("night")

from common import wilson_ci, brier3, ret_to_label

NEUTRAL_BAND = 0.5          # T+1 ±0.5% — accuracy_tracker NEUTRAL_BAND_BY_H[1](v9.6)과 동일
NO_INFO_BRIER = 0.6667      # 1/3씩 무정보 기준
MARKETS = {"kospi": "KS11", "kosdaq": "KQ11"}


# =====================================================================
# 순수 로직 (하네스가 검증)
# =====================================================================
def call_key(call):
    """(for_date, generated_at). 내용 해시는 별도."""
    return (str(call.get("for_date") or ""), str(call.get("generated_at") or ""))


def content_sha(call):
    return hashlib.sha256(
        json.dumps(call, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def pick_operative(entries):
    """for_date 별 '운용 콜'(최신 generated_at) 선정.

    반환 {for_date: entry}. 나머지는 superseded — 채점하지 않는다(같은 거래일에
    콜 여러 건이 전부 채점되면 그 날이 표를 여러 번 행사한다).
    """
    by = {}
    for e in entries:
        fd, ga = call_key(e.get("call") or {})
        if not fd:
            continue
        cur = by.get(fd)
        if cur is None or str((cur.get("call") or {}).get("generated_at") or "") < ga:
            by[fd] = e
    return by


def score_market(mcall, base_close, target_close):
    """한 시장(kospi|kosdaq) 콜 1건 채점. 순수함수.

    반환 dict(ret_pct, outcome, dir_hit, argmax, argmax_hit, brier, range_hit, exp_err).
    """
    out = {"ret_pct": None, "outcome": None, "dir_hit": None, "argmax": None,
           "argmax_hit": None, "brier": None, "range_hit": None, "exp_err": None}
    if not mcall or not base_close or not target_close:
        return out
    ret = (target_close / base_close - 1.0) * 100.0
    out["ret_pct"] = round(ret, 2)
    out["outcome"] = ret_to_label(ret, NEUTRAL_BAND)

    d = str(mcall.get("dir") or "").strip().lower()
    if d in ("up", "down", "flat", "neutral"):
        # neutral 은 flat 과 동치로 본다 — A43(dir↔argmax 규약 미정)이 확정되면 따른다
        dd = "flat" if d == "neutral" else d
        out["dir_hit"] = (dd == out["outcome"])

    probs = [mcall.get("prob_up"), mcall.get("prob_flat"), mcall.get("prob_down")]
    if all(p is not None for p in probs):
        try:
            trio = {"up": float(probs[0]), "flat": float(probs[1]), "down": float(probs[2])}
            out["argmax"] = max(trio, key=trio.get)
            out["argmax_hit"] = (out["argmax"] == out["outcome"])
            out["brier"] = brier3(trio["up"], trio["flat"], trio["down"], out["outcome"])
        except (TypeError, ValueError):
            pass

    lo, hi = mcall.get("range_low"), mcall.get("range_high")
    try:
        if lo is not None and hi is not None:
            out["range_hit"] = (float(lo) <= float(target_close) <= float(hi))
    except (TypeError, ValueError):
        pass
    try:
        if mcall.get("expected_pct") is not None:
            out["exp_err"] = round(ret - float(mcall["expected_pct"]), 2)
    except (TypeError, ValueError):
        pass
    return out


def score_call(call, closes_by_market, today):
    """콜 1건(운용 콜) 채점. 순수함수 — 시세는 {market:{date_str:close}} 로 주입.

    상태: scored | pending(봉 미도래) | void_for_date(그 날짜 봉 없음 — 휴장 지정)
          | invalid(for_date 가 생성일 이전 등)
    """
    fd = str(call.get("for_date") or "")[:10]
    ga = str(call.get("generated_at") or "")[:10]
    try:
        fdt = datetime.strptime(fd, "%Y-%m-%d").date()
        gdt = datetime.strptime(ga, "%Y-%m-%d").date()
    except ValueError:
        return {"status": "invalid", "why": "날짜 형식 오류(for_date=%s)" % fd}
    if fdt <= gdt:
        return {"status": "invalid",
                "why": "for_date 가 생성일 이전/당일(%s <= %s) — 예측이 아니다" % (fd, ga)}

    result = {"status": None, "for_date": fd, "markets": {}}
    any_scored = False
    for mk in MARKETS:
        closes = closes_by_market.get(mk) or {}
        # ★A40 엄격: 오늘 봉 제외. 원장 A40(2026-08-04 실사고) — 완화 금지.
        confirmed = {d: c for d, c in closes.items()
                     if d < today.strftime("%Y-%m-%d")}
        # ★정확 일치만. backward 매칭 = inputs.kr_close_confirmed 로 자기채점(룩어헤드).
        target = confirmed.get(fd)
        base_dates = [d for d in confirmed if d <= ga]
        base = confirmed[max(base_dates)] if base_dates else None
        if target is None:
            later = [d for d in confirmed if d > fd]
            if later:
                result["markets"][mk] = {"status": "void_for_date"}
            else:
                result["markets"][mk] = {"status": "pending"}
            continue
        m = score_market(call.get(mk) or {}, base, target)
        m["status"] = "scored"
        result["markets"][mk] = m
        any_scored = True

    sts = {v.get("status") for v in result["markets"].values()}
    if any_scored:
        result["status"] = "scored"
    elif "pending" in sts:
        result["status"] = "pending"
    elif sts == {"void_for_date"}:
        result["status"] = "void_for_date"
    else:
        result["status"] = "pending"
    return result


# =====================================================================
# IO
# =====================================================================
def load_ledger():
    out = []
    if not os.path.isfile(LEDGER):
        return out
    with open(LEDGER, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
    return out


def ingest():
    """루트 프리뷰 → 원장 적재. 반환 (적재됐나, 사유)."""
    if not os.path.isfile(PREVIEW):
        return False, "night_preview.json 없음(23시 작업 전 — 정상)"
    try:
        with open(PREVIEW, encoding="utf-8-sig") as f:
            call = json.load(f)
    except Exception as e:
        return False, "프리뷰 파싱 실패: %s" % e
    fd, ga = call_key(call)
    if not fd or not ga:
        return False, "for_date/generated_at 없음 — 적재 불가(스키마 확인)"
    sha = content_sha(call)
    for e in load_ledger():
        k = call_key(e.get("call") or {})
        if k == (fd, ga) and e.get("sha") == sha:
            return False, "이미 적재됨(%s)" % fd
    entry = {"ingested_at": datetime.now().isoformat(timespec="seconds"),
             "sha": sha, "call": call}
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return True, "적재: for_date=%s (sha %s)" % (fd, sha)


def _fetch_closes():
    """{market: {date: close}}. FDR 실패 시 빈 dict(채점 보류 — 지어내지 않는다)."""
    out = {}
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import FinanceDataReader as fdr
    except Exception:
        return out
    for mk, sym in MARKETS.items():
        try:
            df = fdr.DataReader(sym, "2026-01-01")
            d = {}
            for ix, c in zip(df.index, df["Close"].tolist()):
                if c == c and c and hasattr(ix, "date"):
                    d[ix.date().strftime("%Y-%m-%d")] = float(c)
            out[mk] = d
        except Exception:
            out[mk] = {}
    return out


BANNER = """<!-- ★★이 파일은 [0.5] 아침 자기보정·23시 콜 작성의 **입력이 아니다**.
  확률·확신도를 이 표로 조정하지 마라 — 표본이 작을 때(N<20 전체 / n<30 관찰)
  그 조정이 곧 과적합이다(휩쏘 실사고 계보). 규칙화는 회고 **사전등록 경로만**.
  회고에서도 market_calls(아침 콜)와 **합산 금지** — 같은 거래일 이중계상. 비교 전용. -->"""


def write_scorecard(entries, closes, today):
    ops = pick_operative(entries)
    n_total = len(entries)
    n_superseded = n_total - len(ops)

    rows, counts = [], {"scored": 0, "pending": 0, "void_for_date": 0, "invalid": 0}
    for fd in sorted(ops):
        r = score_call(ops[fd].get("call") or {}, closes, today)
        r["generated_at"] = (ops[fd].get("call") or {}).get("generated_at")
        rows.append(r)
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    L = [BANNER, "", "# 전야(23시) 지수 콜 성적 — night_track v11.20", ""]
    L.append("갱신: %s · 원장 %d건(운용 %d · superseded %d) · "
             "채점 %d / 대기 %d / void %d / invalid %d"
             % (datetime.now().strftime("%Y-%m-%d %H:%M"), n_total, len(ops),
                n_superseded, counts.get("scored", 0), counts.get("pending", 0),
                counts.get("void_for_date", 0), counts.get("invalid", 0)))
    L.append("")
    L.append("※ void = for_date 에 봉이 없음(휴장일 지정 — 23시 작업이 다음 '거래일'이 아닌")
    L.append("  달력일을 적은 것). 조용히 빼지 않고 여기 센다 — 모집단 왜곡 가시화.")
    L.append("")

    for mk in MARKETS:
        sc = [r["markets"].get(mk) for r in rows
              if r["status"] == "scored" and (r["markets"].get(mk) or {}).get("status") == "scored"]
        n = len(sc)
        L.append("## %s (채점 N=%d)" % (mk.upper(), n))
        if not n:
            L.append("- 채점 표본 없음")
            L.append("")
            continue
        dh = [m for m in sc if m.get("dir_hit") is not None]
        ah = [m for m in sc if m.get("argmax_hit") is not None]
        rh = [m for m in sc if m.get("range_hit") is not None]
        br = [m["brier"] for m in sc if m.get("brier") is not None]
        ee = [abs(m["exp_err"]) for m in sc if m.get("exp_err") is not None]
        if dh:
            hit = sum(1 for m in dh if m["dir_hit"])
            lo, hi = wilson_ci(hit, len(dh))
            L.append("- 방향(dir, 밴드 ±%.1f%%): %d/%d (%.0f%%, CI %s~%s%%)"
                     % (NEUTRAL_BAND, hit, len(dh), hit * 100.0 / len(dh), lo, hi))
        if ah:
            hit = sum(1 for m in ah if m["argmax_hit"])
            L.append("- 확률 argmax: %d/%d (%.0f%%)" % (hit, len(ah), hit * 100.0 / len(ah)))
        if br:
            L.append("- Brier(3분류) 평균: %.4f (무정보 %.4f)"
                     % (sum(br) / len(br), NO_INFO_BRIER))
        if rh:
            hit = sum(1 for m in rh if m["range_hit"])
            L.append("- 레인지 적중: %d/%d" % (hit, len(rh)))
        if ee:
            L.append("- |실현-예상| 평균: %.2f%%p" % (sum(ee) / len(ee)))
        L.append("")

    L.append("## 콜별 (최근 14건)")
    L.append("")
    L.append("| for_date | 상태 | KOSPI 실현% | dir | 적중 | 레인지 | KOSDAQ 실현% | dir | 적중 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows[-14:]:
        ks = r["markets"].get("kospi") or {}
        kq = r["markets"].get("kosdaq") or {}
        op = ops.get(r.get("for_date")) or {}
        call = op.get("call") or {}

        def cell(m, k):
            v = m.get(k)
            if v is None:
                return "-"
            if isinstance(v, bool):
                return "O" if v else "X"
            return str(v)
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r.get("for_date"), r["status"],
            cell(ks, "ret_pct"), (call.get("kospi") or {}).get("dir", "-"),
            cell(ks, "dir_hit"), cell(ks, "range_hit"),
            cell(kq, "ret_pct"), (call.get("kosdaq") or {}).get("dir", "-"),
            cell(kq, "dir_hit")))
    L.append("")
    L.append("주: 기준가는 콜 생성일 이하의 마지막 확정 종가(콜 낼 때 알던 값). 채점 봉은")
    L.append("for_date 정확 일치 + 오늘 이전(A40 엄격 — 원장 A40 참조). 처방 없음 — 사실만.")

    from common import atomic_write_text
    atomic_write_text(SCORECARD, "\n".join(L) + "\n")
    return counts


def main():
    ap = argparse.ArgumentParser(description="전야 콜 영속화+채점(★자기보정 입력 아님)")
    ap.add_argument("--ingest-only", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if args.check:
        entries = load_ledger()
        ops = pick_operative(entries)
        log.info("원장 %d건 / 운용 %d건 / 프리뷰 %s", len(entries), len(ops),
                 "있음" if os.path.isfile(PREVIEW) else "없음")
        print("NIGHT_TRACK=check")
        return 0

    did, why = ingest()
    log.info("%s", why)

    if args.ingest_only:
        print("NIGHT_TRACK=%s" % ("ingested" if did else "no_new"))
        return 0

    entries = load_ledger()
    if not entries:
        log.info("원장이 비어 있다 — 채점 생략(첫 23시 작업 후부터 쌓인다).")
        print("NIGHT_TRACK=empty")
        return 0
    closes = _fetch_closes()
    if not any(closes.values()):
        log.warning("시세 조회 실패 — 채점 보류(지어내지 않는다). 원장 적재는 유지됨.")
        print("NIGHT_TRACK=%s_no_quotes" % ("ingested" if did else "no_new"))
        return 0
    counts = write_scorecard(entries, closes, date.today())
    log.info("night_scorecard.md 갱신 — 채점 %d / 대기 %d / void %d",
             counts.get("scored", 0), counts.get("pending", 0),
             counts.get("void_for_date", 0))
    print("NIGHT_TRACK=%s_scored:%d" % ("ingested" if did else "no_new",
                                        counts.get("scored", 0)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
