# Security policy

## Scope and trust boundary

Hermes Telegram Bridge is a community-authored plugin package, not part of Hermes core and not an endorsement by Nous Research. It runs code in the Hermes Desktop renderer and in the user's local Hermes plugin environment; it is not a sandbox for untrusted code. Install only a reviewed source commit and treat the package and its declared Python dependencies as trusted code.

The Desktop component reads persisted session titles, final assistant replies, and pending clarify requests from local Desktop profiles under the same default Hermes home. Completion previews are redacted and bounded, but redaction is pattern-based and is not a guarantee that every secret or personal detail will be removed. The configured Telegram bot sends notification/card content to `api.telegram.org`. Cards are deliberately copyable and forwardable (`protect_content` is false); Telegram recipients and anyone to whom a card is forwarded can see that content.

The bridge uses the user's existing Telegram configuration in the default Hermes home. It does not copy, write, rotate, or return the bot token, and it does not read or modify credentials belonging to other vendor tools. The token is consumed by the local sender and is not passed to the Desktop renderer. The renderer asks the fixed `desktop-telegram-bridge` CLI command to run through Hermes `cli.exec`; it does not supply an executable path, shell command, or environment. The sender is limited to the validated default local route and refuses named/foreign Hermes homes. The ingress adds handlers to the already-running Telegram Gateway `Application`; it does not create another Telegram polling client.

Inbound interactions are narrow and opt-in. Clarify callbacks and replies are accepted only for a matching card/request and the configured private-home owner, with the current bot/chat/topic and live Desktop session checked. Completion-card replies, where available, are submitted as ordinary text to that same live session. This is not unrestricted chat access and does not create permanent approvals. Version 1.7.0's single package defaults both `replies_enabled` and `approvals_enabled` to false and checks them independently. Its versioned approval-v3 handshake refuses the old resident v2 receiver/helper/renderer combination. An enabled legacy `desktop-clarify-telegram` ID blocks both interaction features until explicit disablement and a normal Gateway restart; installation never automatically removes consent/config or changes data. Its historical approval contract was limited to eligible in-process dangerous-command queue entries. No session/permanent policy is written; no callback directly executes shell text. A receiver/owner restart or lease gap invalidates approval authority; queued decisions are never automatically replayed after a gap. Native response ambiguity remains terminal unknown rather than being retried.

The unpublished 1.8.1 source candidate (not the public 1.8.0 release) keeps the native compatibility value `1.8.0` but requires `desktop-command-approval-v5`. It can offer once/deny for a live native `approval` request when the exact parent queue row matches, or when a healthy, fully validated `approval.pending` snapshot has no matching parent row. A failed, malformed, duplicate, or conflicting queue snapshot never proves absence. Queue-backed dispatch uses `approval.respond {session_id, request_id:queue_id, choice, all:false}`; server-request-backed dispatch uses the frozen `request.answer {id:srq, result:{choice, all:false}}`. A typed `request.answer status:"ok"` is only Gateway acceptance; it settles terminally as uncertain and never claims child queue settlement or command completion. The request-based path covers the native producer summary for shell/Python including `execute_code`, protected instruction and SSH writes, plugin rules, CUA, smart-denied overrides, MCP elicitation, and editor edits; it cannot reveal producer arguments/diffs that were not supplied. Review shows all supplied redacted fields and fails closed above 4,096 UTF-16 units rather than truncating. `approvals_enabled` remains default-off and is never changed by this candidate.

