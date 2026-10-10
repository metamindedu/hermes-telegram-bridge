# Wiki Log

## 2026-10-10 create | Source-management boundaries

- 원격 소스 관리와 운영 반영을 분리하는 결정 및 상주 handler 활성화 함정을 기록했다.
- SCHEMA.md, index.md, runtime-boundaries.md를 초기화했다.

## 2026-10-10 update | Phase 2 native-package boundaries

- 기존 구성요소 디렉터리를 Hermes native `owner/repo/subdir` package 두 개로 설치 가능하게 바꾸고, IDs와 상태 ownership을 유지했다.
- fixed default-home CLI/frame 경로, named/foreign-home refusal, per-home helper namespace 및 fake-home install/update/rollback 검증을 문서화했다.
- 고정 core/action CI와 정확한 native command syntax를 추가했다. 운영 install/enablement/Gateway activation은 수행하지 않았다.
- repository instruction files AGENTS.md/CLAUDE.md는 tool approval gate로 수정하지 못해 phase 1 문구가 남아 있으며, 이것은 문서 동기화의 미해결 blocker다.

## 2026-10-10 update | Native command approval inbox boundaries

- Recorded the versioned, one-shot Telegram Allow Once/Deny path for the real pinned in-process dangerous-command queue only; generic/execute_code/plugin-rule/compute-host approvals stay notification-only.
- Documented exact queue-ID `approval.respond` semantics, local snapshot checks without server fingerprint CAS, complete redacted review requirements, no-replay on leases/restarts, and same-Application/no-second-poller ownership.
- Added the isolated approval inbox to the helper fingerprint/capability handshake and documented that this source change is not Gateway activation or production approval.

## 2026-10-10 update | Bounded native reply transport and Desktop ownership

- Preserved the original Windows argv byte/token envelope and made local encoding/size failures permanent per ask fingerprint rather than repeatedly launching.
- Kept the ASCII result frame while budgeting UTF-8 JSON before reply claims; oversized frozen replies use read-only digest-checked fragments with existing claim/TTL gates and no schema migration.
- Clarified that unrelated files left in the managed Desktop folder can be removed by a later packaged update or rollback; any separately approved preservation/relocation must happen before entry adoption, which alone is not deletion approval.
- Verification now includes synthetic actual-core alignment/adoption followed by packaged update and rollback; no current Desktop files were moved.

## 2026-10-10 update | Approval last-gate and bounded response corrections

- Bumped the matching renderer/helper/ingress handshake to 1.6.1 and `desktop-command-approval-v2` so mixed old/new packages do not grant the pre-dispatch gate.
- Added a read-only last claim-validity check after the final snapshot; it does not renew/reclaim or provide atomic fingerprint CAS.
- Recorded exact complete native JSON budgeting, all-row observation before one claim, command-free unsafe-review notices, and rejection of edited PTB callback messages.
- No schema migration, production activation, live Telegram call, or source publication was performed.
