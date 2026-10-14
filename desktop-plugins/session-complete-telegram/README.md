# Session Completion → Telegram

## Modular settings (public v1.8.0; 1.8.1 source candidate)

Open the dedicated **Telegram Bridge** settings page while the Desktop plugin is enabled. The common service switch preserves individual choices; completion notices, input-needed notices, question answers, completion-card followups and one-time approvals have separate settings. Existing `replies_enabled` and `approvals_enabled` consent remains default-off and is never enabled by installation. Parent-notification switches and exact live authorization still apply. Writes use the captured public native settings API, one known boolean at a time, with readback and no retry after an unknown result. See [`../../docs/modular-settings.md`](../../docs/modular-settings.md) for defaults, revocation, all-profile scope and distinct activation gates.

The current source candidate uses package/helper/renderer 1.8.1 with `desktop-completion-recovery-v1`; the existing approval receiver remains compatible at 1.8.0 / `desktop-command-approval-v4`. The public 1.8.0 release pin is unchanged; this source candidate is not yet published. Receiver heartbeats and saved settings are not proof of a Telegram roundtrip or command completion.

## Completion notification recovery (1.8.1 source candidate only)

A candidate is recorded only from a fresh successful persisted Desktop completion—not from historical replay or a session-history scan. If Desktop reconnects within 30 minutes, the candidate can be rechecked only against the same already-live local connection/session and complete event replay. It does not start, resume, or create a profile/session. Unreadable or incomplete snapshots stay unverified; changed identity or new work cancels the candidate. The candidate table stores bounded metadata only, never the card text, title, preview, token, or destination.

Durable recovery currently applies to the active local profile's live runtime. Fresh completions from other profiles retain the ordinary notification path rather than losing alerts; they do not gain durable recovery. A temporary disconnect or profile-view switch does not itself prove the recorded runtime changed. Ordinary question/attention cards keep their original classification and no-replay policy.

The existing completion dedupe identity is shared with older send paths and multiple windows. Only a durable proof that the exact attempt failed before the Telegram request began allows another attempt; timeouts, helper/result loss, read/write/protocol failures, explicit rejections and any unknown result are not automatically replayed. A send accepted by Telegram remains sent even if the follow-up verification edit fails. Turning service/completion notifications off retires unexecuted candidates; it cannot cancel a write already dispatched. The settings page shows sender counters separately from receiver heartbeat. These behaviors are source-candidate documentation, not release or activation status.

## Optional replies (1.7.0 candidate; unreleased)

After normal native Gateway loading and only when `replies_enabled: true`, newly verified completion cards accept direct plain-text replies from the configured private-home owner. They submit through the captured Desktop route to the same in-memory session with `prompt.submit` and `queued:true`. No session handoff, resume, creation, interruption, or core change. Older cards lack trustworthy routing metadata and are not actionable.

- Up to 2000 characters, original text only; no edits, attachments, forwarded messages, slash commands or untrusted senders.
- Card eligibility lasts at most 24 hours and requires the original Desktop owner/native receiver leases. A temporary 45-second lease lapse blocks delivery but no longer permanently expires the card: the same owner may renew before the original TTL, with exact live-session checks still required. An input rejected/purged during the outage is never replayed; send a new reply after recovery. Reload/restart or changed runtime/durable identity is NOT recovered by this lease fix. Existing terminal bindings are never revived. The native receiver needs a Gateway restart to load the fix; files on disk alone are not activation evidence.
- Observed human-input waiting, missing/changed sessions, and backend voice mode reject the followup. Working sessions queue it. Accepted/queued feedback is not a claim of completed execution.
- Durable one-attempt claims prevent duplicate submission attempts. Unknown outcomes never retry. Snapshot checks are not atomic; a later waiting transition can still queue input, but cannot answer an approval.
- A normal Desktop send can include attachments already drafted there; this plugin never clears those drafts. Text enters the normal conversation and travels through local helper stdout, not helper command arguments.
- Followup-inbox uses a separate restricted directory; its failure must not stop clarify heartbeats or intercept mapped clarify replies. Minimal consumed-message identifiers remain after transient input text is purged.
- See [`../../docs/operations.md`](../../docs/operations.md) for native package install, verification, activation gates, and limits. Do not assume new native code is live from a local source change or installed disk version alone.

