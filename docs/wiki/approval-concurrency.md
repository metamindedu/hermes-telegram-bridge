---
title: Concurrent native approval cards
created: 2026-10-20
updated: 2026-10-23
type: gotcha
tags: [operations, testing, ownership]
---

# Concurrent native approval cards

A Desktop session can expose several simultaneous native approval requests, including parallel writes to different protected instruction files. A session is not an approval identity. Selecting only the first human request or keeping a single approval slot hides later approvals until the first settles; it also lets an earlier unrelated human request mask approvals.

Keep the session record and bounded per-SRQ approval slots, including the first approval before registration can fail. Approval slots must never share the parent's clarify/attention episode or transport-fence state; snapshot reordering must not move a failed request into a fresh slot. Every slot retains the unchanged native request/queue identity, full fingerprints, owner/route/epoch checks, current receiver admission and once/deny-only dispatch. Snapshot failure holds every affected card; verified request/session absence retires only the appropriate cards. One local encoding failure or unknown write must not suppress independent siblings, and an unknown card-registration/notice write must not be replayed. Distinguish admission-only owner-heartbeat failures from card writes: an uncertain heartbeat receipt defers registration and can be retried after a fresh snapshot; it must not permanently fence a card that was never registered.

Test at the actual producer boundary: open two real protected-write requests together, assert both remain pending and produce distinct complete cards through the real Python inbox with mocked Telegram only, then deny exact requests independently. Consume the synthetic native snapshots and actual card receipts through the renderer as a separate seam check. Neither test is a human Telegram roundtrip.

Do not infer the complete historical count from terminal inbox rows: terminal cleanup removes request/review bodies, and cards settled from Desktop are edited to a command-free terminal status. A healthy configuration, fresh receiver heartbeat, successful Telegram send, renderer activation and human acceptance are separate facts.

The supported suite splits guarded bridge tests from native PM container tests. Running native installation inside the canonical child-process guard can correctly fail on a different managed uv executable; preserve that failed run rather than weakening the guard. The PM container uses a read-only root, no host profile/auth/DB mounts and an owned scratch volume.

An unknown card is not a proven native absence. Preserve an unverified **open** card inert under its original absolute lifetime and the same live owner/receiver; ensure the freshness-denial marker survives the next housekeeping pass instead of accidentally becoming immediate expiry. Never rescue a queued decision after observation/lease failure or an ambiguous application write through this mechanism. Lost registration bookkeeping may be recovered only through an exact same-owner read-only lookup followed by a fresh native snapshot; this is not retransmission, owner transfer, terminal revival or canonical-registration recovery. Existing CLI deadlines remain separate.

A failed flush can overwrite an already-published registration result with a generic store error: the reason alone is not proof of prewrite failure. Retain the no-replay fence and read-only recovery path. Bound raw recovery bookkeeping to two minutes from the original attempt, with cleanup before receiver I/O and on verified native request/session absence; retain only the minimal same-request write fence afterward. Do not treat a transient lookup miss as proven absence.

The inbox's last-seen timestamp is not a terminal-transition timestamp. Compare current native request/queue/epoch fingerprints read-only, but do not calculate a historical expiry interval from last-seen or claim that a later healthy snapshot proves an unrecorded timeout.

When a callback was accepted but its decision was discarded, correlate the receiver's own profile logs with the card's first terminal reason/time, not its last-seen value. New bounded content-free diagnostics distinguish callback acceptance, Desktop claim and terminal phase; old rows without events remain historically unexplained. Missing/failed diagnostics do not prove no native write, and renderer-reported reasons are hints rather than authority. Preserve the existing no-replay fence and obtain source/activation evidence separately from readiness. See the operations runbook's content-free diagnostics section for retention, read-only lookup and rollback limits.

See [[runtime-boundaries]] and [[completion-recovery]].
