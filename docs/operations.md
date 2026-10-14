# Unified package candidate, native install, and activation boundary

> **Current release:** public v1.8.0 at `4874276f519f68fdbacb9767f70c5ed760b5c6fb`. Use [current public installation](public-install.md) and [modular settings](modular-settings.md) for current controls/v4 handshake. The phase-3 candidate descriptions below preserve historical 1.7.0 preparation and are not current publication status. This catalog/docs update does not install code, change user settings or restart a Gateway.

## Ownership and current state

The repository is the source of truth. The unreleased v1.7.0 candidate has one installable package directory:

- `metamindedu/hermes-telegram-bridge/desktop-plugins/session-complete-telegram` → stable package/ID `session-complete-telegram`.

The package contains the fixed CLI, `desktop/plugin.js`, and one native Telegram handler factory registered through the public SDK. The factory wires only the existing configured Telegram `Application`; no second poller, core override, global alias, or broad `sys.path` change is introduced. `plugins/desktop-clarify-telegram/` stays in the repository only as a deprecated inert/no-factory compatibility stub, not as a second install requirement.

This task authorizes candidate source/tests/docs/workflow edits and isolated fake-home verification only. It does not authorize production installation/config/credentials/data changes, Gateway/Desktop activation, commit/push, or live Telegram sends. Independent Heavy review and release approval remain required. v1.6.1 at `1a76ed54ccf8a279d4dc4d9bfe1553b90d81d5ca` is historical and used two packages; v1.7.0 is unreleased and has no asserted SHA, CI, catalog admission, or production activation.

## Required pre-install candidate checks (future separately authorized release)

Pin the single primary package subdirectory to one reviewed full SHA. Verify the SHA exists on the intended repository/ref before invoking Hermes.
2. Preserve the exact package IDs and global state root `plugin-data/session-complete-telegram`. Do not copy or reset its SQLite files; there is no schema/data migration.
3. Run pinned-core CI and native Plugin Doctor on the primary package. Install/Doctor tests use isolated fake homes; the local Git transport mock is not a private-remote installation.
4. Confirm the install command leaves `plugins.enabled` and `plugins.entries.session-complete-telegram.settings` unchanged. The native package and Desktop renderer have independent enablement surfaces; `replies_enabled` and `approvals_enabled` are separate default-off switches.

## Native CLI transport limits

- The renderer preserves the original helper argv envelope: UTF-8 JSON request bytes are capped at 22,500 and the base64url token at 30,000 characters. Values are never truncated or repaired; permanent local encoding/size errors do not launch the CLI, show user guidance, and suppress repeated attempts for the same ask fingerprint.
- Native result JSON is serialized as UTF-8 with `ensure_ascii=False` and capped at 24 KiB before the existing ASCII base64 frame. Full stdout remains capped at 48,000 characters, including frame markers and startup/log output.
- The native handler supplies this result budget internally; it is not renderer-controlled. `reply_poll` fits the whole response projection before changing a queued row to `applying` and returns at most one native apply claim per poll, so fragment assembly cannot age sibling claims. Ordinary answers retain the full descriptor/result response. An individually oversized response can use bounded fragment metadata; read-only `reply_fragment` returns allow-listed chunks for the exact owner/id/claim after current-boot, absolute-TTL and 45-second claim-deadline checks. It does not change schema, claim again, extend expiry or refresh the claim lease. Unselected forms remain `queued`.
- The Desktop assembles a bounded chunk count/byte total, verifies exact SHA-256 bytes and strict JSON shape, then repeats the existing live immutable identity/deadline checks before one `request.answer`. Missing, malformed, corrupt or expired fragment data settles the attempted claim as unknown/expired and is never replayed.
- Native `approval_poll` measures the exact complete `{ok,items,terminal}` UTF-8 JSON projection before changing one queued row to `applying`. It observes all active rows first, returns at most one claim, and returns/defer terminal IDs without consuming a claim when the combined frame would overflow. Registration rejects a single non-fitting approval before sending buttons and routes it to command-free Desktop guidance.

## Native command approval inbox (1.7.0 candidate; unreleased)

