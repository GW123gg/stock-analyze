/**
 * ============================================================================
 *  mail_webapp.gs ─ 한국 주식 데일리 리서치 메일 발송 웹앱   [v2, 2026-09-03]
 * ----------------------------------------------------------------------------
 *  호스트(watch_and_send.py / 웹사이트 mailer.py)가 이 웹앱 URL 로 HTTPS POST
 *  (제목/본문/수신자/비밀키)를 보내면, 구글 서버의 Apps Script 가
 *  GmailApp.sendEmail() 로 발송한다. OAuth 토큰·credentials·7일 만료·브랜딩
 *  인증이 전부 불필요하다. (유일한 인증은 최초 배포 시 권한 승인 1회)
 *
 *  ★ 본문(HTML)만 발송한다. 첨부파일은 보내지 않는다.
 *
 * ============================================================================
 *  [v2 에서 새로 생긴 것 — 인증메일 발송 한도]
 * ----------------------------------------------------------------------------
 *  웹사이트 회원가입·비밀번호 재설정에 쓰는 **인증코드 메일**에만 걸리는
 *  한도다. 아침 리서치 리포트 메일은 이 한도와 무관하다(끊기면 안 되므로).
 *
 *      · 한 주소당 하루 50통
 *      · 하루의 경계는 자정이 아니라 **아침 06:00** (아침 리포트 시각과 같다)
 *      · 같은 주소로 연속 발송은 최소 30초 간격
 *
 *  ★왜 서버(파이썬)에도 같은 한도가 있는데 여기에 또 두는가
 *    파이썬 쪽 한도는 '정상 경로'를 지킨다. 그런데 이 웹앱 URL 과 비밀키를
 *    아는 사람은 파이썬을 건너뛰고 여기로 직접 POST 할 수 있다. 그때 마지막으로
 *    막는 것이 이 코드다. **바깥에서 부를 수 있는 문은 그 문 자리에서 잠근다.**
 *    두 겹이 겹치는 것은 낭비가 아니라 설계다.
 *
 *  ★한도를 넘으면 메일을 보내지 않고 ok:false 와 사유를 돌려준다.
 *    조용히 성공한 척하지 않는다 — 그러면 호출부가 '보냈다'고 착각한다.
 *
 * ============================================================================
 *  [인증코드 자체의 폐기]
 * ----------------------------------------------------------------------------
 *  코드를 만들고 검사하고 버리는 일은 **이 파일이 아니라 웹사이트(auth.py)**
 *  가 한다. 이 웹앱은 '보내는 우체국'이지 '코드를 아는 사람'이 아니다.
 *  auth.py 의 동작(2026-09-03 확인):
 *      · 인증 성공 → 그 즉시 code = None (재사용 불가)
 *      · 새 코드를 발급하면 이전 코드는 그 자리에서 덮어써져 무효
 *      · 만료(기본 10분) 또는 시도 5회 초과 → code = None
 *  즉 "쓴 코드는 바로 폐기"는 이미 지켜지고 있다. 이 파일에 코드를 들고 오면
 *  오히려 비밀이 한 곳 더 늘어난다 — 그래서 여기서는 다루지 않는다.
 *
 * ============================================================================
 *  [배포 방법 — 처음 1회만]
 * ----------------------------------------------------------------------------
 *  1) https://script.google.com 접속 → "새 프로젝트"
 *  2) 기본 코드(Code.gs)를 모두 지우고, 이 파일 내용을 통째로 붙여넣기
 *  3) 아래 SECRET_TOKEN 값을 "본인만 아는 길고 무작위한 문자열"로 바꾼다.
 *       예) "k7Qx_92aFh3..." 처럼 32자 이상 권장. (워처 설정과 똑같이 맞춘다)
 *  4) 우측 상단 "배포" → "새 배포"
 *       - 유형(톱니바퀴) : "웹 앱(Web app)"
 *       - 실행 주체      : "나(Me / 본인 계정)"
 *       - 액세스 권한    : "모든 사용자(Anyone)"
 *         ※ 외부에서 구글 로그인 없이 POST 하려면 Anyone 이어야 한다.
 *           보안은 SECRET_TOKEN 이 건다 — URL 이 노출돼도 비밀키 없이는 못 보낸다.
 *  5) 권한 승인 창 → 본인 계정 → "고급" → "(프로젝트명)(으)로 이동" → 허용
 *  6) 표시되는 "웹 앱 URL"(https://script.google.com/macros/s/......../exec) 복사
 *  7) stock_research 폴더에 appscript_config.txt 를 만들고:
 *       appscript_url    = (복사한 웹 앱 URL)
 *       appscript_secret = (3번에서 정한 SECRET_TOKEN 과 똑같은 값)
 *  8) 배포 확인: 브라우저로 웹 앱 URL 을 열면(GET)
 *       {"ok":true,"status":"running", ...} 가 보이면 정상.
 *
 *  [코드 수정 시 재배포]
 *    "배포" → "배포 관리" → 기존 배포의 연필(편집) → 버전 "새 버전" → "배포".
 *    ※ "새 배포"로 또 만들면 URL 이 새로 생긴다. 반드시 "배포 관리 → 편집".
 * ============================================================================
 */