Local Hermes Desktop component and native package `session-complete-telegram`. Its canonical source is `desktop/plugin.js`; Hermes materializes that half in the app-level Desktop plugin root. One native `register(ctx)` declares the fixed `desktop-telegram-bridge` CLI and the optional factory on the existing Telegram Gateway application.

There is one installable package. Native settings `replies_enabled` and `approvals_enabled` default to false and are independent; both leave outbound completion notifications available. All profile observation remains local; helper execution uses only the validated default local route and refuses named/foreign homes. The renderer sends fixed argv through `cli.exec`, not an executable path or shell command. Python results are a bounded strict frame; malformed or ambiguous writes fail closed without retry.

The v1.7.0 candidate install, explicit legacy-ID migration, rollback and activation boundaries are in [`../../docs/operations.md`](../../docs/operations.md). Source edits and task-owned fake-home tests do not install, enable, configure, activate or restart a production home.

## Gateway SDK limitation

The bridge's live authorization checks remain mandatory, but it does not control all ordinary Telegram input. Known-card rejection requires its handlers to be successfully installed. If the host fails to attach even the reject-only guard, the bridge refuses interaction readiness; Hermes may still treat a plain-text old-card reply as ordinary chat. Do not reply to old cards during initialization/receiver failure: use Desktop and normally recover the existing Gateway before using new cards. No SDK/core patch, private handler-store override, second poller or forced Gateway stop is part of this package. This limit also applies while the plugin is disabled/uninstalled. The detailed security disclosure is in the repository's `SECURITY.md`, section **Gateway SDK boundary**.

## Behavior

- Applies to every local profile surfaced by the Desktop connection registry,
  including profiles created after installation. No per-profile configuration,
  shared secret copying, background daemon, core patch, or prompt modification.
- Only fresh, successfully persisted parent `message.complete` events qualify.
  Mid-tool commentary, child watch mirrors, failures, interruptions and replayed
  old completions do not qualify.
- Waits **60 seconds** after the final reply/control bookkeeping, then verifies
  the session is idle, no active child/queued delivery/bounded process remains,
  no question/approval is open, no active/paused goal/loop/heartbeat remains,
  and known todos have no pending/in-progress items.
- Rechecks idle and independently reads the owning profile's state.db in
  read-only mode. The final row must still be the latest row (a queued user
  prompt disqualifies it), its turn lease must be released, and persisted todo
  and async-delivery state must be settled.
- **One notification per fully settled request/work cycle, including follow-up
  requests in the same session.** Internal tool/delegation turns still do not
  notify. A new request after a completed cycle can notify again after settling.
  Duplicate callbacks, multiple windows and reloads cannot resend the same
  persisted final response. Requests arriving within the quiet window coalesce.
  Version 1.1.0 preserves old ledger entries but no longer lets a session-wide
  claim suppress later completions; no new session is needed.
- A notification states that execution has stopped after the final response;
  it is not a semantic proof that every requirement in arbitrary prose was met.
  Hermes has no irreversible 'the user will never send another message' state.
- Includes profile, session title, and the latest validated final assistant reply.
  Version 1.2.0 previews up to 1,200 Unicode characters. Longer replies retain
  the first 900 characters and the ending, separated by `… 중간 생략 …`.
  Since 1.3.2 Markdown prose renders as plain text; literal code is preserved.
  Known credential patterns and credential-bearing URLs are redacted before
  truncation. Earlier conversation, tool output, and hidden reasoning are excluded.
  Preview text is read locally from the exact persisted final row; it is absent
  from CLI inspection results, notification receipts, diagnostics, and commands.
  The existing default profile's Telegram bot + home channel are used.
  No tokens/chat IDs enter the renderer, command arguments, receipts, or logs.
- Completion, attention/approval and clarify cards explicitly send
  `protect_content: false`, allowing Telegram text copy and forwarding.
  Markdown-to-plain previews, literal code, redaction and reply authorization stay unchanged.
