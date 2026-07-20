# -*- coding: utf-8 -*-
"""
credit_collect.py — 신용잔고(빚투)·증시자금 수집 → 루트 credit_balance.json

[왜] 회고 원장 잔존 '크롤 3종' 중 신용잔고. 레버리지(신용융자) 수준·급증은 과열/반대매매
  압력의 국면 신호인데 지금까지 수집이 없었다. deriv/ecos/vkospi 와 같은 '루트 국면신호'
  컨벤션으로 저장하고 snapshot_signals 가 세션에 동결한다.

[출처] 금융투자협회 종합통계(freesis.kofia.or.kr) 공개 JSON 엔드포인트 — 키 불필요.
  POST /meta/getMetaDataList.do  body={"dmSearch":{..., "OBJ_NM": <오브젝트>}}
  - STATSCU0100000070BO = 신용공여 잔고 추이 (사이트 serviceId=STATSCU0100000070 페이지명으로 확인)
  - STATSCU0100000060BO = 증시자금 추이
  tmpV40=1000000000 → 값 단위 '십억원'(본 수집기는 억원으로 x10 변환 저장).

[컬럼 매핑 검증 — 2026-07-20, 언론 보도 실측 대조(추측 아님)]
  MBC 2026-05-18 보도: "신용거래융자 잔고 36조 5,675억(5/15·사상최고), 투자자예탁금 132조 8,596억"
  - 070BO 20260515: TMPV2=36,568(십억)=36.57조 → ★신용거래융자 '총계' 정확 일치
    TMPV3=25,988(유가)+TMPV4=10,580(코스닥)=36,568 → 합산 관계로 소속 확정
    TMPV5=55=TMPV6(47)+TMPV7(8) → 신용거래대주 총/유가/코스닥(내부 합산 정합)
    TMPV9=26,365(십억)=26.4조 → 예탁증권담보융자(스케일 정합 — '추정 매핑' 주석 유지)
  - 060BO 20260515: TMPV2=132,860(십억)=132.86조 → ★투자자예탁금 정확 일치
    TMPV5(위탁매매미수금)·TMPV6(반대매매)·TMPV7(비중%) — TMPV6/TMPV5≈TMPV7 내부 정합.
    TMPV3·TMPV4 는 미검증이라 사용하지 않는다(추측 금지).

[설계] 독립 실행·기존 파일 무수정·graceful(통신 실패 시 exit 0, 파일 미생성)·
  ASCII 로그 태그 [credit]·원자적 저장(common.save_json_atomic)·이모지 금지.
  level_label 임계값은 통계 최적화가 아니라 '정성 판단의 출발점' 어림값(기계 적용 금지).

[사용법]
  python credit_collect.py                # 최근 ~130일 조회 → 루트 credit_balance.json
  python credit_collect.py --check        # 통신/파싱만 검증(저장 안 함)
  python credit_collect.py --out x.json
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

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DEFAULT = os.path.join(HERE, "credit_balance.json")
URL = "http://freesis.kofia.or.kr/meta/getMetaDataList.do"
OBJ_CREDIT = "STATSCU0100000070BO"   # 신용공여 잔고 추이
OBJ_FUNDS = "STATSCU0100000060BO"    # 증시자금 추이

logging.basicConfig(level=logging.INFO, format="[credit] %(message)s")
log = logging.getLogger("credit")

from common import save_json_atomic


def fetch_rows(obj_nm, begin, end):
    """KOFIA freesis 오브젝트 조회 → 일별 row 리스트(오래된→최신). 실패 시 []."""
    if requests is None:
        log.info("requests 미설치 — 수집 불가")
        return []
    body = {"dmSearch": {"tmpV40": "1000000000", "tmpV41": "1", "tmpV1": "D",
                         "tmpV45": begin, "tmpV46": end, "OBJ_NM": obj_nm}}
    try:
        r = requests.post(URL, json=body, timeout=30,
                          headers={"Referer": "http://freesis.kofia.or.kr/",
                                   "User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            log.info("HTTP %s (%s) — 생략", r.status_code, obj_nm)
            return []
        rows = (r.json() or {}).get("ds1") or []
        rows = [x for x in rows if x.get("TMPV1")]
        rows.sort(key=lambda x: x["TMPV1"])          # 응답은 최신순 — 오래된→최신 정렬
        return rows
    except Exception as e:
        log.info("조회 실패(%s: %s) — 생략", type(e).__name__, str(e)[:120])
        return []


def _eok(v):
    """십억원 → 억원(x10). 숫자 아니면 None."""
    try:
        return round(float(v) * 10.0, 1)
    except (TypeError, ValueError):
        return None


def _d5_pct(vals):
    """직전 5거래행 대비 변화율(%). 위치 기반(양 끝 결측·0 이면 None — 갭 신장 방지)."""
    if len(vals) >= 6 and vals[-1] is not None and vals[-6]:
        return round((vals[-1] / vals[-6] - 1.0) * 100.0, 2)
    return None


def build_payload(credit_rows, funds_rows):
    """두 오브젝트 row → credit_balance.json payload(순수함수 — 하네스 테스트 대상).
    데이터 없으면 {}. 임계 라벨은 정성 판단의 '출발점' 어림값(기계 적용 금지)."""
    if not credit_rows:
        return {}
    m_dates = [r["TMPV1"] for r in credit_rows]
    m_total = [_eok(r.get("TMPV2")) for r in credit_rows]
    if m_total[-1] is None:
        return {}
    last = credit_rows[-1]
    asof = f"{m_dates[-1][:4]}-{m_dates[-1][4:6]}-{m_dates[-1][6:]}"

    valid = [v for v in m_total if v is not None]
    look = valid[-60:]
    # pct_rank 은 분포 순위라 결측 제거해도 무방. 단 d1/d5 는 '전일·5거래일 전' 위치 기반이라
    # 결측 제거(valid)로 계산하면 갭이 조용히 신장된다 → 위치 기반(m_total)으로 엄격히.
    pct_rank = round(sum(1 for v in look if v <= valid[-1]) / len(look) * 100.0, 1) if look else None
    d1 = (round(m_total[-1] - m_total[-2], 1)
          if len(m_total) >= 2 and m_total[-2] is not None else None)
    d5 = _d5_pct(m_total)

    margin = {
        "total_eok": m_total[-1], "kospi_eok": _eok(last.get("TMPV3")),
        "kosdaq_eok": _eok(last.get("TMPV4")),
        "d1_chg_eok": d1, "d5_chg_pct": d5, "pct_rank_60d": pct_rank,
    }
    short_sell_eok = _eok(last.get("TMPV5"))            # 신용거래대주(총)
    collateral_eok = _eok(last.get("TMPV9"))            # 예탁증권담보융자(추정 매핑 — 스케일 정합)

    deposit = None
    misu = None
    if funds_rows:
        f_last = funds_rows[-1]
        dep_series = [_eok(r.get("TMPV2")) for r in funds_rows]   # 위치 보존(갭 신장 방지)
        if dep_series and dep_series[-1] is not None:
            _fd = f_last.get("TMPV1") or ""
            _fasof = f"{_fd[:4]}-{_fd[4:6]}-{_fd[6:]}" if len(_fd) == 8 else _fd
            deposit = {"total_eok": dep_series[-1], "d5_chg_pct": _d5_pct(dep_series),
                       "asof": _fasof}
        try:
            misu = {"misu_eok": _eok(f_last.get("TMPV5")),
                    "rt_sell_eok": _eok(f_last.get("TMPV6")),
                    "rt_sell_ratio_pct": float(f_last.get("TMPV7"))
                    if f_last.get("TMPV7") is not None else None}
        except (TypeError, ValueError):
            misu = None

    if pct_rank is not None and pct_rank >= 95 and (d5 or 0) >= 3:
        label = "레버리지 급증(60일 최상위 + 5일 +3%p — 과열·반대매매 취약 경계)"
    elif (d5 is not None and d5 <= -3) or (misu and (misu.get("rt_sell_ratio_pct") or 0) >= 5):
        label = "디레버리징(융자 급감 또는 반대매매 비중 상승 — 투매 압력 소화 국면)"
    elif pct_rank is not None and pct_rank >= 90:
        label = "레버리지 상위권(경계 관찰)"
    else:
        label = "보통"

    return {
        "asof_date": asof,
        "source": "kofia_freesis",
        "what": "신용거래융자(빚투) 잔고·증시자금 — 레버리지 과열/반대매매 압력 국면 신호. 단위 억원.",
        "margin_loan": margin,
        "margin_short_sell_eok": short_sell_eok,
        "collateral_loan_eok": collateral_eok,
        "deposit": deposit,
        "misu": misu,
        "series": [[f"{d[:4]}-{d[4:6]}-{d[6:]}", v]
                   for d, v in zip(m_dates, m_total) if v is not None][-60:],
        "level_label": label,
        "note": ("임계값(백분위 95/90, 5일 ±3%, 반대매매 5%)은 정성 판단의 출발점 어림값 — "
                 "기계 적용 금지. 컬럼 매핑은 2026-05-15 언론 실측(36조5,675억/132조8,596억) 대조 검증. "
                 "collateral_loan 은 추정 매핑(주의)."),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def main():
    ap = argparse.ArgumentParser(description="신용잔고·증시자금 수집 → credit_balance.json")
    ap.add_argument("--days", type=int, default=130, help="조회 캘린더 일수(기본 130 — 60거래일 확보)")
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--check", action="store_true", help="통신/파싱만 검증(저장 안 함)")
    args = ap.parse_args()

    end = datetime.now().strftime("%Y%m%d")
    begin = (datetime.now() - timedelta(days=args.days)).strftime("%Y%m%d")
    credit_rows = fetch_rows(OBJ_CREDIT, begin, end)
    funds_rows = fetch_rows(OBJ_FUNDS, begin, end)
    payload = build_payload(credit_rows, funds_rows)
    if not payload:
        log.info("데이터 없음(통신 실패 또는 응답 공백) — 파일 미생성, 정상 종료")
        return 0
    if args.check:
        log.info("check: 융자행 %d·자금행 %d | asof=%s 융자총 %.0f억(%s)",
                 len(credit_rows), len(funds_rows), payload["asof_date"],
                 payload["margin_loan"]["total_eok"], payload["level_label"])
        return 0
    save_json_atomic(args.out, payload)
    log.info("저장: %s (asof=%s 융자총 %.0f억 d5=%s%% rank=%s%% | %s)",
             args.out, payload["asof_date"], payload["margin_loan"]["total_eok"],
             payload["margin_loan"]["d5_chg_pct"], payload["margin_loan"]["pct_rank_60d"],
             payload["level_label"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log.info("최상위 예외 흡수(%s: %s) — exit 0", type(e).__name__, str(e)[:120])
        sys.exit(0)
