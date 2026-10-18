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

## 2026-10-16 update | Catalog credential and inbox-permission remediation

- On frozen base `dd00deb2b92e43d1c28e3104fc869d057f0585a5`, replaced package-local `.env` parsing with public `agent.secret_scope.get_secret`; preserved validation, `credentials(root)` signature/context role, default-factory token binding, and metadata-only import/registration. Catalog `requires_env` remains a separate owner-owned catalog change; native manifest/default-off settings were not changed.
- Removed the unreferenced `launch.py` bootstrap after a live-source reference scan. Kept `docs/source-manifest.json` byte-for-byte as the historical 2026-10-10 snapshot and documented that it still lists the earlier file.
- Applied the existing approval-inbox chmod pattern to only reply/followup inbox directories and SQLite files: POSIX `0700`/`0600`; Windows inherits its existing parent ACL. No schema/data/retention/policy change.
- RED showed the old file reader failed scoped-source and active factory-token binding assertions. Focused Python checks then collected 136 tests on each generation: 134 passed and 2 POSIX-only tests were skipped on Windows, both on verified current core HEAD `297552a9d646119871391d202509822cd86330df` and on the provided legacy source snapshot under `scratch/apcore/source` (the `74306c1d1b080837744faca3e371267dd09dda4b` path; module origins were qualified, but that extracted snapshot has no `.git` metadata for an independent `rev-parse`). Recovery fixture checks passed 11/11 and focused registered native reply/followup/approval dispatch checks passed 3/3 on current core. The exact inbox mode assertions also passed in a Python 3.14 Linux container with all fake homes/tmp and receipts on profile scratch; this verifies POSIX-visible bits only, not the host's NTFS ACL. Runs used sanitized credential child environments; no production egress or state writes occurred. Fresh Ubuntu CI remains pending.
- New candidate CI and Heavy review remain pending. No operational/production install, commit, push, release, catalog API/update, config/auth/state write, Gateway restart, or live Telegram call was performed; the three registered-CLI tests used only disposable fake-home native-install fixtures under profile scratch.

## 2026-10-16 diagnosis | Canonical approval rollout hold

- Distinguished the approved legacy/recovery source GO and four-job CI at `dd00deb2b92e43d1c28e3104fc869d057f0585a5` from compatibility with the actual canonical owner. Read-only runtime metadata and an actual `CanonicalDesktopProtocol` probe show that the SDK partially adapts events, does not synthesize `open_requests`, may implicitly attach, and serializes its cached generation rather than a caller's frozen generation.
- The native canonical response also requires an attached subscriber actor; a fresh CLI connection is not a permission workaround. A public strict existing-socket/frozen-response primitive needs separate SDK/app scope. The scope-expansion question went unanswered, so it grants no consent. No SDK/core changes, common production install, new public release, or forced shared-Gateway restart were performed. Preserve active unrelated work and leave the canonical deployment gate unmet.
- Keep catalog credential compliance, source synchronization/CI, maintainer admission, actual catalog visibility, installed bytes, activation and a human Telegram roundtrip as distinct outcomes. Updating a draft submission must disclose the canonical limitation and must not be called a completed deployment.

## 2026-10-16 source acceptance | Exact execution-path binding closed

- Preserve the intermediate NO-GOs (`00ed26d5-...`: C0/H2; `0a670e13-...`: C0/H1). Main applied their limited test-bootstrap corrections without relaxing operation permissions: positional process controls are refused; fixture tool names do not confer executable identity; selected programs/scripts and PM entrypoints are pinned; execution path operands and relevant environment paths must be absolute before scope checks; child cwd is physically verified and fixed.
- New recorder regressions prepare different parent/child cwd and shadow files without executing those files. `path-binding-red.log` records FAIL before correction; `path-binding-green.log` records nine Recorder cases PASS afterward. The scrubbed parent recheck `path-binding-parent-focused.json` passed DB guard 12, child qualification four and affected backend/joined integration eleven. Its DB/epoch activity remained in the marked synthetic fixture root.
- Fresh independent Heavy review `a1156bee-e1ab-4f9c-964c-0b6174402b57` accepted the remaining H2 binding boundary at tree `e09fb8f05abeaf5117c49190de6f3162cf8737eb` with C0/H0. It reused the unchanged H1/H3 source acceptances; it explicitly did not grant whole-candidate execution or production GO.
- Delivery is still blocked by native PM fresh-install/Doctor positive qualification, the latest complete aggregate and commit-bound CI. Do not reinterpret the semantic GO or older full passes as release evidence. No new source commit/push, catalog re-pin, operating install/activation, shared restart, other-profile interruption or production DB/generation mutation is asserted.

