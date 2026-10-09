# Project Operations Wiki Schema

## 범위

이 wiki는 Hermes Telegram Bridge의 장기 결정, 운영 경계, 코드만으로 알기 어려운 함정을 보존한다. 현재 실행 상태는 Git, 실제 설치 hash, live Gateway/수신기 및 로그로 확인한다.

## 규칙

- 소스 코드·함수 목록·파일 구조를 복제하지 않는다.
- Secret, 사용자 대화, 고객 데이터, 런타임 수신함과 개인 작업 기록을 저장하지 않는다.
- 지식 페이지는 title/created/updated/type/tags frontmatter와 최소 두 개의 내부 wikilink를 가진다.
- 허용 tags: operations, deployment, ownership, testing, privacy.
- 페이지 추가/변경 시 index.md와 log.md를 함께 갱신한다.
- 운영 절차의 본문은 ../operations.md에 두고 wiki는 durable 결정/함정만 담는다.
