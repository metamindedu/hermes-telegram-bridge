"""Bounded settings DTO, strict setting writes, and OFF retirement behavior."""
from __future__ import annotations

import argparse
import base64
import contextlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

import test_support
import yaml

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "desktop-plugins" / "session-complete-telegram"
PLUGIN_ID = "session-complete-telegram"


class Context:
    plugin_id = PLUGIN_ID

    def __init__(self, root):
        self.root = Path(root)
        self.writes = []

    def get_config(self, key, default=None):
        data = yaml.safe_load((self.root / "config.yaml").read_text(encoding="utf-8")) or {}
        return data.get("plugins", {}).get("entries", {}).get(PLUGIN_ID, {}).get("settings", {}).get(key, default)

    def set_config(self, key, value):
        self.writes.append((key, value))
        path = self.root / "config.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data.setdefault("plugins", {}).setdefault("entries", {}).setdefault(PLUGIN_ID, {}).setdefault("settings", {})[key] = value
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


class SettingsControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = test_support.temporary_directory(prefix="settings-control-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "default-home"
        self.home.mkdir()
        self.installed = self.home / "plugins" / PLUGIN_ID
        self.installed.parent.mkdir()
        shutil.copytree(PACKAGE, self.installed, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.state = self.home / "plugin-data" / PLUGIN_ID
        self.config = self.home / "config.yaml"
        self._write({"plugins": {"enabled": [PLUGIN_ID], "entries": {PLUGIN_ID: {"settings": {}}}}})
        self.ctx = Context(self.home)
        self.home_patch = patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.profile_patch = patch("hermes_cli.profiles.get_active_profile_name", return_value="default")
        self.profile_patch.start()
        self.addCleanup(self.profile_patch.stop)
        self.name = "_settings_control_fixture"
        spec = importlib.util.spec_from_file_location(self.name, self.installed / "__init__.py",
            submodule_search_locations=[str(self.installed)])
        self.package = importlib.util.module_from_spec(spec)
        sys.modules[self.name] = self.package
        spec.loader.exec_module(self.package)
        self.addCleanup(self._remove_modules)
        self.gateway = importlib.import_module(f"{self.name}.telegram_integration")
        self.control = importlib.import_module(f"{self.name}.settings_control")

    def _remove_modules(self):
        for name in tuple(sys.modules):
            if name == self.name or name.startswith(self.name + "."):
                sys.modules.pop(name, None)

    def _write(self, value):
        self.config.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")

    def test_default_selection_and_split_true_do_not_grant_legacy_consent(self):
        status = self.control.settings_status(self.ctx, root=self.home, state=self.state)
        self.assertTrue(status["ok"], status)
        self.assertEqual(status["version"], "1.8.1")
        self.assertEqual(status["settings"], {
            "service_enabled": True,
            "completion_notifications_enabled": True,
            "attention_notifications_enabled": True,
            "clarify_replies_enabled": True,
            "completion_followups_enabled": True,
            "replies_enabled": False,
            "approvals_enabled": False,
        })
        self.assertEqual(status["effective"], {
            "completion_notifications": True,
            "attention_notifications": True,
            "clarify_replies": False,
            "completion_followups": False,
            "approvals": False,
        })
        self.assertIs(status["read_only"], True)
        self.assertEqual(status["selection_reason"], "ready")
        self.assertEqual(set(status), {"ok", "version", "settings", "effective", "read_only",
            "selection_reason", "receiver", "sender"})
        self.assertEqual(set(status["receiver"]), {"version", "heartbeat_ready"})
        self.assertIs(status["receiver"]["heartbeat_ready"], False)
        self.assertEqual(status["sender"], {"version":"1.8.1", "available":True,
            "awaiting_recovery":0, "uncertain_delivery":0, "sent":0, "expired":0})
        self.assertFalse(self.state.exists(), "status must not create bridge state")
        self.assertFalse(self.ctx.writes, "status must not write config")
        self.assertFalse(self.gateway.feature_enabled("clarify_replies_enabled", root=self.home, ctx=self.ctx))
        self.assertFalse(self.gateway.feature_enabled("completion_followups_enabled", root=self.home, ctx=self.ctx))

    def test_actual_registered_cli_uses_public_plugin_context_writer(self):
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest

        manager = PluginManager(scope_key=str(self.home))
        manifest = PluginManifest(name=PLUGIN_ID, key=PLUGIN_ID, source="user", path=str(self.installed))
        native_context = PluginContext(manifest, manager)
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"):
            self.package.register(native_context)
            registration = manager._cli_commands["desktop-telegram-bridge"]
            parser = argparse.ArgumentParser()
            registration["setup_fn"](parser)

            def invoke(request):
                raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
                args = parser.parse_args(["--request", token])
                output = io.StringIO()
                with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}), contextlib.redirect_stdout(output):
                    registration["handler_fn"](args)
                lines = output.getvalue().splitlines()
                self.assertEqual(lines[0], "HERMES_TELEGRAM_BRIDGE_RESULT_V1_BEGIN")
                self.assertEqual(lines[-1], "HERMES_TELEGRAM_BRIDGE_RESULT_V1_END")
                return json.loads(base64.urlsafe_b64decode(lines[1] + "=" * (-len(lines[1]) % 4)))

            status = invoke({"action": "settings_status"})
            self.assertTrue(status["ok"], status)
            self.assertTrue(status["read_only"])
            self.assertFalse(self.state.exists(), "actual PluginContext status must not create state")
            changed = invoke({"action": "settings_set", "key": "attention_notifications_enabled",
                "value": False, "expected": True})
            self.assertTrue(changed["ok"], changed)
            self.assertEqual(changed["readback"], {"key": "attention_notifications_enabled", "value": False})
            self.assertIs(native_context.get_config("attention_notifications_enabled", default=True), False)
        stored = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        settings = stored["plugins"]["entries"][PLUGIN_ID]["settings"]
        self.assertEqual(settings, {"attention_notifications_enabled": False})
        self.assertEqual(stored["plugins"]["enabled"], [PLUGIN_ID])

    def test_effective_module_dependencies_and_saved_child_choices_are_independent(self):
        settings = {
            "service_enabled": True,
            "completion_notifications_enabled": False,
            "attention_notifications_enabled": True,
            "clarify_replies_enabled": False,
            "completion_followups_enabled": True,
            "replies_enabled": True,
            "approvals_enabled": True,
        }
        self._write({"plugins": {"enabled": [PLUGIN_ID], "entries": {PLUGIN_ID: {"settings": settings}}}})
        status = self.control.settings_status(self.ctx, root=self.home, state=self.state)
        self.assertTrue(status["ok"], status)
        self.assertEqual(status["settings"], settings)
        self.assertEqual(status["effective"], {
            "completion_notifications": False,
            "attention_notifications": True,
            "clarify_replies": False,
            "completion_followups": False,
            "approvals": True,
        })
        self.assertFalse(self.gateway.feature_enabled("completion_followups_enabled", root=self.home, ctx=self.ctx))
        self.assertTrue(self.gateway.feature_enabled("approvals_enabled", root=self.home, ctx=self.ctx))

        settings.update({"completion_notifications_enabled": True, "attention_notifications_enabled": False,
                        "clarify_replies_enabled": True, "completion_followups_enabled": True})
        self._write({"plugins": {"enabled": [PLUGIN_ID], "entries": {PLUGIN_ID: {"settings": settings}}}})
        parent_off = self.control.settings_status(self.ctx, root=self.home, state=self.state)
        self.assertTrue(parent_off["ok"], parent_off)
        self.assertIs(parent_off["settings"]["approvals_enabled"], True)
        self.assertIs(parent_off["settings"]["clarify_replies_enabled"], True)
        self.assertEqual(parent_off["effective"], {
            "completion_notifications": True, "attention_notifications": False,
            "clarify_replies": False, "completion_followups": True, "approvals": False,
        })
        self.assertFalse(self.gateway.feature_enabled("approvals_enabled", root=self.home, ctx=self.ctx))
        self.assertTrue(self.gateway.feature_enabled("completion_followups_enabled", root=self.home, ctx=self.ctx))

        settings["service_enabled"] = False
        self._write({"plugins": {"enabled": [PLUGIN_ID], "entries": {PLUGIN_ID: {"settings": settings}}}})
        off = self.control.settings_status(self.ctx, root=self.home, state=self.state)
        self.assertEqual(off["settings"], settings, "master OFF must preserve child choices")
        self.assertFalse(any(off["effective"].values()))
        self.assertEqual(off["selection_reason"], "service_disabled")

    def test_settings_set_is_exact_boolean_single_key_with_expected_guard_and_readback(self):
        result = self.control.settings_set({"action": "settings_set", "key": "service_enabled",
            "value": False, "expected": True}, self.ctx, root=self.home, state=self.state)
        self.assertTrue(result["ok"], result)
        self.assertIs(result["settings"]["service_enabled"], False)
        self.assertEqual(self.ctx.writes, [("service_enabled", False)])
        self.assertTrue(result["read_only"] is False)
        self.assertEqual(result["selection_reason"], "service_disabled")

        before = list(self.ctx.writes)
        for request in (
            {"action": "settings_set", "key": "plugins.enabled", "value": True},
            {"action": "settings_set", "key": "replies_enabled", "value": 1},
            {"action": "settings_set", "key": "approvals_enabled", "value": "true"},
            {"action": "settings_set", "key": "service_enabled", "value": True, "expected": "false"},
            {"action": "settings_set", "key": "service_enabled", "value": True, "path": "model.provider"},
        ):
            rejected = self.control.settings_set(request, self.ctx, root=self.home, state=self.state)
            self.assertFalse(rejected["ok"], (request, rejected))
        stale = self.control.settings_set({"action": "settings_set", "key": "service_enabled",
            "value": True, "expected": True}, self.ctx, root=self.home, state=self.state)
        self.assertFalse(stale["ok"], stale)
        self.assertEqual(self.ctx.writes, before)

    def test_settings_set_reports_writer_failure_and_partial_cleanup_without_false_success(self):
        class FailingWriter(Context):
            def set_config(self, key, value):
                self.writes.append((key, value))
                raise OSError("synthetic writer failure")
        failed_writer = self.control.settings_set({"action": "settings_set", "key": "service_enabled",
            "value": False, "expected": True}, FailingWriter(self.home), root=self.home, state=self.state)
        self.assertFalse(failed_writer["ok"], failed_writer)
        self.assertEqual(failed_writer["reason"], "native_settings_write_failed")
        self.assertEqual(failed_writer["readback"], {"key": "service_enabled", "value": True})
        self.assertEqual(failed_writer["write_status"], "failed")

        reply = importlib.import_module(f"{self.name}.reply_bridge")
        with patch.object(reply, "retire_unexecuted", side_effect=OSError("synthetic cleanup failure")):
            partial = self.control.settings_set({"action": "settings_set", "key": "service_enabled",
                "value": False, "expected": True}, self.ctx, root=self.home, state=self.state)
        self.assertFalse(partial["ok"], partial)
        self.assertEqual(partial["reason"], "settings_cleanup_incomplete")
        self.assertFalse(partial["cleanup"]["ok"])
        self.assertEqual(partial["readback"], {"key": "service_enabled", "value": False})
        self.assertFalse(any(partial["effective"].values()))

    def test_bad_config_legacy_named_or_foreign_contexts_fail_closed_without_write(self):
        self._write({"plugins": {"enabled": [PLUGIN_ID], "entries": {PLUGIN_ID: {"settings": {"unknown": True}}}}})
        bad = self.control.settings_status(self.ctx, root=self.home, state=self.state)
        self.assertFalse(bad["ok"], bad)
        self.assertEqual(bad["selection_reason"], "native_plugin_config_invalid")
        self.assertFalse(any(bad["effective"].values()))

        self._write({"plugins": {"enabled": [PLUGIN_ID, "desktop-clarify-telegram"],
            "entries": {PLUGIN_ID: {"settings": {}}}}})
        legacy = self.control.settings_status(self.ctx, root=self.home, state=self.state)
        self.assertEqual(legacy["selection_reason"], "legacy_package_migration_required")
        self.assertFalse(any(legacy["effective"].values()))

        class ForeignContext(Context):
            plugin_id = "other-plugin"
        foreign = self.control.settings_set({"action": "settings_set", "key": "service_enabled", "value": False},
            ForeignContext(self.home), root=self.home, state=self.state)
        self.assertFalse(foreign["ok"], foreign)

        self._write({"plugins": {"enabled": [PLUGIN_ID], "entries": {PLUGIN_ID: {"settings": {}}}}})
        before = list(self.ctx.writes)
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="custom"):
            custom_home = self.control.settings_set({"action": "settings_set", "key": "service_enabled", "value": False},
                self.ctx, root=self.home, state=self.state)
        self.assertFalse(custom_home["ok"], custom_home)
        self.assertEqual(custom_home["reason"], "default_home_required")
        self.assertEqual(self.ctx.writes, before)

    def test_service_off_retires_unexecuted_rows_preserves_unknown_and_never_replays_on_on(self):
        settings = {
            "service_enabled": True, "completion_notifications_enabled": True,
            "attention_notifications_enabled": True, "clarify_replies_enabled": True,
            "completion_followups_enabled": True, "replies_enabled": True, "approvals_enabled": True,
        }
        self._write({"plugins": {"enabled": [PLUGIN_ID], "entries": {PLUGIN_ID: {"settings": settings}}}})
        reply = importlib.import_module(f"{self.name}.reply_bridge")
        followup = importlib.import_module(f"{self.name}.followup_bridge")
        approval = importlib.import_module(f"{self.name}.approval_bridge")
        now = 10_000.0

        c = reply.connect(self.state)
        descriptor = reply.canonical({"owner": "reply-owner"})
        for fid, state, mid, claim in (("reply-open", "open", 700, None),
                                       ("reply-applying", "applying", 701, "attempted-claim")):
            c.execute("""INSERT INTO forms(id,identity,owner,descriptor,params,questions,bot,boot,chat,topic,
                created,expires,seen,status,mids,texts,answers,selected,answer,claim,dirty)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fid, "identity-" + fid, "reply-owner", descriptor, "{}", "[]", 77, "boot", 99, None,
                 now, now + 1000, now, state, '{"0":' + str(mid) + '}', '{"0":"fixture card"}',
                 "{}", "{}", None, claim, 0))
        c.execute("INSERT INTO tombstones VALUES(77,99,799,'pre-existing',0)")
        c.close()

        c = followup.connect(self.state)
        c.execute("INSERT INTO bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("binding-1", "followup-owner", "{}", 77, 99, 99, None, 800, now, now + 1000, now, "active"))
        c.execute("INSERT INTO receipts VALUES(77,99,801,NULL,'binding-1','pending',NULL,'queued fixture text',?,?)",
                  (now, now))
        c.execute("INSERT INTO receipts VALUES(77,99,802,NULL,'binding-1','applying','attempted-followup',NULL,?,?)",
                  (now, now))
        c.close()

        c = approval.connect(self.state)
        approval_rows = (("approval-open", "open", 900, None, None),
                         ("approval-queued", "queued", 901, "once", None),
                         ("approval-applying", "applying", 902, "once", "attempted-approval"))
        for aid, state, mid, choice, claim in approval_rows:
            c.execute("""INSERT INTO approvals(id,identity,owner,descriptor,request_json,pending_json,
                request_fingerprint,pending_fingerprint,review_text,token,bot,chat,topic,boot,message_id,
                created,expires,seen,status,choice,claim,deadline,dirty)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (aid, "identity-" + aid, "approval-owner", "{}", "{}", "{}", "request", "pending",
                 "fixture review", "token-" + aid, 77, 99, None, "boot", mid, now, now + 1000,
                 now, state, choice, claim, now + 45 if claim else None, 0))
        c.close()

        with patch.object(reply, "retire_unexecuted",
                          side_effect=OSError("synthetic clarify cleanup failure")) as failed_clarify, \
             patch.object(followup, "retire_unexecuted", wraps=followup.retire_unexecuted) as cleaned_followups, \
             patch.object(approval, "retire_unexecuted", wraps=approval.retire_unexecuted) as cleaned_approvals:
            off = self.control.settings_set({"action": "settings_set", "key": "service_enabled",
                "value": False, "expected": True}, self.ctx, root=self.home, state=self.state)
        self.assertFalse(off["ok"], off)
        self.assertEqual(off["reason"], "settings_cleanup_incomplete")
        self.assertEqual((failed_clarify.call_count, cleaned_followups.call_count, cleaned_approvals.call_count), (1, 1, 1))
        self.assertFalse(off["cleanup"]["outcomes"]["clarify"]["ok"])
        self.assertTrue(off["cleanup"]["outcomes"]["followups"]["ok"])
        self.assertTrue(off["cleanup"]["outcomes"]["approvals"]["ok"])

        c = reply.connect(self.state)
        self.assertEqual(c.execute("SELECT status FROM forms WHERE id='reply-open'").fetchone()[0], "open")
        self.assertEqual(c.execute("SELECT status FROM forms WHERE id='reply-applying'").fetchone()[0], "applying")
        self.assertEqual(c.execute("SELECT mid FROM tombstones WHERE form_id='pre-existing'").fetchone()[0], 799)
        c.close()

        with patch.object(reply, "retire_unexecuted",
                          side_effect=OSError("synthetic clarify cleanup failure")) as failed_clarify:
            blocked_on = self.control.settings_set({"action": "settings_set", "key": "service_enabled",
                "value": True, "expected": False}, self.ctx, root=self.home, state=self.state)
        self.assertEqual(failed_clarify.call_count, 1)
        self.assertFalse(blocked_on["ok"], blocked_on)
        self.assertEqual(blocked_on["readback"], {"key": "service_enabled", "value": False})
        self.assertEqual(self.ctx.writes, [("service_enabled", False)], "failed repair must not write ON")

        on = self.control.settings_set({"action": "settings_set", "key": "service_enabled",
            "value": True, "expected": False}, self.ctx, root=self.home, state=self.state)
        self.assertTrue(on["ok"], on)
        self.assertEqual(self.ctx.writes, [("service_enabled", False), ("service_enabled", True)])
        c = reply.connect(self.state)
        self.assertEqual(c.execute("SELECT status FROM forms WHERE id='reply-open'").fetchone()[0], "expired")
        self.assertEqual(c.execute("SELECT status FROM forms WHERE id='reply-applying'").fetchone()[0], "unknown")
        c.close()
        self.assertIsNotNone(reply.lookup_reply(77, 99, 700, state=self.state), "known cards remain reject-only")

        c = followup.connect(self.state)
        self.assertEqual(c.execute("SELECT status FROM bindings WHERE id='binding-1'").fetchone()[0], "expired")
        self.assertEqual(c.execute("SELECT status FROM receipts WHERE message_id=801").fetchone()[0], "rejected")
        self.assertEqual(c.execute("SELECT status FROM receipts WHERE message_id=802").fetchone()[0], "unknown")
        c.close()

        c = approval.connect(self.state)
        self.assertEqual({row[0]: row[1] for row in c.execute("SELECT id,status FROM approvals")}, {
            "approval-open": "expired", "approval-queued": "rejected", "approval-applying": "unknown"})
        self.assertEqual(c.execute("SELECT count(*) FROM tombstones WHERE bot=77 AND chat=99").fetchone()[0], 3)
        c.close()

        c = followup.connect(self.state)
        self.assertEqual(c.execute("SELECT status FROM receipts WHERE message_id=801").fetchone()[0], "rejected")
        c.close()
        c = approval.connect(self.state)
        self.assertEqual(c.execute("SELECT status FROM approvals WHERE id='approval-applying'").fetchone()[0], "unknown")
        c.close()

    def test_retirement_attempts_each_selected_store_when_any_fails(self):
        reply = importlib.import_module(f"{self.name}.reply_bridge")
        followup = importlib.import_module(f"{self.name}.followup_bridge")
        approval = importlib.import_module(f"{self.name}.approval_bridge")
        stores = (("clarify", reply), ("followups", followup), ("approvals", approval))

        for failed, _ in stores:
            with self.subTest(failed=failed):
                self._write({"plugins": {"enabled": [PLUGIN_ID], "entries": {PLUGIN_ID: {"settings": {}}}}})
                with patch.object(reply, "retire_unexecuted",
                                  side_effect=OSError("synthetic cleanup failure") if failed == "clarify" else None,
                                  return_value=0 if failed != "clarify" else None) as clarify, \
                     patch.object(followup, "retire_unexecuted",
                                  side_effect=OSError("synthetic cleanup failure") if failed == "followups" else None,
                                  return_value=0 if failed != "followups" else None) as followups, \
                     patch.object(approval, "retire_unexecuted",
                                  side_effect=OSError("synthetic cleanup failure") if failed == "approvals" else None,
                                  return_value=0 if failed != "approvals" else None) as approvals:
                    result = self.control._retire_unexecuted("service_enabled", self.state)
                self.assertEqual((clarify.call_count, followups.call_count, approvals.call_count), (1, 1, 1))
                self.assertFalse(result["ok"], result)
                self.assertFalse(result["outcomes"][failed]["ok"], result)
                for store, _ in stores:
                    if store != failed:
                        self.assertTrue(result["outcomes"][store]["ok"], result)

    def test_turning_setting_on_waits_for_cleanup_repair(self):
        reply = importlib.import_module(f"{self.name}.reply_bridge")
        followup = importlib.import_module(f"{self.name}.followup_bridge")
        approval = importlib.import_module(f"{self.name}.approval_bridge")
        stores = (("clarify", reply), ("followups", followup), ("approvals", approval))

        for failed, _ in stores:
            with self.subTest(failed=failed):
                self._write({"plugins": {"enabled": [PLUGIN_ID], "entries": {PLUGIN_ID: {"settings": {"service_enabled": False}}}}})
                self.ctx.writes.clear()
                patches = (
                    patch.object(reply, "retire_unexecuted",
                                 side_effect=OSError("synthetic cleanup failure") if failed == "clarify" else None,
                                 return_value=0 if failed != "clarify" else None),
                    patch.object(followup, "retire_unexecuted",
                                 side_effect=OSError("synthetic cleanup failure") if failed == "followups" else None,
                                 return_value=0 if failed != "followups" else None),
                    patch.object(approval, "retire_unexecuted",
                                 side_effect=OSError("synthetic cleanup failure") if failed == "approvals" else None,
                                 return_value=0 if failed != "approvals" else None),
                )
                with patches[0] as clarify, patches[1] as followups, patches[2] as approvals:
                    denied = self.control.settings_set({"action": "settings_set", "key": "service_enabled",
                        "value": True, "expected": False}, self.ctx, root=self.home, state=self.state)
                self.assertEqual((clarify.call_count, followups.call_count, approvals.call_count), (1, 1, 1))
                self.assertFalse(denied["ok"], denied)
                self.assertEqual(denied["reason"], "settings_cleanup_incomplete")
                self.assertEqual(denied["readback"], {"key": "service_enabled", "value": False})
                self.assertEqual(self.ctx.writes, [], "ON must not be written while any retirement fails")

                repaired = self.control.settings_set({"action": "settings_set", "key": "service_enabled",
                    "value": True, "expected": False}, self.ctx, root=self.home, state=self.state)
                self.assertTrue(repaired["ok"], repaired)
                self.assertEqual(self.ctx.writes, [("service_enabled", True)])

    def test_receiver_summary_is_read_only_current_generation_and_pii_free(self):
        settings = {key: True for key in self.control.gateway.SETTING_DEFAULTS}
        self._write({"plugins": {"enabled": [PLUGIN_ID], "entries": {PLUGIN_ID: {"settings": settings}}}})
        reply = importlib.import_module(f"{self.name}.reply_bridge")
        followup = importlib.import_module(f"{self.name}.followup_bridge")
        approval = importlib.import_module(f"{self.name}.approval_bridge")
        now = __import__("time").time()
        reply.bind_receiver(77, 99, None, "boot-reply", state=self.state, now=now)
        self.assertTrue(followup.bind_receiver(77, 99, None, "boot-followup", private_verified=True,
            state=self.state, now=now))
        self.assertTrue(approval.bind_receiver(77, 99, None, "boot-approval", private_verified=True,
            version=approval.VERSION, capability=approval.CAPABILITY, state=self.state, now=now))
        files = sorted(path for path in self.state.rglob("*.sqlite3"))
        before = {path: path.read_bytes() for path in files}
        status = self.control.settings_status(self.ctx, root=self.home, state=self.state)
        self.assertTrue(status["ok"], status)
        self.assertEqual(status["receiver"], {"version": "1.8.1", "heartbeat_ready": True})
        serialized = str(status)
        for private in ("77", "99", "boot-reply", "boot-followup", "boot-approval"):
            self.assertNotIn(private, serialized)
        self.assertEqual(before, {path: path.read_bytes() for path in files})

    def test_status_does_not_expose_tokens_destinations_prompts_or_identifiers(self):
        status = self.control.settings_status(self.ctx, root=self.home, state=self.state)
        serialized = str(status).lower()
        for forbidden in ("bot", "chat", "user", "command", "text", "token", "credential"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main(verbosity=2)
