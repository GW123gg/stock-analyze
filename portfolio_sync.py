#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
portfolio_sync.py — Apps Script 웹앱 → portfolios/<이메일>.csv 동기화 [v11.13 신규]

[흐름]
  사람(본인) → 웹 폼에서 이메일 확인 후 보유 등록 → Google Sheet
                                                       ↓ (이 스크립트, SECRET 필요)
                                        portfolios/<이메일>.csv  → 아침 리서치가 읽음

[★안전]
  · 웹앱은 **본인 이메일로 확인 코드를 받아야** 등록된다(자기 등록 = 동의).
  · 이 스크립트는 **받은 데이터를 사람별 파일로 나눠서만** 쓴다 — 남의 것이 섞이지 않는다.
  · 이메일 형식이 아닌 값, 종목코드가 이상한 행은 **버리고 사유를 보고**한다(조용히 넘어가지 않음).
  · 기존 파일은 덮어쓰기 전에 `.bak` 로 백업한다.
  · `--dry` 가 기본이 아니다 — 실제로 쓰지만, 무엇을 쓸지 항상 출력한다.

[설정] portfolio_sync_config.txt (= *.txt 이라 .gitignore 차단)
    url=https://script.google.com/macros/s/..../exec
    secret=웹앱의 SECRET 과 같은 값

[사용] python portfolio_sync.py          # 가져와서 CSV 갱신
       python portfolio_sync.py --dry    # 무엇이 바뀔지만 보여준다
