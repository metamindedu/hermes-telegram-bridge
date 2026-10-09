# Wiki Log

## 2026-10-10 create | Source-management boundaries

- 원격 소스 관리와 운영 반영을 분리하는 결정 및 상주 handler 활성화 함정을 기록했다.
- SCHEMA.md, index.md, runtime-boundaries.md를 초기화했다.

## 2026-10-10 update | Phase 2 native-package boundaries

- 기존 구성요소 디렉터리를 Hermes native `owner/repo/subdir` package 두 개로 설치 가능하게 바꾸고, IDs와 상태 ownership을 유지했다.
- fixed default-home CLI/frame 경로, named/foreign-home refusal, per-home helper namespace 및 fake-home install/update/rollback 검증을 문서화했다.
- 고정 core/action CI와 정확한 native command syntax를 추가했다. 운영 install/enablement/Gateway activation은 수행하지 않았다.
- repository instruction files AGENTS.md/CLAUDE.md는 tool approval gate로 수정하지 못해 phase 1 문구가 남아 있으며, 이것은 문서 동기화의 미해결 blocker다.
