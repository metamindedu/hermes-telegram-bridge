"""Strict, independent native settings gates for the unified package."""
from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_support
import yaml

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "desktop-plugins" / "session-complete-telegram"
PLUGIN_ID = "session-complete-telegram"
LEGACY_ID = "desktop-clarify-telegram"


class NativeFeatureGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = test_support.temporary_directory(prefix="feature-gates-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "default-home"
        self.root.mkdir()
        self.home_patch = patch.dict(os.environ, {"HERMES_HOME": str(self.root)})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.module_name = "_feature_gate_fixture"
        spec = importlib.util.spec_from_file_location(
            self.module_name, PACKAGE / "__init__.py", submodule_search_locations=[str(PACKAGE)])
        self.package = importlib.util.module_from_spec(spec)
        sys.modules[self.module_name] = self.package
        spec.loader.exec_module(self.package)
        self.addCleanup(self._remove_modules)
        self.gateway = importlib.import_module(f"{self.module_name}.telegram_integration")

    def _remove_modules(self):
        for name in tuple(sys.modules):
            if name == self.module_name or name.startswith(self.module_name + "."):
                sys.modules.pop(name, None)

    def write_config(self, enabled=None, settings=None, *, entries=None, raw=None):
        if raw is not None:
            (self.root / "config.yaml").write_text(raw, encoding="utf-8")
            return
        plugins = {"enabled": list(enabled or [])}
        if entries is None:
            entries = {PLUGIN_ID: {"settings": dict(settings or {})}}
        plugins["entries"] = entries
        (self.root / "config.yaml").write_text(
            yaml.safe_dump({"plugins": plugins}, sort_keys=False), encoding="utf-8")

    def test_both_gates_default_off_and_are_independent(self):
        self.write_config(enabled=[PLUGIN_ID], settings={})
        self.assertFalse(self.gateway.feature_enabled("replies_enabled", root=self.root))
        self.assertFalse(self.gateway.feature_enabled("approvals_enabled", root=self.root))

        self.write_config(enabled=[PLUGIN_ID], settings={"replies_enabled": True})
        self.assertTrue(self.gateway.feature_enabled("replies_enabled", root=self.root))
        self.assertFalse(self.gateway.feature_enabled("approvals_enabled", root=self.root))

        self.write_config(enabled=[PLUGIN_ID], settings={"approvals_enabled": True})
        self.assertFalse(self.gateway.feature_enabled("replies_enabled", root=self.root))
        self.assertTrue(self.gateway.feature_enabled("approvals_enabled", root=self.root))
        self.assertFalse((self.root / "plugin-data").exists(), "gate reads must not create bridge state")

    def test_malformed_root_enabled_entries_settings_and_flags_fail_closed(self):
        for raw in (
            "- not-a-mapping\n",
            "plugins: []\n",
            "plugins:\n  enabled: session-complete-telegram\n",
            "plugins:\n  enabled: []\n  entries: []\n",
            "plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings: []\n",
            "plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings:\n        replies_enabled: 'true'\n        approvals_enabled: true\n",
            "plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings:\n        unknown_flag: true\n",
        ):
            with self.subTest(raw=raw):
                self.write_config(raw=raw)
                self.assertFalse(self.gateway.feature_enabled("replies_enabled", root=self.root))
                self.assertFalse(self.gateway.feature_enabled("approvals_enabled", root=self.root))

    def test_legacy_enabled_blocks_both_features_until_explicit_migration(self):
        self.write_config(enabled=[PLUGIN_ID, LEGACY_ID], settings={
            "replies_enabled": True, "approvals_enabled": True,
        })
        self.assertTrue(self.gateway.migration_required(root=self.root))
        self.assertFalse(self.gateway.feature_enabled("replies_enabled", root=self.root))
        self.assertFalse(self.gateway.feature_enabled("approvals_enabled", root=self.root))

    def test_public_plugin_context_reads_native_settings_without_truthy_coercion(self):
        self.write_config(enabled=[PLUGIN_ID], settings={"replies_enabled": True})

        class Context:
            plugin_id = PLUGIN_ID

            def get_config(self, key, default=None):
                return {"replies_enabled": True}.get(key, default)

        self.assertTrue(self.gateway.feature_enabled("replies_enabled", root=self.root, ctx=Context()))

        class WrongTypedContext(Context):
            def get_config(self, key, default=None):
                return "true" if key == "replies_enabled" else default

        self.assertFalse(self.gateway.feature_enabled("replies_enabled", root=self.root, ctx=WrongTypedContext()))

    def test_loaded_code_capability_is_stable_without_granting_default_off_features(self):
        self.write_config(enabled=[PLUGIN_ID], settings={})
        with patch.dict(os.environ, {"HERMES_HOME": str(self.root)}):
            notifier = importlib.import_module(f"{self.module_name}.notifier")
            state = self.root / "plugin-data" / PLUGIN_ID
            loaded = notifier.dispatch_request({"action": "loaded", "version": "1.8.0"}, root=self.root, state=state)
            self.assertTrue(loaded["ok"])
            self.assertEqual(loaded["capabilities"], [
                "desktop-command-approval-v6", "desktop-completion-recovery-v1"])
            self.assertEqual(set(loaded["completion_policy"]), {"ok", "enabled", "generation"})
            self.assertTrue(loaded["completion_policy"]["ok"])
            self.assertIs(loaded["completion_policy"]["enabled"], True)
            self.assertRegex(loaded["completion_policy"]["generation"], r"^[0-9a-f]{32}$")
            denied = notifier.dispatch_request({"action": "approval_owner_heartbeat", "owner": "fixture"}, root=self.root, state=state)
            self.assertFalse(denied["ok"])
            self.assertEqual(denied["reason"], "approvals_disabled")
            self.write_config(enabled=[PLUGIN_ID], settings={"approvals_enabled": True})
            after = notifier.dispatch_request({"action": "loaded", "version": "1.8.0"}, root=self.root, state=state)
            self.assertEqual(after["capabilities"], loaded["capabilities"])
            # Declaration support is stable; actual action authority is separately rechecked.
            self.assertFalse(self.gateway.feature_enabled("replies_enabled", root=self.root))

    def test_master_and_parent_notification_switches_block_actions_without_clearing_consent(self):
        from unittest.mock import patch
        state = self.root / "plugin-data" / PLUGIN_ID
        settings = {
            "service_enabled": False, "completion_notifications_enabled": True,
            "attention_notifications_enabled": True, "clarify_replies_enabled": True,
            "completion_followups_enabled": True, "replies_enabled": True, "approvals_enabled": True,
        }
        self.write_config(enabled=[PLUGIN_ID], settings=settings)
        with patch.dict(os.environ, {"HERMES_HOME": str(self.root)}):
            notifier = importlib.import_module(f"{self.module_name}.notifier")
            completion = notifier.dispatch_request({"action": "send"}, root=self.root, state=state)
            attention = notifier.dispatch_request({"action": "attention", "kind": "ask"}, root=self.root, state=state)
        self.assertEqual(completion["reason"], "service_disabled")
        self.assertEqual(attention["reason"], "service_disabled")
        self.assertIs(settings["approvals_enabled"], True)
        self.assertFalse((self.root / "plugin-data").exists())

        settings["service_enabled"] = True
        settings["completion_notifications_enabled"] = False
        settings["attention_notifications_enabled"] = False
        self.write_config(enabled=[PLUGIN_ID], settings=settings)
        with patch.dict(os.environ, {"HERMES_HOME": str(self.root)}):
            completion = notifier.dispatch_request({"action": "send"}, root=self.root, state=state)
            reply = notifier.dispatch_request({"action": "reply_poll", "owner": "fixture",
                "observations": [], "unverified": []}, root=self.root, state=state)
            approval = notifier.dispatch_request({"action": "approval_owner_heartbeat", "owner": "fixture"},
                root=self.root, state=state)
        self.assertEqual(completion["reason"], "completion_notifications_disabled")
        self.assertEqual(reply["reason"], "attention_notifications_disabled")
        self.assertEqual(approval["reason"], "attention_notifications_disabled")
        self.assertIs(settings["replies_enabled"], True)
        self.assertIs(settings["approvals_enabled"], True)
        self.assertFalse((self.root / "plugin-data").exists())

    def test_helper_gates_fail_closed_while_outbound_notifications_remain_available(self):
        from unittest.mock import patch

        state = self.root / "plugin-data" / PLUGIN_ID
        self.write_config(enabled=[PLUGIN_ID], settings={})
        with patch.dict(os.environ, {"HERMES_HOME": str(self.root)}):
            notifier = importlib.import_module(f"{self.module_name}.notifier")
            reply = notifier.dispatch_request({"action": "reply_poll", "owner": "fixture",
                "observations": [], "unverified": []}, root=self.root, state=state)
            lookup = notifier.dispatch_request({"action": "reply_registration_lookup", "descriptor": {}, "ask": {}},
                root=self.root, state=state)
            self.assertEqual(lookup["reason"], "replies_disabled")
            reply_claim = notifier.dispatch_request({"action": "reply_claim_valid", "id": "fixture-form",
                "claim": "fixture-claim", "descriptor": {}, "authority": "0" * 64, "deadline": 1},
                root=self.root, state=state)
            approval = notifier.dispatch_request({"action": "approval_owner_heartbeat",
                "capability": "desktop-command-approval-v6", "owner": "fixture"}, root=self.root, state=state)
            with patch.object(notifier, "deliver", return_value={"ok": True, "status": "sent"}) as deliver:
                outbound = notifier.dispatch_request({"action": "send"}, root=self.root, state=state)
            self.assertEqual(reply["reason"], "replies_disabled")
            self.assertEqual(reply_claim["valid"], False)
            self.assertEqual(reply_claim["reason"], "replies_disabled")
            self.assertEqual(approval["reason"], "approvals_disabled")
            self.assertEqual(outbound, {"ok": True, "status": "sent"})
            deliver.assert_called_once()

            nl = chr(10)
            malformed = (
                "plugins: []" + nl,
                nl.join(("plugins:", "  enabled: session-complete-telegram", "")),
                nl.join(("plugins:", "  enabled: [session-complete-telegram]", "  entries:",
                         "    session-complete-telegram:", "      settings:", "        approvals_enabled: 'true'", "")),
            )
            for raw in malformed:
                self.write_config(raw=raw)
                denied_reply = notifier.dispatch_request({"action": "reply_register"}, root=self.root, state=state)
                denied_approval = notifier.dispatch_request({"action": "approval_owner_heartbeat",
                    "capability": "desktop-command-approval-v6"}, root=self.root, state=state)
                self.assertEqual(denied_reply["reason"], "native_plugin_config_invalid")
                self.assertEqual(denied_approval["reason"], "native_plugin_config_invalid")
            self.assertFalse((self.root / "plugin-data").exists(), "failed readiness checks must not create inbox state")

if __name__ == "__main__":
    unittest.main(verbosity=2)
