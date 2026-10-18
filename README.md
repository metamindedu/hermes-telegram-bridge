# Hermes Telegram Bridge

Community-authored plugins that connect Hermes Desktop notifications and selected live-session replies to a Telegram bot already configured by the user in Hermes.

This is an independently authored NewMetaW project, not a fork of `telegram-notify`, not part of Hermes core, and not an official Nous Research product or endorsement. The unified candidate uses ONE installable package, `session-complete-telegram`, for the Desktop component, fixed native CLI command, and optional handlers on the existing Telegram Gateway application. It never starts another polling client. `desktop-clarify-telegram` remains only as a deprecated inert compatibility stub; it is not a required package.

## Modular settings — public v1.8.0

The public 1.8.0 release adds a dedicated **Telegram Bridge** settings page, a common service switch and five separately selectable modules. It preserves the existing default-off replies/approvals consent and all local ownership/no-replay boundaries. The exact release pin is `4874276f519f68fdbacb9767f70c5ed760b5c6fb`; see [modular settings and activation](docs/modular-settings.md). Publication, catalog admission, installation and live activation remain separate states.

## What it does

- Sends one completion card after a persisted Desktop response has settled and the local session has been quiet for 60 seconds. The card includes a redacted final-response preview capped at 1,200 characters. Keep Desktop open through that interval.
- An unpublished source candidate adds bounded recovery only for a freshly recorded completion and the exact same still-live local runtime/connection, for up to 30 minutes. It never starts or resumes a profile session. Incomplete state checks remain pending; changed identity/new work cancels. An uncertain Telegram send is never replayed.
- Sends input-needed notices for supported pending requests. Optional clarify answers and completion-card followups are controlled by `replies_enabled` (default `false`). A reply is accepted only for the matching live request and configured private-home owner.
- Uses local routes only. Remote/foreign homes, unrelated chats, and replies not attached to a verified card are outside the supported path.

**Optional command approvals:** `approvals_enabled` (default `false`) controls **Allow Once / Deny** buttons. The public 1.8.0 release remains pinned to its immutable `desktop-command-approval-v4` contract. The unpublished 1.8.1 source candidate uses `desktop-command-approval-v6`; old/cached v4/v5 halves do not enable the expanded capability. It preserves the two legacy authority paths (`legacyqueue`, `legacyserver_request`) and adds a distinct `canonical_native` path for actual canonical approval prompts, including opaque prompt IDs and execution generations. Canonical handling uses an explicit ready owner discovered under the existing default-home/profile boundary, a read-only session subscription and a fresh full prompt snapshot. The fixed native CLI stores the frozen prompt/route/profile/instance/authority/replay/card/owner binding and sends exactly one native four-field `approval.respond`; Desktop never substitutes a cached generation or sends that canonical write itself. Only once/deny are offered. A typed `resolved` receipt means the Gateway handled that decision, not that a child request settled or a command completed. Stale, changed, expired, denied, oversized, or unsupported snapshots fail closed; an ambiguous post-write result is terminal and is never replayed. The native client/discovery utilities are exported and version/protocol-feature-detected but are not in the current documented-plugin-import allowlist (catalog lint warnings); this source candidate does not claim a stable SDK support guarantee or production GO. Existing `approvals_enabled` and all other user choices remain unchanged.

## Gateway failure boundary

The plugin uses the existing Hermes SDK; it neither patches core nor adds a poller. Its own interactions require installed handlers, a fresh receiver and exact live authorization. Recognized old-card text is rejected **when those consumers are installed**. If the host cannot install even the reject-only handler, the bridge refuses readiness, but Hermes may still route a card reply as ordinary Telegram chat. The plugin does not stop or control the whole Gateway. Do not reply to old cards during a Gateway/plugin failure; use Desktop and restore the normal Gateway before retrying. See [the security boundary](SECURITY.md#gateway-sdk-boundary) for the precise limitation.

## Data and trust

The Desktop component reads persisted session titles, final replies, and clarify requests from local profiles under the same default Hermes home. Completion previews are redacted and bounded, but redaction is not an absolute guarantee. Sending a card transmits its contents to Telegram at `api.telegram.org`. Cards are copyable and forwardable (`protect_content: false`). Local SQLite ledgers retain delivery/deduplication records and some message-ID tombstones; temporary clarify content expires and is purged after its retention window. The plugin uses the user's own existing default-home Telegram setup; it does not copy, write, or rotate the bot token. No telemetry is sent to NewMetaW. The source candidate's sender counters are separate from the receiver heartbeat and are not proof of Telegram delivery. Read [SECURITY.md](SECURITY.md) before installing.

The current messages and question cards are primarily Korean; this release does not claim complete localization across languages.

## Compatibility and availability

The minimum tested/declared Hermes compatibility floor is **0.21.5**. Reproducible CI uses the exact immutable Hermes core commit `74306c1d1b080837744faca3e371267dd09dda4b` and that checkout's `uv.lock`; a SemVer floor alone does not prove compatibility with every core generation. CI tests Windows and Ubuntu with Python 3.14 and Node 24. Physical macOS and a separate user's Hermes Desktop GUI have not been verified by those tests.

Version **1.8.0** is the public MIT **one-package** release at **`4874276f519f68fdbacb9767f70c5ed760b5c6fb`** ([release](https://github.com/metamindedu/hermes-telegram-bridge/releases/tag/v1.8.0)). The exact package has independent source GO C0/H0/M0/L0, Python214/Node149, actual native React DOM6 and successful Windows/Ubuntu commit-bound CI. The author installation separately verified matching package bytes, Doctor/native CLI, all seven settings and five effective modules, receiver heartbeat and unchanged profile config/auth. Actual installed GUI interaction and a human Telegram reply/approval roundtrip remain unverified; heartbeat is not delivery proof. Historical public **1.7.0** stays at `13c08c022965a589e87fe7198ab1a93201539d7e`, and **1.6.1** at `1a76ed54ccf8a279d4dc4d9bfe1553b90d81d5ca` used two packages. The earlier anonymous 1.7.0 installation and real test notification remain historical evidence, not new 1.8.0 roundtrip evidence. Immutable package docs retain their preparation-time status; current availability is recorded here.

The owner submission [catalog PR #135956](https://github.com/NousResearch/hermes-agent/pull/135956) tracks this single 1.8.0 release pin; the obsolete companion [#135957](https://github.com/NousResearch/hermes-agent/pull/135957) is withdrawn. Maintainer review/merge and official search visibility remain pending; direct installation is available now. A submitted update is not official catalog admission, and a public release is not proof of live Telegram interaction.

## Install

Read [Public installation and setup](docs/public-install.md). Install one package from `desktop-plugins/session-complete-telegram`, pinned to the reviewed release SHA. Configure your own Telegram bot and private home in the **default** Hermes home using Hermes's [official Telegram setup](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram). Never use or request an author's token.

See [Catalog submission](docs/catalog-submission.md) for the historical v1.6.1 record and release boundary.

## Maintainer references

These are retained development/operations references, not evidence that any production copy is installed or active:

- [Existing package README — test commands and implementation notes](desktop-plugins/session-complete-telegram/README.md#files-and-tests)
- [Historical native install/update/rollback and activation boundary](docs/operations.md)
