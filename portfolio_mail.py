#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
portfolio_mail.py — 포트폴리오 전략 메일 (★단일 수신자 전용) [v11.12 신규]

[무엇을 하나]
  `portfolio_review.json`(사실) + `portfolio_strategy.md`(분석가가 쓴 판단)를 합쳐
  **개인 포트폴리오 전략 메일**을 만들어 **지정된 한 사람에게만** 보낸다.

[★기존 리서치 메일과 완전히 분리돼 있다]
  · 기존: `research_agent.py mail` → mail_config.txt 의 `to`(수신자 여러 명) — **건드리지 않는다**
  · 이것: 아래 `RECIPIENT` 한 명에게만. 개인 보유 정보라 다른 수신자에게 가면 안 된다.
  두 경로는 서로 무관하며, 이 스크립트는 mail_config.txt 의 `to` 를 **읽지 않는다**(실수 방지).

[안전]
  · 기본은 **미리보기**다. 실제 발송은 `--send` 를 명시해야 한다.
  · 수신자는 코드에 고정돼 있고 CLI 로 바꿀 수 없다 — 오발송 방지.
  · 이모지·4바이트 문자는 제거(cp949 + Apps Script 안전).

[사용법]
  python portfolio_mail.py                    # HTML 미리보기 파일만 생성
  python portfolio_mail.py --send             # 실제 발송
  python portfolio_mail.py --session output\2026-08-06_063000 --send
