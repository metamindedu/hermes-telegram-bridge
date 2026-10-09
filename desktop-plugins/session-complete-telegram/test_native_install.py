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
        manifest.write_text(text.replace("version: '1.5.0'", f"version: '{version}'"), encoding="utf-8")
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
            "v1": _copy_candidate(self.root / "candidate v1", "1.5.0"),
            "v2": _copy_candidate(self.root / "candidate v2", "1.5.1"),
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

    def test_native_fresh_update_and_rollback_keep_ids_config_and_plugin_data(self):
        frontend_path = "desktop-plugins/session-complete-telegram"
        ingress_path = "plugins/desktop-clarify-telegram"
        self._install_package("v1", frontend_path, force=False)
        self._install_package("v1", ingress_path, force=False)
        self._assert_preserved()
        self._assert_package_ids()

        self._install_package("v2", frontend_path, force=True)
        self._install_package("v2", ingress_path, force=True)
        self.assertIn("version: '1.5.1'", (self.home / "plugins" / FRONTEND_ID / "plugin.yaml").read_text(encoding="utf-8"))
        self._assert_preserved()
        self._assert_package_ids()

        # Rollback is the same native pinned install transaction using the
        # previous immutable ref; no bespoke deploy/rollback executor exists.
        self._install_package("v1", frontend_path, force=True)
        self._install_package("v1", ingress_path, force=True)
        self.assertIn("version: '1.5.0'", (self.home / "plugins" / FRONTEND_ID / "plugin.yaml").read_text(encoding="utf-8"))
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
        dispatch.assert_called_once_with({"action": "unknown-native-fixture"}, root=self.home.resolve())
        framed = _frame_value(stdout.getvalue())
        self.assertEqual(framed, {"ok": False, "reason": "unknown_action"})
        self.assertEqual(plugin.PLUGIN_ID, FRONTEND_ID)
        self.assertEqual(plugin._parse_request(encoded), {"action": "unknown-native-fixture"})
        with self.assertRaises(ValueError):
            plugin._parse_request("not-base64!")
        duplicate = base64.urlsafe_b64encode(b'{"action":"inspect","action":"send"}').rstrip(b"=").decode("ascii")
        with self.assertRaises(ValueError):
            plugin._parse_request(duplicate)
        oversized = base64.urlsafe_b64encode(b'{"action":"x","data":"' + b"x" * 17_000 + b'"}').rstrip(b"=").decode("ascii")
        with self.assertRaises(ValueError):
            plugin._parse_request(oversized)
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            plugin._write_frame({"ok": True, "data": "x" * 30_000})
        self.assertEqual(_frame_value(stdout.getvalue()), {"ok": False, "reason": "helper_result_too_large"})
        self._assert_preserved()

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
                ingress.register(ctx)

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
                named_ingress.register(named_ctx)

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
            modules.append(module)
            self.assertEqual(module.ROOT, home.resolve())
            self.assertIn(f"_{'hermes_telegram_bridge_'}", module.__name__)
            self.assertIs(sys.modules.get("notifier"), baseline_notifier)
        self.assertIsNot(modules[0], modules[1])
        self.assertNotEqual(modules[0].__name__, modules[1].__name__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
