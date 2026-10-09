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

The exact install/update/rollback syntax and separate enablement/restart gates live in [[../operations|operations]]. Candidate install, Doctor, fake-home upgrade and rollback tests are local evidence only; no production package was installed or enabled in this phase.

Ingress callbacks still run on the Gateway's existing Telegram `Application`; factory registration alone is not activation evidence. The native package ID `desktop-clarify-telegram` remains separate from the Desktop/CLI package ID `session-complete-telegram`, and their enablement must not be conflated.

Code rollback and state rollback remain different. The shared `plugin-data/session-complete-telegram` directory, notification ledger, reply inbox, followup inbox, tombstones and credentials are never copied/reset by native package install/update/rollback. No state schema change or migration is introduced.

Knowledge rules follow [[SCHEMA]], and navigation is maintained in [[index]].
