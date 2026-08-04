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

# ★수신자 고정 — 개인 보유 정보라 CLI 로 바꾸지 못하게 한다(오발송 방지).
RECIPIENT = "student01@example.kr"

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
             '이 메일은 개인 보유 현황이라 본인에게만 발송됩니다(일반 리서치 메일과 별개).<br>'
             '투자 판단과 책임은 본인에게 있습니다. 수치는 직전 거래일 종가 기준입니다.</div>')
    H.append('</div></div>')
    return "\n".join(H)


def main():
    ap = argparse.ArgumentParser(description="포트폴리오 전략 메일(★단일 수신자)")
    ap.add_argument("--session", default=None)
    ap.add_argument("--send", action="store_true", help="실제 발송(없으면 미리보기만)")
    args = ap.parse_args()

    sess = args.session
    if not sess:
        c = sorted(glob.glob(os.path.join(OUTPUT_DIR, datetime.now().strftime("%Y-%m-%d") + "_*")))
        sess = c[-1] if c else HERE
    log.info("세션: %s", sess)

    review = _load(os.path.join(sess, "portfolio_review.json")) \
        or _load(os.path.join(HERE, "portfolio_review.json"))
    if not review:
        log.error("portfolio_review.json 이 없다 — `python portfolio_review.py` 를 먼저 돌려라.")
        return 1
    strategy = _read(os.path.join(sess, "portfolio_strategy.md"))
    if not strategy.strip():
        log.warning("portfolio_strategy.md 가 없다 — 표만 있는 메일이 된다"
                    "(분석가가 판단을 쓰면 함께 나간다).")

    html = render_html(review, strategy)
    prev = os.path.join(sess, "portfolio_mail_preview.html")
    try:
        with open(prev, "w", encoding="utf-8") as f:
            f.write(html)
        log.info("미리보기 저장: %s", prev)
    except Exception as e:
        log.warning("미리보기 저장 실패: %s", e)

    tot = review.get("totals") or {}
    subject = "[내 포트폴리오] %s — 평가 %s원 (%s)" % (
        datetime.now().strftime("%Y-%m-%d"),
        _won(tot.get("total_value")), _pct(tot.get("total_pnl_pct")))

    if not args.send:
        print("")
        print("미리보기만 만들었다. 실제로 보내려면 --send 를 붙여라.")
        print("  수신자: %s  (고정 — CLI 로 바꿀 수 없다)" % RECIPIENT)
        print("  제목  : %s" % subject)
        print("  본문  : %s" % prev)
        return 0

    # ★단일 수신자로만 발송 — mail_config 의 to 는 읽지 않는다.
    try:
        import research_agent as ra
        cfg = dict(ra.load_mail_config() or {})
        cfg["to"] = RECIPIENT          # ← 여기서만 정해진다
        ok, msg = ra.send_email_appscript(subject, html, cfg=cfg)
    except Exception as e:
        log.error("발송 실패: %s: %s", type(e).__name__, e)
        return 1
    if ok:
        log.info("발송 완료 -> %s", RECIPIENT)
        print("MAIL_RESULT=success")
        return 0
    log.error("발송 실패: %s", msg)
    print("MAIL_RESULT=failed")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
