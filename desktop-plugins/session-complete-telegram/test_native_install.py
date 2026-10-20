"""Native Hermes install/runtime fixtures; all homes are disposable test data."""
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
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_support
from agent.secret_scope import reset_secret_scope, set_secret_scope

REPO = Path(__file__).resolve().parents[2]
FRONTEND_ID = "session-complete-telegram"
INGRESS_ID = "desktop-clarify-telegram"
COMMAND_ID = "desktop-telegram-bridge"
FRAME_BEGIN = "HERMES_TELEGRAM_BRIDGE_RESULT_V1_BEGIN"
FRAME_END = "HERMES_TELEGRAM_BRIDGE_RESULT_V1_END"


def _frame_value(output: str) -> dict:
    lines = output.splitlines()
    if len(lines) != 3 or lines[0] != FRAME_BEGIN or lines[2] != FRAME_END:
        raise AssertionError(f"not one native result frame: {output!r}")
    raw = base64.urlsafe_b64decode(lines[1] + "=" * ((4 - len(lines[1]) % 4) % 4))
    return json.loads(raw.decode("utf-8"))


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
        for module_name in tuple(sys.modules):
            if module_name == name or module_name.startswith(name + "."):
                sys.modules.pop(module_name, None)
        raise
    return module


def _copy_candidate(destination: Path, version: str, *, include_legacy: bool = False) -> Path:
    """Use immutable 1.8.0 bytes for skew tests and current source bytes for 1.8.1."""
    destination.mkdir(parents=True)
    if version == "1.8.0":
        revision = "4874276f519f68fdbacb9767f70c5ed760b5c6fb"
        paths = subprocess.check_output([
            "git", "-C", str(REPO), "ls-tree", "-r", "--name-only", revision, "--",
            "desktop-plugins/session-complete-telegram",
        ]).decode("utf-8").splitlines()
        if not paths:
            raise AssertionError("immutable v1.8.0 fixture tree unavailable; fetch repository history")
        for relative in paths:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(subprocess.check_output(["git", "-C", str(REPO), "show", revision + ":" + relative]))
        return destination
    if version != "1.8.1":
        raise ValueError("only the exact approval-v4 compatibility pin and current source are accepted")
    source = REPO / "desktop-plugins" / FRONTEND_ID
    target = destination / "desktop-plugins" / FRONTEND_ID
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if include_legacy:
        raise ValueError("legacy fixture bytes must use _copy_synthetic_legacy_candidate")
    return destination


def _copy_synthetic_legacy_candidate(destination: Path) -> Path:
    """Extract the exact published v1.6.1 bytes; only homes/data are synthetic."""
    revision = "1a76ed54ccf8a279d4dc4d9bfe1553b90d81d5ca"
    destination.mkdir(parents=True)
    paths = subprocess.check_output([
        "git", "-C", str(REPO), "ls-tree", "-r", "--name-only", revision, "--",
        "desktop-plugins/session-complete-telegram", "plugins/desktop-clarify-telegram",
    ]).decode("utf-8").splitlines()
    if not paths:
        raise AssertionError("immutable v1.6.1 fixture tree unavailable; fetch repository history")
    for relative in paths:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output([
            "git", "-C", str(REPO), "show", revision + ":" + relative,
        ]))
    return destination

