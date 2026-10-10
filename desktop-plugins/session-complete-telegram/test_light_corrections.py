"""Focused regressions for partial wiring and native effective-config gates."""
from __future__ import annotations

import asyncio
import copy
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import time
import unittest
from unittest.mock import patch

import test_support

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "desktop-plugins" / "session-complete-telegram"
PLUGIN_ID = "session-complete-telegram"
LEGACY_ID = "desktop-clarify-telegram"
TOKEN = "77:light-test-token"
CHAT = 99


def _load_package(directory: Path, name: str):
    spec = importlib.util.spec_from_file_location(
        name, directory / "__init__.py", submodule_search_locations=[str(directory)])
    if spec is None or spec.loader is None:
        raise AssertionError("package import spec unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        for item in tuple(sys.modules):
            if item == name or item.startswith(name + "."):
                sys.modules.pop(item, None)
        raise
    return module


class _NoNetworkRequest:
    """PTB request transport returning local synthetic Telegram API responses only."""
    @property
    def read_timeout(self):
        return 1

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        api_method = url.rsplit("/", 1)[-1]
        if api_method == "getMe":
            result = {"id": 77, "is_bot": True, "first_name": "fixture", "username": "fixture_bot"}
        elif api_method == "answerCallbackQuery":
            result = True
        elif api_method == "sendMessage":
            params = getattr(request_data, "parameters", {}) or {}
            result = {"message_id": 990, "date": 1,
                      "chat": {"id": int(params.get("chat_id", CHAT)), "type": "private"},
                      "from": {"id": 77, "is_bot": True, "first_name": "fixture"},
                      "text": params.get("text", "fixture response")}
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode("utf-8")


class LightCorrectionFixture(unittest.TestCase):
    def setUp(self):
        temp = test_support.temporary_directory(prefix="light-correction-")
        self.addCleanup(temp.cleanup)
        self.temp_root = Path(temp.name)
        self.home = self.temp_root / "synthetic default home"
        self.home.mkdir()
        self.local_app_data = self.temp_root / "synthetic LocalAppData"
        self.local_app_data.mkdir()
        self.managed = self.temp_root / "synthetic managed policy"
        self.managed.mkdir()
        self.config = self.home / "config.yaml"
        self.write_config()
        self.env_patch = patch.dict(os.environ, {
            "HERMES_HOME": str(self.home),
            "LOCALAPPDATA": str(self.local_app_data),
            "HERMES_MANAGED_DIR": str(self.managed),
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        from hermes_constants import get_hermes_home
        self.assertEqual(Path(get_hermes_home()).resolve(), self.home.resolve())

        target = self.home / "plugins" / PLUGIN_ID
        target.parent.mkdir(parents=True)
        shutil.copytree(PACKAGE, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.target = target
        self.package_name = f"_light_correction_{id(self):x}"
        self.package = _load_package(target, self.package_name)
        self.addCleanup(self.remove_package_modules)
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest
        self.manager = PluginManager(scope_key=str(self.home))
        manifest = PluginManifest(name=PLUGIN_ID, key=PLUGIN_ID, source="user", path=str(target))
        self.ctx = PluginContext(manifest, self.manager)
        self.package.register(self.ctx)
        factories = self.manager.get_platform_handler_factories("telegram")
        self.assertEqual(len(factories), 1)
        self.factory, owner = factories[0]
        self.assertEqual(owner, PLUGIN_ID)
        self.gateway = importlib.import_module(f"{self.package_name}.telegram_integration")
        self.notifier = importlib.import_module(f"{self.package_name}.notifier")
        self.reply = importlib.import_module(f"{self.package_name}.reply_bridge")
        self.followup = importlib.import_module(f"{self.package_name}.followup_bridge")
        self.approval = importlib.import_module(f"{self.package_name}.approval_bridge")
        self.state = self.home / "plugin-data" / PLUGIN_ID

    def remove_package_modules(self):
        for name in tuple(sys.modules):
            if name == self.package_name or name.startswith(self.package_name + "."):
                sys.modules.pop(name, None)

    def write_config(self, *, replies=True, approvals=True, enabled=True, legacy=False):
        enabled_ids = [PLUGIN_ID] if enabled else []
        if legacy:
            enabled_ids.append(LEGACY_ID)
        lines = [
            "plugins:",
            f"  enabled: [{', '.join(enabled_ids)}]",
            "  disabled: []",
            "  entries:",
            f"    {PLUGIN_ID}:",
            "      settings:",
            f"        replies_enabled: {str(replies).lower()}",
            f"        approvals_enabled: {str(approvals).lower()}",
            "",
        ]
        self.config.write_text("\n".join(lines), encoding="utf-8")

    def write_managed(self, value, *, feature="approvals_enabled", selection=None, extra_settings=None):
        settings = {feature: value}
        settings.update(extra_settings or {})
        policy = {"plugins": {"entries": {PLUGIN_ID: {"settings": settings}}}}
        if selection is not None:
            policy["plugins"].update(selection)
        import yaml
        (self.managed / "config.yaml").write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")

    async def make_application(self):
        from telegram import Update
        from telegram.ext import Application, ExtBot
        from telegram.request import BaseRequest

        class Request(BaseRequest):
            @property
            def read_timeout(self):
                return 1

            async def initialize(self):
                pass

            async def shutdown(self):
                pass

            async def do_request(self, url, method, request_data=None, **kwargs):
                return await _NoNetworkRequest().do_request(url, method, request_data, **kwargs)

        app = Application.builder().bot(ExtBot(TOKEN, request=Request())).build()
        await app.initialize()
        return app, Update

    def add_approval_tombstone(self, mid=700):
        with self.approval.connect(self.state) as db:
            db.execute("INSERT OR IGNORE INTO tombstones VALUES(?,?,?,?)", (77, CHAT, mid, time.time()))

    def add_clarify_tombstone(self, mid=701):
        db = self.reply.connect(self.state)
        try:
            db.execute("INSERT OR IGNORE INTO tombstones VALUES(?,?,?,?,?)", (77, CHAT, mid, None, None))
        finally:
            db.close()

    @staticmethod
    def approval_text_update(Update, app, update_id, inbound_mid, card_mid=700):
        return Update.de_json({"update_id": update_id, "message": {
            "message_id": inbound_mid, "date": 1, "chat": {"id": CHAT, "type": "private"},
            "from": {"id": CHAT, "is_bot": False, "first_name": "owner"}, "text": "stale free text",
            "reply_to_message": {"message_id": card_mid, "date": 1,
                "chat": {"id": CHAT, "type": "private"},
                "from": {"id": 77, "is_bot": True, "first_name": "fixture"},
                "text": "⚠️ Hermes Desktop 명령 승인"}}}, app.bot)

    @staticmethod
    def clarify_text_update(Update, app, update_id, inbound_mid, card_mid=701):
        return Update.de_json({"update_id": update_id, "message": {
            "message_id": inbound_mid, "date": 1, "chat": {"id": CHAT, "type": "private"},
            "from": {"id": CHAT, "is_bot": False, "first_name": "owner"}, "text": "clarify text",
            "reply_to_message": {"message_id": card_mid, "date": 1,
                "chat": {"id": CHAT, "type": "private"},
                "from": {"id": 77, "is_bot": True, "first_name": "fixture"},
                "text": "synthetic clarify card"}}}, app.bot)

    @staticmethod
    def completion_text_update(Update, app, update_id, inbound_mid, card_mid=702):
        return Update.de_json({"update_id": update_id, "message": {
            "message_id": inbound_mid, "date": 1, "chat": {"id": CHAT, "type": "private"},
            "from": {"id": CHAT, "is_bot": False, "first_name": "owner"}, "text": "old completion reply",
            "reply_to_message": {"message_id": card_mid, "date": 1,
                "chat": {"id": CHAT, "type": "private"},
                "from": {"id": 77, "is_bot": True, "first_name": "fixture"},
                "text": "✅ Hermes Desktop 세션 작업 종료"}}}, app.bot)

    @staticmethod
    def approval_callback_update(Update, app, update_id, data=None):
        data = data or ("hda:" + "a" * 32 + ":o")
        return Update.de_json({"update_id": update_id, "callback_query": {
            "id": f"callback-{update_id}", "from": {"id": CHAT, "is_bot": False, "first_name": "owner"},
            "chat_instance": "fixture", "data": data,
            "message": {"message_id": 900 + update_id, "date": 1,
                "chat": {"id": CHAT, "type": "private"},
                "from": {"id": 77, "is_bot": True, "first_name": "fixture"},
                "text": "synthetic approval card"}}}, app.bot)

    async def with_factory_patches(self):
        """Return patch context keeping the test transport strictly local."""
        return None


class PartialWiringCorrectionTests(LightCorrectionFixture):
    def test_approval_text_add_failure_keeps_quarantine_and_retry_is_idempotent(self):
        async def scenario():
            app, Update = await self.make_application()
            baseline_tasks = set(asyncio.all_tasks())
            later = []

            async def downstream(update, context):
                later.append(update.update_id)

            app.add_handler(__import__("telegram.ext", fromlist=["TypeHandler"]).TypeHandler(Update, downstream), group=1)
            self.add_approval_tombstone()
            self.add_clarify_tombstone()
            db = self.followup.connect(self.state)
            try:
                db.execute("INSERT INTO outbound VALUES(77,99,702,NULL,'legacy',?)", (time.time(),))
            finally:
                db.close()
            original_add = type(app).add_handler
            fail = [True]
            failed_consumer = []

            def add_once(target, handler, group=0):
                callback = getattr(handler, "callback", None)
                if target is app and fail[0] and getattr(callback, "__name__", "") == "consume_text_reply":
                    fail[0] = False
                    failed_consumer.append(True)
                    raise RuntimeError("injected one-shot add failure")
                return original_add(target, handler, group=group)

            try:
                with patch.object(self.notifier, "credentials", return_value=(TOKEN, CHAT, None)), \
                        patch.object(self.notifier, "telegram", return_value={"ok": False}):
                    with patch.object(type(app), "add_handler", new=add_once):
                        self.factory(app, object())
                    wired_after_failure = app.bot_data.get("_newmetaw_session_complete_telegram_wired")
                    tasks_after_failure = len(set(asyncio.all_tasks()) - baseline_tasks)
                    receiver_after_failure = self.approval.receiver_info(state=self.state)
                    await app.process_update(self.approval_text_update(Update, app, 1, 801))
                    await app.process_update(self.clarify_text_update(Update, app, 2, 802))
                    await app.process_update(self.completion_text_update(Update, app, 3, 803))

                    self.factory(app, object())
                    handlers_after_retry = {group: len(items) for group, items in app.handlers.items()}
                    tasks_after_retry = len(set(asyncio.all_tasks()) - baseline_tasks)
                    self.factory(app, object())
                    handlers_after_repeat = {group: len(items) for group, items in app.handlers.items()}
                    tasks_after_repeat = len(set(asyncio.all_tasks()) - baseline_tasks)
                    await app.process_update(self.approval_text_update(Update, app, 2, 802))

                self.assertEqual(failed_consumer, [True], "fault injection must hit the real PTB text consumer once")
                self.assertIsNot(wired_after_failure, True, "partial handlers must not be sealed ready")
                self.assertEqual(tasks_after_failure, 0, "no receiver/heartbeat task may exist before all consumers install")
                self.assertFalse(receiver_after_failure, "partial setup must not publish approval receiver readiness")
                self.assertEqual(later, [], "known approval, clarify, and completion cards stay quarantined across setup failure and retry")
                self.assertIs(app.bot_data.get("_newmetaw_session_complete_telegram_wired"), True)
                self.assertEqual(handlers_after_repeat, handlers_after_retry, "same-Application retry must not duplicate handlers")
                self.assertEqual(tasks_after_repeat, tasks_after_retry, "same-Application retry must not duplicate heartbeat tasks")
            finally:
                pending = set(asyncio.all_tasks()) - baseline_tasks
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await app.shutdown()

        asyncio.run(scenario())

    def test_approval_wire_false_does_not_publish_ready_or_drop_known_card_boundary(self):
        async def scenario():
            app, Update = await self.make_application()
            baseline_tasks = set(asyncio.all_tasks())
            later = []

            async def downstream(update, context):
                later.append(update.update_id)

            from telegram.ext import TypeHandler
            app.add_handler(TypeHandler(Update, downstream), group=1)
            self.add_approval_tombstone()
            try:
                with patch.object(self.notifier, "credentials", return_value=(TOKEN, CHAT, None)), \
                        patch.object(self.notifier, "telegram", return_value={"ok": False}), \
                        patch.object(self.approval, "wire", return_value=False):
                    self.factory(app, object())
                    ready = app.bot_data.get("_newmetaw_session_complete_telegram_wired")
                    tasks = len(set(asyncio.all_tasks()) - baseline_tasks)
                    await app.process_update(self.approval_text_update(Update, app, 3, 803))
                self.assertIsNot(ready, True, "approval.wire False is an incomplete install")
                self.assertEqual(tasks, 0)
                self.assertEqual(later, [], "quarantine must consume the known approval card even when approval.wire returns False")
            finally:
                pending = set(asyncio.all_tasks()) - baseline_tasks
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await app.shutdown()

        asyncio.run(scenario())

    def test_late_followup_exception_rolls_back_only_owned_handlers_and_retry(self):
        async def scenario():
            app, Update = await self.make_application()
            baseline_tasks = set(asyncio.all_tasks())
            later = []

            async def downstream(update, context):
                later.append(update.update_id)

            from telegram.ext import TypeHandler
            app.add_handler(TypeHandler(Update, downstream), group=1)
            self.add_approval_tombstone()
            original_followup_wire = self.followup.wire

            def install_then_fail(*args, **kwargs):
                original_followup_wire(*args, **kwargs)
                raise RuntimeError("injected late followup failure")

            try:
                with patch.object(self.notifier, "credentials", return_value=(TOKEN, CHAT, None)), \
                        patch.object(self.notifier, "telegram", return_value={"ok": False}):
                    with patch.object(self.followup, "wire", side_effect=install_then_fail):
                        try:
                            self.factory(app, object())
                        except RuntimeError:
                            pass
                    ready = app.bot_data.get("_newmetaw_session_complete_telegram_wired")
                    handlers_after_failure = {group: len(items) for group, items in app.handlers.items()}
                    tasks_after_failure = len(set(asyncio.all_tasks()) - baseline_tasks)
                    await app.process_update(self.approval_text_update(Update, app, 4, 804))

                    self.factory(app, object())
                    handlers_after_retry = {group: len(items) for group, items in app.handlers.items()}
                    self.factory(app, object())
                    handlers_after_repeat = {group: len(items) for group, items in app.handlers.items()}
                    tasks_after_repeat = len(set(asyncio.all_tasks()) - baseline_tasks)
                    await app.process_update(self.approval_text_update(Update, app, 5, 805))

                self.assertIsNot(ready, True)
                self.assertEqual(tasks_after_failure, 0)
                self.assertEqual(handlers_after_failure.get(-10000, 0), 1, "only the minimal quarantine remains after rollback")
                self.assertEqual(sum(handlers_after_failure.values()), 2, "unrelated downstream handler is preserved")
                self.assertEqual(handlers_after_repeat, handlers_after_retry, "late failure retry must not duplicate owned handlers")
                self.assertEqual(later, [], "known stored card remains consumed after setup failure")
                self.assertGreaterEqual(tasks_after_repeat, 1)
            finally:
                pending = set(asyncio.all_tasks()) - baseline_tasks
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await app.shutdown()

        asyncio.run(scenario())


class NativeEffectiveGateCorrectionTests(LightCorrectionFixture):
    def test_native_managed_overlay_gates_live_callbacks_helpers_and_final_claim(self):
        async def scenario():
            app, Update = await self.make_application()
            baseline_tasks = set(asyncio.all_tasks())
            self.add_approval_tombstone()
            self.add_clarify_tombstone()
            later = []
            choose_calls = []
            intake_calls = []

            async def downstream(update, context):
                later.append(update.update_id)

            from telegram.ext import TypeHandler
            app.add_handler(TypeHandler(Update, downstream), group=1)
            try:
                with patch.object(self.notifier, "credentials", return_value=(TOKEN, CHAT, None)), \
                        patch.object(self.notifier, "telegram", return_value={"ok": False}):
                    self.factory(app, object())
                    with patch.object(self.approval, "choose", side_effect=lambda *a, **k: choose_calls.append(a) or {"ok": False}), \
                            patch.object(self.reply, "intake", side_effect=lambda *a, **k: intake_calls.append(a) or {"ok": True}), \
                            patch.object(self.reply, "flush"):
                        for index, value in enumerate((False, "true", None)):
                            self.write_managed(value, feature="approvals_enabled",
                                               extra_settings={"replies_enabled": value})
                            effective = importlib.import_module("hermes_cli.config").load_config_readonly()
                            effective_snapshot = copy.deepcopy(effective)
                            self.assertEqual(effective["plugins"]["entries"][PLUGIN_ID]["settings"]["approvals_enabled"], value)
                            status, reason = self.gateway.feature_status("approvals_enabled", root=self.home, ctx=self.ctx)
                            self.assertFalse(status, (value, reason))
                            self.assertIs(importlib.import_module("hermes_cli.config").load_config_readonly(), effective)
                            self.assertEqual(effective, effective_snapshot, "native effective settings reads are strictly read-only")
                            await app.process_update(self.approval_callback_update(Update, app, 10 + index))
                            await app.process_update(self.clarify_text_update(Update, app, 20 + index, 820 + index))
                            readiness = self.notifier.dispatch_request({
                                "action": "approval_owner_heartbeat", "capability": self.approval.CAPABILITY,
                                "owner": "fixture-owner"}, root=self.home, state=self.state)
                            reply_ready = self.notifier.dispatch_request({
                                "action": "reply_poll", "owner": "fixture-owner", "observations": [], "unverified": []},
                                root=self.home, state=self.state)
                            claim = self.notifier.dispatch_request({
                                "action": "approval_claim_valid", "capability": self.approval.CAPABILITY,
                                "owner": "fixture-owner", "id": "a" * 32, "claim": "b" * 32,
                                "descriptor": {}, "authority": "c" * 64, "deadline": time.time() + 10},
                                root=self.home, state=self.state)
                            self.assertFalse(readiness.get("feature_ready"), (value, readiness))
                            self.assertFalse(reply_ready.get("feature_ready"), (value, reply_ready))
                            self.assertIs(claim.get("valid"), False, (value, claim))
                        self.assertEqual(choose_calls, [], "live callbacks must use the captured native context, not raw-only config")
                        self.assertEqual(intake_calls, [], "known clarify cards are consumed without intake while native config denies")
                        self.assertEqual(later, [], "revoked known approval and clarify cards never fall through")

                    # A valid, explicit effective opt-in reuses the same handlers;
                    # no factory restart or permanent false latch is allowed.
                    for index, value in enumerate((True, False, True)):
                        self.write_managed(value, feature="approvals_enabled")
                        with patch.object(self.approval, "choose",
                                          side_effect=lambda *a, **k: choose_calls.append(a) or {"ok": False}):
                            await app.process_update(self.approval_callback_update(Update, app, 30 + index))
                    self.assertEqual(len(choose_calls), 2, "off → on → off → on reads the current effective config each time")
                    self.assertIsNot(self.gateway.feature_status("replies_enabled", root=self.home, ctx=self.ctx)[0], False)

            finally:
                pending = set(asyncio.all_tasks()) - baseline_tasks
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await app.shutdown()

        asyncio.run(scenario())

    def test_native_effective_selection_revocation_and_read_errors_fail_closed(self):
        async def scenario():
            app, Update = await self.make_application()
            baseline_tasks = set(asyncio.all_tasks())
            choose_calls = []
            intake_calls = []
            later = []
            self.add_clarify_tombstone()
            from telegram.ext import TypeHandler
            async def downstream(update, context):
                later.append(update.update_id)
            app.add_handler(TypeHandler(Update, downstream), group=1)
            try:
                with patch.object(self.notifier, "credentials", return_value=(TOKEN, CHAT, None)), \
                        patch.object(self.notifier, "telegram", return_value={"ok": False}):
                    self.factory(app, object())
                    self.write_managed(True, feature="approvals_enabled", selection={
                        "enabled": [], "disabled": [PLUGIN_ID],
                    })
                    status, reason = self.gateway.feature_status("approvals_enabled", root=self.home, ctx=self.ctx)
                    self.assertFalse(status, reason)
                    helper = self.notifier.dispatch_request({
                        "action": "approval_owner_heartbeat", "capability": self.approval.CAPABILITY,
                        "owner": "fixture-owner"}, root=self.home, state=self.state)
                    self.assertFalse(helper.get("ok"), helper)
                    with patch.object(self.approval, "choose", side_effect=lambda *a, **k: choose_calls.append(a) or {"ok": False}):
                        await app.process_update(self.approval_callback_update(Update, app, 41))
                    self.assertEqual(choose_calls, [], "effective plugins.enabled/disabled cannot grant authority against native revoke")

                    self.write_managed(True, feature="approvals_enabled")
                    core_config = importlib.import_module("hermes_cli.config")
                    core_plugins = importlib.import_module("hermes_cli.plugins")
                    with patch.object(core_config, "load_config_readonly", side_effect=RuntimeError("synthetic read error")), \
                            patch.object(core_plugins, "load_config_readonly", side_effect=RuntimeError("synthetic API error")):
                        result, reason = self.gateway.feature_status("approvals_enabled", root=self.home, ctx=self.ctx)
                        self.assertFalse(result, reason)
                        approval_ready = self.notifier.dispatch_request({
                            "action": "approval_owner_heartbeat", "capability": self.approval.CAPABILITY,
                            "owner": "fixture-owner"}, root=self.home, state=self.state)
                        reply_ready = self.notifier.dispatch_request({
                            "action": "reply_poll", "owner": "fixture-owner", "observations": [], "unverified": []},
                            root=self.home, state=self.state)
                        self.assertFalse(approval_ready.get("feature_ready"), approval_ready)
                        self.assertFalse(reply_ready.get("feature_ready"), reply_ready)
                        blocked = self.notifier.dispatch_request({
                            "action": "approval_claim_valid", "capability": self.approval.CAPABILITY,
                            "owner": "fixture-owner", "id": "a" * 32, "claim": "b" * 32,
                            "descriptor": {}, "authority": "c" * 64, "deadline": time.time() + 10},
                            root=self.home, state=self.state)
                        self.assertIs(blocked.get("valid"), False, blocked)
                        with patch.object(self.approval, "choose", side_effect=lambda *a, **k: choose_calls.append(a) or {"ok": False}), \
                                patch.object(self.reply, "intake", side_effect=lambda *a, **k: intake_calls.append(a) or {"ok": True}), \
                                patch.object(self.reply, "flush"):
                            await app.process_update(self.approval_callback_update(Update, app, 42))
                            await app.process_update(self.clarify_text_update(Update, app, 43, 843))
                    self.assertEqual(choose_calls, [], "native read/API errors cannot enter the approval action")
                    self.assertEqual(intake_calls, [], "native read/API errors cannot enter clarify intake")
                    self.assertEqual(later, [], "known cards stay consumed during native read/API errors")
            finally:
                pending = set(asyncio.all_tasks()) - baseline_tasks
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await app.shutdown()

        asyncio.run(scenario())

    def test_context_api_false_wrong_type_none_and_error_deny_factory_actions(self):
        async def scenario():
            app, Update = await self.make_application()
            baseline_tasks = set(asyncio.all_tasks())
            choose_calls = []
            self.add_clarify_tombstone()
            intake_calls = []
            try:
                with patch.object(self.notifier, "credentials", return_value=(TOKEN, CHAT, None)), \
                        patch.object(self.notifier, "telegram", return_value={"ok": False}):
                    self.factory(app, object())
                    for index, failure in enumerate((False, "true", None, RuntimeError("synthetic context read error"))):
                        if isinstance(failure, BaseException):
                            getter = patch.object(self.ctx, "get_config", side_effect=failure)
                        else:
                            getter = patch.object(self.ctx, "get_config", return_value=failure)
                        with getter, patch.object(self.approval, "choose", side_effect=lambda *a, **k: choose_calls.append(a) or {"ok": False}), \
                                patch.object(self.reply, "intake", side_effect=lambda *a, **k: intake_calls.append(a) or {"ok": True}), \
                                patch.object(self.reply, "flush"):
                            await app.process_update(self.approval_callback_update(Update, app, 50 + index))
                            await app.process_update(self.clarify_text_update(Update, app, 60 + index, 860 + index))
                            status, reason = self.gateway.feature_status("approvals_enabled", root=self.home, ctx=self.ctx)
                            self.assertFalse(status, reason)
                    self.assertEqual(choose_calls, [], "captured PluginContext result must gate callback action dynamically")
                    self.assertEqual(intake_calls, [], "captured PluginContext result must gate clarify intake dynamically")
            finally:
                pending = set(asyncio.all_tasks()) - baseline_tasks
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await app.shutdown()

        asyncio.run(scenario())


class CachedPublicV161MigrationTests(unittest.TestCase):
    """Mixed cached-old/fresh-new factory behavior using the actual public v1.6.1 Git bytes."""

    def setUp(self):
        temp = test_support.temporary_directory(prefix="v161-mixed-migration-")
        self.addCleanup(temp.cleanup)
        self.temp_root = Path(temp.name)
        self.home = self.temp_root / "synthetic default home"
        self.home.mkdir()
        self.local_app_data = self.temp_root / "synthetic LocalAppData"
        self.local_app_data.mkdir()
        self.managed = self.temp_root / "synthetic managed policy"
        self.managed.mkdir()
        self.env_patch = patch.dict(os.environ, {
            "HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data),
            "HERMES_MANAGED_DIR": str(self.managed),
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        from hermes_constants import get_hermes_home
        self.assertEqual(Path(get_hermes_home()).resolve(), self.home.resolve())
        from test_native_install import _copy_synthetic_legacy_candidate
        old_source = _copy_synthetic_legacy_candidate(self.temp_root / "exact public v1.6.1")
        self.old_source = old_source
        packages = self.home / "plugins"
        packages.mkdir()
        self.target = packages / PLUGIN_ID
        self.legacy_target = packages / LEGACY_ID
        shutil.copytree(old_source / "desktop-plugins" / PLUGIN_ID, self.target)
        shutil.copytree(old_source / "plugins" / LEGACY_ID, self.legacy_target)
        self.config = self.home / "config.yaml"
        self.write_config(legacy=True, replies=True, approvals=True)
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest
        self.PluginContext = PluginContext
        self.PluginManager = PluginManager
        self.PluginManifest = PluginManifest
        self.old_manager = PluginManager(scope_key=str(self.home))
        self.old_ctx = PluginContext(PluginManifest(name=LEGACY_ID, key=LEGACY_ID, source="user",
                                                    path=str(self.legacy_target)), self.old_manager)
        self.old_package_name = f"_cached_public_v161_{id(self):x}"
        self.old_legacy_package = _load_package(self.legacy_target, self.old_package_name)
        self.addCleanup(self._remove_modules)
        self.old_legacy_package.register(self.old_ctx)
        factories = self.old_manager.get_platform_handler_factories("telegram")
        self.assertEqual(len(factories), 1)
        self.old_factory, old_owner = factories[0]
        self.assertEqual(old_owner, LEGACY_ID)
        self.old_names_before = set(sys.modules)
        self.state = self.home / "plugin-data" / PLUGIN_ID

    def write_config(self, *, legacy, replies, approvals):
        enabled = [PLUGIN_ID, LEGACY_ID] if legacy else [PLUGIN_ID]
        disabled = [] if legacy else [LEGACY_ID]
        lines = [
            "plugins:", f"  enabled: [{', '.join(enabled)}]", f"  disabled: [{', '.join(disabled)}]",
            "  entries:", f"    {PLUGIN_ID}:", "      settings:",
            f"        replies_enabled: {str(replies).lower()}",
            f"        approvals_enabled: {str(approvals).lower()}", "",
        ]
        self.config.write_text("\n".join(lines), encoding="utf-8")

    async def make_application(self):
        return await LightCorrectionFixture.make_application(self)

    def _remove_modules(self):
        for prefix in (self.old_package_name, getattr(self, "new_package_name", "")):
            if not prefix:
                continue
            for name in tuple(sys.modules):
                if name == prefix or name.startswith(prefix + "."):
                    sys.modules.pop(name, None)
        for name in tuple(sys.modules):
            if name not in self.old_names_before and name.startswith("_hermes_telegram_bridge_"):
                sys.modules.pop(name, None)

    def test_cached_v161_factory_helper_no_restart_and_restart_mixed_matrix(self):
        async def scenario():
            app, Update = await self.make_application()
            fresh_app = None
            baseline_tasks = set(asyncio.all_tasks())
            try:
                old_frontend = self.old_legacy_package._sibling_package(self.home)
                old_notifier = importlib.import_module(f"{old_frontend.__package__}.notifier")
                self.assertEqual(old_notifier.VERSION, "1.6.1")
                old_reply = importlib.import_module(f"{old_frontend.__package__}.reply_bridge")
                old_approval = importlib.import_module(f"{old_frontend.__package__}.approval_bridge")
                with patch.object(old_notifier, "credentials", return_value=(TOKEN, CHAT, None)), \
                        patch.object(old_notifier, "telegram", return_value={"ok": False}):
                    # This is the real public v1.6.1 resident factory, before source replacement.
                    self.old_factory(app, object())
                self.assertIsNone(app.bot_data.get("_newmetaw_session_complete_telegram_wired"),
                                  "public v1.6.1 did not publish the unified package readiness marker")
                self.assertEqual(old_approval.VERSION, "1.6.1")
                old_handler_counts = {group: len(items) for group, items in app.handlers.items()}
                self.assertGreater(old_handler_counts.get(0, 0), 0)

                # Establish a stored stale clarify card in the old helper's shared state,
                # then install the new source bytes while keeping the old app/factory resident.
                db = old_reply.connect(self.state)
                try:
                    db.execute("INSERT OR IGNORE INTO tombstones VALUES(?,?,?,?,?)", (77, CHAT, 701, None, None))
                finally:
                    db.close()
                shutil.rmtree(self.target)
                shutil.copytree(PACKAGE, self.target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                self.write_config(legacy=False, replies=False, approvals=False)
                new_name = f"_fresh_primary_after_v161_{id(self):x}"
                self.new_package_name = new_name
                new_package = _load_package(self.target, new_name)
                new_manager = self.PluginManager(scope_key=str(self.home))
                new_ctx = self.PluginContext(self.PluginManifest(name=PLUGIN_ID, key=PLUGIN_ID,
                    source="user", path=str(self.target)), new_manager)
                new_package.register(new_ctx)
                new_factory, owner = new_manager.get_platform_handler_factories("telegram")[0]
                self.assertEqual(owner, PLUGIN_ID)

                # The old factory now resolves the replaced package bytes, but its old
                # Application marker is untracked by the new transactional installer.
                # A safe quarantine is retained and no second intake/heartbeat is attached.
                new_sibling = self.old_legacy_package._sibling_package(self.home)
                new_notifier = importlib.import_module(f"{new_sibling.__package__}.notifier")
                with patch.object(new_notifier, "credentials", return_value=(TOKEN, CHAT, None)), \
                        patch.object(new_notifier, "telegram", return_value={"ok": False}):
                    self.old_factory(app, object())
                    new_factory(app, object())
                    new_factory(app, object())
                state = app.bot_data.get("_newmetaw_session_complete_telegram_wiring_state")
                self.assertIsInstance(state, dict)
                self.assertIs(state.get("untracked_existing"), True)
                after_no_restart = {group: len(items) for group, items in app.handlers.items()}
                self.assertEqual(after_no_restart.get(0, 0), old_handler_counts.get(0, 0),
                                 "no-restart old/new factory mix must not add a second intake set")
                self.assertEqual(after_no_restart.get(-10000), 1,
                                 "the only newly owned no-restart handler is the safe reject-only boundary")

                later = []
                async def downstream(update, context):
                    later.append(update.update_id)
                app.add_handler(__import__("telegram.ext", fromlist=["TypeHandler"]).TypeHandler(Update, downstream), group=1)
                intake_calls = []
                with patch.object(old_reply, "intake", side_effect=lambda *a, **k: intake_calls.append(a) or {"ok": True}):
                    await app.process_update(LightCorrectionFixture.clarify_text_update(Update, app, 91, 891))
                self.assertEqual(intake_calls, [], "known stored card is quarantined before the cached v1.6.1 consumer")
                self.assertEqual(later, [], "no-restart stale-card update must not reach the agent group")

                # The old helper remains the actual cached v1.6.1 implementation; its
                # resident receiver has no lease, so it cannot manufacture readiness.
                old_helper = old_notifier.dispatch_request({"action": "approval_owner_heartbeat",
                    "capability": old_approval.CAPABILITY, "owner": "old-owner"}, root=self.home, state=self.state)
                self.assertFalse(old_helper.get("feature_ready"), old_helper)

                # New-source helper reads strict native settings, and an old v2 helper
                # cannot obtain the new v3 approval capability after explicit migration.
                with patch.object(new_notifier, "credentials", return_value=(TOKEN, CHAT, None)):
                    no_consent = new_notifier.dispatch_request({
                        "action": "approval_owner_heartbeat", "capability": old_approval.CAPABILITY,
                        "owner": "old-owner"}, root=self.home, state=self.state)
                    self.assertFalse(no_consent.get("ok"), no_consent)
                    self.assertEqual(no_consent.get("reason"), "approvals_disabled")
                    self.write_config(legacy=False, replies=True, approvals=True)
                    skew = new_notifier.dispatch_request({
                        "action": "approval_owner_heartbeat", "capability": old_approval.CAPABILITY,
                        "owner": "old-owner"}, root=self.home, state=self.state)
                    self.assertEqual(skew.get("reason"), "capability_mismatch", skew)

                # Restart matrix: a fresh PTB Application starts only the new source
                # handlers, which consume the same old tombstone exactly once.
                fresh_app, FreshUpdate = await self.make_application()
                current_notifier = importlib.import_module(f"{new_package.__package__}.notifier")
                with patch.object(current_notifier, "credentials", return_value=(TOKEN, CHAT, None)), \
                        patch.object(current_notifier, "telegram", return_value={"ok": False}):
                    self.assertIs(new_factory(fresh_app, object()), True)
                    ready_counts = {group: len(items) for group, items in fresh_app.handlers.items()}
                    self.assertEqual(ready_counts.get(0), 5, "fresh package installs exactly one approval/reply/followup set")
                    self.assertIs(fresh_app.bot_data.get("_newmetaw_session_complete_telegram_wired"), True)
                    new_factory(fresh_app, object())
                    self.assertEqual({group: len(items) for group, items in fresh_app.handlers.items()}, ready_counts)
                    fresh_intake = []
                    fresh_later = []
                    async def fresh_downstream(update, context):
                        fresh_later.append(update.update_id)
                    fresh_app.add_handler(__import__("telegram.ext", fromlist=["TypeHandler"]).TypeHandler(
                        FreshUpdate, fresh_downstream), group=1)
                    new_reply = importlib.import_module(f"{new_package.__package__}.reply_bridge")
                    with patch.object(new_reply, "intake", side_effect=lambda *a, **k: fresh_intake.append(a) or {"ok": True}), \
                            patch.object(new_reply, "flush"):
                        await fresh_app.process_update(LightCorrectionFixture.clarify_text_update(
                            FreshUpdate, fresh_app, 92, 892))
                    self.assertEqual(len(fresh_intake), 1, "restart activates one fresh intake, not cached + new duplicates")
                    self.assertEqual(fresh_later, [])
            finally:
                pending = set(asyncio.all_tasks()) - baseline_tasks
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await app.shutdown()
                if fresh_app is not None:
                    await fresh_app.shutdown()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
