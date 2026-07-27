# -*- coding: utf-8 -*-
"""
gen_token.py — 카이로스 캡처 에이전트용 공유 토큰 생성·대조 도구 [v10.8 신규]

[왜] pc21 과 노트북(desktop-psk2gpr)이 **같은 토큰**을 들고 있어야 캡처 API 가 열린다.
  손으로 만든 문자열은 짧거나 예측 가능해지기 쉬우므로 `secrets`(CSPRNG)로 만든다.

[★설계 — 토큰을 화면에 안 뿌린다]
  기본 동작은 **파일에 쓰고 지문(fingerprint)만 출력**한다. 지문은 sha256 앞 12자라
  토큰을 역산할 수 없다. 노트북에서도 같은 지문이 나오면 **양쪽이 일치**하는 것이므로,
  토큰을 터미널·로그·채팅에 노출하지 않고도 대조할 수 있다.
  노트북으로 옮길 때만 `--show` 를 쓰되, **그 출력은 채팅·이슈에 붙여넣지 마라.**

[생성 강도] secrets.token_urlsafe(32) = 256비트. URL-safe 라 헤더에 그대로 실린다.

[사용법]
  python gen_token.py --set              # ★기존(노트북) 토큰을 붙여넣어 저장 — 화면·기록에 안 남음
  python gen_token.py                    # 새 토큰 생성 → kairos_api.txt (지문만 출력)
  python gen_token.py --show             # 생성 + 토큰 출력(노트북에 옮길 때만)
  python gen_token.py --fingerprint      # 현재 파일의 지문만 (양쪽 대조용)
  python gen_token.py --verify           # 현재 토큰으로 에이전트 호출 테스트
  python gen_token.py --out other.txt --bytes 48

[★어느 쪽을 써야 하나]
  · **최초 연동**: 노트북에 이미 토큰이 있다 → `--set` 으로 그걸 pc21 에 복사(노트북 무수정).
  · **토큰 교체(유출·주기 교체)**: 인자 없이 생성 → `--show` 로 확인해 **노트북에도** 넣는다.
[주의] 토큰을 바꾸면 **노트북 쪽도 같이 바꿔야** 한다. 한쪽만 바꾸면 401 이 난다.
"""
import os
import sys
import shutil
import hashlib
import secrets
import argparse
import logging
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "kairos_api.txt")

logging.basicConfig(level=logging.INFO, format="[token] %(message)s")
log = logging.getLogger("token")


# =====================================================================
# 순수함수 — 하네스로 검증 가능(부작용 없음)
# =====================================================================
def make_token(nbytes: int = 32) -> str:
    """CSPRNG 토큰. nbytes=32 → 256비트(URL-safe 43자)."""
    n = int(nbytes)
    if n < 16:
        raise ValueError("토큰은 최소 16바이트(128비트) 이상이어야 한다")
    return secrets.token_urlsafe(n)


def fingerprint(token: str) -> str:
    """토큰 지문 — sha256 앞 12자. ★역산 불가라 안전하게 대조에 쓸 수 있다.
    빈 토큰이면 '' (없는 것을 있는 것처럼 보이지 않게)."""
    t = (token or "").strip()
    if not t:
        return ""
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:12]


def render_file(token: str) -> str:
    """kairos_api.txt 본문. 주석에 토큰을 넣지 않는다(지문만)."""
    return (
        "# 카이로스 캡처 에이전트 공유 토큰 (비밀 — 커밋·채팅·로그 금지)\n"
        "# 생성: %s\n"
        "# 지문(sha256 앞12): %s   <- 노트북 쪽과 이 값이 같아야 한다\n"
        "# 노트북에도 같은 토큰을 넣어라. 한쪽만 바꾸면 401 이 난다.\n"
        "token=%s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        fingerprint(token), token)
    )


def save_token(token: str, out_path) -> str:
    """토큰을 파일에 원자적으로 쓴다 → 지문 반환. 기존 파일은 호출자가 백업할 것."""
    t = (token or "").strip()
    if len(t) < 16:
        raise ValueError("토큰이 너무 짧다(16자 미만) — 붙여넣기가 잘렸는지 확인하라")
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(render_file(t))
    os.replace(tmp, out_path)
    return fingerprint(t)


def read_token(path) -> str:
    """kairos_client.load_token 과 같은 규약(중복 구현 아님 — 이 파일 단독 실행 대비 최소본)."""
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


# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="카이로스 공유 토큰 생성·대조")
    ap.add_argument("--out", default=DEFAULT_OUT, help="저장 경로(기본 kairos_api.txt)")
    ap.add_argument("--bytes", type=int, default=32, help="엔트로피 바이트(기본 32=256비트)")
    ap.add_argument("--show", action="store_true", help="토큰을 화면에 출력(노트북 이전용)")
    ap.add_argument("--fingerprint", action="store_true", help="현재 파일 지문만 출력")
    ap.add_argument("--verify", action="store_true", help="현재 토큰으로 에이전트 호출 테스트")
    ap.add_argument("--set", action="store_true",
                    help="기존(노트북) 토큰을 붙여넣어 저장 — 입력이 화면·명령기록에 남지 않는다")
    args = ap.parse_args()

    if args.set:
        # ★getpass: 입력이 에코되지 않아 어깨너머·터미널 스크롤백에 남지 않는다.
        #   명령줄 인자로 받지 않는 이유도 같다(PowerShell 히스토리·ps 목록에 남는다).
        import getpass
        try:
            tok = getpass.getpass("노트북 토큰을 붙여넣고 Enter (화면에 안 보임): ").strip()
        except Exception:
            log.warning("대화형 입력 불가 — 파일을 직접 편집하라: %s", args.out)
            return 1
        if not tok:
            log.warning("입력이 비었다 — 취소")
            return 1
        old = read_token(args.out)
        if old:
            if fingerprint(old) == fingerprint(tok):
                log.info("이미 같은 토큰이다(지문 %s) — 변경 없음", fingerprint(old))
                return 0
            bak = args.out.replace(".txt", "") + ".bak.txt"
            try:
                shutil.copy2(args.out, bak)
                log.info("기존 토큰 백업: %s", os.path.basename(bak))
            except Exception as e:
                log.warning("백업 실패(%s) — 중단", type(e).__name__)
                return 1
        try:
            fp = save_token(tok, args.out)
        except ValueError as e:
            log.warning("%s", e)
            return 1
        except Exception as e:
            log.warning("저장 실패: %s", type(e).__name__)
            return 1
        log.info("저장 완료 → %s", os.path.basename(args.out))
        log.info("지문: %s   ← 노트북의 지문과 같은지 확인하라", fp)
        log.info("확인: python gen_token.py --verify")
        return 0

    if args.fingerprint:
        fp = fingerprint(read_token(args.out))
        if not fp:
            log.warning("%s 에 토큰이 없다", os.path.basename(args.out))
            return 1
        log.info("지문: %s  (노트북에서도 같은 값이면 일치)", fp)
        return 0

    if args.verify:
        tok = read_token(args.out)
        if not tok:
            log.warning("토큰이 없다 — 먼저 생성하라")
            return 1
        try:
            import kairos_client as kc
            h = kc.health()
            log.info("health: hts=%s login_screen=%s", h.get("hts"), h.get("hts_login_screen"))
            d = kc.screens(timeout=30)
            if d.get("error") == "unauthorized":
                log.warning("★401 — 노트북 토큰과 다르다. 양쪽 지문(--fingerprint)을 대조하라")
                return 1
            n = len(d.get("screens") or [])
            log.info("인증 성공 — 화면 %d종 제공. 지문 %s", n, fingerprint(tok))
            return 0
        except Exception as e:
            log.warning("에이전트 호출 실패(%s) — 노트북·테일넷 상태 확인", type(e).__name__)
            return 1

    # ── 생성 ──
    old = read_token(args.out)
    if old:
        bak = args.out.replace(".txt", "") + ".bak.txt"
        try:
            shutil.copy2(args.out, bak)
            log.info("기존 토큰 백업: %s (지문 %s)", os.path.basename(bak), fingerprint(old))
        except Exception as e:
            log.warning("백업 실패(%s) — 중단한다", type(e).__name__)
            return 1

    tok = make_token(args.bytes)
    tmp = args.out + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(render_file(tok))
        os.replace(tmp, args.out)                      # 원자적 교체
    except Exception as e:
        log.warning("저장 실패: %s", type(e).__name__)
        return 1

    log.info("생성 완료 → %s  (%d비트)", os.path.basename(args.out), args.bytes * 8)
    log.info("지문: %s", fingerprint(tok))
    if args.show:
        # ★사용자가 노트북으로 옮길 때만. 이 줄을 채팅·이슈에 붙여넣지 마라.
        print("\n" + tok + "\n")
        log.info("위 토큰을 노트북 에이전트 설정에 넣어라. 붙여넣기 후 화면을 지워라.")
    else:
        log.info("노트북으로 옮기려면: python gen_token.py --show  (또는 파일을 직접 열어라)")
    if old:
        log.warning("★토큰이 바뀌었다 — **노트북 쪽도 같이 바꿔야** 한다(안 하면 401).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
