"""Pure-stdlib negative and positive tests for the pre-open database fence."""
from __future__ import annotations

import ast
import contextvars
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import unittest
from types import SimpleNamespace

import newdb_test_isolation

FENCE = newdb_test_isolation.require_ready()
ROOT = FENCE.fixture_root
ARTIFACT = FENCE.artifact_root
OUTSIDE = Path(os.environ["HERMES_TEST_ISOLATION_OUTSIDE_ROOT"]).resolve(strict=True)
SCENARIO_RECEIPTS: list[dict] = []
CHILD_LAUNCH_RECEIPTS: list[dict] = []


def _record(label: str, before: dict, after: dict) -> None:
    SCENARIO_RECEIPTS.append({
        "scenario": label,
        "denied_target_delta": after["rejected_db_targets"] - before["rejected_db_targets"],
        "delegated_connect_delta": after["delegated_connect_calls"] - before["delegated_connect_calls"],
        "sqlite_audit_rejected_delta": after["sqlite_audit_rejected"] - before["sqlite_audit_rejected"],
        "constructor_delegate_delta": after["native_constructor_delegations"] - before["native_constructor_delegations"],
        "acquire_delegate_delta": after["native_acquire_delegations"] - before["native_acquire_delegations"],
        "epoch_call_delta": after["epoch_calls"] - before["epoch_calls"],
    })


class DatabaseIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.outside = OUTSIDE / "outside sentinel with spaces"
        cls.outside.mkdir()
        cls.sentinel = cls.outside / "do-not-open.sqlite3"
        cls.sentinel_bytes = b"synthetic-outside-sentinel-v1\n"
        cls.sentinel.write_bytes(cls.sentinel_bytes)

    def assert_sentinel_unchanged(self):
        self.assertEqual(self.sentinel.read_bytes(), self.sentinel_bytes)

    def assert_rejected_open(self, label, database, **kwargs):
        before = FENCE.snapshot()
        error = None
        connection = None
        try:
            connection = sqlite3.connect(database, **kwargs)
        except newdb_test_isolation.IsolationError as exc:
            error = exc
        finally:
            if connection is not None:
                connection.close()
        self.assertIsNotNone(error, f"unsafe target unexpectedly reached sqlite3.connect: {label}")
        after = FENCE.snapshot()
        self.assertEqual(after["delegated_connect_calls"], before["delegated_connect_calls"], label)
        self.assertEqual(after["sqlite_audit_authorized"], before["sqlite_audit_authorized"], label)
        self.assertEqual(after["native_constructor_delegations"], before["native_constructor_delegations"], label)
        self.assertEqual(after["native_acquire_delegations"], before["native_acquire_delegations"], label)
        self.assertEqual(after["epoch_calls"], before["epoch_calls"], label)
        self.assertGreater(after["rejected_db_targets"], before["rejected_db_targets"], label)
        self.assert_sentinel_unchanged()
        _record(label, before, after)

    def test_positive_sqlite_connection_reports_only_its_actual_fixture_backing_path(self):
        path = ROOT / "positive" / "actual-backed.sqlite3"
        path.parent.mkdir(parents=True)
        before = FENCE.snapshot()
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE fixture_probe (value TEXT NOT NULL)")
            connection.execute("INSERT INTO fixture_probe VALUES ('synthetic')")
            connection.commit()
            receipt = FENCE.assert_connection_paths(connection, path)
            self.assertTrue(receipt["main_under_fixture_root"])
            self.assertTrue(receipt["all_file_databases_under_fixture_root"])
            self.assertEqual(Path(receipt["main_path"]).resolve(), path.resolve())
        finally:
            connection.close()
        readonly = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            readonly_receipt = FENCE.assert_connection_paths(readonly, path)
            self.assertEqual(Path(readonly_receipt["main_path"]).resolve(), path.resolve())
        finally:
            readonly.close()
        after = FENCE.snapshot()
        self.assertEqual(after["delegated_connect_calls"] - before["delegated_connect_calls"], 2)
        self.assertEqual(after["connections_returned"] - before["connections_returned"], 2)
        SCENARIO_RECEIPTS.append({
            "scenario": "positive-created-sqlite-db-and-readonly-uri",
            "allowed_target_delta": 2,
            "delegated_connect_delta": 2,
            "actual_backing_path_under_fixture_root": True,
            "readonly_uri_under_fixture_root": True,
            "epoch_call_delta": after["epoch_calls"] - before["epoch_calls"],
        })

    def test_would_default_and_production_shaped_absolute_paths_fail_before_open(self):
        # These are synthetic path shapes under the larger task-owned artifact root,
        # not Claire's profile path and not a real database.
        would_default = self.outside / "inherited-default" / "state.db"
        production_shape = self.outside / "Users" / "ClaireFixture" / "AppData" / "Local" / "hermes" / "state.db"
        self.assert_rejected_open("would-default-outside", would_default)
        self.assert_rejected_open("synthetic-production-shaped-absolute", production_shape)

    def test_relative_parent_escape_and_chdir_outside_are_rejected_before_open(self):
        working = ROOT / "relative-work" / "nested"
        working.mkdir(parents=True)
        original_cwd = Path.cwd()
        try:
            os.chdir(working)
            good_before = FENCE.snapshot()
            good = sqlite3.connect("valid-relative.sqlite3")
            good.close()
            good_after = FENCE.snapshot()
            self.assertTrue((working / "valid-relative.sqlite3").is_file())
            SCENARIO_RECEIPTS.append({
                "scenario": "relative-path-after-chdir-inside-root",
                "allowed_target_delta": 1,
                "delegated_connect_delta": good_after["delegated_connect_calls"] - good_before["delegated_connect_calls"],
                "epoch_call_delta": good_after["epoch_calls"] - good_before["epoch_calls"],
            })
            self.assert_rejected_open("relative-parent-traversal", "../../../outside-sentinel/escape.sqlite3")
            os.chdir(self.outside)
            self.assert_rejected_open("chdir-outside-relative-target", "cwd-escape.sqlite3")
            self.assertFalse((self.outside / "cwd-escape.sqlite3").exists())
        finally:
            os.chdir(original_cwd)

    def test_context_inherited_outside_default_is_rejected_in_a_worker_thread(self):
        inherited = contextvars.ContextVar("synthetic_hermes_home_override", default=str(ROOT / "home"))
        token = inherited.set(str(self.outside / "state.db"))
        context = contextvars.copy_context()
        caught = []
        before = FENCE.snapshot()

        def attempt():
            try:
                sqlite3.connect(inherited.get())
            except BaseException as exc:
                caught.append(exc)

        worker = threading.Thread(target=lambda: context.run(attempt), name="fixture-db-context-negative")
        try:
            worker.start()
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive(), "guarded negative worker did not finish")
        finally:
            inherited.reset(token)
        self.assertEqual(len(caught), 1)
        self.assertIsInstance(caught[0], newdb_test_isolation.IsolationError)
        after = FENCE.snapshot()
        self.assertEqual(after["delegated_connect_calls"], before["delegated_connect_calls"])
        self.assertEqual(after["sqlite_audit_authorized"], before["sqlite_audit_authorized"])
        self.assertEqual(after["epoch_calls"], before["epoch_calls"])
        self.assert_sentinel_unchanged()
        _record("inherited-context-override-in-thread", before, after)

    def test_percent_encoded_readonly_sqlite_uri_is_rejected_before_open(self):
        uri = self.sentinel.as_uri() + "?mode=ro&cache=private"
        self.assertIn("%20", uri)
        self.assert_rejected_open("percent-encoded-readonly-uri", uri, uri=True)

    def test_uri_query_escape_and_unknown_vfs_are_rejected_by_the_validator(self):
        before = FENCE.snapshot()
        for uri in (
            self.sentinel.as_uri() + "?mode=rw&vfs=unexpected",
            self.sentinel.as_uri() + "?mode=ro&mode=rw",
        ):
            with self.assertRaises(newdb_test_isolation.IsolationError):
                FENCE.database_path(uri, uri=True)
        after = FENCE.snapshot()
        self.assertEqual(after["delegated_connect_calls"], before["delegated_connect_calls"])
        self.assertEqual(after["epoch_calls"], before["epoch_calls"])
        self.assert_sentinel_unchanged()
        _record("uri-query-unknown-or-duplicate-option", before, after)

    def test_symlink_or_owned_windows_junction_escape_is_detected_physically(self):
        link = ROOT / "physical-escape"
        target = self.outside
        made = False
        mechanism = "symlink"
        try:
            os.symlink(target, link, target_is_directory=True)
            made = True
        except (OSError, NotImplementedError):
            if os.name == "nt":
                mechanism = "windows-junction"
                result = subprocess.run(
                    ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
                    capture_output=True, timeout=15, check=False,
                )
                made = result.returncode == 0 and link.exists()
        if not made:
            self.fail("symlink unavailable and owned Windows junction creation failed; physical-escape coverage is uncovered")
        try:
            self.assert_rejected_open(f"physical-{mechanism}-escape", link / "link-target.sqlite3")
            self.assertFalse((target / "link-target.sqlite3").exists())
        finally:
            if link.exists() or os.path.lexists(link):
                # For Windows junctions os.rmdir removes the link itself, not its target.
                try:
                    os.rmdir(link)
                except OSError:
                    if link.is_symlink():
                        link.unlink()
                    else:
                        raise

    def test_saved_raw_entrypoint_cannot_return_an_unprotected_in_root_connection(self):
        path = ROOT / "unwrapped-in-root.sqlite3"
        before = FENCE.snapshot()
        connection = None
        try:
            with self.assertRaises(newdb_test_isolation.IsolationError):
                connection = FENCE.raw_connect_probe(path)
        finally:
            if connection is not None:
                connection.close()
        self.assertFalse(path.exists(), "an unwrapped connection must not bypass the ATTACH authorizer")
        after = FENCE.snapshot()
        self.assertGreater(after["sqlite_audit_rejected"], before["sqlite_audit_rejected"])
        self.assertEqual(after["delegated_connect_calls"], before["delegated_connect_calls"])
        self.assertEqual(after["epoch_calls"], before["epoch_calls"])
        _record("unwrapped-in-root-connection-denied-before-open", before, after)

    def test_unwrapped_sqlite_entrypoint_is_stopped_by_preopen_audit_hook(self):
        target = self.outside / "raw-entrypoint.sqlite3"
        before = FENCE.snapshot()
        connection = None
        try:
            with self.assertRaises(newdb_test_isolation.IsolationError):
                connection = FENCE.raw_connect_probe(target)
        finally:
            if connection is not None:
                connection.close()
        after = FENCE.snapshot()
        self.assertGreater(after["sqlite_audit_rejected"], before["sqlite_audit_rejected"])
        self.assertEqual(after["delegated_connect_calls"], before["delegated_connect_calls"])
        self.assertEqual(after["sqlite_audit_authorized"], before["sqlite_audit_authorized"])
        self.assertEqual(after["epoch_calls"], before["epoch_calls"])
        self.assertFalse(target.exists())
        self.assert_sentinel_unchanged()
        _record("raw-sqlite-entrypoint-audit-before-open", before, after)

    def test_attach_outside_root_is_denied_before_sqlite_attempt_and_sentinel_is_unchanged(self):
        connect_before = FENCE.snapshot()
        connection = sqlite3.connect(ROOT / "attach-main.sqlite3")
        connect_after = FENCE.snapshot()
        before = FENCE.snapshot()
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("ATTACH DATABASE ? AS outside_fixture", (str(self.sentinel),))
        finally:
            connection.close()
        after = FENCE.snapshot()
        self.assertEqual(connect_after["delegated_connect_calls"] - connect_before["delegated_connect_calls"], 1)
        self.assertGreater(after["attach_denied"], before["attach_denied"])
        self.assertEqual(after["attach_authorized"], before["attach_authorized"])
        self.assert_sentinel_unchanged()
        SCENARIO_RECEIPTS.append({
            "scenario": "attach-main-sqlite-inside-root",
            "allowed_target_delta": 1,
            "delegated_connect_delta": connect_after["delegated_connect_calls"] - connect_before["delegated_connect_calls"],
            "outside_attach_denied": True,
            "epoch_call_delta": after["epoch_calls"] - connect_before["epoch_calls"],
        })
        _record("attach-outside-root-denied", before, after)

    def test_native_default_constructor_acquire_cache_alias_and_epoch_wrappers_preflight_first(self):
        safe_path = ROOT / "native-cache-key.sqlite3"
        foreign_path = self.sentinel
        counters = {"constructor": 0, "acquire": 0, "store_open": 0, "initialize": 0}

        class FakeDB:
            def __init__(self, db_path=None, read_only=False):
                counters["constructor"] += 1
                self.db_path = Path(db_path)
                self.read_only = read_only
                self._conn = None

        default_path = [foreign_path]

        class FakeState:
            SessionDB = FakeDB
            @staticmethod
            def _default_db_path():
                return default_path[0]

        class FakeRegistry:
            _generations = {}
            @staticmethod
            def acquire(path=None):
                counters["acquire"] += 1
                return FakeDB(path)

        class FakePersistence:
            class SessionPersistenceMixin:
                def _open_session_db_for_active_scope(self, db_path=None):
                    counters["store_open"] += 1
                    return None

        class FakeAuthority:
            @staticmethod
            async def initialize_session_authority(runner, *, profile_id, instance_id, db=None, register=True):
                counters["initialize"] += 1
                return object()

        FENCE.install_native_fences(FakeState, FakeRegistry, FakePersistence, FakeAuthority)
        before = FENCE.snapshot()
        with self.assertRaises(newdb_test_isolation.IsolationError):
            FakeState.SessionDB()
        with self.assertRaises(newdb_test_isolation.IsolationError):
            FakeRegistry.acquire()
        default_path[0] = safe_path
        FakeRegistry._generations = {safe_path: SimpleNamespace(db=SimpleNamespace(db_path=foreign_path, _conn=None))}
        with self.assertRaises(newdb_test_isolation.IsolationError):
            FakeRegistry.acquire(safe_path)  # rejects the foreign cached alias before registry delegation
        FakeRegistry._generations = {}

        store = FakePersistence.SessionPersistenceMixin()
        store._db_handles = {safe_path: SimpleNamespace(db_path=foreign_path, _conn=None)}
        with self.assertRaises(newdb_test_isolation.IsolationError):
            store._open_session_db_for_active_scope(safe_path)
        fake_foreign_db = SimpleNamespace(db_path=foreign_path, _conn=None)
        with self.assertRaises(newdb_test_isolation.IsolationError):
            import asyncio
            asyncio.run(FakeAuthority.initialize_session_authority(
                SimpleNamespace(_session_db=fake_foreign_db), profile_id="fixture", instance_id="fixture",
                db=fake_foreign_db,
            ))

        after = FENCE.snapshot()
        self.assertEqual(counters, {"constructor": 0, "acquire": 0, "store_open": 0, "initialize": 0})
        self.assertEqual(after["native_constructor_delegations"], before["native_constructor_delegations"])
        self.assertEqual(after["native_acquire_delegations"], before["native_acquire_delegations"])
        self.assertEqual(after["session_store_open_delegations"], before["session_store_open_delegations"])
        self.assertEqual(after["epoch_calls"], before["epoch_calls"])
        self.assert_sentinel_unchanged()
        _record("default-acquire-constructor-cache-alias-and-pre-epoch", before, after)

    def test_canonical_integration_has_a_preimport_guard_without_executing_the_module(self):
        source_path = Path(__file__).with_name("test_canonical_integration.py")
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))
        compile(tree, str(source_path), "exec")  # syntax-only; never imports or executes the integration module
        imports = [
            (index, statement) for index, statement in enumerate(tree.body)
            if isinstance(statement, (ast.Import, ast.ImportFrom))
            and not (isinstance(statement, ast.ImportFrom) and statement.module == "__future__")
        ]
        self.assertGreaterEqual(len(imports), 2)
        self.assertIsInstance(imports[0][1], ast.Import)
        self.assertEqual([alias.name for alias in imports[0][1].names], ["sys"])
        guard_index = next((index for index, statement in enumerate(tree.body)
                            if isinstance(statement, ast.If) and "_DB_ISOLATION" in ast.dump(statement.test)), None)
        self.assertIsNotNone(guard_index, "direct unittest import must have a fail-closed guard branch")
        first_integration_import = next(index for index, statement in imports
                                        if isinstance(statement, ast.Import) and
                                        any(alias.name == "argparse" for alias in statement.names))
        self.assertLess(guard_index, first_integration_import)
        self.assert_sentinel_unchanged()
        SCENARIO_RECEIPTS.append({
            "scenario": "direct-integration-import-fails-closed-before-hermes-imports",
            "verification": "AST parse/compile only; module not executed",
            "guard_before_integration_imports": True,
            "epoch_call_delta": FENCE.snapshot()["epoch_calls"],
        })


