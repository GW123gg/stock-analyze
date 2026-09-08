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
    # ★호재·악재 섹션도 본문이다(v11.32 규칙 → v11.33 에서 기계 검사 신설).
    #   2026-08-28 실측: (1-확실) 섹션이 부록 안(전체의 80.7% 지점)에 실려
    #   독자 눈에 닿지 않았다. 파생·ETF 가 3주 전 겪은 것과 **같은 경로**인데,
    #   그때는 위 검사를 만들었으면서 이번 규칙은 산문 지시로만 뒀다 —
    #   같은 실수를 반복하지 않도록 여기서 함께 센다.
    if re.search(r"1-확실|호재·악재|호재/악재", md) and not re.search(
            r"1-확실|호재·악재|호재/악재", body):
        issues.append("호재·악재 섹션이 부록에만 있다 — (1-확실)은 본문이다([5.8]). "
                      "지면이 모자라면 줄이되 옮기지 마라(v11.18 파생·ETF 와 같은 사고).")
    # 뉴스 커버리지 한 줄([5.8] v11.32) — 수집 종목 수와 판정 종목 수를 함께 적었는가
    if re.search(r"news_bundle|뉴스", md) and not re.search(r"특이 뉴스 없음|종 수집", md):
        issues.append("뉴스 커버리지 한 줄이 없다 — '뉴스 N종 수집 / M종 재료 있음 / "
                      "K종 특이사항 없음' + 재료 없는 종목은 '특이 뉴스 없음: <나열>'([5.8]).")

    # ★2-b) 결론이 맨 위에 있는가 (v11.30 — 사용자 요구: "결론 먼저, 설명은 그 다음")
    #   [7.0] 은 원래 1블록(대시보드)을 요구하는데 실제 발행본이 산문으로 시작한 날이 있었다
    #   (2026-08-20·21). 규약만으로는 안 지켜져서 lint 가 직접 본다.
    # 판정은 '첫 표가 얼마나 위에 있나'로 한다. 인용구·머리말은 허용하되,
    # 표가 나오기 전에 **산문이 길게 이어지면** 결론이 묻힌 것이다.
    lines = body.split("\n")
    first_tbl = next((i for i, l in enumerate(lines) if l.count("|") >= 2), None)
    if first_tbl is None:
        issues.append("본문에 표가 하나도 없다 — 지수 방향·오를 종목·내릴 종목·파생을 "
                      "**표로 먼저** 싣고 설명은 그 아래로 내려라([7.0] 1블록).")
    else:
        before = "\n".join(lines[:first_tbl])
        # 표 앞의 '산문 글자수' — 제목·인용구·표머리는 빼고 실제 문단만 센다.
        prose = "\n".join(l for l in lines[:first_tbl]
                          if l.strip() and not l.lstrip().startswith(("#", ">", "-", "*", "|")))
        if len(prose) > 500:
            issues.append("첫 표가 너무 아래에 있다(앞선 산문 %dB) — 독자는 첫 화면에서 "
                          "결론을 봐야 한다. **지수 표를 맨 앞으로** 올리고 설명은 그 뒤로 "
                          "내려라([7.0] 1블록)." % len(prose))
        head = body[:max(1400, first_tbl and len(before) + 800)]
        if "코스피" not in head or "코스닥" not in head:
            issues.append("맨 위 표에 코스피·코스닥이 함께 있어야 한다 — "
                          "지수 방향을 가장 먼저 실어라([7.0] 1블록 1).")

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


