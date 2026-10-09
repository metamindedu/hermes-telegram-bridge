# Hermes Telegram Bridge 작업 지침

이 저장소는 Desktop 텔레그램 알림과 Gateway 수신 핸들러를 함께 보존하는 개발 원본이다.

1. README.md, PLAN.md, docs/operations.md와 docs/wiki/{SCHEMA.md,index.md,log.md}를 먼저 읽는다.
2. 현재 단계는 원격 소스 관리다. 공식 통합 설치 패키지나 이식 가능한 플러그인으로 소개하지 않는다.
3. 설치본을 직접 수정하지 않는다. 먼저 저장소에서 개발·검증하고, 승인된 반영만 수행한다.
4. Git commit/push는 운영 반영이나 Gateway 재시작을 승인하지 않는다. 재시작 전 공유 Gateway 범위와 active work를 확인한다.
5. 기존 plugin ID, 상태 저장 위치, 영구 중복 방지 기록과 사용자 인증 조건을 유지한다. 상태 DB를 코드와 함께 복사하거나 삭제하지 않는다.
6. Secret, .env, 프로필 설정, 대화, 수신함, 로그 및 개인 작업 증거는 커밋하지 않는다. 합성 테스트 문자열과 실자격증명을 구분한다.
7. 관련 없는 사용자 변경을 보존한다. 공유 동작/인증/비동기 계약 변경은 독립 Heavy 검수와 실제 테스트를 거친다.
8. Node 테스트와 Python 브리지 테스트를 각각 실행·보고한다. notifier의 기존 Hermes bootstrap 오류를 전체 테스트 통과로 숨기지 않는다.
9. 현재 Windows 설치 경로가 코드에 남아 있다. 경로 변경과 공식 패키지화는 별도 승인·검증 작업이다.
10. AGENTS.md와 CLAUDE.md의 공통 지침을 동기화한다. wiki는 탐색 자료이며 현재 운영 상태의 증거가 아니다.
