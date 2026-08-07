#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report_mail.py — 2차 리포트(장중·마감·전야) 발송 [v11.17 신규]

[왜 따로인가]
  아침 리포트는 `research_agent.py mail` 이 대시보드·콜카드까지 얹어 보낸다(무겁고
  아침 전용). 장중·마감·전야는 **짧은 후속 보고**라 같은 렌더를 태울 이유가 없고,
  태우면 [7.0] 크기 예산도 깨진다. 그래서 가벼운 md->HTML 경로로 따로 보낸다.
  ★두 경로를 통합하지 마라(CLAUDE.md 리팩터 지뢰 — 렌더 경로 2종은 의도된 분리다).

[종류]
  intraday    장중 점검   — 세션의 intraday_*.md
  after_close 마감 후 결산 — 세션의 after_close.md
  night       전야 리서치 — 루트의 night_preview.md

[★신선도 — 이 파일들은 '있기만 해도' 통과한다]
  night_preview.md 는 루트에서 매일 밤 덮어써진다. 23시 작업이 실패하면 **어젯밤 파일이
  그대로 남아** 오늘 것처럼 발송된다(CLAUDE.md 의 그 함정). 그래서 짝 json 의
  `for_date`/`trade_date` 와 파일 mtime 을 둘 다 본다. 낡았으면 **보내지 않는다.**

[★중복 발송]
  같은 리포트를 두 번 보내지 않는다. 내용이 바뀌면(해시 변화) 다시 보낼 수 있다.
  의도적 재발송은 --force.

[사용]
  python report_mail.py --kind intraday                 # 미리보기(발송 안 함)
  python report_mail.py --kind intraday --send
  python report_mail.py --kind night --send
  python report_mail.py --check                         # 무엇을 보낼 수 있는지 + 할당량
