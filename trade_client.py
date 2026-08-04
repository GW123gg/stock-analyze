#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trade_client.py — 카이로스 매매 서버(:8443) 호출 클라이언트 [v11.9 신규]

[구조] pc21(여기)이 **호출하는 쪽**, 노트북(desktop-psk2gpr)이 카이로스를 조작하는 쪽.
  캡처 에이전트(:8788, kairos_client.py)와 **다른 서버·다른 토큰·다른 헤더**다:
    8788 → `X-Capture-Token`      (캡처 전용, 주문 기능 없음)
    8443 → `Authorization: Bearer` (주문·시세·계좌)
  두 토큰을 섞으면 401 이 난다. 이 파일은 8443 만 다룬다.

[★★이 파일이 하지 않는 것 — 설계상 금지]
  1) **주문을 체결시키지 않는다.** `fire()` 는 있지만 이 모듈 어디서도 자동 호출하지 않으며,
     서버가 DRY-RUN 이면 폼만 채우고 멈추고, LIVE 라도 `auto_confirm=0` 이면 확인 패널을
     열어둔 채 멈춘다 — **확인 버튼은 사람이 카이로스 앞에서 누른다.**
  2) `live_enabled` 를 켜지 않는다(그건 노트북 config.toml + 서버 재시작이며 사용자 결정이다).
  3) `enforce_cash` 를 끄거나 `/account/manual` 에 임의 값을 넣지 않는다.
  4) 실패해도 **재시도 루프를 돌지 않는다** — 중복 주문이 이 시스템 최대 위험이다.

[비밀] 토큰은 `kairos_trade_api.txt`(= *.txt 이므로 .gitignore 차단)에서 읽는다.
  형식: `token=...` 한 줄, 또는 파일 전체가 토큰 한 줄. **어떤 경로로도 값을 출력하지 않는다.**

[사용법]
  python trade_client.py status              # 서버 모드·안전상태·계좌 (토큰 필요)
  python trade_client.py account --refresh   # 화면에서 예수금 다시 읽어 갱신
  python trade_client.py quote 005930        # 현재가 + 호가 20단
  python trade_client.py armed               # 지금 폼에 채워진 주문
  python trade_client.py disarm              # 서버 메모리의 arm 해제(화면 입력은 남음)
