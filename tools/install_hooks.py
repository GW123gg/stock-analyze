#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""커밋 훅 설치 - 한 번만 실행하면 된다.

    python tools/install_hooks.py

[왜 스크립트로 설치하나]
  .git/hooks 는 git 이 추적하지 않는다. 저장소를 새로 clone 하면 훅이 없다.
  그래서 훅 본체는 tools/ 에 두고(=추적됨), 이 스크립트가 .git/hooks 에
  얇은 호출자만 만든다. 나중에 검사 규칙을 고치면 훅을 다시 깔 필요가 없다.

[윈도우 주의]
  git 은 훅을 sh 로 실행한다(Git for Windows 는 bash 를 함께 깐다).
  그래서 훅은 배치가 아니라 sh 스크립트여야 한다. 줄바꿈도 LF 로 쓴다 -
  CRLF 면 `#!/bin/sh\\r` 가 되어 "no such file or directory" 로 죽는다.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOOK = (
    "#!/bin/sh\n"
    "# 자동 생성 - tools/install_hooks.py 가 만든다. 직접 고치지 마라.\n"
    "# 검사 규칙은 tools/check_no_secrets.py 에 있다.\n"
    "python tools/check_no_secrets.py || exit 1\n"
)


def main() -> int:
    # 콘솔이 cp949 여도 죽지 않게. 인코딩을 바꾸면 오히려 글자가 깨진다.
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    out = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=ROOT,
                         capture_output=True, text=True)
    if out.returncode != 0:
        print("git 저장소가 아니다:", ROOT)
        return 1
    gitdir = out.stdout.strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.join(ROOT, gitdir)

    hooks = os.path.join(gitdir, "hooks")
    os.makedirs(hooks, exist_ok=True)
    path = os.path.join(hooks, "pre-commit")

    # ★newline="" 로 열어야 파이썬이 \n 을 \r\n 으로 바꾸지 않는다.
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(HOOK)
    try:
        os.chmod(path, 0o755)          # 윈도우에서는 무의미하지만 해가 없다
    except OSError:
        pass

    print("설치 완료:", path)
    print("이제 커밋할 때마다 tools/check_no_secrets.py 가 먼저 돈다.")
    print("확인:  git commit  (문제가 있으면 커밋이 멈춘다)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
