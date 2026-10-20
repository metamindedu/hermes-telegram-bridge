---
title: Concurrent native approval cards
created: 2026-10-20
updated: 2026-10-20
type: gotcha
tags: [operations, testing, ownership]
---

# Concurrent native approval cards

A Desktop session can expose several simultaneous native approval requests, including parallel writes to different protected instruction files. A session is not an approval identity. Selecting only the first human request or keeping a single approval slot hides later approvals until the first settles; it also lets an earlier unrelated human request mask approvals.

Keep the session record and bounded per-SRQ approval slots, including the first approval before registration can fail. Approval slots must never share the parent's clarify/attention episode or transport-fence state; snapshot reordering must not move a failed request into a fresh slot. Every slot retains the unchanged native request/queue identity, full fingerprints, owner/route/epoch checks, current receiver admission and once/deny-only dispatch. Snapshot failure holds every affected card; verified request/session absence retires only the appropriate cards. One local encoding failure or unknown write must not suppress independent siblings, and an unknown card-registration/notice write must not be replayed. Distinguish admission-only owner-heartbeat failures from card writes: an uncertain heartbeat receipt defers registration and can be retried after a fresh snapshot; it must not permanently fence a card that was never registered.

Test at the actual producer boundary: open two real protected-write requests together, assert both remain pending and produce distinct complete cards through the real Python inbox with mocked Telegram only, then deny exact requests independently. Consume the synthetic native snapshots and actual card receipts through the renderer as a separate seam check. Neither test is a human Telegram roundtrip.

Do not infer the complete historical count from terminal inbox rows: terminal cleanup removes request/review bodies, and cards settled from Desktop are edited to a command-free terminal status. A healthy configuration, fresh receiver heartbeat, successful Telegram send, renderer activation and human acceptance are separate facts.

The supported suite splits guarded bridge tests from native PM container tests. Running native installation inside the canonical child-process guard can correctly fail on a different managed uv executable; preserve that failed run rather than weakening the guard. The PM container uses a read-only root, no host profile/auth/DB mounts and an owned scratch volume.

See [[runtime-boundaries]] and [[completion-recovery]].
