# Hermes Telegram Bridge

Community-authored plugins that connect Hermes Desktop notifications and selected live-session replies to a Telegram bot already configured by the user in Hermes.

This is an independently authored NewMetaW project, not a fork of `telegram-notify`, not part of Hermes core, and not an official Nous Research product or endorsement. It contains two separately installed packages:

- `session-complete-telegram` — a Desktop component plus a fixed native CLI command for local notifications.
- `desktop-clarify-telegram` — an optional native ingress on the existing Telegram Gateway application; it does not start another polling client.

## What it does

- Sends one completion card after a persisted Desktop response has settled and the local session has been quiet for 60 seconds. The card includes a redacted final-response preview capped at 1,200 characters. Keep Desktop open and connected through that interval.
- Sends input-needed notices for supported pending requests. With the optional ingress enabled, clarify cards can accept a choice or text only for the matching live request and configured private-home owner. A reply to a verified completion card can be queued as ordinary text to that same live Desktop session.
- Uses local routes only. Remote/foreign homes, unrelated chats, and replies not attached to a verified card are outside the supported path.

**Command approvals (1.6.1):** eligible dangerous-command requests in the local Desktop approval queue can show **Allow Once / Deny** buttons. A click is bound to the original private-home owner, exact bot message, live route/session, and immutable request/queue IDs. Only one matching decision is delivered; session/permanent permission is never granted. Unverifiable, generic/plugin-rule, isolated compute-host, or unsafe/oversized review requests remain notification-only and must be answered in Desktop. A delivered decision is not proof that the command finished.

## Data and trust

The Desktop component reads persisted session titles, final replies, and clarify requests from local profiles under the same default Hermes home. Completion previews are redacted and bounded, but redaction is not an absolute guarantee. Sending a card transmits its contents to Telegram at `api.telegram.org`. Cards are copyable and forwardable (`protect_content: false`). Local SQLite ledgers retain delivery/deduplication records and some message-ID tombstones; temporary clarify content expires and is purged after its retention window. The plugin uses the user's own existing default-home Telegram setup; it does not copy, write, or rotate the bot token. No telemetry is sent to NewMetaW. Read [SECURITY.md](SECURITY.md) before installing.

The current messages and question cards are primarily Korean; this release does not claim complete localization across languages.

## Compatibility and availability

The minimum tested/declared Hermes compatibility floor is **0.21.5**. Reproducible CI uses the exact immutable Hermes core commit `74306c1d1b080837744faca3e371267dd09dda4b` and that checkout's `uv.lock`; a SemVer floor alone does not prove compatibility with every core generation. CI tests Windows and Ubuntu with Python 3.14 and Node 24. Physical macOS and a separate user's Hermes Desktop GUI have not been verified by those tests.

The plugin package code in this candidate reports version `1.6.1`. Public repository publication, a final release SHA, and maintainer-merged catalog entries are pending. Until those are published, the install and catalog documents are templates: do not use their pending SHA placeholders or assume the names are already discoverable in Hermes.

## Install

Read [Public installation and setup](docs/public-install.md). Both packages must be installed from their own subdirectories at the same reviewed full commit SHA; the repository root is not itself a plugin package. Configure your own Telegram bot and private home in the **default** Hermes home using Hermes's [official Telegram setup](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram). Never use or request an author's token.

Catalog listing is a separate maintainer-reviewed process. The two community-entry templates and submission checklist are in [Catalog submission](docs/catalog-submission.md).

## Maintainer references

These are retained development/operations references, not evidence that any production copy is installed or active:

- [Existing package README — test commands and implementation notes](desktop-plugins/session-complete-telegram/README.md#files-and-tests)
- [Historical native install/update/rollback and activation boundary](docs/operations.md)
