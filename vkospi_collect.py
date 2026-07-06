# -*- coding: utf-8 -*-
"""
vkospi_collect.py — VKOSPI(코스피200 변동성지수) 수집 → 루트 vkospi.json

[왜] 회고 F1 게이트(cowork_instructions [6.6] F1)가 "VKOSPI 역대 고공/급등이면 '공포'로 보고 롱 회피"를
  요구하는데 지금까지 수집기가 없어 Cowork 가 매번 웹검색으로 때웠다. 이 수집기가 그 공백을 채운다.
  (회고 3회 재현 최강 발견 = '추천일 시장 국면이 결과를 좌우' → 국면 판별 데이터 보강.)

[API] 금융위원회_지수시세정보 (공공데이터포털 data.go.kr)
  https://apis.data.go.kr/1160100/service/GetMarketIndexInfoService/getDerivationProductMarketIndex
  - 인증: serviceKey — vkospi_api.txt (없으면 fsc_api.txt 폴백: 같은 공공데이터포털 키로
    '금융위원회_지수시세정보' 활용신청만 하면 동일 키 사용 가능). REST GET, resultType=json.
  - likeIdxNm=변동성 으로 조회 후 idxNm 에 '변동성' 포함 항목(VKOSPI)만 채택.
  - 응답 item: basDt, idxNm, clpr(종가), vs(대비), fltRt(등락률%), mkp/hipr/lopr 등.

[출력] 루트 vkospi.json (deriv/ecos/market_caution 과 동일한 '루트 국면신호' 컨벤션):
  {asof_date, source, latest{value, chg_pct, d5_chg_pct}, pct_rank_60d, level_label,
   series[[YYYY-MM-DD, close], ...(오래된→최신)], generated_at}
  ※ level_label 임계값은 통계 최적화가 아니라 '정성 판단의 출발점' 어림값(policy §9 철학).

[설계] 독립 실행·기존 파일 무수정·graceful(키 없음/통신 실패 시 exit 0, 파일 미생성)·
  ASCII 로그 태그 [vkospi]·비밀키 출력 금지·원자적 저장(common.save_json_atomic).

[사용법]
  python vkospi_collect.py                 # 최근 ~90일 → 루트 vkospi.json
  python vkospi_collect.py --check         # 키/통신만 검증(수집 안 함)
  python vkospi_collect.py --days 120 --out x.json
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
KEY_FILE = os.path.join(HERE, "vkospi_api.txt")
FSC_KEY_FILE = os.path.join(HERE, "fsc_api.txt")
OUT_DEFAULT = os.path.join(HERE, "vkospi.json")

API_URL = ("https://apis.data.go.kr/1160100/service/"
           "GetMarketIndexInfoService/getDerivationProductMarketIndex")

logging.basicConfig(level=logging.INFO, format="[vkospi] %(message)s")
log = logging.getLogger("vkospi")

from common import save_json_atomic


# =====================================================================
# 키 로딩 — vkospi_api.txt 우선, 없으면 fsc_api.txt 폴백 (형식은 fsc 와 동일)
# =====================================================================
def _read_key_file(path) -> str:
    """'key=...' / 'servicekey=...' 또는 키 한 줄. 비밀값 — 절대 로그/출력 금지."""
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip().lower() in ("key", "servicekey", "service_key",
                                             "vkospi_key", "fsc_key", "apikey"):
                        return v.strip().strip('"').strip("'")
                    continue
                return line.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _is_placeholder(k: str) -> bool:
    """미기입 플레이스홀더 감지(ecos 로더와 동일 정책 — 가짜 키 전송 방지)."""
    return (not k) or ("여기에" in k) or ("붙여넣" in k) or ("YOUR_" in k.upper()) or len(k) < 20


def load_key() -> tuple:
    """(key, 출처파일명). vkospi_api.txt → fsc_api.txt 순. 플레이스홀더는 무시."""
    k = _read_key_file(KEY_FILE)
    if not _is_placeholder(k):
        return k, "vkospi_api.txt"
    k = _read_key_file(FSC_KEY_FILE)
    if not _is_placeholder(k):
        return k, "fsc_api.txt(폴백)"
    return "", ""


# =====================================================================
# 조회 + 파싱 (파싱은 순수함수 — 단독 테스트 가능)
# =====================================================================
def fetch_items(key: str, begin: str, end: str, num_rows: int = 200) -> list:
    """파생상품지수시세에서 '변동성' 지수 항목을 가져온다. 실패 시 빈 리스트."""
    if requests is None:
        log.info("requests 미설치 — 수집 불가")
        return []
    p = {"serviceKey": key, "resultType": "json",
         "numOfRows": str(num_rows), "pageNo": "1",
         "beginBasDt": begin, "endBasDt": end,
         "likeIdxNm": "변동성"}
    try:
        r = requests.get(API_URL, params=p, timeout=30)
        if r.status_code != 200:
            log.info("HTTP %s — 키 미등록/활용신청 필요 가능(공공데이터포털에서 "
                     "'금융위원회_지수시세정보' 활용신청)", r.status_code)
            return []
        body = r.json().get("response", {}).get("body", {})
        items = (body.get("items") or {}).get("item") or []
        if isinstance(items, dict):
            items = [items]
        return items
    except Exception as e:
        log.info("조회 실패(%s: %s) — 생략", type(e).__name__, str(e)[:120])
        return []


def build_payload(items: list) -> dict:
    """API item 리스트 → vkospi.json payload. 데이터 없으면 {}.
    임계 라벨은 정성 판단의 '출발점' 어림값이다(기계 적용 금지)."""
    rows = {}
    for it in items:
        try:
            name = str(it.get("idxNm") or "")
            if "변동성" not in name:
                continue
            d = str(it.get("basDt") or "")
            c = float(it.get("clpr"))
            if len(d) == 8:
                rows[f"{d[:4]}-{d[4:6]}-{d[6:]}"] = round(c, 2)
        except Exception:
            continue
    if not rows:
        return {}
    series = sorted(rows.items())                      # 오래된 → 최신
    closes = [c for _, c in series]
    last_d, last_c = series[-1]
    chg = round((last_c / closes[-2] - 1) * 100, 2) if len(closes) >= 2 and closes[-2] else None
    d5 = round((last_c / closes[-6] - 1) * 100, 2) if len(closes) >= 6 and closes[-6] else None
    look = closes[-60:]
    below = sum(1 for c in look if c <= last_c)
    pct_rank = round(below / len(look) * 100, 1)       # 60거래일 내 백분위(높을수록 공포 상위)
    if last_c >= 25 or (d5 is not None and d5 >= 30):
        label = "공포(고공/급등 — F1: 롱 회피 검토)"
    elif last_c >= 20 or pct_rank >= 90:
        label = "경계(변동성 상위권)"
    elif last_c <= 14:
        label = "안정(저변동성 — 안도 국면)"
    else:
        label = "보통"
    return {
        "asof_date": last_d,
        "source": "fsc_index",
        "latest": {"value": last_c, "chg_pct": chg, "d5_chg_pct": d5},
        "pct_rank_60d": pct_rank,
        "level_label": label,
        "series": series[-90:],
        "note": "임계값(25/20/14, d5 +30%)은 정성 판단의 출발점 어림값 — 맥락과 함께 해석",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="VKOSPI 수집 → vkospi.json")
    ap.add_argument("--days", type=int, default=90, help="조회 캘린더 일수(기본 90)")
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--check", action="store_true", help="키/통신만 검증")
    args = ap.parse_args()

    key, src = load_key()
    if not key:
        log.info("키 없음(vkospi_api.txt / fsc_api.txt) — 수집 생략. "
                 "vkospi_api.txt 에 공공데이터포털 인증키를 넣으면 동작한다.")
        return 0
    log.info("키 로드: %s", src)

    end = datetime.now().strftime("%Y%m%d")
    begin = (datetime.now() - timedelta(days=args.days)).strftime("%Y%m%d")
    items = fetch_items(key, begin, end)
    if args.check:
        log.info("check: 응답 item %d건 (변동성 필터 전)", len(items))
        return 0
    payload = build_payload(items)
    if not payload:
        log.info("VKOSPI 데이터 없음(활용신청 전이거나 응답 비어있음) — 파일 미생성, 정상 종료")
        return 0
    save_json_atomic(args.out, payload)
    log.info("저장: %s (asof=%s value=%.2f %s | 60d백분위 %.0f%%)",
             args.out, payload["asof_date"], payload["latest"]["value"],
             payload["level_label"], payload["pct_rank_60d"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log.info("최상위 예외 흡수(%s: %s) — exit 0", type(e).__name__, str(e)[:120])
        sys.exit(0)
