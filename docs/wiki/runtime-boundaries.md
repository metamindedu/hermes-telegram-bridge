---
title: Source and runtime boundaries
created: 2026-10-10
updated: 2026-10-10
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

## SDK boundary accepted for the single-package release

The user chose the existing public SDK without a new SDK/core development project. Successfully installed consumers reject recognized old-card replies; if the SDK cannot attach a guard, the bridge refuses readiness but cannot stop ordinary Gateway input. This limit must be prominent in install/security documentation, and a native dispatcher/PTB characterization test must verify both actual host fallthrough and denied bridge authority. It is not a sandbox or platform-wide fail-closed claim. Bridge ownership, consent, exact queue/request binding, one-shot claims, state preservation and no ambiguous replay remain required. See [[../operations|operations]] and [[../public-install|public installation]] for safe recovery guidance.

Knowledge rules follow [[SCHEMA]], and navigation is maintained in [[index]].
