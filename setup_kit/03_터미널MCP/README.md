# terminal-mcp (v2.0.0)

Claude(데스크톱 앱 / Cowork / 일반 채팅)에서 **터미널 명령 실행 + 로그 분석**을 할 수 있게 해주는 MCP 서버.
의존성이 없는(zero-dependency) Node.js 서버라 앱 내장 Node 런타임으로 바로 실행됩니다.

## 도구 (tools)
- `run_command` — 셸 명령 실행. **`wait_ms`(기본 90초)** 안에 끝나면 결과(stdout/stderr/exit) 반환. 안 끝나면 **죽이지 않고** `job_id`를 돌려줌.
- `wait_job` — 실행 중인 명령을 **더 기다림**.
- `cancel_job` — 실행 중인 명령을 **중단**. `force=false`=인터럽트(Ctrl-C류), `force=true`=프로세스 트리 강제종료.
- `list_jobs` — 실행 중/최근 완료된 명령 목록.
- `read_file` — 텍스트/로그 읽기. `tail_lines`·`head_lines`·`grep`(정규식) 지원.
- `list_directory` — 디렉터리 항목을 종류/크기/수정시각과 함께 나열.

### 오래 걸리는 작업 흐름
1. `run_command` → 90초 안에 안 끝나면 `job-N`과 함께 "STILL RUNNING" 신호.
2. Claude가 상황 보고 판단: **더 기다리면** `wait_job(job-N)`, **멈추려면** `cancel_job(job-N)`.
3. 90초 넘는 대기 동안 progress 알림을 보내 클라이언트 타임아웃을 방지.
4. 절대 안전캡(`hard_kill_ms`, 기본 30분)이 지나면 자동 강제종료(고아 프로세스 방지, 0=비활성).

## 설치
1. Claude Desktop 앱 → **Settings → Extensions**
2. **Install Extension** → 바탕화면 `terminal-mcp.mcpb` 선택
3. 설정에서 기본 작업폴더 / 셸 / 대기시간 / 안전캡 / 정책파일 조정

데스크톱 앱에 설치되므로 **일반 채팅과 Cowork 양쪽**에서 쓸 수 있습니다.

## 자동 승인 (무인 실행 / Cowork)
자동 승인은 **서버가 아니라 Claude 앱의 권한 설정**에서 켭니다:
- 도구 실행 시 뜨는 권한 창에서 **"Allow always"**(항상 허용)를 누르면 규칙이 저장돼 그 도구는 이후 안 묻습니다.
- Cowork 예약/자동 실행은 이 "항상 허용" 규칙(`~/.claude/settings.json`의 allow 규칙)을 **상속**합니다.
- 권한 단위는 "특정 대화"가 아니라 **도구 단위(전역)** 입니다. 즉 한 번 허용하면 채팅·Cowork 모두 안 묻습니다.

> ⚠️ 자동 승인을 켜면 사람 확인 단계가 사라집니다. 아래 **정책 파일**로 서버 측 안전선을 꼭 설정하세요.

## 안전 정책 파일 (가드레일 / 킬스위치)
무인 모드의 안전장치. 확장 설정의 **Safety policy file**에 JSON 경로를 지정하면, `run_command`가 명령마다 정책을 다시 읽어 검사합니다. (`policy.example.json` 참고)

| 키 | 의미 |
|----|------|
| `block` | `true`면 모든 명령 거부(마스터 킬스위치) |
| `deny_patterns` | 매칭되는 명령 거부 (위험·파괴 명령) |
| `allow_patterns` | 채우면 화이트리스트 모드: 매칭되는 명령만 허용 |
| `allowed_cwds` | 채우면 지정 폴더 하위에서만 실행 허용 |

예) stock_research 폴더에서 python/git만 무인 허용:
```json
{ "allow_patterns": ["^python ", "^py ", "^git ", "^Get-"],
  "allowed_cwds": ["C:\\Users\\USER\\Desktop\\stock_research"],
  "deny_patterns": ["Remove-Item.*-Recurse.*-Force", "\\brm\\b\\s+-rf\\b"] }
```

## 설정 (user_config)
| 키 | 설명 | 기본값 |
|----|------|--------|
| `default_cwd` | 명령 기본 실행 폴더 | 홈 폴더 |
| `default_shell` | powershell / cmd / bash | powershell |
| `default_wait_ms` | "실행 중" 신호까지 대기 시간 | 90000 |
| `hard_kill_ms` | 절대 안전캡(강제종료), 0=비활성 | 1800000 |
| `policy_file` | 안전 정책 JSON 경로 | (없음) |

## 수정 후 재패킹
`server/index.js`를 고친 뒤:
```powershell
npx --yes @anthropic-ai/mcpb pack "C:\Users\USER\Desktop\terminal-mcp-src" "C:\Users\USER\Desktop\terminal-mcp.mcpb"
```
그리고 앱에서 확장 제거 후 새 `.mcpb`로 다시 설치.

## 기술 메모
- PowerShell은 `-EncodedCommand`(UTF-16LE base64)로 전달 → 따옴표/escape 문제 회피. `$ProgressPreference` 억제 + UTF-8 강제.
- stderr의 CLIXML은 평문으로 복원(한글 에러 깨짐 방지).
- Windows에서 `cancel_job`은 `taskkill /T`(트리)로 종료. 진짜 SIGINT/KeyboardInterrupt를 백그라운드 프로세스에 전달하는 건 Windows 한계로 보장되지 않으므로, 긴 작업은 스크립트가 주기적으로 체크포인트하도록 짜는 걸 권장.
- 출력 상한 1MB/스트림.
