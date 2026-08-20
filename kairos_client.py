# -*- coding: utf-8 -*-
"""
kairos_client.py — 카이로스 캡처 에이전트(노트북) 호출 클라이언트 [v10.8 신규]

[구조] pc21(여기)이 **호출하는 쪽**, 노트북(desktop-psk2gpr)이 캡처하는 쪽.
  카이로스는 소프트웨어 가짜 입력을 차단하므로 모든 클릭·입력은 노트북에 물린 **라즈베리파이
  피코(실물 USB HID)** 가 수행한다. 여기서는 HTTP 명령만 보낸다. 통신은 Tailscale 테일넷 내부.

[★검증 3종 — 조용한 오류가 이 시스템 최대 위험]
  1) marker_text 의 [번호] == 요청 화면번호   (노트북이 창 제목에서 '실제로 읽은' 값이다.
     요청값을 되돌려주지 않으므로, 이게 다르면 엉뚱한 화면이 찍힌 것 → 폐기)
  2) sha256(png) == 응답의 sha256            (전송 손상·중간 교체)
  3) settled == True                          (화면 갱신 중 캡처면 값이 흔들린다 → 재요청)

[★watchlist 생애주기] 0231·0261 은 관심종목이 비면 빈 화면이다. set → capture → **reset** 을
  반드시 try/finally 로 묶어라(예외가 나도 사용자 관심종목을 오염된 채로 두지 않는다).

[비밀] 토큰은 kairos_api.txt(= *.txt 이므로 .gitignore 차단)에서 읽는다.
  **절대 로그·예외 메시지·커밋에 남기지 마라.**

[사용법]
  python kairos_client.py --health
  python kairos_client.py --screens
  python kairos_client.py --capture short_lend --tickers 005930,000660 --out-dir tmp
"""
import os
import sys
import json
import time
import base64
import hashlib
import argparse
import logging
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "kairos_api.txt")
CONFIG_FILE = os.path.join(HERE, "kairos_config.txt")

# 테일넷 IP 는 바뀔 수 있다 → 이름 우선, IP 폴백(가이드 1절 경고 반영)
DEFAULT_BASES = ("http://desktop-psk2gpr:8788", "http://100.84.184.80:8788")
# 캡처 1장 5~15초. ★0254(투자자 일별, 변형 15종)는 실측 **298초**(2026-08-19 배치 로그:
# 10:01:36 -> 10:06:34). 옛 값 180초는 "15변형이면 1~2분"이라는 잘못된 추정이라 그 화면만
# 매번 TimeoutError 로 죽었다(노드는 정상 완료 중인데 pc21 이 먼저 끊는다). 여유를 둔다.
DEFAULT_TIMEOUT = 420

logging.basicConfig(level=logging.INFO, format="[kairos] %(message)s")
log = logging.getLogger("kairos")


class KairosError(RuntimeError):
    """에이전트 호출/검증 실패. ★메시지에 토큰을 절대 넣지 마라."""


# =====================================================================
# 설정·토큰
# =====================================================================
def load_token(path=TOKEN_FILE) -> str:
    """kairos_api.txt 에서 토큰. 'token=...' 또는 한 줄. 없으면 ''."""
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip().lower() in ("token", "capture_token", "x_capture_token"):
                        return v.strip().strip('"').strip("'")
                    continue
                return line.strip('"').strip("'")
    except Exception:
        pass
    return ""


def load_bases(path=CONFIG_FILE):
    """kairos_config.txt 의 base_url(쉼표 다중 가능). 없으면 기본값."""
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for raw in f:
                    line = raw.strip()
                    if line.lower().startswith("base_url"):
                        v = line.split("=", 1)[1].strip()
                        got = tuple(x.strip().rstrip("/") for x in v.split(",") if x.strip())
                        if got:
                            return got
        except Exception:
            pass
    return DEFAULT_BASES