class ChildProcessFenceTests(unittest.TestCase):
    """Pure bootstrap regressions plus one synthetic, DB-free PM stage child."""

    def setUp(self):
        self.work = ROOT / f"pm-child-tests-{os.getpid()}-{id(self):x}"
        self.work.mkdir()
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)

    def _owned_pm_environment(self):
        venv = self.work / "pm-venv"
        executable_dir = venv / ("Scripts" if os.name == "nt" else "bin")
        executable_dir.mkdir(parents=True)
        executable = executable_dir / ("python.exe" if os.name == "nt" else "python")
        executable.write_bytes(b"synthetic executable; it must be rewritten before launch\n")
        (venv / "pyvenv.cfg").write_text("home = fixture-only\n", encoding="utf-8")
        if os.name == "nt":
            site = venv / "Lib" / "site-packages"
        else:
            site = venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
        site.mkdir(parents=True)
        environment = dict(os.environ)
        environment.update({
            "HERMES_TEST_ISOLATION_ROOT": str(ROOT),
            "HERMES_TEST_ISOLATION_TOKEN": FENCE.token,
            "HERMES_TEST_ISOLATION_GUARD_TESTS_PASSED": "1",
        })
        return executable, site, environment

    def _bootstrap_helper(self, bootstrap):
        core_root, _core_ref, _generation = bootstrap._qualified_core_checkout()
        return SimpleNamespace(fixture_root=FENCE.fixture_root, artifact_root=FENCE.artifact_root,
                               token=FENCE.token, core_root=core_root)

    def test_unknown_python_and_unapproved_scripts_never_reach_popen(self):
        import importlib

        bootstrap = importlib.import_module("guarded_unittest")
        policy_helper = self._bootstrap_helper(bootstrap)
        executable, _site, environment = self._owned_pm_environment()
        outside_python = OUTSIDE / "python.exe"
        outside_python.write_bytes(b"synthetic outside executable\n")
        unknown_python = ROOT / "unknown-python" / "python.exe"
        unknown_python.parent.mkdir()
        unknown_python.write_bytes(b"synthetic unqualified executable\n")
        arbitrary_script = self.work / "not-the-pm-worker.py"
        arbitrary_script.write_text("raise SystemExit(0)\n", encoding="utf-8")
        calls = []

        class PopenRecorder:
            def __init__(self, args, *pargs, **kwargs):
                calls.append((args, pargs, kwargs))

        import subprocess
        original_popen = subprocess.Popen
        try:
            subprocess.Popen = PopenRecorder
            bootstrap._install_child_process_fence(policy_helper)
            rejected = (
                [str(outside_python), "-I", "-B", "-c", "print('outside')"],
                [str(unknown_python), "-I", "-B", "-c", "print('unknown')"],
                [str(executable), "-I", "-B", "-c", "import os"],
                [str(executable), "-I", "-B", str(arbitrary_script)],
            )
            for command in rejected:
                with self.subTest(command=command):
                    with self.assertRaises(RuntimeError):
                        subprocess.Popen(command, env=environment)
                    self.assertEqual(len(calls), 0, "rejected Python command reached the original Popen")

            bad_marker = dict(environment, HERMES_TEST_ISOLATION_TOKEN="wrong-marker")
            with self.assertRaises(RuntimeError):
                subprocess.Popen([sys.executable, "-c", "pass"], env=bad_marker)
            bad_scope = dict(environment, HERMES_TEST_ISOLATION_ROOT=str(OUTSIDE))
            with self.assertRaises(RuntimeError):
                subprocess.Popen([sys.executable, "-c", "pass"], env=bad_scope)
            incomplete = dict(environment, HERMES_TEST_ISOLATION_GUARD_TESTS_PASSED="0")
            with self.assertRaises(RuntimeError):
                subprocess.Popen([sys.executable, "-c", "pass"], env=incomplete)
            self.assertEqual(len(calls), 0, "invalid marker/scope/readiness reached the original Popen")
        finally:
            subprocess.Popen = original_popen

    def test_publication_node_operation_is_exact_and_wrong_flags_or_scope_never_launch(self):
        import importlib
        import subprocess

        bootstrap = importlib.import_module('guarded_unittest')
        policy_helper = self._bootstrap_helper(bootstrap)
        _executable, _site, environment = self._owned_pm_environment()
        package = Path(__file__).resolve().parent
        command = ['node', '--test-isolation=none', '--test',
                   '--test-name-pattern=approval-publication-epoch-interleaving',
                   '--test-reporter=tap', '--test-reporter-destination=stderr',
                   'approval_concurrency.test.mjs']
        calls = []

        class PopenRecorder:
            def __init__(self, args, *pargs, **kwargs):
                calls.append((args, kwargs))

        original_popen = subprocess.Popen
        try:
            subprocess.Popen = PopenRecorder
            bootstrap._install_child_process_fence(policy_helper)
            subprocess.Popen(command, cwd=str(package), env=environment)
            self.assertEqual(len(calls), 1, 'the one qualified fixture operation must reach Popen')
            invalid = [
                (command[:-1] + ['canonical.test.mjs'], package),
                ([*command[:3], '--test-name-pattern=.*', *command[4:]], package),
                (command + ['--eval', 'process.exit(0)'], package),
                ([*command[:5], '--test-reporter-destination=stdout', command[-1]], package),
                (command, self.work),
            ]
            for args, cwd in invalid:
                with self.subTest(operation=args[1:]):
                    with self.assertRaises(RuntimeError):
                        subprocess.Popen(args, cwd=str(cwd), env=environment)
                    self.assertEqual(len(calls), 1, 'an unqualified Node command reached Popen')
        finally:
            subprocess.Popen = original_popen

    def test_owned_pm_stage_and_fixed_worker_are_rewritten_without_changing_main_child_path(self):
        import importlib
        import subprocess

        bootstrap = importlib.import_module("guarded_unittest")
        policy_helper = self._bootstrap_helper(bootstrap)
        executable, site, environment = self._owned_pm_environment()
        core_root = Path(os.environ["HERMES_CORE_ROOT"]).resolve(strict=True)
        worker = core_root / "pm" / "worker.py"
        self.assertTrue(worker.is_file())
        calls = []

        class PopenRecorder:
            def __init__(self, args, *pargs, **kwargs):
                calls.append((args, kwargs))

        original_popen = subprocess.Popen
        try:
            subprocess.Popen = PopenRecorder
            bootstrap._install_child_process_fence(policy_helper)
            stage = [str(executable), "-I", "-B", "-c",
                     "import packaging, tomli_w, truststore; from ruamel.yaml import YAML"]
            subprocess.Popen(stage, env=environment)
            worker_command = [str(executable), "-I", "-B", str(worker)]
            subprocess.Popen(worker_command, env=environment)
            subprocess.Popen([sys.executable, "-c", "print('normal-child')"], env=environment)
        finally:
            subprocess.Popen = original_popen

        self.assertEqual(len(calls), 3)
        stage_args, worker_args, normal_args = [call[0] for call in calls]
        self.assertEqual(stage_args[0], sys.executable)
        self.assertEqual(stage_args[1:3], ["-I", "-S"])
        self.assertIn("--pm-stage-check", stage_args)
        self.assertIn(str(site.resolve()), stage_args)
        self.assertEqual(worker_args[0], sys.executable)
        self.assertEqual(worker_args[1:3], ["-I", "-S"])
        self.assertIn("--pm-worker", worker_args)
        self.assertIn(str(worker.resolve()), worker_args)
        self.assertNotIn("--child-code", stage_args)
        self.assertNotIn("--child-code", worker_args)
        self.assertIn("--child-code", normal_args)
        self.assertNotIn("--pm-worker", normal_args)

    def test_pm_stage_child_installs_stdlib_db_and_import_fences_before_validation_imports(self):
        import json
        import subprocess
        import importlib

        bootstrap = importlib.import_module("guarded_unittest")
        policy_helper = self._bootstrap_helper(bootstrap)
        executable, site, environment = self._owned_pm_environment()
        (site / "packaging").mkdir()
        (site / "packaging" / "__init__.py").write_text("", encoding="utf-8")
        (site / "tomli_w.py").write_text("", encoding="utf-8")
        (site / "truststore.py").write_text("", encoding="utf-8")
        (site / "ruamel").mkdir()
        (site / "ruamel" / "__init__.py").write_text("", encoding="utf-8")
        (site / "ruamel" / "yaml.py").write_text("class YAML: pass\n", encoding="utf-8")

        original_popen = subprocess.Popen
        guarded_popen = None
        try:
            bootstrap._install_child_process_fence(FENCE)
            guarded_popen = subprocess.Popen
            with guarded_popen(
                [str(executable), "-I", "-B", "-c",
                 "import packaging, tomli_w, truststore; from ruamel.yaml import YAML"],
                env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            ) as process:
                stdout, stderr = process.communicate(timeout=40)
                self.assertEqual(process.returncode, 0, stdout + stderr)
        finally:
            if guarded_popen is not None:
                self.assertIs(guarded_popen.__bases__[0], original_popen)
            subprocess.Popen = original_popen

        self.assertEqual(stdout, "", "guard receipt must not corrupt a worker's stdout protocol")
        self.assertEqual(stderr, "")
        metadata = Path(os.environ["HERMES_TEST_ISOLATION_META_ROOT"])
        matching = [json.loads(path.read_text(encoding="utf-8")) for path in metadata.glob("child-*.json")
                    if path.is_file()]
        receipt = next((item for item in matching if item.get("child_kind") == "pm-stage-check"), None)
        self.assertIsNotNone(receipt, "PM bootstrap must leave its guard receipt under private metadata")
        self.assertEqual(receipt["outcome"], "PASS")
        self.assertTrue(receipt["db_guard_installed_before_child_imports"])
        self.assertTrue(receipt["pm_import_guard_installed_before_child_imports"])
        self.assertTrue(receipt["pm_import_guard_self_test"])
        self.assertEqual(receipt["counters"]["epoch_calls"], 0)
        self.assertEqual(receipt["blocked_core_modules_loaded"], [])

    def test_wrong_marker_ref_and_fixture_scope_still_fail_closed(self):
        import importlib
        from unittest.mock import patch

        bootstrap = importlib.import_module("guarded_unittest")
        with self.assertRaises(newdb_test_isolation.IsolationError):
            newdb_test_isolation.DatabaseFence._verify_marked_root(ROOT, ARTIFACT, "not-the-run-token")

        with patch.dict(os.environ, {"HERMES_TEST_ISOLATION_ROOT": str(ARTIFACT)}):
            with self.assertRaises(RuntimeError):
                bootstrap._existing_roots()
        with patch.dict(os.environ, {"HERMES_CORE_REF": "0" * 40}):
            with self.assertRaises(RuntimeError):
                bootstrap._qualify_core(FENCE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