The separate `approval-inbox/approvals.sqlite3` is not a schema extension to the clarify or followup stores. Its callback namespace is `hda:` with an opaque random token and a one-character `o` (once) / `d` (deny) action; command, session, queue IDs and credentials are never in callback data. Only the exact default bot, private-home owner, chat, topic, original bot-authored message ID and unchanged full card text are actionable. Inline/inaccessible/forwarded/edited/foreign/expired/duplicate taps are rejected without creating a receipt or changing approval state. Known/tombstoned approval-card text replies are consumed by successfully installed handlers and never interpreted by the bridge as approval or agent input. Whole-platform behavior when handler attachment fails is outside this guarantee; see the SDK boundary below.

The monitor requires a fresh local route, waiting runtime row, durable session identity, backend epoch, `srq-*` request, matching `approval.pending` queue ID, exact full request and pending fingerprints, and the pinned core dangerous-command producer key before registration, before every poll claim, and again immediately before dispatch. The helper uses the real pinned `tools.approval_detection.detect_dangerous_command`; it does not stub or copy the classifier. It accepts only once/deny. Desktop calls `approval.respond` with the frozen runtime `session_id`, exact queue `request_id`, `choice`, and explicit `all:false`. Only a numeric `resolved===1` is applied; zero is expired, malformed/other/timeout is unknown. It never falls back to `request.answer`, retries an ambiguous RPC, or writes permanent/session allowlists. A coalesced follower remains subject to the core's fresh-prompt behavior for once.

The native approval queue ID binds the core's first settlement; the current RPC contract exposes no server-side fingerprint CAS. After the post-claim snapshot, a read-only native `approval_claim_valid` action checks the exact owner/card ID/claim/frozen descriptor authority, applying row, live receiver boot/version/capability/destination, owner lease, claim deadline, absolute expiry, and current ingress enablement. The renderer validates its typed receipt and rechecks stop/deadline immediately before `approval.respond`. This is the last fresh local gate, not an atomic server-side fingerprint CAS or cross-service exactly-once guarantee. `approval.pending` does not expose the enqueue timestamp, so local card expiry is bounded by the configured profile `approvals.timeout` and a currently pending queue; the native queue remains the decision authority and rejects a late tap with `resolved:0`. The clarify one-hour minimum is not reused.

The receiver capability is enabled only when the unified helper/renderer version agrees and the existing private-chat receiver heartbeat is live. The candidate advertises `desktop-command-approval-v3`; old v2 receivers/helpers/renderers do not grant capability. No new Telegram polling consumer is created: approval callbacks and known-card text consumers are installed on the same PTB `Application` by the primary package. A normal safe Gateway restart loads new handler code; source replacement or plugin rediscovery alone is not an activation guarantee.

Review cards show the complete redacted literal command and description without Markdown stripping or truncation. Redaction omissions/collisions, non-NFC/unsafe controls, unavailable timeout, or a card above Telegram's single-message limit disable buttons; the fallback notification contains Desktop guidance, not a partial command presented as approval evidence. Sends use `protect_content:false`, a same-text edit with the keyboard retained, and no retry after an ambiguous send.

## SDK initialization failure and recovery

The bridge is not a whole-Gateway input firewall. Successfully attached known-card consumers reject stale replies even when optional actions are disabled, and malformed managed policy does not itself remove these consumers. If the SDK cannot attach even the reject-only guard after one bounded local retry, the bridge refuses readiness and no bridge approval/answer is delivered; the native host can still accept that reply as ordinary Telegram input. See `SECURITY.md` for this user-approved lifecycle limit. Do not reply to old cards during initialization/receiver failure; use Desktop, inspect the error, and recover the existing Gateway at a safe point. No core patch, private handler-store override, second poller, unbounded retry, or forced shared-process stop is used.

## Exact install, update, and rollback commands

Replace each variable with a previously verified full SHA. These commands are documented only and were **not executed** in this task.

Fresh install into the default/global home without enabling either agent plugin:

```bash
CANDIDATE_SHA=<verified-40-character-commit-sha>
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/desktop-plugins/session-complete-telegram --ref "$CANDIDATE_SHA" --no-enable --yes-deps

```

Update the one primary package directory to a reviewed candidate while preserving config and state:

```bash
NEW_SHA=<verified-new-40-character-commit-sha>
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/desktop-plugins/session-complete-telegram --ref "$NEW_SHA" --force --no-enable --yes-deps

```

Rollback the primary package through the native installer to the previously reviewed immutable commit. A rollback to v1.6.1 restores the historical two-package runtime and uses that release’s separate legacy procedure:

