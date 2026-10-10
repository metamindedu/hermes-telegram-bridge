# Hermes plugin catalog submission templates

This file records two **community** catalog-entry templates for the two distinct native packages in this repository. It does not create catalog entries or make either package discoverable. Each package must be admitted through a human-reviewed PR to the official Hermes catalog, and a maintainer must merge that PR. The repository owner will submit only after the public release snapshot is finalized and both packages validate at that exact commit.

The official admission sources read for this preparation are the current [`plugin-catalog/README.md`](https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/README.md) and the [catalog submission guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission). A catalog entry is a trust signal only after maintainer review and merge; a template, direct Git install, or successful local test is not catalog admission or Nous endorsement.

## Required release pin

Use the **same exact 40-character SHA** in both entries, and ensure it is the published commit containing the code and version shown below. The final release SHA is intentionally unknown here. The value `PENDING` is a visible placeholder, not an installable pin. Do not substitute the isolated baseline SHA, a branch, a tag, a short SHA, or a SHA guessed before the final release exists. The version `1.6.0` below is read from both package manifests and the Desktop entry in this baseline; update it only if the final pinned code reports a different version.

## Entry 1 — Desktop component and fixed CLI helper

```yaml
# TEMPLATE ONLY — replace sha with the exact final release SHA before submission.
name: session-complete-telegram
repo: https://github.com/metamindedu/hermes-telegram-bridge
sha: "PENDING: exact 40-character release SHA"
subdir: desktop-plugins/session-complete-telegram
description: Sends settled Hermes Desktop completion and input-needed notices through the user's configured Telegram home.
maintainer: NewMetaW
tier: community
category: desktop
requires_hermes: ">=0.21.5"
version: "1.6.0"
capabilities:
  provides_tools: []
  provides_hooks: []
  provides_middleware: []
  requires_env: []
```

At this pinned code the package's `register(ctx)` registers one fixed CLI command, `desktop-telegram-bridge`, through `ctx.register_cli_command`. That registration is not a Hermes tool, hook, or middleware, so do not invent values in those three catalog capability arrays. The package does not declare `requires_env` or a `config_schema`; the existing Telegram setup read is a separate user-configuration dependency and must be disclosed explicitly as described below.

## Entry 2 — Optional existing-Gateway ingress

```yaml
# TEMPLATE ONLY — replace sha with the exact same final release SHA as entry 1.
name: desktop-clarify-telegram
repo: https://github.com/metamindedu/hermes-telegram-bridge
sha: "PENDING: exact 40-character release SHA"
subdir: plugins/desktop-clarify-telegram
description: Adds opt-in, narrowly scoped clarify-card and completion-reply handlers to the existing Telegram Gateway.
maintainer: NewMetaW
tier: community
category: platform
requires_hermes: ">=0.21.5"
version: "1.6.0"
capabilities:
  provides_tools: []
  provides_hooks: []
  provides_middleware: []
  requires_env: []
```

At this pinned code `register(ctx)` registers one factory through `ctx.register_telegram_handler`; the factory attaches scoped handlers to the existing Telegram `Application`. It does not create a second poller. This platform-handler registration is not a tool, hook, or middleware, so the corresponding capability arrays remain empty. The current manifest likewise declares no plugin-owned `requires_env` or `config_schema`.

Before submitting, run the official validator on **each exact package subdirectory** at the commit to be pinned:

```bash
hermes plugins validate /path/to/checkout/desktop-plugins/session-complete-telegram --install-deps
hermes plugins validate /path/to/checkout/plugins/desktop-clarify-telegram --install-deps
```

Review every scanner warning, verify the declared version and capability arrays against the pinned source, and do not bypass a dangerous finding. Any final catalog YAML must use the real full SHA and values accepted by the current official validator; these documentation templates are deliberately not validator-ready.

## Disclosure for the maintainer PR

The PR and the pinned README should disclose all of the following, even if a catalog scanner does not infer them from code:

- The sender reads the user's existing default Hermes home's Telegram configuration (`TELEGRAM_BOT_TOKEN` and `TELEGRAM_HOME_CHANNEL`) created through Hermes's official setup. It sends card/preview contents to `https://api.telegram.org` using that user's bot and destination. The package neither provisions nor copies, writes, rotates, or returns the token; the renderer does not receive it.
- The Desktop component locally reads persisted session titles, final assistant responses, and pending clarify request text/choices from local profiles under that same default home. It waits for a 60-second settled quiet period; completion previews are redacted and capped at 1,200 characters, but redaction is not an absolute guarantee.
- Telegram cards are intentionally copyable/forwardable (`protect_content: false`). The package stores local SQLite delivery/deduplication data, descriptors, and message-ID tombstones; clarify question/form content may remain until up to 24 hours after expiry, while raw inbound reply text is cleared on settlement or expiry.
- The renderer uses a fixed argv for `desktop-telegram-bridge` through the Hermes `cli.exec` route. It does not supply an arbitrary executable, shell command, or environment, and the helper refuses named/foreign homes. The native ingress is opt-in, uses the existing Gateway `Application`, and accepts only replies/callbacks tied to exact cards and the configured private owner/live session. It is not unrestricted chat access or a permanent approval mechanism.
- Version 1.6 provides Allow Once / Deny only for eligible dangerous-command approvals in Desktop's local in-process queue, with original-message/private-owner and live-request authority. Generic/plugin-rule/compute-host or unsafe/oversized review requests remain notification-only. Exact queue IDs and `all:false` prevent broad approval; no session/permanent rights are written. Ambiguous delivery is never replayed, and a delivered decision is not command-completion evidence. Approval review/state uses a separate local inbox.
- The source sends no telemetry to NewMetaW. The current user-facing notices/cards are primarily Korean; do not claim complete localization.
- The package is independently authored by NewMetaW and is not a fork of `telegram-notify`, a bundled Hermes plugin, or a Nous Research product. The listing must be `community`, not `official`; Nous Research has not endorsed it.

Because credentials are read from the user's existing default-home setup rather than declared as package-owned `requires_env` or `config_schema`, do not assume the catalog validator will discover or disclose that read automatically. Keep the capability arrays above aligned with the actual `ctx` registrations and ask the catalog maintainer to confirm any current-schema requirement at review time.

## Admission and update boundaries

The two catalog entries are distinct IDs and must each point at their own `subdir`; `owner/repo` at the repository root is not an installable plugin package because there is no root `plugin.yaml`. Both entries are `community` listings and must use the same exact release SHA. The official catalog's human-merged review and scanner are separate from publication of the source repository. After a maintainer accepts a listing, catalog updates still require a reviewed SHA-bump PR; the packages do not self-update.

Do not create a catalog file, open a PR, change repository visibility, or claim a listing from this worktree. Those publication and admission steps are owned by the parent maintainer after final integration and verification.
