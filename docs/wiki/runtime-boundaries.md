---
title: Source and runtime boundaries
created: 2026-10-10
updated: 2026-10-16
type: concept
tags: [operations, ownership, deployment, privacy]
---

# Source and runtime boundaries

Phase 2 separates the bridge into two native directory packages inside one repository: the completion/CLI package and the optional clarify-ingress package. The Desktop half remains app-wide through Hermes' existing materialization path; ingress stays opt-in and attaches only to the existing Telegram application. These are package/ownership boundaries, not permission to install or activate them.

The completion package owns the fixed local CLI helper under the verified default Hermes home. The renderer observes local profile routes, but helper execution is always routed through the default local home. Named/foreign homes fail closed. The ingress loader binds the sibling helper modules to the same home in a per-home namespace; it does not add a polling consumer or a cross-home module alias.

The native request preserves the original 22,500-byte UTF-8 / 30,000-character base64url argv envelope. Results remain an ASCII outer base64 frame around at most 24 KiB of UTF-8 JSON (48,000 stdout characters including framing/logs). Native reply polling budgets the whole response before claiming; oversized frozen reply data uses bounded, digest-checked, read-only fragments with no schema change or claim/TTL renewal. See [[../operations|operations]] for exact limits and gates.

The exact install/update/rollback syntax and separate enablement/restart gates live in [[../operations|operations]]. Candidate install, Doctor, fake-home upgrade and rollback tests are local evidence only; no production package was installed or enabled in this phase.

Ingress callbacks are wired by the primary native package `session-complete-telegram` on the existing Telegram `Application`; `desktop-clarify-telegram` is retained only as a deprecated inert/no-factory compatibility stub. Both IDs and the shared `plugin-data/session-complete-telegram` state root remain unchanged. The native helper and handlers fail closed with `legacy_package_migration_required` while the legacy ID is effectively enabled; migration is explicit and never edits config or SQLite state automatically.

Version 1.7.0 is the one-package public release pinned at `13c08c022965a589e87fe7198ab1a93201539d7e`, using `desktop-command-approval-v3`. `replies_enabled` and `approvals_enabled` are independent default-off native settings; malformed/unknown/wrong-typed configuration fails closed, while outbound notifications remain independent. The v2 final read-only claim-validity gate and all existing exact owner/card/private-route/session/request/queue, command-free failure notice, `all:false`, no-grant, and no-replay constraints are retained. The 1.6.1 release at `1a76ed54ccf8a279d4dc4d9bfe1553b90d81d5ca` is historical; public release and commit-bound CI/anonymous installation were verified separately from maintainer catalog admission and production activation. Detailed state and activation limits are in [[../operations|operations]].

Code rollback and state rollback remain different. The shared `plugin-data/session-complete-telegram` directory, notification ledger, reply inbox, followup inbox, tombstones and credentials are never copied/reset by native package install/update/rollback. No state schema change or migration is introduced.

Catalog snapshot immutability and publication status must remain separate: keep both package entries pinned to the exact reviewed source, and publish later availability/PR-status documentation on the default branch without moving that package pin or its release tag. Public source and anonymous direct install do not imply human-maintainer catalog admission, search visibility or live Gateway activation. See [[../catalog-submission|catalog submissions]] and [[../public-install|public installation]].

## Modular release and catalog update boundaries

Public v1.8.0 uses `desktop-command-approval-v4` with the common service and parent-module gates while retaining default-off replies/approvals consent. Catalog SHA bumps require a fresh upstream review even when the owner has already published and installed the source. Update the existing unmerged single-package submission; never silently move historical release tags, change the fork main, or claim search admission from a successful branch push. Current pin/status belongs in [[../public-install|public installation]] and [[../catalog-submission|catalog submission]], not copied runtime observations.

## 1.8.1 approval coverage source candidate

The unpublished 1.8.1 source candidate keeps the public 1.8.0 pin and immutable approval-v4 fixture bytes unchanged, and preserves completion-recovery-v1. The renderer/helper/receiver approval capability is now v6; old v4/v5 halves fail closed. Existing settings (especially default-off `approvals_enabled`) are not written or migrated.