Local SQLite files below `plugin-data/session-complete-telegram/` hold delivery/deduplication records, descriptors, and message-ID tombstones. The separate `approval-inbox` holds temporary review/request data and one-shot decisions; terminal approval rows clear review/request content while retaining replay-prevention identifiers. Some minimal identifiers and terminal ledger rows are intentionally retained for duplicate/replay prevention and are not automatically purged. Clarify question/form content is removed after its retention window, which can extend up to 24 hours past card expiry; raw inbound reply text is cleared on settlement or expiry. The inboxes are plaintext local storage. Before enablement, protect the default home and bridge directories with owner-only POSIX permissions or a Windows ACL restricted to the owner, SYSTEM, and administrators. The current release does not automatically provision or verify this OS-level boundary; it must not be used from a shared/world-readable home. Disabling or uninstalling the packages does not require deleting these ledgers. Keep them if you want to preserve deduplication and safe rejection of old cards.

The source sends no telemetry to NewMetaW. The only intended external service call in the sender is to the user's configured Telegram Bot API at `api.telegram.org`.

## Gateway SDK boundary

Bridge authorization and whole-platform input admission are different. The plugin retains exact owner/card/session/request/queue checks, independent default-off consent, live leases and last claim validity, one-shot decisions, `all:false`, and no ambiguous replay. Invalid/unreadable managed policy denies bridge actions; an enabled, correctly owned factory still attempts to install known-card consumers so a policy error does not itself remove that boundary.

Known-card rejection requires those handlers to be installed on the existing Telegram application. After an initial installation exception, the plugin checks the exact public handler registration; it retains an attached guard, or attempts one bounded local pre-attachment retry. If neither attempt attaches the reject-only handler, the plugin logs failure and refuses interaction readiness. It does not grant approval authority or submit a bridge answer. The current Hermes host isolates factory failures and may continue **ordinary Telegram input**, including a plain-text reply to an old card. This is a documented SDK lifecycle limit, not a guarantee that the platform stopped, and ordinary input is not a plugin approval receipt. The general agent remains subject to its own normal authorization rules.

The user-approved release scope excludes a core/SDK patch, a private handler-store override, a second poller, unbounded retries, or stopping the shared Gateway to turn this limit into a platform-wide guarantee. Do not answer old cards while the receiver is unavailable or Gateway/plugin initialization has failed. Use Desktop, inspect the initialization error, and perform a normal safe Gateway recovery before using new cards. Disabling/uninstalling the package likewise does not make the plugin an input firewall for the remaining Gateway.

## Reporting a vulnerability

Please do not include bot tokens, `.env` or `config.yaml` contents, private session dumps, database files, or unredacted Telegram messages in a report.

If GitHub private vulnerability reporting is enabled for the repository, use its [private reporting page](https://github.com/metamindedu/hermes-telegram-bridge/security/advisories/new). Its availability has not been confirmed in this documentation. If it is unavailable, open a minimal [GitHub issue](https://github.com/metamindedu/hermes-telegram-bridge/issues) asking the maintainers for a secure contact; do not publish exploit details or private user data in that issue.

Include the affected package ID, exact installed commit SHA, Hermes version/core commit if known, operating system, and a concise reproduction that uses synthetic data. There is no response-time SLA stated here. Do not post a public exploit until a maintainer has confirmed a safe disclosure path.

## Safe disablement

1. Disable the one native package in the default Hermes home:

   ```bash
   hermes --profile default plugins disable session-complete-telegram
   ```

   For a historical two-package installation, also explicitly disable `desktop-clarify-telegram`. Do not remove its configuration or data automatically.

2. Turn off **Session Completion → Telegram** in the Hermes Desktop Plugins screen. The Desktop component stops its subscriptions/timers when disabled. A running Gateway may keep the registered handler object until its next normal restart; the ingress checks its enabled state before processing interactions.
3. Leave `plugin-data/session-complete-telegram/` intact unless you intentionally accept losing its deduplication and old-card rejection history. Do not delete SQLite files as a routine disable step.

Turning off `replies_enabled` or `approvals_enabled` blocks that interaction independently without disabling ordinary completion notifications. The one native package toggle and the Desktop renderer toggle are separate controls. A resident handler set checks live settings; upgrading/enabling the native package still needs a normal Gateway restart to load its factory.
