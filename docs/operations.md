# Phase 2 source verification, native install, and activation boundary

## Ownership and current state

The repository is the source of truth. The official native packages are the two existing directories installed from the same immutable Git commit:

- `metamindedu/hermes-telegram-bridge/desktop-plugins/session-complete-telegram` → package/ID `session-complete-telegram`.
- `metamindedu/hermes-telegram-bridge/plugins/desktop-clarify-telegram` → package/ID `desktop-clarify-telegram`.

Hermes resolves `owner/repo/subdir` through its native installer. The first package includes both the Python CLI registration and `desktop/plugin.js`; the Desktop shell materializes that JS half by its existing package mechanism. The second package registers one factory on the existing Telegram `Application`. The Gateway remains the only Telegram update/polling owner.

Phase 2 authorizes source/tests/docs/CI, isolated fake-home installation checks and development-branch commit/push/PR delivery. It does not authorize production installation/config/credentials/data changes, Gateway/Desktop activation or live Telegram sends. Independent Heavy review, exact remote SHA, CI and actual private pinned install must be verified before completion; task-owned fixture writes are not production activation.

## Required pre-install candidate checks (future separately authorized release)

1. Pin both subdirectory installs to the same reviewed 40-character commit SHA. Verify the SHA exists on the intended repository/ref before invoking Hermes.
2. Preserve the exact package IDs and global state root `plugin-data/session-complete-telegram`. Do not copy or reset its SQLite files; there is no schema/data migration.
3. Run the pinned-core CI commands and native Plugin Doctor on both package directories. Install/Doctor tests use isolated fake homes; their local Git transport mock is not evidence that the private remote install has run.
4. Confirm install commands leave `plugins.enabled` unchanged. The frontend/CLI, ingress and Desktop renderer have independent enablement surfaces; do not infer one from another.

## Native CLI transport limits

- The renderer preserves the original helper argv envelope: UTF-8 JSON request bytes are capped at 22,500 and the base64url token at 30,000 characters. Values are never truncated or repaired; permanent local encoding/size errors do not launch the CLI, show user guidance, and suppress repeated attempts for the same ask fingerprint.
- Native result JSON is serialized as UTF-8 with `ensure_ascii=False` and capped at 24 KiB before the existing ASCII base64 frame. Full stdout remains capped at 48,000 characters, including frame markers and startup/log output.
- The native handler supplies this result budget internally; it is not renderer-controlled. `reply_poll` fits the whole response projection before changing a queued row to `applying` and returns at most one native apply claim per poll, so fragment assembly cannot age sibling claims. Ordinary answers retain the full descriptor/result response. An individually oversized response can use bounded fragment metadata; read-only `reply_fragment` returns allow-listed chunks for the exact owner/id/claim after current-boot, absolute-TTL and 45-second claim-deadline checks. It does not change schema, claim again, extend expiry or refresh the claim lease. Unselected forms remain `queued`.
- The Desktop assembles a bounded chunk count/byte total, verifies exact SHA-256 bytes and strict JSON shape, then repeats the existing live immutable identity/deadline checks before one `request.answer`. Missing, malformed, corrupt or expired fragment data settles the attempted claim as unknown/expired and is never replayed.

## Native command approval inbox (1.6.0)

The separate `approval-inbox/approvals.sqlite3` is not a schema extension to the clarify or followup stores. Its callback namespace is `hda:` with an opaque random token and a one-character `o` (once) / `d` (deny) action; command, session, queue IDs and credentials are never in callback data. Only the exact default bot, private-home owner, chat, topic, original bot-authored message ID and unchanged full card text are actionable. Inline/inaccessible/forwarded/edited/foreign/expired/duplicate taps are rejected without creating a receipt or changing approval state. Known/tombstoned approval-card text replies are consumed and never interpreted as approval or agent input.

The monitor requires a fresh local route, waiting runtime row, durable session identity, backend epoch, `srq-*` request, matching `approval.pending` queue ID, exact full request and pending fingerprints, and the pinned core dangerous-command producer key before registration, before every poll claim, and again immediately before dispatch. The helper uses the real pinned `tools.approval_detection.detect_dangerous_command`; it does not stub or copy the classifier. It accepts only once/deny. Desktop calls `approval.respond` with the frozen runtime `session_id`, exact queue `request_id`, `choice`, and explicit `all:false`. Only a numeric `resolved===1` is applied; zero is expired, malformed/other/timeout is unknown. It never falls back to `request.answer`, retries an ambiguous RPC, or writes permanent/session allowlists. A coalesced follower remains subject to the core's fresh-prompt behavior for once.

The native approval queue ID binds the core's first settlement; the current RPC contract exposes no server-side fingerprint CAS. Fresh snapshot checks are a client-side guard and must not be described as atomic CAS. `approval.pending` does not expose the enqueue timestamp, so local card expiry is bounded by the configured profile `approvals.timeout` and a currently pending queue; the native queue remains the decision authority and rejects a late tap with `resolved:0`. The clarify one-hour minimum is not reused.

The receiver capability is enabled only when the ingress/helper/renderer package version agrees and the existing private-chat receiver heartbeat is live. No new Telegram polling consumer is created: approval callbacks and the known-card text consumer are installed on the same PTB `Application` from `reply_bridge.wire`. A restart/owner/boot change retires prior authority. Lease loss rejects a queued choice and makes an applying claim unknown; it is never replayed after reconnection. Unsupported generic, execute_code, plugin-rule, sudo, secret/vault, or compute-host approvals lacking an authoritative in-process queue remain notification-only.

