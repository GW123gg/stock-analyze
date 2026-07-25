# -*- coding: utf-8 -*-
"""
email_charts.py — 이메일 안전 시각자료(차트) 렌더러 [v10.0 신규]

[왜] 리포트가 숫자 나열이라 읽기 어렵다(2026-07-25 사용자 지적). 그림을 넣고 싶지만
  이메일 클라이언트(Gmail 웹·앱, Outlook)는 <script>·<svg>·<canvas>·외부이미지·data:URI 를
  대부분 차단한다. **살아남는 유일한 기법이 table + 인라인 CSS 막대**다 — 이 모듈은 그것만 쓴다.

[설계 원칙]
  - **순수함수**(IO·부작용 0, import 시 stdout 무접촉) — common.py 규약과 동일. 하네스로 검증.
  - **결측이면 그리지 않는다**: 입력이 없거나 이상하면 '' 반환. 깨진 차트를 내보내느니 생략한다.
  - **수치 텍스트 병기 의무**: 색·막대가 지워지는 클라이언트에서도 정보 손실 0(접근성).
  - **색맹 배려**: 상승/하락을 빨강/파랑으로(한국 관행이자 적록색맹에서도 구분됨). 회색=중립.
  - 이모지 금지(BMP 기호만), 폭은 %로만(모바일 반응형), Outlook 대비 bgcolor 속성 병기.

[제공]
  prob_bar()        시장 방향 확률 100% 누적 막대(상승/횡보/하락)
  range_gauge()     지수 레벨 게이지(지지 — 현재 — 저항 위치)
  compare_bars()    항목 비교 가로 막대(픽 확신도 등)
  rr_bar()          손절 — 진입 — 목표 리스크리워드 막대
  sparkbars()       추세 스파크라인(세로 막대열)
  render_chart_fence()  분석가가 리포트에 쓴 ```chart 블록 → 위 차트 HTML
"""

# 색: 상승=적, 하락=청(한국 관행), 중립=회. 적/청은 적록색맹에서도 구분된다.
C_UP = "#d94f4f"
C_DOWN = "#3b6fd4"
C_FLAT = "#9aa7bd"
C_TRACK = "#e6eaf1"
C_INK = "#1f2a44"
C_SUB = "#56607a"
C_BAR = "#4a72b8"

