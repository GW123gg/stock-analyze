# 전야 리서치(23:00) — Claude Code CLI 전환 가이드

## 왜 코워크가 아니라 CLI 인가

| 문제 | 코워크 예정작업 | Claude Code CLI + 작업 스케줄러 |
|---|---|---|
| 23시 피크타임 지연 | 대기열에 밀림 · 사람이 없어 재시도 불가 | 로컬에서 즉시 시작, 재시도도 로컬 |
| 계정 의존 | 계정 전환 시 일정이 통째로 소실 (**2026-08-13~18 3거래일 미발행 실사고**) | 일정이 이 PC 에 있음 |
| 지시문 노후 | 복사본이 낡아도 아무도 모름 (**2026-07-29 6단계 누락 사고**) | `prompts\night_research.md` 를 git 이 추적 |
| 실패 감지 | 조용히 실패 | `night_cli_status.json` + 아침 작업이 읽고 보고 |

★**아침 06:20 은 옮기지 않았다.** 실계좌 9명 발송이 걸린 핵심 경로라, 전야로 한 달쯤
검증한 뒤에 넓히는 것이 맞다. 전야는 지수 콜만 내므로 시험대로 적합하다.

## 구성 파일

| 파일 | 역할 |
|---|---|
| `prompts\night_research.md` | 실제 지시문 (git 추적 — 여기만 고치면 된다) |
| `run_night_research.cmd` | 실행 러너 (로그·상태기록 포함) |
| `night_cli_status.py` | 결과를 `night_cli_status.json` 으로 기록 |
| `logs\night_cli_YYYYMMDD.log` | 실행 로그 (append) |

## 작업 스케줄러 등록

PowerShell 을 **일반 권한**으로 열고 한 번만 실행한다.

```powershell
$act = New-ScheduledTaskAction -Execute "C:\Users\USER\Desktop\stock_research\run_night_research.cmd" `
       -WorkingDirectory "C:\Users\USER\Desktop\stock_research"
$trg = New-ScheduledTaskTrigger -Daily -At 23:00
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
       -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
       -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "StockNightResearch" -Action $act -Trigger $trg `
       -Settings $set -RunLevel Limited -Force
```

옵션 설명 — 각각 이유가 있다:

- `-StartWhenAvailable` : 23시에 PC 가 꺼져 있었으면 켜진 뒤 실행한다(놓치는 것보다 늦는 게 낫다).
- `-ExecutionTimeLimit 1시간` : 모델이 멈춰도 무한정 물고 있지 않게. **빼면 기본 3일**이다.
- `-MultipleInstances IgnoreNew` : 이전 실행이 아직 돌면 새로 띄우지 않는다(중복 발송 방지).
- `-RunLevel Limited` : 관리자 권한 불필요. 올릴 이유가 없다.

### 요일 지정 (권장)

23시 전야 리서치는 **다음 날이 거래일일 때만** 의미가 있다. 금·토 밤은 건너뛴다.

```powershell
$trg = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday,Monday,Tuesday,Wednesday,Thursday -At 23:00
Set-ScheduledTask -TaskName "StockNightResearch" -Trigger $trg
```

※ 공휴일은 요일로 못 거른다. 프롬프트가 `for_date` 를 **다음 거래일**로 잡게 지시돼 있고,
`night_track` 이 휴장일 지정을 `void` 로 기록하므로 채점이 오염되지는 않는다.

### 확인·수동 실행·해제

```powershell
Get-ScheduledTask -TaskName "StockNightResearch" | Select-Object TaskName,State
Start-ScheduledTask -TaskName "StockNightResearch"          # 지금 한 번 돌려보기
Get-ScheduledTaskInfo -TaskName "StockNightResearch"        # 마지막 실행 결과
Unregister-ScheduledTask -TaskName "StockNightResearch" -Confirm:$false   # 해제
```

시간 변경은 작업 스케줄러 GUI 에서 트리거만 고치면 된다.

## 모델 바꾸기

기본 `opus`. 인자로 바꾼다 — 작업 스케줄러 동작의 '인수 추가'에 `sonnet` 을 넣으면 된다.

