# Source status and maintenance plan

Updated: 2026-10-20.

## One source, separate delivery states

| Surface | Status |
| --- | --- |
| Active source | One personal-use package, `session-complete-telegram`, in this repository. No public/private split. |
| Source version | 1.8.1, approval capability `desktop-command-approval-v6` and recovery capability `desktop-completion-recovery-v1`. No 1.8.1 release tag is published. |
| Last public release | Historical v1.8.0 at `4874276f519f68fdbacb9767f70c5ed760b5c6fb`; its approval-v4 bytes and tag remain immutable. |
| Catalog | Withdrawn. Upstream PRs #135956 and #135957 are closed and unmerged, not awaiting admission. |
| Local operation | Installation, loaded code, receiver readiness and human Telegram interaction require their own evidence; a main merge is not activation. |

## Current consolidation scope

The 2026-10-20 user request authorizes tidying the accumulated work into main. Consolidate completion-notification recovery, expanded approvals, compatibility/test isolation, default transport binding and concurrent-approval fixes through a squash PR. Keep the original working branch and its commits as historical evidence; do not force-push, rewrite tags or silently turn an earlier WIP/NO-GO/failed run into acceptance.

This is source integration and documentation cleanup, not a new implementation or deployment. Do not reinstall the plugin, change settings or credentials, send a Telegram test message, alter runtime databases/epochs, restart the shared Gateway, reopen catalog submissions or publish a new release as part of this step.

## Source evidence and its limits

- Functional source is pinned to `239806cc2383f065141660132a574561d0b80d12` (tree `fb63e2262c3243054594e6ac01ae0fd909f50fe1`). Consolidation changes documentation only; executable/package/test/workflow bytes must remain identical to that source.
- Its commit-bound GitHub Actions run `39353863839` succeeded: Windows/Ubuntu bridge tests for legacy/current/installed immutable core generations, plus the separate native-PM container suite for each generation. The workflow and actual job results, not a historical plan, define that execution coverage.
- The concurrent-approval fix has independent Heavy GO with Critical/High/Medium zero for that exact reviewed tree. This does not certify every future Hermes API or every user's installation.
- Earlier local native-install aggregates failed or were interrupted. Keep those receipts distinct from later focused passes and successful hosted native-PM runs; do not relabel an old exit 1/137 as success.
- Human Telegram reply/approval-button roundtrip and other users' GUI behavior remain unverified. Receiver heartbeat is readiness, not delivery or command-completion proof.
- Before completing integration, verify the exact PR head/tree, its CI, the resulting main commit/tree, and main-push CI. Synchronize the local main only from a clean checkout; retain the original working branch.

## Non-negotiable maintenance boundaries

- One package/CLI/handler factory on the existing Telegram application; no second poller or core/SDK/app patch.
- Preserve package IDs, default-home ownership, saved opt-in settings, private destination/card identity, exact prompt/generation bindings, TTL/leases, once/deny-only decisions and ambiguous-write no-replay.
- The canonical adapter uses feature-detected exported native utilities outside the documented plugin import allowlist. It is a compatibility adapter, not stable plugin-API certification. Unsupported hosts fail closed. Catalog withdrawal does not remove this compatibility limitation.
- Keep the existing SDK limitation visible: when the host cannot install the old-card rejection guard, the bridge refuses readiness but cannot stop ordinary Telegram chat input.
- Run canonical/session tests only with the before-first-access fixture database confinement in `tests/support/guarded_unittest.py`. Native-PM tests use the separate credential-free container path, never operational homes, config, SQLite stores or authentication mounts.
- Keep secrets, private conversations, runtime databases, inboxes, diagnostic content and private evidence out of Git. Do not reset ledgers/tombstones or weaken the native installer/scanner to make tests green.

## Historical checkpoints

`549b9d5` (2026-10-18) intentionally stored an incomplete WIP candidate after catalog withdrawal. Its source-only authorization did not then authorize a main merge, release or activation. Later corrections and the current integration request supersede that checkpoint's delivery state, not its historical facts or failures.

The former phase-by-phase PLAN is retained byte-for-byte in Git at `239806cc2383f065141660132a574561d0b80d12:PLAN.md`; inspect it with `git show 239806cc2383f065141660132a574561d0b80d12:PLAN.md`. It is not the current checklist. Detailed chronological findings remain in [the wiki log](docs/wiki/log.md); operational contracts remain in [operations](docs/operations.md), and public-release installation instructions remain in [public setup](docs/public-install.md).
