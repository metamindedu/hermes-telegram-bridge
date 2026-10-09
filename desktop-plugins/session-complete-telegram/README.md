# Session Completion → Telegram

## Completion replies (1.5.0)

After approved native Gateway restart, newly verified completion cards accept direct plain-text replies from the configured private-home owner. They submit through the captured Desktop route to the same in-memory session with `prompt.submit` and `queued:true`. No session handoff, resume, creation, interruption, or core change. Older cards lack trustworthy routing metadata and are not actionable.

- Up to 2000 characters, original text only; no edits, attachments, forwarded messages, slash commands or untrusted senders.
- Card eligibility lasts at most 24 hours and requires the original Desktop owner/native receiver leases. A temporary 45-second lease lapse blocks delivery but no longer permanently expires the card: the same owner may renew before the original TTL, with exact live-session checks still required. An input rejected/purged during the outage is never replayed; send a new reply after recovery. Reload/restart or changed runtime/durable identity is NOT recovered by this lease fix. Existing terminal bindings are never revived. The native receiver needs a Gateway restart to load the fix; files on disk alone are not activation evidence.
- Observed human-input waiting, missing/changed sessions, and backend voice mode reject the followup. Working sessions queue it. Accepted/queued feedback is not a claim of completed execution.
- Durable one-attempt claims prevent duplicate submission attempts. Unknown outcomes never retry. Snapshot checks are not atomic; a later waiting transition can still queue input, but cannot answer an approval.
- A normal Desktop send can include attachments already drafted there; this plugin never clears those drafts. Text enters the normal conversation and travels through local helper stdout, not helper command arguments.
- Followup-inbox uses a separate restricted directory; its failure must not stop clarify heartbeats or intercept mapped clarify replies. Minimal consumed-message identifiers remain after transient input text is purged.
- See `IMPLEMENTATION.md` for actual tests, independent review, activation gates, and limits. Do not assume new native code is live from disk version alone.

Local, app-wide Hermes Desktop plugin. Installed at:
`C:/Users/ovpow/AppData/Local/hermes/desktop-plugins/session-complete-telegram`.

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
- Explicitly detached services and satisfied non-completion readiness watches
  do not block. Unclassified running background commands conservatively block.
- Desktop must remain open/connected through the quiet window. Closing the app,
  losing the connection or a missing state contract fails closed (no notification).
  Remote SSH/cloud profiles are outside this local installation's scope.
- Delivery claims persist in
  `C:/Users/ovpow/AppData/Local/hermes/plugin-data/session-complete-telegram/notifications.sqlite3`.
  A network timeout is recorded as `unknown` and is not automatically resent;
  Telegram's send API has no idempotency key. Explicit rejected sends are also
  retained for inspection rather than retried noisily.

## Disable / remove

Disable **Session Completion → Telegram** in Desktop's plugin list. This retires
its event subscriptions/timers immediately. To uninstall, use Desktop's plugin
remove action or remove only this plugin folder. The sender ledger can be kept
for audit/deduplication. For the optional 1.4.0 reply bridge, also disable the
default native plugin with `hermes plugins disable desktop-clarify-telegram`.
Its handlers may remain wired until gateway restart but check enablement and
Desktop/receiver leases before accepting anything.

## Files and tests

- `plugin.js`: app lifecycle, scoped profile routing, quiet-state gate.
- `notifier.py`: read-only durable-state checks, SQLite at-most-once claims,
  existing Telegram credentials, fixed-destination sender.
- `plugin.test.mjs`: JS behavior/race/future-profile tests (Node built-in runner).
- `test_notifier.py`: Python tests using real Hermes `SessionDB` schema and
  isolated temporary homes; Telegram calls are stubbed ONLY in unit tests.

From this folder:

```
node --test plugin.test.mjs reply.test.mjs
# Run with the Python selected by pm.environments.project_python(repo):
python -m unittest test_notifier test_reply_bridge -v
```

No Git commit or global delegation-model/config change was made.

## Telegram answers (1.4.0)

- Only active `clarify` requests are actionable: single-choice, multiple-choice
  with Submit, and explicit text replies to the exact Telegram card. A batch
  waits for all answers before submitting one complete response to Desktop.
- Approval, sudo, secret/vault prompts and questions in final prose remain
  notification-only. No approval or secret-entry bypass exists.
- The existing default messaging gateway owns all Telegram updates. A native
  `desktop-clarify-telegram` plugin adds scoped PTB handlers; no second
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
- `reply_bridge.py` owns ingress/store/helper logic; `native/` contains the
  native-plugin installation sources. `reply.test.mjs` and `test_reply_bridge.py`
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

### Initial installation evidence

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