// ★★★ 반드시 본인만 아는 길고 무작위한 문자열로 변경하세요 (32자 이상 권장) ★★★
// 이 값은 appscript_config.txt 의 appscript_secret 과 정확히 일치해야 합니다.
// 영문 대소문자+숫자+밑줄만 쓰는 것을 권장합니다 (특수문자/한글/공백 피하기).
var SECRET_TOKEN = "CHANGE_ME_aZ09Kq7XR2mWvR8sLtE4hNbY6cD3fG1_replace_this";

// ── 인증메일 한도 ───────────────────────────────────────────────────────────
var VERIFY_DAILY_MAX    = 50;   // 한 주소당 하루 몇 통까지
var VERIFY_COOLDOWN_SEC = 30;   // 같은 주소로 다시 보내기까지 최소 간격(초)
var VERIFY_RESET_HOUR   = 6;    // 하루의 경계(아침 6시). 자정이 아니다.
var TZ                  = "Asia/Seoul";

/**
 * POST 엔드포인트 — 호스트가 호출.
 *
 * 요청 JSON: {
 *   secret,               필수. SECRET_TOKEN 과 같아야 한다.
 *   to,                   필수. 콤마/세미콜론 구분 가능.
 *   subject, htmlBody,    필수(본문은 body 로 보내도 된다).
 *   kind                  선택. "verify" 면 인증메일 한도가 걸린다.
 * }
 *
 * ★kind 를 안 보내도 제목으로 인증메일인지 알아본다 — 호출부를 고치지 않아도
 *   한도가 걸리게 하기 위해서다(구버전 호출부와 섞여 돌 수 있다).
 */
function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return _json({ ok: false, error: "no_post_data" });
    }

    var req;
    try {
      req = JSON.parse(e.postData.contents);
    } catch (parseErr) {
      return _json({ ok: false, error: "invalid_json" });
    }

    // ── 비밀키 검증 (URL 이 노출돼도 비밀키 없으면 발송 불가) ──
    if (!req.secret || !_safeEq(String(req.secret), SECRET_TOKEN)) {
      return _json({ ok: false, error: "unauthorized" });
    }

    var to = req.to;
    var subject = req.subject || "[데일리 리서치] 리포트";
    var htmlBody = req.htmlBody || req.body || "";

    if (!to)       return _json({ ok: false, error: "missing_to" });
    if (!htmlBody) return _json({ ok: false, error: "missing_body" });

    // ── 수신자 정리 ──
    var recipients = String(to)
      .split(/[,;]/)
      .map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0; });

    if (recipients.length === 0) {
      return _json({ ok: false, error: "no_recipients" });
    }

    // ── 인증메일이면 한도를 건다 ──
    //    ★리포트 메일에는 걸지 않는다. 아침 발송이 한도에 걸려 끊기면
    //      그날 리서치가 통째로 사라지고, 그건 스팸 방지보다 훨씬 큰 손해다.
    var isVerify = _isVerifyMail(req, subject);
    if (isVerify) {
      // 인증메일은 원래 한 사람에게만 간다. 여러 명이면 요청 자체가 이상하다.
      if (recipients.length > 1) {
        return _json({ ok: false, error: "verify_multi_recipient",
                       message: "인증메일은 한 번에 한 사람에게만 보냅니다." });
      }
      var gate = _verifyGate(recipients[0]);
      if (!gate.ok) {
        return _json({ ok: false, error: gate.error, message: gate.message,
                       retry_after_sec: gate.retry_after_sec || 0 });
      }
    }

    // ── plain text fallback (HTML 미지원 클라이언트용) ──
    var plain = _stripHtml(htmlBody);
    if (!plain) plain = "리포트를 확인하세요. (HTML 본문)";

    // ── 발송 ──
    GmailApp.sendEmail(recipients.join(","), subject, plain, {
      htmlBody: htmlBody,
      name: "리서치 에이전트"
    });

    // ★보낸 뒤에만 기록한다. 발송이 실패했는데 한도를 깎으면
    //   쓰지도 못한 횟수를 잃는다.
    if (isVerify) _verifyMark(recipients[0]);

    return _json({
      ok: true,
      to: recipients.join(","),
      recipients: recipients.length,
      subject: subject,
      kind: isVerify ? "verify" : "report",
      ts: new Date().toISOString()
    });

  } catch (err) {
    // 어떤 예외도 500 대신 ok:false JSON 으로 반환
    return _json({ ok: false, error: String(err) });
  }
}