_MAX_ROWS = 12          # 한 차트의 최대 행(메일 길이 폭주 방지)
_MAX_SPARK = 40         # 스파크라인 최대 포인트


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _num(v):
    """숫자로 바꿔 반환, 실패하면 None(결측 처리용)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(str(v).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):    # NaN/inf 방어
        return None
    return f


def _title(text) -> str:
    if not text:
        return ""
    return (f'<div style="font-size:12px;font-weight:800;color:{C_INK};'
            f'margin:14px 0 6px;">{_esc(text)}</div>')


def _caption(text) -> str:
    if not text:
        return ""
    return (f'<div style="font-size:11px;color:{C_SUB};margin:5px 0 0;'
            f'line-height:1.5;">{_esc(text)}</div>')


def prob_bar(prob_up, prob_flat, prob_down, title="", caption="") -> str:
    """시장 방향 확률 3종 → 100% 누적 가로 막대. 합이 0이면 '' (결측 생략)."""
    u, f, d = _num(prob_up), _num(prob_flat), _num(prob_down)
    if u is None or f is None or d is None:
        return ""
    vals = [u, f, d]
    if any(v < 0 for v in vals):
        return ""
    total = sum(vals)
    if total <= 0:
        return ""
    # 0~1 확률이든 0~100 퍼센트든 받아들인다(합으로 정규화).
    pcts = [round(v / total * 100.0) for v in vals]
    pcts[2] = max(0, 100 - pcts[0] - pcts[1])          # 반올림 오차를 마지막 칸이 흡수
    labels = ("상승", "횡보", "하락")
    colors = (C_UP, C_FLAT, C_DOWN)
    cells = []
    for lb, pc, col in zip(labels, pcts, colors):
        if pc <= 0:
            continue
        # 좁은 칸(12% 미만)엔 글자를 넣지 않는다(깨짐 방지) — 아래 범례가 수치를 보증한다.
        inner = (f'{lb} {pc}%' if pc >= 12 else "")
        cells.append(
            f'<td width="{pc}%" bgcolor="{col}" style="background:{col};width:{pc}%;'
            f'padding:6px 2px;text-align:center;font-size:11px;font-weight:700;'
            f'color:#ffffff;white-space:nowrap;">{inner}</td>')
    if not cells:
        return ""
    legend = " &nbsp; ".join(
        f'<span style="color:{col};font-weight:800;">■</span> {lb} {pc}%'
        for lb, pc, col in zip(labels, pcts, colors))
    return (
        _title(title) +
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;table-layout:fixed;width:100%;'
        f'border-radius:6px;overflow:hidden;"><tr>{"".join(cells)}</tr></table>'
        f'<div style="font-size:11px;color:{C_SUB};margin:5px 0 0;">{legend}</div>' +
        _caption(caption))


def range_gauge(low, current, high, low_label="지지", high_label="저항",
                title="", caption="") -> str:
    """지지—현재—저항 위치 게이지. low<high 이고 셋 다 숫자일 때만 그린다."""
    lo, cur, hi = _num(low), _num(current), _num(high)
    if lo is None or cur is None or hi is None or hi <= lo:
        return ""
    ratio = (cur - lo) / (hi - lo)
    ratio = min(1.0, max(0.0, ratio))                   # 범위 밖이면 끝에 붙인다
    left = int(round(ratio * 100))
    left = min(97, max(0, left))                        # 마커 자리 확보
    right = max(0, 100 - left - 3)

    def _fmt(v):
        return f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 10000 else f"{v:,.0f}"

    return (
        _title(title) +
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;table-layout:fixed;width:100%;"><tr>'
        f'<td width="{left}%" bgcolor="{C_TRACK}" style="background:{C_TRACK};'
        f'width:{left}%;height:12px;font-size:1px;line-height:12px;">&nbsp;</td>'
        f'<td width="3%" bgcolor="{C_INK}" style="background:{C_INK};width:3%;'
        f'height:12px;font-size:1px;line-height:12px;">&nbsp;</td>'
        f'<td width="{right}%" bgcolor="{C_TRACK}" style="background:{C_TRACK};'
        f'width:{right}%;height:12px;font-size:1px;line-height:12px;">&nbsp;</td>'
        f'</tr></table>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="width:100%;margin:4px 0 0;"><tr>'
        f'<td style="font-size:11px;color:{C_SUB};text-align:left;">'
        f'{_esc(low_label)} {_fmt(lo)}</td>'
        f'<td style="font-size:11px;color:{C_INK};text-align:center;font-weight:800;">'
        f'현재 {_fmt(cur)}</td>'
        f'<td style="font-size:11px;color:{C_SUB};text-align:right;">'
        f'{_esc(high_label)} {_fmt(hi)}</td>'
        f'</tr></table>' + _caption(caption))


def compare_bars(items, title="", caption="", value_suffix="", color=C_BAR,
                 axis_max=None) -> str:
    """[(라벨, 값), ...] → 가로 비교 막대. 빈 입력이면 ''.

    axis_max: 축 상한을 '고정'한다(예: 확신도는 스키마 상한 0.8).
      None 이면 최대값 기준 상대 폭 — 값들이 촘촘할 때(예: 확신도 0.42~0.50) **차이를 과장**한다.
      실측(2026-07-25 조사): 픽 확신도 (max-min)이 0.05~0.25(중앙값 0.13)라 상대 스케일이면
      0.42가 0.50 대비 84% 막대로 보여 '큰 차이'로 오독된다 → 고정 축이 정직하다.
    값은 절대값으로 폭을 잡고 부호는 텍스트로 보인다(음수 혼재 시 폭 비교가 무의미하므로).
    """
    rows = []
    for it in (items or []):
        try:
            label, raw = it[0], it[1]
        except (TypeError, IndexError):
            continue
        v = _num(raw)
        if v is None:
            continue
        rows.append((str(label), v))
    if not rows:
        return ""
    # 상한 초과분은 자르되 **조용히 자르지 않는다** — 몇 개를 생략했는지 캡션에 밝힌다
    # (침묵 절단은 '전부 보여줬다'는 오해를 만든다).
    dropped = max(0, len(rows) - _MAX_ROWS)
    rows = rows[:_MAX_ROWS]
    if dropped:
        _more = f"외 {dropped}개 생략(상위 {_MAX_ROWS}개만 표시)"
        caption = f"{caption} · {_more}" if caption else _more
    _fixed = _num(axis_max)
    if _fixed is not None and _fixed > 0:
        peak = _fixed
        _ax = f"축 0~{_fixed:g}{value_suffix} 고정"
        caption = f"{caption} · {_ax}" if caption else _ax
    else:
        peak = max(abs(v) for _, v in rows) or 1.0
    out = []
    for label, v in rows:
        pc = int(round(abs(v) / peak * 100))
        pc = min(100, max(2, pc))                       # 0%면 막대가 안 보이니 최소 2%
        rest = 100 - pc
        col = color
        txt = f"{v:g}{value_suffix}"
        out.append(
            f'<tr>'
            f'<td width="32%" style="width:32%;padding:3px 8px 3px 0;font-size:12px;'
            f'color:{C_INK};white-space:nowrap;overflow:hidden;">{_esc(label)}</td>'
            f'<td width="52%" style="width:52%;padding:3px 0;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;table-layout:fixed;width:100%;"><tr>'
            f'<td width="{pc}%" bgcolor="{col}" style="background:{col};width:{pc}%;'
            f'height:11px;font-size:1px;line-height:11px;border-radius:3px;">&nbsp;</td>'
            f'<td width="{rest}%" style="width:{rest}%;font-size:1px;line-height:11px;">'
            f'&nbsp;</td></tr></table></td>'
            f'<td width="16%" style="width:16%;padding:3px 0 3px 8px;font-size:12px;'
            f'color:{C_SUB};text-align:right;white-space:nowrap;">{_esc(txt)}</td>'
            f'</tr>')
    return (_title(title) +
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;width:100%;">{"".join(out)}</table>' +
            _caption(caption))


def rr_bar(entry, target, stop, title="", caption="") -> str:
    """손절 — 진입 — 목표 를 한 줄로. 하방(손절~진입)=청, 상방(진입~목표)=적."""
    e, t, s = _num(entry), _num(target), _num(stop)
    if e is None or t is None or s is None:
        return ""
    if not (s < e < t):                                  # 숏이거나 값이 뒤집히면 생략
        return ""
    span = t - s
    if span <= 0:
        return ""
    down_pc = int(round((e - s) / span * 100))
    down_pc = min(97, max(3, down_pc))
    up_pc = 100 - down_pc

    def _fmt(v):
        return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:g}"

    rr = (t - e) / (e - s) if (e - s) > 0 else None
    rr_txt = f" &nbsp;·&nbsp; RR {rr:.1f}" if rr else ""
    return (
        _title(title) +
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;table-layout:fixed;width:100%;'
        f'border-radius:5px;overflow:hidden;"><tr>'
        f'<td width="{down_pc}%" bgcolor="{C_DOWN}" style="background:{C_DOWN};'
        f'width:{down_pc}%;height:10px;font-size:1px;line-height:10px;">&nbsp;</td>'
        f'<td width="{up_pc}%" bgcolor="{C_UP}" style="background:{C_UP};'
        f'width:{up_pc}%;height:10px;font-size:1px;line-height:10px;">&nbsp;</td>'
        f'</tr></table>'
        f'<div style="font-size:11px;color:{C_SUB};margin:4px 0 0;">'
        f'손절 {_fmt(s)} &nbsp;|&nbsp; <span style="color:{C_INK};font-weight:800;">'
        f'진입 {_fmt(e)}</span> &nbsp;|&nbsp; 목표 {_fmt(t)}{rr_txt}</div>' +
        _caption(caption))


def sparkbars(series, title="", caption="", color=C_BAR) -> str:
    """숫자 시계열 → 세로 막대열(추세 스파크라인). 값 2개 미만이면 ''."""
    vals = [_num(v) for v in (series or [])]
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return ""
    vals = vals[-_MAX_SPARK:]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    w = round(100.0 / len(vals), 3)
    cells = []
    for i, v in enumerate(vals):
        h = 4 + int(round((v - lo) / rng * 30))          # 4~34px
        # 마지막 막대는 강조(현재 시점)
        col = C_INK if i == len(vals) - 1 else color
        cells.append(
            f'<td width="{w}%" valign="bottom" style="width:{w}%;padding:0 1px;'
            f'vertical-align:bottom;">'
            f'<div style="height:{h}px;background:{col};font-size:1px;'
            f'line-height:1px;">&nbsp;</div></td>')

    def _fmt(v):
        return f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 10000 else f"{v:,.0f}"

    return (_title(title) +
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;table-layout:fixed;width:100%;height:36px;">'
            f'<tr>{"".join(cells)}</tr></table>'
            f'<div style="font-size:11px;color:{C_SUB};margin:4px 0 0;">'
            f'저 {_fmt(lo)} &nbsp;~&nbsp; 고 {_fmt(hi)} &nbsp;·&nbsp; '
            f'<span style="color:{C_INK};font-weight:700;">현재 {_fmt(vals[-1])}</span>'
            f' &nbsp;({len(vals)}개 구간)</div>' + _caption(caption))


# =====================================================================
# 분석가가 리포트 마크다운에 쓰는 ```chart 블록 파서
#   예)
#     ```chart
#     type: bar
#     title: 픽별 확신도
#     data: 셀트리온=0.50, 삼성바이오로직스=0.46, NAVER=0.42
#     caption: 0.5~0.6 구간이 실측 변별력이 있는 구간
#     ```
#   지원 type: bar(비교막대) / prob(확률3종) / gauge(지지-현재-저항) / rr(손절-진입-목표) / spark(추세)
# =====================================================================
def _parse_spec(text):
    """'key: value' 줄들 → dict. 파싱 실패한 줄은 무시(관대하게)."""
    spec = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        spec[k.strip().lower()] = v.strip()
    return spec


def _parse_pairs(s):
    """'A=1, B=2' 또는 'A:1, B:2' → [(A,1.0),(B,2.0)]."""
    out = []
    for chunk in (s or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        sep = "=" if "=" in chunk else (":" if ":" in chunk else None)
        if not sep:
            continue
        label, raw = chunk.split(sep, 1)
        v = _num(raw)
        if v is None:
            continue
        out.append((label.strip(), v))
    return out


def _parse_nums(s):
    out = []
    for chunk in (s or "").replace(",", " ").split():
        v = _num(chunk)
        if v is not None:
            out.append(v)
    return out


def render_chart_fence(spec_text) -> str:
    """```chart 블록 본문 → 차트 HTML. 알 수 없는 형식이면 ''(조용히 생략).

    렌더러가 HTML 을 만들므로 분석가는 HTML 을 쓸 필요가 없다(약한 모델도 안전).
    """
    spec = _parse_spec(spec_text)
    kind = (spec.get("type") or "").lower()
    title = spec.get("title", "")
    caption = spec.get("caption", "")
    try:
        if kind in ("prob", "probability", "확률"):
            pairs = dict(_parse_pairs(spec.get("data", "")))
            up = pairs.get("up", pairs.get("상승"))
            flat = pairs.get("flat", pairs.get("횡보"))
            down = pairs.get("down", pairs.get("하락"))
            return prob_bar(up, flat, down, title=title, caption=caption)
        if kind in ("gauge", "range", "게이지"):
            pairs = dict(_parse_pairs(spec.get("data", "")))
            return range_gauge(
                pairs.get("low", pairs.get("지지", pairs.get("support"))),
                pairs.get("current", pairs.get("현재", pairs.get("cur"))),
                pairs.get("high", pairs.get("저항", pairs.get("resistance"))),
                title=title, caption=caption)
        if kind in ("rr", "riskreward", "리스크리워드"):
            pairs = dict(_parse_pairs(spec.get("data", "")))
            return rr_bar(
                pairs.get("entry", pairs.get("진입")),
                pairs.get("target", pairs.get("목표")),
                pairs.get("stop", pairs.get("손절")),
                title=title, caption=caption)
        if kind in ("spark", "sparkline", "trend", "추세"):
            return sparkbars(_parse_nums(spec.get("data", "")),
                             title=title, caption=caption)
        if kind in ("bar", "bars", "compare", "막대"):
            return compare_bars(_parse_pairs(spec.get("data", "")),
                                title=title, caption=caption,
                                value_suffix=spec.get("suffix", ""))
    except Exception:
        return ""                                        # 어떤 예외도 메일을 깨지 않는다
    return ""
