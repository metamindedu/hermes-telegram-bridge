# 원격 소스 관리 1단계

## 승인된 범위

- 비공개 GitHub 저장소 `metamindedu/hermes-telegram-bridge`.
- 기존 두 플러그인의 소스·테스트를 byte-for-byte 보존.
- 운영 데이터/개인 작업 기록을 제외하고, 원본 일치·비밀정보 검사 및 실제 테스트 후 commit/push.
- 원격 privacy와 정확한 main SHA, fresh clone을 독립적으로 확인.

## 하지 않는 일

- 운영 설치본 수정/이동, 설정 변경, Gateway/Desktop 재시작, Telegram 발송.
- 공식 통합 패키지화, 경로 재설계, 자동 배포 framework, CI 전면 개선.
- .env, 프로필 설정, 대화·수신함·알림 기록 및 로그 업로드.

## 완료 조건

1. 허용한 소스가 설치본과 정확히 일치한다.
2. 새 Git 이력에 운영 데이터·실자격증명이 없음을 검사한다.
3. 저장소에서 관련 Node/Python 테스트를 실행하고 실제 결과를 기록한다.
4. 비공개 원격 main과 로컬 commit/tree가 일치하고 fresh clone에서도 확인된다.
5. 원격 관리와 운영 설치/활성화가 분리됨을 명시한다.