/**
 * GET 엔드포인트 — 배포 확인용 헬스체크.
 * ★한도 설정값도 함께 돌려준다. 재배포를 잊어서 옛 버전이 도는 경우를
 *   호스트에서 눈으로 확인할 수 있다.
 */
function doGet(e) {
  return _json({
    ok: true,
    status: "running",
    version: "v2-2026-09-03",
    verify_limits: {
      daily_max: VERIFY_DAILY_MAX,
      cooldown_sec: VERIFY_COOLDOWN_SEC,
      reset_hour: VERIFY_RESET_HOUR
    },
    ts: new Date().toISOString()
  });
}

// ────────────────────────────────────────────────────────────────────────────
//  인증메일 한도
// ────────────────────────────────────────────────────────────────────────────

/** 이 요청이 인증코드 메일인가. kind 가 없으면 제목으로 알아본다. */
function _isVerifyMail(req, subject) {
  if (req.kind) return String(req.kind).toLowerCase() === "verify";
  return /인증|verify|verification|비밀번호\s*재설정/i.test(String(subject || ""));
}

/**
 * 이 시각이 속한 '하루'의 이름. 06:00 이전은 전날로 친다.
 *
 * ★자정 기준이면 밤 11시에 한도를 다 쓴 사람은 한 시간만 기다리면 되고
 *   새벽 1시에 다 쓴 사람은 23시간을 기다린다 — 같은 규칙인데 체감이 24배
 *   다르다. 경계를 하루의 시작(06:00)에 두면 그 왜곡이 없다.
 */
function _dayKey(date) {
  var shifted = new Date(date.getTime() - VERIFY_RESET_HOUR * 3600 * 1000);
  return Utilities.formatDate(shifted, TZ, "yyyy-MM-dd");
}

/** 주소를 그대로 저장하지 않는다 — 해시로 줄여 쓴다(속성에 주소가 안 남게). */
function _addrKey(email) {
  var raw = Utilities.computeDigest(
    Utilities.DigestAlgorithm.SHA_256,
    "verify:" + String(email || "").trim().toLowerCase(),
    Utilities.Charset.UTF_8);
  var hex = raw.map(function (b) {
    return ("0" + (b & 0xFF).toString(16)).slice(-2);
  }).join("");
  return "v_" + hex.substring(0, 24);
}

/**
 * 보내도 되는지 판정. 반환 {ok, error, message, retry_after_sec}
 * ★기록은 여기서 하지 않는다 — 실제로 보낸 뒤에 _verifyMark 가 한다.
 */
function _verifyGate(email) {
  var props = PropertiesService.getScriptProperties();
  var k = _addrKey(email);
  var now = new Date();
  var rec = {};
  try {
    rec = JSON.parse(props.getProperty(k) || "{}");
  } catch (err) {
    rec = {};
  }

  var last = Number(rec.last || 0);
  if (last > 0) {
    var gapSec = Math.floor((now.getTime() - last) / 1000);
    if (gapSec < VERIFY_COOLDOWN_SEC) {
      var left = VERIFY_COOLDOWN_SEC - gapSec;
      return { ok: false, error: "verify_cooldown",
               retry_after_sec: left,
               message: "인증메일은 " + VERIFY_COOLDOWN_SEC +
                        "초에 한 번만 보낼 수 있습니다. " + left + "초 후에 다시 시도해 주세요." };
    }
  }

  var day = _dayKey(now);
  if (rec.day === day && Number(rec.count || 0) >= VERIFY_DAILY_MAX) {
    return { ok: false, error: "verify_daily_limit",
             message: "인증메일 발송 한도(" + VERIFY_DAILY_MAX + "회)를 다 썼습니다. " +
                      "매일 아침 " + VERIFY_RESET_HOUR + ":00 에 초기화됩니다." };
  }
  return { ok: true };
}

/** 실제로 보낸 뒤 1회 기록. */
function _verifyMark(email) {
  var props = PropertiesService.getScriptProperties();
  var k = _addrKey(email);
  var now = new Date();
  var day = _dayKey(now);
  var rec = {};
  try {
    rec = JSON.parse(props.getProperty(k) || "{}");
  } catch (err) {
    rec = {};
  }
  if (rec.day !== day) rec = { day: day, count: 0 };
  rec.count = Number(rec.count || 0) + 1;
  rec.last = now.getTime();
  props.setProperty(k, JSON.stringify(rec));
}

