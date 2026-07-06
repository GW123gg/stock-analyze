# -*- coding: utf-8 -*-
r"""
apify_key.py — Apify 토큰 로더(stock_research/apify_api.txt) — 다계정·교체 지원

[목적] 토큰을 데스크톱 JSON 대신 작업폴더의 apify_api.txt 에서 읽는다(교체 쉬움).
  한 줄당 토큰 1개(위=우선). 직접호출 경로(테스트/호스트)는 이 순서대로 시도하다
  402(크레딧 소진)/401 나면 다음 키로 폴백한다(rotate).

[보안] 토큰 전체는 출력하지 않는다. mask() 로 접두 9자만 표시.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, "apify_api.txt")
PLACEHOLDER = "PASTE_YOUR_APIFY_TOKEN_HERE"


def load_apify_keys():
    """apify_api.txt → [(token, label), ...] (위에서부터 순서 유지). 자리표시자/주석/빈 줄 제외."""
    out = []
    if not os.path.isfile(KEY_FILE):
        return out
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # 인라인 주석 분리: '토큰  # 메모'
                if "#" in line:
                    tok = line.split("#", 1)[0].strip()
                    label = line.split("#", 1)[1].strip()
                else:
                    tok, label = line, ""
                if tok and tok != PLACEHOLDER:
                    out.append((tok, label))
    except Exception:
        return out
    return out


def active_key():
    """맨 위(활성) 토큰. 없으면 None."""
    keys = load_apify_keys()
    return keys[0][0] if keys else None


def mask(tok):
    if not tok:
        return "(없음)"
    return tok[:9] + "***(len=%d)" % len(tok)


def remaining_usd(tok):
    """계정 잔여 크레딧(USD) = maxMonthlyUsageUsd - current.monthlyUsageUsd. 실패 시 None.
    /v2/users/me/limits 조회는 무료(수집 아님)."""
    try:
        import requests
    except Exception:
        return None
    try:
        r = requests.get("https://api.apify.com/v2/users/me/limits",
                         params={"token": tok}, timeout=20)
        if r.status_code != 200:
            return None
        d = r.json().get("data", {})
        cur = d.get("current", {}).get("monthlyUsageUsd")
        mx = d.get("limits", {}).get("maxMonthlyUsageUsd")
        if cur is None or mx is None:
            return None
        return max(0.0, float(mx) - float(cur))
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    keys = load_apify_keys()
    if not keys:
        print("[apify_key] 유효한 토큰 없음 — apify_api.txt 에 토큰을 넣어주세요(현재 자리표시자/빈 파일).")
    else:
        print("[apify_key] 키 %d개 로드:" % len(keys))
        for i, (t, lab) in enumerate(keys):
            print("  [%d] %s  %s" % (i, mask(t), ("(" + lab + ")") if lab else ""))
