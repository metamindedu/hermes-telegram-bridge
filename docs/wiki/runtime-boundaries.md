---
title: Source and runtime boundaries
created: 2026-10-10
updated: 2026-10-10
type: concept
tags: [operations, ownership, deployment, privacy]
---

# Source and runtime boundaries

원격 저장소는 개발 원본이며 push가 실제 설치/활성화로 이어지지 않는 것이 이 프로젝트의 기본 운영 경계다. 처음에는 안정 코드를 재설계하지 않고 두 구성 요소를 한 기준 commit에 보존한다.

수신기 callback은 Gateway에 상주한다. 파일 변경/registry rescan만으로 기존 callback 교체를 단정하지 않는다. 승인된 재시작과 새 수신기 heartbeat 확인이 필요하며, 그때 열린 질문은 boot 변경으로 종료될 수 있다.

코드 rollback과 상태 rollback은 다르다. 원격에는 실행 DB를 올리지 않으며, 코드 복구가 영구 중복 방지 기록을 삭제하거나 terminal 카드를 부활시키면 안 된다.

지식 규칙은 [[SCHEMA]], 탐색은 [[index]]를 따른다.
