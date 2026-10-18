---
title: Completion notification recovery boundary
created: 2026-10-14
updated: 2026-10-15
type: decision
tags: [operations, testing, privacy, ownership]
---

# Completion notification recovery boundary

The proposed source candidate is a narrow repair for a newly observed completion whose notification is still unexecuted. It does not reconstruct old completions from transcript history, change the public v1.8.0 pin, or authorize installation or Gateway activation. Keep these separate gates visible in [[runtime-boundaries]] and [[../operations|operations]].

The renderer records only metadata under the existing package-owned notification database. A stable candidate identity and revision fence duplicate windows, late registration/cancellation and pre-dispatch claims. The active and total row caps both matter: terminal cleanup is required so repeated sessions cannot grow the candidate metadata without bound. No message text, title, preview, Telegram destination or credential belongs in the candidate record or sender status projection.

An important SDK trap is that `host.profileRoutes()` is an inventory, not a connected-state proof, and the pinned `host.requestProfile()` API can cold-start a pooled backend. Recovery therefore must check the actual SDK gateway/profile/source atoms before any candidate-scoped profile RPC; otherwise it must retain/reject conservatively rather than warm a profile server. Same runtime means the exact local route, runtime/durable session identity, `started_at`, replay epoch and a complete contiguous replay after the original watermark. A healthy validated absence is different from a failed or malformed snapshot.

Keep the active target route separate from the default credential owner: the public captured active gateway can run the fixed `--profile default` CLI on the same local source. Do not require two profiles to be foreground simultaneously. Fresh nonforeground completions retain ordinary notifications without durable recovery. Socket unavailability is unverified, not confirmed identity change. Consume a bookkeeping sequence once while preserving the original event watermark; generation rotation fences late registration across observed OFF/ON. Capacity pressure must never remove still-protective cancellation fences; use bounded refusal instead.

The recovery-only 2026-10-14 source step kept the receiver and approval-v4 protocol at v1.8.0 while package/helper/renderer recovery was independently versioned at 1.8.1. Keep `loaded.version` as the approval compatibility value and require an explicit new recovery capability; the package version alone must never grant the new path. That historical step preserved old 1.8.0 approval-v4 behavior without changing receiver modules. The separate 2026-10-15 approval-coverage candidate retained the recovery capability/state logic and public v1.8.0 fixture while moving the source handshake to v5; the 2026-10-16 plugin-only canonical follow-on moved helper/renderer/receiver together to v6 without changing recovery. See [[runtime-boundaries]] for the three frozen modes, subscription/write boundary and consent gate.

Only an exact attempt's durable pre-request connection failure is retryable. Dispatching, unknown, malformed/lost helper receipts, read/write/protocol errors, and explicit Bot API rejection are not. Existing `sending`, `unknown`, `failed` and `sent` delivery rows are never reset; the same completion-v2 dedupe key arbitrates old and new helper paths. Sender counters are separate from receiver heartbeat and are not delivery proof.

Service/completion OFF retires only unexecuted candidates, and ON waits for cleanup. An in-flight Telegram write cannot be revoked; an external OFF→ON configuration edit that was never observed cannot be retroactively detected. Keep the recovery-only transport policy and operational test recipe in [[../operations|operations]], and do not broaden it to replies, followups or approvals.