- Explicitly detached services and satisfied non-completion readiness watches
  do not block. Unclassified running background commands conservatively block.
- Desktop must be open for the fresh event and initial identity capture. The 1.8.1 source candidate can recheck a durably recorded completion after a temporary interruption only when the same local route/runtime remains live and unchanged before the original 30-minute TTL. Desktop/backend restarts, changed runtime/connection/epoch, missing metadata or incomplete replay are not recovered; no backend is started to inspect a candidate. Remote SSH/cloud profiles remain outside this local route boundary.
- Delivery claims persist in
  `plugin-data/session-complete-telegram/notifications.sqlite3`.
  A read/write/protocol timeout or otherwise uncertain send is recorded as unknown
  and is never automatically resent; Telegram's send API has no idempotency key.
  The source candidate retries only a durable exact-attempt proof of a pre-request
  connection failure. Explicit API rejection is terminal.

## Disable / remove

Disable **Session Completion → Telegram** in Desktop's plugin list. This retires
its event subscriptions/timers immediately. To uninstall, use Desktop's plugin
remove action or remove only this plugin folder. The sender ledger can be kept
for audit/deduplication. For migration from the historical two-package release, explicitly disable the old native ID with `hermes --profile default plugins disable desktop-clarify-telegram`, enable `session-complete-telegram` and the optional settings you want, then restart the existing Gateway at a safe point. Do not delete/copy/reset package data; the helper refuses while the legacy ID remains enabled. A running Gateway may retain its resident handler object until restart, so disabled helpers continue to reject interactions.
## Files and tests

- `desktop/plugin.js`: app lifecycle, scoped profile routing, quiet-state gate and native `cli.exec` helper.
- `__init__.py` / `runtime.py`: fixed CLI registration and package-home validation.
- `notifier.py` / `completion_recovery.py`: read-only durable-state checks, shared completion-v2 dedupe, bounded metadata-only recovery, revision/claim/send CAS and recovery-only proven-connect-failure classification.
- `followup_bridge.py`: completion-card reply lifecycle and isolated followup inbox.
- `approval_bridge.py`: separate command-approval inbox, full literal review, receiver lease, and one-shot tap claim; Telegram never answers the core queue directly.
- `plugin.test.mjs`, `reply.test.mjs`, `followup.test.mjs`, `approval.test.mjs`, `settings.test.mjs`, `completion_recovery.test.mjs`: Node behavior/race tests (Node built-in runner).
- `test_notifier.py`, `test_reply_bridge.py`, `test_followup_bridge.py`, `test_approval_bridge.py`, `test_completion_recovery.py`: Python bridge/recovery tests using the actual pinned Hermes schemas and native CLI/result frame; Telegram transport is mocked only where a unit scenario requires it.
- `test_native_install.py`, `test_registration.py`, `test_feature_gates.py`, `test_settings_control.py`: real Hermes install/Doctor/PluginContext fake-home contracts, one-package registration, strict default-off settings, migration refusal, recovery cleanup and runtime home guards.

From this folder, in the pinned-core environment described by `docs/operations.md`:

```
node --check desktop/plugin.js
node --test plugin.test.mjs reply.test.mjs followup.test.mjs approval.test.mjs materialization.test.mjs settings.test.mjs completion_recovery.test.mjs recovery_review.test.mjs
python -m unittest test_notifier test_reply_bridge test_followup_bridge test_approval_bridge test_native_install test_registration test_feature_gates test_light_corrections test_final_boundaries test_settings_control test_review_revocation test_review_notification_boundaries test_producer_epoch test_completion_recovery test_recovery_review -v
```

The test dependencies are declared in `requirements-test.txt`. Do not run the native installer against a production home as part of source tests.

## Telegram command approvals (1.7.0 candidate; unreleased)

