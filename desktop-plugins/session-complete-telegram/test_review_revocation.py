"""Focused regressions for followup revocation epochs and late completion sends."""
from __future__ import annotations

import re
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import followup_bridge as b
import test_support


class RevocationEpochTests(unittest.TestCase):
    def setUp(self):
        self.temp = test_support.temporary_directory(prefix="revocation-epoch-")
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.now = time.time()
        self.assertTrue(b.bind_receiver(77, 99, None, "boot", private_verified=True,
                                        state=self.state, now=self.now))
        self.assertTrue(b.heartbeat_owner("owner", state=self.state, now=self.now)["feature_ready"])
        self.descriptor = {
            "owner": "owner", "connectionId": "local", "profile": "default",
            "targetProfile": "default", "sid": "sid", "session_key": "durable",
            "epoch": "epoch", "started_at": "100", "final_row_id": 42,
        }

    def register(self, message_id: int, **kwargs):
        return b.register_completion(
            self.descriptor, bot=77, chat=99, topic=None, message_id=message_id,
            state=self.state, now=self.now, **kwargs,
        )

    def assert_reject_only_card(self, message_id: int):
        mapped = b.lookup_outbound(77, 99, message_id, state=self.state)
        self.assertEqual(mapped, {"kind": "legacy", "binding": None})
        with b.connect(self.state) as db:
            row = db.execute("SELECT binding_id,kind FROM outbound WHERE bot=77 AND chat=99 AND mid=?",
                             (message_id,)).fetchone()
            self.assertEqual(tuple(row), (None, "legacy"))
            self.assertEqual(db.execute("SELECT count(*) FROM bindings WHERE outbound_mid=?",
                                        (message_id,)).fetchone()[0], 0)

    def test_capture_is_stable_and_does_not_create_missing_inbox(self):
        missing = self.state / "not-created"
        first = b.capture_registration_epoch(state=missing)
        second = b.capture_registration_epoch(state=missing)
        self.assertEqual(first, second)
        self.assertIsInstance(first, str)
        self.assertFalse(missing.exists(), "capturing a token must not create inbox state")

    def test_retirement_rotates_epoch_even_without_active_bindings_and_hides_metadata(self):
        before = b.capture_registration_epoch(state=self.state)
        self.assertEqual(b.retire_unexecuted(state=self.state, now=self.now + 1), 0)
        after = b.capture_registration_epoch(state=self.state)
        self.assertNotEqual(before, after)
        self.assertRegex(after, r"^[0-9a-f]{32}$")
        with b.connect(self.state) as db:
            row = db.execute("SELECT bot,chat,mid,binding_id,kind FROM outbound WHERE bot=0 OR chat=0 OR mid=0").fetchone()
            self.assertEqual(tuple(row[:3]), (0, 0, 0))
            self.assertEqual(row[4], "revocation")
            self.assertRegex(row[3], r"^[0-9a-f]{32}$")
        self.assertIsNone(b.lookup_outbound(0, 0, 0, state=self.state),
                          "reserved epoch metadata is not an outbound Telegram card")
        invalid = b.register_completion(self.descriptor, bot=0, chat=99, topic=None,
                                        message_id=10, state=self.state, now=self.now,
                                        expected_epoch=after, policy=lambda: True)
        self.assertEqual(invalid["reason"], "invalid_send_mapping",
                         "non-positive message destinations cannot create authority")

    def test_off_terminalizes_pending_and_applying_and_late_completion_is_reject_only(self):
        original = self.register(100)
        self.assertTrue(original["ok"], original)
        binding_id = original["binding_id"]
        identity = {
            "bot_id": 77, "author_bot_id": 77, "user_id": 99, "chat_id": 99,
            "private": True, "sender_chat": False, "topic": None, "reply_topic": None,
            "forwarded": False, "edited": False, "business": False, "command": False,
            "text": "followup", "boot": "boot",
        }
        self.assertTrue(b.ingest(77, 99, 100, 301, binding_id,
                                 {**identity, "inbound_id": 200}, state=self.state, now=self.now)["ok"])
        self.assertTrue(b.ingest(77, 99, 100, 302, binding_id,
                                 {**identity, "inbound_id": 201}, state=self.state, now=self.now)["ok"])
        queued = next(row for row in b.ready("owner", state=self.state, now=self.now)["items"]
                      if row["message_id"] == 201)
        applying = b.claim("owner", 77, 99, 201, binding_id, queued["descriptor"],
                           state=self.state, now=self.now)
        self.assertTrue(applying["ok"], applying)

        captured_before_send = b.capture_registration_epoch(state=self.state)
        retired = b.retire_unexecuted(state=self.state, now=self.now + 1)
        self.assertEqual(retired, 2)
        with b.connect(self.state) as db:
            self.assertEqual(db.execute("SELECT status,text FROM receipts WHERE message_id=200").fetchone()[:],
                             ("rejected", None))
            self.assertEqual(db.execute("SELECT status,text,claim FROM receipts WHERE message_id=201").fetchone()[:],
                             ("unknown", None, None))
            self.assertEqual(db.execute("SELECT status FROM bindings WHERE id=?", (binding_id,)).fetchone()[0],
                             "expired")
        self.assertEqual(b.ready("owner", state=self.state, now=self.now + 2)["items"], [])

        captured_after_on = b.capture_registration_epoch(state=self.state)
        stale = self.register(101, expected_epoch=captured_before_send, policy=lambda: True)
        self.assertFalse(stale["ok"], stale)
        self.assert_reject_only_card(101)
        self.assertEqual(b.ready("owner", state=self.state, now=self.now + 2)["items"], [])

        # Existing direct callers remain compatible only in the pre-revocation generation.
        legacy = self.register(102)
        self.assertFalse(legacy["ok"], legacy)
        self.assert_reject_only_card(102)

        fresh = self.register(103, expected_epoch=captured_after_on, policy=lambda: True)
        self.assertTrue(fresh["ok"], fresh)
        self.assertEqual(b.lookup_outbound(77, 99, 103, state=self.state)["kind"], "completion")

    def test_malformed_epoch_metadata_fails_closed_and_still_records_reject_only_card(self):
        with b.connect(self.state) as db:
            db.execute("INSERT INTO outbound VALUES(0,0,0,'not-an-epoch','revocation',?)", (self.now,))
        self.assertIsNone(b.capture_registration_epoch(state=self.state))
        result = self.register(110, expected_epoch=None, policy=lambda: True)
        self.assertFalse(result["ok"], result)
        self.assert_reject_only_card(110)

    def test_policy_false_and_true_are_checked_inside_registration_transaction(self):
        epoch = b.capture_registration_epoch(state=self.state)
        original_connect = b.connect
        tracked = []

        class TrackedConnection:
            def __init__(self, connection):
                self.connection = connection
                self.in_transaction = False

            def execute(self, sql, *args, **kwargs):
                result = self.connection.execute(sql, *args, **kwargs)
                self.in_transaction = self.connection.in_transaction
                return result

            def __getattr__(self, name):
                return getattr(self.connection, name)

        def connect(*args, **kwargs):
            connection = TrackedConnection(original_connect(*args, **kwargs))
            tracked.append(connection)
            return connection

        observed = []
        with patch.object(b, "connect", side_effect=connect):
            denied = self.register(120, expected_epoch=epoch,
                                   policy=lambda: (observed.append(tracked[-1].in_transaction) or False))
            allowed = self.register(121, expected_epoch=epoch,
                                    policy=lambda: (observed.append(tracked[-1].in_transaction) or True))
        self.assertFalse(denied["ok"], denied)
        self.assert_reject_only_card(120)
        self.assertTrue(allowed["ok"], allowed)
        self.assertEqual(observed, [True, True], "policy must be read while BEGIN IMMEDIATE is held")


if __name__ == "__main__":
    unittest.main(verbosity=2)
