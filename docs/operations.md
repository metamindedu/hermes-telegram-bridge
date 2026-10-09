# 개발·설치 반영·복구 경계

## 소스와 운영 상태의 분리

저장소는 코드의 개발 원본이다. 운영 설치본과 상태 DB는 Git 밖에 둔다. 수신함, 알림/전송 dedup ledger, 영구 send claims, 메시지 tombstones를 코드에 묶어 복사하거나 초기화하지 않는다. 과거 카드의 terminal 상태를 복구 명목으로 되살리지 않는다.

## 검증

- Node: `node --check plugin.js`와 모든 포함된 `.test.mjs`.
- Python: `test_reply_bridge`, `test_followup_bridge`; 실제 PTB dispatch도 mock 네트워크로 확인.
- `test_notifier`는 해당 Hermes core/PM 환경을 명시한 별도 검증이다. 기존 bootstrap 오류는 독립적으로 보고한다.
- 실행 결과, 의미 검수, 파일 반영, 런타임 활성화 및 live-user 왕복은 별개다.

## 승인된 운영 반영

1. 설치 후보 commit을 확정하고 관련 테스트와 필요한 독립 검수를 마친다.
2. 운영 설치 파일과 비교해 관련 없는 변경을 덮어쓰지 않는다.
3. 두 구성 요소를 서로 맞는 후보로 반영하고 정확한 파일 hash를 확인한다. Git pull/push만으로 자동 반영하지 않는다.
4. Desktop의 지원되는 hot reload/loaded receipt를 확인한다.
5. 상주 Gateway Python 변경은 명시적으로 승인받아 재시작한다. 공유 served-profile 범위, active work, 열린 질문 카드부터 확인한다. 재시작은 receiver boot를 바꾸므로 열린 카드가 종료될 수 있다.
6. 새 PID의 live status, 기존 adapter 재연결, 수신기 heartbeat와 설치 hash를 읽어 확인한다. CLI exit 0만으로 완료를 주장하지 않는다.
7. 실제 Telegram 답변 왕복이나 장기 soak를 실행하지 않았다면 그 한계를 명시한다.

## 복구

Git의 정확한 이전 commit에서 코드만 되돌린다. 상태 DB·자격증명은 변경하지 않는다. 이전 버전과 현재 스키마가 호환되는지 먼저 확인하고, 필요한 재시작은 별도로 승인받는다. 일반 코드 변경에 중복 ad-hoc 백업을 만들지 않는다.

## 최초 원격 등록의 검증 범위

초기 질문 카드 수정은 관련 브리지 Python 68개와 Node 95개 테스트를 통과했고 독립 검수에서 남은 관련 Critical/High/Medium이 모두 닫혔다. 소스 manifest는 그 설치 기준점을 기록한다. 개인 대화·작업 UUID·PID별 로그는 저장소에 넣지 않는다. 최초 원격 등록이 새 배포나 다른 PC 설치 검증을 의미하지 않는다.