Legacy cards freeze one of two modes: `legacyqueue` (exact full queue row → `approval.respond`) or `legacyserver_request` (healthy, well-formed absence → `request.answer`). The canonical path is separately frozen as `canonical_native`: it preserves the actual opaque prompt ID, generation, full prompt, root/profile/route, Desktop owner, native instance, authority/replay epoch, private destination/card, receiver boot, TTL and claim. Snapshot failure/malformed legacy rows/duplicate IDs/authority changes never downgrade or switch a mode. All modes offer only once/deny; legacy request `status:"ok"` and canonical remote `resolved` are not child-settlement or command-completion proof. Canonical writes use the stored SQLite row via fixed CLI, durable `dispatching` CAS and exactly one four-field native response. The published `desktop-command-approval-v4` bytes, current saved consent, inbox schema and completion-recovery behavior remain unchanged. Operations details and test boundary are in [[../operations|operations]].

Cards render all redacted fields actually present in the native request, including a supplied editor edit; they do not imply unseen plugin arguments or protected-write diffs. Reviews remain one message with a 4,096 UTF-16-unit cap; unsafe/oversized payloads get Desktop-only guidance rather than truncation. No schema migration, installation, activation, Gateway restart or live Telegram interaction is implied by source tests.

## Structured-review and core-generation correction

Native structured review redacts each raw string leaf before JSON serialization so newline/key context is not lost. Credential-named fields are inspected recursively; their present scalar values are conservatively masked (null and empty-string values remain empty) while field names and surrounding object/array structure remain. Keys are validated without rewriting them; unsafe or redacted keys and unstable/omitted text fail closed to the existing command-free Desktop notice. This is a bounded regression guard, not a claim that any regex redactor detects every possible secret.

The source workflow explicitly tests immutable Hermes cores `74306c1d1b080837744faca3e371267dd09dda4b` (legacy) and `297552a9d646119871391d202509822cd86330df` (current), on Windows and Ubuntu with each checkout's own lock. The producer fixture binds imports to `HERMES_CORE_ROOT` and verifies the declared generation against actual module origins and Git HEAD in CI. The legacy core's absent `gateway.session_local_editor` is asserted as absent; the current core runs the actual editor producer through native request capture and bridge registration. Local focused tests do not substitute for lock-synced CI or independent source review.

## SDK boundary accepted for the single-package release

The user chose the existing public SDK without a new SDK/core development project. Successfully installed consumers reject recognized old-card replies; if the SDK cannot attach a guard, the bridge refuses readiness but cannot stop ordinary Gateway input. This limit must be prominent in install/security documentation, and a native dispatcher/PTB characterization test must verify both actual host fallthrough and denied bridge authority. It is not a sandbox or platform-wide fail-closed claim. Bridge ownership, consent, exact queue/request binding, one-shot claims, state preservation and no ambiguous replay remain required. See [[../operations|operations]] and [[../public-install|public installation]] for safe recovery guidance.

## Catalog admission remediation on the frozen 1.8.1 source

The catalog blocker on source `dd00deb2b92e43d1c28e3104fc869d057f0585a5` is concrete maintainer feedback: package credential capture must use only the public `agent.secret_scope.get_secret` API. `notifier.credentials(root)` preserves `root` for caller state/authority context but does not read a `.env` file. The default-home CLI and default Telegram factory execute under Hermes-loaded default credentials; registration/import stay metadata-only, and the standalone multiplexing `UnscopedSecretError` is not hidden by a plugin-created scope or environment fallback. The existing token/destination validation and factory `application.bot.token` equality guard remain intact. Catalog-level `requires_env` is separate from the native `plugin.yaml`/registration contract; do not turn credential absence into an import/metadata-registration gate.

The unused `launch.py` subprocess bootstrap had no references from current live source and has been removed. `docs/source-manifest.json` remains the immutable 2026-10-10 historical snapshot and still records that earlier file; it is not a current source inventory. The reply/followup inboxes now request owner-only directory/file modes (`0700`/`0600`) on POSIX, matching the existing approval-inbox pattern. Windows inherits the parent ACL; chmod success is not an NTFS ACL claim. No inbox schema, rows, retention policy, consent setting, approval-v5, or completion-recovery behavior changes here. See [[../operations|operations]] and [[../catalog-submission|catalog submission]] for the separate admission/release gates.

