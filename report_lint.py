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
    # ★파생·ETF 는 **추천이 없어도** 본문에 한 줄이 있어야 한다([7.0] 4.5, v11.18).
    #   실측: 분석가가 매일 검토해 "변동성 과열이라 안 한다"는 결론까지 냈는데 그게
    #   부록에만 적혀, 독자에겐 "파생 얘기가 아예 없다"로 보였다.
    if not re.search(r"파생|선물|옵션|ETF", body):
        issues.append("본문에 파생·ETF 언급이 없다 — 추천이 없어도 "
                      "'오늘 파생·ETF 추천 없음 — <이유>' 한 줄을 넣어라([7.0] 4.5).")

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


def _rules_growth(n=12):
    """★지시서 비대화 계측 [v11.23] — 가지치기 의무가 집행되고 있는지 숫자로 본다.

    [왜] 회고 v11.6 이 "규칙은 추가만 되고 제거되지 않으면 그 자체가 과적합"이라며 가지치기를
    의무화했는데, 그 의무가 지켜지는지 **아무도 세지 않았다.** 2026-08-08 실측: 51커밋 동안
    증가 50회·감소 0회, 1,204→2,611줄(+117%/33일). 의무가 있어도 계측이 없으면 안 지켜진다.
    """
    import subprocess
    f = "cowork_instructions.md"
    try:
        log = subprocess.run(["git", "log", "--format=%H|%ad|%s", "--date=short", "--", f],
                             cwd=HERE, capture_output=True, text=True,
                             encoding="utf-8").stdout.strip().split("\n")
    except Exception as e:
        print("git 조회 실패: %s" % e)
        print("RULES_GROWTH=error")
        return 1
    rows = []
    for line in [x for x in log if x.strip()]:
        h, d, s = line.split("|", 2)
        body = subprocess.run(["git", "show", "%s:%s" % (h, f)], cwd=HERE,
                              capture_output=True, text=True, encoding="utf-8").stdout
        rows.append((d, body.count("\n"), s[:44]))
    rows.reverse()
    inc = sum(1 for i in range(1, len(rows)) if rows[i][1] > rows[i - 1][1])
    dec = sum(1 for i in range(1, len(rows)) if rows[i][1] < rows[i - 1][1])
    print("%s 줄 수 추이 (최근 %d커밋)" % (f, min(n, len(rows))))
    print("%-12s %7s %8s  %s" % ("날짜", "줄수", "증감", "커밋"))
    for i, (d, ln, s) in enumerate(rows[-n:]):
        j = len(rows) - min(n, len(rows)) + i
        delta = "" if j == 0 else "%+d" % (ln - rows[j - 1][1])
        print("%-12s %7d %8s  %s" % (d, ln, delta, s))
    print()
    print("전체 %d커밋: 증가 %d회 / 감소 %d회 | %d → %d줄 (%+.0f%%)"
          % (len(rows), inc, dec, rows[0][1], rows[-1][1],
             (rows[-1][1] / rows[0][1] - 1) * 100 if rows[0][1] else 0))
    # 감소가 한 번도 없으면 가지치기 의무가 미집행이다 — 자문이므로 막지는 않는다
    if dec == 0 and len(rows) > 5:
        print("★가지치기 미집행: 감소 0회다. 회고 v11.6 의무(청소 후보 지목→물리 삭제)를")
        print("  이번 회차에 집행하거나, 왜 지울 것이 없는지 근거를 남겨라.")
        print("RULES_GROWTH=warn:no_pruning")
        return 0
    print("RULES_GROWTH=ok:inc%d_dec%d" % (inc, dec))
    return 0


def main():
    ap = argparse.ArgumentParser(description="아침 리포트 [7.0] 계약 검사(자문)")
    ap.add_argument("--session", default=None, help="세션 폴더(없으면 오늘 최신)")
    ap.add_argument("--file", default=None, help="md 파일 직접 지정")
    ap.add_argument("--history", action="store_true",
                    help="과거 리포트 전수 측정(읽기 전용 — _archive 포함, 추세 표)")
    ap.add_argument("--rules", action="store_true",
                    help="★지시서 비대화 계측(과적합 가지치기 의무 추적)")
    args = ap.parse_args()

    if args.rules:
        return _rules_growth()

    if args.history:
        # ★읽기 전용이다. _archive 는 원래 분석·수정 금지 구역이지만(절대규칙 4),
        #   이 모드는 사용자가 명시 요청한 '과거 리포트 전수 검토' 용도로 md 를 읽기만 한다.
        #   어떤 세션 파일도 쓰지 않는다.
        import glob as _g
        paths = sorted(_g.glob(os.path.join(OUTPUT_DIR, "20??-??-??_*", "03_final_report.md"))
                       + _g.glob(os.path.join(OUTPUT_DIR, "_archive", "20??-??-??_*",
                                              "03_final_report.md")))
        print("%-20s %8s %7s %7s %5s %5s %5s %5s" %
              ("세션", "md_B", "본문_B", "부록%", "위반", "파생", "이모지", "경고"))
        print("-" * 72)
        tot_warn = 0
        for mp in paths:
            sess = os.path.basename(os.path.dirname(mp))
            with open(mp, encoding="utf-8-sig", errors="replace") as f:
                md_h = f.read()
            iss_h, m_h = lint_report(md_h)
            body_h, _app = split_report(md_h)
            forb = sum(len(re.findall(pat, body_h)) for pat, _lab in _FORBIDDEN_BODY)
            deriv = len(re.findall(r"파생|선물|옵션|ETF", body_h))
            appp = 100 * m_h["app_b"] // max(1, m_h["md_b"])
            tot_warn += len(iss_h)
            print("%-20s %8s %7s %6d%% %5d %5d %5d %5d" %
                  (sess, format(m_h["md_b"], ","), format(m_h["body_b"], ","),
                   appp, forb, deriv, m_h["emoji"], len(iss_h)))
        print("-" * 72)
        print("%d개 리포트 · 경고 총 %d건 (현행 계약 기준 소급 측정 — 과거 리포트는"
              % (len(paths), tot_warn))
        print("당시 계약이 달랐으므로 위반 수는 '지금 기준이면'의 참고치다)")
        print("REPORT_LINT=history:%d" % len(paths))
        return 0

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
