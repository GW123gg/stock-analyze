#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
portfolio_allowlist.py — 포트폴리오 웹앱을 **초대제**로 만든다 [v11.13]

리서치 메일 수신자(mail_config.txt 의 `to`)만 웹 폼에서 등록할 수 있게,
허용목록을 웹앱에 올린다.

[★주소를 구글로 보내지 않는다]
  mail_config.txt 는 내용을 밖으로 내보내면 안 되는 파일이다(프로젝트 규칙 1).
  그래서 주소 원문 대신 **HMAC-SHA256(SECRET, 주소) 해시**만 올린다.
    · 웹앱은 입력받은 주소를 같은 방식으로 해시해 대조한다 — 동작은 똑같다.
    · 구글 시트가 새어도 **누가 허용됐는지 알 수 없다**(원문이 없다).
    · 이 스크립트는 주소를 화면에 찍지 않는다. 개수와 마스킹된 형태만 보여준다.

[왜 확인 코드를 없애지 않았나]
  허용목록은 '누가 등록할 수 있나'만 정한다. 수신자들은 서로의 주소를 알기 때문에,
  코드 확인을 빼면 **남의 주소를 입력해 그 사람 포트폴리오를 보거나 고칠 수 있다.**
  그래서 두 단계를 다 둔다: 허용목록(자격) + 확인 코드(본인 확인).

[사용]
  python portfolio_allowlist.py --check     # 몇 명이 대상인지만 본다(전송 안 함)
  python portfolio_allowlist.py --push      # 웹앱에 올린다
  python portfolio_allowlist.py --push --extra a@b.com,c@d.com   # 친구 추가
"""
from __future__ import annotations

import os
import sys
import re
import json
import hmac
import hashlib
import argparse
import logging
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(level=logging.INFO, format="[allowlist] %(message)s")
log = logging.getLogger("allowlist")


def mask(email):
    """로그용 — 원문을 찍지 않는다. `ab***@gm***.com` 형태."""
    try:
        local, _, dom = email.partition("@")
        d, _, tld = dom.rpartition(".")
        return "%s***@%s***.%s" % (local[:2], d[:2], tld)
    except Exception:
        return "***"


def normalize(email):
    e = str(email or "").strip().lower()
    return e if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e) else None


def recipients_from_mail_config():
    """mail_config.txt 의 `to` 에서 수신자 주소를 읽는다. ★반환만 하고 찍지 않는다."""
    try:
        import research_agent as ra
        cfg = ra.load_mail_config()
        raw = ra._mail_recipients(cfg)
    except Exception as e:
        log.error("메일 설정을 읽지 못했다: %s", e)
        return []
    out = []
    for r in raw:
        n = normalize(r)
        if n and n not in out:
            out.append(n)
    return out


def hash_all(emails, secret):
    return [hmac.new(secret.encode("utf-8"), e.encode("utf-8"),
                     hashlib.sha256).hexdigest() for e in emails]


def push(url, secret, hashes, timeout=60):
    body = json.dumps({"action": "set_allowlist", "secret": secret,
                       "hashes": hashes}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "stock_research/allowlist"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except Exception:
        raise RuntimeError("응답이 JSON 이 아니다(배포 URL 확인). 앞부분: %s" % raw[:160])


def main():
    ap = argparse.ArgumentParser(description="포트폴리오 웹앱 초대제 허용목록")
    ap.add_argument("--check", action="store_true", help="대상만 확인(전송 안 함)")
    ap.add_argument("--push", action="store_true", help="웹앱에 올린다")
    ap.add_argument("--extra", default="", help="추가 허용 주소(쉼표 구분)")
    args = ap.parse_args()

    if not args.check and not args.push:
        args.check = True       # 안전 기본값: 아무 것도 안 보낸다

    emails = recipients_from_mail_config()
    extra = [e for e in (normalize(x) for x in args.extra.split(",")) if e]
    for e in extra:
        if e not in emails:
            emails.append(e)

    if not emails:
        log.error("허용할 주소가 하나도 없다.")
        log.error("  mail_config.txt 의 `to` 를 확인하거나 --extra 로 직접 넣어라.")
        return 1

    log.info("허용 대상 %d명 (수신자 %d + 추가 %d)",
             len(emails), len(emails) - len(extra), len(extra))
    for e in emails:
        log.info("   %s", mask(e))       # ★원문은 찍지 않는다

    if args.check:
        log.info("확인만 했다. 올리려면 --push 를 붙여라.")
        return 0

    try:
        import portfolio_sync as ps
        cfg = ps.load_config()
    except Exception as e:
        log.error("설정을 못 읽었다: %s", e)
        return 1
    url, secret = cfg.get("url", ""), cfg.get("secret", "")
    if not url or not secret:
        log.error("portfolio_sync_config.txt 에 url·secret 이 없다.")
        return 1

    hashes = hash_all(emails, secret)
    try:
        r = push(url, secret, hashes)
    except Exception as e:
        log.error("전송 실패: %s", e)
        return 1
    if not r.get("ok"):
        err = r.get("error") or "unknown"
        if err == "unauthorized":
            log.error("SECRET 불일치 — 웹앱의 SECRET 과 config 의 secret 을 맞춰라")
        else:
            log.error("웹앱 오류: %s", err)
        return 1

    log.info("올림 완료 — 허용 %d건 (주소 원문은 전송하지 않았다. 해시만)", r.get("count"))
    log.info("이제 이 주소들만 웹 폼에서 등록할 수 있다.")
    log.info("★SECRET 을 바꾸면 해시가 전부 달라진다 — 그때는 이 명령을 다시 돌려라.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
