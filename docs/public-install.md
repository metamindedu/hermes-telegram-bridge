# Public installation and setup

The MIT public release **1.6.1** is available at exact reviewed package commit **`1a76ed54ccf8a279d4dc4d9bfe1553b90d81d5ca`**. Both package subdirectories were installed anonymously from GitHub with stored credentials/helpers disabled, verified byte-identical, and passed Doctor and CLI checks in an isolated home. Official catalog submissions [#135956](https://github.com/NousResearch/hermes-agent/pull/135956) and [#135957](https://github.com/NousResearch/hermes-agent/pull/135957) are open; maintainer admission/search visibility is not yet claimed. Install directly with the full pin below, not a moving branch, tag or short SHA.

## Before installing

1. Use Hermes **0.21.5 or later**. The reproducible test contract is the immutable Hermes core commit `74306c1d1b080837744faca3e371267dd09dda4b` with its locked environment; the version floor by itself is not proof that a different core generation has the same API contract.
2. In the Hermes Desktop app, select the **default** profile/home and finish Hermes's official [Telegram setup](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram) in that home. The bridge consumes the existing bot token and configured Telegram home from that user's setup; it does not issue, copy, or rotate credentials. Never paste a token into a command, issue, or report. The local machine also needs outbound HTTPS access to `api.telegram.org` for delivery; no connection to NewMetaW is used.
3. For inbound clarify/completion replies, configure a private Telegram home owned by the intended user. The ingress requires the exact private owner/chat identity and a live Desktop session. A public group/channel is not an authorized inbound reply surface.
4. Protect the default Hermes home and its bridge storage against other local OS users before enabling the plugin. The bridge keeps plaintext local request/receipt data under `plugin-data/session-complete-telegram/`. On POSIX, the containing directories must be owner-only (mode `0700`); on Windows, verify the directory ACL grants access only to your account, SYSTEM, and administrators. The plugin does not claim to provision or audit those OS permissions for you. Do not enable it in a shared/world-readable home.
5. Review the exact repository commit and the install scanner's results before consenting to its declared Python dependencies. The Desktop renderer has the app's authority; this package is not a sandbox. Do not bypass a dangerous scanner result or disable security checks.

Hermes's official setup guide is the source of truth for creating/configuring a bot. The bridge never uses an author's bot or home.

## Install both packages without enabling them

Each package lives in a different subdirectory; the root has no `plugin.yaml` and must not be installed as one package. Use one matching, verified full SHA for both commands:

```bash
CANDIDATE_SHA=1a76ed54ccf8a279d4dc4d9bfe1553b90d81d5ca

hermes --profile default plugins install metamindedu/hermes-telegram-bridge/desktop-plugins/session-complete-telegram --ref "$CANDIDATE_SHA" --no-enable --yes-deps
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/plugins/desktop-clarify-telegram --ref "$CANDIDATE_SHA" --no-enable --yes-deps
```

`--yes-deps` is explicit consent for the reviewed package dependencies; `--no-enable` keeps the native packages disabled. Let Hermes run its normal install/security validation and read any caution findings. Do not add a generic force flag or disable the scanner to get past a finding. This direct Git path is usable only after the owner has actually published the repository and verified SHA.

## Enable only the surfaces you want

The two native packages and the Desktop component have independent enablement:

```bash
# Required for the Desktop helper/CLI command:
hermes --profile default plugins enable session-complete-telegram

# Optional: needed only for inbound clarify-card and completion-card replies:
hermes --profile default plugins enable desktop-clarify-telegram
```

Then, in Hermes Desktop's Plugins screen, enable **Session Completion → Telegram**. This renderer toggle is separate from enabling the native CLI package. Completion notices need the first package and the Desktop toggle. Inbound clarify answers additionally need the second package and the existing Telegram Gateway to be running. The ingress registers handlers on that Gateway's existing `Application`; it does not create another update poller.

Version 1.6.1 supports **Allow Once / Deny** on eligible dangerous-command requests in Desktop's local in-process queue. This needs both matching 1.6.1 packages, the Desktop component, and a fresh `desktop-command-approval-v2` receiver/version/capability handshake. After an approved code update, restart the existing Telegram Gateway at a safe idle point so its resident factory loads the matching handlers; source replacement or plugin rediscovery alone is not a handler-upgrade guarantee. Unverifiable, generic/plugin-rule/compute-host and unsafe review requests remain notification-only. A decision receipt is not command-completion evidence. No session/permanent permissions are offered.

## Verify and use

After installation, check both native packages in the default home with Hermes's normal commands:

```bash
hermes --profile default plugins list
hermes --profile default plugins doctor session-complete-telegram --ci
hermes --profile default plugins doctor desktop-clarify-telegram --ci
```

Package installation/Doctor results do not prove that the Desktop renderer is enabled, a Gateway has loaded its handler, or a message was delivered. Keep Desktop open and connected while a response settles; the completion check waits 60 seconds and fails closed if it cannot confirm the local state. A live test notification sends a real Telegram message, so do not trigger one unless you explicitly want that external message.

Only local Desktop routes under the same default home are supported. Remote/foreign homes are refused. For incoming cards, only the configured private owner replying to the exact original card/request is eligible; this is not a general-purpose Telegram chat bot.

## Updates and disablement

Updates must follow an exact full-SHA pin; the packages have no self-updater. After catalog admission, the recommended update path is a maintainer-reviewed SHA-bump PR followed by Hermes's catalog updater:

```bash
hermes --profile default plugins update session-complete-telegram
hermes --profile default plugins update desktop-clarify-telegram
```

Both catalog entries must already point to the reviewed release commit. Do not update from a moving branch or tag. Direct-Git re-pinning of an already installed package may require Hermes's `--force` replacement path, which also suppresses the normal caution prompt; it is not a routine install/update recommendation. Do not use it to skip review or a scanner finding. Before any package replacement, keep unrelated files out of Hermes's managed Desktop plugin directory: package reconciliation can replace that managed directory and remove files placed there. The plugin's persistent ledgers live outside it under the default home's `plugin-data/session-complete-telegram/`; retain those ledgers for deduplication and old-card rejection.

To disable, run the corresponding `hermes --profile default plugins disable <package-id>` command and turn off **Session Completion → Telegram** in Desktop. Disable the ingress separately if you only want outbound completion notifications. A running Gateway may retain its registered handler object until its next normal restart, but disabled ingress checks refuse interactions. Do not remove the ledgers as a routine disable/uninstall step.

## What has and has not been tested

The repository CI's current workflow runs the Node syntax/behavior suites and the Python modules `test_notifier`, `test_reply_bridge`, `test_followup_bridge`, `test_approval_bridge`, `test_native_install`, and `test_registration`, on Windows and Ubuntu, using Python 3.14, Node 24, and immutable Hermes core commit `74306c1d1b080837744faca3e371267dd09dda4b`. Registration/runtime guard regressions are included; commit-bound remote CI results must be checked for the final release. These are source/API-contract and isolated fake-home tests, not physical macOS tests; they do not verify a separate user's Desktop GUI or prove production installation/Gateway activation.

Historical native install/update/rollback command forms and the separate activation boundary remain linked in [operations notes](operations.md); those notes document past development work and are not a claim that this release is installed or active.
