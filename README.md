# Hermes Telegram Bridge

Community-authored plugins that connect Hermes Desktop notifications and selected live-session replies to a Telegram bot already configured by the user in Hermes.

This is an independently authored NewMetaW project, not a fork of `telegram-notify`, not part of Hermes core, and not an official Nous Research product or endorsement. The unified candidate uses ONE installable package, `session-complete-telegram`, for the Desktop component, fixed native CLI command, and optional handlers on the existing Telegram Gateway application. It never starts another polling client. `desktop-clarify-telegram` remains only as a deprecated inert compatibility stub; it is not a required package.

## What it does

- Sends one completion card after a persisted Desktop response has settled and the local session has been quiet for 60 seconds. The card includes a redacted final-response preview capped at 1,200 characters. Keep Desktop open and connected through that interval.
- Sends input-needed notices for supported pending requests. Optional clarify answers and completion-card followups are controlled by `replies_enabled` (default `false`). A reply is accepted only for the matching live request and configured private-home owner.
- Uses local routes only. Remote/foreign homes, unrelated chats, and replies not attached to a verified card are outside the supported path.

**Optional command approvals:** `approvals_enabled` (default `false`) controls **Allow Once / Deny** buttons for eligible local Desktop command approvals. The 1.7.0 candidate requires the `desktop-command-approval-v3` versioned handshake; old cached v2 receivers/helpers/renderers do not match. A click remains bound to the exact owner/card/route/session/request/queue, grants no session/permanent permission, and never proves command completion.

## Gateway failure boundary

The plugin uses the existing Hermes SDK; it neither patches core nor adds a poller. Its own interactions require installed handlers, a fresh receiver and exact live authorization. Recognized old-card text is rejected **when those consumers are installed**. If the host cannot install even the reject-only handler, the bridge refuses readiness, but Hermes may still route a card reply as ordinary Telegram chat. The plugin does not stop or control the whole Gateway. Do not reply to old cards during a Gateway/plugin failure; use Desktop and restore the normal Gateway before retrying. See [the security boundary](SECURITY.md#gateway-sdk-boundary) for the precise limitation.

## Data and trust

The Desktop component reads persisted session titles, final replies, and clarify requests from local profiles under the same default Hermes home. Completion previews are redacted and bounded, but redaction is not an absolute guarantee. Sending a card transmits its contents to Telegram at `api.telegram.org`. Cards are copyable and forwardable (`protect_content: false`). Local SQLite ledgers retain delivery/deduplication records and some message-ID tombstones; temporary clarify content expires and is purged after its retention window. The plugin uses the user's own existing default-home Telegram setup; it does not copy, write, or rotate the bot token. No telemetry is sent to NewMetaW. Read [SECURITY.md](SECURITY.md) before installing.

The current messages and question cards are primarily Korean; this release does not claim complete localization across languages.

## Compatibility and availability

The minimum tested/declared Hermes compatibility floor is **0.21.5**. Reproducible CI uses the exact immutable Hermes core commit `74306c1d1b080837744faca3e371267dd09dda4b` and that checkout's `uv.lock`; a SemVer floor alone does not prove compatibility with every core generation. CI tests Windows and Ubuntu with Python 3.14 and Node 24. Physical macOS and a separate user's Hermes Desktop GUI have not been verified by those tests.

Version **1.6.1** is the historical public MIT release at the reviewed pin **`1a76ed54ccf8a279d4dc4d9bfe1553b90d81d5ca`** ([release](https://github.com/metamindedu/hermes-telegram-bridge/releases/tag/v1.6.1)); it used two packages. Version **1.7.0 is an unreleased candidate** and has no published SHA, CI result, catalog admission, or production approval claimed here.

The v1.6.1 catalog submissions are historical and not evidence for v1.7.0 admission. Parent-owned catalog/PR updates remain pending Heavy review and a separately approved public release; do not assume either name is currently discoverable through `hermes plugins search`.

## Install

Read [Public installation and setup](docs/public-install.md). The candidate uses one install command from the existing `desktop-plugins/session-complete-telegram` subdirectory, pinned to a reviewed full SHA after release. Configure your own Telegram bot and private home in the **default** Hermes home using Hermes's [official Telegram setup](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram). Never use or request an author's token.

See [Catalog submission](docs/catalog-submission.md) for the historical v1.6.1 record and release boundary.

## Maintainer references

These are retained development/operations references, not evidence that any production copy is installed or active:

- [Existing package README — test commands and implementation notes](desktop-plugins/session-complete-telegram/README.md#files-and-tests)
- [Historical native install/update/rollback and activation boundary](docs/operations.md)