```bash
PREVIOUS_SHA=<verified-previous-40-character-commit-sha>
hermes --profile default plugins install metamindedu/hermes-telegram-bridge/desktop-plugins/session-complete-telegram --ref "$PREVIOUS_SHA" --force --no-enable --yes-deps

```

The primary package keeps its exact declared Python dependency pins (markdown-it-py, PyYAML and python-telegram-bot). Hermes PM owns installation and dependency consent; --yes-deps is explicit consent for the reviewed declarations, not permission for another candidate. --no-enable --yes-deps preserves selection instead of activating the packages. Do not use --no-deps or disabled dependency checks for an actual production rollout.

After a separately approved candidate install, verify the primary package with the normal Doctor contract:

```bash
hermes --profile default plugins doctor session-complete-telegram --ci

```

## Separate activation gate (not performed)

### Existing manual Desktop entry migration

The native reconciler will not overwrite an existing marker-less `desktop-plugins/session-complete-telegram/plugin.js` when it differs from the new native package entry. A successful Python package install therefore does NOT prove the running Desktop half was upgraded.

For a separately approved transition from Phase 1: install the one primary release package, explicitly disable the legacy companion, and verify fixed native CLI registration/enablement. Before entry alignment/adoption, inspect unrelated operator files and obtain separate approval to preserve or relocate only those files outside the managed Desktop folder. Perform any approved preservation/relocation before adoption; do not make arbitrary backups or operating moves. Then align ONLY `plugin.js` with the reviewed native package's `desktop/plugin.js` and read back byte equality. The supported reconciler can adopt that identical entry and write its own marker while leaving other files in place at that moment. **Entry-only alignment/adoption approval never approves later deletion of unrelated files:** a later packaged update or packaged rollback may replace the whole managed Desktop folder, deleting unrelated files left inside it. Do not forge the marker, delete the whole legacy folder or copy/reset plugin-data. Verify the actual loaded entry and Desktop toggle before any approved Gateway handler restart. First-phase code restoration uses its Git snapshot and the legacy layout, NOT the two-package installer commands; native package rollback is only to another previously verified packaged commit.

This migration and a packaged update/rollback after adoption are tested only with synthetic entries in a task-owned fake app root using the actual pinned-core reconciler. The fixture relocates its synthetic operator note outside the managed tree before alignment, verifies that in-tree unrelated files are replaced by packaged update and rollback, and retains the approved task-owned copy outside the managed tree. It has not been applied to the current Desktop instance.

Installing a package is not activation. Python command registration/ingress enablement, Desktop renderer enablement, Desktop hot reload, and Gateway handler loading are distinct. Any production enablement or restart needs a separate approval and readback. Ingress disablement must leave the completion package and ordinary Desktop completion notifications independently enabled. No plugin-enable, profile/config write, Desktop hot reload, Gateway restart, or live Telegram round trip was done here.

## Completion notification recovery — 1.8.1 source candidate (unreleased)

This scoped source candidate does not move the public 1.8.0 pin and does not authorize install, enablement, renderer materialization, Gateway restart, or live delivery. Package/helper/renderer use 1.8.1 and the separate `desktop-completion-recovery-v1` capability. The `loaded.version` compatibility field stays `1.8.0`; the installed `approval_bridge.py` and receiver keep `1.8.0` + `desktop-command-approval-v4` unchanged. The new renderer requires both the 1.8.1 package marker and recovery capability; old 1.8.0 helpers therefore do not activate recovery, while 1.8.0 approval-v4 remains independently compatible.

Only a fresh, non-replayed successful `message.complete` can record a candidate. Native metadata is limited to opaque candidate key, route/profile/runtime/durable identities, `started_at`, epoch, event watermark, quiet time, absolute expiry, revision/claim/attempt state and keyed source/destination bindings. It stores no title, final text, preview, Telegram target, or credential. TTL is fixed at 30 minutes; at most 256 active candidates and 1,024 total candidate rows are retained, with bounded terminal cleanup. Re-registration never resets the original watermark, quiet/lifetime, or terminal state. Expiry and settings cleanup affect candidate metadata only, never old delivery rows.

Recovery uses a captured public `host.getGateway().request()` on the already-open local socket. The active named profile can execute the fixed default-home CLI (`--profile default`); it does not require the default profile to be foreground. Recovery admission/restoration is restricted to the currently active local target because the public SDK cannot prove arbitrary nonforeground sockets are live without dialing them. Fresh nonforeground completions retain the existing ordinary all-profile notification path; they do not gain durable recovery. An already recorded candidate waits through temporary disconnection or foreground changes within its original TTL. Ordinary question/attention classification and its no-replay policy remain unchanged.