## Canonical approval compatibility and rollout hold

The legacy handler contract and the canonical session authority are different test targets even in the same core checkout. In core `297552a9d646119871391d202509822cd86330df`, canonical replay does not provide `open_requests`; approval replay is an abbreviated `approvals` projection. Canonical decisions bind `session_id`, `prompt_id`, `execution_generation` and `choice`, and require a subscriber actor. Desktop's SDK performs partial event/request adaptation, but may implicitly attach and serialize a cached generation instead of the caller's frozen one. A fresh CLI actor is not an equivalent subscriber and must not be used as a permission workaround.

The v5 source candidate was not a canonical adapter. Preserve its legacy/recovery GO, CI and earlier no-attach NO-GO as evidence for their original scopes; do not relabel them. The 2026-10-16 follow-on decision explicitly permitted read-only attach/subscription for approval only. The implementation below supersedes the earlier hold as a source design, while documented-import warnings, independent review/CI, release, activation and human-roundtrip gates remain.

## Current plugin-only API scope

The current v6 source candidate uses the current `hermes-gateway-v1` protocol through feature-detected, exported non-underscore `GatewayClient`/discovery utilities. It connects only to an explicit ready owner discovered for the target profile under the existing default-home root; it never calls `connect_gateway(None)`, executes a named-home package, creates/submits model work, resumes agent execution, interrupts, or bypasses subscriber/capability guards. These imports are currently outside the documented plugin import allowlist (catalog warnings, not automatic denial), so this is a narrow compatibility adapter, not a stable plugin-API certification. Unsupported/missing hosts fail closed. Completion recovery retains its separate no-resume contract.

For each candidate card, the helper attaches once for a full canonical snapshot, then detaches only the acknowledged subscription with no `end_reason`. The card binds opaque prompt ID/generation, full provided review, native instance/authority/replay epochs, public stored-session mapping, root/profile/route/connection, Desktop owner, private bot/chat/topic/message/card text, receiver boot, TTL and claim. A metadata-only mount on a reap race is never enough if any binding changes. For apply the CLI re-reads its stored row, refreshes the full snapshot and policy/lease/owner/claim, CASes `dispatching`, and emits exactly one native four-field `approval.respond`. Typed `resolved` means the Gateway handled the decision only; it does not assert child settlement or command completion. Already-resolved, stale/owner/permission denial, expiry and post-write uncertainty are terminal; no ambiguous replay. Full transcript/history never enters the CLI frame, SQLite descriptor, card or logs. See [[../../docs/operations|operations]] for limits and test boundaries.

## Before-first-access database confinement for source tests

A temporary `sessions` directory is not a temporary session DB. `SessionStore` primes the active/default DB during construction, and canonical authority startup advances that DB's epoch. Tests must use the repository-owned `tests/support/guarded_unittest.py` in a fresh `-I -S` process: establish a unique marked fixture root, install physical-path/SQLite fences before native imports and constructors, explicitly scope the fixture owner thread, and verify its actual handle/backing paths before authority initialization. Rejected default, URI, traversal, junction, cache and unwrapped-connection targets must reach neither a native open nor an epoch write. The fence also preserves the ATTACH authorizer and constrains test-owned Python descendants; PM-only workers are isolated from session/authority/model imports. No bypass switch or production DB repair is part of test setup.

Keep arbitrary-code test launchers outside the installable package rather than override the native scanner. Qualify cold native dependency imports before a timed publication oracle; do not stretch or waive a missing-request assertion to conceal setup work. Actual SDK event transformations are a separate boundary from raw canonical replay: remove only a verified same-ID `request_id` alias on a detached plugin copy, retain the exact native ID/generation and unknown fields, and reject an alias mismatch.

The resumed user scope prohibits shared-Gateway restart and other-profile interruption. Source/fixture success does not activate cached production handlers or authorize operational DB/epoch changes. See [[../../PLAN|current plan]] and [[../operations|operations]] for the separate release and activation gates.

Knowledge rules follow [[SCHEMA]], and navigation is maintained in [[index]].