# =====================================================================
# 저수준 호출
# =====================================================================
def _request(base, path, token, body=None, timeout=DEFAULT_TIMEOUT):
    url = base.rstrip("/") + path
    headers = {}
    if token:
        headers["X-Capture-Token"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def call(path, token=None, body=None, timeout=DEFAULT_TIMEOUT, bases=None):
    """base 후보를 순서대로 시도(이름 → IP). 마지막 실패를 올린다. ★토큰 미노출."""
    token = load_token() if token is None else token
    bases = bases or load_bases()
    last = None
    for b in bases:
        try:
            return _request(b, path, token, body=body, timeout=timeout)
        except urllib.error.HTTPError as e:
            # 401/403 은 다른 base 로 재시도해도 같다 → 즉시 중단
            try:
                payload = json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                payload = {"ok": False, "error": "http_%d" % e.code}
            if e.code in (401, 403):
                return payload
            last = KairosError("HTTP %d (%s)" % (e.code, path))
        except Exception as e:
            last = KairosError("%s (%s @ %s)" % (type(e).__name__, path, b))
    raise last or KairosError("호출 실패: %s" % path)


# =====================================================================
# 고수준 API
# =====================================================================
def health(timeout=20):
    return call("/health", token="", timeout=timeout)


def screens(timeout=30):
    return call("/screens", timeout=timeout)


def get_watchlist(timeout=60):
    return call("/watchlist", timeout=timeout)


def set_watchlist(tickers, timeout=180):
    """관심종목 교체. 숫자 6자리만 허용(에이전트가 400 반환하므로 여기서 먼저 거른다)."""
    # ★4자리 미만은 거부한다 — zfill 로 무조건 채우면 오타 "12" 가 000012 라는 **다른 종목**으로
    #   조용히 둔갑한다(하네스가 잡은 실제 결함). 4~6자리 숫자만 관용적으로 zfill 한다.
    clean = []
    for t in (tickers or []):
        s = str(t).strip()
        if s.isdigit() and 4 <= len(s) <= 6:
            clean.append(s.zfill(6))
        else:
            log.warning("종목코드 형식 오류로 제외: %r (숫자 4~6자리만 허용)", t)
    if not clean:
        raise KairosError("유효한 종목코드가 없다(숫자 6자리만 허용)")
    return call("/watchlist", body={"tickers": clean}, timeout=timeout)


def reset_watchlist(timeout=180):
    """넣은 개수만큼만 삭제해 원상복구. ★분석 후 반드시 호출."""
    return call("/watchlist/reset", body={}, timeout=timeout)


def verify_capture(screen_no, meta, cap):
    """캡처 1장 검증 → (ok, reason, png_bytes). 실패 시 png 는 b''."""
    marker = str((meta or {}).get("marker_text") or "")
    if not marker:
        return False, "marker_text 없음(창 제목을 못 읽음)", b""
    if screen_no and ("[%s]" % screen_no) not in marker:
        return False, "엉뚱한 화면 — 기대 [%s], 실제 %r" % (screen_no, marker[:60]), b""
    try:
        png = base64.b64decode(cap.get("png_b64") or "")
    except Exception:
        return False, "base64 디코드 실패", b""
    if not png.startswith(b"\x89PNG"):
        return False, "PNG 시그니처 아님", b""
    want = str(cap.get("sha256") or "").lower()
    if want and hashlib.sha256(png).hexdigest() != want:
        return False, "sha256 불일치(전송 손상)", b""
    # ★v11.2 계좌 마스킹 실패 차단(2026-07-31 지시서): Tesseract 설치 후 정상값은 0(가린 개수)이며
    #   **-1 은 마스킹이 실패한 것**이다. 그 이미지에는 계좌번호가 그대로 남아 있을 수 있으므로
    #   쓰지 않는다. (설치 전에는 항상 -1 이라 '참고'로만 다뤘는데, 이제 의미가 바뀌었다.)
    if cap.get("masked") == -1:
        return False, "masked=-1(계좌 마스킹 실패 — 계좌번호 노출 위험)", b""
    if cap.get("settled") is False:
        return False, "settled=false(화면 갱신 중 캡처)", png
    return True, "", png


def capture(screen, variant=None, expect_no=None, timeout=DEFAULT_TIMEOUT, retry_unsettled=1):
    """화면 캡처 → (shots, meta). shots = [{label, variant, png, bytes, ok, reason}].

    variant 생략 시 에이전트가 전 변형을 찍어 여러 장을 돌려준다.
    settled=false 면 retry_unsettled 회까지 재요청(값이 흔들린 캡처를 쓰지 않기 위해).
    """
    q = "/capture?screen=%s" % screen + (("&variant=%s" % variant) if variant else "")
    for attempt in range(retry_unsettled + 1):
        d = call(q, timeout=timeout)
        if not d.get("ok"):
            _err = str(d.get("error") or "")
            # ★PicoBusy: 07:00·16:30 자동 배치가 피코를 쓰는 중이다(뮤텍스 공유).
            #   실패가 아니라 '잠깐 겹친 것'이므로 한 번 기다렸다 재시도한다.
            if "PicoBusy" in _err and attempt < retry_unsettled:
                log.info("PicoBusy — 자동 배치와 겹침. 20초 후 재시도 (%s)", screen)
                time.sleep(20)
                continue
            raise KairosError("캡처 실패(%s): %s" % (screen, _err))
        meta = d.get("meta") or {}
        no = expect_no or meta.get("screen_no")
        shots, unsettled = [], False
        for c in (d.get("captures") or []):
            ok, reason, png = verify_capture(no, meta, c)
            if (not ok) and "settled=false" in reason:
                unsettled = True
            # masked: 노트북의 계좌번호 자동 마스킹 건수. **-1 = 마스킹 미수행**
            #   (Tesseract 미설치 시 -1 로 온다 — 인계문서는 '자동 마스킹된다'고 하지만
            #    실측 -1 이었다). 계좌 정보가 그대로 찍혔을 수 있으니 그대로 실어 보고한다.
            shots.append({"label": c.get("label"), "variant": c.get("variant"),
                          "png": png, "bytes": len(png), "ok": ok, "reason": reason,
                          "masked": c.get("masked")})
        if unsettled and attempt < retry_unsettled:
            log.info("settled=false → 재요청 (%s, %d/%d)", screen, attempt + 1, retry_unsettled)
            time.sleep(3)
            continue
        return shots, meta
    return [], {}


def walk(keys=None, batch=None, dwell=3.0, variants=False, timeout=None,
         variant_names=None):
    """★클릭만 하는 순회 — 캡처·저장·전송을 하지 않는다(2026-08-20 신설).

    [왜] 캡처 검증은 창 제목의 화면번호만 본다. 라디오·그룹 토글이 틀려도 ok 로
    통과한다(실측: 0231 빈 표, 0261 업종, 0254 KRX 기준). 사람이 카이로스 화면을
    직접 보면서 확인하려면 그림을 남기지 않고 화면만 바꿔주는 경로가 필요하다.
    dwell 은 사람이 볼 시간(초)이다.
    """
    body = {"dwell": float(dwell), "variants": bool(variants)}
    if variant_names:
        # 0254 는 변형 15개(실측 약 300초) — 눈으로 볼 때는 보통 하나면 된다.
        body["variant_names"] = list(variant_names)
    if keys:
        body["screens"] = list(keys)
    if batch:
        body["batch"] = batch
    # 화면수 x (전환 + dwell) 이라 기본 타임아웃으로는 모자랄 수 있다.
    n = len(keys) if keys else 10
    tmo = timeout or max(DEFAULT_TIMEOUT, int(n * (dwell + 12)) + 60)
    return call("/walk", body=body, timeout=tmo)


# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="카이로스 캡처 에이전트 클라이언트")
    ap.add_argument("--health", action="store_true")
    ap.add_argument("--screens", action="store_true")
    ap.add_argument("--watchlist", action="store_true", help="현재 관심종목 조회")
    ap.add_argument("--capture", default=None, metavar="KEY")
    ap.add_argument("--variant", default=None)
    ap.add_argument("--tickers", default=None, help="쉼표구분 — 캡처 전 관심종목 교체(후 자동 복구)")
    ap.add_argument("--out-dir", default=None, help="PNG 저장 폴더")
    ap.add_argument("--reset", action="store_true", help="관심종목 원상복구만 실행")
    ap.add_argument("--walk", action="store_true",
                    help="★클릭만 하는 순회(캡처 안 함) — 화면을 눈으로 확인할 때")
    ap.add_argument("--walk-screens", default=None, metavar="KEYS",
                    help="--walk 대상 화면 key 쉼표구분(생략하면 전부)")
    ap.add_argument("--walk-batch", default=None, help="--walk 대상 배치(morning 등)")
    ap.add_argument("--dwell", type=float, default=3.0, help="--walk 화면마다 멈추는 초(기본 3)")
    ap.add_argument("--walk-variants", action="store_true",
                    help="--walk 에서 변형(코스피/코스닥 등)까지 순회 — 0254 는 15변형이라 오래 걸린다")
    ap.add_argument("--walk-variant", default=None, metavar="NAMES",
                    help="--walk 에서 이 변형만(쉼표구분). 예: kospi")
    args = ap.parse_args()

    if not load_token():
        log.warning("kairos_api.txt 에 토큰이 없다 — /health 외에는 401 이 난다.")

    if args.health:
        h = health()
        log.info("health: hts=%s login_screen=%s screen=%s (%s)",
                 h.get("hts"), h.get("hts_login_screen"),
                 h.get("current_screen"), h.get("now_kst"))
        return 0
    if args.screens:
        d = screens()
        for s in (d.get("screens") or []):
            log.info("%-18s %-5s %-32s ready=%s variants=%d",
                     s.get("key"), s.get("no"), (s.get("name") or "")[:32],
                     s.get("ready"), len(s.get("variants") or []))
        return 0
    if args.watchlist:
        log.info("watchlist: %s", json.dumps(get_watchlist(), ensure_ascii=False))
        return 0
    if args.reset:
        log.info("reset: %s", json.dumps(reset_watchlist(), ensure_ascii=False))
        return 0
    if args.walk:
        h = health()
        if not h.get("hts") or h.get("hts_login_screen"):
            log.warning("HTS 상태 불가(hts=%s login=%s) — 중단",
                        h.get("hts"), h.get("hts_login_screen"))
            return 1
        keys = [k.strip() for k in (args.walk_screens or "").split(",") if k.strip()]
        log.info("순회 시작 — 클릭만 하고 캡처하지 않는다 (dwell=%.1fs, variants=%s)",
                 args.dwell, args.walk_variants)
        vnames = [v.strip() for v in (args.walk_variant or "").split(",") if v.strip()]
        r = walk(keys=keys, batch=args.walk_batch, dwell=args.dwell,
                 variants=args.walk_variants, variant_names=vnames)
        if not r.get("ok") and r.get("error"):
            log.warning("순회 실패: %s %s", r.get("error"), r.get("detail") or "")
            return 1
        for x in (r.get("results") or []):
            mark = "OK  " if x.get("ok") else "FAIL"
            log.info("  [%s] %s %-24s %s", mark, x.get("no"),
                     (x.get("name") or "")[:24],
                     x.get("marker_text") if x.get("ok") else x.get("error"))
            for v in (x.get("views") or []):
                if v.get("label") or v.get("variant"):
                    log.info("          - %s: %s", v.get("label") or v.get("variant"),
                             v.get("title"))
        log.info("순회 %s/%s 성공 — ★캡처하지 않았습니다(PNG 0장)",
                 r.get("ok_count"), r.get("walked"))
        return 0 if r.get("ok") else 1

    if args.capture:
        h = health()
        if not h.get("hts") or h.get("hts_login_screen"):
            log.warning("HTS 상태 불가(hts=%s login=%s) — 중단",
                        h.get("hts"), h.get("hts_login_screen"))
            return 0
        did_set = False
        try:
            if args.tickers:
                r = set_watchlist([t for t in args.tickers.split(",") if t.strip()])
                did_set = True
                log.info("watchlist 설정: removed=%s added=%s", r.get("removed"), r.get("added"))
            shots, meta = capture(args.capture, variant=args.variant)
            log.info("marker=%r shots=%d", meta.get("marker_text"), len(shots))
            for i, s in enumerate(shots):
                log.info("  [%s] %s %s (%dB)", "OK" if s["ok"] else "폐기",
                         s.get("label") or s.get("variant") or "-", s["reason"], s["bytes"])
                if s["ok"] and args.out_dir:
                    os.makedirs(args.out_dir, exist_ok=True)
                    fn = "%s_%s.png" % (args.capture, s.get("variant") or i)
                    with open(os.path.join(args.out_dir, fn), "wb") as f:
                        f.write(s["png"])
        finally:
            if did_set:
                try:
                    r = reset_watchlist()
                    log.info("watchlist 복구: removed=%s", r.get("removed"))
                except Exception as e:
                    log.warning("watchlist 복구 실패(수동 확인 필요): %s", type(e).__name__)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KairosError as e:
        log.warning("%s", e)
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(0)
