# Modular settings — public v1.8.0

Public immutable package pin: `4874276f519f68fdbacb9767f70c5ed760b5c6fb`. This current default-branch guide records the published release; docs frozen inside that pin keep their original preparation-time language. Catalog submission, maintainer admission, installation and actual live interaction are separate gates.

## Scope

`session-complete-telegram` keeps its existing native package, Desktop and CLI IDs. The dedicated **Telegram Bridge** settings page controls the default local home's service, which observes all local Desktop profiles. It does not clone credentials, edit other profiles' settings, start a second poller, or change Hermes core/model/approval policies.

The Desktop plugin enable switch, native plugin selection and the service switch are different controls. Disabling the Desktop plugin removes this page; re-enable it in the ordinary plugin list. Native plugin disablement, managed policy, unsupported routes and malformed configuration still deny bridge actions.

## Controls and preserved defaults

| Setting | Default | Meaning |
|---|---|---|
| `service_enabled` | true | Common switch for all five modules; individual values are retained while off. |
| `completion_notifications_enabled` | true | Completion notifications; also required for replies to completion cards. |
| `attention_notifications_enabled` | true | Input/approval-needed notifications; also required for question answers and command approvals. |
| `clarify_replies_enabled` | true | Restricts question-answer delivery; never grants the existing replies consent. |
| `completion_followups_enabled` | true | Restricts completion-card followups; never grants the existing replies consent. |
| `replies_enabled` | false | Preserved existing consent shared by question answers and followups. |
| `approvals_enabled` | false | Preserved existing consent for one-time command approval/denial only. |

The five effective modules are completion notifications, attention notifications, clarify replies, completion followups and approvals. They have separate choices, not independent authority: the common service/native selection, relevant parent-notification switch and original replies/approvals consent remain required. Installing code does not opt in to either existing default-off feature.

## Writes and revocation

The renderer uses the existing authenticated fixed native CLI with explicit `--profile default`; it never supplies an executable/path/shell command or writes YAML. The captured public `PluginContext.set_config` writes exactly one allowlisted boolean leaf. The helper checks the default installed owner, strict raw and native effective policy, expected current leaf and post-write readback. The expected-value preflight is not an atomic CAS against unrelated external writers.

One click dispatches at most one write. Missing, malformed or ambiguous post-write receipts require explicit refresh and never automatic retry or fallback transport. The remaining choices and unrelated configuration are preserved. Every selected inbox cleanup is attempted independently; re-enabling an OFF setting must finish its affected retirement first, and cleanup failure denies the ON write. A confirmed false value retires affected posting/open/queued/applying interactions; terminal deduplication/tombstones remain, and re-enabling never revives previously disabled cards or replays consumed writes. Completion sends capture the followup registration epoch before network I/O; registration checks the same epoch and current policy inside its inbox transaction. A late pre-OFF card remains reject-only even after ON. The revocation marker uses impossible `(0,0,0)` Telegram coordinates in the existing outbound table, without a schema migration or a new ledger. Disabling a feature cannot undo a native RPC already in flight.

Callbacks, intake, observation, claim and final dispatch reread the relevant module policy. Existing bot/private owner/card/route/session/request/queue/fingerprint/TTL/lease/no-replay checks remain mandatory. The known-card rejection boundary is unchanged: if the host cannot attach even the reject-only consumer, ordinary Gateway input can still fall through; see `../SECURITY.md`.

## Runtime boundary

Package/helper/renderer version is 1.8.0 with `desktop-command-approval-v4`. Mixed older/newer halves deny the versioned action handshake. This is not a database/schema migration. Preserve all existing config/auth/ledgers and use the previously reviewed Git pin for rollback.

A receiver heartbeat is a diagnostic observation, not proof of a Telegram send, handler admission, command execution or overall Gateway health. Failed status reads are shown as unverified, not as proof that saved switches are off. The page uses the actual SDK settings-page registration when available and the existing dedicated-route contributions on the older supported SDK.

Native Python code replacement does not hot-reload a resident Gateway factory. Publication, native installation/Doctor, app-level Desktop materialization, live settings rendering, safe shared-Gateway restart, handler activation and actual Telegram roundtrip are separate verification stages. Restart only after fresh shared-owner/active-work checks; preserve Desktop and unrelated jobs. Any live test of an off/default-off feature must stay within the user's actual consent and restore its exact previous value without replaying unknown writes.

## Pending completion-recovery source candidate (1.8.1; not public or installed)

The current public immutable release remains v1.8.0 at the pin above. A separate unpublished 1.8.1 source candidate adds a read-only **sender** summary beside (not inside) the existing receiver heartbeat: awaiting recovery, uncertain delivery, sent and expired counts, with no content or identifiers. The summary is not a delivery guarantee. The candidate retires unexecuted completion candidates before service/completion OFF→ON through the public settings writer; cleanup failure blocks re-enable. Dispatching writes cannot be revoked, and an external OFF→ON config edit that the plugin never observes cannot be detected retroactively. Recovery retains the same consent defaults and approval-v4 receiver compatibility; it does not change handler protocols or the public 1.8.0 pin. Independent review and all activation/release gates remain pending.

## Required verification

Run all Node suites in `.github/workflows/tests.yml`, including `settings.test.mjs`, `completion_recovery.test.mjs` and `recovery_review.test.mjs`, and all Python modules in `.github/workflows/tests.yml`, including `test_settings_control`. Keep real native PluginContext/CLI/PTB fixtures, exact historical rollback bytes, Unicode/frame/claim/security regressions and preservation assertions. Mock monitor imports are not native React evidence. Exercise actual React, native ToggleRow and TanStack pending/error/refresh behavior separately, then verify the real installed Hermes page and all-profile common scope. Final independent Heavy review binds the immutable candidate, complete receipts and explicit SDK boundary before publication/activation.