"""
from __future__ import annotations

import os
import re
import sys
import glob
import json
import hashlib
import argparse
import logging
from datetime import datetime, date

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
SENT_INDEX = os.path.join(HERE, "report_mail_sent.json")

logging.basicConfig(level=logging.INFO, format="[rmail] %(message)s")
log = logging.getLogger("rmail")

# Gmail 소비자 계정 하루 수신자 한도. 아침·포트폴리오·확인코드와 **나눠 쓴다**.
GMAIL_DAILY_RECIPIENTS = 100

KINDS = {
    "intraday": {
        "label": "장중 점검",
        "scope": "session",
        "patterns": ["intraday_*.md"],
        "meta": "intraday_review.json",
        "date_field": "trade_date",
        "subject": "[장중 점검] %s — 아침 픽 진행 상황",
    },
    "after_close": {
        "label": "마감 후 결산",
        "scope": "session",
        "patterns": ["after_close.md"],
        "meta": "intraday_review.json",
        "date_field": "trade_date",
        "subject": "[마감 후] %s 정리",
    },
    "night": {
        "label": "전야 리서치",
        "scope": "root",
        "patterns": ["night_preview.md"],
        "meta": "night_preview.json",
        "date_field": "for_date",
        "subject": "[전야] %s 시장 방향 예상",
    },
}


def _read(p):
    try:
        with open(p, encoding="utf-8-sig") as f:
            return f.read()
    except Exception:
        return ""


def _load_json(p):
    try:
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def latest_session():
    c = sorted(glob.glob(os.path.join(OUTPUT_DIR, "20??-??-??_*")))
    c = [p for p in c if os.path.isdir(p) and "_archive" not in p and "_designtest" not in p]
    return c[-1] if c else None


def find_report(kind, session=None):
    """(md경로|None, 짝 json경로|None)."""
    k = KINDS[kind]
    base = HERE if k["scope"] == "root" else (session or latest_session())
    if not base or not os.path.isdir(base):
        return None, None
    hits = []
    for pat in k["patterns"]:
        hits += sorted(glob.glob(os.path.join(base, pat)))
    if not hits:
        return None, None
    md = max(hits, key=os.path.getmtime)      # 여러 개면 가장 최근 것
    meta = os.path.join(base, k["meta"]) if k.get("meta") else None
    return md, (meta if meta and os.path.isfile(meta) else None)


def freshness(kind, md_path, meta_path, today=None):
    """반환 (ok:bool, 사유:str, 기준일:str|None).

    ★'파일이 있다'는 신선함이 아니다. 짝 json 의 날짜와 mtime 을 둘 다 본다.
    """
    today = today or date.today()
    k = KINDS[kind]
    age_h = (datetime.now() - datetime.fromtimestamp(os.path.getmtime(md_path))).total_seconds() / 3600.0

    asof = None
    if meta_path:
        d = _load_json(meta_path)
        asof = str(d.get(k["date_field"]) or "").strip()[:10] or None

    if kind == "night":
        # 전야는 **내일**을 다룬다 — for_date 가 오늘이나 내일이어야 한다.
        if asof:
            try:
                fd = datetime.strptime(asof, "%Y-%m-%d").date()
            except ValueError:
                return False, "for_date 를 읽지 못했다(%s)" % asof, asof
            if (fd - today).days < 0:
                return False, ("지난 밤 것이다(%s 대상) — 23시 작업이 실패했을 수 있다. "
                               "보내지 않는다." % asof), asof
        if age_h > 20:
            return False, "파일이 %.0f시간 전 것이다 — 오늘 밤 작업 산출물이 아니다." % age_h, asof
        return True, "", asof

    # 장중·마감은 오늘 세션 것이어야 한다
    if asof and asof != today.strftime("%Y-%m-%d"):
        return False, "오늘(%s) 것이 아니다(%s 기준)." % (today.strftime("%Y-%m-%d"), asof), asof
    if age_h > 24:
        return False, "파일이 %.0f시간 전 것이다." % age_h, asof
    return True, "", asof


# ★Gmail 은 <style> 블록을 **제거한다**. 태그마다 style 속성을 직접 박아야 한다
#   (안 그러면 표가 테두리 없이 무너지고, 일부 클라이언트는 CSS 를 본문 글자로 보여준다).
_INLINE = {
    "table": "border-collapse:collapse;width:100%;font-size:13px;margin:10px 0;",
    "th": "border:1px solid #dde3ee;padding:6px 8px;text-align:left;"
          "background:#eef2fa;font-weight:600;",
    "td": "border:1px solid #dde3ee;padding:6px 8px;text-align:left;",
    "h1": "font-size:17px;margin:18px 0 6px;color:#111;",
    "h2": "font-size:16px;margin:18px 0 6px;color:#111;",
    "h3": "font-size:15px;margin:16px 0 6px;color:#111;",
    "h4": "font-size:14px;margin:14px 0 6px;color:#111;",
    "hr": "border:0;border-top:1px solid #e8ebf2;margin:16px 0;",
    "blockquote": "margin:10px 0;padding:8px 12px;background:#f7f9fc;"
                  "border-left:3px solid #c8d3e8;color:#444;",
    "ul": "margin:8px 0;padding-left:20px;",
    "ol": "margin:8px 0;padding-left:20px;",
    "li": "margin:3px 0;",
    "p": "margin:8px 0;",
}


def inline_styles(html):
    """<tag> -> <tag style="..."> . 이미 style 이 있으면 건드리지 않는다."""
    for tag, css in _INLINE.items():
        html = re.sub(r"<%s(?=[\s>])(?![^>]*\bstyle=)" % tag,
                      '<%s style="%s"' % (tag, css), html)
        html = html.replace("<%s>" % tag, '<%s style="%s">' % (tag, css))
    return html


def render(kind, md_text, asof=None, capture_html=""):
    """Gmail 안전 HTML. 아침 렌더(render_report_html)를 쓰지 않는다 — 무겁고 아침 전용."""
    try:
        import research_agent as ra
        body = ra._fallback_md_to_html(md_text)
    except Exception:
        body = "<pre>%s</pre>" % (md_text.replace("&", "&amp;")
                                  .replace("<", "&lt;").replace(">", "&gt;"))
    body = inline_styles(body)
    k = KINDS[kind]
    H = []
    H.append('<div style="font-family:-apple-system,Segoe UI,Malgun Gothic,sans-serif;'
             'background:#eef1f6;padding:18px;">')
    H.append('<div style="max-width:720px;margin:0 auto;background:#fff;'
             'border-radius:10px;padding:22px;color:#222;line-height:1.7;font-size:14px;">')
    H.append('<div style="font-size:12px;color:#777;margin-bottom:2px;">%s</div>' % k["label"])
    H.append('<div style="font-size:12px;color:#999;margin-bottom:16px;'
             'padding-bottom:12px;border-bottom:1px solid #e8ebf2;">%s%s</div>'
             % (datetime.now().strftime("%Y-%m-%d %H:%M"),
                (" · 기준 %s" % asof) if asof else ""))
    if capture_html:
        H.append(capture_html)
    H.append(body)
    H.append('<div style="margin-top:20px;padding-top:12px;border-top:1px solid #e8ebf2;'
             'font-size:11px;color:#999;line-height:1.6;">'
             '아침 리포트의 후속 보고입니다. 투자 판단과 책임은 본인에게 있습니다.</div>')
    H.append('</div></div>')
    return "\n".join(H)


def capture_status(kind, session=None):
    """이 시각의 카이로스 캡처 상태. 반환 dict.

    ★장중·장후·전야 리포트는 **그 시각에 살아 있는 값**이 있어야 한다(v11.19 사용자 요구).
      아침 06:20 자료만 재탕하면 이미 아침 메일로 나간 내용을 되풀이하는 것이다.
      특히 밤에는 KRX 정규장이 닫혀 공개 API 가 거의 죽어, 야간선물은 HTS 캡처가
      **유일한 국내 실시간 창구**다.
    """
    base = session or latest_session()
    out = {"required": True, "present": False, "n_ok": 0, "n_req": 0,
           "age_min": None, "screens": [], "failed": [], "path": None}
    if not base:
        return out
    p = os.path.join(base, "hts_capture_%s.json" % kind)
    if not os.path.isfile(p):
        return out
    d = _load_json(p)
    out["present"] = True
    out["path"] = p
    out["n_ok"] = int(d.get("n_ok") or 0)
    out["n_req"] = int(d.get("n_requested") or 0)
    out["blocked"] = d.get("blocked")
    try:
        gen = datetime.strptime(str(d.get("generated_at"))[:19], "%Y-%m-%d %H:%M:%S")
        out["age_min"] = int((datetime.now() - gen).total_seconds() // 60)
    except Exception:
        pass
    for c in (d.get("captures") or []):
        nm = "%s(%s)" % (c.get("screen"), c.get("screen_no") or "")
        if str(c.get("status")) == "ok":
            out["screens"].append(nm)
        else:
            out["failed"].append("%s — %s" % (nm, c.get("status")))
    return out


def render_capture_block(cs, kind):
    """메일 상단에 붙는 카이로스 캡처 상태 블록. 실패를 **숨기지 않는다**."""
    label = KINDS[kind]["label"]
    if not cs["present"]:
        return ('<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" '
                'style="background:#ffebee;border:1px solid #ffcdd2;border-radius:8px;'
                'margin-bottom:16px;"><tr><td style="padding:12px 14px;">'
                '<div style="font-size:13px;font-weight:600;color:#b71c1c;">'
                '실시간 화면 캡처 없음</div>'
                '<div style="font-size:12px;color:#8a3a3a;margin-top:4px;line-height:1.6;">'
                '이 %s 보고는 <b>아침 06:20 자료만</b> 사용했습니다. 장중에 바뀐 수급·'
                '베이시스·야간선물은 반영되지 않았습니다 — 그만큼 할인해서 읽으세요.</div>'
                '</td></tr></table>' % label)
    ok, req = cs["n_ok"], cs["n_req"]
    bad = ok < req
    bg, bd, fg = (("#fff8e1", "#ffe0a3", "#8a5a00") if bad
                  else ("#f7f9fc", "#e2e8f4", "#26437a"))
    H = ['<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" '
         'style="background:%s;border:1px solid %s;border-radius:8px;margin-bottom:16px;">'
         '<tr><td style="padding:12px 14px;">' % (bg, bd)]
    H.append('<div style="font-size:13px;font-weight:600;color:%s;">'
             '실시간 화면 캡처 %d/%d%s</div>'
             % (fg, ok, req, ("  · %d분 전" % cs["age_min"]) if cs["age_min"] is not None else ""))
    if cs["screens"]:
        H.append('<div style="font-size:12px;color:#555;margin-top:4px;">%s</div>'
                 % ", ".join(cs["screens"][:8]))
    if cs["failed"]:
        H.append('<div style="font-size:12px;color:#8a5a00;margin-top:4px;line-height:1.6;">'
                 '실패: %s<br>실패한 화면의 값은 <b>확인 불가</b>입니다 — 0 으로 읽지 마세요.</div>'
                 % ", ".join(cs["failed"][:6]))
    H.append('</td></tr></table>')
    return "".join(H)


def _sent_index():
    return _load_json(SENT_INDEX) or {}


def _mark_sent(key, sha, n_to):
    idx = _sent_index()
    idx[key] = {"sent_at": datetime.now().isoformat(timespec="seconds"),
                "sha": sha, "recipients": n_to}
    try:
        from common import save_json_atomic
        save_json_atomic(SENT_INDEX, idx)
    except Exception:
        with open(SENT_INDEX, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False, indent=1)


def quota_note(n_to):
    """오늘 이 계정이 몇 통이나 쓰는지 어림. 아침 리포트와 **같은 한도**를 나눠 쓴다."""
    used = 0
    for v in _sent_index().values():
        try:
            if str(v.get("sent_at", ""))[:10] == date.today().strftime("%Y-%m-%d"):
                used += int(v.get("recipients") or 0)
        except Exception:
            pass
    return ("2차 리포트로 오늘 %d명 발송함(+이번 %d명). "
            "아침 리포트·포트폴리오·확인코드가 같은 하루 %d명 한도를 나눠 쓴다."
            % (used, n_to, GMAIL_DAILY_RECIPIENTS))


def main():
    ap = argparse.ArgumentParser(description="2차 리포트(장중·마감·전야) 발송")
    ap.add_argument("--kind", choices=sorted(KINDS), help="보낼 종류")
    ap.add_argument("--session", default=None, help="세션 폴더(장중·마감용)")
    ap.add_argument("--send", action="store_true", help="실제 발송(없으면 미리보기)")
    ap.add_argument("--force", action="store_true", help="중복·신선도 가드를 넘어 강행")
    ap.add_argument("--no-capture-check", action="store_true",
                    help="카이로스 캡처가 아예 없어도 보낸다(권장하지 않음)")
    ap.add_argument("--check", action="store_true", help="무엇을 보낼 수 있는지만 본다")
    args = ap.parse_args()

    if args.check or not args.kind:
        log.info("%-12s %-10s %-10s %s", "종류", "상태", "캡처", "파일")
        log.info("-" * 72)
        for kind in sorted(KINDS):
            md, meta = find_report(kind, args.session)
            if not md:
                log.info("%-12s %-10s %-10s -", kind, "없음", "-")
                continue
            ok, why, asof = freshness(kind, md, meta)
            _cs = capture_status(kind, args.session)
            _cap = ("캡처 %d/%d" % (_cs["n_ok"], _cs["n_req"])) if _cs["present"] else "★캡처없음"
            log.info("%-12s %-10s %-10s %s%s", kind, "보낼수있음" if ok else "낡음", _cap,
                     os.path.relpath(md, HERE), ("  (%s)" % why) if why else "")
        log.info("")
        log.info(quota_note(0))
        return 0

    md_path, meta_path = find_report(args.kind, args.session)
    if not md_path:
        log.error("[%s] 리포트 파일이 없다 — 아직 작성되지 않았다(정상일 수 있음).", args.kind)
        return 1
    md = _read(md_path)
    if not md.strip():
        log.error("[%s] 파일이 비어 있다: %s", args.kind, md_path)
        return 1

    ok, why, asof = freshness(args.kind, md_path, meta_path)
    if not ok:
        if not args.force:
            log.error("[%s] 낡은 리포트라 보내지 않는다 — %s", args.kind, why)
            log.error("  파일: %s", os.path.relpath(md_path, HERE))
            log.error("  정말 보내려면 --force (권장하지 않음).")
            return 1
        log.warning("[%s] 신선도 가드를 --force 로 넘긴다 — %s", args.kind, why)

    sha = hashlib.sha256(md.encode("utf-8")).hexdigest()[:16]
    key = "%s|%s" % (args.kind, asof or date.today().strftime("%Y-%m-%d"))
    prev = _sent_index().get(key)
    if prev and prev.get("sha") == sha and not args.force:
        log.info("[%s] 이미 같은 내용으로 보냈다(%s) — 건너뛴다.", args.kind, prev.get("sent_at"))
        print("REPORT_MAIL=skipped:already_sent")
        return 0

    # ★★카이로스 실시간 캡처 확인 — 이 리포트들의 존재 이유가 "아침 이후의 값"이다.
    #   캡처를 **아예 안 돌린 것**은 운영 실수라 막는다(사람이 고칠 수 있다).
    #   캡처는 돌았는데 일부 실패한 것은 현실이므로, 막지 않고 메일에 크게 표시한다.
    sess_for_cap = args.session or (os.path.dirname(md_path)
                                    if KINDS[args.kind]["scope"] == "session" else None)
    cs = capture_status(args.kind, sess_for_cap)
    if not cs["present"]:
        if not (args.no_capture_check or args.force):
            log.error("[%s] 카이로스 캡처가 없다 — 아침 자료만으로는 보내지 않는다.", args.kind)
            log.error("  먼저: python hts_capture_collect.py --phase %s", args.kind)
            log.error("  (캡처가 정말 불가능하면 --no-capture-check 로 보낼 수 있지만,")
            log.error("   그 메일에는 '실시간 캡처 없음' 경고가 크게 붙는다.)")
            print("REPORT_MAIL=blocked:no_capture")
            return 1
        log.warning("[%s] 캡처 없이 보낸다 — 메일에 경고 배너가 붙는다.", args.kind)
    elif cs["n_ok"] < cs["n_req"]:
        log.warning("[%s] 캡처 %d/%d — 실패분은 '확인 불가'로 메일에 표시된다: %s",
                    args.kind, cs["n_ok"], cs["n_req"], ", ".join(cs["failed"][:4]))
    else:
        log.info("[%s] 캡처 %d/%d ok (%s분 전): %s", args.kind, cs["n_ok"], cs["n_req"],
                 cs["age_min"], ", ".join(cs["screens"][:6]))

    subject = KINDS[args.kind]["subject"] % (asof or date.today().strftime("%Y-%m-%d"))
    html = render(args.kind, md, asof, capture_html=render_capture_block(cs, args.kind))

    # 미리보기는 항상 남긴다(발송 전 눈으로 확인할 수 있게)
    prev_path = os.path.join(os.path.dirname(md_path),
                             "report_mail_%s.html" % args.kind)
    try:
        with open(prev_path, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass

    try:
        import research_agent as ra
        cfg = ra.load_mail_config()
        n_to = len(ra._mail_recipients(cfg))
    except Exception as e:
        log.error("메일 설정을 못 읽었다: %s", e)
        return 1
    if not n_to:
        log.error("수신자(mail_config 의 to)가 없다.")
        return 1

    log.info("[%s] %s | 수신자 %d명 | 본문 %sB", args.kind, subject, n_to,
             format(len(html.encode("utf-8")), ","))
    log.info("  미리보기: %s", os.path.relpath(prev_path, HERE))
    log.info("  %s", quota_note(n_to))

    if not args.send:
        log.info("미리보기만 만들었다. 실제로 보내려면 --send 를 붙여라.")
        print("REPORT_MAIL=preview")
        return 0

    ok2, msg = ra.send_email_appscript(subject, html, cfg=cfg)
    if not ok2:
        log.error("발송 실패: %s", msg)
        print("REPORT_MAIL=failed:%s" % str(msg)[:80])
        return 1
    _mark_sent(key, sha, n_to)
    log.info("발송 완료: %s", msg)
    # ★전야는 발송 성공 직후 원장에 적재한다(v11.21) — 23시 프롬프트에 별도 명령이 없어도
    #   '발송된 콜'이 반드시 원장에 남는다. 위치가 발송 **뒤**인 이유(적대 검증):
    #   미리보기·신선도 차단된 낡은 콜이 운용 콜을 supersede 하면 안 된다.
    #   비치명 — 적재 실패가 이미 성공한 발송 결과를 바꾸지 않는다. 아침 배선은 보정용으로 유지(멱등).
    if args.kind == "night":
        try:
            import night_track
            _did, _why = night_track.ingest()
            log.info("전야 원장 적재: %s", _why)
        except Exception as _e:
            log.warning("전야 원장 적재 실패(발송은 완료 — 아침 배선이 보정): %s", _e)
    print("REPORT_MAIL=success:%s:%d" % (args.kind, n_to))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
