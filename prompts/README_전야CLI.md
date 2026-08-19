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