- `Allow Once` and `Deny` buttons are available only for the pinned core's real dangerous-command gate when the current Desktop `approval` request exactly matches its `approval.pending` queue entry, runtime session, queue ID, command, description, and producer pattern key. The helper re-runs the actual pinned `tools.approval_detection` classifier; no copied classifier is used.
- The Telegram card displays the complete redacted command and description literally (no Markdown parsing, previews, or truncation). If redaction is unstable/ambiguous, suspicious credential text survives, Unicode/control characters are unsafe, the card exceeds Telegram's one-message bound, or the configured timeout is unavailable/expired, no buttons are sent; a notification-only card directs the user to Desktop.
- Only `once` or `deny` can be submitted, always to the exact frozen queue ID with `all:false`. Only a numeric `resolved === 1` is reported as delivered; `resolved === 0` is expired and every other/timeout result is unknown. There is no `request.answer` fallback, retry, permanent/session allowlist update, or free-text approval.
- `execute_code`, plugin-rule/action, sudo, secret/vault, generic/unknown approvals, and compute-host paths without an authoritative in-process native queue remain notification-only. Queue IDs provide core first-settlement binding; Desktop additionally rechecks the complete request and pending snapshots before registration, polling, and the final RPC. The RPC has no server-side fingerprint-CAS field, so this is not described as one.
- The separate `approval-inbox` uses one-shot posting/open/queued/applying/terminal transitions, a short receiver/owner lease, and no replay after ambiguous sends, stale queued taps, owner change, or restart. A tap during a lease gap creates no receipt; a queued choice observed across a gap is terminally rejected. It shares the existing gateway `Application` and heartbeat; it never starts another Telegram poller.
- Approval cards follow the profile's `approvals.timeout` with no clarify one-hour floor. The current `approval.pending` snapshot is rechecked on each pass and the native queue remains the deadline authority; a late tap can only receive `resolved:0` and is never redirected to another answer method.
- The unified package, helper, and Desktop advertise version 1.7.0 and `desktop-command-approval-v3`. Older cached v2 receivers/helpers/renderers remain fail-closed. Upgrading native source is not a Gateway hot reload; use the normal safe Gateway restart to load the candidate factory.

## Telegram answers (1.4.0)

- Only active `clarify` requests are actionable: single-choice, multiple-choice
  with Submit, and explicit text replies to the exact Telegram card. A batch
  waits for all answers before submitting one complete response to Desktop.
- Dangerous command approvals are actionable only as scoped above; sudo, secret/vault prompts and questions in final prose remain notification-only. No secret-entry bypass exists.
- The existing default Telegram Gateway owns all updates. The unified
  `session-complete-telegram` package adds scoped PTB handlers; no second
  getUpdates client, webhook change, core patch or secondary-profile config.
- Replies are accepted only from the configured positive private-home user,
  exact bot/chat/topic/message, current receiver boot and live Desktop owner.
  A question card stays answerable as long as the asking profile's clarify wait
  (`agent.clarify_timeout`, legacy `clarify.timeout` when set), floored at 1 hour
  and capped at 24 hours; the card text states the actual duration. Desktop
  validates the exact local route, runtime/durable session, backend epoch,
  request ID and full question snapshot
  immediately before `request.answer`. Changed/local-answered questions expire.
- Desktop and the messaging gateway must remain running and connected.
  Answers are polled on the existing 8-second sweep, not instantly.
- Reply inbox: `plugin-data/session-complete-telegram/reply-inbox/replies.sqlite3`.
  Provision its directory with owner/SYSTEM/Administrators-only ACL before
  enabling. Raw answers are purged on settlement/expiry; form content is removed
  after one day. Minimal bot/chat/message-ID tombstones remain (no answer text)
  so replying to an old card never becomes an unrelated agent prompt.
- Use one answering surface per question. Backend first **complete** settlement
  wins. A local partial batch lock remains an editable draft; a near-simultaneous
  Telegram full submission can replace it. Snapshot validation is not atomic CAS.
- A claimed reply must still be within its deadline when Desktop starts the RPC;
  expiry cannot revoke an already in-flight backend write. Restarting/reloading
  the owning Desktop window is not recovered: its existing cards cannot be
  answered again and end at their original TTL, and only the original owner may
  renew a card.