"""
from __future__ import annotations

import os
import sys
import json
import glob
import argparse
import logging
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")

# ★★수신자 규칙 — 개인 보유 정보이므로 **자기 데이터는 자기에게만** 간다.
#   수신자는 CLI 인자가 아니라 **portfolios/<이메일>.csv 의 파일명**에서 나온다.
#   즉 "그 사람의 포트폴리오 파일이 존재한다"는 사실 자체가 수신 자격이고,
#   등록은 본인이 웹 폼에서 자기 이메일로 직접 한다(= 자기 등록이 동의).
#   임의의 주소로 보내는 경로는 이 파일에 없다.
PORTFOLIO_DIR = os.path.join(HERE, "portfolios")

logging.basicConfig(level=logging.INFO, format="[pfmail] %(message)s")
log = logging.getLogger("pfmail")


def _load(p):
    try:
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def _read(p):
    try:
        with open(p, encoding="utf-8-sig") as f:
            return f.read()
    except Exception:
        return ""


def _esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _won(v):
    if v is None:
        return "-"
    try:
        return format(int(v), ",")
    except Exception:
        return "-"


def _pct(v):
    return ("%+.2f%%" % v) if v is not None else "-"


def _color(v):
    """한국 관례: 상승 빨강, 하락 파랑."""
    if v is None:
        return "#666666"
    return "#c62828" if v > 0 else ("#1565c0" if v < 0 else "#666666")


def render_html(review, strategy_md, when=None):
    """Gmail 안전 HTML — table + inline CSS 만(script·svg·canvas·외부이미지 금지)."""
    when = when or datetime.now().strftime("%Y-%m-%d %H:%M")
    tot = (review or {}).get("totals") or {}
    poss = (review or {}).get("positions") or []

    H = []
    H.append('<div style="font-family:-apple-system,Segoe UI,Malgun Gothic,sans-serif;'
             'background:#eef1f6;padding:18px;">')
    H.append('<div style="max-width:720px;margin:0 auto;background:#ffffff;'
             'border-radius:10px;padding:22px;">')
    H.append('<h2 style="margin:0 0 4px;font-size:19px;color:#111;">내 포트폴리오 전략</h2>')
    H.append('<div style="color:#777;font-size:12px;margin-bottom:16px;">%s · %s</div>'
             % (_esc(when), _esc((review or {}).get("asof_note") or "")))

    # ── 요약 ──
    if tot.get("total_cost"):
        c = _color(tot.get("total_pnl_pct"))
        H.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                 'style="background:#f7f9fc;border-radius:8px;margin-bottom:16px;">'
                 '<tr><td style="padding:14px 16px;">')
        H.append('<div style="font-size:13px;color:#555;">평가금액 '
                 '<b style="font-size:17px;color:#111;">%s원</b></div>' % _won(tot.get("total_value")))
        H.append('<div style="font-size:13px;color:#555;margin-top:4px;">'
                 '원금 %s원 · 손익 <b style="color:%s;">%s원 (%s)</b></div>'
                 % (_won(tot.get("total_cost")), c, _won(tot.get("total_pnl")),
                    _pct(tot.get("total_pnl_pct"))))
        if tot.get("top_weight_pct"):
            warn = " — 한 종목 집중도가 높다" if tot["top_weight_pct"] >= 40 else ""
            H.append('<div style="font-size:12px;color:#777;margin-top:6px;">'
                     '최대 비중: %s %.1f%%%s</div>'
                     % (_esc(tot.get("top_name")), tot["top_weight_pct"], warn))
        H.append('</td></tr></table>')

    # ── ★읽지 못한 행 알림 ──
    #   빠진 종목을 조용히 넘기면 받는 사람은 자기 보유가 다 반영된 줄 안다.
    #   특히 종목코드 오타는 **다른 회사**를 분석하게 만드는 문제라 반드시 보여야 한다.
    _w = [w for w in ((review or {}).get("warnings") or [])
          if "건너뜀" in w or "아님" in w or "비어" in w]
    if _w:
        H.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                 'style="background:#fff8e1;border:1px solid #ffe0a3;border-radius:8px;'
                 'margin-bottom:16px;"><tr><td style="padding:12px 14px;">')
        H.append('<div style="font-size:13px;font-weight:600;color:#8a5a00;">'
                 '아래 %d건은 반영하지 못했습니다</div>' % len(_w))
        H.append('<ul style="margin:6px 0 0;padding-left:18px;font-size:12px;color:#7a5200;">')
        for w in _w[:8]:
            H.append('<li style="margin:2px 0;">%s</li>' % _esc(w))
        H.append('</ul>')
        H.append('<div style="font-size:11px;color:#9a7430;margin-top:6px;">'
                 '등록 화면에서 고치면 다음 메일부터 반영됩니다.</div>')
        H.append('</td></tr></table>')

    # ── 보유 표 ──
    if poss:
        H.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                 'style="border-collapse:collapse;font-size:13px;margin-bottom:8px;">')
        H.append('<tr style="background:#eef2fa;">')
        for h in ("종목", "수량", "평단가", "현재가", "손익", "수익률", "지수대비", "보유"):
            H.append('<th style="padding:8px 6px;text-align:right;color:#333;'
                     'border-bottom:1px solid #dde3ee;font-weight:600;">%s</th>' % h)
        H.append('</tr>')
        for p in poss:
            cc = _color(p.get("pnl_pct"))
            ca = _color(p.get("alpha_pct"))
            H.append('<tr>')
            H.append('<td style="padding:8px 6px;text-align:left;border-bottom:1px solid #f0f2f7;">'
                     '<b>%s</b><div style="color:#999;font-size:11px;">%s</div></td>'
                     % (_esc(p.get("name")), _esc(p.get("ticker"))))
            for v in (p.get("qty"), _won(p.get("avg_price")), _won(p.get("last_close"))):
                H.append('<td style="padding:8px 6px;text-align:right;'
                         'border-bottom:1px solid #f0f2f7;">%s</td>' % _esc(v))
            H.append('<td style="padding:8px 6px;text-align:right;color:%s;'
                     'border-bottom:1px solid #f0f2f7;">%s</td>' % (cc, _won(p.get("pnl"))))
            H.append('<td style="padding:8px 6px;text-align:right;color:%s;font-weight:600;'
                     'border-bottom:1px solid #f0f2f7;">%s</td>' % (cc, _pct(p.get("pnl_pct"))))
            H.append('<td style="padding:8px 6px;text-align:right;color:%s;'
                     'border-bottom:1px solid #f0f2f7;">%s</td>' % (ca, _pct(p.get("alpha_pct"))))
            H.append('<td style="padding:8px 6px;text-align:right;color:#777;'
                     'border-bottom:1px solid #f0f2f7;">%s일</td>'
                     % _esc(p.get("held_days") if p.get("held_days") is not None else "-"))
            H.append('</tr>')
        H.append('</table>')
        H.append('<div style="font-size:11px;color:#999;margin-bottom:16px;">'
                 '지수대비 = 내 수익률 - 같은 기간 코스피 수익률. '
                 '음수여도 지수가 더 빠졌으면 종목 선택은 나쁘지 않았던 것이다.</div>')
    else:
        H.append('<div style="padding:14px;background:#fff8e1;border-radius:8px;'
                 'font-size:13px;color:#7a5c00;margin-bottom:16px;">'
                 'portfolio.csv 에 보유 종목이 없습니다. 엑셀로 열어 매수일시·종목코드·'
                 '종목명·평단가·수량을 채우면 다음 메일부터 표시됩니다.</div>')

    # ── 전략(분석가가 쓴 부분) ──
    if strategy_md.strip():
        H.append('<h3 style="font-size:15px;color:#111;margin:20px 0 8px;'
                 'padding-top:14px;border-top:1px solid #e8ebf2;">오늘의 판단</h3>')
        H.append('<div style="font-size:13.5px;line-height:1.75;color:#222;">')
        for ln in strategy_md.splitlines():
            s = ln.rstrip()
            if not s.strip():
                continue
            if s.startswith("### "):
                H.append('<div style="font-weight:700;margin:14px 0 4px;color:#111;">%s</div>'
                         % _esc(s[4:]))
            elif s.startswith("## "):
                H.append('<div style="font-weight:700;font-size:14.5px;margin:16px 0 6px;'
                         'color:#111;">%s</div>' % _esc(s[3:]))
            elif s.lstrip().startswith(("- ", "* ")):
                H.append('<div style="margin:3px 0 3px 10px;">· %s</div>'
                         % _esc(s.lstrip()[2:]))
            else:
                H.append('<div style="margin:6px 0;">%s</div>' % _esc(s))
        H.append('</div>')
    else:
        H.append('<div style="font-size:12px;color:#999;margin-top:14px;">'
                 '(오늘 전략 코멘트가 없습니다 — 분석가가 portfolio_strategy.md 를 쓰면 여기 들어갑니다)</div>')

    H.append('<div style="margin-top:22px;padding-top:12px;border-top:1px solid #e8ebf2;'
             'font-size:11px;color:#999;line-height:1.6;">'
             '이 메일은 개인 보유 현황이라 <b>본인에게만</b> 발송됩니다(일반 리서치 메일과 별개).<br>'
             '* 표시는 자동매매 대상이 아닌 계좌입니다 — 직접 매매하셔야 합니다.<br>'
             '투자 판단과 책임은 본인에게 있습니다. 수치는 직전 거래일 종가 기준입니다.</div>')
    H.append('</div></div>')
    return "\n".join(H)


def send_one(email, review, strategy, do_send):
    """한 사람에게 그 사람 것만. 반환 (ok:bool, 메시지)."""
    html = render_html(review, strategy)
    tot = review.get("totals") or {}
    subject = "[내 포트폴리오] %s — 평가 %s원 (%s)" % (
        datetime.now().strftime("%Y-%m-%d"),
        _won(tot.get("total_value")), _pct(tot.get("total_pnl_pct")))
    if not do_send:
        return True, "미리보기만(발송 안 함)"
    # ★수신자는 인자로 받은 email 하나뿐. mail_config 의 to 는 읽지 않는다.
    try:
        import research_agent as ra
        cfg = dict(ra.load_mail_config() or {})
        cfg["to"] = email
        ok, msg = ra.send_email_appscript(subject, html, cfg=cfg)
        return ok, msg
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def main():
    ap = argparse.ArgumentParser(
        description="포트폴리오 전략 메일(★자기 데이터는 자기에게만)")
    ap.add_argument("--session", default=None)
    ap.add_argument("--email", default=None, help="이 사람만")
    ap.add_argument("--all", action="store_true", help="portfolios/ 의 전원")
    ap.add_argument("--send", action="store_true", help="실제 발송(없으면 미리보기)")
    args = ap.parse_args()

    sess = args.session
    if not sess:
        c = sorted(glob.glob(os.path.join(OUTPUT_DIR, datetime.now().strftime("%Y-%m-%d") + "_*")))
        sess = c[-1] if c else HERE
    log.info("세션: %s", sess)

    # ★수신 자격 = portfolios/ 에 그 사람 CSV 가 있는가. 없으면 보내지 않는다.
    try:
        import portfolio_review as pr
        people = dict(pr.list_people(PORTFOLIO_DIR))
    except Exception as e:
        log.error("포트폴리오 목록을 못 읽었다: %s", e)
        return 1
    if args.email:
        if args.email not in people:
            log.error("등록되지 않은 주소다: %s", args.email)
            log.error("  portfolios/<이메일>.csv 가 있어야 발송할 수 있다"
                      "(본인이 웹 폼에서 직접 등록).")
            return 1
        targets = [args.email]
    elif args.all:
        targets = sorted(people)
    else:
        targets = sorted(people)[:1] if len(people) == 1 else []
        if not targets:
            log.error("여러 명이 등록돼 있다 — --email 로 지정하거나 --all 을 써라.")
            log.error("  등록: %s", ", ".join(sorted(people)) or "(없음)")
            return 1

    n_ok = 0
    for email in targets:
        rv = _load(os.path.join(sess, "portfolio_review_%s.json" % email))
        if not rv and len(people) == 1:
            # ★이름 없는 파일은 등록자가 1명일 때만 쓴다. 구버전 파일에는 email 필드가
            #   없어서 아래 소유자 검사가 통과해 버린다 — 여러 명이면 아예 손대지 않는다.
            rv = _load(os.path.join(sess, "portfolio_review.json"))
            if rv and rv.get("email") and rv["email"] != email:
                rv = None       # ★남의 데이터를 잘못 보내지 않는다
        if not rv:
            log.error("[%s] portfolio_review 가 없다 — "
                      "`python portfolio_review.py --email %s` 를 먼저 돌려라.", email, email)
            continue
        if rv.get("email") and rv["email"] != email:
            log.error("[%s] 데이터의 소유자가 다르다(%s) — 발송 중단.", email, rv["email"])
            continue

        # ★전략 본문에는 그 사람의 보유가 글로 적혀 있다 — 폴백을 아무에게나 주면
        #   B 가 A 의 보유 논의를 그대로 받는다. 이름 없는 portfolio_strategy.md 는
        #   **등록자가 1명일 때만** 쓴다(누구 것인지 확실할 때만).
        strat = _read(os.path.join(sess, "portfolio_strategy_%s.md" % email))
        if not strat.strip() and len(people) == 1:
            strat = _read(os.path.join(sess, "portfolio_strategy.md"))
        if not strat.strip():
            log.warning("[%s] 전략 코멘트가 없다 — 표만 있는 메일이 된다.", email)
            if len(people) > 1 and os.path.isfile(os.path.join(sess, "portfolio_strategy.md")):
                log.warning("  (이름 없는 portfolio_strategy.md 는 여러 명일 때 쓰지 않는다 — "
                            "portfolio_strategy_%s.md 로 저장하라)", email)

        html = render_html(rv, strat)
        prev = os.path.join(sess, "portfolio_mail_%s.html" % email)
        try:
            with open(prev, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass

        ok, msg = send_one(email, rv, strat, args.send)
        if ok:
            n_ok += 1
            log.info("[%s] %s  (미리보기: %s)", email,
                     "발송 완료" if args.send else "미리보기 생성", os.path.basename(prev))
        else:
            log.error("[%s] 발송 실패: %s", email, msg)

    if not args.send:
        print("")
        print("미리보기만 만들었다. 실제로 보내려면 --send 를 붙여라.")
        print("  대상: %s" % ", ".join(targets))
    print("MAIL_RESULT=%s (%d/%d)" % ("success" if n_ok == len(targets) else "partial",
                                      n_ok, len(targets)))
    return 0 if n_ok == len(targets) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
