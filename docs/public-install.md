# Public installation and setup

## Release status

Public MIT **1.8.0** is one installable package pinned at `4874276f519f68fdbacb9767f70c5ed760b5c6fb` ([release](https://github.com/metamindedu/hermes-telegram-bridge/releases/tag/v1.8.0)). Exact-source independent review, Python214/Node149, native React DOM6 and Windows/Ubuntu CI passed. Catalog PR #135956 tracks this pin but still requires maintainer admission/merge; obsolete companion #135957 is closed. Direct pinned installation is available before catalog search. Historical 1.7.0 remains at `13c08c022965a589e87fe7198ab1a93201539d7e`; its anonymous installation/test-send evidence is not a new 1.8.0 live roundtrip. The author 1.8.0 installation has separate package/Doctor/native CLI, settings, receiver-heartbeat and config/auth preservation checks. Actual installed GUI interaction and human reply/approval clicks remain unverified; heartbeat does not prove message delivery.

The candidate keeps the stable native ID `session-complete-telegram`, Desktop ID, fixed CLI command `desktop-telegram-bridge`, and `plugin-data/session-complete-telegram` state root. Hermes compatibility remains declared as `>=0.21.5`; this floor does not guarantee compatibility with every newer core. The reproducible test contract is Hermes core commit `74306c1d1b080837744faca3e371267dd09dda4b` with its locked environment.

## Before installing

1. Use Hermes **0.21.5 or later**. Select the **default** Hermes home/profile and complete Hermes's official [Telegram setup](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram) there.
2. Configure your own Telegram bot and private home. The plugin uses the existing credentials and destination; it never asks for or copies an author's token. Never put credentials into a command, issue, or report.
3. Protect the default Hermes home and `plugin-data/session-complete-telegram/` from other local OS users. On POSIX, use owner-only permissions; on Windows, verify the ACL is limited to your account, SYSTEM, and administrators.
4. Review the exact source commit and Hermes install-scanner findings before consenting to the declared Python dependencies. The Desktop renderer has app authority; this package is not a sandbox. Do not bypass security findings.

## Install one package

There is **one** install command. Keep the full immutable release SHA:

```bash
CANDIDATE_SHA=4874276f519f68fdbacb9767f70c5ed760b5c6fb
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/desktop-plugins/session-complete-telegram --ref "$CANDIDATE_SHA" --no-enable --yes-deps
```

`--yes-deps` is explicit consent for the reviewed manifest dependencies; `--no-enable` keeps the native package disabled. Let Hermes run its normal scanner and Doctor checks. The repository root is not itself a package.

## Enable the supported surfaces

The one native package registers the fixed CLI command and a Telegram handler factory through Hermes's public SDK. Enable the native package in the default home, then separately enable **Session Completion → Telegram** in Hermes Desktop:

```bash
hermes --profile default plugins enable session-complete-telegram
```

The dedicated **Telegram Bridge** settings page exposes the common `service_enabled` switch and five effective modules: completion notifications, attention notifications, clarify replies, completion-card followups and command approvals. Seven native boolean settings back these controls; see [all defaults and parent gates](modular-settings.md#controls-and-preserved-defaults).

The existing interaction consents remain independent and **off by default**:

- `replies_enabled`: required for clarify answers and ordinary completion-card followups.
- `approvals_enabled`: required for eligible **Allow Once / Deny** command-approval buttons.

The common service/native selection and relevant parent-notification switches must also be enabled. Installing code never grants these consents or inherits them from the old package ID. Turning a module off retires affected cards; turning it back on does not revive them or replay consumed input. The configured private-home owner, exact card/live route/session/request/queue checks and no-replay protections remain mandatory. Approval capability uses `desktop-command-approval-v4`; mixed older/newer action-handshake components fail closed. Approval never grants session/permanent permission, and delivery is not command completion.

The factory and handlers are loaded by the existing Telegram Gateway. If the native package is newly enabled or source was upgraded, use the Gateway's normal safe restart so it loads the registered factory; settings changes are checked live by existing handlers, but enabling a disabled package or replacing source is not a factory hot reload. No second poller is created.

## When Gateway initialization fails

If the receiver is unavailable or plugin handlers failed to load, do not reply to old cards: Hermes may process that text as ordinary Telegram chat. Bridge actions still require live authorization; the plugin does not stop the whole Gateway or guarantee general-input filtering when the host cannot install its guard. Use Desktop and recover the existing Gateway normally, then use a new verified card. This SDK boundary is disclosed in [SECURITY.md](../SECURITY.md#gateway-sdk-boundary); no core modification is required to install this package.

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