- A temporary 45-second transport-lease lapse — a delayed Desktop sweep, helper
  timeout, receiver heartbeat gap, or an unverifiable snapshot — blocks delivery
  instead of expiring the card. The same owner may renew before the original
  TTL. An unverified observation immediately revokes ingress freshness for open and
  queued cards, even within 45 seconds: answers tapped or typed during the gap are
  rejected as `lease_stale` and never consumed, so they can be sent again after recovery.
  Incomplete active-session/question snapshots never imply absence; a healthy empty
  `sessions` list and a definitive identity/boot change remain terminal. Only an in-flight
  delivery claim lapses terminally (`unknown`, never replayed).
- A send or answer with an ambiguous outcome is never automatically retried.
  Telegram shows applied only after Desktop's authoritative response receipt.
- `reply_bridge.py` owns clarify ingress/store/helper logic; `telegram_integration.py` is the primary package's internal Gateway integration. `plugins/desktop-clarify-telegram/` is a deprecated inert/no-factory compatibility stub. `reply.test.mjs` and `test_reply_bridge.py`
  cover bridge behavior separately from completion-notification regressions.

## Verification evidence

### Production run through an accidental test driver (version 1.3.0, 2026-09-26)

- A leftover temporary verification driver auto-ran at app start after a reboot and
  exercised all three input-needed paths in real sessions: clarify ask (messages
  `1241`, `1242`), command-approval ask (`1243`, `1244`), question-final (`1245`,
  `1246`). All six sends are sender-verified in the ledger. Two driver instances
  ran concurrently, hence the doubled count.
- The driver's launch route was labeled `default` while its launch home was the
  claire profile home; the alias gate resolved every notification to
  `프로필: claire`, exactly as designed.
- Lesson recorded in the operations skill: remove temporary driver plugins before
  pausing work — they reload on every app start.

### Real long-answer preview test (version 1.2.0)

- Separate live Desktop session `20260923_165703_941f40`, persisted final row
  `441177`: actual model reply 2,371 characters, preview exactly 1,200.
- Normal event/quiet-window path sent Telegram message `1179`; its exact text
  was verified unchanged via same-text edit. Beginning and ending were retained,
  the middle omission marker was present, and the payload was 1,349 UTF-16 units.
- Test session closed; temporary driver removed. Unit suites passed:
  24 JavaScript tests and 26 Python tests, including preview and redaction cases.

### Real two-request Desktop test (version 1.1.1)

- Created a separate Desktop session and submitted two real model requests via
  the app's retained SDK profile connection; did not invoke the sender directly.
- Same stored session: `20260923_164250_a659a1`; two user rows and two final replies.
- Final reply 1 → 60.3 seconds → Telegram message `1177`, independently verified.
- Follow-up in the SAME session → 60.2 seconds → message `1178`, verified.
- Observed another 70.5 seconds: exactly two notifications, no third duplicate.
- Live testing exposed `default` route aliasing a named backend's launch home.
  Version 1.1.1 resolves only a missing default-session alias by exact session ID
  plus final row ID in local named homes. Ambiguous matches fail closed; explicit
  named-profile requests never fall back to another home.
- Regression suite: 24 JavaScript tests and 20 Python tests passed.

### Phase 1 initial installation evidence (historical)

This evidence describes the earlier Phase 1 direct launcher and `shell.exec` path; it is not the Phase 2 native `cli.exec` transport or activation status.
- Desktop's actual loaded plugin called the helper through the supported
  `host.requestProfile(defaultRoute, 'shell.exec', ...)` RPC; its
  `desktop_loaded` receipt lists all 11 currently installed profiles.
- Unit tests cover 30 successive internal turns, late children, pending todos,
  active leases, queued prompts, incomplete snapshots, multiple profiles with
  identical runtime IDs, a future profile name, concurrent windows, compression
  lineage, and ambiguous network delivery.
- A real installation-test notification was sent to the configured private home.
  Telegram returned message ID `1176`. An idempotent same-text edit addressed that
  exact message and returned `message is not modified`, verifying its existence
  and text without another notification. This does not prove the user read it.
- The current live work session's own completion notification necessarily occurs
  only AFTER the agent finishes its final response and the 60-second quiet window.
