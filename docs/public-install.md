# Public installation and setup

## Release status

The historical public MIT release **1.6.1** is pinned at `1a76ed54ccf8a279d4dc4d9bfe1553b90d81d5ca` and used two packages. **1.7.0 is an unreleased candidate**: no candidate SHA, CI result, catalog merge/admission, or production activation is claimed. Do not run the placeholder command until a reviewed full SHA has actually been published.

The candidate keeps the stable native ID `session-complete-telegram`, Desktop ID, fixed CLI command `desktop-telegram-bridge`, and `plugin-data/session-complete-telegram` state root. Hermes compatibility remains declared as `>=0.21.5`; this floor does not guarantee compatibility with every newer core. The reproducible test contract is Hermes core commit `74306c1d1b080837744faca3e371267dd09dda4b` with its locked environment.

## Before installing

1. Use Hermes **0.21.5 or later**. Select the **default** Hermes home/profile and complete Hermes's official [Telegram setup](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram) there.
2. Configure your own Telegram bot and private home. The plugin uses the existing credentials and destination; it never asks for or copies an author's token. Never put credentials into a command, issue, or report.
3. Protect the default Hermes home and `plugin-data/session-complete-telegram/` from other local OS users. On POSIX, use owner-only permissions; on Windows, verify the ACL is limited to your account, SYSTEM, and administrators.
4. Review the exact source commit and Hermes install-scanner findings before consenting to the declared Python dependencies. The Desktop renderer has app authority; this package is not a sandbox. Do not bypass security findings.

## Install one package

There is **one** candidate install command. Replace the variable only with a reviewed, published 40-character release SHA; the current unreleased candidate has no such SHA.

```bash
CANDIDATE_SHA=<reviewed-published-40-character-sha>
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/desktop-plugins/session-complete-telegram --ref "$CANDIDATE_SHA" --no-enable --yes-deps
```

`--yes-deps` is explicit consent for the reviewed manifest dependencies; `--no-enable` keeps the native package disabled. Let Hermes run its normal scanner and Doctor checks. The repository root is not itself a package.

## Enable the supported surfaces

The one native package registers the fixed CLI command and a Telegram handler factory through Hermes's public SDK. Enable the native package in the default home, then separately enable **Session Completion → Telegram** in Hermes Desktop:

```bash
hermes --profile default plugins enable session-complete-telegram
```

In the package's native **Plugins settings** page, two independent switches are available and both default to **off**:

- `replies_enabled`: clarify answers and ordinary completion-card followups.
- `approvals_enabled`: eligible **Allow Once / Deny** command-approval buttons.

Neither switch disables outbound completion/attention notifications, and neither switch inherits consent from the old package ID. The existing configured private-home owner, exact card, live route/session/request/queue checks and no-replay protections remain required. Approval capability uses `desktop-command-approval-v3`; an old cached v2 receiver, helper, or renderer fails closed. Approval never grants session/permanent permission and a delivered decision does not prove command completion.

The factory and handlers are loaded by the existing Telegram Gateway. If the native package is newly enabled or source was upgraded, use the Gateway's normal safe restart so it loads the registered factory; settings changes are checked live by existing handlers, but enabling a disabled package or replacing source is not a factory hot reload. No second poller is created.

## Explicit migration from the old two-package release

The repository retains `desktop-clarify-telegram` only as a deprecated inert compatibility stub. If that ID remains enabled, the unified factory and helper readiness fail closed with `legacy_package_migration_required` to prevent overlap with an installed or already-resident v1.6.1 factory.

For a deliberate migration: use Hermes's native CLI to disable `desktop-clarify-telegram`; enable `session-complete-telegram`; select only the optional settings you want; then normally restart the existing Gateway. Do not delete the old package/config entries or copy, migrate, or reset any SQLite files as part of the source change. Installation and Desktop/Gateway activation are separate states. A rollback to historical v1.6.1 restores its two-package runtime and therefore follows that historical release's pinned procedure; config, package data, ledgers and tombstones remain user-owned and are not rewritten automatically.

## Verify without sending a live message

```bash
hermes --profile default plugins list
hermes --profile default plugins doctor session-complete-telegram --ci
hermes --profile default desktop-telegram-bridge --help
```

Install/Doctor results do not prove Desktop enablement, Gateway handler activation, or message delivery. A live test notification sends a real Telegram message; do not trigger one unless you want that external message. Only same-default-home local routes and the configured private owner replying to an exact original card are supported.

Source tests use isolated fake homes and the pinned Hermes API contract; they are not evidence of a separate user's GUI, physical macOS behavior, production activation, or compatibility guarantees for newer core versions. See [operations notes](operations.md) for the activation and rollback boundary.