/**
 * 오래된 기록 청소 — 스크립트 속성은 최대 500KB 라 무한정 쌓이면 터진다.
 *
 * ★설치 방법(선택): 왼쪽 "트리거" → "트리거 추가" →
 *   실행할 함수 `cleanupVerifyRecords` / 시간 기반 / 일 단위 타이머 / 새벽 3~4시.
 *   안 걸어도 당장은 문제없지만, 서로 다른 주소가 수천 개 쌓이면 필요하다.
 */
function cleanupVerifyRecords() {
  var props = PropertiesService.getScriptProperties();
  var all = props.getProperties();
  var cutoff = new Date().getTime() - 7 * 86400 * 1000;   // 7일
  var removed = 0;
  for (var k in all) {
    if (k.indexOf("v_") !== 0) continue;
    var rec = {};
    try { rec = JSON.parse(all[k] || "{}"); } catch (err) { rec = {}; }
    if (Number(rec.last || 0) < cutoff) {
      props.deleteProperty(k);
      removed += 1;
    }
  }
  Logger.log("오래된 인증 기록 " + removed + "건 삭제");
  return removed;
}

// ────────────────────────────────────────────────────────────────────────────
//  헬퍼
// ────────────────────────────────────────────────────────────────────────────

/**
 * 비밀키 비교. 길이가 같을 때 **끝까지 다 보고** 결과를 낸다.
 *
 * ★`a === b` 는 다른 글자를 만나는 순간 멈춘다. 그 미세한 시간차를 수천 번
 *   재면 비밀키를 한 글자씩 알아낼 수 있다(타이밍 공격). Apps Script 는
 *   네트워크 지연이 커서 실제로 성공하기는 어렵지만, 비교를 이렇게 쓰는 데
 *   드는 비용이 0 이므로 그냥 안전한 쪽으로 쓴다.
 */
function _safeEq(a, b) {
  a = String(a || "");
  b = String(b || "");
  if (a.length === 0 || b.length === 0) return false;
  if (a.length !== b.length) return false;
  var diff = 0;
  for (var i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

/** JSON 응답 헬퍼 */
function _json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

/** HTML 태그를 대충 제거해 plain text fallback 생성 */
function _stripHtml(html) {
  if (!html) return "";
  return String(html)
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p>/gi, "\n")
    .replace(/<\/tr>/gi, "\n")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/[ \t]+/g, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim()
    .substring(0, 8000);
}

// ────────────────────────────────────────────────────────────────────────────
//  자가 점검 — 배포 전에 에디터에서 이 함수를 한 번 실행해 보세요.
//  (메일을 보내지 않습니다. 한도 계산만 확인합니다.)
// ────────────────────────────────────────────────────────────────────────────
function selfTest() {
  var out = [];
  function chk(name, cond) { out.push((cond ? "OK   " : "실패 ") + name); }

  // 06시 경계
  function at(s) { return new Date(s); }
  chk("09-03 05:59 는 전날(09-02)", _dayKey(at("2026-09-03T05:59:00+09:00")) === "2026-09-02");
  chk("09-03 06:00 은 당일(09-03)", _dayKey(at("2026-09-03T06:00:00+09:00")) === "2026-09-03");
  chk("09-04 00:01 은 아직 09-03", _dayKey(at("2026-09-04T00:01:00+09:00")) === "2026-09-03");
  chk("09-04 06:01 은 09-04",      _dayKey(at("2026-09-04T06:01:00+09:00")) === "2026-09-04");

  // 비밀키 비교
  chk("같은 값은 true",   _safeEq("abc123", "abc123") === true);
  chk("다른 값은 false",  _safeEq("abc123", "abc124") === false);
  chk("길이 다르면 false", _safeEq("abc", "abcd") === false);
  chk("빈 값은 false",    _safeEq("", "") === false);

  // 인증메일 판별
  chk("kind=verify 인식", _isVerifyMail({ kind: "verify" }, "아무 제목") === true);
  chk("kind=report 는 아님", _isVerifyMail({ kind: "report" }, "인증코드") === false);
  chk("제목으로 인식",    _isVerifyMail({}, "[데일리 리서치] 인증코드") === true);
  chk("리포트는 아님",    _isVerifyMail({}, "[데일리 리서치] 2026-09-03 아침 리포트") === false);

  // 주소 해시가 주소를 남기지 않는가
  var k = _addrKey("someone@example.com");
  chk("해시에 주소가 안 남는다", k.indexOf("someone") < 0 && k.indexOf("example") < 0);
  chk("같은 주소는 같은 키",     _addrKey("A@Example.com") === _addrKey("a@example.com"));

  var msg = out.join("\n");
  Logger.log(msg);
  return msg;
}
