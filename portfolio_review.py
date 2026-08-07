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
import re
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
CSV_FILE = os.path.join(HERE, "portfolio.csv")          # 구 단일 파일(하위호환)
PORTFOLIO_DIR = os.path.join(HERE, "portfolios")       # ★사람마다 <이메일>.csv

# ★자동매매 대상 증권사(미래에셋 계좌 = 노드 러너가 붙는 곳). 그 외는 '참고만'.
#   '카이로스' 는 그 노트북의 옛 이름 — 기존 CSV 호환용으로 계속 인식한다.
AUTO_BROKERS = ("카이로스", "미래에셋", "미래에셋카이로스", "kairos", "mirae")
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
def is_auto_broker(broker):
    """이 증권사가 자동매매 대상인가. 아니면 '참고만'이다.

    ★KB 등 다른 증권사 보유는 이 시스템이 주문을 낼 수 없다 — 사용자가 직접 매매한다.
      그래도 포트폴리오 전체를 봐야 비중·집중도·전략 판단이 맞으므로 함께 읽되, 구분해서 보여준다.
    """
    b = str(broker or "").strip().lower().replace(" ", "")
    return any(k.lower().replace(" ", "") in b or b in k.lower() for k in AUTO_BROKERS) if b else True


def email_from_path(path):
    """portfolios/<이메일>.csv → 이메일. 아니면 None."""
    base = os.path.basename(path)
    if not base.lower().endswith(".csv"):
        return None
    e = base[:-4].strip()
    return e if ("@" in e and "." in e.split("@")[-1] and len(e) > 5) else None


def list_people(folder=None):
    """portfolios/ 안의 (이메일, 경로) 목록. 이메일 형식이 아닌 파일은 무시."""
    folder = folder or PORTFOLIO_DIR
    out = []
    try:
        for n in sorted(os.listdir(folder)):
            p = os.path.join(folder, n)
            if not os.path.isfile(p):
                continue
            e = email_from_path(p)
            if e:
                out.append((e, p))
    except FileNotFoundError:
        pass
    return out


# =====================================================================
# 국가 (v11.15 — 미국·일본 주식)
# =====================================================================
# ★국가는 **종목 식별의 일부**다. 빼면 일본 7203(도요타)이 한국 007203 으로 조회되고,
#   증권사가 카이로스면 자동매매 대상으로까지 잡힌다(실측 확인).
COUNTRIES = {
    "KR": {"name": "한국", "cur": "KRW", "sym": "원", "suffix": "",
           "bench": "KS11", "bench_name": "코스피", "fx": None, "dp": 0},
    "US": {"name": "미국", "cur": "USD", "sym": "$", "suffix": "",
           "bench": "US500", "bench_name": "S&P500", "fx": "USD/KRW", "dp": 2},
    "JP": {"name": "일본", "cur": "JPY", "sym": "엔", "suffix": ".T",
           "bench": "N225", "bench_name": "닛케이225", "fx": "JPY/KRW", "dp": 1},
}
DEFAULT_COUNTRY = "KR"

# 환율(원/1단위) 상식 범위 — 벗어나면 단위를 잘못 적은 것이다.
#   ★엔은 증권사가 흔히 '100엔당'으로 보여준다. 900 을 그대로 적으면 평가액이 100배가 된다.
FX_SANE = {"USD": (500.0, 3000.0), "JPY": (3.0, 30.0)}

_COUNTRY_ALIAS = {
    "KR": "KR", "한국": "KR", "국내": "KR", "KOR": "KR", "KOSPI": "KR", "KRX": "KR", "": "KR",
    "US": "US", "미국": "US", "미주": "US", "USA": "US", "NASDAQ": "US", "NYSE": "US",
    "JP": "JP", "일본": "JP", "JPN": "JP", "도쿄": "JP", "TSE": "JP",
}


def normalize_country(raw):
    """'미국'/'US'/'usa' -> 'US'. 모르면 None(호출부가 거절한다). 빈칸은 KR(하위호환)."""
    s = str(raw or "").strip().upper().replace(" ", "")
    return _COUNTRY_ALIAS.get(s)


def country_meta(code):
    return COUNTRIES.get(code or DEFAULT_COUNTRY, COUNTRIES[DEFAULT_COUNTRY])