```
run_night_research.cmd sonnet
```

## 배선만 시험하기 (메일 발송 없이)

```powershell
$env:NIGHT_PROMPT = "C:\Users\USER\Desktop\stock_research\_scratch\smoke_night.md"
cmd /c "C:\Users\USER\Desktop\stock_research\run_night_research.cmd haiku"
Remove-Item Env:\NIGHT_PROMPT
```

## 실패했는지 어떻게 아나

`night_cli_status.json` 의 `ok` 를 본다. **아침 작업(1단계)이 이 파일을 읽고 최종 보고에 옮긴다.**

- `rc != 0` (프로세스 실패) → `ok:false`
- `rc == 0` 인데 프롬프트가 `NIGHT_RESULT=failed` 로 자기신고 → **역시 `ok:false`**
  (모델이 "완료했다"고 말하면서 실제로는 못 한 경우를 걸러낸다)

## 권한 설계

`--permission-mode acceptEdits` + `--allowedTools "Bash(python *) Read Write Edit Glob Grep WebSearch WebFetch"`.

`bypassPermissions` 를 **쓰지 않는다.** 무인 실행이라 승인 프롬프트가 뜨면 그대로 멈추므로
필요한 도구만 미리 여는 방식이 맞고, `Bash(python *)` 로 파이썬 실행만 허용해 임의 셸 명령을 막는다.

---

# 회고도 CLI 로 (2026-08-20 추가)

## 왜 옮겼나 — 실사고

2026-08-20, **회고 코워크가 03:15~07:20(4시간) 돌면서 06:20 아침 슬롯을 삼켰다.**
그날 아침 리서치는 세션조차 만들어지지 않았고(메일·사이트 모두 공백), 사람이 오전에 발견했다.
회고가 오래 걸린 이유는 **KRX 차단(A46 재발)으로 pykrx 재시도가 폭주**한 것이다(에러 로그 71,428줄).

코워크 대기열에서 회고를 빼면 두 작업이 자원을 다투지 않는다 — **구조적 해결**이다.

## 구성 (전야와 같은 구조)

| 파일 | 역할 |
|---|---|
| `prompts/retro_review.md` | 회고 지시문(git 추적) |
| `run_retro_review.cmd` | 러너(ASCII 전용) |
| `retro_cli_status.py` | `retro_cli_status.json` 기록 |

프롬프트에 **시간 예산**을 넣었다 — 05:30 을 넘기면 그 시점 결론까지만 정리하고 마치라고 지시한다.
아침을 다시 삼키지 않게 하는 안전장치다.

## 등록

```powershell
$act = New-ScheduledTaskAction -Execute "C:\Users\USER\Desktop\stock_research\run_retro_review.cmd" -WorkingDirectory "C:\Users\USER\Desktop\stock_research"
$trg = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday,Wednesday,Thursday,Friday,Saturday -At "03:30"
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "StockRetroReview" -Action $act -Trigger $trg -Settings $set -RunLevel Limited -Force
```

- 요일 **화~토**: 회고는 직전 거래일까지의 결과를 본다. 월요일 새벽은 주말이라 새 만기가 없다.
- `ExecutionTimeLimit 3시간`: KRX 차단 시 retro_label 만으로 1시간을 넘긴다(실측). 다만 프롬프트가
  05:30 에 스스로 정리하므로 3시간에 닿을 일은 드물다.
- 아침 코워크(06:20)를 그대로 둬도, 회고를 CLI 로 옮기는 것만으로 슬롯 경합이 사라진다.

## 시각 선택

| 안 | 장점 | 단점 |
|---|---|---|
| **03:30 (현행 유지)** | 아침이 최신 피드백을 받는다 | FSC 가 직전 거래일 종가를 아직 안 실어 라벨이 D-2 에 머문다 |
| 17:30 (장 마감 후) | 라벨이 하루 앞당겨진다(A21 해소) | 그날 아침 분석은 전날 피드백을 쓴다 |

CLI 로 옮기면 둘 다 안전하다 — 아침 슬롯과 겹치지 않으므로.
