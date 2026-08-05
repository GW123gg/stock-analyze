#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
portfolio_review.py — 내 실제 보유 포트폴리오 측정 [v11.12 신규]

[무엇을 하나]
  `portfolio.csv`(사용자가 엑셀로 직접 관리)를 읽어, 각 보유 종목이 **지금 어디에 와 있는지**를
  직전 거래일 종가 기준으로 계산한다. 손익·보유일수·지수 대비 알파·우리 추천과의 관계까지.

[★설계 원칙 — 판정하지 않는다]
  이 스크립트는 **사실만 준다.** "팔아라/버텨라" 는 분석가(코워크)가 쓴다.
  `holding_review.py` 와 같은 철학이다 — 숫자를 만드는 쪽과 판단하는 쪽을 분리해야
  판단이 데이터에 끌려다니지 않는다.

[세 가지를 헷갈리지 마라]
  · `portfolio.csv`        — **사용자의 실제 보유**(수동 관리). 이 파일.
  · `positions.json`(노트북) — 매매서버가 보는 계좌(자동매매용). 실제와 다를 수 있다.
  · `holding_review.json`   — **우리가 추천했던 것들**의 사후 추적(보유와 무관).

[입력] portfolio.csv — 엑셀에서 편집. 열: 매수일시, 종목코드, 종목명, 평단가, 수량, 메모
  · 엑셀이 저장하는 UTF-8 BOM·cp949 를 전부 읽는다.
  · 같은 종목을 여러 번 샀으면 **행을 여러 개** 쓰면 된다(가중평균을 자동 계산한다).