The actual SDK distinguishes route inventory from an already-open gateway: `host.profileRoutes()` can list a route whose backend is not running, and `host.requestProfile()` may cold-start pooled backends. Recovery therefore checks the SDK's live gateway state plus exact `connectionId` and profile before every profile RPC. It never dials another profile to look up a candidate and never invokes create/resume/activate/prompt methods. A candidate is restored only for the unchanged local route, runtime ID, durable key, `started_at`, epoch and complete contiguous replay from the original watermark. Missing/malformed/failed/truncated snapshots remain unverifiable until retry/TTL; a validated empty active list, changed identity, new work, pending requests, active children/processes/control state or todos cancels it. The sender independently rechecks the persisted final row, lease/delegation/todo gates and current native setting/destination before dispatch.

Claim and dispatch use revision/token CAS in `BEGIN IMMEDIATE` and the exact existing completion-v2 delivery dedupe key. An expired pre-dispatch claim may be superseded; a stale helper cannot pass the new token/revision. Once the delivery boundary is `dispatching`, helper loss, process crash, malformed receipt, or absence of a later receipt is uncertain and never replayed. Only a durable proof tied to the exact attempt's direct pre-request `httpx.ConnectError`/`ConnectTimeout` is retryable. The recovery-only client is fresh, direct, `trust_env=False`, non-redirecting and has zero transport retries; read/write/protocol/JSON uncertainty and Bot API rejection are terminal. A failed verification edit after a confirmed send leaves it sent. No retry behavior changes for attention, replies, followups, or approvals.

Turning service or completion notifications OFF retires pending/read-blocked/claimed/proven-unsent candidates before a public settings path can turn them back ON. Dispatching candidates and writes already in flight are not revocable. A config OFF→ON edit made entirely outside the observed settings path cannot be detected retroactively; it is not permission to revive old candidates. Read-only `sender` counts are separate from the existing receiver heartbeat and expose no content or identifiers; neither is proof of a real Telegram delivery.

The focused suites are `completion_recovery.test.mjs` and `test_completion_recovery.py`; CI must run them alongside every existing Node/Python suite. Python recovery tests use the real registered native CLI/result frame, isolated fake homes, the pinned Hermes schemas and mocked Telegram transport only. `telegram_integration.py`, `reply_bridge.py`, `followup_bridge.py`, and `approval_bridge.py` remain unchanged. This source test evidence is not independent review, installation, activation, or release evidence.

## Verification

- Node: `node --check desktop/plugin.js` and `node --test plugin.test.mjs reply.test.mjs followup.test.mjs approval.test.mjs materialization.test.mjs settings.test.mjs completion_recovery.test.mjs recovery_review.test.mjs helper_budget.test.mjs`.
- Python: `python -m unittest test_notifier test_reply_bridge test_followup_bridge test_approval_bridge test_native_install test_registration test_feature_gates test_light_corrections test_final_boundaries test_settings_control test_review_revocation test_review_notification_boundaries test_producer_epoch test_completion_recovery test_recovery_review -v`; `test_approval_bridge` runs against the real pinned dangerous-command detector, queue settlement/coalesced follower behavior, native dispatcher, and actual PTB `Application.process_update`; Telegram transport is mocked only where a unit scenario requires it.
- CI: `.github/workflows/tests.yml` runs those exact suites on Windows and Ubuntu, with `contents: read`, pinned action SHAs, Hermes core commit `74306c1d1b080837744faca3e371267dd09dda4b`, and the core `uv.lock` inside a job-owned environment. It does not deploy, restart, upload secrets, or claim GUI verification on another PC.
- Fake-home native install tests use only a mock for Git transport. They exercise Hermes' actual install/publication/manifest/Doctor/context contracts, verify the new approval helper/capability across fresh install, update and rollback, and preserve sentinels/IDs. No live Telegram calls occur.

## State and rollback

The only rollback target is the exact previously reviewed package commit, installed through the same native Hermes command. Existing `plugin-data/session-complete-telegram` and configuration are retained; no database copy, reset, migration or ad-hoc backup is part of this phase. `docs/source-manifest.json` remains an explicitly historical Phase 1 snapshot.