## 2026-10-16 verification checkpoint | Independent High findings and bounded fixes

- Fresh independent review `666a1604-d0f5-4807-b893-fed9839483d4` rejected tree `b923fa489794ffba1466e8e0c3367a65764d2b49` with C0/H3/M0/L0: canonical first-card registration omitted the owner heartbeat; an owner change could reissue an ambiguous native approval slot; child launch checks could be bypassed by executable overrides and indirect shells. Preserve that NO-GO and the original full-suite receipts; they are not final acceptance of later source.
- The bounded correction added canonical lease/capability preparation, cross-owner native-slot protection including terminal history, and process launch denials exercised first with a Popen recorder. Parent rechecked the modified backend/integration through a scrubbed-OS-environment guard: DB confinement 12/12, child qualification 4/4, affected canonical tests 11/11 passed with actual synthetic DB paths under the marked fixture root. Latest standalone Node: 202 cases, 201 passed, one real-event case supplied by the Python integration. No live approval was sent.
- Native fresh-install/Doctor qualification on the latest guard still fails closed on a legitimate `uv venv` command. Pinning the actual managed uv binary corrected tool identity but did not satisfy all operation predicates. Do not loosen the allowlist, silently skip this test, infer a complete latest aggregate, repin the catalog or activate production from the affected-test pass. The remaining native-PM verification method needs diagnosis/acceptance before release gates can complete.
- The correction worker reported a possible credential-like inherited-environment disclosure in a failed diagnostic. Values are not reprinted here and validity/exposure scope is unconfirmed. Parent diagnostics now use an explicit OS/tool environment allowlist, and empty-recorder assertions compare only counts rather than printing captured environment mappings. No key rotation or auth/config change is authorized or performed by this checkpoint.

## 2026-10-16 resumption | Database fence and SDK-boundary corrections

- Implemented a fresh-process before-first-open DB fence, explicit owner-thread home override, actual SessionDB/backing-path validation before authority startup and guarded Python descendant handling. Parent negative tests additionally rejected an unwrapped C connection that otherwise lacked an ATTACH authorizer. Pure DB tests passed 12/12 before native qualification; the first actual native DB lived inside the marked fixture root with zero epoch calls.
- Corrected the SDK projection at the plugin boundary only: clone provided review data, remove only a same-ID alias, preserve opaque native IDs/generations/unknown fields, reject mismatch and avoid mutating the host cache. The actual installed SDK transformation passed the focused observer regressions; a real-core replay event is now transformed by that same SDK before the integration observer.
- After confinement qualification, the canonical integration passed 3/3, including ten real current producer families with once/deny settlement against exact waiters. The SSH fixture now targets the actual synthetic OS home rather than an arbitrary profile directory. These are isolated-source results, not a production button/human callback claim.
- Preserve the first complete guarded aggregates as failures: native-install cases were blocked because the development executor was accidentally inside the install package; a legacy producer timed out while cold-importing the real Gateway redactor. Moved the executor/helpers into repository test support without weakening the scanner, shortened only owned Windows fixture names, and prepared the real native redactor before the unchanged timed oracle. Targeted native install/Doctor and legacy producer checks then passed.
- Guarded PM-only child support keeps derived interpreters, dependency paths, fixed entrypoints and all metadata inside the verified fixture scope; session/authority/model imports remain denied. The actual PM targeted run had zero epoch/native-session constructor calls. Complete current/legacy aggregates, independent Heavy acceptance, commit-bound CI, catalog admission and live activation remain separate pending gates. No shared restart, other-profile interruption or operational DB/generation modification is part of this continuation.

## 2026-10-16 incident boundary | Actual DB confinement before resuming tests

- The user reports an earlier canonical test touched the real Claire database and triggered `stale_epoch`; the session was restored separately. Treat earlier fixture passes as unqualified for database safety, not proof of isolation. This work does not repeat recovery or modify production epochs/data.
- Code inspection confirms `SessionStore(sessions_dir, config)` primes a DB selected from the active home scope, not from `sessions_dir`; authority initialization starts a runtime epoch in the resulting DB. Changing environment variables after scoped/native imports is not a sufficient boundary. Require a before-first-access actual DB-path fence and explicit fixture-owned handle validation before any authority startup.
- The user authorizes resumption of the partial plugin candidate but prohibits shared-Gateway restart and interruption of other profiles. Do not reuse historical restart consent. Unsafe old aggregate/pilot commands remain blocked until the confinement gate is proven; no operational DB or execution-generation repair is part of this continuation.

## 2026-10-16 checkpoint | Canonical integration not accepted

