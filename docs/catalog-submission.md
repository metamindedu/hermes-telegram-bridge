# Hermes plugin catalog submissions

The two **community** entries were submitted separately, one catalog file per PR, to the official Hermes repository:
- [#135956 — session-complete-telegram](https://github.com/NousResearch/hermes-agent/pull/135956)
- [#135957 — desktop-clarify-telegram](https://github.com/NousResearch/hermes-agent/pull/135957)

Both submissions are open and pin the publicly released MIT code `1a76ed54ccf8a279d4dc4d9bfe1553b90d81d5ca` / version `1.6.1`. Submission is not admission: human maintainer review/merge and real discovery/install checks remain required. Direct pinned installation is already verified without GitHub credentials.

The two open PRs' GitHub Actions currently report `action_required`: a repository maintainer must approve those fork workflows before CI can execute. No passing upstream catalog CI or maintainer admission is claimed.

The official admission sources read for this preparation are the current [`plugin-catalog/README.md`](https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/README.md) and the [catalog submission guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission). A catalog entry is a trust signal only after maintainer review and merge; a template, direct Git install, or successful local test is not catalog admission or Nous endorsement.

## Required release pin

Both entries pin **`1a76ed54ccf8a279d4dc4d9bfe1553b90d81d5ca`**, the immutable public v1.6.1 package source, rather than the moving default branch or the release tag. The current main branch may contain publication-status documentation updates without changing this reviewed package pin. Future releases need a new reviewed full SHA and maintainer SHA-bump PR.

## Entry 1 — Desktop component and fixed CLI helper

```yaml
# Submitted immutable pin; companion entry uses the same SHA.
name: session-complete-telegram
repo: https://github.com/metamindedu/hermes-telegram-bridge
sha: "1a76ed54ccf8a279d4dc4d9bfe1553b90d81d5ca"
subdir: desktop-plugins/session-complete-telegram
description: Sends settled Hermes Desktop completion and input-needed notices through the user's configured Telegram home.
maintainer: NewMetaW
tier: community
category: desktop
requires_hermes: ">=0.21.5"
version: "1.6.1"
capabilities:
  provides_tools: []
  provides_hooks: []
  provides_middleware: []
  requires_env: []
```

At this pinned code the package's `register(ctx)` registers one fixed CLI command, `desktop-telegram-bridge`, through `ctx.register_cli_command`. That registration is not a Hermes tool, hook, or middleware, so do not invent values in those three catalog capability arrays. The package does not declare `requires_env` or a `config_schema`; the existing Telegram setup read is a separate user-configuration dependency and must be disclosed explicitly as described below.

## Entry 2 — Optional existing-Gateway ingress

```yaml
# Submitted immutable pin; Desktop entry uses the same SHA.
name: desktop-clarify-telegram
repo: https://github.com/metamindedu/hermes-telegram-bridge
sha: "1a76ed54ccf8a279d4dc4d9bfe1553b90d81d5ca"
subdir: plugins/desktop-clarify-telegram
description: Adds opt-in, narrowly scoped clarify-card and completion-reply handlers to the existing Telegram Gateway.
maintainer: NewMetaW
tier: community
category: platform
requires_hermes: ">=0.21.5"
version: "1.6.1"
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

Review every scanner warning, verify the declared version and capability arrays against the pinned source, and do not bypass a dangerous finding. The submitted YAML files parse with the current official loader and both direct admission API reports passed without warnings. Public native installations exercised declared dependency consent and passed. An additional cold, strict-lazy-disabled fixture could not finish the CLI `--install-deps` preparation; explicit native PM preparation resolved the two candidates, but a later single-candidate implicit rebuild was refused by that fixture policy. Those failures are retained and are not counted as a successful complete CLI admission run. Official catalog CI and maintainer acceptance remain separate, pending gates.

## Disclosure for the maintainer PR

The PR and the pinned README should disclose all of the following, even if a catalog scanner does not infer them from code:

- The sender reads the user's existing default Hermes home's Telegram configuration (`TELEGRAM_BOT_TOKEN` and `TELEGRAM_HOME_CHANNEL`) created through Hermes's official setup. It sends card/preview contents to `https://api.telegram.org` using that user's bot and destination. The package neither provisions nor copies, writes, rotates, or returns the token; the renderer does not receive it.
- The Desktop component locally reads persisted session titles, final assistant responses, and pending clarify request text/choices from local profiles under that same default home. It waits for a 60-second settled quiet period; completion previews are redacted and capped at 1,200 characters, but redaction is not an absolute guarantee.
- Telegram cards are intentionally copyable/forwardable (`protect_content: false`). The package stores local SQLite delivery/deduplication data, descriptors, and message-ID tombstones; clarify question/form content may remain until up to 24 hours after expiry, while raw inbound reply text is cleared on settlement or expiry.
- The renderer uses a fixed argv for `desktop-telegram-bridge` through the Hermes `cli.exec` route. It does not supply an arbitrary executable, shell command, or environment, and the helper refuses named/foreign homes. The native ingress is opt-in, uses the existing Gateway `Application`, and accepts only replies/callbacks tied to exact cards and the configured private owner/live session. It is not unrestricted chat access or a permanent approval mechanism.
- Version 1.6.1 provides Allow Once / Deny only for eligible dangerous-command approvals in Desktop's local in-process queue, with original-message/private-owner and live-request authority. It requires matching version/capability handshakes, including the read-only final claim-validity gate before dispatch; that gate is not atomic server-side fingerprint CAS. Generic/plugin-rule/compute-host or unsafe/oversized review requests remain notification-only. Exact queue IDs and `all:false` prevent broad approval; no session/permanent rights are written. Ambiguous delivery is never replayed, and a delivered decision is not command-completion evidence. Approval review/state uses a separate local inbox.
- The source sends no telemetry to NewMetaW. The current user-facing notices/cards are primarily Korean; do not claim complete localization.
- The package is independently authored by NewMetaW and is not a fork of `telegram-notify`, a bundled Hermes plugin, or a Nous Research product. The listing must be `community`, not `official`; Nous Research has not endorsed it.

Because credentials are read from the user's existing default-home setup rather than declared as package-owned `requires_env` or `config_schema`, do not assume the catalog validator will discover or disclose that read automatically. Keep the capability arrays above aligned with the actual `ctx` registrations and ask the catalog maintainer to confirm any current-schema requirement at review time.

## Admission and update boundaries

The two catalog entries are distinct IDs and must each point at their own `subdir`; `owner/repo` at the repository root is not an installable plugin package because there is no root `plugin.yaml`. Both entries are `community` listings and must use the same exact release SHA. The official catalog's human-merged review and scanner are separate from publication of the source repository. After a maintainer accepts a listing, catalog updates still require a reviewed SHA-bump PR; the packages do not self-update.

The parent maintainer performed repository publication, anonymous pinned installation and both entry submissions after final integration and verification. Those are distinct from maintainer admission, catalog search visibility and production installation/activation; none of the latter is implied by the public source or an open PR.
