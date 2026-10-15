---
title: Source and runtime boundaries
created: 2026-10-10
updated: 2026-10-15
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

The unpublished 1.8.1 candidate keeps the public 1.8.0 pin and approval-v4 fixture bytes unchanged, and preserves completion-recovery-v1. Only the approval capability changes to v5 across renderer/helper/receiver to reject mixed cached v4 halves. Existing settings (especially default-off `approvals_enabled`) are not written or migrated.

An action requires a live native `approval` `srq-<12 hex>` and a frozen dispatcher: exact matching native queue row → `approval.respond` with the exact 32-hex queue ID; or a healthy complete queue snapshot with no matching/conflicting row → `request.answer` with the frozen srq. Failed/malformed/duplicate/conflicting queue observations never prove absence. A mode change, stale lease, route/session/epoch/owner change, or fingerprint mismatch cannot switch methods. Both paths send only once/deny and `all:false`; no persistent/session permission is granted. The request answer's typed `status:"ok"` is only Gateway acceptance, not proof that a compute-host child settled the decision; it becomes terminal uncertainty with no replay. Operations details and test boundary are in [[../operations|operations]].

Cards render all redacted fields actually present in the native request, including a supplied editor edit; they do not imply unseen plugin arguments or protected-write diffs. Reviews remain one message with a 4,096 UTF-16-unit cap; unsafe/oversized payloads get Desktop-only guidance rather than truncation. No schema migration, installation, activation, Gateway restart or live Telegram interaction is implied by source tests.

## Structured-review and core-generation correction

Native structured review redacts each raw string leaf before JSON serialization so newline/key context is not lost. Credential-named fields are inspected recursively; their present scalar values are conservatively masked (null and empty-string values remain empty) while field names and surrounding object/array structure remain. Keys are validated without rewriting them; unsafe or redacted keys and unstable/omitted text fail closed to the existing command-free Desktop notice. This is a bounded regression guard, not a claim that any regex redactor detects every possible secret.

The source workflow explicitly tests immutable Hermes cores `74306c1d1b080837744faca3e371267dd09dda4b` (legacy) and `297552a9d646119871391d202509822cd86330df` (current), on Windows and Ubuntu with each checkout's own lock. The producer fixture binds imports to `HERMES_CORE_ROOT` and verifies the declared generation against actual module origins and Git HEAD in CI. The legacy core's absent `gateway.session_local_editor` is asserted as absent; the current core runs the actual editor producer through native request capture and bridge registration. Local focused tests do not substitute for lock-synced CI or independent source review.

## SDK boundary accepted for the single-package release

The user chose the existing public SDK without a new SDK/core development project. Successfully installed consumers reject recognized old-card replies; if the SDK cannot attach a guard, the bridge refuses readiness but cannot stop ordinary Gateway input. This limit must be prominent in install/security documentation, and a native dispatcher/PTB characterization test must verify both actual host fallthrough and denied bridge authority. It is not a sandbox or platform-wide fail-closed claim. Bridge ownership, consent, exact queue/request binding, one-shot claims, state preservation and no ambiguous replay remain required. See [[../operations|operations]] and [[../public-install|public installation]] for safe recovery guidance.

Knowledge rules follow [[SCHEMA]], and navigation is maintained in [[index]].