- The official read-only-subscription route passed the actual canonical dispatcher/loopback-client technical pilot (36 checks); separately, real native control-ticket/owner authentication and read-only attach/detach from both the named and default helper homes preserved this existing live session. No live approval decision was sent. These are distinct from complete plugin or human Telegram proof.
- The implementation allocation `f317551a-7570-497c-b229-b4daa872ae10` timed out after 90 minutes (exit 124, route evidence unverified, owned process-tree cleanup confirmed). Its code is a retained partial checkpoint. No automatic new implementation allocation, release, install or shared-Gateway restart followed.
- Parent checkpoint reconciliation: Node 196 tests, 195 pass and one conditional event test (injected by the Python integration); Python non-native-install slice 279 tests, one SSH-config producer fixture failure and two POSIX-only skips. The aggregate failed. Do not infer success from a log directory named `green`.
- A parent probe invoking the actual installed `CanonicalDesktopProtocol.event` found that its same-ID `request_id` alias is forwarded unchanged to the new canonical observer and rejected as an unknown native prompt field. Raw canonical event injection missed that SDK transformation. A bounded correction must preserve the exact opaque ID and generation, validate any documented alias equality, reject mismatches/unknown fields, and add a test through the real SDK transformation rather than weaken full-snapshot checks. This does not require a core edit.
- Evidence: profile-scratch `telegram-release-181/canonical-sdk-alias-red.json`, `apall/canonical-reconcile-node/result.json`, `apall/canonical-reconcile-fast/result.json`. Full/native-install/legacy gates, independent Heavy GO, CI, activation and human roundtrip remain uncompleted; maintainer import-support clarification is pending.

## 2026-10-16 decision | Continue with official APIs and no core edits

- The user explicitly chose a plugin-only design using current official APIs. Normal read-only attach/subscription may be used for an approval response; the earlier absolute no-attach design condition is retired for that path, not for completion recovery. No new prompt/model task, agent-execution resume, unauthorized actor, arbitrary/private transport or unrelated interruption is approved.
- Require an actual canonical authority/subscriber pilot and fresh full prompt/generation checks before implementing or claiming success. Preserve previous source GO/CI and no-attach NO-GO results in their original scopes.
- Corrected three secret-scope fixture regressions without changing runtime authentication or assertions: copy the caller context into each test worker and use a genuinely missing public secret scope instead of removing a now-unused credential file. The non-native-install Python slice ran 271 tests successfully with two POSIX-only skips on Windows; this is not the full native-install suite or live Telegram evidence.

## 2026-10-16 implementation | canonical approvals source candidate v6

- Implemented the plugin-only `canonical_native` mode alongside explicitly named `legacyqueue` and `legacyserver_request`; no fallback after a frozen mode, no synthetic SRQ, and no change to the published 1.8.0/v4 receiver fixture, 1.8.1 package/recovery version, 1.8.0 approval compatibility value, or completion-recovery behavior.
- The canonical adapter uses current exported non-underscore Gateway client/discovery utilities and exact canonical RPC shapes, but current catalog lint marks the imports undocumented (warnings; not a stable documented-plugin API guarantee). It attaches read-only to an explicit ready owner, bounds a full snapshot at the current public client's 8 MiB ceiling, detaches only its own subscription without `end_reason`, persists full prompt/generation/authority/replay/route/card/owner bindings, and CASes before one stored-row four-field response. Ambiguous post-write outcomes are terminal/no replay.
- Focused current-core integration uses `297552a9d646119871391d202509822cd86330df` actual `AuthorityConnection`, live waiter, Gateway client RPC framing, actual replayed approval event through the Desktop `onEvent` callback, fixed registered CLI and actual PTB callback dispatch. The legacy `74306c1d1b080837744faca3e371267dd09dda4b` qualification fails unsupported without fallback. Owner identity/endpoint discovery and Telegram transport are fixture-stubbed; the separate live owner/ticket qualification and pilot remain technical evidence only.
- Verified locally: canonical Python suite 7/7 pass; `test_approval_bridge` 36/36 pass (its ten real current producer cases cover the legacy routes, not yet an end-to-end canonical application for every producer); targeted Node approval/recovery suites 60 pass, 1 actual-event test skipped in standalone mode because it is supplied by the Python integration, and `node --check` passes. The Python integration passed after invoking the actual-event Node test with the event obtained from core replay.
- Remaining gates: canonical end-to-end once/deny exercise for all ten real producer families, full focused/full CI/native-install suite, independent Heavy review, maintainer import-catalog acceptance, immutable candidate/release approval, install/activation, and human Telegram roundtrip. No production GO is claimed. Local materialization test requires the workflow's Hermes checkout at the repository `hermes-agent/` path; that checkout is absent locally, so this test did not pass here.
