# Security policy

## Scope and trust boundary

Hermes Telegram Bridge is a community-authored plugin package, not part of Hermes core and not an endorsement by Nous Research. It runs code in the Hermes Desktop renderer and in the user's local Hermes plugin environment; it is not a sandbox for untrusted code. Install only a reviewed source commit and treat the package and its declared Python dependencies as trusted code.

The Desktop component reads persisted session titles, final assistant replies, and pending clarify requests from local Desktop profiles under the same default Hermes home. Completion previews are redacted and bounded, but redaction is pattern-based and is not a guarantee that every secret or personal detail will be removed. The configured Telegram bot sends notification/card content to `api.telegram.org`. Cards are deliberately copyable and forwardable (`protect_content` is false); Telegram recipients and anyone to whom a card is forwarded can see that content.

The bridge uses the user's existing Telegram configuration in the default Hermes home. It does not copy, write, rotate, or return the bot token, and it does not read or modify credentials belonging to other vendor tools. The token is consumed by the local sender and is not passed to the Desktop renderer. The renderer asks the fixed `desktop-telegram-bridge` CLI command to run through Hermes `cli.exec`; it does not supply an executable path, shell command, or environment. The sender is limited to the validated default local route and refuses named/foreign Hermes homes. The ingress adds handlers to the already-running Telegram Gateway `Application`; it does not create another Telegram polling client.

Inbound interactions are narrow and opt-in. Clarify callbacks and replies are accepted only for a matching card/request and the configured private-home owner, with the current bot/chat/topic and live Desktop session checked. Completion-card replies, where available, are submitted as ordinary text to that same live session. This is not unrestricted chat access and does not create permanent approvals. In the documented 1.5.0 baseline, command-approval notices are notification-only; Telegram cannot approve or deny them. A separate feature is planned for 1.6, unreleased/not included here: its intended actions are only approve once or deny for the matching pending request through Desktop's in-process approval queue, not generic host authorization. Do not assume it exists until 1.6 is integrated and verified.

Local SQLite files below `plugin-data/session-complete-telegram/` hold delivery/deduplication records, descriptors, and message-ID tombstones. Some minimal identifiers and terminal ledger rows are intentionally retained for duplicate/replay prevention and are not automatically purged. Clarify question/form content is removed after its retention window, which can extend up to 24 hours past card expiry; raw inbound reply text is cleared on settlement or expiry. The inboxes are plaintext local storage. Before enablement, protect the default home and bridge directories with owner-only POSIX permissions or a Windows ACL restricted to the owner, SYSTEM, and administrators. The current release does not automatically provision or verify this OS-level boundary; it must not be used from a shared/world-readable home. Disabling or uninstalling the packages does not require deleting these ledgers. Keep them if you want to preserve deduplication and safe rejection of old cards.

The source sends no telemetry to NewMetaW. The only intended external service call in the sender is to the user's configured Telegram Bot API at `api.telegram.org`.

## Reporting a vulnerability

Please do not include bot tokens, `.env` or `config.yaml` contents, private session dumps, database files, or unredacted Telegram messages in a report.

If GitHub private vulnerability reporting is enabled for the repository, use its [private reporting page](https://github.com/metamindedu/hermes-telegram-bridge/security/advisories/new). Its availability has not been confirmed in this documentation. If it is unavailable, open a minimal [GitHub issue](https://github.com/metamindedu/hermes-telegram-bridge/issues) asking the maintainers for a secure contact; do not publish exploit details or private user data in that issue.

Include the affected package ID, exact installed commit SHA, Hermes version/core commit if known, operating system, and a concise reproduction that uses synthetic data. There is no response-time SLA stated here. Do not post a public exploit until a maintainer has confirmed a safe disclosure path.

## Safe disablement

1. Disable the packages in the default Hermes home:

   ```bash
   hermes --profile default plugins disable session-complete-telegram
   hermes --profile default plugins disable desktop-clarify-telegram
   ```

2. Turn off **Session Completion → Telegram** in the Hermes Desktop Plugins screen. The Desktop component stops its subscriptions/timers when disabled. A running Gateway may keep the registered handler object until its next normal restart; the ingress checks its enabled state before processing interactions.
3. Leave `plugin-data/session-complete-telegram/` intact unless you intentionally accept losing its deduplication and old-card rejection history. Do not delete SQLite files as a routine disable step.

Disabling the ingress does not itself disable ordinary Desktop completion notifications; the two native packages and the Desktop toggle are independent controls.
