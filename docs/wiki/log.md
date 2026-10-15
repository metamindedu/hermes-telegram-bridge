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
- No schema migration, production activation, live Telegram call, or source publication was performed in that correction step.

## 2026-10-10 update | Public pin versus publication status

- Recorded the durable distinction between immutable package/catalog source pins and later default-branch publication-status documentation.
- Public MIT source and anonymous pinned installation were verified separately from upstream fork-workflow approval, maintainer admission, discoverability and production activation. See `../catalog-submission.md` for the actual submission record; this wiki is not runtime evidence.

## 2026-10-10 update | Unified one-package candidate (unreleased)

- The approved design keeps `session-complete-telegram` as the stable native/Desktop/CLI ID and moves Gateway factory wiring into its package; the legacy `desktop-clarify-telegram` source is inert and must be explicitly disabled before the new factory/helper readiness is allowed.
- Native `replies_enabled` and `approvals_enabled` are independent default-off settings at the primary package's `plugins.entries` settings path. Strict raw config/type validation fails closed; outbound notifications remain independent. Stale/tombstoned card replies remain consumed on the existing PTB Application.
- The 1.7.0 candidate uses approval-v3; old v2 cached receiver/helper/renderer combinations are denied. The historical public v1.6.1 pin remains unchanged. This source task did not publish a SHA, merge catalog work, or activate production.
- Install/update/rollback preserve the stable ID and all user config, ledgers, SQLite state and tombstones; no schema migration or automatic legacy config edit is planned. See `../operations.md` and `../public-install.md` for the candidate boundary.

## 2026-10-10 update | Existing-SDK scope clarification

- User chose one package with existing public APIs, without SDK development. Keep bridge authorization fail-closed, but do not promise to stop ordinary Telegram input when the host cannot install its guard.
- Keep initialization-failure behavior as an executed native dispatcher/PTB characterization test that also verifies no bridge readiness or approval authority. Preserve earlier failed tests and candidate NO-GO results; the accepted scope changed, not historical evidence.
- Document the limitation and Desktop/normal-Gateway recovery path in public setup/security guidance; retain owner/consent/queue/no-replay and user-state preservation requirements.

## 2026-10-10 update | Single-package public release and catalog consolidation

- Public MITv1.7.0 tag/release pins13c08c022965a589e87fe7198ab1a93201539d7e, with source GO C0/H0/M0/L0,182Python/127Node, exactWindows/Ubuntu CI and anonymous single-package native installation. Later publication-status docs are separate from that immutable source.
- Owner updated #135956 to the single1.7.0 entry and closed obsolete companion #135957 without merge. Upstream maintainer admission/search and production activation remain distinct pending gates; public release does not imply either.
- Retain the documented SDK initialization limit and earlier failed-candidate evidence. No SDK development or automatic config/auth/DB migration is part of this release.
- On 2026-10-10 the author default installation was separately migrated to 13c08c0: one source package plus Desktop entry, both opt-ins, preserved four docs/credentials/ledgers, shared-Gateway safe restart and a verified real Telegram test send. A manual reply and approval-click round trip remains user-dependent.

## 2026-10-13 update | Modular release and catalog pin synchronization

- Public v1.8.0 stays pinned to 4874276f519f68fdbacb9767f70c5ed760b5c6fb; later default-branch docs do not move that source/tag.
- Synchronize the existing unmerged single-package catalog submission, all public install pins and five-module/default-off consent guidance. Upstream admission/search and live interaction remain separate gates.
- Preserve historical 1.7.0 evidence and the SDK limitation; this publication metadata update does not modify package code, live settings, credentials, data or Gateway processes.

## 2026-10-14 update | Bounded completion recovery source candidate

- Recorded the separate unpublished 1.8.1 helper/renderer recovery capability while preserving the receiver's 1.8.0 approval-v4 compatibility value and exact pinned 1.8.0 fixture bytes.
- The SDK route list is not connection evidence and routed RPC may cold-start a backend; recovery is restricted to the exact already-open SDK route, with complete replay and strict unknown-versus-absence handling.
- Candidate metadata is content-free and bounded by active and total-row caps. Existing delivery states remain untouched; only a durable exact-attempt pre-request connection failure may retry. Sender status remains distinct from receiver heartbeat.
- Source tests are isolated to fake homes and mocked Telegram transport. No production install, enablement, profile/config/auth/ledger write, live send, Gateway restart, commit, push, or publication occurred; independent Heavy review and release/activation gates remain pending.

## 2026-10-15 update | Bounded approval-coverage source candidate

- Kept the public 1.8.0 pin and immutable v4 fixture bytes; renderer/helper/receiver approval capability is v5 while package and recovery stay 1.8.1 and the compatibility field stays 1.8.0.
- Frozen two native dispatch paths only: exact matching parent queue row → `approval.respond`; healthy well-formed absence → `request.answer`. Snapshot failures, malformed rows, duplicate/conflicting IDs and later mode changes cannot downgrade/switch authority.
- `request.answer status:"ok"` remains terminal uncertainty because the compute-host parent relay can swallow child errors; it does not assert child decision or command completion and cannot be replayed.
- Full provided native request fields are redacted and shown with a 4,096 UTF-16-unit safe fallback; omitted tool arguments/diffs are not invented. No multipart or DB schema migration.
- `approvals_enabled` and other saved choices remain default-off/unchanged. Candidate source/tests/docs do not install, activate, restart, commit, push, publish or call live Telegram. Verification and independent review are separately reported in the task result.

## 2026-10-15 correction | Structured approval review and core-generation coverage

- Fixed the reviewed JSON-before-redaction leak by applying the real core redactor to raw string leaves before serialization; recursively mask credential-named values, preserve validated keys and ordinary metadata, and reject unstable/unsafe reviews to command-free guidance.
- The producer fixture now uses native `check_dangerous_command` for shell/Python detection, keeps smart-mode coverage without Tirith bootstrap, and uses only the portable `server_requests.cancel(sid, reason)` signature. It asserts the exact core import root and generation; legacy 743 has no editor module, while current 297 executes the real editor producer.
- Local focused evidence: Python approval suite 36/36 on each declared generation; Node approval suite 23/23. These used the existing local Python 3.14.7 environment, not fresh per-pin `uv sync`; GitHub Windows/Ubuntu CI and a new independent Heavy review remain pending. No live Telegram, install, config/credential write, restart, commit, push, or publication.