[출력] portfolio_review.json (세션폴더 또는 루트) + 콘솔 표
"""
from __future__ import annotations

import os
import sys
import csv
import json
import glob
import argparse
import logging
from datetime import datetime, date

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(HERE, "portfolio.csv")
OUTPUT_DIR = os.path.join(HERE, "output")

logging.basicConfig(level=logging.INFO, format="[portfolio] %(message)s")
log = logging.getLogger("portfolio")

try:
    import FinanceDataReader as fdr
    FDR_OK = True
except Exception:
    fdr = None
    FDR_OK = False


# =====================================================================
# 순수 계산 (하네스가 검증한다)
# =====================================================================
def normalize_ticker(raw):
    """엑셀이 망가뜨린 종목코드를 6자리로 복원. 반환 (ticker|None, 복원했나:bool).

    ★엑셀은 CSV 의 `034020` 을 **숫자로 인식해 34020 으로 저장**한다(선행 0 소실).
      한국 종목코드는 6자리 고정이라 5자리 이하는 무조건 선행 0 누락이므로 복원이 안전하다.
    받아주는 형태: 034020 / 34020 / ="034020"(엑셀 텍스트 서식) / A034020(HTS 표기) / 공백 포함
    """
    s = str(raw or "").strip()
    if not s:
        return None, False
    # ="034020" — 엑셀에서 텍스트로 강제할 때 쓰는 형식
    if s.startswith('="') and s.endswith('"'):
        s = s[2:-1].strip()
    elif s.startswith("=") and len(s) > 1:
        s = s[1:].strip().strip('"')
    s = s.strip('"').strip("'").replace(" ", "").replace("-", "")
    # A034020 / KR7034020003 같은 접두 표기
    if len(s) > 6 and s[0].isalpha() and s[1:].isdigit():
        s = s[1:]
    if not s.isdigit():
        return None, False
    if len(s) > 6:
        return None, False
    fixed = len(s) < 6
    return s.zfill(6), fixed


def excel_safe_ticker(tk):
    """엑셀이 다시 열어도 선행 0 을 안 지우게 하는 표기. `="034020"` 형식."""
    return '="%s"' % tk


def parse_row(row):
    """CSV 한 행 → 표준 dict. 못 읽으면 (None, 사유)."""
    def g(*names):
        for n in names:
            for k in row:
                if k and k.strip().replace(" ", "") == n:
                    return (row[k] or "").strip()
        return ""

    tk_raw = g("종목코드", "ticker", "코드")
    if not tk_raw:
        return None, "종목코드 없음"
    tk, fixed = normalize_ticker(tk_raw)
    if not tk:
        return None, "종목코드 형식 오류: %s" % tk_raw
    try:
        price = float(str(g("평단가", "avg_price", "매입가")).replace(",", ""))
        qty = int(float(str(g("수량", "qty", "주식개수", "개수")).replace(",", "")))
    except (TypeError, ValueError):
        return None, "평단가/수량이 숫자가 아님"
    if price <= 0 or qty <= 0:
        return None, "평단가·수량은 0보다 커야 함"
    d = g("매수일시", "buy_date", "매수일")[:10].replace("/", "-").replace(".", "-")
    return {"ticker": tk, "name": g("종목명", "name") or tk,
            "avg_price": price, "qty": qty, "buy_date": d,
            "memo": g("메모", "memo", "note"),
            "ticker_fixed": fixed, "ticker_raw": tk_raw}, ""


def merge_lots(rows):
    """같은 종목의 여러 매수 행 → 가중평균 1건. 매수일시는 가장 이른 날."""
    by = {}
    for r in rows:
        t = r["ticker"]
        if t not in by:
            by[t] = {"ticker": t, "name": r["name"], "qty": 0, "cost": 0.0,
                     "buy_date": r["buy_date"], "lots": 0, "memo": r.get("memo", "")}
        b = by[t]
        b["qty"] += r["qty"]
        b["cost"] += r["avg_price"] * r["qty"]
        b["lots"] += 1
        if r["buy_date"] and (not b["buy_date"] or r["buy_date"] < b["buy_date"]):
            b["buy_date"] = r["buy_date"]
        if r.get("memo") and r["memo"] not in (b["memo"] or ""):
            b["memo"] = (b["memo"] + " / " + r["memo"]).strip(" /")
    out = []
    for b in by.values():
        b["avg_price"] = round(b["cost"] / b["qty"], 2) if b["qty"] else 0.0
        out.append(b)
    return sorted(out, key=lambda x: -x["cost"])


def compute_position(pos, last_close, index_ret_pct=None, today=None):
    """보유 1건 + 현재가 → 손익 지표. 사실만 계산하고 판정하지 않는다."""
    out = dict(pos)
    out["last_close"] = last_close
    if not last_close or last_close <= 0:
        out.update({"value": None, "pnl": None, "pnl_pct": None, "alpha_pct": None})
        out["_note"] = "현재가 조회 실패"
        return out
    value = last_close * pos["qty"]
    cost = pos["avg_price"] * pos["qty"]
    out["value"] = round(value)
    out["cost"] = round(cost)
    out["pnl"] = round(value - cost)
    out["pnl_pct"] = round((last_close / pos["avg_price"] - 1.0) * 100.0, 2)
    # 지수 대비 — '내 종목이 나빴나, 시장이 나빴나'를 가른다(F8-b 와 같은 축)
    out["alpha_pct"] = (round(out["pnl_pct"] - index_ret_pct, 2)
                        if index_ret_pct is not None else None)
    # 보유일수(캘린더)
    try:
        d0 = datetime.strptime(pos["buy_date"], "%Y-%m-%d").date()
        out["held_days"] = ((today or date.today()) - d0).days
    except Exception:
        out["held_days"] = None
    return out


def portfolio_totals(positions):
    """전체 합계. 평가액·원금·손익·손익률·집중도."""
    val = sum(p["value"] for p in positions if p.get("value"))
    cost = sum(p["cost"] for p in positions if p.get("cost"))
    n_priced = sum(1 for p in positions if p.get("value"))
    out = {"n_positions": len(positions), "n_priced": n_priced,
           "total_value": round(val), "total_cost": round(cost),
           "total_pnl": round(val - cost) if cost else 0,
           "total_pnl_pct": round((val / cost - 1.0) * 100.0, 2) if cost else None}
    # 집중도 — 한 종목이 너무 크면 그 자체가 위험이다
    if val > 0:
        top = max(positions, key=lambda p: p.get("value") or 0)
        out["top_ticker"] = top["ticker"]
        out["top_name"] = top.get("name")
        out["top_weight_pct"] = round((top.get("value") or 0) / val * 100.0, 1)
    return out


# =====================================================================
# 시세·교차참조
# =====================================================================
def _last_close(ticker, today=None):
    """직전 거래일 종가. ★오늘 봉은 제외한다(A40 원칙 — 미확정 장중값 유입 차단)."""
    if not FDR_OK:
        return None
    try:
        df = fdr.DataReader(ticker, "2026-01-01")
        if df is None or df.empty:
            return None
        t = today or date.today()
        for ix in reversed(df.index):
            d = ix.date() if hasattr(ix, "date") else None
            if d is not None and d < t:
                v = float(df.loc[ix, "Close"])
                return v if v > 0 else None
    except Exception:
        return None
    return None


def _index_ret_since(buy_date, today=None):
    """매수일 이후 KOSPI 수익률(%). 알파 계산용."""
    if not FDR_OK or not buy_date:
        return None
    try:
        df = fdr.DataReader("KS11", buy_date)
        if df is None or len(df) < 2:
            return None
        t = today or date.today()
        closes = [(ix.date() if hasattr(ix, "date") else None, float(c))
                  for ix, c in zip(df.index, df["Close"].tolist()) if c == c]
        closes = [(d, c) for d, c in closes if d is not None and d < t]
        if len(closes) < 2:
            return None
        return round((closes[-1][1] / closes[0][1] - 1.0) * 100.0, 2)
    except Exception:
        return None


def _our_history(ticker):
    """우리가 이 종목을 추천한 적 있나. recommended_history.json 참조."""
    p = os.path.join(HERE, "recommended_history.json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        for t in d.get("tickers") or []:
            if str(t.get("ticker")) == ticker:
                ds = t.get("dates") or []
                return {"recommended": True, "count": len(ds),
                        "first": ds[0] if ds else None, "last": ds[-1] if ds else None,
                        "tags": list(dict.fromkeys(t.get("tags") or []))[:3]}
    except Exception:
        pass
    return {"recommended": False}


def _today_pick(ticker, session):
    """오늘 픽/숏에 이 종목이 있나 — 있으면 오늘의 목표·손절을 붙여준다."""
    if not session:
        return None
    try:
        with open(os.path.join(session, "predictions.json"), encoding="utf-8") as f:
            d = json.load(f)
        for kind, key in (("pick", "picks"), ("short", "shorts")):
            for it in d.get(key) or []:
                if str(it.get("ticker")) == ticker:
                    return {"kind": kind, "entry_ref": it.get("entry_ref"),
                            "target_pct": it.get("target_pct"), "stop_pct": it.get("stop_pct"),
                            "thesis": (it.get("thesis") or "")[:120],
                            "entry_window": it.get("entry_window")}
    except Exception:
        pass
    return None


def load_portfolio(path=CSV_FILE):
    """CSV 로드. 엑셀 BOM·cp949 전부 대응. 반환 (positions, warnings)."""
    if not os.path.isfile(path):
        return [], ["portfolio.csv 가 없습니다 (%s)" % path]
    raw, warns = None, []
    for enc in ("utf-8-sig", "cp949", "utf-8"):
        try:
            with open(path, encoding=enc, newline="") as f:
                raw = list(csv.DictReader(f))
            break
        except (UnicodeDecodeError, LookupError):
            continue
        except Exception as e:
            return [], ["CSV 읽기 실패: %s" % e]
    if raw is None:
        return [], ["CSV 인코딩을 판별하지 못했습니다(UTF-8 로 저장해 보세요)"]

    rows = []
    for i, r in enumerate(raw, 2):      # 2행부터(1행은 헤더)
        if not any((v or "").strip() for v in r.values()):
            continue
        p, why = parse_row(r)
        if p is None:
            warns.append("%d행 건너뜀 — %s" % (i, why))
            continue
        if "예시 행" in (p.get("memo") or ""):
            warns.append("%d행은 예시 행입니다 — 지우고 실제 보유를 넣으세요" % i)
            continue
        if p.get("ticker_fixed"):
            warns.append("%d행 종목코드 복원: %s -> %s (엑셀이 선행 0 을 지웠다 — "
                         "`--normalize` 로 파일을 고칠 수 있다)"
                         % (i, p.get("ticker_raw"), p["ticker"]))
        rows.append(p)
    return merge_lots(rows), warns


def normalize_csv(path=CSV_FILE):
    """엑셀이 망가뜨린 CSV 를 제자리에서 고친다. 반환 (고친행수, 메시지리스트).

    하는 일: ① 종목코드 6자리 복원 + `="034020"` 표기로 저장(엑셀이 다시 안 지운다)
             ② **UTF-8 BOM 으로 저장** — BOM 이 없으면 엑셀이 cp949 로 읽어 한글이 깨진다
    원본은 `portfolio.csv.bak` 로 백업한다.
    """
    msgs = []
    if not os.path.isfile(path):
        return 0, ["파일이 없습니다: %s" % path]
    raw = None
    for enc in ("utf-8-sig", "cp949", "utf-8"):
        try:
            with open(path, encoding=enc, newline="") as f:
                rd = csv.DictReader(f)
                raw = list(rd)
                cols = rd.fieldnames or []
            msgs.append("읽기 인코딩: %s" % enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
        except Exception as e:
            return 0, ["읽기 실패: %s" % e]
    if raw is None:
        return 0, ["인코딩 판별 실패"]

    n_fix = 0
    tcol = next((c for c in cols if c and c.strip().replace(" ", "")
                 in ("종목코드", "ticker", "코드")), None)
    if not tcol:
        return 0, ["'종목코드' 열을 찾지 못했습니다 (열: %s)" % ", ".join(cols)]
    for r in raw:
        tk, fixed = normalize_ticker(r.get(tcol))
        if tk:
            if fixed:
                msgs.append("복원: %s -> %s" % (r.get(tcol), tk))
                n_fix += 1
            r[tcol] = excel_safe_ticker(tk)
    try:
        import shutil
        shutil.copy2(path, path + ".bak")
        # ★utf-8-sig = BOM 포함. 엑셀이 UTF-8 로 인식해 한글이 안 깨진다.
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(raw)
    except Exception as e:
        return 0, msgs + ["저장 실패: %s" % e]
    msgs.append("저장 완료(UTF-8 BOM + 엑셀 안전 종목코드). 백업: %s.bak"
                % os.path.basename(path))
    return n_fix, msgs


# =====================================================================
# 실행
# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="내 포트폴리오 측정(★판정하지 않는다)")
    ap.add_argument("--csv", default=CSV_FILE)
    ap.add_argument("--session", default=None, help="세션폴더(없으면 오늘 세션 자동)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--normalize", action="store_true",
                    help="엑셀이 망가뜨린 CSV 를 고친다(선행 0 복원 + UTF-8 BOM 저장)")
    args = ap.parse_args()

    if args.normalize:
        n, msgs = normalize_csv(args.csv)
        for m in msgs:
            log.info("%s", m)
        log.info("종목코드 복원 %d건", n)
        return 0

    positions, warns = load_portfolio(args.csv)
    for w in warns:
        log.warning("%s", w)
    if not positions:
        log.info("보유 종목이 없습니다 — portfolio.csv 를 채우세요.")

    sess = args.session
    if not sess:
        c = sorted(glob.glob(os.path.join(OUTPUT_DIR, datetime.now().strftime("%Y-%m-%d") + "_*")))
        sess = c[-1] if c else None

    out = []
    for p in positions:
        lc = _last_close(p["ticker"])
        idx = _index_ret_since(p["buy_date"])
        row = compute_position(p, lc, idx)
        row["our_history"] = _our_history(p["ticker"])
        row["today_pick"] = _today_pick(p["ticker"], sess)
        out.append(row)
        log.info("  %s %s: 현재 %s / 손익 %s (%s)",
                 p["ticker"], p["name"],
                 format(int(lc), ",") if lc else "조회실패",
                 format(row["pnl"], ",") + "원" if row.get("pnl") is not None else "-",
                 ("%+.2f%%" % row["pnl_pct"]) if row.get("pnl_pct") is not None else "-")

    tot = portfolio_totals(out)

    print("")
    print("=" * 92)
    print("내 포트폴리오 (%s 기준 — 직전 거래일 종가)" % datetime.now().strftime("%Y-%m-%d"))
    print("=" * 92)
    print("%-8s %-12s %6s %11s %11s %10s %9s %8s"
          % ("티커", "종목", "수량", "평단가", "현재가", "평가손익", "수익률", "지수대비"))
    print("-" * 92)
    for r in out:
        print("%-8s %-12s %6d %11s %11s %10s %9s %8s"
              % (r["ticker"], (r.get("name") or "")[:11], r["qty"],
                 format(int(r["avg_price"]), ","),
                 format(int(r["last_close"]), ",") if r.get("last_close") else "-",
                 format(r["pnl"], ",") if r.get("pnl") is not None else "-",
                 ("%+.2f%%" % r["pnl_pct"]) if r.get("pnl_pct") is not None else "-",
                 ("%+.2f%%" % r["alpha_pct"]) if r.get("alpha_pct") is not None else "-"))
    print("-" * 92)
    if tot.get("total_cost"):
        print("합계: 원금 %s / 평가 %s / 손익 %s (%s)"
              % (format(tot["total_cost"], ","), format(tot["total_value"], ","),
                 format(tot["total_pnl"], ","),
                 ("%+.2f%%" % tot["total_pnl_pct"]) if tot.get("total_pnl_pct") is not None else "-"))
        if tot.get("top_weight_pct"):
            print("최대 비중: %s %s%%" % (tot.get("top_name"), tot["top_weight_pct"]))
    print("")

    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "asof_note": "직전 거래일 종가 기준(오늘 봉 제외 — 미확정값 차단)",
               "what": ("사용자의 실제 보유 포트폴리오 측정. ★사실만 담는다 — "
                        "매도/보유 판단은 분석가(코워크)가 쓴다."),
               "source_csv": os.path.basename(args.csv),
               "warnings": warns, "totals": tot, "positions": out}
    dest = args.out or os.path.join(sess or HERE, "portfolio_review.json")
    try:
        from common import save_json_atomic
        save_json_atomic(dest, payload)
        log.info("저장: %s", dest)
    except Exception as e:
        log.warning("저장 실패: %s", e)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
