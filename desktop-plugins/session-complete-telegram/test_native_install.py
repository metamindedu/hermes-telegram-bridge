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
import sys
import time
import unittest
from unittest.mock import patch

import test_support

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


def _copy_candidate(destination: Path, version: str) -> Path:
    destination.mkdir(parents=True)
    for source, relative in (
        (REPO / "desktop-plugins" / FRONTEND_ID, Path("desktop-plugins") / FRONTEND_ID),
        (REPO / "plugins" / INGRESS_ID, Path("plugins") / INGRESS_ID),
    ):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        manifest = target / "plugin.yaml"
        text = manifest.read_text(encoding="utf-8")
        manifest.write_text(text.replace("version: '1.6.0'", f"version: '{version}'"), encoding="utf-8")
    return destination


class NativeInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = test_support.temporary_directory(prefix="native phase2 ")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        # Native PM keeps generation leases until process exit. Close only the
        # leases created in this owned fixture before Windows directory cleanup.
        self.addCleanup(self._release_fixture_pm_leases)
        self.home = self.root / "fake Hermes home"
        self.home.mkdir()
        self.config = self.home / "config.yaml"
        self.config_bytes = b"plugins:\n  enabled: []\nphase2_sentinel: do-not-change\n"
        self.config.write_bytes(self.config_bytes)
        self.state = self.home / "plugin-data" / FRONTEND_ID
        self.state.mkdir(parents=True)
        self.sentinel = self.state / "sentinel.bin"
        self.sentinel_bytes = b"preserve-existing-ledger-and-data\x00phase2"
        self.sentinel.write_bytes(self.sentinel_bytes)
        self.candidates = {
            "v1": _copy_candidate(self.root / "candidate v1", "1.6.0"),
            "v2": _copy_candidate(self.root / "candidate v2", "1.6.1"),
        }
        self.revisions = {
            "v1": "1" * 40,
            "v2": "2" * 40,
        }

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

        with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}), \
                patch.object(plugins_cmd, "_clone_plugin_repo", side_effect=local_transport), \
                contextlib.redirect_stdout(io.StringIO()):
            # This is the native install command path. Only Git transport is
            # replaced; scanner, manifest parsing, publication and metadata are real.
            plugins_cmd.cmd_install(identifier, force=force, enable=False, ref=revision,
                                    no_deps=True, yes_deps=False)
        self.assertEqual(len(copied), 1)
        return self.home / "plugins" / Path(package_dir).name

    def _assert_preserved(self):
        self.assertEqual(self.config.read_bytes(), self.config_bytes)
        self.assertEqual(self.sentinel.read_bytes(), self.sentinel_bytes)

    def _assert_package_ids(self):
        frontend = self.home / "plugins" / FRONTEND_ID
        ingress = self.home / "plugins" / INGRESS_ID
        self.assertIn("name: session-complete-telegram", (frontend / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertIn("id: ID", (frontend / "desktop" / "plugin.js").read_text(encoding="utf-8"))
        self.assertIn("name: desktop-clarify-telegram", (ingress / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertTrue((self.home / "plugin-data" / FRONTEND_ID).is_dir())
        self.assertTrue((self.home / "plugins" / FRONTEND_ID).is_dir())
        self.assertTrue((self.home / "plugins" / INGRESS_ID).is_dir())

    def _native_frontend_handler(self, module_name):
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest

        self._install_package("v1", "desktop-plugins/session-complete-telegram", force=False)
        target = self.home / "plugins" / FRONTEND_ID
        manager = PluginManager(scope_key=str(self.home))
        manifest = PluginManifest(name=FRONTEND_ID, key=FRONTEND_ID, source="user", path=str(target))
        ctx = PluginContext(manifest, manager)
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
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
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}), \
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

    def test_native_fresh_update_and_rollback_keep_ids_config_and_plugin_data(self):
        frontend_path = "desktop-plugins/session-complete-telegram"
        ingress_path = "plugins/desktop-clarify-telegram"
        self._install_package("v1", frontend_path, force=False)
        self._install_package("v1", ingress_path, force=False)
        self._assert_preserved()
        self._assert_package_ids()
        self.assertTrue((self.home / "plugins" / FRONTEND_ID / "approval_bridge.py").is_file())
        self.assertIn("desktop-command-approval-v1", (self.home / "plugins" / FRONTEND_ID / "approval_bridge.py").read_text(encoding="utf-8"))

        self._install_package("v2", frontend_path, force=True)
        self._install_package("v2", ingress_path, force=True)
        self.assertIn("version: '1.6.1'", (self.home / "plugins" / FRONTEND_ID / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertTrue((self.home / "plugins" / FRONTEND_ID / "approval_bridge.py").is_file())
        self._assert_preserved()
        self._assert_package_ids()

        # Rollback is the same native pinned install transaction using the
        # previous immutable ref; no bespoke deploy/rollback executor exists.
        self._install_package("v1", frontend_path, force=True)
        self._install_package("v1", ingress_path, force=True)
        self.assertIn("version: '1.6.0'", (self.home / "plugins" / FRONTEND_ID / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertTrue((self.home / "plugins" / FRONTEND_ID / "approval_bridge.py").is_file())
        self._assert_preserved()
        self._assert_package_ids()

    def test_native_plugin_doctor_accepts_both_installed_packages(self):
        from hermes_cli import plugins_cmd

        self._install_package("v1", "desktop-plugins/session-complete-telegram", force=False)
        self._install_package("v1", "plugins/desktop-clarify-telegram", force=False)
        for plugin_id in (FRONTEND_ID, INGRESS_ID):
            with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}), contextlib.redirect_stdout(io.StringIO()):
                plugins_cmd.cmd_plugin_doctor(str(self.home / "plugins" / plugin_id), ci=True)
        self._assert_preserved()

    def test_real_plugin_context_registers_one_ingress_factory_only(self):
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest

        self._install_package("v1", "plugins/desktop-clarify-telegram", force=False)
        target = self.home / "plugins" / INGRESS_ID
        manager = PluginManager(scope_key=str(self.home))
        manifest = PluginManifest(name=INGRESS_ID, key=INGRESS_ID, source="user", path=str(target))
        ctx = PluginContext(manifest, manager)
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
            plugin = _load_package(target, "phase2_ingress_fixture")
            plugin.register(ctx)
        factories = manager.get_platform_handler_factories("telegram")
        self.assertEqual(len(factories), 1)
        factory, owner = factories[0]
        self.assertEqual(owner, INGRESS_ID)
        self.assertEqual(factory.__name__, "telegram_factory")
        self.assertEqual(manager.get_platform_handler_factories("telegram"), [(factory, INGRESS_ID)])

    def test_real_frontend_cli_registration_parsing_and_single_action_dispatch(self):
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest

        self._install_package("v1", "desktop-plugins/session-complete-telegram", force=False)
        target = self.home / "plugins" / FRONTEND_ID
        manager = PluginManager(scope_key=str(self.home))
        manifest = PluginManifest(name=FRONTEND_ID, key=FRONTEND_ID, source="user", path=str(target))
        ctx = PluginContext(manifest, manager)
        encoded = base64.urlsafe_b64encode(b'{"action":"unknown-native-fixture"}').rstrip(b"=").decode("ascii")
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
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
                                         native=True, result_budget=plugin.MAX_RESULT_BYTES)
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
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}), \
                patch.object(notifier, "dispatch_request", wraps=notifier.dispatch_request) as large_dispatch, \
                contextlib.redirect_stdout(io.StringIO()) as large_stdout:
            registration["handler_fn"](parser.parse_args(["--request", encoded_large]))
        large_dispatch.assert_called_once_with(large_request, root=self.home.resolve(),
                                              native=True, result_budget=plugin.MAX_RESULT_BYTES)
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

        plugin, handler, parser = self._native_frontend_handler("phase2_real_reply_dispatch")
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
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
        self.assertEqual(disabled, {"ok": False, "reason": "bridge_disabled"})

        self.config.write_text("plugins:\n  enabled:\n    - desktop-clarify-telegram\n", encoding="utf-8")
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
        plugin, handler, parser = self._native_frontend_handler("phase2_real_followup_dispatch")
        self.config.write_text("plugins:\n  enabled:\n    - desktop-clarify-telegram\n", encoding="utf-8")
        (self.home / ".env").write_text(
            "TELEGRAM_BOT_TOKEN=77:synthetic-token\nTELEGRAM_HOME_CHANNEL=99\n", encoding="utf-8")
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
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

    def test_registered_native_dispatcher_frames_real_approval_inbox_claim(self):
        """Run approval_* through the actual registered CLI handler in a fake Hermes home."""
        plugin, handler, parser = self._native_frontend_handler("phase2_real_approval_dispatch")
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
            notifier_module = importlib.import_module("phase2_real_approval_dispatch.notifier")
            bridge = importlib.import_module("phase2_real_approval_dispatch.approval_bridge")
        original_config = self.config.read_bytes()
        self.addCleanup(self.config.write_bytes, original_config)
        self.config.write_text(
            "plugins:\n  enabled:\n    - desktop-clarify-telegram\napprovals:\n  timeout: 300\n",
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
            "epoch":"native-epoch", "request_id":request["id"], "queue_id":queue_id,
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
            self.assertEqual(polled["items"][0]["descriptor"]["queue_id"],queue_id)
            self.assertEqual([method for method,_ in calls],["sendMessage","editMessageText","editMessageText"])
            self.assertIs(calls[0][1]["protect_content"],False)

    def test_native_loaded_handshake_hides_capability_on_helper_version_skew(self):
        plugin, handler, parser = self._native_frontend_handler("phase2_approval_version_skew")
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
            bridge = importlib.import_module("phase2_approval_version_skew.approval_bridge")
        with patch.object(bridge, "VERSION", "1.5.0"), patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
            result, _ = self._call_native(handler, parser, {"action":"loaded","version":"1.6.0","profiles":[]})
        self.assertEqual(result["version"], "1.6.0")
        self.assertNotIn("desktop-command-approval-v1", result.get("capabilities", []))

    def test_profile_context_guards_refuse_foreign_and_named_homes(self):
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest

        self._install_package("v1", "plugins/desktop-clarify-telegram", force=False)
        self._install_package("v1", "desktop-plugins/session-complete-telegram", force=False)
        ingress_target = self.home / "plugins" / INGRESS_ID
        frontend_target = self.home / "plugins" / FRONTEND_ID
        ctx = PluginContext(PluginManifest(name=INGRESS_ID, key=INGRESS_ID, source="user"),
                            PluginManager(scope_key=str(self.home)))
        ingress = _load_package(ingress_target, "phase2_home_guard_fixture")

        other = self.root / "other home"
        other.mkdir()
        with patch.dict(os.environ, {"HERMES_HOME": str(other)}):
            with self.assertRaisesRegex(RuntimeError, "foreign_hermes_home_refused"):
                ingress._validated_home(ctx)

        named_home = self.root / "global home" / "profiles" / "claire"
        named_package = named_home / "plugins" / INGRESS_ID
        named_package.mkdir(parents=True)
        shutil.copytree(ingress_target, named_package, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        named_ingress = _load_package(named_package, "phase2_named_home_guard_fixture")
        named_ctx = PluginContext(PluginManifest(name=INGRESS_ID, key=INGRESS_ID, source="user"),
                                  PluginManager(scope_key=str(named_home)))
        with patch.dict(os.environ, {"HERMES_HOME": str(named_home)}):
            with self.assertRaisesRegex(RuntimeError, "named_profile_home_refused"):
                named_ingress._validated_home(named_ctx)

        _load_package(frontend_target, "phase2_frontend_home_guard_fixture")
        runtime = importlib.import_module("phase2_frontend_home_guard_fixture.runtime")
        with patch.dict(os.environ, {"HERMES_HOME": str(other)}):
            with self.assertRaisesRegex(RuntimeError, "foreign_hermes_home_refused"):
                runtime.resolve_package_home(frontend_target / "__init__.py")

        named_frontend = named_home / "plugins" / FRONTEND_ID
        named_frontend.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(frontend_target, named_frontend, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        _load_package(named_frontend, "phase2_named_frontend_guard_fixture")
        named_runtime = importlib.import_module("phase2_named_frontend_guard_fixture.runtime")
        with patch.dict(os.environ, {"HERMES_HOME": str(named_home)}):
            with self.assertRaisesRegex(RuntimeError, "named_profile_home_refused"):
                named_runtime.resolve_package_home(named_frontend / "__init__.py")

    def test_ingress_factory_fails_closed_when_frontend_manifest_version_skews(self):
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import PluginManifest

        self._install_package("v1", "plugins/desktop-clarify-telegram", force=False)
        self._install_package("v1", "desktop-plugins/session-complete-telegram", force=False)
        frontend_manifest = self.home / "plugins" / FRONTEND_ID / "plugin.yaml"
        frontend_manifest.write_text(frontend_manifest.read_text(encoding="utf-8").replace(
            "version: '1.6.0'", "version: '1.5.0'"), encoding="utf-8")
        manager = PluginManager(scope_key=str(self.home))
        ctx = PluginContext(PluginManifest(name=INGRESS_ID, key=INGRESS_ID, source="user",
                                           path=str(self.home / "plugins" / INGRESS_ID)), manager)
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
            ingress = _load_package(self.home / "plugins" / INGRESS_ID, "phase2_factory_skew_fixture")
            ingress.register(ctx)
            factory, _owner = manager.get_platform_handler_factories("telegram")[0]
            frontend = ingress._sibling_package(self.home.resolve())
            reply_bridge = importlib.import_module(f"{frontend.__package__}.reply_bridge")
            with patch.object(reply_bridge, "wire") as wire:
                factory(object(), object())
        self.assertEqual(wire.call_count, 1)
        self.assertEqual(len(wire.call_args.args), 4)
        self.assertIsNone(wire.call_args.args[3], "version-skewed helper must not receive approval capability")

    def test_sibling_loader_namespaces_same_machine_fake_homes(self):
        ingress_source = REPO / "plugins" / INGRESS_ID
        baseline_notifier = sys.modules.get("notifier")
        loader = _load_package(ingress_source, "phase2_loader_test")
        homes = [self.root / "alternate home one", self.root / "alternate home two"]
        modules = []
        for index, home in enumerate(homes):
            package = home / "plugins" / FRONTEND_ID
            package.parent.mkdir(parents=True)
            shutil.copytree(REPO / "desktop-plugins" / FRONTEND_ID, package,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
                frontend = loader._sibling_package(home.resolve())
                module = importlib.import_module(f"{frontend.__package__}.notifier")
                approval = importlib.import_module(f"{frontend.__package__}.approval_bridge")
            modules.append(module)
            self.assertEqual(approval.CAPABILITY, "desktop-command-approval-v1")
            self.assertEqual(approval.VERSION, module.VERSION)
            self.assertIn("approval_bridge.py", loader._HELPER_FILES)
            self.assertEqual(module.ROOT, home.resolve())
            self.assertIn(f"_{'hermes_telegram_bridge_'}", module.__name__)
            self.assertIs(sys.modules.get("notifier"), baseline_notifier)
        self.assertIsNot(modules[0], modules[1])
        self.assertNotEqual(modules[0].__name__, modules[1].__name__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