Review cards show the complete redacted literal command and description without Markdown stripping or truncation. Redaction omissions/collisions, non-NFC/unsafe controls, unavailable timeout, or a card above Telegram's single-message limit disable buttons; the fallback notification contains Desktop guidance, not a partial command presented as approval evidence. Sends use `protect_content:false`, a same-text edit with the keyboard retained, and no retry after an ambiguous send.

## Exact install, update, and rollback commands

Replace each variable with a previously verified full SHA. These commands are documented only and were **not executed** in this task.

Fresh install into the default/global home without enabling either agent plugin:

```bash
CANDIDATE_SHA=<verified-40-character-commit-sha>
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/desktop-plugins/session-complete-telegram --ref "$CANDIDATE_SHA" --no-enable --yes-deps
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/plugins/desktop-clarify-telegram --ref "$CANDIDATE_SHA" --no-enable --yes-deps
```

Update both existing package directories to one new candidate while preserving their enablement selection:

```bash
NEW_SHA=<verified-new-40-character-commit-sha>
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/desktop-plugins/session-complete-telegram --ref "$NEW_SHA" --force --no-enable --yes-deps
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/plugins/desktop-clarify-telegram --ref "$NEW_SHA" --force --no-enable --yes-deps
```

Rollback both packages to the previously reviewed commit through the same native installer transaction:

```bash
PREVIOUS_SHA=<verified-previous-40-character-commit-sha>
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/desktop-plugins/session-complete-telegram --ref "$PREVIOUS_SHA" --force --no-enable --yes-deps
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/plugins/desktop-clarify-telegram --ref "$PREVIOUS_SHA" --force --no-enable --yes-deps
```

Package-owned Python dependencies are declared explicitly in plugin.yaml (markdown-it-py, PyYAML and python-telegram-bot for the completion package; python-telegram-bot for ingress). Hermes PM owns installation and dependency consent; --yes-deps is explicit consent for the reviewed declarations, not permission for another candidate. --no-enable --yes-deps preserves selection instead of activating the packages. Do not use --no-deps or disabled dependency checks for an actual production rollout.

After a separately approved install, native verification uses the normal Doctor contract:

```bash
hermes --profile default plugins doctor session-complete-telegram --ci
hermes --profile default plugins doctor desktop-clarify-telegram --ci
```

## Separate activation gate (not performed)

### Existing manual Desktop entry migration

The native reconciler will not overwrite an existing marker-less `desktop-plugins/session-complete-telegram/plugin.js` when it differs from the new native package entry. A successful Python package install therefore does NOT prove the running Desktop half was upgraded.

For a separately approved transition from Phase 1: first install the two matching candidates and verify the fixed native CLI registration/enablement. Before entry alignment/adoption, inspect unrelated operator files and obtain separate approval to preserve or relocate only those files outside the managed Desktop folder. Perform any approved preservation/relocation before adoption; do not make arbitrary backups or operating moves. Then align ONLY `plugin.js` with the reviewed native package's `desktop/plugin.js` and read back byte equality. The supported reconciler can adopt that identical entry and write its own marker while leaving other files in place at that moment. **Entry-only alignment/adoption approval never approves later deletion of unrelated files:** a later packaged update or packaged rollback may replace the whole managed Desktop folder, deleting unrelated files left inside it. Do not forge the marker, delete the whole legacy folder or copy/reset plugin-data. Verify the actual loaded entry and Desktop toggle before any approved Gateway handler restart. First-phase code restoration uses its Git snapshot and the legacy layout, NOT the two-package installer commands; native package rollback is only to another previously verified packaged commit.

This migration and a packaged update/rollback after adoption are tested only with synthetic entries in a task-owned fake app root using the actual pinned-core reconciler. The fixture relocates its synthetic operator note outside the managed tree before alignment, verifies that in-tree unrelated files are replaced by packaged update and rollback, and retains the approved task-owned copy outside the managed tree. It has not been applied to the current Desktop instance.

Installing a package is not activation. Python command registration/ingress enablement, Desktop renderer enablement, Desktop hot reload, and Gateway handler loading are distinct. Any production enablement or restart needs a separate approval and readback. Ingress disablement must leave the completion package and ordinary Desktop completion notifications independently enabled. No plugin-enable, profile/config write, Desktop hot reload, Gateway restart, or live Telegram round trip was done here.

## Verification

- Node: `node --check desktop/plugin.js` and `node --test plugin.test.mjs reply.test.mjs followup.test.mjs approval.test.mjs materialization.test.mjs`.
- Python: `python -m unittest test_notifier test_reply_bridge test_followup_bridge test_approval_bridge test_native_install -v`; `test_approval_bridge` runs against the real pinned dangerous-command detector, queue settlement/coalesced follower behavior, native dispatcher, and actual PTB `Application.process_update`; its transport alone is mocked.
- CI: `.github/workflows/tests.yml` runs those exact suites on Windows and Ubuntu, with `contents: read`, pinned action SHAs, Hermes core commit `74306c1d1b080837744faca3e371267dd09dda4b`, and the core `uv.lock` inside a job-owned environment. It does not deploy, restart, upload secrets, or claim GUI verification on another PC.
- Fake-home native install tests use only a mock for Git transport. They exercise Hermes' actual install/publication/manifest/Doctor/context contracts, verify the new approval helper/capability across fresh install, update and rollback, and preserve sentinels/IDs. No live Telegram calls occur.

## State and rollback

The only rollback target is the exact previously reviewed package commit, installed through the same native Hermes command. Existing `plugin-data/session-complete-telegram` and configuration are retained; no database copy, reset, migration or ad-hoc backup is part of this phase. `docs/source-manifest.json` remains an explicitly historical Phase 1 snapshot.
