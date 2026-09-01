#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""커밋 직전 검사 - 스테이징된 **내용 자체**에 비밀·개인정보가 있는지 본다.

[왜 이게 필요한가]
  CLAUDE.md 절대규칙 1 은 "비밀 파일을 커밋하지 마라"이고, 지금까지 그것을
  지킨 건 사람의 주의력뿐이었다. .gitignore 는 파일 '이름'만 막는다. 코드
  안에 이메일 한 줄 적는 것도, `git add -f` 한 번도 못 막는다.

  ★2026-09-01 실측: 이력 122개 커밋 안에 실제 이메일 3종이 남아 있었다
    (본인 학교 메일·본인 gmail·제3자 gmail). 작업 트리는 깨끗했는데
    이력은 아니었다. 한 번 공개되면 크롤러가 사본을 떠 되돌릴 수 없다.

  ★작업 트리가 아니라 **인덱스(`git show :파일`)** 를 읽는다.
    파일을 고친 뒤 add 를 안 했다면 커밋되는 건 예전 내용이다.
    작업 트리를 보면 '고쳤으니 괜찮다'고 착각한다.

[쓰는 법]
  손으로:    python tools/check_no_secrets.py
  자동으로:  python tools/install_hooks.py  (한 번만. 이후 커밋마다 돈다)

[종료 코드]  0 = 안전 / 1 = 문제 있음(커밋 중단)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def git(*a: str) -> bytes:
    return subprocess.run(["git"] + list(a), cwd=ROOT,
                          capture_output=True).stdout


# ── 절대 들어가면 안 되는 경로 ──────────────────────────────────────
#    .gitignore 와 일부러 겹친다. `git add -f` 로 우회한 것을 여기서 다시 잡는다.
#    ★열거가 아니라 패턴을 믿는다 - 2026-08-27 실측에서 CLAUDE.md 의 비밀파일
#      목록에 kis_api.txt·vkospi_api.txt 가 빠져 있었다. 목록은 낡는다.
FORBIDDEN_PATH = [
    (re.compile(r"_api\.txt$"), "API 키 파일"),
    (re.compile(r"gemini_keys\.txt$|_keys\.txt$"), "키 묶음 파일"),
    (re.compile(r"^mail_config|^appscript_config|^krx_account"), "자격증명 설정"),
    (re.compile(r"gmail_credentials\.json$|gmail_token\.json$"), "Gmail 자격증명"),
    (re.compile(r"\.key$|\.pem$|\.pfx$"), "자격증명 파일"),
    (re.compile(r"\.full$"), "설정 원본(.full)"),
    # 운영 산출물 - 회원 이메일·보유 종목·발송 이력이 들어 있다.
    (re.compile(r"^output/"), "세션 산출물(리포트·회원별 포트폴리오)"),
    (re.compile(r"^logs/"), "로그"),
    (re.compile(r"^_backup/|^_retired/"), "로컬 백업"),
    (re.compile(r"^sent_index\.json"), "발송 이력(수신자 주소)"),
    (re.compile(r"\.bak"), "백업 사본"),
    (re.compile(r"__pycache__|\.pyc$"), "파이썬 부산물"),
]

# ── 내용에서 찾을 것 ────────────────────────────────────────────────
#    (라벨, 찾는 규칙, 이건 봐준다)
#
#    ★테일넷 주소(100.64/10 · *.ts.net)는 일부러 검사하지 않는다.
#      100.64/10 은 CGNAT 대역이라 인터넷에서 도달할 수 없고, 사이트 주소는
#      애초에 특기입증자료에 적어 공개하는 값이다. 여기서 막으려면 자동매매
#      폴백 경로(kairos_client·trade_client 의 IP 폴백)를 건드려야 하는데,
#      실제로 얻는 안전이 없다(2026-09-01 판단).
_ALLOW_MAIL = re.compile(
    r"example\.|invalid\.local|noreply@|@stock\.research|@anthropic\.com|"
    r"your_id@|@b\.com|@d\.com|@x\.com|@y\.com|@테스트\.com|localhost"
)

PATTERNS = [
    ("실제 이메일 주소",
     re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
     _ALLOW_MAIL),
    ("API 키·토큰 형태",
     re.compile(r"\b(?:sk-|AKfycb|ghp_|gho_|AIza|xox[baprs]-)[A-Za-z0-9_\-]{12,}"),
     None),
    ("KIS/증권 앱키처럼 보이는 값",
     re.compile(r"\bPS[A-Za-z0-9]{30,}\b"), None),
    ("계좌번호 형태",
     re.compile(r"\b\d{8}-?\d{2}\b(?=\s*(?:계좌|account))", re.I), None),
]


def main() -> int:
    # ★콘솔이 cp949 면 못 찍는 글자 하나에 검사기가 통째로 죽는다.
    #   2026-09-01 실측: em dash 때문에 훅이 크래시했고, 그 바람에 '검사 실패'가
    #   아니라 '검사기 고장'으로 커밋이 막혔다 - 이유를 알 수 없는 차단이 된다.
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass

    names = [n for n in git("diff", "--cached", "--name-only", "-z")
             .decode("utf-8").split("\0") if n]
    if not names:
        print("스테이징된 파일이 없다 - 검사할 것이 없다")
        return 0

    problems = []
    for n in names:
        for rx, why in FORBIDDEN_PATH:
            if rx.search(n):
                problems.append(("경로", n, why, ""))

        raw = git("show", ":" + n)
        try:
            txt = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue          # 이진 파일은 규칙 대상이 아니다
        for label, rx, allow in PATTERNS:
            for m in rx.finditer(txt):
                hit = m.group(0)
                if allow and allow.search(hit):
                    continue
                line = txt.count("\n", 0, m.start()) + 1
                problems.append(("내용", "%s:%d" % (n, line), label, hit))

    print("스테이징 파일 %d개 검사" % len(names))
    if not problems:
        print("비밀·개인정보 없음")
        return 0

    print()
    print("★문제 %d건 - 커밋을 멈춘다" % len(problems))
    for kind, where, why, hit in problems:
        print("  [%s] %-36s %s%s"
              % (kind, where, why, ("  ->  " + hit) if hit else ""))
    print()
    print("고치는 법")
    print("  · 경로 문제 : git restore --staged <파일>  (그리고 .gitignore 를 보강하라)")
    print("  · 내용 문제 : 값을 설정 파일이나 환경변수로 빼라. 지운 척하지 마라 -")
    print("                이미 커밋했다면 작업 트리만 고쳐도 이력에는 남아 있다.")
    print("                이력에서 지우려면 git filter-repo --replace-text 가 필요하다")
    print("                (--replace-message 도 따로 걸어야 커밋 메시지가 지워진다).")
    print("  · 오탐이면 tools/check_no_secrets.py 의 '봐준다' 규칙을 고쳐라.")
    print("    --no-verify 로 넘기지 마라. 그 우회가 이 검사의 존재 이유를 없앤다.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