# ★v11.41(호스트 감사 2026-09-08): 원장이 무효화·철회·기각한 근거가 지시서에 수치로 살아남는 것을 잡는다.
#   실측: '66%(12/18)'(출처 부재·재현 불가)·'6회 재현 + alpha 음수'(A22 무효화)·'검증된 사실: 강점은 숏'
#   (24회차 철회)·'사후검증 표본 0'(공포 픽 68행 존재)이 정정된 절 옆의 다른 절에 그대로 남아 있었다.
#   정정하면 이 목록에서 지우지 말고 남겨라 — 재유입(복붙·구판 병합)을 막는 것이 목적이다. 자문(막지 않음).
_BANNED_CITATIONS = [
    (r"66%\(12/18\)", "시장콜 66%(12/18): 출처 없음·accuracy_log 재현 불가(6월 KOSPI 콜 7건) — 09-08 감사"),
    (r"45%\(14/31\)", "단순 롱 45%(14/31): 같은 출처 부재 — 09-08 감사"),
    (r"86%\(31/36\)", "숏 86%(31/36): 원장 A24 가 07-26 철회(적중률 축 폐기)"),
    (r"6회 재현 \+ alpha 음수", "k5 게이트 '6회 재현': 같은 6일 재계산 + A22 룩어헤드 빈티지 무효화"),
    (r"검증된 사실: 이 시스템의 강점은", "F8 '강점은 숏': 24회차 숏 선별력 없음 확정으로 철회"),
    (r"사후검증 표본 0", "F8 국면 게이트 '표본 0': 공포 만기 픽 68행/15일 존재(09-08) — 낡은 서술"),
    (r"회고 3회 재현·'검증' 등급", "F8-b '검증' 등급: 회고 37회차가 '관찰(계보 유지)'로 표기"),
]
_DOC_COPIES = [
    os.path.join(HERE, "setup_kit", "02_코워크_예정작업", "analyze_instructions_mcp.md"),
    os.path.join(os.path.dirname(HERE), "stock_research_mcp", "analyze_instructions_mcp.md"),
]


def _doc_checks():
    """★v11.41 지시서 문서 검사 2종(자문) — (1) 무효화된 인용 잔존 (2) MCP판 사본이 원본과 다른 빌드인가.
    출력 마지막 줄: DOC_CITATIONS=ok|warn:N 과 DOC_COPY=ok|stale:N|missing."""
    import hashlib
    src = os.path.join(HERE, "cowork_instructions.md")
    try:
        with open(src, encoding="utf-8", errors="replace") as f:
            body = f.read()
    except Exception as e:
        print("cowork_instructions.md 읽기 실패: %s" % e)
        print("DOC_CITATIONS=error")
        return 1
    n_bad = 0
    for pat, why in _BANNED_CITATIONS:
        hits = [i + 1 for i, ln in enumerate(body.split("\n")) if re.search(pat, ln)]
        if hits:
            n_bad += 1
            print("★무효화된 인용 잔존 L%s — %s" % (",".join(str(h) for h in hits[:6]), why))
    print("DOC_CITATIONS=%s" % ("ok" if not n_bad else "warn:%d" % n_bad))
    # (2) 사본 빌드 해시 대조 — build_mcp_instructions.py 가 머리에 'sha256:<원본 해시>' 마커를 남긴다
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    stale, found = 0, 0
    for p in _DOC_COPIES:
        if not os.path.isfile(p):
            continue
        found += 1
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                head = f.read(4000)
        except Exception:
            head = ""
        m = re.search(r"sha256:([0-9a-f]{16})", head)
        if not m or m.group(1) != sha:
            stale += 1
            print("★MCP판 사본이 원본과 다른 빌드다: %s (%s) → python build_mcp_instructions.py"
                  % (p, "마커 없음(구판 수동 사본)" if not m else "해시 불일치"))
    print("DOC_COPY=%s" % ("missing" if not found else ("ok" if not stale else "stale:%d" % stale)))
    return 0


def main():
    ap = argparse.ArgumentParser(description="아침 리포트 [7.0] 계약 검사(자문)")
    ap.add_argument("--session", default=None, help="세션 폴더(없으면 오늘 최신)")
    ap.add_argument("--file", default=None, help="md 파일 직접 지정")
    ap.add_argument("--history", action="store_true",
                    help="과거 리포트 전수 측정(읽기 전용 — _archive 포함, 추세 표)")
    ap.add_argument("--rules", action="store_true",
                    help="★지시서 비대화 계측(과적합 가지치기 의무 추적) + 무효화 인용·사본 빌드 검사(v11.41)")
    ap.add_argument("--doc", action="store_true",
                    help="★v11.41 지시서 문서 검사만(무효화 인용 잔존·MCP판 사본 해시)")
    args = ap.parse_args()

    if args.doc:
        return _doc_checks()
    if args.rules:
        rc = _rules_growth()
        print()
        _doc_checks()
        return rc

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