"""
from __future__ import annotations

import os
import sys
import csv
import json
import shutil
import argparse
import logging
import urllib.parse
import urllib.request
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "portfolio_sync_config.txt")
PORTFOLIO_DIR = os.path.join(HERE, "portfolios")
COLS = ["국가", "증권사", "종목코드", "종목명", "평단가", "수량", "매수환율", "메모", "매수일시"]

logging.basicConfig(level=logging.INFO, format="[pfsync] %(message)s")
log = logging.getLogger("pfsync")


GS_FILE = os.path.join(HERE, "cowork", "portfolio_webapp.gs")


def secret_from_gs(path=None):
    """웹앱 소스(.gs)의 `var SECRET = '...'` 를 읽는다. 못 읽으면 None.

    ★.gs 가 정본이다 — 거기 적힌 값이 곧 배포된 값이라 설정 파일과 어긋날 수가 없다.
      (실측: 설정 파일의 secret 줄이 편집기 덮어쓰기로 두 번 사라졌다. 그때마다
       손으로 복구하는 대신 정본에서 가져온다.)
    """
    p = path or GS_FILE
    if not os.path.isfile(p):
        return None
    try:
        import re
        with open(p, encoding="utf-8", errors="replace") as f:
            m = re.search(r"var\s+SECRET\s*=\s*'([^']*)'", f.read())
        if not m:
            return None
        v = m.group(1)
        return v if (v and "CHANGE_ME" not in v and len(v) >= 16) else None
    except Exception:
        return None


def load_config():
    """url·secret. ★값을 출력하지 않는다.

    secret 이 설정에 없으면 .gs 에서 채운다(둘은 어차피 같아야 한다).
    """
    cfg = {}
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8-sig", errors="replace") as f:
                for raw in f:
                    s = raw.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, _, v = s.partition("=")
                    cfg[k.strip().lower()] = v.strip()
        except Exception as e:
            log.warning("설정 읽기 실패: %s", e)

    if not cfg.get("secret"):
        gs = secret_from_gs()
        if gs:
            cfg["secret"] = gs
            log.info("secret 을 cowork/portfolio_webapp.gs 에서 가져왔다"
                     "(설정 파일에 없음 — 정상 동작).")
    return cfg


def fetch_rows(url, secret, timeout=60):
    """웹앱에서 전체 행을 가져온다. 실패하면 예외."""
    q = urllib.parse.urlencode({"action": "all", "secret": secret})
    req = urllib.request.Request(url + ("&" if "?" in url else "?") + q,
                                 headers={"User-Agent": "stock_research/portfolio_sync"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    try:
        d = json.loads(body)
    except Exception:
        raise RuntimeError("응답이 JSON 이 아니다(배포 URL·권한 확인). 앞부분: %s" % body[:160])
    if not d.get("ok"):
        err = d.get("error") or "unknown"
        if err == "unauthorized":
            raise RuntimeError("SECRET 불일치 — 웹앱의 SECRET 과 config 의 secret 을 맞춰라")
        raise RuntimeError("웹앱 오류: %s" % err)
    return d.get("rows") or []


def group_by_email(rows):
    """행 → {이메일: [행...]}. 형식이 틀린 행은 버리고 사유를 모은다."""
    try:
        import portfolio_review as pr
    except Exception as e:
        raise RuntimeError("portfolio_review import 실패: %s" % e)

    by, bad = {}, []
    for i, r in enumerate(rows, 1):
        em = str(r.get("email") or "").strip().lower()
        if "@" not in em or "." not in em.split("@")[-1]:
            bad.append("%d행: 이메일 형식 아님(%s)" % (i, em[:40]))
            continue
        # ★국가 먼저 — 코드 체계가 나라마다 다르다(KR 6자리 / US 영문 / JP 4자리).
        country = pr.normalize_country(r.get("country"))
        if country is None:
            bad.append("%d행(%s): 국가를 알 수 없음(%s)" % (i, em, str(r.get("country"))[:20]))
            continue
        meta = pr.country_meta(country)
        tk, _fixed = pr.normalize_ticker(r.get("ticker"), country)
        if not tk:
            bad.append("%d행(%s): %s 종목코드 이상(%s)"
                       % (i, em, meta["name"], str(r.get("ticker"))[:20]))
            continue
        try:
            price = float(str(r.get("avg_price")).replace(",", ""))
            qty = int(float(str(r.get("qty")).replace(",", "")))
        except (TypeError, ValueError):
            bad.append("%d행(%s): 평단가/수량이 숫자가 아님" % (i, em))
            continue
        if price <= 0 or qty <= 0:
            bad.append("%d행(%s): 평단가·수량이 0 이하" % (i, em))
            continue
        # 해외는 매수 시점 환율(원/1단위)이 있어야 원화 손익을 낼 수 있다.
        buy_fx = ""
        if meta["fx"]:
            try:
                buy_fx = float(str(r.get("buy_fx")).replace(",", ""))
            except (TypeError, ValueError):
                bad.append("%d행(%s): %s 종목인데 매수환율이 없거나 숫자가 아님"
                           % (i, em, meta["name"]))
                continue
            ok, why = pr.fx_sane(meta["cur"], buy_fx)
            if not ok:
                bad.append("%d행(%s): 매수환율 — %s" % (i, em, why))
                continue
        by.setdefault(em, []).append({
            "국가": meta["name"],
            "증권사": str(r.get("broker") or "카이로스").strip(),
            "종목코드": pr.excel_safe_ticker(tk),      # ★엑셀이 선행 0 을 안 지우게
            "종목명": str(r.get("name") or "").strip(),
            "평단가": int(price) if price == int(price) else price,
            "수량": qty,
            "매수환율": buy_fx,
            "메모": str(r.get("memo") or "").strip(),
            "매수일시": "",                            # 웹에서 받지 않는다(불타기/물타기라 무의미)
        })
    return by, bad


def write_csv(email, rows, dry=False):
    """한 사람의 CSV 를 쓴다. 반환 (경로, 변경여부)."""
    os.makedirs(PORTFOLIO_DIR, exist_ok=True)
    path = os.path.join(PORTFOLIO_DIR, email + ".csv")
    buf = []
    buf.append(",".join(COLS))
    for r in rows:
        vals = []
        for c in COLS:
            v = str(r.get(c, ""))
            vals.append('"%s"' % v.replace('"', '""') if ("," in v or '"' in v) else v)
        buf.append(",".join(vals))
    new = "\n".join(buf) + "\n"

    old = ""
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                old = f.read()
        except Exception:
            old = ""
    if old.strip() == new.strip():
        return path, False
    if dry:
        return path, True
    if old:
        try:
            shutil.copy2(path, path + ".bak")
        except Exception:
            pass
    # ★utf-8-sig = BOM. 엑셀이 한글을 깨지 않게.
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(new)
    return path, True


def main():
    ap = argparse.ArgumentParser(description="웹앱 → portfolios/*.csv 동기화")
    ap.add_argument("--dry", action="store_true", help="무엇이 바뀔지만 보여준다")
    args = ap.parse_args()

    cfg = load_config()
    url, secret = cfg.get("url", ""), cfg.get("secret", "")
    if not url or not secret:
        log.error("portfolio_sync_config.txt 에 url·secret 이 없다.")
        log.error("  형식:")
        log.error("    url=https://script.google.com/macros/s/..../exec")
        log.error("    secret=<웹앱 SECRET 과 같은 값>")
        return 1

    try:
        rows = fetch_rows(url, secret)
    except Exception as e:
        log.error("가져오기 실패: %s", e)
        return 1
    log.info("웹앱에서 %d행 수신", len(rows))

    by, bad = group_by_email(rows)
    for b in bad:
        log.warning("버림 — %s", b)
    if not by:
        log.info("등록된 포트폴리오가 없다(웹 폼에서 아직 아무도 등록 안 함).")
        return 0

    n_changed = 0
    for em in sorted(by):
        path, changed = write_csv(em, by[em], dry=args.dry)
        log.info("%-34s %2d종  %s", em, len(by[em]),
                 ("변경 예정" if args.dry else "갱신됨") if changed else "변화 없음")
        n_changed += 1 if changed else 0

    # 웹앱에서 사라진 사람은 지우지 않는다 — 실수로 파일이 날아가는 것보다 남는 게 낫다.
    try:
        import portfolio_review as pr
        local = {e for e, _ in pr.list_people(PORTFOLIO_DIR)}
        gone = sorted(local - set(by))
        if gone:
            log.info("웹앱에 없지만 로컬에 남은 사람(삭제하지 않음): %s", ", ".join(gone))
    except Exception:
        pass

    log.info("완료 — %d명 중 %d명 %s", len(by), n_changed,
             "변경 예정" if args.dry else "갱신")
    if args.dry:
        log.info("실제로 쓰려면 --dry 없이 다시 실행하라.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