def normalize_ticker(raw, country=None):
    """종목코드 정규화. 반환 (ticker|None, 복원했나:bool).

    ★국가마다 코드 체계가 다르다 — 한 규칙으로 다루면 다른 나라 종목이 조회된다.
      · KR: 숫자 6자리(엑셀이 지운 선행 0 복원). `034020` / `34020` / `="034020"` / `A034020`
      · US: 영문 티커. `AAPL` / `aapl` / `BRK.B` / `BRK-B`
      · JP: 숫자 4자리(도쿄증권거래소). `7203`. **선행 0 을 채우지 않는다** — 채우면 KR 코드가 된다.
    """
    c = country or DEFAULT_COUNTRY
    s = str(raw or "").strip()
    if not s:
        return None, False
    # ="034020" — 엑셀에서 텍스트로 강제할 때 쓰는 형식
    if s.startswith('="') and s.endswith('"'):
        s = s[2:-1].strip()
    elif s.startswith("=") and len(s) > 1:
        s = s[1:].strip().strip('"')
    s = s.strip('"').strip("'").replace(" ", "")

    if c == "US":
        s = s.upper().replace("-", ".")          # BRK-B -> BRK.B
        if not re.match(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$", s):
            return None, False
        return s, False

    if c == "JP":
        s = s.replace("-", "")
        if s.upper().endswith(".T"):
            s = s[:-2]
        if not (s.isdigit() and len(s) == 4):    # ★4자리 고정. zfill 금지(KR 코드와 충돌)
            return None, False
        return s, False

    # KR
    s = s.replace("-", "")
    if len(s) > 6 and s[0].isalpha() and s[1:].isdigit():   # A034020 / KR7034020003
        s = s[1:]
    if not s.isdigit() or len(s) > 6:
        return None, False
    fixed = len(s) < 6
    return s.zfill(6), fixed


def market_symbol(ticker, country):
    """시세 조회용 심볼. 일본은 `.T` 를 붙여야 한다(7203 은 404, 7203.T 는 OK — 실측)."""
    return "%s%s" % (ticker, country_meta(country)["suffix"])


def excel_safe_ticker(tk):
    """엑셀이 다시 열어도 선행 0 을 안 지우게 하는 표기. `="034020"` 형식."""
    return '="%s"' % tk


_NAME_CACHE = {}


def _norm_name(s):
    """비교용 정규화 — 공백·괄호·특수문자 제거 + 대문자."""
    return "".join(ch for ch in str(s or "").upper()
                   if ch.isalnum() or "가" <= ch <= "힣")


def official_name(ticker):
    """KRX 공식 종목명. 조회 못 하면 None(네트워크·장애 시 막지 않기 위해)."""
    if ticker in _NAME_CACHE:
        return _NAME_CACHE[ticker]
    name = None
    # ★pykrx 는 없는 종목코드에 대해 자기 로깅 호출이 깨져 트레이스백을 쏟아낸다
    #   (CLAUDE.md 에 기록된 `not all arguments converted` 그 패턴 — 수집기 로그를 통째로
    #    날린 전례가 있다). 조회하는 동안만 막는다.
    _root = logging.getLogger()
    _prev = _root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        from pykrx import stock as _krx
        name = (_krx.get_market_ticker_name(ticker) or "").strip() or None
        if isinstance(name, str) and name.startswith("["):   # 조회 실패 시 리스트 문자열
            name = None
    except Exception:
        name = None
    finally:
        logging.disable(_prev)
    _NAME_CACHE[ticker] = name
    return name


def verify_ticker_name(ticker, typed_name):
    """종목코드가 정말 그 회사인가. 반환 (ok, 공식이름).

    ★오타 한 글자로 **전혀 다른 회사**가 분석된다. 실측: 두산에너빌리티(034020)를
      `30420` 으로 적어 0 을 채우자 `030420`(디패션)이 되어 그대로 통과했다.
      조회가 안 되거나 이름을 안 적었으면 막지 않는다(ok=True) — 확인 불가와 불일치는 다르다.
    """
    if not str(typed_name or "").strip():
        return True, None
    real = official_name(ticker)
    if not real:
        return True, None
    return _norm_name(real) == _norm_name(typed_name), real


def sniff_delimiter(head_line):
    """헤더 한 줄에서 구분자를 고른다. 엑셀 '유니코드 텍스트' 저장은 탭이다."""
    best, n = ",", head_line.count(",")
    for d in ("\t", ";", "|"):
        c = head_line.count(d)
        if c > n:
            best, n = d, c
    return best


# 메모에 증권사가 적혀 있는 경우(증권사 열이 아예 없는 옛 파일) 판별용.
# ★모르면 카이로스로 두지 않는다 — 자동매매 대상이 아닌 물량을 대상으로 오인하면
#   러너가 만질 수 없는 주식을 계획에 넣는다.
_BROKER_HINTS = (
    ("미래에셋", ("미래에셋", "미래에셋대우", "mirae", "카이로스", "kairos")),
    ("KB", ("kb", "케이비", "국민")),
    ("삼성", ("삼성증권",)),
    ("키움", ("키움",)),
    ("NH", ("nh", "농협")),
    ("한국투자", ("한국투자", "한투")),
    ("토스", ("토스",)),
)


def broker_from_memo(memo):
    """메모 문자열에서 증권사를 추정. 못 찾으면 None."""
    s = str(memo or "").lower()
    if not s:
        return None
    for label, keys in _BROKER_HINTS:
        for k in keys:
            if k in s:
                return label
    return None


def parse_row(row):
    """CSV 한 행 → 표준 dict. 못 읽으면 (None, 사유)."""
    def g(*names):
        for n in names:
            for k in row:
                if k and k.strip().replace(" ", "") == n:
                    return (row[k] or "").strip()
        return ""

    # ★국가 먼저 — 코드 체계·시세 심볼·벤치마크·통화가 전부 여기서 갈린다.
    ctry_raw = g("국가", "country", "시장", "market")
    country = normalize_country(ctry_raw)
    if country is None:
        return None, ("국가를 알 수 없음: %s (한국/미국/일본 중 하나)" % ctry_raw)
    meta = country_meta(country)

    tk_raw = g("종목코드", "ticker", "코드")
    if not tk_raw:
        return None, "종목코드 없음"
    tk, fixed = normalize_ticker(tk_raw, country)
    if not tk:
        hint = {"KR": "숫자 6자리", "US": "영문 티커(AAPL)", "JP": "숫자 4자리(7203)"}[country]
        return None, "종목코드 형식 오류: %s — %s는 %s" % (tk_raw, meta["name"], hint)
    price_raw = str(g("평단가", "avg_price", "매입가")).replace(",", "")
    qty_raw = str(g("수량", "qty", "주식개수", "개수")).replace(",", "")
    if not price_raw.strip():
        # 빈 칸을 0 으로 삼키면 손익이 조용히 틀린다 — 채우라고 말한다.
        return None, "평단가가 비어 있음 — 평균단가를 채워 주세요"
    try:
        price = float(price_raw)
        qty = int(float(qty_raw))
    except (TypeError, ValueError):
        return None, "평단가/수량이 숫자가 아님"
    if price <= 0 or qty <= 0:
        return None, "평단가·수량은 0보다 커야 함"
    d = g("매수일시", "buy_date", "매수일")[:10].replace("/", "-").replace(".", "-")
    memo = g("메모", "memo", "note")
    # ★증권사 열이 없는 옛 파일은 메모에서 찾아본다. 그래도 없을 때만 카이로스로 둔다
    #   (사용자의 자동매매 계좌가 카이로스라 그것이 기존 동작이다).
    broker = g("증권사", "broker", "계좌") or broker_from_memo(memo) or "미래에셋"

    # 매수 시점 평균 환율(원/1단위). 해외만 필요하다.
    buy_fx = None
    if meta["fx"]:
        fx_raw = str(g("매수환율", "buy_fx", "환율", "fx")).replace(",", "")
        if not fx_raw.strip():
            return None, ("%s 종목은 **매수환율**이 필요하다(1%s당 원). "
                          "비워 두면 원화 손익을 계산할 수 없다." % (meta["name"], meta["sym"]))
        try:
            buy_fx = float(fx_raw)
        except (TypeError, ValueError):
            return None, "매수환율이 숫자가 아님: %s" % fx_raw
        ok, why = fx_sane(meta["cur"], buy_fx)
        if not ok:
            return None, "매수환율 오류 — %s" % why

    # ★★자동매매는 **국내 주식만** 대상이다. 카이로스 러너는 국내 HTS 만 조작한다.
    #   국가를 안 보면 도요타(7203)가 국내 007203 으로 주문될 수 있다.
    auto = is_auto_broker(broker) and country == "KR"

    return {"country": country, "country_name": meta["name"],
            "currency": meta["cur"], "cur_symbol": meta["sym"],
            "ticker": tk, "name": g("종목명", "name") or tk,
            "avg_price": price, "qty": qty,
            "buy_fx": buy_fx,                   # 해외만. KR 은 None
            "buy_date": d,                      # ★선택 — 불타기/물타기면 의미가 없어 비워도 된다
            "broker": broker, "auto_tradable": auto,
            "memo": memo,
            "ticker_fixed": fixed, "ticker_raw": tk_raw}, ""


def merge_lots(rows):
    """같은 종목의 여러 매수 행 → 가중평균 1건. 매수일시는 가장 이른 날."""
    by = {}
    for r in rows:
        # ★증권사가 다르면 같은 종목이어도 따로 센다 — 카이로스 것만 자동매매 대상이라
        #   합쳐버리면 '얼마를 자동으로 팔 수 있는가'가 틀어진다.
        # ★국가도 키다 — 일본 7203 과 한국 007203 은 다른 회사다.
        c = r.get("country") or DEFAULT_COUNTRY
        t = (c, r["ticker"], r.get("broker") or "")
        if t not in by:
            by[t] = {"country": c, "country_name": r.get("country_name"),
                     "currency": r.get("currency"), "cur_symbol": r.get("cur_symbol"),
                     "ticker": r["ticker"], "name": r["name"], "qty": 0, "cost": 0.0,
                     "fx_cost": 0.0,
                     "buy_date": r["buy_date"], "lots": 0, "memo": r.get("memo", ""),
                     "broker": r.get("broker") or "미래에셋",
                     "auto_tradable": r.get("auto_tradable", True)}
        b = by[t]
        b["qty"] += r["qty"]
        b["cost"] += r["avg_price"] * r["qty"]
        # 매수환율도 **금액 가중평균** — 단순평균이면 큰 매수의 환율이 묻힌다.
        if r.get("buy_fx"):
            b["fx_cost"] += r["buy_fx"] * r["avg_price"] * r["qty"]
        b["lots"] += 1
        if r["buy_date"] and (not b["buy_date"] or r["buy_date"] < b["buy_date"]):
            b["buy_date"] = r["buy_date"]
        if r.get("memo") and r["memo"] not in (b["memo"] or ""):
            b["memo"] = (b["memo"] + " / " + r["memo"]).strip(" /")
    out = []
    for b in by.values():
        b["avg_price"] = round(b["cost"] / b["qty"], 2) if b["qty"] else 0.0
        b["buy_fx"] = round(b["fx_cost"] / b["cost"], 4) if b["cost"] and b["fx_cost"] else None
        b.pop("fx_cost", None)
        # 원가를 원화로 — 나라가 섞인 포트폴리오는 이게 없으면 합계를 못 낸다.
        b["cost_krw"] = b["cost"] * (b["buy_fx"] or 1.0)
        out.append(b)
    return sorted(out, key=lambda x: -x["cost_krw"])


def compute_position(pos, last_close, index_ret_pct=None, today=None, now_fx=None):
    """보유 1건 + 현재가 → 손익 지표. 사실만 계산하고 판정하지 않는다.

    ★해외 종목은 **현지 수익률과 원화 수익률을 나눈다.**
      달러로 +10% 올랐어도 원화가 10% 강세면 내 돈은 그대로다. 둘을 뭉뚱그리면
      '종목이 좋았나 환율이 좋았나'를 알 수 없다 — 알파(종목탓/시장탓)와 같은 축이다.
      `value`·`cost`·`pnl` 은 **전부 원화**로 통일한다(나라가 섞인 합계를 내야 하므로).
    """
    out = dict(pos)
    out["last_close"] = last_close
    ctry = pos.get("country") or DEFAULT_COUNTRY
    is_fx = country_meta(ctry)["fx"] is not None
    buy_fx = pos.get("buy_fx") or 1.0
    cur_fx = (now_fx if is_fx else 1.0)

    if not last_close or last_close <= 0:
        out.update({"value": None, "pnl": None, "pnl_pct": None, "alpha_pct": None,
                    "local_pnl_pct": None, "fx_pnl_pct": None})
        out["_note"] = "현재가 조회 실패"
        return out
    if is_fx and not cur_fx:
        out.update({"value": None, "pnl": None, "pnl_pct": None, "alpha_pct": None,
                    "local_pnl_pct": round((last_close / pos["avg_price"] - 1.0) * 100.0, 2),
                    "fx_pnl_pct": None})
        out["_note"] = "환율 조회 실패 — 원화 환산 불가"
        return out

    # 현지통화 기준(순수 종목 성과)
    local_ret = (last_close / pos["avg_price"] - 1.0) * 100.0
    out["local_pnl_pct"] = round(local_ret, 2)
    out["now_fx"] = round(cur_fx, 4) if is_fx else None
    # 환율 기여분
    out["fx_pnl_pct"] = round((cur_fx / buy_fx - 1.0) * 100.0, 2) if is_fx else None

    # 원화 기준(실제 내 돈)
    value = last_close * pos["qty"] * cur_fx
    cost = pos["avg_price"] * pos["qty"] * buy_fx
    out["value"] = round(value)
    out["cost"] = round(cost)
    out["pnl"] = round(value - cost)
    out["pnl_pct"] = round((value / cost - 1.0) * 100.0, 2) if cost else None
    # 지수 대비 — '내 종목이 나빴나, 시장이 나빴나'를 가른다(F8-b 와 같은 축)
    #   ★해외는 **현지 수익률 vs 현지 지수**로 비교한다(환율은 종목 선택과 무관).
    base = out["local_pnl_pct"] if is_fx else out["pnl_pct"]
    out["alpha_pct"] = (round(base - index_ret_pct, 2)
                        if index_ret_pct is not None and base is not None else None)
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
_FX_CACHE = {}


def fx_rate(country, today=None):
    """원/1단위 현재 환율. KR 은 1.0. 조회 실패는 None(0 으로 대체하지 않는다).

    ★JPY/KRW 는 **1엔당 원**이다(실측 8.94). 증권사가 흔히 쓰는 '100엔당'과 다르니
      사용자 입력을 받을 때도 단위를 명시하고 FX_SANE 로 검사한다.
    """
    m = country_meta(country)
    if not m["fx"]:
        return 1.0
    key = m["fx"]
    if key in _FX_CACHE:
        return _FX_CACHE[key]
    v = _last_close(key, today=today, raw=True)
    _FX_CACHE[key] = v
    return v


def fx_sane(currency, rate):
    """환율이 상식 범위인가. 반환 (ok, 사유|None)."""
    lo, hi = FX_SANE.get(currency, (0.0, float("inf")))
    if rate is None or rate <= 0:
        return False, "환율이 없다"
    if rate < lo:
        return False, ("환율 %s 은 너무 작다(%s~%s 범위). 단위를 확인하라." % (rate, lo, hi))
    if rate > hi:
        extra = ""
        if currency == "JPY" and lo <= rate / 100.0 <= hi:
            extra = " — '100엔당'으로 적으신 것 같다. 1엔당으로 바꾸면 %.2f 다." % (rate / 100.0)
        return False, ("환율 %s 은 너무 크다(%s~%s 범위).%s" % (rate, lo, hi, extra))
    return True, None


def _last_close(ticker, today=None, raw=False):
    """직전 거래일 종가. ★오늘 봉은 제외한다(A40 원칙 — 미확정 장중값 유입 차단).

    raw=True 면 ticker 를 그대로 쓴다(환율·지수처럼 국가 접미사가 필요 없는 심볼).
    """
    if not FDR_OK:
        return None
    try:
        df = fdr.DataReader(ticker, "2026-01-01")
        if df is None or df.empty:
            return None
        t = today or date.today()
        # ★결측(NaN)을 만나면 **더 뒤로 가야 한다.** 예전 코드는 '오늘 이전 첫 봉'에서
        #   무조건 반환해, 그 봉이 NaN 이면 None 을 돌려주고 멈췄다(실측: USD/KRW 의
        #   2026-08-06 이 NaN 이라 해외 손익이 통째로 비었다). 국내 종목도 직전일이
        #   결측이면 같은 증상이 난다.
        scanned = 0
        for ix in reversed(df.index):
            d = ix.date() if hasattr(ix, "date") else None
            if d is None or d >= t:
                continue
            scanned += 1
            if scanned > 15:            # 너무 오래된 값을 조용히 쓰지 않는다(상폐·거래정지)
                return None
            try:
                v = float(df.loc[ix, "Close"])
            except (TypeError, ValueError):
                continue
            if v == v and v > 0:        # v == v 는 NaN 판별
                return v
    except Exception:
        return None
    return None


def _index_ret_since(buy_date, today=None, country=None):
    """매수일 이후 **그 나라 지수** 수익률(%). 알파 계산용.

    ★미국 주식을 코스피와 비교하면 알파가 무의미하다 — 나라마다 벤치마크를 쓴다
      (KR=코스피 / US=S&P500 / JP=닛케이225. 전부 실측으로 조회 확인).
    """
    if not FDR_OK or not buy_date:
        return None
    try:
        df = fdr.DataReader(country_meta(country)["bench"], buy_date)
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
    raw, warns, delim = None, [], ","
    for enc in ("utf-8-sig", "cp949", "utf-8"):
        try:
            with open(path, encoding=enc, newline="") as f:
                # ★엑셀 '유니코드 텍스트'로 저장하면 **탭 구분**이다.
                #   쉼표로 고정해서 읽으면 헤더가 통째로 한 칸이 돼 "종목코드 없음"만 나온다.
                delim = sniff_delimiter(f.readline())
                f.seek(0)
                raw = list(csv.DictReader(f, delimiter=delim))
            break
        except (UnicodeDecodeError, LookupError):
            continue
        except Exception as e:
            return [], ["CSV 읽기 실패: %s" % e]
    if raw is None:
        return [], ["CSV 인코딩을 판별하지 못했습니다(UTF-8 로 저장해 보세요)"]
    if delim != ",":
        warns.append("구분자가 쉼표가 아닙니다(%s) — 읽기는 했지만 `--normalize` 로 "
                     "쉼표 CSV 로 바꿔 두면 안전합니다"
                     % {"\t": "탭", ";": "세미콜론", "|": "파이프"}.get(delim, delim))

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
        # ★코드가 그 회사가 맞는지 확인 — 틀리면 **다른 회사를 분석하게 된다.**
        # ★KRX 조회는 국내 종목만 가능하다. 해외는 확인 불가로 두고 막지 않는다
        #   ('확인 불가'와 '불일치'는 다르다 — 확인 못 한다고 버리면 미국주식이 전멸한다).
        ok, real = ((True, None) if p.get("country", "KR") != "KR"
                    else verify_ticker_name(p["ticker"], p["name"]))
        if not ok:
            warns.append("%d행 건너뜀 — 종목코드 %s 는 '%s' 입니다('%s' 아님). "
                         "코드를 확인해 주세요."
                         % (i, p["ticker"], real, p["name"]))
            continue
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
                delim = sniff_delimiter(f.readline())   # 탭 구분 파일도 고칠 수 있게
                f.seek(0)
                rd = csv.DictReader(f, delimiter=delim)
                raw = list(rd)
                cols = rd.fieldnames or []
            msgs.append("읽기 인코딩: %s%s" % (enc, "" if delim == "," else " (구분자 탭/기타 → 쉼표로 변환)"))
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
    ccol = next((c for c in cols if c and c.strip().replace(" ", "")
                 in ("국가", "country", "시장", "market")), None)
    for r in raw:
        # ★국가별로 코드 체계가 다르다 — 국가를 무시하면 미국 AAPL 이 버려지고
        #   일본 7203 이 한국 007203 으로 바뀐다.
        rc = normalize_country(r.get(ccol) if ccol else "") or DEFAULT_COUNTRY
        tk, fixed = normalize_ticker(r.get(tcol), rc)
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
def review_one(csv_path, email, sess, today=None):
    """한 사람의 포트폴리오를 측정해 payload 반환. 네트워크(시세) 사용."""
    positions, warns = load_portfolio(csv_path)
    for w in warns:
        log.warning("  [%s] %s", email, w)

    out = []
    for p in positions:
        c = p.get("country") or DEFAULT_COUNTRY
        lc = _last_close(market_symbol(p["ticker"], c), today, raw=True)
        idx = (_index_ret_since(p["buy_date"], today, country=c)
               if p.get("buy_date") else None)
        row = compute_position(p, lc, idx, today, now_fx=fx_rate(c, today))
        row["our_history"] = _our_history(p["ticker"])
        row["today_pick"] = _today_pick(p["ticker"], sess)
        out.append(row)

    tot = portfolio_totals(out)
    # ★자동매매 가능분과 참고분을 나눠 센다 — 이 시스템이 주문할 수 있는 건 카이로스뿐이다.
    auto = [r for r in out if r.get("auto_tradable")]
    manual = [r for r in out if not r.get("auto_tradable")]
    tot["auto_value"] = sum(r.get("value") or 0 for r in auto)
    tot["manual_value"] = sum(r.get("value") or 0 for r in manual)
    tot["n_auto"] = len(auto)
    tot["n_manual"] = len(manual)
    tot["brokers"] = sorted({(r.get("broker") or "") for r in out} - {""})

    return {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "email": email,
            "asof_note": "직전 거래일 종가 기준(오늘 봉 제외 — 미확정값 차단)",
            "what": ("실제 보유 포트폴리오 측정. ★사실만 담는다 — 매도/보유 판단은 분석가가 쓴다. "
                     "증권사가 '카이로스'인 것만 자동매매 대상이고, 나머지는 참고용(직접 매매)."),
            "source_csv": os.path.basename(csv_path),
            "warnings": warns, "totals": tot, "positions": out}


def _print_table(payload):
    out = payload["positions"]
    tot = payload["totals"]
    print("")
    print("=" * 104)
    print("포트폴리오: %s   (%s 기준 — 직전 거래일 종가)"
          % (payload["email"], datetime.now().strftime("%Y-%m-%d")))
    print("=" * 104)
    print("%-4s %-8s %-8s %-11s %5s %11s %11s %11s %8s %8s %8s"
          % ("국가", "증권사", "티커", "종목", "수량", "평단가", "현재가",
             "평가손익(원)", "수익률", "환차익", "지수대비"))
    print("-" * 104)

    def _amt(v, dp):
        """현지통화 금액 — 달러·엔은 소수점이 의미 있다(250.35 를 250 으로 자르면 안 된다)."""
        if v is None:
            return "-"
        return format(v, ",.%df" % dp) if dp else format(int(v), ",")

    for r in out:
        mark = "" if r.get("auto_tradable") else " *"
        dp = country_meta(r.get("country"))["dp"]
        print("%-4s %-8s %-8s %-11s %5d %11s %11s %11s %8s %8s %8s%s"
              % (country_meta(r.get("country"))["name"],
                 (r.get("broker") or "")[:8], r["ticker"], (r.get("name") or "")[:10], r["qty"],
                 _amt(r.get("avg_price"), dp), _amt(r.get("last_close"), dp),
                 format(r["pnl"], ",") if r.get("pnl") is not None else "-",
                 ("%+.2f%%" % r["pnl_pct"]) if r.get("pnl_pct") is not None else "-",
                 ("%+.2f%%" % r["fx_pnl_pct"]) if r.get("fx_pnl_pct") is not None else "-",
                 ("%+.2f%%" % r["alpha_pct"]) if r.get("alpha_pct") is not None else "-", mark))
    print("-" * 104)
    # 해외가 있으면 통화·환율 기준을 명시한다 — 평단가 250 과 70,000 이 같은 열에 섞이므로
    _fx_used = {r.get("country"): r.get("now_fx") for r in out if r.get("now_fx")}
    if _fx_used:
        print("  ※ 평단가·현재가는 **현지 통화**(미국 $, 일본 엔), 평가손익은 **원화**. "
              + " / ".join("%s %s원" % (country_meta(c)["cur"], format(v, ",.2f"))
                           for c, v in sorted(_fx_used.items())))
        print("  ※ 수익률=원화 기준(환율 포함) · 환차익=그중 환율이 만든 몫 · "
              "지수대비=현지 수익률 - 그 나라 지수")
    if tot.get("total_cost"):
        print("합계: 원금 %s / 평가 %s / 손익 %s (%s)"
              % (format(tot["total_cost"], ","), format(tot["total_value"], ","),
                 format(tot["total_pnl"], ","),
                 ("%+.2f%%" % tot["total_pnl_pct"]) if tot.get("total_pnl_pct") is not None else "-"))
        if tot.get("n_manual"):
            print("  주문 가능 %d종 %s원 / * 직접 매매 %d종 %s원"
                  % (tot["n_auto"], format(tot["auto_value"], ","),
                     tot["n_manual"], format(tot["manual_value"], ",")))
        if tot.get("top_weight_pct"):
            print("  최대 비중: %s %s%%" % (tot.get("top_name"), tot["top_weight_pct"]))
    print("")


def main():
    ap = argparse.ArgumentParser(description="포트폴리오 측정(★판정하지 않는다)")
    ap.add_argument("--email", default=None, help="이 사람만(portfolios/<이메일>.csv)")
    ap.add_argument("--all", action="store_true", help="portfolios/ 의 전원")
    ap.add_argument("--dir", default=None, help="포트폴리오 폴더(기본 portfolios/)")
    ap.add_argument("--csv", default=None, help="특정 CSV 직접 지정(구 단일 파일 호환)")
    ap.add_argument("--session", default=None, help="세션폴더(없으면 오늘 세션 자동)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--normalize", action="store_true",
                    help="엑셀이 망가뜨린 CSV 를 고친다(선행 0 복원 + UTF-8 BOM 저장)")
    args = ap.parse_args()

    # 대상 결정: --csv > --email > --all > (기본) 전원
    targets = []
    if args.csv:
        targets = [(email_from_path(args.csv) or "(파일지정)", args.csv)]
    elif args.email:
        p = os.path.join(args.dir or PORTFOLIO_DIR, args.email + ".csv")
        if not os.path.isfile(p):
            log.error("그 사람의 포트폴리오가 없다: %s", p)
            return 1
        targets = [(args.email, p)]
    else:
        targets = list_people(args.dir)
        if not targets and os.path.isfile(CSV_FILE):
            targets = [("(구 portfolio.csv)", CSV_FILE)]   # 하위호환
    if not targets:
        log.error("portfolios/ 에 <이메일>.csv 가 하나도 없다.")
        return 1

    if args.normalize:
        total = 0
        for email, path in targets:
            n, msgs = normalize_csv(path)
            log.info("[%s] 복원 %d건", email, n)
            for m in msgs:
                log.info("   %s", m)
            total += n
        log.info("전체 복원 %d건", total)
        return 0

    sess = args.session
    if not sess:
        c = sorted(glob.glob(os.path.join(OUTPUT_DIR, datetime.now().strftime("%Y-%m-%d") + "_*")))
        sess = c[-1] if c else None

    made = []
    for email, path in targets:
        log.info("측정: %s", email)
        payload = review_one(path, email, sess)
        _print_table(payload)
        # 저장 — 사람마다 파일을 나눈다(남의 것과 섞이면 안 된다)
        if args.out:
            # ★여러 명인데 --out 하나면 뒷사람이 앞사람을 덮어써 **남의 데이터로 바뀐다**.
            #   사람 수가 2 이상이면 이름에 이메일을 끼워 강제로 분리한다.
            if len(targets) > 1:
                base, ext = os.path.splitext(args.out)
                dest = "%s_%s%s" % (base, email, ext or ".json")
            else:
                dest = args.out
        elif email.count("@") == 1:
            dest = os.path.join(sess or HERE, "portfolio_review_%s.json" % email)
        else:
            dest = os.path.join(sess or HERE, "portfolio_review.json")
        try:
            from common import save_json_atomic
            save_json_atomic(dest, payload)
            log.info("저장: %s", dest)
            made.append(dest)
        except Exception as e:
            log.warning("저장 실패: %s", e)
    log.info("완료 — %d명 측정", len(made))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
