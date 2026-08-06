#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report_lint.py — 아침 리포트([7.0] 이메일 가독성 계약) 기계 검사 [v11.14 신규]

[왜 필요한가 — 실측]
  · 렌더(HTML)는 md 의 약 4.1배다(2026-08-06: md 26,137B -> 106,165B. 셀마다 붙는
    Gmail 안전 inline style 900개가 원인 — 렌더 축소는 지뢰라 **md 예산**으로 잡는다).
  · Gmail 은 102,400B 를 넘으면 본문을 접는다. 7/28~8/6 열흘 중 9일이 초과였다
    (최대 188KB). [7.0] 이 지켜진 오늘도 106KB — 계약을 사람이 매번 기억할 수 없다.

[성격]
  · **자문(advisory)** — 발송을 막지 않는다(스키마 게이트와 다름). 문체 때문에
    라이브 메일이 하루 통째로 빠지는 것이 접힘보다 나쁘다.
  · 출력 마지막 줄 `REPORT_LINT=ok` 또는 `REPORT_LINT=warn:<건수>` 를 코워크가
    최종 보고에 그대로 옮긴다.

[사용] python report_lint.py --session output\2026-08-06_063600
"""
from __future__ import annotations

import os
import re
import sys
import glob
import argparse
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")

# 실측 2026-08-06: 106,165 / 26,137 = 4.06. 보수적으로 4.1.
RENDER_RATIO = 4.1
GMAIL_CLIP_B = 102_400
# md 예산: 24,500 * 4.1 = 100,450 — 접힘 임계 바로 아래.
MD_BUDGET_B = 24_500
BODY_BUDGET_B = 8_000          # 본문(--- 앞). 오늘 5,935B — 여유 있는 상한.

# 본문에 나오면 안 되는 것([7.0]: 게이트ID·지시번호·raw 필드명·모호한 지연 표현)
_FORBIDDEN_BODY = [
    (r"\bF[1-9]\b", "게이트 ID(F1..F9)"),
    (r"\bB[0-9]{1,2}\b(?![%년월일])", "게이트 ID(B..)"),
    (r"\[[0-9]+\.[0-9]+\]", "지시 번호([5.9] 등)"),
    (r"force_scores?|deriv_sentiment|market_caution|ecos_macro|vkospi\b|"
     r"credit_balance|flow_data|signals_snapshot|retro_feedback|predictions\.json",
     "raw 파일·필드명"),
    (r"T\+1~2", "모호한 지연 표현(숫자로 적어라)"),
]


def split_report(md):
    """(본문, 부록). 첫 '\\n---' 줄이 경계. 경계가 없으면 부록 없음."""
    m = re.search(r"\n---[ \t]*\n", md)
    if not m:
        return md, ""
    return md[:m.start()], md[m.start():]


def lint_report(md, html_bytes=None):
    """리포트 md 검사. 반환 (issues:list[str], metrics:dict). 부작용 없음."""
    issues = []
    body, app = split_report(md)
    md_b = len(md.encode("utf-8"))
    body_b = len(body.encode("utf-8"))
    app_b = len(app.encode("utf-8"))

    # 1) 크기 — 실제 HTML 이 있으면 그걸로, 없으면 비율 추정
    est = html_bytes if html_bytes else int(md_b * RENDER_RATIO)
    src = "실측" if html_bytes else ("추정 md x %.1f" % RENDER_RATIO)
    if est > GMAIL_CLIP_B:
        over_md = int((est - GMAIL_CLIP_B) / RENDER_RATIO)
        issues.append("메일이 Gmail 에서 접힌다(%s %sB > %sB). md 를 약 %sB 줄여라 — "
                      "부록의 표·중복 서술부터, 판단 근거는 마지막에."
                      % (src, format(est, ","), format(GMAIL_CLIP_B, ","),
                         format(max(over_md, md_b - MD_BUDGET_B), ",")))
    if body_b > BODY_BUDGET_B:
        issues.append("본문이 길다(%sB > %sB) — 본문은 요지만, 상세는 부록으로."
                      % (format(body_b, ","), format(BODY_BUDGET_B, ",")))

    # 2) 구조
    if not app:
        issues.append("본문/부록 구분자(`---`)가 없다 — 전문이 본문으로 발송된다.")
    elif "## 부록" not in app and "## 상세" not in app:
        issues.append("부록 머리글이 없다(`## 부록 — 분석 근거` 권장).")
    if ("## 오늘의 픽" not in body) and ("픽 없음" not in body):
        issues.append("본문에 '오늘의 픽'(또는 '픽 없음' 명시)이 없다.")
    if "체크포인트" not in body:
        issues.append("본문에 '내일 체크포인트'가 없다.")

    # 3) 본문 금지 토큰
    for pat, label in _FORBIDDEN_BODY:
        hits = re.findall(pat, body)
        if hits:
            issues.append("본문에 %s %d건 — 부록으로 옮겨라(예: %s)."
                          % (label, len(hits), str(hits[0])[:20]))

    # 4) 이모지(BMP 밖) — cp949 콘솔·일부 클라이언트에서 깨진다(절대규칙 2)
    emoji = sum(1 for c in md if ord(c) > 0xFFFF)
    if emoji:
        issues.append("4바이트 문자(이모지) %d개 — 기호는 BMP(▲▼)만." % emoji)

    return issues, {"md_b": md_b, "body_b": body_b, "app_b": app_b,
                    "est_render_b": est, "emoji": emoji}


def main():
    ap = argparse.ArgumentParser(description="아침 리포트 [7.0] 계약 검사(자문)")
    ap.add_argument("--session", default=None, help="세션 폴더(없으면 오늘 최신)")
    ap.add_argument("--file", default=None, help="md 파일 직접 지정")
    args = ap.parse_args()

    if args.file:
        path, html_path = args.file, None
    else:
        sess = args.session
        if not sess:
            c = sorted(glob.glob(os.path.join(
                OUTPUT_DIR, datetime.now().strftime("%Y-%m-%d") + "_*")))
            sess = c[-1] if c else None
        if not sess or not os.path.isdir(sess):
            print("세션을 찾지 못했다 — --session 으로 지정하라.")
            print("REPORT_LINT=error")
            return 1
        path = os.path.join(sess, "03_final_report.md")
        html_path = os.path.join(sess, "03_final_report.html")

    if not os.path.isfile(path):
        print("리포트가 없다: %s" % path)
        print("REPORT_LINT=error")
        return 1

    with open(path, encoding="utf-8-sig") as f:
        md = f.read()
    html_bytes = None
    if html_path and os.path.isfile(html_path):
        html_bytes = os.path.getsize(html_path)

    issues, m = lint_report(md, html_bytes)
    print("[lint] md %sB (본문 %s / 부록 %s) -> 렌더 %sB%s" %
          (format(m["md_b"], ","), format(m["body_b"], ","), format(m["app_b"], ","),
           format(m["est_render_b"], ","), "" if html_bytes else " (추정)"))
    for i, msg in enumerate(issues, 1):
        print("[lint] %d) %s" % (i, msg))
    if issues:
        print("REPORT_LINT=warn:%d" % len(issues))
    else:
        print("REPORT_LINT=ok")
    return 0                     # ★자문 — 항상 0 (발송을 막지 않는다)


if __name__ == "__main__":
    sys.exit(main())
