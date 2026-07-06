# -*- coding: utf-8 -*-
"""
rebuild_consolidated.py — 두 Cowork 지시문을 한 파일로 합쳐 'cowork_지시사항_통합본.md' 재생성.
  PART A = cowork_instructions.md (분석 리서치 Cowork)
  PART B = mock_invest_cowork_guide.md (모의투자 Cowork)
cowork_instructions.md 나 mock_invest_cowork_guide.md 를 수정한 뒤 이 스크립트를 한 번 돌리면
통합본이 최신으로 갱신된다 (원문을 그대로 합치므로 전사 오류 없음).
  실행:  python rebuild_consolidated.py
"""
import re

research = open('cowork_instructions.md', encoding='utf-8').read()
mock = open('mock_invest_cowork_guide.md', encoding='utf-8').read()
research = re.sub(r'<!--.*?-->\s*', '', research, count=1, flags=re.DOTALL).lstrip()


def drop_h1(s):
    lines = s.lstrip().split('\n')
    if lines and lines[0].startswith('# '):
        return '\n'.join(lines[1:]).lstrip('\n')
    return s.lstrip()


research_body = drop_h1(research).rstrip()
mock_body = drop_h1(mock).rstrip()

PREAMBLE = '''# Cowork 통합 지시사항 (최종본) — 리서치 + 모의투자

이 문서는 stock_research 자동 리서치 시스템의 '두 Cowork 역할'에 대한 최종 통합 지시사항이다.
두 역할은 각각 별도의 Claude Desktop(Cowork) 인스턴스로 동작한다.

  - PART A = 분석(리서치) Cowork : 매일 아침(또는 수동) 호스트가 모은 데이터를 분석해
            03_final_report.md + predictions.json 을 만들고 report-done 으로 발송을 신호한다.
  - PART B = 모의투자 Cowork    : 호스트가 '전달 폴더'로 보내준 그 분석을 읽어 가상(모의) 매매를 한다.

[두 역할의 연결]
  분석 Cowork 가 리포트를 끝내면(REPORT_DONE) 호스트가 (mock_forward_config.txt 의 enabled=1 일 때만)
  그 리포트를 모의투자 Cowork 의 '전달 폴더'(folder=...)로 복사한다. 그래서 PART B 는 enabled=1 일
  때만 실제로 동작한다(아래 PART B [활성 조건] 참조).

[사용법]
  - 분석 Cowork(stock_research 폴더에서 도는 Desktop)에게는 PART A 를 지시문으로 준다.
  - 모의투자 Cowork(전달 폴더를 읽는 별도 Desktop)에게는 PART B 를 지시문으로 준다.
    (PART B 의 내용은 전달 폴더에 mock_invest_cowork_guide.md 로도 자동 복사된다.)
  - 한 인스턴스에 두 역할을 동시에 맡기지 마라(샌드박스·작업 폴더가 다르다).
'''
PARTA = '''

================================================================================
==============================   PART A   ======================================
        분석(리서치) Cowork 지시사항  —  stock_research 폴더에서 분석 수행
================================================================================
================================================================================
'''
PARTB = '''

================================================================================
==============================   PART B   ======================================
        모의투자 Cowork 지시사항  —  전달받은 분석으로 가상(모의) 매매
================================================================================
================================================================================

[활성 조건 — 매우 중요] 이 PART B 는 mock_forward_config.txt 의 enabled=1 일 때만 동작한다.
  - enabled=1 : 분석이 끝날 때마다 리포트가 전달 폴더로 복사된다 → 새 리포트가 오면 아래 루틴 수행.
  - enabled=0 : 리포트가 전달되지 않는다 → 전달 폴더에 새 데이터가 없으므로, 신규 매매를 하지 말고
                대기한다(보유분이 있으면 마크투마켓·손절/익절 점검만, 신규매수 보류).
  판별법: 전달 폴더의 latest.json 이 '새 세션'으로 갱신됐는지 본다. 갱신이 없으면 전달 꺼짐(또는
  분석 미실행)으로 간주하고 대기하라. (enabled 스위치 자체는 호스트 쪽 파일이라 네가 못 볼 수 있다.)
'''
out = (PREAMBLE + PARTA + '\n' + research_body + '\n' + PARTB + '\n' + mock_body + '\n')
open('cowork_지시사항_통합본.md', 'w', encoding='utf-8').write(out)
print('OK rebuilt cowork_지시사항_통합본.md lines=%d' % (out.count('\n') + 1))
