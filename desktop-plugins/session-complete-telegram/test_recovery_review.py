"""Heavy-review regressions through the registered native CLI and real SQLite schema."""
from __future__ import annotations

import concurrent.futures
import contextlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from test_completion_recovery import CompletionRecoveryTests, ROUTE, TOKEN


class RecoveryReviewTests(CompletionRecoveryTests):
    def test_h5_route_scoped_list_never_mutates_other_profiles_or_connections(self):
        route_b = {"mode": "local", "connectionId": "connection-b", "profile": "claire",
                   "targetProfile": "claire"}
        home_b = self.home / "profiles" / "claire"
        final_b = self.make_session("route-b-session", home_b)
        b = self.record(self.identity(route=route_b, runtime_id="route-b-runtime",
                                      session_key="route-b-session", final_id=final_b))
        route_c = {"mode": "local", "connectionId": "connection-c", "profile": "default",
                   "targetProfile": "default"}
        c = self.record(self.identity(route=route_c, runtime_id="route-c-runtime",
                                      session_key="route-c-session"))
        self.assertTrue(b["ok"] and c["ok"], (b, c))
        with self.db() as db:
            db.execute("UPDATE completion_candidates SET destination_binding=? WHERE candidate_id IN (?,?)",
                       ("f" * 64, b["candidate_id"], c["candidate_id"]))
            db.commit()
            before = {row[0]: row[1:] for row in db.execute(
                "SELECT candidate_id,state,revision,claim_token,claim_until FROM completion_candidates "
                "WHERE candidate_id IN (?,?)", (b["candidate_id"], c["candidate_id"]))}
        a = self.record(self.identity(runtime_id="route-a-runtime", session_key="route-a-session"))
        self.assertTrue(a["ok"], a)
        listed = self.listing(ROUTE)
        self.assertTrue(listed["ok"], listed)
        self.assertEqual([item["candidate_id"] for item in listed["items"]], [a["candidate_id"]])
        with self.db() as db:
            after = {row[0]: row[1:] for row in db.execute(
                "SELECT candidate_id,state,revision,claim_token,claim_until FROM completion_candidates "
                "WHERE candidate_id IN (?,?)", (b["candidate_id"], c["candidate_id"]))}
        self.assertEqual(after, before, "listing route A must not touch another profile/connection")

    def test_h7_record_barrier_off_on_generation_blocks_late_insert(self):
        identity = self.identity(runtime_id="generation-race", session_key="generation-race-session")
        entered = threading.Event()
        release = threading.Event()
        original = self.recovery._resolve_source
        calls = 0

        def blocked(root, value):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                if not release.wait(20):
                    raise AssertionError("record barrier timed out")
            return original(root, value)

        result = []
        with patch.object(self.recovery, "_resolve_source", side_effect=blocked):
            worker = threading.Thread(target=lambda: result.append(self.record(identity)))
            worker.start()
            self.assertTrue(entered.wait(10), "registered record did not reach the barrier")
            # Same native SQLite generation transitions used by public OFF/ON;
            # each transaction rotates even though the delayed row does not exist.
            self.assertEqual(self.recovery.retire_unexecuted(self.state), 0)
            off = self.recovery.policy(self.state, enabled=False)
            self.assertFalse(off["enabled"])
            on = self.recovery.policy(self.state, enabled=True)
            self.assertTrue(on["enabled"])
            release.set()
            worker.join(30)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]["ok"], result[0])
        self.assertEqual(result[0]["reason"], "generation_changed")
        self.assertNotEqual(identity["generation"], on["generation"])
        self.assertEqual(self.listing()["items"], [])

    def test_h7_public_off_on_rotates_generation_with_no_candidate_rows(self):
        before = self.invoke({"action": "completion_recovery_policy"})
        self.assertTrue(before["ok"] and before["enabled"])
        off = self.invoke({"action": "settings_set", "key": "completion_notifications_enabled",
                           "value": False, "expected": True})
        self.assertTrue(off["ok"], off)
        disabled = self.invoke({"action": "completion_recovery_policy"})
        self.assertTrue(disabled["ok"])
        self.assertFalse(disabled["enabled"])
        self.assertNotEqual(before["generation"], disabled["generation"])
        on = self.invoke({"action": "settings_set", "key": "completion_notifications_enabled",
                          "value": True, "expected": False})
        self.assertTrue(on["ok"], on)
        enabled = self.invoke({"action": "completion_recovery_policy"})
        self.assertTrue(enabled["ok"] and enabled["enabled"])
        self.assertNotEqual(disabled["generation"], enabled["generation"])
        with self.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM completion_candidates").fetchone()[0], 0)
        stale = self.record(self.identity(runtime_id="disabled-window", session_key="disabled-window-session",
                                          generation=before["generation"]))
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["reason"], "generation_changed")

    def test_h6_two_native_connections_cas_loser_reads_winner_without_cancelling(self):
        item = self.record(self.identity(runtime_id="cas-runtime", session_key="cas-session"))
        self.assertTrue(item["ok"], item)
        with self.notifier.state_db(self.state):
            pass
        snapshot = self.listing()["items"][0]
        barrier = threading.Barrier(2)
        ref = {"candidate_id": snapshot["candidate_id"], "revision": snapshot["revision"],
               "generation": snapshot["generation"]}

        def claim_connection():
            barrier.wait(timeout=10)
            return self.recovery.claim(self.state, self.home, ref, ROUTE)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim_connection) for _ in range(2)]
            outcomes = [future.result(timeout=30) for future in futures]
        winners = [value for value in outcomes if value.get("ok") is True]
        losers = [value for value in outcomes if value.get("reason") == "candidate_changed"]
        self.assertEqual((len(winners), len(losers)), (1, 1), outcomes)
        winner = winners[0]
        exact = self.invoke({"action": "completion_recovery_get", "candidate_id": item["candidate_id"],
                             "route": ROUTE})
        self.assertTrue(exact["ok"], exact)
        self.assertEqual(exact["item"]["state"], "claimed")
        self.assertEqual(exact["item"]["revision"], winner["revision"])
        self.assertEqual(exact["item"]["generation"], item["generation"])
        with self.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT state FROM completion_candidates WHERE candidate_id=?",
                                        (item["candidate_id"],)).fetchone()[0], "claimed")

        calls = []
        def api(_token, method, payload):
            calls.append(method)
            if method == "sendMessage":
                return {"kind": "response", "response": {"ok": True, "result": {
                    "message_id": 811, "chat": {"id": -100123456, "type": "supergroup"},
                    "text": payload["text"]}}}
            return {"kind": "unknown"}
        with patch.object(self.notifier, "credentials", return_value=(TOKEN, -100123456, 7)), \
             patch.object(self.notifier, "completion_telegram", side_effect=api):
            sent = self.invoke(self.send_request(item, winner))
        self.assertTrue(sent["ok"], sent)
        self.assertEqual(sent["status"], "sent")
        self.assertEqual(calls, ["sendMessage", "editMessageText"])

    def test_h6_late_expired_claim_refuses_only_old_token_and_keeps_candidate(self):
        item = self.record(self.identity(runtime_id="late-claim", session_key="late-claim-session"))
        with self.notifier.state_db(self.state):
            pass
        old = self.claim(item)
        self.assertTrue(old["ok"], old)
        future = old["claim_until"] + 1
        key = self.notifier.completion_dedupe_key(self.home, "default", item["session_key"],
                                                  item["session_key"], item["final_row_id"])
        late = self.recovery.begin_dispatch(self.state, self.home,
            {"candidate_id": old["candidate_id"], "revision": old["revision"],
             "claim_token": old["claim_token"], "generation": old["generation"]}, ROUTE,
            dedupe_key=key, profile="default", session_key=item["session_key"],
            runtime_id=item["runtime_id"], started_at=item["started_at"], epoch=item["epoch"],
            final_row_id=item["final_row_id"], now=future)
        self.assertFalse(late["ok"])
        self.assertEqual(late["reason"], "claim_expired")
        with self.db() as db:
            row = db.execute("SELECT state,revision,claim_token FROM completion_candidates WHERE candidate_id=?",
                             (item["candidate_id"],)).fetchone()
            self.assertEqual(tuple(row), ("claimed", old["revision"], old["claim_token"]))
            self.assertEqual(db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)
        fresh = self.recovery.claim(self.state, self.home,
            {"candidate_id": item["candidate_id"], "revision": old["revision"],
             "generation": old["generation"]}, ROUTE, now=future)
        self.assertTrue(fresh["ok"], fresh)
        self.assertNotEqual(fresh["claim_token"], old["claim_token"])

    def test_m1_protected_terminal_rows(self):
        terminal_states = ("cancelled", "disabled", "expired", "sent", "unknown")
        for index, terminal in enumerate(terminal_states):
            with self.subTest(state=terminal):
                item = self.record(self.identity(runtime_id=f"terminal-{index}",
                    session_key=f"terminal-session-{index}"))
                self.assertTrue(item["ok"], item)
                with self.db() as db:
                    db.execute("UPDATE completion_candidates SET state=?,reason='new_work',expires=?,updated=? "
                               "WHERE candidate_id=?", (terminal, time.time() + 600, time.time(), item["candidate_id"]))
                    db.commit()
                with patch.object(self.recovery, "MAX_TOTAL", 1):
                    refused = self.record(self.identity(runtime_id=f"after-terminal-{index}",
                        session_key=f"after-terminal-session-{index}"))
                self.assertFalse(refused["ok"], refused)
                self.assertEqual(refused["reason"], "storage_full")
                with self.db() as db:
                    self.assertEqual(db.execute("SELECT state FROM completion_candidates WHERE candidate_id=?",
                                                 (item["candidate_id"],)).fetchone()[0], terminal)

    def test_m1_full_capacity_cancellation_uses_bounded_global_fence(self):
        existing = self.record(self.identity(runtime_id="capacity-existing", session_key="capacity-existing-session"))
        self.assertTrue(existing["ok"], existing)
        with patch.object(self.recovery, "MAX_TOTAL", 1):
            saturated = self.invoke({"action": "completion_recovery_cancel", "identity": {
                "route": ROUTE, "runtime_id": "unrecorded-cancel", "final_row_id": 98765},
                "reason": "new_work"})
            self.assertTrue(saturated["ok"], saturated)
            self.assertTrue(saturated.get("admission_fenced"))
            attempt = self.record(self.identity(runtime_id="fenced-admission", session_key="fenced-admission-session"))
            self.assertFalse(attempt["ok"], attempt)
            self.assertEqual(attempt["reason"], "admission_fence_active")
            with self.db() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM completion_candidates").fetchone()[0], 1)
                fence_until = db.execute("SELECT fence_until FROM completion_recovery_meta WHERE singleton=1").fetchone()[0]
                self.assertGreater(fence_until, time.time())
                self.assertEqual(db.execute("SELECT state FROM completion_candidates WHERE candidate_id=?",
                                            (existing["candidate_id"],)).fetchone()[0], "pending")

    def test_m2_utf8_budgeted_keyset_pages_and_exact_get(self):
        made = []
        for index in range(20):
            sid = f"후보😊{index:02d}" + ("界" * 155)
            final_id = self.make_session(sid)
            value = self.identity(runtime_id=sid, session_key=sid, final_id=final_id,
                epoch="🧠" * 98, started_at="🕒" * 98, seq=10 + index)
            result = self.record(value)
            self.assertTrue(result["ok"], result)
            made.append(result)
        cursor = None
        pages = []
        seen_cursors = set()
        for _ in range(256):
            page = self.invoke({"action": "completion_recovery_list", "route": ROUTE, "cursor": cursor})
            self.assertTrue(page["ok"], page)
            encoded = json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.assertLessEqual(len(encoded), self.recovery.MAX_RESULT_BYTES)
            self.assertLessEqual(len(page["items"]), 16)
            ids = [item["candidate_id"] for item in page["items"]]
            self.assertEqual(ids, sorted(ids))
            pages.extend(page["items"])
            next_cursor = page["cursor"]
            if next_cursor is None:
                break
            self.assertIsInstance(next_cursor, str)
            self.assertRegex(next_cursor, r"^[0-9a-f]{64}$")
            self.assertNotEqual(next_cursor, cursor)
            self.assertNotIn(next_cursor, seen_cursors)
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            self.fail("native keyset pagination exceeded 256 pages")
        listed_ids = [item["candidate_id"] for item in pages]
        expected = sorted(item["candidate_id"] for item in made)
        self.assertEqual(listed_ids, expected)
        self.assertEqual(len(listed_ids), len(set(listed_ids)))
        self.assertGreater(len(pages), 16)
        item_id = pages[0]["candidate_id"]
        exact = self.invoke({"action": "completion_recovery_get", "candidate_id": item_id, "route": ROUTE})
        self.assertTrue(exact["ok"], exact)
        self.assertEqual(exact["item"], pages[0])
        self.assertEqual(exact["item"]["bookkeeping_seq"], exact["item"]["seq"])
        self.assertEqual(exact["item"]["generation"], self.policy["generation"])

    def test_h4_touch_consumes_new_bookkeeping_once_without_extending_ttl(self):
        item = self.record(self.identity(runtime_id="bookkeeping", session_key="bookkeeping-session"))
        self.assertEqual(item["bookkeeping_seq"], item["seq"])
        initial_revision = item["revision"]
        initial_quiet = item["quiet_at"]
        initial_expiry = item["expires_at"]
        same = self.invoke({"action": "completion_recovery_touch", "candidate": {
            "candidate_id": item["candidate_id"], "revision": item["revision"],
            "generation": item["generation"]}, "bookkeeping_seq": item["seq"],
            "quiet_at": time.time()})
        self.assertTrue(same["ok"], same)
        self.assertEqual(same["revision"], initial_revision)
        self.assertEqual(same["quiet_at"], initial_quiet)
        self.assertEqual(same["expires_at"], initial_expiry)
        advanced = self.invoke({"action": "completion_recovery_touch", "candidate": {
            "candidate_id": item["candidate_id"], "revision": item["revision"],
            "generation": item["generation"]}, "bookkeeping_seq": item["seq"] + 1,
            "quiet_at": time.time()})
        self.assertTrue(advanced["ok"], advanced)
        self.assertEqual(advanced["revision"], initial_revision + 1)
        self.assertEqual(advanced["bookkeeping_seq"], item["seq"] + 1)
        repeated = self.invoke({"action": "completion_recovery_touch", "candidate": {
            "candidate_id": item["candidate_id"], "revision": item["revision"],
            "generation": item["generation"]}, "bookkeeping_seq": item["seq"] + 1,
            "quiet_at": time.time() + 10})
        self.assertTrue(repeated["ok"], repeated)
        self.assertEqual(repeated["revision"], advanced["revision"])
        self.assertEqual(repeated["quiet_at"], advanced["quiet_at"])
        self.assertEqual(repeated["expires_at"], initial_expiry)
        stale_newer = self.invoke({"action": "completion_recovery_touch", "candidate": {
            "candidate_id": item["candidate_id"], "revision": item["revision"],
            "generation": item["generation"]}, "bookkeeping_seq": item["seq"] + 2,
            "quiet_at": time.time() + 10})
        self.assertEqual(stale_newer["reason"], "candidate_changed")
        exact = self.invoke({"action": "completion_recovery_get", "candidate_id": item["candidate_id"],
                             "route": ROUTE})["item"]
        self.assertEqual(exact["bookkeeping_seq"], item["seq"] + 1)
        self.assertEqual(exact["expires_at"], initial_expiry)

    def test_m3_native_quiet_59_retries_and_60_dispatches_before_ledger(self):
        now = float(int(time.time()))
        item = self.record(self.identity(runtime_id="quiet-boundary", session_key="quiet-boundary-session",
                                         created_at=now - 120, quiet_at=now - 59))
        self.assertTrue(item["ok"], item)
        claim = self.claim(item)
        self.assertTrue(claim["ok"], claim)
        request = self.send_request(item, claim)
        with patch.object(self.recovery, "time", SimpleNamespace(time=lambda: now)):
            before = self.invoke(request)
        self.assertEqual((before.get("phase"), before.get("retryable"), before.get("reason")),
                         ("pre_dispatch", True, "quiet_period"))
        with self.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)
            row = db.execute("SELECT state,revision FROM completion_candidates WHERE candidate_id=?",
                             (item["candidate_id"],)).fetchone()
            self.assertEqual(tuple(row), ("claimed", claim["revision"]))
        calls = []
        def transport(_token, method, _payload):
            calls.append(method)
            return {"kind": "proven_unsent"}
        with patch.object(self.recovery, "time", SimpleNamespace(time=lambda: now + 1)), \
             patch.object(self.notifier, "credentials", return_value=(TOKEN, -100123456, 7)), \
             patch.object(self.notifier, "completion_telegram", side_effect=transport):
            at_boundary = self.invoke(request)
        self.assertEqual(at_boundary.get("status"), "unsent_retryable", at_boundary)
        self.assertEqual(calls, ["sendMessage"])
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status FROM deliveries").fetchone()[0], "unsent_retryable")

    def test_l1_diagnostics_projects_expiry_without_writing_and_dispatching_stays_uncertain(self):
        pending = self.record(self.identity(runtime_id="projection-pending", session_key="projection-pending-session"))
        before = (self.state / "notifications.sqlite3").read_bytes()
        projected = self.recovery.diagnostics(self.state, now=pending["expires_at"] + 1)
        self.assertEqual(projected["awaiting_recovery"], 0)
        self.assertEqual(projected["expired"], 1)
        self.assertEqual((self.state / "notifications.sqlite3").read_bytes(), before)

        dispatching = self.record(self.identity(runtime_id="projection-dispatching", session_key="projection-dispatching-session"))
        claim = self.claim(dispatching)
        key = self.notifier.completion_dedupe_key(self.home, "default", dispatching["session_key"],
                                                  dispatching["session_key"], dispatching["final_row_id"])
        with self.notifier.state_db(self.state):
            pass
        started = self.recovery.begin_dispatch(self.state, self.home, {
            "candidate_id": claim["candidate_id"], "revision": claim["revision"],
            "claim_token": claim["claim_token"], "generation": claim["generation"]}, ROUTE,
            dedupe_key=key, profile="default", session_key=dispatching["session_key"],
            runtime_id=dispatching["runtime_id"], started_at=dispatching["started_at"],
            epoch=dispatching["epoch"], final_row_id=dispatching["final_row_id"])
        self.assertTrue(started["ok"], started)
        with self.db() as db:
            db.execute("UPDATE completion_candidates SET expires=? WHERE candidate_id=?",
                       (time.time() - 1, dispatching["candidate_id"]))
            db.commit()
        before = (self.state / "notifications.sqlite3").read_bytes()
        uncertain = self.recovery.diagnostics(self.state, now=time.time())
        self.assertGreaterEqual(uncertain["uncertain_delivery"], 1)
        self.assertEqual((self.state / "notifications.sqlite3").read_bytes(), before)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT state FROM completion_candidates WHERE candidate_id=?",
                                        (dispatching["candidate_id"],)).fetchone()[0], "dispatching")
            self.assertEqual(db.execute("SELECT status FROM deliveries WHERE dedupe_key=?", (key,)).fetchone()[0], "sending")

    def test_h8_native_read_and_credential_failures_are_typed_pre_dispatch(self):
        item = self.record(self.identity(runtime_id="typed-read", session_key="typed-read-session"))
        with self.notifier.state_db(self.state):
            pass
        claim = self.claim(item)
        db_path = self.home / "state.db"
        moved = self.home / "state.db.fixture-held"
        db_path.rename(moved)
        try:
            read_error = self.invoke(self.send_request(item, claim))
        finally:
            moved.rename(db_path)
        self.assertEqual((read_error.get("phase"), read_error.get("retryable"), read_error.get("reason")),
                         ("pre_dispatch", True, "source_storage_unavailable"))
        with self.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT state FROM completion_candidates WHERE candidate_id=?",
                                        (item["candidate_id"],)).fetchone()[0], "claimed")

        item2 = self.record(self.identity(runtime_id="typed-credential", session_key="typed-credential-session"))
        claim2 = self.claim(item2)
        env = self.home / ".env"
        moved_env = self.home / ".env.fixture-held"
        env.rename(moved_env)
        try:
            credential_error = self.invoke(self.send_request(item2, claim2))
        finally:
            moved_env.rename(env)
        self.assertEqual((credential_error.get("phase"), credential_error.get("retryable"),
                          credential_error.get("reason")),
                         ("pre_dispatch", True, "credentials_unavailable"))
        with self.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT state FROM completion_candidates WHERE candidate_id=?",
                                        (item2["candidate_id"],)).fetchone()[0], "claimed")

        item3 = self.record(self.identity(runtime_id="typed-ledger", session_key="typed-ledger-session"))
        claim3 = self.claim(item3)
        with patch.object(self.notifier, "state_db", side_effect=OSError("fixture storage error")):
            storage_error = self.invoke(self.send_request(item3, claim3))
        self.assertEqual((storage_error.get("phase"), storage_error.get("retryable"),
                          storage_error.get("reason")),
                         ("pre_dispatch", True, "recovery_storage_unavailable"))

    def test_h8_ambiguous_telegram_send_is_unknown_and_never_replayed(self):
        item = self.record(self.identity(runtime_id="ambiguous-send", session_key="ambiguous-send-session"))
        claim = self.claim(item)
        sends = []
        def timeout(_token, method, _payload):
            sends.append(method)
            return {"kind": "unknown"}
        with patch.object(self.notifier, "credentials", return_value=(TOKEN, -100123456, 7)), \
             patch.object(self.notifier, "completion_telegram", side_effect=timeout):
            first = self.invoke(self.send_request(item, claim))
        self.assertEqual(first.get("status"), "unknown", first)
        self.assertEqual(sends, ["sendMessage"])
        exact = self.invoke({"action": "completion_recovery_get", "candidate_id": item["candidate_id"],
                             "route": ROUTE})
        self.assertEqual(exact["item"]["state"], "unknown")
        retry = self.invoke({"action": "completion_recovery_claim", "candidate": {
            "candidate_id": item["candidate_id"], "revision": exact["item"]["revision"],
            "generation": item["generation"]}, "route": ROUTE})
        self.assertFalse(retry["ok"])
        self.assertEqual(retry["reason"], "candidate_not_claimable")
        self.assertEqual(sends, ["sendMessage"])
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status FROM deliveries").fetchone()[0], "unknown")

    def test_h9_question_classification_uses_ordinary_attention_without_recovery_send(self):
        with contextlib.closing(sqlite3.connect(self.home / "state.db")) as db:
            db.execute("UPDATE messages SET content='계속 진행할까요?',timestamp=? WHERE id=?",
                       (time.time() - 120, self.final_id))
            db.commit()
        item = self.record(self.identity(runtime_id=self.sid, session_key=self.sid))
        self.assertTrue(item["ok"], item)
        with self.notifier.state_db(self.state):
            pass
        request = {"action": "completion_recovery_classify", "route": ROUTE,
                   "profile": "default", "session_key": self.sid, "final_row_id": self.final_id,
                   "desktop_observed": True}
        classification = self.invoke(request)
        self.assertEqual(classification, {"ok": True, "kind": "attention"})
        claim = self.claim(item)
        recovery_sends = []
        with patch.object(self.notifier, "completion_telegram", side_effect=lambda *args: recovery_sends.append(args)):
            handoff = self.invoke(self.send_request(item, claim))
        self.assertEqual(handoff, {"ok": False, "phase": "pre_dispatch", "retryable": False,
                                   "reason": "attention_required"})
        self.assertEqual(recovery_sends, [])
        with self.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT state FROM completion_candidates WHERE candidate_id=?",
                                        (item["candidate_id"],)).fetchone()[0], "claimed")
        self.invoke({"action": "completion_recovery_cancel", "identity": {
            "route": ROUTE, "runtime_id": item["runtime_id"], "final_row_id": item["final_row_id"]},
            "reason": "new_work"})
        ordinary_calls = []
        def ordinary(_token, method, payload):
            ordinary_calls.append(method)
            mid = 900 if method == "sendMessage" else 900
            if method == "sendMessage":
                return {"ok": True, "result": {"message_id": mid, "chat": {"id": -100123456},
                                                   "text": payload["text"]}}
            return {"ok": True, "result": {"message_id": mid}}
        original_deliver = self.notifier.deliver
        def deliver_with_api(req, **kwargs):
            return original_deliver(req, api=ordinary, **kwargs)
        with patch.object(self.notifier, "credentials", return_value=(TOKEN, -100123456, 7)), \
             patch.object(self.notifier, "deliver", side_effect=deliver_with_api):
            delivered = self.invoke({"action": "send", "profile": "default", "session_key": self.sid,
                                     "final_row_id": self.final_id, "desktop_observed": True})
        self.assertTrue(delivered["ok"], delivered)
        self.assertEqual(delivered["kind"], "question")
        self.assertEqual(ordinary_calls, ["sendMessage", "editMessageText"])

        off = self.invoke({"action": "settings_set", "key": "completion_notifications_enabled",
                           "value": False, "expected": True})
        self.assertTrue(off["ok"], off)
        question_sid = "attention-while-completion-off"
        question_final = self.make_session(question_sid)
        with contextlib.closing(sqlite3.connect(self.home / "state.db")) as db:
            db.execute("UPDATE messages SET content='계속 진행할까요?',timestamp=? WHERE id=?",
                       (time.time() - 120, question_final))
            db.commit()
        off_request = {"action": "completion_recovery_classify", "route": ROUTE,
                       "profile": "default", "session_key": question_sid,
                       "final_row_id": question_final, "desktop_observed": True}
        again = self.invoke(off_request)
        self.assertEqual(again, {"ok": True, "kind": "attention"})
        off_calls = []
        def ordinary_off(_token, method, payload):
            off_calls.append(method)
            if method == "sendMessage":
                return {"ok": True, "result": {"message_id": 901,
                    "chat": {"id": -100123456}, "text": payload["text"]}}
            return {"ok": True, "result": {"message_id": 901}}
        def deliver_off(req, **kwargs):
            return original_deliver(req, api=ordinary_off, **kwargs)
        with patch.object(self.notifier, "credentials", return_value=(TOKEN, -100123456, 7)), \
             patch.object(self.notifier, "deliver", side_effect=deliver_off):
            ordinary_handoff = self.invoke({"action": "send", "profile": "default",
                "session_key": question_sid, "final_row_id": question_final, "desktop_observed": True})
        self.assertTrue(ordinary_handoff["ok"], ordinary_handoff)
        self.assertEqual(ordinary_handoff["kind"], "question")
        self.assertEqual(off_calls, ["sendMessage", "editMessageText"])
        disabled_question = self.invoke({"action": "settings_set", "key": "attention_notifications_enabled",
                                        "value": False, "expected": True})
        self.assertTrue(disabled_question["ok"], disabled_question)
        classified_disabled = self.invoke(request)
        self.assertEqual(classified_disabled, {"ok": True, "kind": "disabled"})

    def test_h10_followup_epoch_is_captured_before_send_and_private_binding_is_real(self):
        # Private recipient verification is required; the usual supergroup fixture
        # intentionally cannot create a completion-followup binding.
        (self.home / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_HOME_CHANNEL=99\n", encoding="utf-8")
        enabled = self.invoke({"action": "settings_set", "key": "replies_enabled",
                               "value": True, "expected": False})
        self.assertTrue(enabled["ok"], enabled)
        followup = __import__(f"{self.name}.followup_bridge", fromlist=["*"])
        followup.bind_receiver(123456, 99, None, "private-boot", private_verified=True, state=self.state)
        followup.heartbeat_owner("followup-owner", state=self.state)
        item = self.record(self.identity(runtime_id=self.sid, session_key=self.sid))
        claim = self.claim(item)
        request = self.send_request(item, claim)
        request["followup"] = {"owner": "followup-owner", "connectionId": "local", "profile": "default",
            "targetProfile": "default", "sid": item["runtime_id"], "session_key": item["session_key"],
            "epoch": item["epoch"], "started_at": item["started_at"], "final_row_id": item["final_row_id"]}
        events = []
        capture = followup.capture_registration_epoch
        def capture_epoch(**kwargs):
            events.append("capture")
            return capture(**kwargs)
        def transport(_token, method, payload):
            events.append(method)
            mid = 991
            if method == "sendMessage":
                return {"kind": "response", "response": {"ok": True, "result": {
                    "message_id": mid, "chat": {"id": 99, "type": "private"},
                    "from": {"id": 123456, "is_bot": True}, "text": payload["text"]}}}
            return {"kind": "response", "response": {"ok": True, "result": {"message_id": mid}}}
        with patch.object(followup, "capture_registration_epoch", side_effect=capture_epoch), \
             patch.object(self.notifier, "credentials", return_value=(TOKEN, 99, None)), \
             patch.object(self.notifier, "completion_telegram", side_effect=transport):
            result = self.invoke(request)
        self.assertTrue(result["ok"], result)
        self.assertIn("binding_id", result)
        self.assertEqual(events, ["capture", "sendMessage", "editMessageText"])
        self.assertEqual(followup.lookup_outbound(123456, 99, 991, state=self.state)["kind"], "completion")

    def test_h10_followup_off_on_during_send_keeps_old_card_reject_only(self):
        (self.home / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_HOME_CHANNEL=99\n", encoding="utf-8")
        enabled = self.invoke({"action": "settings_set", "key": "replies_enabled",
                               "value": True, "expected": False})
        self.assertTrue(enabled["ok"], enabled)
        followup = __import__(f"{self.name}.followup_bridge", fromlist=["*"])
        followup.bind_receiver(123456, 99, None, "private-boot", private_verified=True, state=self.state)
        followup.heartbeat_owner("followup-owner", state=self.state)
        item = self.record(self.identity(runtime_id=self.sid, session_key=self.sid))
        claim = self.claim(item)
        request = self.send_request(item, claim)
        request["followup"] = {"owner": "followup-owner", "connectionId": "local", "profile": "default",
            "targetProfile": "default", "sid": item["runtime_id"], "session_key": item["session_key"],
            "epoch": item["epoch"], "started_at": item["started_at"], "final_row_id": item["final_row_id"]}
        toggles = []
        def transport(_token, method, payload):
            if method == "sendMessage":
                off = self.invoke({"action": "settings_set", "key": "replies_enabled",
                                   "value": False, "expected": True})
                on = self.invoke({"action": "settings_set", "key": "replies_enabled",
                                  "value": True, "expected": False})
                toggles.extend((off.get("ok"), on.get("ok")))
                return {"kind": "response", "response": {"ok": True, "result": {
                    "message_id": 992, "chat": {"id": 99, "type": "private"},
                    "from": {"id": 123456, "is_bot": True}, "text": payload["text"]}}}
            return {"kind": "response", "response": {"ok": True, "result": {"message_id": 992}}}
        with patch.object(self.notifier, "credentials", return_value=(TOKEN, 99, None)), \
             patch.object(self.notifier, "completion_telegram", side_effect=transport):
            result = self.invoke(request)
        self.assertEqual(toggles, [True, True])
        self.assertTrue(result["ok"], result)
        self.assertNotIn("binding_id", result)
        self.assertEqual(followup.lookup_outbound(123456, 99, 992, state=self.state),
                         {"kind": "legacy", "binding": None})

    def test_h10_followup_destination_change_after_send_does_not_rebind_private_card(self):
        (self.home / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_HOME_CHANNEL=99\n", encoding="utf-8")
        enabled = self.invoke({"action": "settings_set", "key": "replies_enabled",
                               "value": True, "expected": False})
        self.assertTrue(enabled["ok"], enabled)
        followup = __import__(f"{self.name}.followup_bridge", fromlist=["*"])
        followup.bind_receiver(123456, 99, None, "private-boot", private_verified=True, state=self.state)
        followup.heartbeat_owner("followup-owner", state=self.state)
        item = self.record(self.identity(runtime_id=self.sid, session_key=self.sid))
        claim = self.claim(item)
        request = self.send_request(item, claim)
        request["followup"] = {"owner": "followup-owner", "connectionId": "local", "profile": "default",
            "targetProfile": "default", "sid": item["runtime_id"], "session_key": item["session_key"],
            "epoch": item["epoch"], "started_at": item["started_at"], "final_row_id": item["final_row_id"]}
        credential_reads = 0
        def rotating_credentials(_root):
            nonlocal credential_reads
            credential_reads += 1
            return (TOKEN, 99 if credential_reads <= 2 else 100, None)
        def transport(_token, method, payload):
            if method == "sendMessage":
                return {"kind": "response", "response": {"ok": True, "result": {
                    "message_id": 993, "chat": {"id": 99, "type": "private"},
                    "from": {"id": 123456, "is_bot": True}, "text": payload["text"]}}}
            return {"kind": "response", "response": {"ok": True, "result": {"message_id": 993}}}
        with patch.object(self.notifier, "credentials", side_effect=rotating_credentials), \
             patch.object(self.notifier, "completion_telegram", side_effect=transport):
            result = self.invoke(request)
        self.assertTrue(result["ok"], result)
        self.assertNotIn("binding_id", result)
        self.assertGreaterEqual(credential_reads, 3)
        outbound = followup.lookup_outbound(123456, 99, 993, state=self.state)
        self.assertFalse(isinstance(outbound, dict) and outbound.get("kind") == "completion", outbound)
        with followup.connect(self.state) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM bindings WHERE outbound_mid=993").fetchone()[0], 0)

    def test_h11_default_alias_resolves_actual_profile_home_and_dispatches(self):
        profile_home = self.home / "profiles" / "claire"
        final_id = self.make_session("alias-session", profile_home)
        alias_identity = self.identity(runtime_id="alias-runtime", session_key="alias-session",
                                       final_id=final_id, route=ROUTE)
        item = self.record(alias_identity)
        self.assertTrue(item["ok"], item)
        with self.db() as db:
            row = db.execute("SELECT resolved_profile,source_binding FROM completion_candidates WHERE candidate_id=?",
                             (item["candidate_id"],)).fetchone()
        self.assertEqual(row[0], "claire")
        self.assertNotEqual(row[1], self.recovery._source_binding(self.home))
        claim = self.claim(item)
        def transport(_token, method, payload):
            if method == "sendMessage":
                return {"kind": "response", "response": {"ok": True, "result": {
                    "message_id": 777, "chat": {"id": -100123456, "type": "supergroup"},
                    "text": payload["text"]}}}
            return {"kind": "unknown"}
        with patch.object(self.notifier, "credentials", return_value=(TOKEN, -100123456, 7)), \
             patch.object(self.notifier, "completion_telegram", side_effect=transport):
            result = self.invoke(self.send_request(item, claim))
        self.assertEqual(result.get("status"), "sent", result)

    def test_h11_ambiguous_default_alias_and_junction_retarget_fail_closed(self):
        sid = "ambiguous-alias-session"
        first_home = self.home / "profiles" / "claire"
        second_home = self.home / "profiles" / "other"
        first_final = self.make_session(sid, first_home)
        second_final = self.make_session(sid, second_home)
        self.assertEqual(first_final, second_final)
        ambiguous = self.record(self.identity(runtime_id="ambiguous-runtime", session_key=sid,
                                              final_id=first_final, route=ROUTE))
        self.assertFalse(ambiguous["ok"], ambiguous)
        self.assertEqual(ambiguous["reason"], "source_identity_unavailable")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM completion_candidates").fetchone()[0], 0)

        source_home = self.home / "profiles" / "retargeted"
        other_home = self.home / "profiles" / "actual-target"
        final_id = self.make_session("junction-session", source_home)
        self.make_session("junction-session", other_home)
        route = {"mode": "local", "connectionId": "connection-retarget", "profile": "retargeted",
                 "targetProfile": "retargeted"}
        candidate = self.record(self.identity(route=route, runtime_id="junction-runtime",
            session_key="junction-session", final_id=final_id))
        self.assertTrue(candidate["ok"], candidate)
        held_home = self.home / "profiles" / "retargeted-original"
        source_home.rename(held_home)
        try:
            if os.name == "nt":
                subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(source_home), str(other_home)],
                               check=True, capture_output=True)
            else:
                source_home.symlink_to(other_home, target_is_directory=True)
            listing = self.invoke({"action": "completion_recovery_list", "route": route})
            self.assertTrue(listing["ok"], listing)
            self.assertEqual(listing["items"], [])
            with self.db() as db:
                self.assertEqual(db.execute("SELECT state FROM completion_candidates WHERE candidate_id=?",
                                            (candidate["candidate_id"],)).fetchone()[0], "cancelled")
        finally:
            if source_home.exists() or source_home.is_symlink():
                if source_home.is_symlink() or getattr(source_home, "is_junction", lambda: False)():
                    source_home.unlink()
                else:
                    shutil.rmtree(source_home)
            held_home.rename(source_home)

    def test_expired_foreign_route_metadata_releases_global_capacity(self):
        route_b = {**ROUTE, "connectionId": "other-local-source"}
        with patch.object(self.recovery, "MAX_ACTIVE", 1), patch.object(self.recovery, "MAX_TOTAL", 1):
            old = self.record(self.identity(runtime_id="old-route"))
            self.assertTrue(old["ok"], old)
            with self.notifier.state_db(self.state) as db:
                db.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?,?)", ("a" * 64, "default", "old", "sending", 1, None, 0))
            protected = self.record(self.identity(route=route_b, runtime_id="new-route"))
            self.assertFalse(protected["ok"])
            later = time.time() + self.recovery.TTL_SECONDS + 2
            with patch.object(self.recovery, "time", SimpleNamespace(time=lambda: later)):
                admitted = self.record(self.identity(route=route_b, runtime_id="new-route",
                    created_at=later-1, quiet_at=later-1))
            self.assertTrue(admitted["ok"], admitted)
            with self.db() as db:
                self.assertEqual(db.execute("SELECT * FROM deliveries").fetchall(),
                    [("a" * 64, "default", "old", "sending", 1.0, None, 0)])
                self.assertEqual(db.execute("SELECT count(*) FROM completion_candidates").fetchone()[0], 1)

    def test_source_home_io_error_preserves_candidate_then_recovers(self):
        source_home = self.home / "profiles" / "claire"
        final = self.make_session("io-session", source_home)
        route = {**ROUTE, "profile": "claire", "targetProfile": "claire"}
        item = self.record(self.identity(route=route, runtime_id="io-runtime", session_key="io-session", final_id=final))
        self.assertTrue(item["ok"], item)
        with self.db() as db:
            before = db.execute("SELECT * FROM completion_candidates").fetchall()
        for method, failure in (("resolve", PermissionError), ("stat", OSError)):
            original = getattr(Path, method)
            def failing(path, *args, **kwargs):
                if path == source_home:
                    raise failure("synthetic path I/O failure")
                return original(path, *args, **kwargs)
            with self.subTest(method=method), patch.object(Path, method, failing):
                result = self.invoke({"action":"completion_recovery_get", "candidate_id":item["candidate_id"], "route":route})
                self.assertFalse(result["ok"], result)
                self.assertEqual(result.get("phase"), "pre_dispatch", result)
                self.assertIs(result.get("retryable"), True, result)
            with self.db() as db:
                self.assertEqual(db.execute("SELECT * FROM completion_candidates").fetchall(), before)
        recovered = self.invoke({"action":"completion_recovery_get", "candidate_id":item["candidate_id"], "route":route})
        self.assertTrue(recovered["ok"], recovered)
        self.assertEqual(recovered["item"]["state"], "pending")
        claimed = self.invoke({"action":"completion_recovery_claim", "candidate":{
            "candidate_id":item["candidate_id"], "revision":item["revision"], "generation":item["generation"]}, "route":route})
        self.assertTrue(claimed["ok"], claimed)
        request = self.send_request(item, claimed, final_id=final, route=route,
            profile="claire", runtime_id="io-runtime", session_key="io-session")
        with patch.object(self.notifier, "profile_home", side_effect=PermissionError("synthetic source access")):
            blocked = self.invoke(request)
        self.assertEqual(blocked.get("phase"), "pre_dispatch", blocked)
        self.assertIs(blocked.get("retryable"), True, blocked)
        calls = []
        def transport(_token, method, payload):
            calls.append(method)
            if method == "sendMessage":
                return {"kind":"response", "response":{"ok":True, "result":{
                    "message_id":889, "chat":{"id":-100123456, "type":"supergroup"}, "text":payload["text"]}}}
            return {"kind":"unknown"}
        with patch.object(self.notifier, "completion_telegram", side_effect=transport):
            sent = self.invoke(request)
        self.assertEqual(sent.get("status"), "sent", sent)
        self.assertEqual(calls.count("sendMessage"), 1)

    def test_default_alias_enumeration_io_is_retryable_without_state_change(self):
        alias_home = self.home / "profiles" / "claire"
        final = self.make_session("alias-io-session", alias_home)
        item = self.record(self.identity(runtime_id="alias-io-runtime", session_key="alias-io-session", final_id=final))
        self.assertTrue(item["ok"], item)
        claim = self.claim(item)
        self.assertTrue(claim["ok"], claim)
        send = self.send_request(item, claim, final_id=final,
            runtime_id="alias-io-runtime", session_key="alias-io-session")
        actions = [
            {"action":"completion_recovery_list", "route":ROUTE},
            {"action":"completion_recovery_get", "route":ROUTE, "candidate_id":item["candidate_id"]},
            {"action":"completion_recovery_claim", "route":ROUTE, "candidate":{
                "candidate_id":item["candidate_id"], "revision":claim["revision"], "generation":item["generation"]}},
            {**send, "action":"completion_recovery_classify"}, send,
        ]
        with self.db() as db:
            before = db.execute("SELECT * FROM completion_candidates").fetchall()
        original = os.scandir
        class UnreadableEntry:
            def __init__(self, entry):
                self.name, self.path = entry.name, entry.path
            def is_dir(self, **kwargs):
                raise PermissionError("synthetic entry access failure")
            def stat(self, **kwargs):
                raise PermissionError("synthetic entry metadata failure")
        class UnreadableIterator:
            def __init__(self, iterator): self.iterator = iterator
            def __enter__(self): return self
            def __exit__(self, *args): self.iterator.close()
            def __iter__(self): return (UnreadableEntry(entry) for entry in self.iterator)
        for mode in ("enumeration", "entry"):
            def failing_scandir(path):
                if Path(path) == self.home / "profiles":
                    if mode == "enumeration": raise PermissionError("synthetic profiles enumeration failure")
                    return UnreadableIterator(original(path))
                return original(path)
            with self.subTest(mode=mode), patch.object(os, "scandir", failing_scandir):
                for request in actions:
                    result = self.invoke(request)
                    self.assertFalse(result["ok"], (mode, request["action"], result))
                    self.assertEqual(result.get("phase"), "pre_dispatch", result)
                    self.assertIs(result.get("retryable"), True, result)
            with self.db() as db:
                self.assertEqual(db.execute("SELECT * FROM completion_candidates").fetchall(), before)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='deliveries'").fetchone())
        calls = []
        def transport(_token, method, payload):
            calls.append(method)
            if method == "sendMessage":
                return {"kind":"response", "response":{"ok":True, "result":{
                    "message_id":890, "chat":{"id":-100123456, "type":"supergroup"}, "text":payload["text"]}}}
            return {"kind":"unknown"}
        with patch.object(self.notifier, "completion_telegram", side_effect=transport):
            recovered = self.invoke(send)
        self.assertEqual(recovered.get("status"), "sent", recovered)
        self.assertEqual(calls.count("sendMessage"), 1)

    def test_policy_loaded_projection_has_generation_and_does_not_enable(self):
        loaded = self.invoke({"action": "loaded", "version": "1.8.0"})
        self.assertTrue(loaded["ok"], loaded)
        self.assertEqual(loaded["completion_policy"], self.policy)
        self.assertTrue(loaded["completion_policy"]["enabled"])
        with self.db() as db:
            row = db.execute("SELECT enabled,generation FROM completion_recovery_meta WHERE singleton=1").fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], loaded["completion_policy"]["generation"])


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
