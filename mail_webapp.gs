/**
 * ============================================================================
 *  mail_webapp.gs ─ 한국 주식 데일리 리서치 리포트 메일 발송 웹앱
 * ----------------------------------------------------------------------------
 *  호스트 워처(watch_and_send.py)가 이 웹앱 URL 로 HTTPS POST(제목/본문/수신자/
 *  비밀키)를 보내면, 구글 서버의 Apps Script 가 GmailApp.sendEmail() 로 메일을
 *  발송한다. OAuth 토큰·credentials·7일 만료·브랜딩 인증이 전부 불필요하다.
 *  (유일한 인증은 최초 배포 시 권한 승인 1회 — 이후 만료 없음)
 *
 *  ★ 본문(HTML)만 발송한다. 첨부파일은 보내지 않는다. (단순화)
 *
 * ============================================================================
 *  [배포 방법 — 처음 1회만]
 * ----------------------------------------------------------------------------
 *  1) https://script.google.com 접속 → "새 프로젝트"
 *  2) 기본 코드(Code.gs)를 모두 지우고, 이 파일 내용을 통째로 붙여넣기
 *  3) 아래 SECRET_TOKEN 값을 "본인만 아는 길고 무작위한 문자열"로 바꾼다.
 *       예) "k7Qx_92aFh3..." 처럼 32자 이상 권장. (이 값은 워처 설정과 똑같이 맞춘다)
 *  4) 우측 상단 "배포" → "새 배포" 클릭
 *       - 유형(톱니바퀴) : "웹 앱(Web app)" 선택
 *       - 설명          : 아무거나 (예: "stock mail v1")
 *       - 실행 주체      : "나(Me / 본인 계정)"
 *       - 액세스 권한    : "모든 사용자(Anyone)"
 *         ※ 왜 Anyone 인가? 외부(워처)에서 구글 로그인 없이 POST 하려면 Anyone 이어야 한다.
 *           대신 보안은 아래 SECRET_TOKEN 으로 건다 — 비밀키가 없으면 발송되지 않으므로
 *           URL 이 노출돼도 스팸 악용이 불가능하다. (URL+비밀키 둘 다 알아야 발송됨)
 *  5) "배포" 누르면 권한 승인 창이 뜬다 → 본인 구글 계정 선택 →
 *       "Google에서 확인하지 않은 앱" 경고가 나오면 "고급" → "(프로젝트명)(으)로 이동" →
 *       Gmail 발송 권한 "허용". (이 승인이 유일한 인증이며 만료되지 않는다)
 *  6) 배포 완료 후 표시되는 "웹 앱 URL"
 *       (https://script.google.com/macros/s/........./exec) 을 복사한다.
 *  7) 호스트의 stock_research 폴더에 appscript_config.txt 를 만들고 아래처럼 적는다:
 *       appscript_url    = (복사한 웹 앱 URL)
 *       appscript_secret = (위 3번에서 정한 SECRET_TOKEN 과 똑같은 값)
 *  8) 배포 확인: 브라우저로 웹 앱 URL 을 그냥 열면(GET) {"ok":true,"status":"running"}
 *       가 보이면 정상이다.
 *
 *  [코드 수정 시 재배포]
 *    SECRET_TOKEN 등을 바꾸면, "배포" → "배포 관리" → 기존 배포의 연필(편집) 아이콘 →
 *    버전을 "새 버전"으로 선택 → "배포". (URL 은 그대로 유지된다)
 *    ※ "새 배포"로 또 만들면 URL 이 새로 생기므로, 보통 "배포 관리 → 편집 → 새 버전"을 쓴다.
 * ============================================================================
 */

// ★★★ 반드시 본인만 아는 길고 무작위한 문자열로 변경하세요 (32자 이상 권장) ★★★
// 이 값은 appscript_config.txt 의 appscript_secret 과 정확히 일치해야 합니다.
// 영문 대소문자+숫자+밑줄만 쓰는 것을 권장합니다 (특수문자/한글/공백 피하기).
var SECRET_TOKEN = "CHANGE_ME_aZ09Kq7XR2mWvR8sLtE4hNbY6cD3fG1_replace_this";

/**
 * POST 엔드포인트 — 워처가 호출.
 * 요청 JSON: { secret, to, subject, htmlBody (또는 body) }
 * 응답 JSON: { ok: true|false, ... }
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

    // ── 비밀키 검증 (URL 노출돼도 비밀키 없으면 발송 불가) ──
    if (!req.secret || req.secret !== SECRET_TOKEN) {
      return _json({ ok: false, error: "unauthorized" });
    }

    var to = req.to;
    var subject = req.subject || "[데일리 리서치] 리포트";
    var htmlBody = req.htmlBody || req.body || "";

    if (!to) {
      return _json({ ok: false, error: "missing_to" });
    }
    if (!htmlBody) {
      return _json({ ok: false, error: "missing_body" });
    }

    // ── 수신자 콤마/세미콜론 정리 ──
    var recipients = String(to)
      .split(/[,;]/)
      .map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0; });

    if (recipients.length === 0) {
      return _json({ ok: false, error: "no_recipients" });
    }
    var toField = recipients.join(",");

    // ── plain text fallback (HTML 미지원 클라이언트용) ──
    var plain = _stripHtml(htmlBody);
    if (!plain) plain = "리포트를 확인하세요. (HTML 본문)";

    // ── 발송 ──
    GmailApp.sendEmail(toField, subject, plain, {
      htmlBody: htmlBody,
      name: "리서치 에이전트"
    });

    return _json({
      ok: true,
      to: toField,
      recipients: recipients.length,
      subject: subject,
      ts: new Date().toISOString()
    });

  } catch (err) {
    // 어떤 예외도 500 대신 ok:false JSON 으로 반환
    return _json({ ok: false, error: String(err) });
  }
}

/**
 * GET 엔드포인트 — 배포 확인용 헬스체크.
 * 브라우저로 URL 을 열면 {"ok":true,"status":"running"} 가 보이면 정상.
 */
function doGet(e) {
  return _json({ ok: true, status: "running", ts: new Date().toISOString() });
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
