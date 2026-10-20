# Hermes plugin catalog submission — withdrawn

Public MIT release **1.8.0** is pinned at **`4874276f519f68fdbacb9767f70c5ed760b5c6fb`**. Historical 1.7.0 remains at `13c08c022965a589e87fe7198ab1a93201539d7e`. The package and Desktop ID remain `session-complete-telegram`; native CLI remains `desktop-telegram-bridge`. One package includes the Desktop half and optional handlers on the existing Telegram Application; no companion install or second poller is required.

## Withdrawn submissions (verified 2026-10-20)

- [#135956](https://github.com/NousResearch/hermes-agent/pull/135956): closed on 2026-10-18, unmerged. The owner withdrew catalog registration and retains a single personal-use source, not separate public/private variants.
- [#135957](https://github.com/NousResearch/hermes-agent/pull/135957): withdrawn and closed, not merged. The old companion is an inert compatibility stub, not a second catalog listing.

These were originally submitted for historical two-package 1.6.1 at `1a76ed54ccf8a279d4dc4d9bfe1553b90d81d5ca`; that release remains unchanged. Earlier candidate failures and NO-GO evidence are not retroactively accepted.

## Historical immutable 1.8.0 entry (not an active submission)

```yaml
name: session-complete-telegram
repo: https://github.com/metamindedu/hermes-telegram-bridge
sha: "4874276f519f68fdbacb9767f70c5ed760b5c6fb"
subdir: desktop-plugins/session-complete-telegram
title: "Hermes Telegram Bridge"
maintainer: metamindedu
tier: community
category: desktop
requires_hermes: ">=0.21.5"
version: "1.8.0"
capabilities:
  provides_tools: []
  provides_hooks: []
  provides_middleware: []
  requires_env: []
```

The historical submitted entry additionally included disclosure text, setup `docs_url`, `readme:false`, and `known_issues`. Immutable source docs were frozen before release, so the setup page was used rather than presenting that pre-publication status as current catalog status. These metadata choices do not change the installed SHA and do not reactivate the withdrawn submission.

`register(ctx)` declares one fixed CLI and one existing-Gateway factory; these are not tools/hooks/middleware. The seven native boolean settings control a common service switch and five effective modules. Existing `replies_enabled` and `approvals_enabled` consent defaults off independently; common/native and parent-notification gates remain required. No configuration consent is implied by code installation or a catalog update. Manifest/registration/capability declarations were checked with the official parser/validator; no undeclared capability is invented.

## Disclosures maintained in source and PR

- Uses the user's existing default-home Telegram bot/private owner; sends card content to `api.telegram.org`. Reads same-home local profile session titles, persisted final responses and pending requests. Redacted completion previews are capped at1,200characters, but redaction is not absolute.
- Credentials remain with their owner: no token passed to the renderer, copying, refresh/rotation/writes or other vendor credential-store access. No author telemetry. Cards are copyable/forwardable.
- Local plaintext SQLite retains expiring inbox content and minimal deduplication/tombstone identifiers. Owner-only OS permissions are required and not automatically audited.
- Exact private owner/original card/live route/session/request/queue, strict raw AND effective consent, full safe review, fresh leases/final read-only claim validity, once/deny with `all:false`, no permanent/session grants and no ambiguous replay remain mandatory. Unsupported/unsafe requests stay command-free Desktop guidance; delivery is not command completion. Local freshness is not atomic server fingerprint CAS.
- Existing public SDK only: no runtime core/private-store override, self-updater, arbitrary renderer shell, second poller, or forced shared Gateway stop. When even reject-only handler installation fails, the bridge refuses readiness but ordinary Telegram input can continue, including old-card text. Use Desktop during initialization/receiver failure and recover Gateway normally. This lifecycle limit is prominent in README/SECURITY/setup docs and catalog `known_issues`.
- Messages are primarily Korean. Windows/Ubuntu contract CI is not physical macOS, another user's GUI or a guarantee for every newer core. This is independent NewMetaW community code, not a Nous Research endorsement or fork of a bundled plugin.

## Historical evidence and admission limits

Independent source review: **GO C0/H0/M0/L0** for the exact 1.8.0 existing-SDK package. Actual Python214/Node149, native React DOM6 and exact-SHA Windows/Ubuntu CI passed. The author native installation/Doctor/settings readback and receiver heartbeat passed with config/auth preserved, but installed GUI interaction and human Telegram roundtrip remain unverified. Anonymous installation, full-history Gitleaks and public PM/dependency evidence described for 1.7.0 are historical; do not relabel them as new 1.8.0 execution. The historical metadata step exercised the catalog parser/admission API without changing production dependencies. Those checks did not establish maintainer admission, and this source consolidation does not rerun or reopen catalog submission.

A strict lazy-disabled standalone `--install-deps` fixture still printed dependency-preparation refusal while its validation report passed. That historical failure is retained and is **not** represented as complete dependency preparation. Production lazy-install/scanner/CAS/lease policy was not weakened. A future separately authorized catalog submission would require its own official CI and maintainer dependency/security review.

The catalog has a human-merged gate: a public release is not search visibility. Both submissions are now withdrawn; do not reopen them, repin entries or infer catalog admission from source integration. Historical direct exact-SHA installation remains documented in [public setup](public-install.md). Current 1.8.1 source scope is in [the maintenance plan](../PLAN.md). Production install/activation and live Telegram reply/click acceptance remain separate from source synchronization.