class NativeInstallTests(unittest.TestCase):
    def setUp(self):
        # Keep a real space in the fixture path, but avoid overflowing native
        # Windows download/staging names under the profile scratch prefix.
        self.temp = test_support.temporary_directory(prefix=" ")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.fake_os_root = self.root / "o"
        self.local_app_data = self.fake_os_root / "AppData" / "Local"
        self.local_app_data.mkdir(parents=True)
        # Native PM must not enumerate the real Windows profile tree when the
        # fixture home itself is nested under the real AppData path.
        # Native PM keeps generation leases until process exit. Close only leases
        # created in this owned fixture before Windows directory cleanup.
        self.addCleanup(self._release_fixture_pm_leases)
        self.home = self.root / "h"
        self.home.mkdir()
        self.config = self.home / "config.yaml"
        self.config_bytes = b"plugins:\n  enabled: []\nphase2_sentinel: do-not-change\n"
        self.expected_config_bytes = self.config_bytes
        self.config.write_bytes(self.config_bytes)
        self.state = self.home / "plugin-data" / FRONTEND_ID
        self.state.mkdir(parents=True)
        self.sentinel = self.state / "sentinel.bin"
        self.sentinel_bytes = b"preserve-existing-ledger-and-data\x00phase2"
        self.sentinel.write_bytes(self.sentinel_bytes)
        self.ledger_files = {}
        self.candidates = {
            "old": _copy_synthetic_legacy_candidate(self.root / "candidate old two-package"),
            "current": _copy_candidate(self.root / "candidate unified 1.8.1", "1.8.1"),
        }
        self.revisions = {"old": "1" * 40, "current": "8" * 40}

    def _scope_fake_telegram_credentials(self):
        scope = set_secret_scope({"TELEGRAM_BOT_TOKEN": "77:synthetic-token",
                                  "TELEGRAM_HOME_CHANNEL": "99"},
                                 profile_home=str(self.home))
        self.addCleanup(reset_secret_scope, scope)
        return scope

    def _assert_pm_fixture_scope(self):
        from hermes_constants import get_default_hermes_root, get_hermes_home
        from pm.environments import dependency_home_root
        from pm.plugins_state import dependency_homes

        fixture = self.root.resolve()
        active_home = Path(get_hermes_home()).resolve()
        default_home = Path(get_default_hermes_root()).resolve()
        local_app_data = Path(os.environ["LOCALAPPDATA"]).resolve()
        dependency_root = dependency_home_root().resolve()
        homes = [Path(home).resolve() for home in dependency_homes()]
        self.assertTrue(active_home.is_relative_to(fixture), active_home)
        self.assertTrue(default_home.is_relative_to(fixture), default_home)
        self.assertTrue(local_app_data.is_relative_to(fixture), local_app_data)
        self.assertEqual(active_home, self.home.resolve())
        self.assertEqual(local_app_data, self.local_app_data.resolve())
        self.assertTrue(dependency_root.is_relative_to(fixture), dependency_root)
        self.assertTrue(homes, "native PM must include its default dependency home")
        self.assertEqual(homes[0], dependency_root, "PM default dependency home must be task-owned")
        self.assertTrue(all(home.is_relative_to(fixture) for home in homes), homes)

    def _release_fixture_pm_leases(self):
        from pm import runtime

        root = self.root.resolve()
        for generation, release in tuple(runtime._HELD.items()):
            if generation.resolve().is_relative_to(root):
                release()
                runtime._HELD.pop(generation)

    def test_fixture_cleanup_releases_only_owned_real_runtime_leases(self):
        from hermes_cli.runtime_state import lease_directory
        from pm import runtime

        generation = self.root / "owned runtime generation"
        generation.mkdir()
        (generation / ".lease-managed").write_text("", encoding="utf-8")
        release = lease_directory(generation)
        outside = self.root.parent / "unrelated-generation"
        calls = []
        with patch.object(runtime, "_HELD", {generation: release, outside: lambda: calls.append("foreign")}):
            self._release_fixture_pm_leases()
            self.assertEqual(set(runtime._HELD), {outside})
            self.assertEqual(calls, [])
            self.assertEqual(list((generation / ".leases").iterdir()), [])

    def _install_package(self, candidate: str, package_dir: str, *, force: bool):
        from hermes_cli import plugins_cmd

        repo = self.candidates[candidate]
        revision = self.revisions[candidate]
        identifier = f"{repo.as_uri()}#{package_dir}"
        resolved_url, resolved_subdir = plugins_cmd._resolve_git_url(identifier)
        self.assertEqual(resolved_subdir, package_dir)
        copied = []

        def local_transport(clone_dir, git_url, requested_revision, subdir):
            self.assertEqual(git_url, resolved_url)
            self.assertEqual(subdir, resolved_subdir)
            self.assertEqual(requested_revision, revision)
            shutil.copytree(repo, clone_dir)
            copied.append((git_url, subdir, requested_revision))
            return revision

        with patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}), \
                patch.object(plugins_cmd, "_clone_plugin_repo", side_effect=local_transport), \
                contextlib.redirect_stdout(io.StringIO()):
            self._assert_pm_fixture_scope()
            # This is the native install command path. Only Git transport is
            # replaced; scanner, manifest parsing, publication and metadata are real.
            plugins_cmd.cmd_install(identifier, force=force, enable=False, ref=revision,
                                    no_deps=True, yes_deps=False)
        self.assertEqual(len(copied), 1)
        return self.home / "plugins" / Path(package_dir).name

    def _assert_preserved(self):
        self.assertEqual(self.config.read_bytes(), self.expected_config_bytes)
        self.assertEqual(self.sentinel.read_bytes(), self.sentinel_bytes)
        for ledger, contents in self.ledger_files.items():
            self.assertEqual(ledger.read_bytes(), contents, str(ledger))

    def _assert_package_ids(self):
        frontend = self.home / "plugins" / FRONTEND_ID
        self.assertIn("name: session-complete-telegram", (frontend / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertIn("id: ID", (frontend / "desktop" / "plugin.js").read_text(encoding="utf-8"))
        self.assertTrue((self.home / "plugin-data" / FRONTEND_ID).is_dir())
        self.assertTrue((self.home / "plugins" / FRONTEND_ID).is_dir())

    def _native_frontend_handler(self, module_name, candidate="current"):
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest

        self._install_package(candidate, "desktop-plugins/session-complete-telegram", force=False)
        target = self.home / "plugins" / FRONTEND_ID
        manager = PluginManager(scope_key=str(self.home))
        manifest = PluginManifest(name=FRONTEND_ID, key=FRONTEND_ID, source="user", path=str(target))
        ctx = PluginContext(manifest, manager)
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}):
            plugin = _load_package(target, module_name)
            plugin.register(ctx)
        registration = manager._cli_commands[COMMAND_ID]
        parser = argparse.ArgumentParser()
        registration["setup_fn"](parser)
        return plugin, registration["handler_fn"], parser

    def _call_native(self, handler, parser, request):
        raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        args = parser.parse_args(["--request", encoded])
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            handler(args)
        output = stdout.getvalue()
        self.assertLessEqual(len(output), 48_000, "native frame exceeds cli.exec output ceiling")
        return _frame_value(output), output

    def _reply_request(self, ask, owner):
        return {
            "owner": owner, "connectionId": "local", "profile": "default",
            "targetProfile": "default", "sid": "runtime-native",
            "session_key": "runtime-native", "epoch": "epoch-native",
            "request_id": ask["id"],
            "fingerprint": json.dumps(ask, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        }

    def _queue_native_answers(self, bridge, state, form_id, answers):
        row = bridge.read_form(form_id, state)
        mids = json.loads(row["mids"])
        for index, answer in enumerate(answers):
            identity = {
                "bot": 77, "boot": "boot-native", "chat": 99, "user": 99,
                "topic": None, "mid": int(mids[str(index)]), "private": True,
                "human": True, "bot_author": True, "forwarded": False,
                "edited": False, "event": f"fixture-event-{form_id}-{index}",
            }
            result = bridge.intake(form_id, index, "text", answer, identity, state=state)
            self.assertTrue(result["ok"], result)

    def test_native_historical_two_package_to_unified_and_old_rollback_preserves_state(self):
        import sqlite3
        frontend_path = "desktop-plugins/session-complete-telegram"
        legacy_path = "plugins/desktop-clarify-telegram"
        self.ledger_queries = {}
        ledgers = (
            (self.state / "reply-inbox" / "replies.sqlite3",
             "CREATE TABLE tombstones(bot INTEGER, chat INTEGER, mid INTEGER, form_id TEXT, question_index INTEGER);"
             "INSERT INTO tombstones VALUES(77,99,700,'fixture-tombstone',0);", "SELECT mid FROM tombstones", 700),
            (self.state / "followup-inbox" / "followups.sqlite3",
             "CREATE TABLE preservation_marker(value TEXT); INSERT INTO preservation_marker VALUES('followup-ledger');",
             "SELECT value FROM preservation_marker", "followup-ledger"),
            (self.state / "approval-inbox" / "approvals.sqlite3",
             "CREATE TABLE preservation_marker(value TEXT); INSERT INTO preservation_marker VALUES('approval-ledger');",
             "SELECT value FROM preservation_marker", "approval-ledger"),
        )
        for ledger, setup_sql, query, expected in ledgers:
            ledger.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(ledger)
            try:
                connection.executescript(setup_sql)
            finally:
                connection.close()
            self.ledger_files[ledger] = ledger.read_bytes()
            self.ledger_queries[ledger] = (query, expected)

        self._install_package("old", frontend_path, force=False)
        self._install_package("old", legacy_path, force=False)
        self._assert_preserved()
        self._assert_package_ids()
        self.assertIn("version: '1.6.1'", (self.home / "plugins" / INGRESS_ID / "plugin.yaml").read_text(encoding="utf-8"))

        # A single native install updates only the primary package. The legacy
        # package, config selection, and all durable state remain untouched.
        self._install_package("current", frontend_path, force=True)
        self.assertIn("version: '1.8.1'", (self.home / "plugins" / FRONTEND_ID / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertTrue((self.home / "plugins" / INGRESS_ID / "__init__.py").is_file())
        self._assert_preserved()
        self._assert_package_ids()

        # Exact old packaged rollback replaces only the primary managed folder;
        # it does not reverse the user's config choice or erase old ledgers.
        self._install_package("old", frontend_path, force=True)
        self.assertIn("version: '1.6.1'", (self.home / "plugins" / FRONTEND_ID / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertIn("version: '1.6.1'", (self.home / "plugins" / INGRESS_ID / "plugin.yaml").read_text(encoding="utf-8"))
        self._assert_preserved()
        self._assert_package_ids()
        for ledger, (query, expected) in self.ledger_queries.items():
            connection = sqlite3.connect(ledger)
            try:
                self.assertEqual(connection.execute(query).fetchone()[0], expected, str(ledger))
            finally:
                connection.close()

    def test_native_fresh_single_install_and_plugin_doctor(self):
        from hermes_cli import plugins_cmd

        self._install_package("current", "desktop-plugins/session-complete-telegram", force=False)
        self._assert_package_ids()
        self.assertFalse((self.home / "plugins" / INGRESS_ID).exists(), "fresh install must not install the legacy package")
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}), \
                contextlib.redirect_stdout(io.StringIO()):
            self._assert_pm_fixture_scope()
            plugins_cmd.cmd_plugin_doctor(str(self.home / "plugins" / FRONTEND_ID), ci=True)
        self._assert_preserved()

    def test_one_real_native_context_registers_cli_and_one_telegram_factory(self):
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest

        self._install_package("current", "desktop-plugins/session-complete-telegram", force=False)
        target = self.home / "plugins" / FRONTEND_ID
        manager = PluginManager(scope_key=str(self.home))
        manifest = PluginManifest(name=FRONTEND_ID, key=FRONTEND_ID, source="user", path=str(target))
        ctx = PluginContext(manifest, manager)
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}):
            plugin = _load_package(target, "unified_native_context_fixture")
            plugin.register(ctx)
        self.assertIn(COMMAND_ID, manager._cli_commands)
        factories = manager.get_platform_handler_factories("telegram")
        self.assertEqual(len(factories), 1)
        factory, owner = factories[0]
        self.assertEqual(owner, FRONTEND_ID)
        self.assertEqual(factory.__name__, "telegram_factory")

        # The repository-only legacy compatibility stub cannot wire a duplicate.
        legacy_target = REPO / "plugins" / INGRESS_ID
        legacy = _load_package(legacy_target, "deprecated_native_context_fixture")
        old_ctx = PluginContext(PluginManifest(name=INGRESS_ID, key=INGRESS_ID, source="user",
                                               path=str(legacy_target)), manager)
        legacy.register(old_ctx)
        self.assertEqual(manager.get_platform_handler_factories("telegram"), [(factory, FRONTEND_ID)])

    def test_real_ptb_application_uses_independent_live_gates_and_never_duplicates_handlers(self):
        import asyncio
        from unittest.mock import AsyncMock
        from telegram import Update
        from telegram.ext import Application, TypeHandler
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest

        self._install_package("current", "desktop-plugins/session-complete-telegram", force=False)
        nl = chr(10)
        self.config.write_text(nl.join(("plugins:", "  enabled: [session-complete-telegram]", "  entries:",
            "    session-complete-telegram:", "      settings:", "        replies_enabled: false",
            "        approvals_enabled: false", "")), encoding="utf-8")
        self.expected_config_bytes = self.config.read_bytes()
        target = self.home / "plugins" / FRONTEND_ID
        manager = PluginManager(scope_key=str(self.home))
        ctx = PluginContext(PluginManifest(name=FRONTEND_ID, key=FRONTEND_ID, source="user", path=str(target)), manager)
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}):
            plugin = _load_package(target, "ptb_unified_gates_fixture")
            plugin.register(ctx)
            notifier_module = importlib.import_module("ptb_unified_gates_fixture.notifier")
            reply_bridge = importlib.import_module("ptb_unified_gates_fixture.reply_bridge")
            approval_bridge = importlib.import_module("ptb_unified_gates_fixture.approval_bridge")
            followup_bridge = importlib.import_module("ptb_unified_gates_fixture.followup_bridge")
            factory, owner = manager.get_platform_handler_factories("telegram")[0]
        self.assertEqual(owner, FRONTEND_ID)

        def write_flags(*, replies, approvals):
            self._assert_preserved()
            self.config.write_text(nl.join(("plugins:", "  enabled: [session-complete-telegram]", "  entries:",
                "    session-complete-telegram:", "      settings:",
                f"        replies_enabled: {str(replies).lower()}",
                f"        approvals_enabled: {str(approvals).lower()}", "")), encoding="utf-8")
            self.expected_config_bytes = self.config.read_bytes()

        async def run():
            app = Application.builder().token("77:synthetic-token").build()
            from types import SimpleNamespace
            from gateway.config import Platform, PlatformConfig, HomeChannel
            native_config = PlatformConfig(enabled=True, token="77:synthetic-token",
                home_channel=HomeChannel(Platform.TELEGRAM, "99", "Fixture Home"))
            native_adapter = SimpleNamespace(platform=Platform.TELEGRAM, config=native_config,
                gateway_runner=SimpleNamespace(config=SimpleNamespace(platforms={Platform.TELEGRAM:native_config})))
            with (
                    patch.object(type(app.bot), "initialize", new=AsyncMock()),
                    patch.object(type(app.bot), "shutdown", new=AsyncMock()),
                    patch.object(notifier_module, "credentials", return_value=("77:synthetic-token", 99, None)),
                    patch.object(notifier_module, "telegram", return_value={"ok": False}),
            ):
                await app.initialize()
                before_tasks = set(asyncio.all_tasks())
                self.assertIs(factory(app, native_adapter), True)
                handler_counts = {group: len(handlers) for group, handlers in app.handlers.items()}
                self.assertIs(factory(app, native_adapter), True)
                self.assertEqual({group: len(handlers) for group, handlers in app.handlers.items()}, handler_counts)
                later = []
                async def downstream(update, context):
                    later.append(update)
                app.add_handler(TypeHandler(Update, downstream), group=1)

                def callback_update(update_id, callback_id, data):
                    return Update.de_json({"update_id": update_id, "callback_query": {
                        "id": callback_id, "from": {"id": 99, "is_bot": False, "first_name": "owner"},
                        "chat_instance": "fixture", "data": data,
                        "message": {"message_id": 901, "date": 1,
                            "chat": {"id": 99, "type": "private"},
                            "from": {"id": 77, "is_bot": True, "first_name": "bot"},
                            "text": "synthetic card"}}}, app.bot)

                # Both gates are off at factory invocation, but handlers stay in
                # place to consume stale callbacks/cards without agent fallthrough.
                choose_calls = []
                with (
                        patch.object(approval_bridge, "choose", side_effect=lambda *a, **k: choose_calls.append(a) or {"ok": False}),
                        patch.object(type(callback_update(1, "q-off", "hda:" + "a" * 32 + ":o").callback_query),
                                     "answer", new=AsyncMock()) as denied_answer,
                ):
                    denied = callback_update(1, "q-off", "hda:" + "a" * 32 + ":o")
                    await app.process_update(denied)
                self.assertEqual(choose_calls, [])
                self.assertGreater(denied_answer.await_count, 0)
                self.assertEqual(later, [])

                # Enabling replies after the handlers were wired activates only
                # that gate; approvals remain off in this same Application.
                write_flags(replies=True, approvals=False)
                intake_calls = []
                with (
                        patch.object(reply_bridge, "intake", side_effect=lambda *a, **k: intake_calls.append(a) or {"ok": True}),
                        patch.object(reply_bridge, "flush"),
                ):
                    clarify = callback_update(2, "q-reply", "hdq:" + "b" * 32 + ":0:select:1")
                    with patch.object(type(clarify.callback_query), "answer", new=AsyncMock()) as reply_answer:
                        await app.process_update(clarify)
                self.assertEqual(len(intake_calls), 1)
                self.assertEqual(reply_answer.await_args.args[0], "선택을 받았습니다.")
                self.assertEqual(choose_calls, [], "reply enablement must not enable approvals")

                # Conversely, approvals remain independently live when replies
                # are switched off without replacing the resident handler set.
                write_flags(replies=False, approvals=True)
                with (
                        patch.object(approval_bridge, "choose", side_effect=lambda *a, **k: choose_calls.append(a) or {"ok": False}),
                        patch.object(type(callback_update(3, "q-approval", "hda:" + "c" * 32 + ":o").callback_query),
                                     "answer", new=AsyncMock()) as approval_answer,
                ):
                    approval_update = callback_update(3, "q-approval", "hda:" + "c" * 32 + ":o")
                    await app.process_update(approval_update)
                self.assertEqual(len(choose_calls), 1)
                self.assertGreater(approval_answer.await_count, 0)

                # A stale completion-card reply is still stopped when replies
                # are revoked, instead of reaching the general prompt handler.
                write_flags(replies=False, approvals=False)
                with followup_bridge.connect() as tombstone_db:
                    tombstone_db.execute("INSERT INTO outbound VALUES(77,99,700,NULL,'legacy',?)", (time.time(),))
                self.assertEqual(followup_bridge.lookup_outbound(77, 99, 700)["kind"], "legacy")
                text = Update.de_json({"update_id": 4, "message": {"message_id": 902, "date": 1,
                    "chat": {"id": 99, "type": "private"},
                    "from": {"id": 99, "is_bot": False, "first_name": "owner"}, "text": "old reply",
                    "reply_to_message": {"message_id": 700, "date": 1,
                        "chat": {"id": 99, "type": "private"},
                        "from": {"id": 77, "is_bot": True, "first_name": "bot"},
                        "text": "✅ Hermes Desktop 세션 작업 종료"}}}, app.bot)
                await app.process_update(text)
                self.assertEqual(later, [])
                with followup_bridge.connect() as tombstone_db:
                    consumed = tombstone_db.execute(
                        "SELECT kind FROM outbound WHERE bot=77 AND chat=99 AND mid=902").fetchone()
                self.assertIsNotNone(consumed, "revoked stale-card reply must be tombstoned")
                self.assertEqual(consumed["kind"], "legacy")
                self.assertEqual(handler_counts, {group: len(handlers) for group, handlers in app.handlers.items() if group != 1})

                extra_tasks = set(asyncio.all_tasks()) - before_tasks
                for task in extra_tasks:
                    task.cancel()
                if extra_tasks:
                    await asyncio.gather(*extra_tasks, return_exceptions=True)
                await app.shutdown()

        with patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}):
            asyncio.run(run())
        self._assert_preserved()

    def test_real_frontend_cli_registration_parsing_and_single_action_dispatch(self):
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest

        self._install_package("current", "desktop-plugins/session-complete-telegram", force=False)
        target = self.home / "plugins" / FRONTEND_ID
        manager = PluginManager(scope_key=str(self.home))
        manifest = PluginManifest(name=FRONTEND_ID, key=FRONTEND_ID, source="user", path=str(target))
        ctx = PluginContext(manifest, manager)
        encoded = base64.urlsafe_b64encode(b'{"action":"unknown-native-fixture"}').rstrip(b"=").decode("ascii")
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}):
            plugin = _load_package(target, "phase2_cli_fixture")
            plugin.register(ctx)
            registration = manager._cli_commands[COMMAND_ID]
            parser = argparse.ArgumentParser()
            registration["setup_fn"](parser)
            args = parser.parse_args(["--request", encoded])
            notifier = importlib.import_module("phase2_cli_fixture.notifier")
            with patch.object(notifier, "dispatch_request", wraps=notifier.dispatch_request) as dispatch, \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                registration["handler_fn"](args)
        dispatch.assert_called_once_with({"action": "unknown-native-fixture"}, root=self.home.resolve(),
                                         native=True, result_budget=plugin.MAX_RESULT_BYTES, ctx=ctx)
        framed = _frame_value(stdout.getvalue())
        self.assertEqual(framed, {"ok": False, "reason": "unknown_action"})
        self.assertEqual(plugin.PLUGIN_ID, FRONTEND_ID)
        self.assertEqual(plugin._parse_request(encoded), {"action": "unknown-native-fixture"})
        large_ask = {"id": "srq-acde1234abcd", "method": "clarify", "params": {
            "session_id": "runtime-native", "question": "가" * 2500, "choices": ["나" * 500]}}
        large_request = {"action": "unknown-native-fixture", "ask": large_ask,
            "descriptor": {"fingerprint": json.dumps(large_ask, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}}
        large_raw = json.dumps(large_request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        encoded_large = base64.urlsafe_b64encode(large_raw).rstrip(b"=").decode("ascii")
        self.assertLessEqual(len(large_raw), plugin.MAX_REQUEST_BYTES)
        self.assertLessEqual(len(encoded_large), plugin.MAX_REQUEST_TOKEN_CHARS)
        self.assertEqual(plugin._parse_request(encoded_large), large_request)
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}), \
                patch.object(notifier, "dispatch_request", wraps=notifier.dispatch_request) as large_dispatch, \
                contextlib.redirect_stdout(io.StringIO()) as large_stdout:
            registration["handler_fn"](parser.parse_args(["--request", encoded_large]))
        large_dispatch.assert_called_once_with(large_request, root=self.home.resolve(),
                                              native=True, result_budget=plugin.MAX_RESULT_BYTES, ctx=ctx)
        self.assertEqual(_frame_value(large_stdout.getvalue()), {"ok": False, "reason": "unknown_action"})
        with self.assertRaises(ValueError):
            plugin._parse_request("not-base64!")
        duplicate = base64.urlsafe_b64encode(b'{"action":"inspect","action":"send"}').rstrip(b"=").decode("ascii")
        with self.assertRaises(ValueError):
            plugin._parse_request(duplicate)
        oversized = base64.urlsafe_b64encode(b'{"action":"x","data":"' + b"x" * 23_000 + b'"}').rstrip(b"=").decode("ascii")
        with self.assertRaises(ValueError):
            plugin._parse_request(oversized)
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            plugin._write_frame({"ok": True, "data": "x" * 30_000})
        self.assertEqual(_frame_value(stdout.getvalue()), {"ok": False, "reason": "helper_result_too_large"})
        self._assert_preserved()

    def test_registered_native_dispatcher_frames_real_reply_unicode_claims_and_fragments(self):
        from hermes_state import SessionDB

        self._scope_fake_telegram_credentials()

        plugin, handler, parser = self._native_frontend_handler("phase2_real_reply_dispatch")
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}):
            notifier = importlib.import_module("phase2_real_reply_dispatch.notifier")
            bridge = importlib.import_module("phase2_real_reply_dispatch.reply_bridge")
        self.assertIs(notifier.sys, sys, "exercise the real stdlib import; no test injection")
        state = self.home / "plugin-data" / FRONTEND_ID
        bridge.bind_receiver(77, 99, None, "boot-native", state=state)

        def telegram_stub(token, method, payload):
            if method == "sendMessage":
                message_id = 10_000 + sum(1 for call in telegram_calls if call[0] == "sendMessage")
                telegram_calls.append((method, payload))
                return {"ok": True, "result": {"message_id": message_id, "chat": {"id": 99},
                        "text": payload["text"], "from": {"id": 77, "is_bot": True}}}
            telegram_calls.append((method, payload))
            return {"ok": True, "result": {"message_id": payload.get("message_id", 10_000), "chat": {"id": 99}}}

        telegram_calls = []
        self.config.write_text("plugins:\n  enabled: []\n", encoding="utf-8")
        disabled_ask = {"id": "srq-aabbcc112233", "method": "clarify",
                        "params": {"session_id": "runtime-native", "question": "Disabled?"}}
        disabled = self._call_native(handler, parser, {"action": "reply_register",
            "descriptor": self._reply_request(disabled_ask, "owner-native"), "ask": disabled_ask})[0]
        self.assertEqual(disabled, {"ok": False, "feature_ready": False, "reason": "session_complete_plugin_disabled"})

        self.config.write_text("plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings:\n        replies_enabled: true\n", encoding="utf-8")
        (self.home / ".env").write_text(
            "TELEGRAM_BOT_TOKEN=77:synthetic-token\nTELEGRAM_HOME_CHANNEL=99\n", encoding="utf-8")
        db = SessionDB(self.home / "state.db")
        db.create_session("runtime-native", source="desktop")
        db.set_session_title("runtime-native", "Native dispatcher fixture")
        db.close()

        def register_form(request_id, count, owner="owner-native"):
            ask = {"id": request_id, "method": "clarify", "params": {"session_id": "runtime-native",
                "questions": [{"qid": f"q{i+1}", "question": f"Question {i+1}?", "choices": []}
                              for i in range(count)]}}
            result, output = self._call_native(handler, parser, {"action": "reply_register",
                "descriptor": self._reply_request(ask, owner), "ask": ask})
            self.assertTrue(result.get("ok"), result)
            self.assertTrue(result.get("new"), result)
            self.assertLessEqual(len(output), 48_000)
            return ask, result["id"]

        with patch.object(notifier, "telegram", side_effect=telegram_stub):
            ask_small, small_id = register_form("srq-bbccdd223344", 3)
            self._queue_native_answers(bridge, state, small_id,
                ["가" * 2000, "나" * 2000, "다" * 2000])
            poll_small, poll_output = self._call_native(handler, parser, {"action": "reply_poll",
                "owner": "owner-native", "observations": [{"id": small_id}], "unverified": []})
            self.assertLessEqual(len(poll_output), 48_000)
            raw_small = base64.urlsafe_b64decode(poll_output.splitlines()[1] + "=" * ((4 - len(poll_output.splitlines()[1]) % 4) % 4))
            self.assertLessEqual(len(raw_small), plugin.MAX_RESULT_BYTES)
            self.assertEqual(len(poll_small["items"]), 1)
            small_item = poll_small["items"][0]
            self.assertEqual(set(small_item), {"id", "claim", "deadline", "descriptor", "result"})
            self.assertEqual(small_item["descriptor"], self._reply_request(ask_small, "owner-native"))
            self.assertEqual(small_item["result"], {"answers": {"q1": "가" * 2000,
                "q2": "나" * 2000, "q3": "다" * 2000}})
            repeated, _ = self._call_native(handler, parser, {"action": "reply_poll", "owner": "owner-native",
                "observations": [{"id": small_id}], "unverified": []})
            self.assertEqual(repeated["items"], [], "an applying claim must not be returned again")
            settled, _ = self._call_native(handler, parser, {"action": "reply_settle", "id": small_id,
                "claim": small_item["claim"], "status": "applied"})
            self.assertEqual(settled, {"ok": True, "status": "applied"})
            replay, _ = self._call_native(handler, parser, {"action": "reply_settle", "id": small_id,
                "claim": small_item["claim"], "status": "applied"})
            self.assertFalse(replay["ok"])

            ask_large, large_id = register_form("srq-ccddeeff445566", 5)
            ask_last, last_id = register_form("srq-ddeeff001122", 1)
            self._queue_native_answers(bridge, state, large_id, ["😊" * 2000 for _ in range(5)])
            self._queue_native_answers(bridge, state, last_id, ["끝 😊"])
            connection = bridge.connect(state)
            try:
                connection.execute("UPDATE forms SET created=? WHERE id=?", (time.time() - 20, large_id))
                connection.execute("UPDATE forms SET created=? WHERE id=?", (time.time() - 10, last_id))
            finally:
                connection.close()

            fragment_poll, fragment_output = self._call_native(handler, parser, {"action": "reply_poll",
                "owner": "owner-native", "observations": [{"id": large_id}, {"id": last_id}], "unverified": []})
            self.assertLessEqual(len(fragment_output), 48_000)
            self.assertEqual(len(fragment_poll["items"]), 1)
            fragment_item = fragment_poll["items"][0]
            self.assertEqual(fragment_item["id"], large_id)
            self.assertEqual(set(fragment_item), {"id", "claim", "deadline", "fragment"})
            self.assertEqual(set(fragment_item["fragment"]), {"sha256", "bytes", "chunks", "chunk_bytes"})
            self.assertGreater(fragment_item["fragment"]["bytes"], plugin.MAX_RESULT_BYTES)
            self.assertLessEqual(fragment_item["fragment"]["chunks"], 16)
            self.assertEqual(bridge.read_form(last_id, state)["status"], "queued",
                             "an aggregate item not selected for this frame must remain unclaimed")

            next_poll, _ = self._call_native(handler, parser, {"action": "reply_poll", "owner": "owner-native",
                "observations": [{"id": large_id}, {"id": last_id}], "unverified": []})
            self.assertEqual(len(next_poll["items"]), 1)
            last_item = next_poll["items"][0]
            self.assertEqual(last_item["id"], last_id)
            self.assertIn("result", last_item)
            self.assertEqual(last_item["result"], {"answers": {"q1": "끝 😊"}})
            self.assertEqual(bridge.read_form(last_id, state)["status"], "applying")

            before = bridge.read_form(large_id, state)
            chunks = []
            fragment = fragment_item["fragment"]
            for index in range(fragment["chunks"]):
                part, part_output = self._call_native(handler, parser, {"action": "reply_fragment",
                    "owner": "owner-native", "id": large_id, "claim": fragment_item["claim"],
                    "sha256": fragment["sha256"], "index": index})
                self.assertLessEqual(len(part_output), 48_000)
                self.assertTrue(part["ok"], part)
                self.assertEqual(part["index"], index)
                chunks.append(base64.urlsafe_b64decode(part["data"] + "=" * ((4 - len(part["data"]) % 4) % 4)))
            joined = b"".join(chunks)
            self.assertEqual(len(joined), fragment["bytes"])
            import hashlib
            self.assertEqual(hashlib.sha256(joined).hexdigest(), fragment["sha256"])
            payload = json.loads(joined.decode("utf-8"))
            self.assertEqual(set(payload), {"descriptor", "result"})
            self.assertEqual(payload["descriptor"], self._reply_request(ask_large, "owner-native"))
            self.assertEqual(payload["result"], {"answers": {f"q{i+1}": "😊" * 2000 for i in range(5)}})
            again, _ = self._call_native(handler, parser, {"action": "reply_fragment", "owner": "owner-native",
                "id": large_id, "claim": fragment_item["claim"], "sha256": fragment["sha256"], "index": 0})
            self.assertEqual(again["data"], base64.urlsafe_b64encode(chunks[0]).rstrip(b"=").decode("ascii"))
            after = bridge.read_form(large_id, state)
            self.assertEqual((after["status"], after["claim"], after["seen"], after["expires"]),
                             (before["status"], before["claim"], before["seen"], before["expires"]))

            self.assertTrue(self._call_native(handler, parser, {"action": "reply_settle", "id": large_id,
                "claim": fragment_item["claim"], "status": "applied"})[0]["ok"])
            self.assertTrue(self._call_native(handler, parser, {"action": "reply_settle", "id": last_id,
                "claim": last_item["claim"], "status": "applied"})[0]["ok"])
            no_replay, _ = self._call_native(handler, parser, {"action": "reply_poll", "owner": "owner-native",
                "observations": [{"id": small_id}, {"id": large_id}, {"id": last_id}], "unverified": []})
            self.assertEqual(no_replay["items"], [])
            after_settle, _ = self._call_native(handler, parser, {"action": "reply_fragment", "owner": "owner-native",
                "id": large_id, "claim": fragment_item["claim"], "sha256": fragment["sha256"], "index": 0})
            self.assertEqual(after_settle["reason"], "fragment_unavailable")

    def test_registered_native_followup_settle_and_reject_dispatch_receipts(self):
        self._scope_fake_telegram_credentials()
        plugin, handler, parser = self._native_frontend_handler("phase2_real_followup_dispatch")
        self.config.write_text("plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings:\n        replies_enabled: true\n", encoding="utf-8")
        (self.home / ".env").write_text(
            "TELEGRAM_BOT_TOKEN=77:synthetic-token\nTELEGRAM_HOME_CHANNEL=99\n", encoding="utf-8")
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}):
            notifier = importlib.import_module("phase2_real_followup_dispatch.notifier")
            followup = importlib.import_module("phase2_real_followup_dispatch.followup_bridge")
        self.assertIs(notifier.sys, sys, "exercise the real stdlib import; no test injection")
        state = self.home / "plugin-data" / FRONTEND_ID
        owner = "followup-native-owner"
        now = time.time()
        followup.bind_receiver(77, 99, None, "boot-followup", private_verified=True, state=state, now=now)
        followup.heartbeat_owner(owner, state=state, now=now)
        descriptor = {"owner": owner, "connectionId": "local", "profile": "default",
            "targetProfile": "default", "sid": "runtime-native", "session_key": "runtime-native",
            "epoch": "epoch-native", "started_at": "100", "final_row_id": 42}
        binding = followup.register_completion(descriptor, bot=77, chat=99, topic=None,
            message_id=900, state=state, now=now)
        self.assertTrue(binding["ok"], binding)
        binding_id = binding["binding_id"]
        network = []

        def telegram_stub(token, method, payload):
            network.append((method, payload))
            return {"ok": True, "result": {"message_id": payload.get("reply_parameters", {}).get("message_id", 900),
                    "chat": {"id": 99}}}

        def ingest(inbound_id, update_id):
            return followup.ingest(77, 99, 900, update_id, binding_id, {
                "inbound_id": inbound_id, "bot_id": 77, "author_bot_id": 77,
                "user_id": 99, "chat_id": 99, "private": True, "sender_chat": False,
                "topic": None, "reply_topic": None, "forwarded": False, "edited": False,
                "business": False, "command": False, "text": "synthetic followup content",
                "boot": "boot-followup",
            }, state=state, now=now)

        with patch.object(notifier, "telegram", side_effect=telegram_stub):
            self.assertTrue(ingest(200, 300)["ok"])
            ready, _ = self._call_native(handler, parser, {"action": "followup_ready", "owner": owner})
            self.assertTrue(ready["ok"])
            pending = ready["items"][0]
            claim, _ = self._call_native(handler, parser, {"action": "followup_claim", "owner": owner,
                "bot": 77, "chat": 99, "inbound_id": 200, "binding_id": binding_id,
                "descriptor": pending["descriptor"]})
            self.assertTrue(claim["ok"], claim)
            begun, _ = self._call_native(handler, parser, {"action": "followup_begin_submit", "owner": owner,
                "bot": 77, "chat": 99, "inbound_id": 200, "claim": claim["claim"]})
            self.assertTrue(begun["ok"], begun)
            settled, _ = self._call_native(handler, parser, {"action": "followup_settle", "owner": owner,
                "bot": 77, "chat": 99, "inbound_id": 200, "claim": claim["claim"],
                "status": "accepted_queued"})
            self.assertTrue(settled["ok"], settled)
            self.assertTrue(ingest(201, 301)["ok"])
            rejected, _ = self._call_native(handler, parser, {"action": "followup_reject", "owner": owner,
                "bot": 77, "chat": 99, "inbound_id": 201, "binding_id": binding_id})
            self.assertEqual(rejected, {"ok": True, "status": "rejected"})

        self.assertEqual([method for method, _ in network], ["sendMessage", "sendMessage"])
        self.assertTrue(all("synthetic followup content" not in payload["text"] for _, payload in network))
        self.assertIn("대기열", network[0][1]["text"])
        self.assertIn("거절", network[1][1]["text"])
        with followup.connect(state) as c:
            rows = c.execute("SELECT message_id,status,text FROM receipts ORDER BY message_id").fetchall()
            feedback = c.execute("SELECT count(*) FROM feedback").fetchone()[0]
        self.assertEqual([(row["message_id"], row["status"], row["text"]) for row in rows],
                         [(200, "accepted_queued", None), (201, "rejected", None)])
        self.assertEqual(feedback, 2)

    def test_malformed_timeout_native_receipt_drives_command_free_checkask(self):
        """Actual registered CLI receipts feed the real renderer, not a preset error category."""
        import subprocess
        plugin, handler, parser = self._native_frontend_handler('native_timeout_review_boundary',candidate='current')
        with patch.dict(os.environ, {'HERMES_HOME':str(self.home),'LOCALAPPDATA':str(self.local_app_data)}):
            notice_module=importlib.import_module('native_timeout_review_boundary.notifier')
            bridge=importlib.import_module('native_timeout_review_boundary.approval_bridge')
        state=self.home/'plugin-data'/FRONTEND_ID
        owner='native-timeout-owner'
        bridge.bind_receiver(77,99,None,'native-timeout-boot',private_verified=True,state=state)
        bridge.owner_heartbeat(owner,state=state)
        command='rm -rf approval-native-fixture && echo password=synthetic-secret-value-739102'
        dangerous,key,description=__import__('tools.approval_detection',fromlist=['detect_dangerous_command']).detect_dangerous_command(command)
        self.assertTrue(dangerous)
        with self.assertRaisesRegex(ValueError,'redaction_omission'):
            bridge.review_card({'params':{'command':command,'description':description}},300)
        calls=[]
        def telegram_stub(token,method,payload):
            calls.append((method,payload))
            return {'ok':True,'result':{'message_id':901,'chat':{'id':99,'type':'private'},'text':payload['text']}}
        script=r'''
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {registerHooks} from 'node:module';
registerHooks({resolve(s,c,n){
 if(s==='@hermes/plugin-sdk')return {url:'data:text/javascript,export const host={}',shortCircuit:true};
 // This fixture exercises the monitor only. Never pretend to render real React.
 if(s==='react/jsx-runtime')return {url:'data:text/javascript,export const jsx=()=>{throw Error("unexpected UI render in monitor fixture")}',shortCircuit:true};
 return n(s,c)
}});
const x=JSON.parse(readFileSync(0,'utf8'));
const {CompletionMonitor}=await import(x.entry);
const actions=[],notices=[];
const snapshot={epoch:'native-epoch',open_requests:[x.request]};
const api={state:{profile:{get:()=> 'default'}},profileRoutes:async()=>[x.route],notify:n=>notices.push(n),
 requestProfile:async(_r,m)=>{if(m==='session.events.since')return snapshot;if(m==='approval.pending')return {approvals:[x.pending]};throw Error(m)}};
const monitor=new CompletionMonitor(api,{setTimeout:()=>()=>{}});monitor.approvalCapable=true;monitor.replyOwner=x.owner;
monitor.helper=async req=>{actions.push(req.action);if(req.action==='approval_owner_heartbeat')return x.heartbeat;
 if(req.action==='approval_register')return x.registration;if(req.action==='approval_notice')return x.notice;
 if(req.action==='attention')return {ok:true,status:'sent'};throw Error(req.action)};
const record={sid:x.session.id,ask:null,approval:null,askChecking:false};
await monitor.checkAsk(record,x.route,x.session,snapshot,x.request);
assert.deepEqual(actions,['approval_owner_heartbeat','approval_register','approval_notice']);
assert.equal(record.approval,null);assert.equal(record.ask.id,x.request.id);
assert.equal(JSON.stringify(notices).includes(x.command),false);assert.equal(JSON.stringify(notices).includes('synthetic-secret-value-739102'),false);
console.log('actual native timeout receipt: command-free renderer boundary verified');
'''
        with patch.object(notice_module,'credentials',return_value=('77:synthetic-token',99,None)), \
                patch.object(notice_module,'telegram',side_effect=telegram_stub):
            for index,block in enumerate(('malformed',['malformed'])):
                with self.subTest(block=block):
                    self.config.write_text('plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings:\n        approvals_enabled: true\napprovals: '+json.dumps(block)+'\n',encoding='utf-8')
                    queue_id=('a' if index==0 else 'b')*32
                    request={'id':('srq-aabbcc112233' if index==0 else 'srq-112233aabbcc'),'method':'approval','params':{
                        'session_id':'runtime-native','request_id':queue_id,'command':command,'description':description,
                        'pattern_key':key,'pattern_keys':[key],'allow_permanent':True,'allow_session':True,
                        'choices':['once','session','always','deny']}}
                    pending={k:request['params'][k] for k in ('request_id','command','description','pattern_key','pattern_keys','allow_permanent','allow_session')}
                    route={'mode':'local','connectionId':'native-local','profile':'default','targetProfile':'default'}
                    session={'id':'runtime-native','session_key':'durable-native','status':'waiting'}
                    descriptor={'owner':owner,'connectionId':route['connectionId'],'profile':'default','targetProfile':'default',
                        'sid':session['id'],'session_key':session['session_key'],'epoch':'native-epoch',
                        'request_id':request['id'],'queue_id':queue_id,'dispatch_mode':'legacyqueue',
                        'request_fingerprint':bridge._digest(request),'pending_fingerprint':bridge._digest(pending)}
                    heartbeat,_=self._call_native(handler,parser,{'action':'approval_owner_heartbeat','capability':bridge.CAPABILITY,'owner':owner})
                    registration,_=self._call_native(handler,parser,{'action':'approval_register','capability':bridge.CAPABILITY,
                        'owner':owner,'descriptor':descriptor,'request':request,'pending':pending,'route':route,'session':session,'epoch':'native-epoch'})
                    self.assertEqual(registration,{'ok':False,'reason':'timeout_unavailable'})
                    before=len(calls)
                    notice_req={'action':'approval_notice','capability':bridge.CAPABILITY,'owner':owner,
                        'descriptor':descriptor,'request':request,'pending':pending,'reason':registration['reason']}
                    notice,_=self._call_native(handler,parser,notice_req)
                    self.assertTrue(notice['notification_only'])
                    duplicate,_=self._call_native(handler,parser,notice_req)
                    self.assertTrue(duplicate['duplicate'])
                    self.assertEqual(sum(m=='sendMessage' for m,_ in calls[before:]),1)
                    for _,payload in calls[before:]:
                        self.assertNotIn('reply_markup',payload)
                        self.assertNotIn(command,payload['text']);self.assertNotIn('synthetic-secret-value-739102',payload['text'])
                    c=bridge.connect(state)
                    try:self.assertEqual(c.execute('SELECT count(*) FROM approvals').fetchone()[0],0)
                    finally:c.close()
                    payload={'entry':(self.home/'plugins'/FRONTEND_ID/'desktop/plugin.js').as_uri(),'owner':owner,
                        'route':route,'session':session,'request':request,'pending':pending,'command':command,
                        'heartbeat':heartbeat,'registration':registration,'notice':notice}
                    child_env = dict(os.environ)
                    child_env.update({"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)})
                    result=subprocess.run(['node','--input-type=module','-e',script],input=json.dumps(payload),
                        text=True,capture_output=True,timeout=20,env=child_env)
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_registered_native_dispatcher_frames_real_approval_inbox_claim(self):
        """Run approval_* through the actual registered CLI handler in a fake Hermes home."""
        self._scope_fake_telegram_credentials()
        plugin, handler, parser = self._native_frontend_handler("phase2_real_approval_dispatch",candidate="current")
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}):
            notifier_module = importlib.import_module("phase2_real_approval_dispatch.notifier")
            bridge = importlib.import_module("phase2_real_approval_dispatch.approval_bridge")
        original_config = self.config.read_bytes()
        self.addCleanup(self.config.write_bytes, original_config)
        self.config.write_text(
            "plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings:\n        approvals_enabled: true\napprovals:\n  timeout: 300\n",
            encoding="utf-8")
        (self.home / ".env").write_text(
            "TELEGRAM_BOT_TOKEN=77:synthetic-token\nTELEGRAM_HOME_CHANNEL=99\n", encoding="utf-8")
        state = self.home / "plugin-data" / FRONTEND_ID
        now = time.time()
        bridge.bind_receiver(77, 99, None, "native-approval-boot", private_verified=True, state=state, now=now)
        owner = "native-desktop-owner"
        self.assertTrue(bridge.owner_heartbeat(owner, state=state, now=now)["feature_ready"])
        command = "rm -rf approval-native-fixture"
        dangerous, key, description = __import__("tools.approval_detection", fromlist=["detect_dangerous_command"]).detect_dangerous_command(command)
        self.assertTrue(dangerous)
        queue_id = "a" * 32
        request = {"id":"srq-aabbcc112233", "method":"approval", "params":{
            "session_id":"runtime-native", "request_id":queue_id, "command":command,
            "description":description, "pattern_key":key, "pattern_keys":[key],
            "allow_permanent":True, "allow_session":True,
            "choices":["once","session","always","deny"]}}
        pending = {k:request["params"][k] for k in ("request_id","command","description","pattern_key",
            "pattern_keys","allow_permanent","allow_session")}
        route = {"mode":"local", "connectionId":"native-local", "profile":"default", "targetProfile":"default"}
        session = {"id":"runtime-native", "session_key":"durable-native", "status":"waiting"}
        descriptor = {"owner":owner, "connectionId":route["connectionId"], "profile":route["profile"],
            "targetProfile":route["targetProfile"], "sid":session["id"], "session_key":session["session_key"],
            "epoch":"native-epoch", "request_id":request["id"], "queue_id":queue_id, "dispatch_mode":"legacyqueue",
            "request_fingerprint":bridge._digest(request), "pending_fingerprint":bridge._digest(pending)}
        calls=[]
        def telegram_stub(token, method, payload):
            calls.append((method, payload))
            if method == "sendMessage":
                return {"ok":True,"result":{"message_id":901,"chat":{"id":99,"type":"private"},
                    "text":payload["text"],"message_thread_id":None,"from":{"id":77,"is_bot":True},
                    "reply_markup":payload.get("reply_markup")}}
            return {"ok":True,"result":{"message_id":payload.get("message_id",901),"chat":{"id":99,"type":"private"}}}
        with patch.object(notifier_module, "telegram", side_effect=telegram_stub):
            heartbeat, _ = self._call_native(handler, parser, {"action":"approval_owner_heartbeat",
                "capability":bridge.CAPABILITY,"owner":owner})
            self.assertTrue(heartbeat["feature_ready"], heartbeat)
            registered, output = self._call_native(handler, parser, {"action":"approval_register",
                "capability":bridge.CAPABILITY,"owner":owner,"descriptor":descriptor,
                "request":request,"pending":pending,"route":route,"session":session,"epoch":"native-epoch"})
            self.assertTrue(registered["ok"], registered)
            self.assertLessEqual(len(output), 48_000)
            row = bridge.lookup_message(77,99,901,state=state)
            self.assertTrue(row)
            stored = bridge.connect(state)
            try: card = dict(stored.execute("SELECT * FROM approvals WHERE id=?",(registered["id"],)).fetchone())
            finally: stored.close()
            identity = {"bot":77,"boot":"native-approval-boot","chat":99,"user":99,"topic":None,
                "mid":901,"private":True,"human":True,"bot_author":True,"forwarded":False,
                "edited":False,"inline":False,"inaccessible":False,"message_text":card["review_text"],"event":"native-tap"}
            tapped = bridge.choose(card["token"], "once", identity, state=state, now=time.time()+0.1)
            self.assertTrue(tapped.get("ok"), tapped)
            observation = {"id":registered["id"],"request_fingerprint":descriptor["request_fingerprint"],
                "pending_fingerprint":descriptor["pending_fingerprint"],"authority":bridge._digest(descriptor)}
            polled, poll_output = self._call_native(handler, parser, {"action":"approval_poll",
                "capability":bridge.CAPABILITY,"owner":owner,"observations":[observation],"unverified":[],"absent":[]})
            self.assertLessEqual(len(poll_output),48_000)
            self.assertTrue(polled["ok"],polled)
            self.assertEqual(polled["items"][0]["choice"],"once")
            self.assertEqual(polled['items'][0]['descriptor']['queue_id'],queue_id)
            claim=polled['items'][0]
            before_gate=bridge.connect(state)
            try:before_row=dict(before_gate.execute('SELECT * FROM approvals WHERE id=?',(claim['id'],)).fetchone())
            finally:before_gate.close()
            authority=bridge._digest(claim['descriptor'])
            validity, validity_output = self._call_native(handler, parser, {"action":"approval_claim_valid",
                "capability":bridge.CAPABILITY,"owner":owner,"id":claim['id'],"claim":claim['claim'],
                "descriptor":claim['descriptor'],"authority":authority,"deadline":claim['deadline']})
            self.assertLessEqual(len(validity_output),48_000)
            self.assertEqual(validity,{"ok":True,"valid":True,"id":claim['id'],"claim":claim['claim'],
                "authority":authority,"deadline":claim['deadline']})
            after_gate=bridge.connect(state)
            try:after_row=dict(after_gate.execute('SELECT * FROM approvals WHERE id=?',(claim['id'],)).fetchone())
            finally:after_gate.close()
            self.assertEqual(after_row,before_row,'registered native read gate must not mutate claim state')
            # A REAL valid applying claim must stop when the managed policy
            # becomes unreadable/malformed; raw true cannot restore authority.
            managed = self.root / 'managed applying-claim policy'
            managed.mkdir()
            policy = managed / 'config.yaml'
            with patch.dict(os.environ, {'HERMES_MANAGED_DIR': str(managed)}):
                for text in ('plugins: [', '- non-mapping policy',
                             'plugins:\n  entries:\n    session-complete-telegram:\n      settings:\n        approvals_enabled: false\n'):
                    policy.write_text(text, encoding='utf-8')
                    stopped, _ = self._call_native(handler, parser, {
                        'action': 'approval_claim_valid', 'capability': bridge.CAPABILITY,
                        'owner': owner, 'id': claim['id'], 'claim': claim['claim'],
                        'descriptor': claim['descriptor'], 'authority': authority,
                        'deadline': claim['deadline']})
                    self.assertIs(stopped.get('valid'), False, stopped)
                policy.write_text('{}\n', encoding='utf-8')
                restored, _ = self._call_native(handler, parser, {
                    'action': 'approval_claim_valid', 'capability': bridge.CAPABILITY,
                    'owner': owner, 'id': claim['id'], 'claim': claim['claim'],
                    'descriptor': claim['descriptor'], 'authority': authority,
                    'deadline': claim['deadline']})
                self.assertIs(restored.get('valid'), True, restored)
            for label,changes in (
                ('owner',{'owner':'other-native-owner'}),('claim',{'claim':'0'*32}),
                ('authority',{'authority':'0'*64})):
                with self.subTest(gate=label):
                    denied, denied_output = self._call_native(handler, parser, {
                        "action":"approval_claim_valid","capability":bridge.CAPABILITY,
                        "owner":changes.get('owner',owner),"id":claim['id'],
                        "claim":changes.get('claim',claim['claim']),"descriptor":claim['descriptor'],
                        "authority":changes.get('authority',authority),"deadline":claim['deadline']})
                    self.assertLessEqual(len(denied_output),48_000)
                    self.assertIs(denied.get('valid'),False,denied)

            inbox=bridge.connect(state)
            try:
                inbox.execute('UPDATE approvals SET deadline=? WHERE id=?',(time.time()-1,claim['id']))
            finally:inbox.close()
            expired,_=self._call_native(handler,parser,{"action":"approval_claim_valid","capability":bridge.CAPABILITY,
                "owner":owner,"id":claim['id'],"claim":claim['claim'],"descriptor":claim['descriptor'],
                "authority":authority,"deadline":claim['deadline']})
            self.assertIs(expired.get('valid'),False,expired)
            inbox=bridge.connect(state)
            try:
                inbox.execute('UPDATE approvals SET deadline=?,expires=? WHERE id=?',
                    (claim['deadline'],time.time()-1,claim['id']))
            finally:inbox.close()
            absolute_expired,_=self._call_native(handler,parser,{"action":"approval_claim_valid","capability":bridge.CAPABILITY,
                "owner":owner,"id":claim['id'],"claim":claim['claim'],"descriptor":claim['descriptor'],
                "authority":authority,"deadline":claim['deadline']})
            self.assertIs(absolute_expired.get('valid'),False,absolute_expired)
            inbox=bridge.connect(state)
            try:inbox.execute('UPDATE approvals SET expires=? WHERE id=?',(before_row['expires'],claim['id']))
            finally:inbox.close()

            live_config="plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings:\n        replies_enabled: true\n        approvals_enabled: true\napprovals:\n  timeout: 300\n"
            self.config.write_text("plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings:\n        replies_enabled: true\n        approvals_enabled: false\napprovals:\n  timeout: 300\n",encoding="utf-8")
            disabled,_=self._call_native(handler,parser,{"action":"approval_claim_valid","capability":bridge.CAPABILITY,
                "owner":owner,"id":claim['id'],"claim":claim['claim'],"descriptor":claim['descriptor'],
                "authority":authority,"deadline":claim['deadline']})
            self.assertIs(disabled.get('valid'),False,disabled)
            self.assertEqual(disabled.get('reason'),'approvals_disabled')
            self.config.write_text(live_config,encoding="utf-8")
            self.assertTrue(bridge.bind_receiver(77,99,None,"native-approval-boot-after-claim",
                private_verified=True,state=state,now=time.time()))
            lost,_=self._call_native(handler,parser,{"action":"approval_claim_valid","capability":bridge.CAPABILITY,
                "owner":owner,"id":claim['id'],"claim":claim['claim'],"descriptor":claim['descriptor'],
                "authority":authority,"deadline":claim['deadline']})
            self.assertIs(lost.get('valid'),False,lost)
            replay,_=self._call_native(handler,parser,{"action":"approval_poll","capability":bridge.CAPABILITY,
                "owner":owner,"observations":[{"id":claim['id'],"request_fingerprint":descriptor['request_fingerprint'],
                    "pending_fingerprint":descriptor['pending_fingerprint'],"authority":authority}],
                "unverified":[],"absent":[]})
            self.assertEqual(replay.get('items'),[]);self.assertIn(claim['id'],replay.get('terminal',[]))
            self.assertEqual([method for method,_ in calls],["sendMessage","editMessageText","editMessageText","editMessageText"])
            self.assertIs(calls[0][1]["protect_content"],False)

    def test_native_settings_status_and_single_key_write_use_captured_plugin_context(self):
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest
        target = self.home / "plugins" / FRONTEND_ID
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(REPO / "desktop-plugins" / FRONTEND_ID, target,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        config = chr(10).join(("plugins:", "  enabled: [session-complete-telegram]", "  entries:",
            "    session-complete-telegram:", "      settings:", "        replies_enabled: false",
            "        approvals_enabled: false", "phase2_sentinel: keep", "" )).encode()
        self.config.write_bytes(config)
        self.expected_config_bytes = config
        manager = PluginManager(scope_key=str(self.home))
        manifest = PluginManifest(name=FRONTEND_ID, key=FRONTEND_ID, source="user", path=str(target))
        native_context = PluginContext(manifest, manager)
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}):
            plugin = _load_package(target, "phase2_native_settings_context")
            plugin.register(native_context)
        registration = manager._cli_commands[COMMAND_ID]
        parser = argparse.ArgumentParser()
        registration["setup_fn"](parser)
        handler = registration["handler_fn"]
        inventory = sorted(str(path.relative_to(self.state)) for path in self.state.rglob("*"))
        with (
                patch("hermes_cli.profiles.get_active_profile_name", return_value="default"),
                patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}),
        ):
            status, _ = self._call_native(handler, parser, {"action": "settings_status"})
            self.assertTrue(status["ok"], status)
            self.assertTrue(status["read_only"])
            self.assertEqual(status["version"], "1.8.1")
            self.assertEqual(status["settings"]["service_enabled"], True)
            self.assertEqual(status["effective"], {
                "completion_notifications": True, "attention_notifications": True,
                "clarify_replies": False, "completion_followups": False, "approvals": False,
            })
            self.assertEqual(inventory, sorted(str(path.relative_to(self.state)) for path in self.state.rglob("*")),
                             "read-only settings status must not create state")
            self.assertEqual(self.config.read_bytes(), config)
            changed, _ = self._call_native(handler, parser, {"action": "settings_set", "key": "service_enabled",
                "value": False, "expected": True})
            self.assertTrue(changed["ok"], changed)
            self.assertFalse(changed["read_only"])
            self.assertEqual(changed["readback"], {"key": "service_enabled", "value": False})
            self.assertFalse(any(changed["effective"].values()))
            stale, _ = self._call_native(handler, parser, {"action": "settings_set", "key": "service_enabled",
                "value": True, "expected": True})
            self.assertFalse(stale["ok"], stale)
        self.assertIn(b"phase2_sentinel: keep", self.config.read_bytes())
        self.assertIn(b"service_enabled: false", self.config.read_bytes())
        self.assertIn(b"replies_enabled: false", self.config.read_bytes())
        self.expected_config_bytes = self.config.read_bytes()
        self.assertEqual(sorted([*inventory, "notifications.sqlite3"]),
                         sorted(str(path.relative_to(self.state)) for path in self.state.rglob("*")),
                         "public OFF creates only the bounded recovery-generation store")
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.state / "notifications.sqlite3")) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {"completion_candidates", "completion_recovery_meta"})
            self.assertEqual(db.execute("SELECT count(*) FROM completion_candidates").fetchone()[0], 0)
            generation, enabled = db.execute("SELECT generation,enabled FROM completion_recovery_meta WHERE singleton=1").fetchone()
            self.assertRegex(generation, r"^[0-9a-f]{32}$")
            self.assertEqual(enabled, 0)
        self._assert_preserved()

    def test_native_loaded_handshake_hides_capability_on_helper_version_skew(self):
        _plugin, handler, parser = self._native_frontend_handler("phase2_approval_version_skew")
        config = ("plugins:\n  enabled: [session-complete-telegram]\n  entries:\n"
                  "    session-complete-telegram:\n      settings:\n        approvals_enabled: true\n").encode()
        self.config.write_bytes(config)
        self.expected_config_bytes = config
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}):
            bridge = importlib.import_module("phase2_approval_version_skew.approval_bridge")
        with patch.object(bridge, "VERSION", "1.6.1"), patch.object(bridge, "CAPABILITY", "desktop-command-approval-v2"), \
                patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}):
            result, _ = self._call_native(handler, parser, {"action":"loaded","version":"1.6.1","profiles":[]})
        self.assertEqual(result["version"], "1.8.0")
        self.assertEqual(result["package_version"], "1.8.1")
        self.assertEqual(result["capabilities"], ["desktop-completion-recovery-v1"])

    def test_new_helper_loaded_handshake_rejects_old_renderer_contract(self):
        _plugin, handler, parser = self._native_frontend_handler("phase2_new_handshake_fixture", candidate="current")
        config = ("plugins:\n  enabled: [session-complete-telegram]\n  entries:\n"
                  "    session-complete-telegram:\n      settings:\n        approvals_enabled: true\n").encode()
        self.config.write_bytes(config)
        self.expected_config_bytes = config
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}):
            result, _ = self._call_native(handler, parser, {"action":"loaded","version":"1.6.1","profiles":[]})
        self.assertEqual(result["version"], "1.8.0")
        self.assertEqual(result["package_version"], "1.8.1")
        self.assertEqual(result["capabilities"], ["desktop-command-approval-v6", "desktop-completion-recovery-v1"])
        self.assertNotEqual((result["version"], "desktop-command-approval-v6"),
                            ("1.6.1", "desktop-command-approval-v2"))

    def test_immutable_180_helper_and_renderer_keep_approval_v4_while_181_adds_recovery(self):
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest
        pinned = _copy_candidate(self.root / "exact-public-180", "1.8.0")
        old_source = pinned / "desktop-plugins" / FRONTEND_ID
        old_home = self.root / "old-helper-home"
        old_target = old_home / "plugins" / FRONTEND_ID
        old_target.parent.mkdir(parents=True)
        shutil.copytree(old_source, old_target)
        old_home.mkdir(exist_ok=True)
        (old_home / "config.yaml").write_text(
            "plugins:\n  enabled: [session-complete-telegram]\n  entries:\n"
            "    session-complete-telegram:\n      settings: {}\n", encoding="utf-8")
        old_manager = PluginManager(scope_key=str(old_home))
        old_manifest = PluginManifest(name=FRONTEND_ID, key=FRONTEND_ID, source="user", path=str(old_target))
        old_context = PluginContext(old_manifest, old_manager)
        with patch.dict(os.environ, {"HERMES_HOME": str(old_home), "LOCALAPPDATA": str(self.local_app_data)}):
            old_package = _load_package(old_target, "immutable_180_approval_compat_fixture")
            old_package.register(old_context)
        old_registration = old_manager._cli_commands[COMMAND_ID]
        old_parser = argparse.ArgumentParser()
        old_registration["setup_fn"](old_parser)
        raw = json.dumps({"action":"loaded", "version":"1.8.1", "profiles":[]}, separators=(",", ":")).encode()
        token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"), \
             patch.dict(os.environ, {"HERMES_HOME": str(old_home), "LOCALAPPDATA": str(self.local_app_data)}), \
             contextlib.redirect_stdout(io.StringIO()) as old_output:
            old_registration["handler_fn"](old_parser.parse_args(["--request", token]))
        old_reply = _frame_value(old_output.getvalue())
        self.assertEqual(old_reply["version"], "1.8.0")
        self.assertNotIn("package_version", old_reply)
        self.assertEqual(old_reply["capabilities"], ["desktop-command-approval-v4"])
        old_renderer = (old_source / "desktop" / "plugin.js").read_text(encoding="utf-8")
        self.assertIn("VERSION = '1.8.0'", old_renderer)
        self.assertIn("reply.version === VERSION", old_renderer)

        _plugin, new_handler, new_parser = self._native_frontend_handler("current_181_compat_fixture", candidate="current")
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}):
            new_reply, _ = self._call_native(new_handler, new_parser, {"action":"loaded","version":"1.8.0","profiles":[]})
        self.assertEqual(new_reply["version"], "1.8.0")
        self.assertEqual(new_reply["package_version"], "1.8.1")
        self.assertEqual(new_reply["capabilities"], ["desktop-command-approval-v6", "desktop-completion-recovery-v1"])
        self.assertEqual(old_reply["version"], new_reply["version"])
        self.assertEqual(old_reply["capabilities"], ["desktop-command-approval-v4"])
        self.assertNotEqual(old_reply["capabilities"][0], new_reply["capabilities"][0])

    def test_primary_runtime_location_and_home_guards_refuse_foreign_and_named_homes(self):
        self._install_package("current", "desktop-plugins/session-complete-telegram", force=False)
        target = self.home / "plugins" / FRONTEND_ID
        _load_package(target, "primary_runtime_home_guard_fixture")
        runtime = importlib.import_module("primary_runtime_home_guard_fixture.runtime")

        foreign = self.root / "foreign home"
        foreign.mkdir()
        with patch.dict(os.environ, {"HERMES_HOME": str(foreign)}):
            with self.assertRaisesRegex(RuntimeError, "foreign_hermes_home_refused"):
                runtime.resolve_package_home(target / "__init__.py")

        named_home = self.root / "global home" / "profiles" / "arbitrary-user"
        named_package = named_home / "plugins" / FRONTEND_ID
        named_package.parent.mkdir(parents=True)
        shutil.copytree(target, named_package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        _load_package(named_package, "primary_named_home_guard_fixture")
        named_runtime = importlib.import_module("primary_named_home_guard_fixture.runtime")
        with patch.dict(os.environ, {"HERMES_HOME": str(named_home)}):
            with self.assertRaisesRegex(RuntimeError, "named_profile_home_refused"):
                named_runtime.resolve_package_home(named_package / "__init__.py")

        misplaced = self.root / "misplaced" / FRONTEND_ID
        misplaced.parent.mkdir()
        shutil.copytree(target, misplaced, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
            with self.assertRaisesRegex(RuntimeError, "native_plugin_location_invalid"):
                runtime.resolve_package_home(misplaced / "__init__.py")
        self.assertFalse((foreign / "plugin-data").exists())
        self.assertFalse((named_home / "plugin-data").exists())
    def test_legacy_enabled_blocks_factory_and_all_helper_readiness(self):
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest

        self._install_package("current", "desktop-plugins/session-complete-telegram", force=False)
        self._install_package("old", "plugins/desktop-clarify-telegram", force=False)
        self.config.write_text(
            "plugins:\n  enabled: [session-complete-telegram, desktop-clarify-telegram]\n"
            "  entries:\n    session-complete-telegram:\n      settings:\n"
            "        replies_enabled: true\n        approvals_enabled: true\n",
            encoding="utf-8")
        self.expected_config_bytes = self.config.read_bytes()
        target = self.home / "plugins" / FRONTEND_ID
        manager = PluginManager(scope_key=str(self.home))
        ctx = PluginContext(PluginManifest(name=FRONTEND_ID, key=FRONTEND_ID, source="user", path=str(target)), manager)
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}):
            plugin = _load_package(target, "legacy_overlap_guard_fixture")
            plugin.register(ctx)
            factory, owner = manager.get_platform_handler_factories("telegram")[0]
            self.assertEqual(owner, FRONTEND_ID)
            gateway = importlib.import_module("legacy_overlap_guard_fixture.telegram_integration")
            with self.assertLogs("hermes.plugin.session_complete_telegram", level="WARNING") as logs:
                factory(object(), object())
            self.assertTrue(any("legacy_package_migration_required" in line for line in logs.output))

            registration = manager._cli_commands[COMMAND_ID]
            parser = argparse.ArgumentParser()
            registration["setup_fn"](parser)
            self.assertEqual(self._call_native(registration["handler_fn"], parser,
                {"action": "reply_poll", "owner": "fixture", "observations": [], "unverified": []})[0],
                {"ok": False, "feature_ready": False, "reason": "legacy_package_migration_required"})
            self.assertEqual(self._call_native(registration["handler_fn"], parser,
                {"action": "approval_owner_heartbeat", "capability": "desktop-command-approval-v6",
                 "owner": "fixture"})[0],
                {"ok": False, "feature_ready": False, "reason": "legacy_package_migration_required"})
        self.assertEqual(manager.get_platform_handler_factories("telegram"), [(factory, FRONTEND_ID)])
        self._assert_preserved()

    def test_mixed_v2_receiver_helper_and_renderer_contracts_are_denied(self):
        plugin, handler, parser = self._native_frontend_handler("phase2_new_handshake_fixture", candidate="current")
        config = ("plugins:\n  enabled: [session-complete-telegram]\n  entries:\n"
                  "    session-complete-telegram:\n      settings:\n        approvals_enabled: true\n").encode()
        self.config.write_bytes(config)
        self.expected_config_bytes = config
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}):
            notifier_module = importlib.import_module("phase2_new_handshake_fixture.notifier")
            bridge = importlib.import_module("phase2_new_handshake_fixture.approval_bridge")
            result, _ = self._call_native(handler, parser, {"action": "loaded", "version": "1.6.1", "profiles": []})
            old_helper = notifier_module.dispatch_request({
                "action": "approval_owner_heartbeat", "capability": "desktop-command-approval-v2", "owner": "old-owner"
            }, root=self.home, state=self.state, native=True)
        self.assertEqual(result["version"], "1.8.0")
        self.assertEqual(result["package_version"], "1.8.1")
        self.assertEqual(result["capabilities"], ["desktop-command-approval-v6", "desktop-completion-recovery-v1"])
        self.assertNotEqual(result["version"], "1.6.1")
        self.assertEqual(old_helper, {"ok": False, "reason": "capability_mismatch"})
        self.assertEqual(bridge.VERSION, "1.8.0")
        self.assertEqual(bridge.CAPABILITY, "desktop-command-approval-v6")
    def test_legacy_compatibility_package_is_deprecated_and_registers_no_factory(self):
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest

        manager = PluginManager(scope_key=str(self.home))
        legacy_path = REPO / "plugins" / INGRESS_ID
        manifest = PluginManifest(name=INGRESS_ID, key=INGRESS_ID, source="user", path=str(legacy_path))
        ctx = PluginContext(manifest, manager)
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home), "LOCALAPPDATA": str(self.local_app_data)}):
            legacy = _load_package(legacy_path, "deprecated_legacy_stub_fixture")
            legacy.register(ctx)
        self.assertEqual(manager.get_platform_handler_factories("telegram"), [])
        self.assertEqual(manager._cli_commands, {})
        text = (legacy_path / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("Deprecated compatibility stub", text)
        self._assert_preserved()

    def test_internal_modules_are_relative_and_do_not_publish_global_aliases(self):
        baseline_modules = {name: sys.modules.get(name) for name in ("notifier", "reply_bridge", "followup_bridge",
                                                                      "approval_bridge", "telegram_integration")}
        baseline_path = list(sys.path)
        package_modules = []
        for index, label in enumerate(("internal-home-one", "internal-home-two")):
            home = self.root / label
            package = home / "plugins" / FRONTEND_ID
            package.parent.mkdir(parents=True)
            shutil.copytree(REPO / "desktop-plugins" / FRONTEND_ID, package,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            module_name = f"_relative_package_fixture_{index}"
            with patch.dict(os.environ, {"HERMES_HOME": str(home), "LOCALAPPDATA": str(self.local_app_data)}):
                plugin = _load_package(package, module_name)
                notifier_module = importlib.import_module(f"{module_name}.notifier")
                reply_module = importlib.import_module(f"{module_name}.reply_bridge")
                followup_module = importlib.import_module(f"{module_name}.followup_bridge")
                approval_module = importlib.import_module(f"{module_name}.approval_bridge")
                gateway_module = importlib.import_module(f"{module_name}.telegram_integration")
            package_modules.append((notifier_module, reply_module, followup_module, approval_module, gateway_module))
            self.assertEqual(notifier_module.VERSION, "1.8.1")
            self.assertEqual(approval_module.CAPABILITY, "desktop-command-approval-v6")
            self.assertEqual(approval_module.VERSION, notifier_module.APPROVAL_COMPAT_VERSION)
            self.assertEqual(notifier_module.ROOT, home.resolve())
            self.assertTrue(all(module.__name__.startswith(module_name + ".") for module in package_modules[-1]))
            self.assertFalse((home / "plugin-data").exists(), "declaration/import must not access bridge state")
        self.assertNotEqual(package_modules[0][0].__name__, package_modules[1][0].__name__)
        self.assertEqual(sys.path, baseline_path, "package imports must not widen global import paths")
        self.assertTrue(all(sys.modules.get(name) is baseline for name, baseline in baseline_modules.items()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