"""
import os
import sys
import json
import argparse
import logging
import urllib.request
import urllib.error

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "kairos_trade_api.txt")
CONFIG_FILE = os.path.join(HERE, "kairos_config.txt")

# 테일넷 이름 우선, IP 폴백(테일넷 IP 는 바뀔 수 있다)
DEFAULT_BASES = ("http://desktop-psk2gpr:8443", "http://100.84.184.80:8443")
DEFAULT_TIMEOUT = 120          # 주문 조작은 피코 물리 클릭이라 느리다(뮤텍스 대기 포함)

logging.basicConfig(level=logging.INFO, format="[trade] %(message)s")
log = logging.getLogger("trade")


class TradeError(RuntimeError):
    """매매 서버 호출/검증 실패. ★메시지에 토큰을 절대 넣지 마라."""


# =====================================================================
# 설정·토큰
# =====================================================================
def load_token(path=TOKEN_FILE) -> str:
    """kairos_trade_api.txt 에서 토큰. 'token=...' 또는 한 줄. 없으면 ''."""
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, _, v = line.partition("=")
                    if k.strip().lower() in ("token", "trade_token", "access_token"):
                        return v.strip()
                    continue
                return line
    except Exception:
        return ""
    return ""


def load_bases():
    """kairos_config.txt 의 trade_base=... 우선, 없으면 기본 후보."""
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8", errors="replace") as f:
                for raw in f:
                    line = raw.strip()
                    if line.startswith("trade_base="):
                        b = line.split("=", 1)[1].strip()
                        if b:
                            return (b,)
        except Exception:
            pass
    return DEFAULT_BASES


# =====================================================================
# 저수준 호출
# =====================================================================
def _request(base, path, token, body=None, method=None, timeout=DEFAULT_TIMEOUT):
    url = base.rstrip("/") + path
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token     # ★8788 과 헤더가 다르다
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    m = method or ("POST" if data is not None else "GET")
    req = urllib.request.Request(url, data=data, headers=headers, method=m)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except Exception:
        return {"ok": True, "_raw": raw[:400]}


def call(path, token=None, body=None, method=None, timeout=DEFAULT_TIMEOUT, bases=None):
    """base 후보를 순서대로 시도(이름 → IP). ★토큰 미노출·재시도 루프 없음."""
    token = load_token() if token is None else token
    bases = bases or load_bases()
    last = None
    for b in bases:
        try:
            return _request(b, path, token, body=body, method=method, timeout=timeout)
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode("utf-8", "replace"))
                detail = payload.get("detail") or payload.get("message") or str(payload)[:200]
            except Exception:
                detail = ""
            # 4xx 는 다른 base 로 재시도해도 같은 답이다 → 즉시 중단
            if 400 <= e.code < 500:
                hint = ""
                if e.code == 401:
                    hint = " (kairos_trade_api.txt 토큰 확인 — 8788 캡처 토큰과 다른 값이다)"
                elif e.code == 423:
                    hint = " (킬스위치/긴급정지/수동모드 — 해제는 사용자가)"
                raise TradeError("HTTP %d %s%s" % (e.code, detail, hint))
            last = TradeError("HTTP %d %s" % (e.code, detail))
        except Exception as e:                       # 연결 실패 → 다음 base
            last = TradeError("%s: %s" % (type(e).__name__, e))
    raise last or TradeError("호출 실패(원인 불명)")


# =====================================================================
# 읽기 API (부작용 없음 — /quote·/account/refresh 는 화면 전환은 하나 주문은 아니다)
# =====================================================================
def status(token=None):
    """서버 모드(LIVE/DRYRUN)·안전 플래그·계좌 요약. **주문 전 반드시 확인.**"""
    return call("/status", token=token, timeout=30)


def account(token=None):
    """positions.json 기준 예수금·보유. ★파일 값이라 실제와 다를 수 있다 — refresh 병행."""
    return call("/account", token=token, timeout=30)


def account_refresh(token=None):
    """[0611] 매수탭의 '현금주문자산'을 화면에서 읽어 갱신(읽기 전용, 주문 안 함)."""
    return call("/account/refresh", token=token, body={}, timeout=DEFAULT_TIMEOUT)


def quote(symbol, token=None):
    """[0626] X-Ray 화면 판독 → 현재가·호가 20단·예수금.
    ★`ok`(=quote_ok)와 `ladder_ok` 는 신뢰도가 따로다 — 호가를 쓰려면 ladder_ok 를 봐라."""
    return call("/quote?symbol=%s" % symbol, token=token, timeout=DEFAULT_TIMEOUT)


def limits(symbol, price, token=None):
    """그 가격에 낼 수 있는 최대 수량(예수금·한도 반영)."""
    return call("/account/limits?symbol=%s&price=%s" % (symbol, int(price)),
                token=token, timeout=30)


def armed(token=None):
    """지금 서버 메모리에 준비된 주문(폼에 채워진 것)."""
    return call("/order/armed", token=token, timeout=30)


# =====================================================================
# 주문 준비 (★체결 아님)
# =====================================================================
def arm(symbol, side, qty, token=None):
    """[0611] 로 전환 + 종목·수량을 폼에 채운다. **가격은 비우고 버튼은 안 누른다.**"""
    if side not in ("buy", "sell"):
        raise TradeError("side 는 buy/sell 이어야 한다: %r" % side)
    q = int(qty)
    if q <= 0:
        raise TradeError("수량은 1 이상이어야 한다: %r" % qty)
    return call("/order/arm", token=token,
                body={"symbol": str(symbol), "side": side, "qty": q})


def disarm(token=None):
    """서버 메모리의 arm 해제. ★화면에 입력된 값은 그대로 남는다(사람이 지워야 한다)."""
    return call("/order/disarm", token=token, body={})


def fire(price, last_price, token=None, i_understand_this_may_submit=False):
    """가격을 채운다. 서버가 DRY-RUN 이면 여기서 멈추고, LIVE 면 확인 패널까지 연다.

    ★★이 함수는 **자동으로 호출되지 않는다.** 호출자가 명시적으로 플래그를 세워야 한다 —
      LIVE + auto_confirm=1 조합에서는 이 호출이 실제 주문 제출로 이어질 수 있기 때문이다.
      (기본 설정은 auto_confirm=0 이라 사람이 확인 버튼을 누르지만, 설정은 바뀔 수 있다.)
    ★last_price 를 반드시 넘겨라 — 없으면 서버의 ±5% 가격밴드 검사가 통째로 건너뛰어진다.
    """
    if not i_understand_this_may_submit:
        raise TradeError(
            "fire() 는 명시적 승인 없이 호출할 수 없다 — "
            "i_understand_this_may_submit=True 를 넘겨라. "
            "(주문 제출로 이어질 수 있는 유일한 지점이다)")
    if last_price is None:
        raise TradeError("last_price 없이 fire 금지 — 가격밴드 검사가 무력화된다.")
    return call("/order/fire", token=token,
                body={"price": int(price), "last_price": int(last_price)})


# =====================================================================
# 안전 장치
# =====================================================================
def emergency_stop(token=None):
    """긴급정지 + 미체결 전체취소 '시도'.
    ★응답의 `cancel_all` 을 반드시 확인하라 — false 면 미체결이 그대로 살아 있다.
      true 여도 '올취 버튼을 눌렀다'는 뜻이지 '비었다'는 검증은 아니다(주문내역 직접 확인)."""
    return call("/emergency_stop", token=token, body={})


def killswitch(engage=True, token=None):
    """주문 전면 차단(해제 포함)."""
    return call("/killswitch", token=token, body={"engage": bool(engage)})


# =====================================================================
# 진단 요약 (사람이 읽는 한 줄)
# =====================================================================
def describe_status(st) -> str:
    """status() 응답 → 한 줄 요약. 이모지 금지(cp949)."""
    if not isinstance(st, dict):
        return "상태 파싱 실패"
    mode = st.get("mode") or ("LIVE" if st.get("live_enabled") else "DRYRUN")
    bits = ["mode=%s" % mode]
    for k, label in (("killswitch", "킬스위치"), ("emergency_stop", "긴급정지"),
                     ("manual_mode", "수동모드")):
        if st.get(k):
            bits.append("%s ON" % label)
    acct = st.get("account") or {}
    if acct:
        bits.append("현금=%s" % acct.get("cash"))
    if st.get("enforce_cash") is not None:
        bits.append("enforce_cash=%s" % st.get("enforce_cash"))
    return " | ".join(str(b) for b in bits)


# =====================================================================
# CLI (읽기 명령만 — 주문 관련은 order_plan.py 가 다룬다)
# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="카이로스 매매 서버(:8443) 클라이언트 — 읽기 전용 CLI")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status", help="서버 모드·안전상태·계좌")
    a_acc = sub.add_parser("account", help="예수금·보유")
    a_acc.add_argument("--refresh", action="store_true", help="화면에서 다시 읽어 갱신")
    a_q = sub.add_parser("quote", help="현재가·호가")
    a_q.add_argument("symbol")
    sub.add_parser("armed", help="폼에 채워진 주문")
    sub.add_parser("disarm", help="arm 해제(화면 입력은 남음)")
    args = ap.parse_args()

    if not load_token():
        log.warning("kairos_trade_api.txt 에 토큰이 없다 — 모든 호출이 401 이 난다.")
        log.warning("  노트북 config.toml 의 [auth] access_token 값을 그 파일에 넣어라"
                    " (형식: token=... / 이 파일은 *.txt 라 git 에 안 올라간다).")

    try:
        if args.cmd == "status":
            st = status()
            print(describe_status(st))
            print(json.dumps(st, ensure_ascii=False, indent=2)[:1500])
        elif args.cmd == "account":
            if args.refresh:
                print("화면에서 예수금 재조회(피코 사용 — 배치와 겹치면 대기)...")
                print(json.dumps(account_refresh(), ensure_ascii=False, indent=2)[:800])
            print(json.dumps(account(), ensure_ascii=False, indent=2)[:1200])
        elif args.cmd == "quote":
            q = quote(args.symbol)
            qq = q.get("quote") or {}
            ld = q.get("ladder") or {}
            print("%s  현재가=%s (%s%%)  quote_ok=%s ladder_ok=%s"
                  % (args.symbol, qq.get("price"), qq.get("rate"),
                     q.get("quote_ok"), q.get("ladder_ok")))
            if q.get("ladder_ok"):
                print("  최우선 매도=%s / 매수=%s" % (ld.get("best_ask"), ld.get("best_bid")))
            else:
                print("  ★호가 판독 실패 — levels 는 비어 있다(추측 금지)")
            if q.get("problems"):
                print("  problems:", q["problems"])
        elif args.cmd == "armed":
            print(json.dumps(armed(), ensure_ascii=False, indent=2))
        elif args.cmd == "disarm":
            print(json.dumps(disarm(), ensure_ascii=False, indent=2))
            print("※ 서버 메모리만 비웠다 — 화면의 입력값은 사람이 지워야 한다.")
        else:
            ap.print_help()
            return 0
    except TradeError as e:
        log.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
