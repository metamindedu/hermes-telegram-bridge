"""Repository-owned bootstrap for isolated Hermes bridge unittest processes.

Invoke with ``python -I -S guarded_unittest.py``.  The isolated interpreter skips
site startup and editable-install hooks; this script installs the stdlib-only DB
fence before adding the one qualified core checkout or loading any test module.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.abc
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

SUPPORT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SUPPORT_DIR.parents[1]
PACKAGE_DIR = REPO_ROOT / "desktop-plugins" / "session-complete-telegram"
MARKER = ".hermes-telegram-bridge-owned-v1"
LOCAL_SCRATCH = Path(
    "C:/Users/ovpow/AppData/Local/hermes/profiles/claire/cache/scratch"
)
CORE_MODULES = (
    "test_notifier", "test_reply_bridge", "test_followup_bridge", "test_approval_bridge",
    "test_canonical_backend", "test_db_isolation", "test_canonical_integration",
    "test_native_install", "test_registration", "test_feature_gates", "test_light_corrections",
    "test_final_boundaries", "test_settings_control", "test_review_revocation",
    "test_review_notification_boundaries", "test_producer_epoch", "test_completion_recovery",
    "test_recovery_review",
)
PM_STAGE_CHECK_CODE = "import packaging, tomli_w, truststore; from ruamel.yaml import YAML"
PM_DENIED_IMPORT_PREFIXES = (
    "hermes_state", "gateway.session", "gateway.run", "run_agent", "models", "providers",
    "agent.model", "agent.provider", "agent.session", "agent.runtime_session",
    "hermes_cli.model", "hermes_cli.provider", "hermes_cli.session", "hermes_cli.gateway",
)


def _parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("isolation", "native-db", "canonical", "suite"), default="isolation")
    parser.add_argument("--base", help="existing, explicitly owned test-artifact parent directory")
    parser.add_argument("--child-code", help=argparse.SUPPRESS)
    pm_child = parser.add_mutually_exclusive_group()
    pm_child.add_argument("--pm-stage-check", action="store_true", help=argparse.SUPPRESS)
    pm_child.add_argument("--pm-worker", help=argparse.SUPPRESS)
    parser.add_argument("--pm-site-packages", help=argparse.SUPPRESS)
    parser.add_argument("--verbosity", type=int, choices=(1, 2), default=2)
    parser.add_argument("-v", "--verbose", dest="verbosity", action="store_const", const=2,
                        default=argparse.SUPPRESS)
    parser.add_argument("modules", nargs="*")
    return parser.parse_args(argv)


def _allowed_base(raw: str) -> Path:
    if not raw:
        raise RuntimeError("HERMES_TEST_ISOLATION_BASE/--base is required; refusing implicit temp roots")
    candidate = Path(raw)
    if not candidate.is_absolute() or not candidate.is_dir():
        raise RuntimeError("explicit test-artifact base must already exist and be an absolute directory")
    base = candidate.resolve(strict=True)
    workspace = os.environ.get("GITHUB_WORKSPACE")
    if workspace:
        expected = (Path(workspace) / ".ci-scratch").resolve(strict=True)
    else:
        expected = LOCAL_SCRATCH.resolve(strict=True)
    if base != expected:
        raise RuntimeError("test-artifact base is outside the explicitly permitted scratch root")
    if base == Path(base.anchor).resolve(strict=True):
        raise RuntimeError("a filesystem root cannot be used as a test-artifact base")
    return base


def _write_marker(root: Path, token: str) -> None:
    with (root / MARKER).open("x", encoding="ascii", newline="") as marker:
        marker.write(token)
        marker.flush()
        os.fsync(marker.fileno())


def _create_roots(base: Path) -> tuple[Path, Path, Path, Path, str, str]:
    run_id = uuid.uuid4().hex
    # Keep the native PM generation path below Windows' legacy path ceiling.
    # mkdir is exclusive, so a short-name collision fails rather than reusing a root.
    artifact = base / f"telegram-db-{run_id[:8]}"
    artifact.mkdir(mode=0o700)
    fixture = artifact / "fixture"
    metadata = artifact / "metadata"
    outside = artifact / "outside-sentinel"
    fixture.mkdir(mode=0o700)
    metadata.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    (metadata / "logs").mkdir(mode=0o700)
    token = uuid.uuid4().hex + uuid.uuid4().hex
    _write_marker(artifact, token)
    _write_marker(fixture, token)
    return artifact.resolve(strict=True), fixture.resolve(strict=True), metadata.resolve(strict=True), \
        outside.resolve(strict=True), token, run_id


def _configure_fake_environment(fixture: Path, artifact: Path, metadata: Path, outside: Path,
                                token: str, *, create: bool) -> None:
    paths = {
        "home": fixture / "home",
        "user_home": fixture / "users" / "home",
        "user_profile": fixture / "users" / "profile",
        "local_appdata": fixture / "appdata" / "local",
        "roaming_appdata": fixture / "appdata" / "roaming",
        "xdg_config": fixture / "xdg" / "config",
        "xdg_data": fixture / "xdg" / "data",
        "xdg_cache": fixture / "xdg" / "cache",
        "tmp": fixture / "tmp",
    }
    if create:
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        for relative in ("profiles/claire", "sessions", "plugins", "plugin-data"):
            (paths["home"] / relative).mkdir(parents=True, exist_ok=True)
    else:
        for path in paths.values():
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(fixture):
                raise RuntimeError("child process home/config/temp path escaped the owned fixture root")

    # Never echo the inherited environment; remove service credentials before tests
    # and Python children inherit only the sanitized, fixture-owned values.
    for key in tuple(os.environ):
        upper = key.upper()
        if upper.startswith(("TELEGRAM_", "OPENAI_", "ANTHROPIC_", "GOOGLE_", "SUPABASE_",
                             "CLOUDFLARE_", "PORTONE_", "AWS_", "AZURE_", "MODAL_")):
            os.environ.pop(key, None)
        elif upper.endswith(("_API_KEY", "_ACCESS_TOKEN", "_CLIENT_SECRET", "_PASSWORD")):
            os.environ.pop(key, None)

    env_values = {
        "HERMES_HOME": paths["home"],
        "HOME": paths["user_home"],
        "USERPROFILE": paths["user_profile"],
        "LOCALAPPDATA": paths["local_appdata"],
        "APPDATA": paths["roaming_appdata"],
        "XDG_CONFIG_HOME": paths["xdg_config"],
        "XDG_DATA_HOME": paths["xdg_data"],
        "XDG_CACHE_HOME": paths["xdg_cache"],
        "HERMES_TEST_TMPDIR": paths["tmp"],
        "TMPDIR": paths["tmp"],
        "TMP": paths["tmp"],
        "TEMP": paths["tmp"],
        "RUNNER_TEMP": paths["tmp"],
        "HERMES_TEST_ISOLATION_ROOT": fixture,
        "HERMES_TEST_ISOLATION_ARTIFACT_ROOT": artifact,
        "HERMES_TEST_ISOLATION_META_ROOT": metadata,
        "HERMES_TEST_ISOLATION_OUTSIDE_ROOT": outside,
        "HERMES_TEST_ISOLATION_TOKEN": token,
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    path_keys = ("HERMES_HOME", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
                 "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "HERMES_TEST_TMPDIR",
                 "TMPDIR", "TMP", "TEMP", "RUNNER_TEMP")
    if not create:
        for key in path_keys:
            inherited = os.environ.get(key)
            if not inherited:
                continue
            resolved = Path(inherited).resolve(strict=False)
            if not resolved.is_relative_to(fixture):
                raise RuntimeError("guarded child home/config/temp override escaped the fixture root")
            env_values[key] = inherited
    for key, value in env_values.items():
        os.environ[key] = str(value)
    os.environ["HERMES_TEST_ISOLATION_GUARD_TESTS_PASSED"] = "0"
    if os.name == "nt":
        drive, tail = os.path.splitdrive(str(fixture))
        os.environ["HOMEDRIVE"] = drive
        os.environ["HOMEPATH"] = tail

    # ``tempfile`` consults this cache after imports; set it explicitly so even
    # tests that bypass test_support remain inside the same marked fixture root.
    tempfile.tempdir = str(paths["tmp"])


def _load_helper():
    sys.path.insert(0, str(PACKAGE_DIR))
    sys.path.insert(0, str(SUPPORT_DIR))
    import newdb_test_isolation
    return newdb_test_isolation


def _require_fresh_core_import_state(helper) -> None:
    helper_names = ("agent", "gateway", "hermes_state", "hermes_constants", "hermes_cli",
                    "hermes_state_registry", "hermes_state_runtime", "tools", "acp_adapter")
    loaded = [name for name in sys.modules if any(
        name == prefix or name.startswith(prefix + ".") for prefix in helper_names
    )]
    raw_root = os.environ.get("HERMES_CORE_ROOT")
    if raw_root:
        core_root = Path(raw_root).resolve(strict=True)
        for name, module in tuple(sys.modules.items()):
            origin = getattr(module, "__file__", None)
            if origin:
                try:
                    if Path(origin).resolve(strict=True).is_relative_to(core_root):
                        loaded.append(name)
                except (OSError, RuntimeError):
                    continue
    if loaded:
        raise RuntimeError("Hermes modules were loaded before the test DB isolation bootstrap")


def _existing_roots() -> tuple[Path, Path, Path, Path, str]:
    required = (
        "HERMES_TEST_ISOLATION_ROOT", "HERMES_TEST_ISOLATION_ARTIFACT_ROOT",
        "HERMES_TEST_ISOLATION_META_ROOT", "HERMES_TEST_ISOLATION_OUTSIDE_ROOT",
        "HERMES_TEST_ISOLATION_TOKEN",
    )
    if any(not os.environ.get(key) for key in required):
        raise RuntimeError("guarded child is missing the parent-owned fixture receipt")
    fixture = Path(os.environ[required[0]]).resolve(strict=True)
    artifact = Path(os.environ[required[1]]).resolve(strict=True)
    metadata = Path(os.environ[required[2]]).resolve(strict=True)
    outside = Path(os.environ[required[3]]).resolve(strict=True)
    token = os.environ[required[4]]
    if fixture.parent != artifact or metadata.parent != artifact or outside.parent != artifact:
        raise RuntimeError("fixture, metadata, and outside-sentinel roots are ambiguous")
    if os.environ.get("HERMES_TEST_ISOLATION_GUARD_TESTS_PASSED") != "1":
        raise RuntimeError("guarded child cannot start before the parent pure-stdlib isolation tests pass")
    return fixture, artifact, metadata, outside, token


def _source_head() -> str:
    result = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                            check=True, capture_output=True, text=True, timeout=20)
    value = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise RuntimeError("bridge source HEAD could not be qualified")
    return value


def _source_hashes() -> dict[str, str]:
    result = {}
    for name in ("guarded_unittest.py", "newdb_test_isolation.py", "test_db_isolation.py",
                 "test_canonical_integration.py", "test_support.py"):
        path = (SUPPORT_DIR if name in {"guarded_unittest.py", "newdb_test_isolation.py"} else PACKAGE_DIR) / name
        if path.is_file():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _write_receipt(helper, metadata: Path, *, phase: str, outcome: str,
                   tests_run: int = 0, source_head: str | None = None,
                   scenario_receipts: list | None = None, native_db_receipt: dict | None = None,
                   error_kind: str | None = None) -> Path:
    receipt = {
        "schema": "hermes-telegram-bridge-db-isolation-v1",
        "phase": phase,
        "outcome": outcome,
        "tests_run": tests_run,
        "source_head": source_head,
        "source_sha256": _source_hashes(),
        "guard_installed_before_core_imports": True,
        "core_modules_loaded_before_guard": 0,
        "core_origin_count": helper.loaded_core_origin_count(),
        "counters": helper.snapshot(),
        "scenarios": scenario_receipts or [],
        "native_session_db": native_db_receipt,
        "error_kind": error_kind,
        "metadata_root": str(metadata),
        "log_root": str(metadata / "logs"),
    }
    path = metadata / f"receipt-{os.getpid()}-{uuid.uuid4().hex}.json"
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _run_modules(module_names: list[str], verbosity: int):
    import unittest
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in module_names:
        suite.addTests(loader.loadTestsFromName(name))
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return result


def _run_test_case(module, case_name: str, verbosity: int):
    import unittest
    case = getattr(module, case_name, None)
    if not isinstance(case, type) or not issubclass(case, unittest.TestCase):
        raise RuntimeError("focused isolation test case is missing")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(case)
    return unittest.TextTestRunner(verbosity=verbosity).run(suite)


def _run_popen_recorder_qualification(helper, verbosity: int):
    """Exercise descendant-launch denials without ever creating a real child."""
    import io
    import subprocess as subprocess_module
    import unittest
    from types import SimpleNamespace

    fixture = Path(helper.fixture_root).resolve(strict=True)
    artifact = Path(helper.artifact_root).resolve(strict=True)
    env = dict(os.environ)
    env["HERMES_TEST_ISOLATION_ROOT"] = str(fixture)
    env["HERMES_TEST_ISOLATION_ARTIFACT_ROOT"] = str(artifact)
    env["HERMES_TEST_ISOLATION_TOKEN"] = helper.token
    env["HERMES_TEST_ISOLATION_GUARD_TESTS_PASSED"] = "1"
    configured_core = os.environ.get("HERMES_CORE_ROOT")
    policy_core = Path(configured_core).resolve(strict=True) if configured_core else SUPPORT_DIR
    policy_helper = SimpleNamespace(fixture_root=fixture, artifact_root=artifact,
                                    token=helper.token, core_root=policy_core)

    class PopenRecorderTests(unittest.TestCase):
        def rejected(self, attempts, *, token=None):
            calls = []

            class PopenRecorder:
                def __init__(self, args, *pargs, **kwargs):
                    calls.append((args, pargs, kwargs))

            original = subprocess_module.Popen
            subprocess_module.Popen = PopenRecorder
            try:
                _install_child_process_fence(SimpleNamespace(
                    fixture_root=fixture, artifact_root=artifact,
                    token=policy_helper.token if token is None else token,
                    core_root=policy_core))
                for index, (args, kwargs) in enumerate(attempts):
                    with self.subTest(index=index):
                        with self.assertRaises(RuntimeError):
                            subprocess_module.Popen(args, **kwargs)
                        self.assertEqual(len(calls), 0, "denied launch reached the Popen recorder")
            finally:
                subprocess_module.Popen = original

        def test_fake_argv0_with_python_executable_override_is_denied(self):
            self.rejected([(["fake-native-tool.exe", "-c", "pass"],
                            {"executable":sys.executable, "env":env})])

        def test_pythonw_and_unapproved_interpreter_aliases_are_denied(self):
            self.rejected([
                (["pythonw.exe", "-c", "pass"], {"env":env}),
                (["py.exe", "-c", "pass"], {"env":env}),
                (["C:/unapproved/python3.14.exe", "-c", "pass"], {"env":env}),
            ])

        def test_positional_process_controls_never_reach_original_popen(self):
            calls = []

            class PopenRecorder:
                def __init__(self, args, *pargs, **kwargs):
                    calls.append(True)

            original = subprocess_module.Popen
            subprocess_module.Popen = PopenRecorder
            try:
                _install_child_process_fence(policy_helper)
                variants = (
                    (-1, sys.executable),
                    (-1, None, None, None, None, None, True),
                    (-1, None, None, None, None, None, True, True, str(artifact)),
                    (-1, None, None, None, None, None, True, False, str(fixture), {"PATH":"untrusted"}),
                )
                for index, pargs in enumerate(variants):
                    with self.subTest(index=index):
                        with self.assertRaises(RuntimeError):
                            subprocess_module.Popen([sys.executable,"-c","pass"], *pargs, env=env)
                        self.assertEqual(len(calls),0,"positional controls reached original Popen")
            finally:
                subprocess_module.Popen = original

        def test_unverified_fixture_uv_and_interpreter_are_denied(self):
            import shutil
            fake_uv = fixture / ("unverified-uv/uv.exe" if os.name == "nt" else "unverified-uv/uv")
            fake_uv.parent.mkdir(exist_ok=True)
            fake_uv.write_bytes(b"not a qualified uv binary")
            fake_python = fixture / "unverified-python-target"
            fake_python.write_bytes(b"not a qualified interpreter")
            destination = fixture / "uv-target"
            cache = fixture / "uv-cache-test"
            cache.mkdir(exist_ok=True)
            good_env = dict(env,UV_PYTHON=sys.executable,UV_PROJECT_ENVIRONMENT=str(destination),
                UV_CACHE_DIR=str(cache),UV_PYTHON_DOWNLOADS="never")
            self.rejected([
                ([str(fake_uv),"--version"],{"cwd":str(fixture),"env":good_env}),
                ([str(fake_uv),"venv","--relocatable","--no-project","--no-config","--python",
                  sys.executable,str(destination)],{"cwd":str(fixture),"env":good_env}),
            ])
            selected_uv = shutil.which("uv")
            if selected_uv:
                bad_env = dict(good_env,UV_PYTHON=str(fake_python))
                self.rejected([
                    ([selected_uv,"venv","--relocatable","--no-project","--no-config","--python",
                      str(fake_python),str(destination)],{"cwd":str(fixture),"env":bad_env}),
                ])

        def test_selected_uv_exact_operation_has_a_recorder_positive(self):
            import shutil
            selected = shutil.which("uv")
            if not selected:
                self.skipTest("uv is not available for this explicit tool-identity qualification")
            cache = fixture / "qualified-uv-cache"
            cache.mkdir(exist_ok=True)
            destination = fixture / "qualified-uv-target"
            qualified_env = dict(env,UV_PYTHON=sys.executable,UV_PROJECT_ENVIRONMENT=str(destination),
                UV_CACHE_DIR=str(cache),UV_PYTHON_DOWNLOADS="never")
            calls = []

            class PopenRecorder:
                def __init__(self, args, *pargs, **kwargs):
                    calls.append(tuple(args))

            original = subprocess_module.Popen
            subprocess_module.Popen = PopenRecorder
            try:
                _install_child_process_fence(policy_helper)
                subprocess_module.Popen([selected,"venv","--relocatable","--no-project","--no-config",
                    "--python",sys.executable,str(destination)],cwd=str(fixture),env=qualified_env)
                self.assertEqual(len(calls),1)
                self.assertEqual(calls[0][0],str(Path(selected).resolve(strict=True)))
            finally:
                subprocess_module.Popen = original

        def test_relative_execution_operands_are_denied_before_child_cwd_reinterpretation(self):
            import shutil
            previous_cwd = Path.cwd()
            shadow_cwd = fixture / "shadow-cwd"
            shadow_runner = shadow_cwd / "tests" / "support" / "guarded_unittest.py"
            shadow_runner.parent.mkdir(parents=True,exist_ok=True)
            shadow_runner.write_bytes(b"unverified shadow runner; recorder must never execute it")
            try:
                os.chdir(REPO_ROOT)
                self.rejected([
                    ([sys.executable,"-I","-S","tests/support/guarded_unittest.py","--child-code","pass"],
                        {"cwd":str(shadow_cwd),"env":env}),
                ])
                selected_uv = shutil.which("uv")
                if selected_uv:
                    relative_python = os.path.relpath(sys.executable,REPO_ROOT)
                    depth = sum(part == ".." for part in Path(relative_python).parts)+1
                    deep_cwd = fixture.joinpath(*(["d"]*depth))
                    deep_cwd.mkdir(parents=True,exist_ok=True)
                    shadow_python = (deep_cwd / relative_python).resolve(strict=False)
                    self.assertTrue(shadow_python.is_relative_to(fixture))
                    shadow_python.parent.mkdir(parents=True,exist_ok=True)
                    shadow_python.write_bytes(b"unverified shadow interpreter; recorder must never execute it")
                    destination = fixture / "absolute-target"
                    cache = fixture / "absolute-cache"
                    cache.mkdir(exist_ok=True)
                    good_env = dict(env,UV_PYTHON=sys.executable,UV_PROJECT_ENVIRONMENT=str(destination),
                        UV_CACHE_DIR=str(cache),UV_PYTHON_DOWNLOADS="never")
                    command = [selected_uv,"venv","--relocatable","--no-project","--no-config",
                        "--python",sys.executable,str(destination)]
                    relative_arg = list(command)
                    relative_arg[6] = relative_python
                    relative_dest = list(command)
                    relative_dest[7] = os.path.relpath(destination,REPO_ROOT)
                    attempts = [(relative_arg,{"cwd":str(deep_cwd),"env":good_env}),
                        (relative_dest,{"cwd":str(deep_cwd),"env":good_env})]
                    for key,value in (("UV_PYTHON",sys.executable),("UV_PROJECT_ENVIRONMENT",destination),
                            ("UV_CACHE_DIR",cache)):
                        attempts.append((command,{"cwd":str(deep_cwd),
                            "env":dict(good_env,**{key:os.path.relpath(value,REPO_ROOT)})}))
                    self.rejected(attempts)
            finally:
                os.chdir(previous_cwd)

        def test_guarded_python_is_pinned_to_validated_absolute_executable(self):
            calls = []

            class PopenRecorder:
                def __init__(self, args, *pargs, **kwargs):
                    calls.append(tuple(args))

            original = subprocess_module.Popen
            subprocess_module.Popen = PopenRecorder
            try:
                _install_child_process_fence(policy_helper)
                relative = os.path.relpath(sys.executable,Path.cwd())
                subprocess_module.Popen([relative,"-I","-S",str(Path(__file__).resolve()),
                    "--child-code","pass"],cwd=str(fixture),env=env)
                self.assertEqual(len(calls),1)
                self.assertEqual(calls[0][0],str(Path(sys.executable).resolve(strict=True)))
            finally:
                subprocess_module.Popen = original

        def test_shell_and_indirect_command_wrappers_are_denied(self):
            self.rejected([
                ("python.exe -c pass", {"shell":True, "env":env}),
                (["cmd.exe", "/c", "python.exe -c pass"], {"env":env}),
                (["powershell.exe", "-Command", "python.exe -c pass"], {"env":env}),
                (["bash.exe", "-c", "python -c pass"], {"env":env}),
                (["sh.exe", "-c", "python -c pass"], {"env":env}),
                (["env.exe", "python.exe", "-c", "pass"], {"env":env}),
            ])

        def test_missing_marker_outside_home_or_root_and_forged_root_marker_are_denied(self):
            missing = dict(env)
            missing.pop("HERMES_TEST_ISOLATION_TOKEN", None)
            outside_home = dict(env, HOME=str(Path(helper.fixture_root).parent / "outside-home"),
                                HERMES_HOME=str(Path(helper.fixture_root).parent / "outside-home"))
            outside_root = dict(env, HERMES_TEST_ISOLATION_ROOT=str(artifact))
            forged_marker = dict(env, HERMES_TEST_ISOLATION_TOKEN="0" * 64)
            valid_command = [sys.executable, "-c", "pass"]
            attempts = [
                (valid_command, {"env":missing}),
                (valid_command, {"env":outside_home}),
                (valid_command, {"env":outside_root}),
                (valid_command, {"env":forged_marker}),
            ]
            # A forged marker token must not authorize a caller merely because the
            # same forged value appears in its environment and helper object.
            self.rejected(attempts, token="0" * 64)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PopenRecorderTests)
    return unittest.TextTestRunner(stream=io.StringIO(),verbosity=verbosity).run(suite)


def _run_guard_qualification(helper, metadata: Path, verbosity: int):
    import test_db_isolation

    # Keep the 12 pure-stdlib DB-fence tests as the first, exact qualification
    # phase. Child-process policy tests run only after this class succeeds.
    result = _run_test_case(test_db_isolation, "DatabaseIsolationTests", verbosity)
    import sys as _sys
    scenarios = list(getattr(test_db_isolation, "SCENARIO_RECEIPTS", ()))
    receipt_path = _write_receipt(
        helper, metadata, phase="isolation", outcome="PASS" if result.wasSuccessful() else "FAIL",
        tests_run=result.testsRun, source_head=_source_head(), scenario_receipts=scenarios,
        error_kind=None if result.wasSuccessful() else "AssertionError",
    )
    counts = helper.snapshot()
    print(f"DB_ISOLATION_RECEIPT phase=isolation outcome={'PASS' if result.wasSuccessful() else 'FAIL'} "
          f"tests={result.testsRun} denied_targets={counts['rejected_db_targets']} delegated_connects="
          f"{counts['delegated_connect_calls']} epoch_calls={counts['epoch_calls']} "
          f"source_head={_source_head()} receipt={receipt_path}")
    return result, receipt_path


def _run_child_launch_qualification(helper, metadata: Path, verbosity: int):
    import test_db_isolation

    # This gate is issued only after all 12 pre-open DB tests pass. The focused
    # launch regressions may now start a separate, marked PM bootstrap child.
    os.environ["HERMES_TEST_ISOLATION_GUARD_TESTS_PASSED"] = "1"
    result = _run_test_case(test_db_isolation, "ChildProcessFenceTests", verbosity)
    import sys as _sys
    module = _sys.modules.get("test_db_isolation")
    scenarios = list(getattr(module, "CHILD_LAUNCH_RECEIPTS", ())) if module else []
    receipt_path = _write_receipt(
        helper, metadata, phase="child-launch", outcome="PASS" if result.wasSuccessful() else "FAIL",
        tests_run=result.testsRun, source_head=_source_head(), scenario_receipts=scenarios,
        error_kind=None if result.wasSuccessful() else "AssertionError",
    )
    counts = helper.snapshot()
    print(f"DB_ISOLATION_RECEIPT phase=child-launch outcome="
          f"{'PASS' if result.wasSuccessful() else 'FAIL'} tests={result.testsRun} "
          f"delegated_connects={counts['delegated_connect_calls']} epoch_calls={counts['epoch_calls']} "
          f"source_head={_source_head()} receipt={receipt_path}")
    return result, receipt_path


def _site_packages() -> Path:
    import sysconfig
    path = Path(sysconfig.get_paths()["purelib"]).resolve(strict=True)
    prefix = Path(sys.prefix).resolve(strict=True)
    if not path.is_relative_to(prefix):
        raise RuntimeError("dependency site-packages is not inside the selected interpreter environment")
    return path


def _qualified_core_checkout() -> tuple[Path, str, str]:
    raw_root = os.environ.get("HERMES_CORE_ROOT")
    expected_ref = os.environ.get("HERMES_CORE_REF")
    expected_generation = os.environ.get("HERMES_CORE_GENERATION")
    if not raw_root or not expected_ref or expected_generation not in ("current", "legacy", "installed"):
        raise RuntimeError("HERMES_CORE_ROOT, exact HERMES_CORE_REF, and core generation are required")
    if expected_generation == "installed" and expected_ref != "d923a5eb487a17c0a68801bba9ef0f56c81040a8":
        raise RuntimeError("installed-host qualification requires its explicit immutable source pin")
    core_root = Path(raw_root).resolve(strict=True)
    actual_ref = subprocess.run(["git", "-C", str(core_root), "rev-parse", "HEAD"],
                                check=True, capture_output=True, text=True, timeout=20).stdout.strip()
    if actual_ref != expected_ref:
        raise RuntimeError("Hermes core checkout does not match the declared immutable source ref")
    current_layout = ((core_root / "gateway" / "session_authority.py").is_file() and
                      (core_root / "hermes_cli" / "gateway_client.py").is_file())
    actual_generation = "current" if current_layout else "legacy"
    expected_layout = "legacy" if expected_generation == "installed" else expected_generation
    if actual_generation != expected_layout:
        raise RuntimeError("Hermes core generation does not match its declared source layout")
    required = (
        "hermes_constants.py", "hermes_state.py", "hermes_state_registry.py",
        "gateway/session.py", "gateway/session_persistence.py", "agent/redact.py",
    )
    if actual_generation == "current":
        required += ("gateway/session_authority.py",)
    for relative in required:
        if not (core_root / relative).is_file():
            raise RuntimeError("qualified Hermes source checkout is missing a required module")
    return core_root, actual_ref, actual_generation


def _qualify_core(helper) -> tuple[Path, str]:
    core_root, actual_ref, _actual_generation = _qualified_core_checkout()
    # ``-S`` deliberately skipped all .pth files. Add only this selected process's
    # dependency directory and the exact source checkout; never process editable hooks.
    site_packages = _site_packages()
    sys.path.insert(0, str(site_packages))
    sys.path.insert(0, str(core_root))
    helper.pin_core_sources(core_root, actual_ref)
    return core_root, actual_ref


def _run_native_db_qualification(fence, metadata: Path) -> int:
    expected_ref = "297552a9d646119871391d202509822cd86330df"
    if os.environ.get("HERMES_CORE_GENERATION") != "current" or \
            os.environ.get("HERMES_CORE_REF") != expected_ref:
        raise RuntimeError("native-db phase is limited to the declared current-core 297552a9d646119871391d202509822cd86330df")
    core_root, core_ref = _qualify_core(fence)
    fence.install_actual_sessiondb_fences()
    fence.require_db_fences()
    import hermes_state
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    native_home = fence.fixture_root / "native-sessiondb-home"
    native_home.mkdir(parents=True, exist_ok=True)
    expected = fence.validate_path(native_home / "state.db")
    if "gateway.session_authority" in sys.modules:
        raise RuntimeError("session authority was imported before the DB-only qualification")
    scope_token = set_hermes_home_override(native_home)
    before = fence.snapshot()
    db = None
    path_receipt = None
    try:
        # This is the first native SessionDB construction in the run. No Gateway,
        # session authority, epoch initialization, network, or plugin code is called.
        expected = fence.validate_path(expected)
        db = hermes_state.SessionDB(db_path=expected)
        path_receipt = fence.verify_session_db(db, expected)
        opened = fence.snapshot()
        if opened["native_constructor_delegations"] - before["native_constructor_delegations"] != 1:
            raise RuntimeError("native SessionDB constructor did not pass exactly one checked preflight")
        if opened["delegated_connect_calls"] <= before["delegated_connect_calls"]:
            raise RuntimeError("native SessionDB constructor did not produce a guarded SQLite open")
        if opened["epoch_calls"] != 0:
            raise RuntimeError("native-db phase unexpectedly called an epoch initializer")
    finally:
        try:
            if db is not None:
                db.close()
        finally:
            reset_hermes_home_override(scope_token)
    after = fence.snapshot()
    if after["epoch_calls"] != 0:
        raise RuntimeError("native-db phase must not call runtime epoch initialization")
    authority_loaded = "gateway.session_authority" in sys.modules
    if authority_loaded:
        raise RuntimeError("DB-only qualification unexpectedly imported session authority")
    native_receipt = {
        "db_path": path_receipt["db_path"],
        "fixture_root": path_receipt["fixture_root"],
        "main_under_fixture_root": path_receipt["main_under_fixture_root"],
        "all_file_databases_under_fixture_root": path_receipt["all_file_databases_under_fixture_root"],
        "constructor_delegation_delta": after["native_constructor_delegations"] - before["native_constructor_delegations"],
        "sqlite_connect_delegation_delta": after["delegated_connect_calls"] - before["delegated_connect_calls"],
        "sqlite_audit_authorized_delta": after["sqlite_audit_authorized"] - before["sqlite_audit_authorized"],
        "epoch_calls": after["epoch_calls"],
        "session_authority_module_loaded": authority_loaded,
    }
    receipt_path = _write_receipt(
        fence, metadata, phase="native-db", outcome="PASS", source_head=_source_head(),
        native_db_receipt=native_receipt,
    )
    print(f"FIRST_SESSIONDB_PATH_RECEIPT outcome=PASS core_ref={core_ref} "
          f"db_path={native_receipt['db_path']} main_under_fixture_root=true "
          f"sqlite_connects={native_receipt['sqlite_connect_delegation_delta']} epoch_calls=0 "
          f"session_authority_module_loaded=false core_origin_count={fence.loaded_core_origin_count()} "
          f"receipt={receipt_path}")
    return 0


def _is_pm_denied_module(name: str) -> bool:
    return any(name == prefix or name.startswith(prefix) for prefix in PM_DENIED_IMPORT_PREFIXES)


class _PMImportFence(importlib.abc.MetaPathFinder):
    """Keep PM-only workers away from session, Gateway, and model runtime modules."""

    def __init__(self):
        self.blocked_imports: list[str] = []

    def find_spec(self, fullname, path=None, target=None):
        del path, target
        if _is_pm_denied_module(fullname):
            self.blocked_imports.append(fullname)
            raise ImportError(f"PM test child refused protected runtime import: {fullname}")
        return None


def _install_pm_import_fence() -> _PMImportFence:
    loaded = sorted(name for name in sys.modules if _is_pm_denied_module(name))
    if loaded:
        raise RuntimeError("protected session/model modules were imported before the PM-only fence")
    fence = _PMImportFence()
    # It must run before both PathFinder and the pinned-core source finder.
    sys.meta_path.insert(0, fence)
    return fence


def _pm_import_guard_self_test(import_fence: _PMImportFence) -> None:
    for name in ("hermes_state", "hermes_state_registry", "gateway.session", "gateway.run",
                 "run_agent", "models", "providers"):
        try:
            importlib.import_module(name)
        except ImportError:
            continue
        raise RuntimeError(f"PM import fence allowed protected module {name}")
    for name in ("agent.model_metadata", "agent.runtime_session_store", "hermes_cli.models"):
        try:
            import_fence.find_spec(name)
        except ImportError:
            continue
        raise RuntimeError(f"PM import fence did not reject protected module {name}")
    if not import_fence.blocked_imports or any(
            _is_pm_denied_module(name) for name in sys.modules):
        raise RuntimeError("PM import fence self-test left a protected module loaded")


def _pm_venv_paths(executable_value, fixture_root: Path) -> tuple[Path, Path]:
    fixture = Path(fixture_root).resolve(strict=True)
    try:
        raw = os.fspath(executable_value)
        candidate = Path(raw)
        if not candidate.is_absolute():
            raise ValueError("PM interpreter path is not absolute")
        executable = candidate.resolve(strict=True)
        allowed_names = {"python.exe"} if os.name == "nt" else {"python", "python3"}
        if executable.name.casefold() not in allowed_names:
            raise ValueError("PM interpreter alias is not an approved Python executable name")
        if not executable.is_file() or not executable.is_relative_to(fixture):
            raise ValueError("PM interpreter is not a real file inside the active fixture")
        if executable.parent.name.casefold() not in {"scripts", "bin"}:
            raise ValueError("PM interpreter is not in a venv executable directory")
        venv_root = executable.parent.parent.resolve(strict=True)
        if not venv_root.is_relative_to(fixture):
            raise ValueError("PM venv escaped the active fixture")
        config = (venv_root / "pyvenv.cfg").resolve(strict=True)
        if not config.is_file() or not config.is_relative_to(venv_root):
            raise ValueError("PM interpreter has no owned pyvenv.cfg")
        candidates = (
            venv_root / "Lib" / "site-packages",
            venv_root / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages",
        )
        existing = [path for path in candidates if path.is_dir()]
        if len(existing) != 1:
            raise ValueError("PM venv site-packages is missing or ambiguous")
        site_packages = existing[0].resolve(strict=True)
        if not site_packages.is_dir() or not site_packages.is_relative_to(venv_root):
            raise ValueError("PM site-packages escaped its owned venv")
        return executable, site_packages
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeError("Python executable is not a qualified fixture-owned PM venv") from exc


def _validated_pm_site_packages(value, fixture_root: Path) -> tuple[Path, Path]:
    fixture = Path(fixture_root).resolve(strict=True)
    try:
        if not Path(value).is_absolute():
            raise ValueError("PM dependency operand must be absolute")
        site_packages = Path(value).resolve(strict=True)
        if not site_packages.is_dir() or not site_packages.is_relative_to(fixture):
            raise ValueError("site-packages is outside the active fixture")
        roots = []
        for parent in site_packages.parents:
            if parent == fixture or not parent.is_relative_to(fixture):
                break
            config = parent / "pyvenv.cfg"
            if config.is_file() and config.resolve(strict=True).is_relative_to(parent.resolve(strict=True)):
                roots.append(parent.resolve(strict=True))
        if len(roots) != 1:
            raise ValueError("PM site-packages does not identify one fixture-owned venv")
        venv_root = roots[0]
        accepted = {
            (venv_root / "Lib" / "site-packages").resolve(strict=False),
            (venv_root / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages").resolve(strict=False),
        }
        if site_packages not in accepted:
            raise ValueError("PM site-packages is not the selected interpreter's canonical path")
        return venv_root, site_packages
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RuntimeError("PM dependency site-packages is not proven fixture-owned") from exc


def _write_pm_child_receipt(fence, metadata: Path, *, kind: str, core_root: Path,
                            core_ref: str, import_fence: _PMImportFence, outcome: str) -> Path:
    blocked_loaded = sorted(name for name in sys.modules if _is_pm_denied_module(name))
    counters = fence.snapshot()
    if blocked_loaded or counters["epoch_calls"] != 0:
        outcome = "FAIL"
    record = {
        "schema": "hermes-telegram-bridge-db-isolation-child-v1",
        "child_kind": kind,
        "outcome": outcome,
        "core_ref": core_ref,
        "core_root": str(core_root),
        "db_guard_installed_before_child_imports": fence.ready and newdb_guard_identity(fence),
        "pm_import_guard_installed_before_child_imports": import_fence in sys.meta_path,
        "pm_import_guard_self_test": bool(import_fence.blocked_imports),
        "pm_blocked_import_attempts": len(import_fence.blocked_imports),
        "blocked_core_modules_loaded": blocked_loaded,
        "counters": counters,
    }
    path = metadata / f"child-{os.getpid()}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def newdb_guard_identity(fence) -> bool:
    return sys.modules.get("_hermes_telegram_bridge_db_isolation") is fence


def _prepare_pm_child(fence, site_packages_value) -> tuple[Path, str, _PMImportFence]:
    if not site_packages_value:
        raise RuntimeError("PM-only child requires its selected fixture-owned site-packages")
    core_root, core_ref, _generation = _qualified_core_checkout()
    venv_root, site_packages = _validated_pm_site_packages(site_packages_value, fence.fixture_root)
    if not venv_root.is_relative_to(fence.fixture_root.resolve(strict=True)):
        raise RuntimeError("selected PM venv escaped the active fixture")
    # No parent/core interpreter dependency path is added here. PM gets only its
    # own selected venv dependencies and the qualified Core source checkout.
    sys.path.insert(0, str(site_packages))
    sys.path.insert(0, str(core_root))
    fence.pin_core_sources(core_root, core_ref)
    import_fence = _install_pm_import_fence()
    _pm_import_guard_self_test(import_fence)
    if fence.snapshot()["epoch_calls"] != 0:
        raise RuntimeError("PM-only bootstrap unexpectedly called session epoch initialization")
    os.environ["HERMES_TEST_ISOLATION_GUARD_TESTS_PASSED"] = "1"
    _install_child_process_fence(fence)
    return core_root, core_ref, import_fence


def _run_pm_child(fence, metadata: Path, args) -> int:
    if args.modules:
        raise RuntimeError("PM-only child does not accept arbitrary trailing arguments")
    if bool(args.pm_stage_check) == bool(args.pm_worker):
        raise RuntimeError("exactly one bounded PM child operation is required")
    core_root, core_ref, import_fence = _prepare_pm_child(fence, args.pm_site_packages)
    kind = "pm-stage-check" if args.pm_stage_check else "pm-worker"
    outcome = "PASS"
    status = 0
    try:
        if args.pm_stage_check:
            sys.argv = ["-c"]
            namespace = {"__name__": "__main__", "__builtins__": __builtins__}
            exec(compile(PM_STAGE_CHECK_CODE, "<pm-runtime-dependency-check>", "exec"), namespace, namespace)
        else:
            expected_worker = (core_root / "pm" / "worker.py").resolve(strict=True)
            supplied_worker = Path(args.pm_worker).resolve(strict=True)
            if supplied_worker != expected_worker or not supplied_worker.is_file():
                raise RuntimeError("PM child may execute only the qualified core/pm/worker.py")
            import runpy
            sys.argv = [str(supplied_worker)]
            runpy.run_path(str(supplied_worker), run_name="__main__")
    except SystemExit as exc:
        status = int(exc.code or 0)
        outcome = "PASS" if status == 0 else "FAIL"
    except BaseException:
        outcome = "FAIL"
        raise
    finally:
        _write_pm_child_receipt(fence, metadata, kind=kind, core_root=core_root,
                                core_ref=core_ref, import_fence=import_fence, outcome=outcome)
    return status


def _install_child_process_fence(helper) -> None:
    """Fence every descendant launch by executable, operation, and marked fixture scope."""
    import hashlib as hashlib_module
    import shutil
    import subprocess as subprocess_module

    original_popen = subprocess_module.Popen
    runner_path = Path(__file__).resolve(strict=True)
    fixture_root = Path(helper.fixture_root).resolve(strict=True)
    artifact_root = Path(helper.artifact_root).resolve(strict=True)
    configured_core_root = getattr(helper, "core_root", None)
    core_root = (Path(configured_core_root).resolve(strict=True) if configured_core_root is not None
                 else _qualified_core_checkout()[0])
    expected_exe = os.path.normcase(os.path.realpath(sys.executable))
    if fixture_root.parent != artifact_root:
        raise RuntimeError("child-process fixture/artifact roots are not a direct owned pair")

    def normalized(path: Path) -> str:
        return os.path.normcase(os.path.realpath(os.fspath(path)))

    def is_within(path: Path, root: Path) -> bool:
        try:
            return path.resolve(strict=False).is_relative_to(root.resolve(strict=True))
        except (OSError, RuntimeError, ValueError):
            return False

    def validate_marked_scope(env) -> None:
        if not hasattr(env, "get"):
            raise RuntimeError("child environment is not a verifiable mapping")
        expected = {
            "HERMES_TEST_ISOLATION_ROOT": fixture_root,
            "HERMES_TEST_ISOLATION_ARTIFACT_ROOT": artifact_root,
            "HERMES_TEST_ISOLATION_META_ROOT": artifact_root / "metadata",
            "HERMES_TEST_ISOLATION_OUTSIDE_ROOT": artifact_root / "outside-sentinel",
        }
        if env.get("HERMES_TEST_ISOLATION_GUARD_TESTS_PASSED") != "1":
            raise RuntimeError("child launch is missing the verified isolation-test marker")
        if env.get("HERMES_TEST_ISOLATION_TOKEN") != helper.token:
            raise RuntimeError("child launch fixture token does not match the active owner")
        if not isinstance(helper.token, str) or re.fullmatch(r"[0-9a-f]{64}", helper.token) is None:
            raise RuntimeError("active isolation token has an invalid shape")
        for name, expected_path in expected.items():
            raw = env.get(name)
            if not isinstance(raw, str) or not Path(raw).is_absolute():
                raise RuntimeError("child launch is missing an absolute marked-root binding")
            try:
                actual = Path(raw).resolve(strict=True)
                expected_actual = expected_path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise RuntimeError("child launch marked-root binding is unavailable") from exc
            if normalized(actual) != normalized(expected_actual):
                raise RuntimeError("child launch marked-root binding differs from the active fixture")
        for root in (artifact_root, fixture_root):
            marker_path = root / MARKER
            try:
                resolved_marker = marker_path.resolve(strict=True)
                marker_value = resolved_marker.read_text(encoding="ascii")
            except (OSError, RuntimeError, UnicodeError) as exc:
                raise RuntimeError("child launch cannot verify the owned root marker") from exc
            if resolved_marker.parent != root or marker_value != helper.token:
                raise RuntimeError("child launch root marker does not match the active fixture token")

        scoped_path_vars = (
            "HERMES_HOME", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
            "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
            "HERMES_TEST_TMPDIR", "TMPDIR", "TMP", "TEMP", "RUNNER_TEMP",
        )
        for name in scoped_path_vars:
            raw = env.get(name)
            if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
                raise RuntimeError("child launch lacks a fixture-owned home/cache/temp path")
            if not is_within(Path(raw), fixture_root):
                raise RuntimeError("child launch home/cache/temp path escapes the marked fixture")
        env_core = env.get("HERMES_CORE_ROOT")
        if env_core:
            try:
                if not Path(env_core).is_absolute():
                    raise RuntimeError("child core binding must be absolute")
                if normalized(Path(env_core).resolve(strict=True)) != normalized(core_root):
                    raise RuntimeError("child launch core root differs from the pinned read-only checkout")
            except (OSError, RuntimeError) as exc:
                raise RuntimeError("child launch core root cannot be qualified") from exc
        env_install = env.get("HERMES_INSTALL_ROOT")
        if env_install:
            try:
                if not Path(env_install).is_absolute():
                    raise RuntimeError("child install binding must be absolute")
                if normalized(Path(env_install).resolve(strict=True)) != normalized(core_root):
                    raise RuntimeError("child launch install root differs from the pinned read-only checkout")
            except (OSError, RuntimeError) as exc:
                raise RuntimeError("child launch install root cannot be qualified") from exc

    allowed_cwd_roots = (fixture_root, REPO_ROOT.resolve(strict=True), core_root)

    def validate_cwd(raw) -> Path:
        try:
            cwd = Path.cwd() if raw is None else Path(os.fspath(raw))
            if not cwd.is_absolute():
                cwd = Path.cwd() / cwd
            cwd = cwd.resolve(strict=True)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise RuntimeError("child current directory cannot be physically qualified") from exc
        if not any(is_within(cwd, root) for root in allowed_cwd_roots):
            raise RuntimeError("child current directory escapes the source/core/fixture allowlist")
        return cwd

    tool_paths = {}
    for name in ("git", "node", "uv", "cmd.exe"):
        raw = shutil.which(name)
        if raw:
            try:
                resolved = Path(raw).resolve(strict=True)
                if resolved.is_file():
                    tool_paths[name] = resolved
            except (OSError, RuntimeError):
                continue
    tool_aliases = {
        alias.casefold(): name
        for name in tool_paths
        for alias in (name, name.removesuffix(".exe"), name + ".exe")
    }
    allowed_node_inline_sha256 = {
        "1c46216b4801aa8b466680139da5c39075f021881451cfddc29e6ef3b90c4219",
        "1679156f41eadc40967479fcc7237e41cb8534858dc71157f159542fb4009dce",
        # Added only for the joined canonical monitor → fixed-CLI fixture adapter.
        "50c377da37173588d597095b9baed3cafff394b14c7bea961cd1aefad33151dd",
    }
    canonical_event_pattern = "actual core approval event enters the SDK callback adapter and fixed CLI observer"
    readonly_git_refs = {
        "4874276f519f68fdbacb9767f70c5ed760b5c6fb",
        "1a76ed54ccf8a279d4dc4d9bfe1553b90d81d5ca",
    }
    readonly_git_paths = {
        "desktop-plugins/session-complete-telegram",
        "plugins/desktop-clarify-telegram",
    }

    def resolve_program(raw: str) -> Path | None:
        if not raw or "\x00" in raw:
            return None
        candidate = Path(raw)
        if candidate.is_absolute() or "/" in raw or "\\\\" in raw:
            try:
                return candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                return None
        name = tool_aliases.get(raw.casefold())
        if name:
            return tool_paths[name]
        found = shutil.which(raw)
        if found:
            try:
                return Path(found).resolve(strict=True)
            except (OSError, RuntimeError):
                return None
        return None

    def selected_tool(program: Path) -> str | None:
        program_path = normalized(program)
        for name, path in tool_paths.items():
            if program_path == normalized(path):
                return name
        return None

    def valid_git_operation(words: list[str]) -> bool:
        if len(words) < 5 or words[1] != "-C":
            return False
        try:
            if not Path(words[2]).is_absolute():
                return False
            repository = Path(words[2]).resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        if normalized(repository) not in {normalized(REPO_ROOT), normalized(core_root)}:
            return False
        tail = words[3:]
        if tail == ["rev-parse", "HEAD"]:
            return True
        if len(tail) >= 6 and tail[:3] == ["ls-tree", "-r", "--name-only"]:
            ref = tail[3]
            if ref not in readonly_git_refs or tail[4] != "--":
                return False
            paths = tail[5:]
            return bool(paths) and all(path in readonly_git_paths for path in paths)
        if tail[:1] == ["show"] and len(tail) == 2:
            ref_path = tail[1]
            ref, separator, path = ref_path.partition(":")
            return bool(separator and ref in readonly_git_refs and "\\\\" not in path and
                not path.startswith("/") and ".." not in path.split("/") and
                any(path.startswith(prefix + "/") for prefix in readonly_git_paths))
        return False

    def valid_node_operation(words: list[str], cwd: Path) -> bool:
        args = words[1:]
        if args == [str(REPO_ROOT / "tests" / "support" / "verify_native_api_wire.mjs")]:
            # Exact DB-free Node producer + pure pinned Electron routing peer.
            # No arbitrary script, flag, cwd or changed helper is admitted.
            peer = REPO_ROOT / "tests" / "support" / "verify_native_api_wire.mjs"
            return (normalized(cwd) == normalized(PACKAGE_DIR.resolve(strict=True)) and
                    peer.is_file() and hashlib_module.sha256(peer.read_bytes()).hexdigest() ==
                    "28901afc4d4d6622034f66a3fff6bb8499554d79e09156f2d3204a0f6c536eb0")
        if args == ["--test", "--test-name-pattern", canonical_event_pattern, "canonical.test.mjs"]:
            return normalized(cwd) == normalized(PACKAGE_DIR.resolve(strict=True))
        if args == ["--test-isolation=none", "--test",
                    "--test-name-pattern=approval-publication-epoch-interleaving",
                    "--test-reporter=tap", "--test-reporter-destination=stderr",
                    "approval_concurrency.test.mjs"]:
            # One fixed, DB-free Node peer for the already-fenced Python consumer.
            # No general Node flags, scripts, cwd or reporter destinations admitted.
            package = PACKAGE_DIR.resolve(strict=True)
            script = (package / "approval_concurrency.test.mjs").resolve(strict=True)
            return normalized(cwd) == normalized(package) and script.parent == package
        if len(args) == 3 and args[0] == "--input-type=module" and args[1] == "-e":
            digest = hashlib_module.sha256(args[2].encode("utf-8", errors="strict")).hexdigest()
            return digest in allowed_node_inline_sha256 and normalized(cwd) in {
                normalized(PACKAGE_DIR.resolve(strict=True)), normalized(REPO_ROOT.resolve(strict=True))}
        return False

    def valid_junction_operation(words: list[str]) -> bool:
        if os.name != "nt" or len(words) != 6 or words[1:4] != ["/c", "mklink", "/J"]:
            return False
        link_raw, target_raw = words[4], words[5]
        safe_chars = re.compile(r"^[A-Za-z0-9 _.:\\\\/+-]+$")
        if not safe_chars.fullmatch(link_raw) or not safe_chars.fullmatch(target_raw):
            return False
        try:
            link = Path(link_raw)
            if not Path(target_raw).is_absolute():
                return False
            target = Path(target_raw).resolve(strict=True)
            link_parent = link.parent.resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        return (link.is_absolute() and not link.exists() and
                is_within(link_parent, fixture_root) and is_within(target, artifact_root))

    def valid_uv_operation(words: list[str], cwd: Path, env) -> bool:
        args = words[1:]
        if args == ["--version"]:
            return is_within(cwd, fixture_root)
        # The only mutating uv operation qualified by the disposable native PM
        # install fixture is its exact isolated venv bootstrap. It may create a
        # venv only under the marked artifact, use only the fixture/selected Python,
        # and inherit the already-validated task-owned cache/config/temp roots.
        if (len(args) < 7 or args[:5] !=
                ["venv", "--relocatable", "--no-project", "--no-config", "--python"]):
            return False
        python_raw, destination_raw = args[5], args[6]
        optional = args[7:]
        if any(flag not in {"--offline", "--verbose"} for flag in optional) or len(set(optional)) != len(optional):
            return False
        try:
            operands = (python_raw, destination_raw, env.get("UV_PROJECT_ENVIRONMENT", ""),
                        env.get("UV_PYTHON", ""), env.get("UV_CACHE_DIR", ""))
            if any(not Path(value).is_absolute() for value in operands):
                return False
            python_path = Path(python_raw).resolve(strict=True)
            destination = Path(destination_raw).resolve(strict=False)
            project_env = Path(env.get("UV_PROJECT_ENVIRONMENT", "")).resolve(strict=False)
            uv_python = Path(env.get("UV_PYTHON", "")).resolve(strict=True)
            cache = Path(env.get("UV_CACHE_DIR", "")).resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        python_allowed = normalized(python_path) == expected_exe
        return bool(
            python_path.is_file() and python_allowed and normalized(uv_python) == normalized(python_path) and
            env.get("UV_PYTHON_DOWNLOADS") == "never" and
            normalized(destination) == normalized(project_env) and
            is_within(destination, fixture_root) and is_within(destination.parent, fixture_root) and
            is_within(cache, fixture_root) and is_within(cwd, fixture_root))

    def rewrite_pm_child(command: list[str], executable_value: str) -> list[str]:
        _executable, site_packages = _pm_venv_paths(executable_value, fixture_root)
        if len(command) < 4 or command[1:3] != ["-I", "-B"]:
            raise RuntimeError("fixture PM Python child flags are not the approved isolated form")
        tail = command[3:]
        if tail == ["-c", PM_STAGE_CHECK_CODE]:
            return [sys.executable, "-I", "-S", str(runner_path), "--pm-stage-check",
                    "--pm-site-packages", str(site_packages)]
        if len(tail) == 1:
            try:
                script = Path(tail[0]).resolve(strict=True)
                expected_worker = (core_root / "pm" / "worker.py").resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise RuntimeError("PM Python script path could not be qualified") from exc
            if script == expected_worker and script.is_file():
                return [sys.executable, "-I", "-S", str(runner_path), "--pm-worker", str(script),
                        "--pm-site-packages", str(site_packages)]
        raise RuntimeError("fixture PM Python child is not the exact stage check or fixed worker")

    def rewrite(args, kwargs):
        if "executable" in kwargs:
            raise RuntimeError("Popen executable overrides are refused by the DB isolation harness")
        if kwargs.get("shell") is True:
            raise RuntimeError("shell-launched children are refused by the DB isolation harness")
        if kwargs.get("shell") not in (None, False):
            raise RuntimeError("non-boolean shell launch mode is refused")
        if isinstance(args, (str, bytes)) or not isinstance(args, (list, tuple)) or not args:
            raise RuntimeError("string/empty child commands are refused; pass a qualified argv list")
        command = []
        for value in args:
            if isinstance(value, bytes):
                raise RuntimeError("byte-string child argv is refused")
            try:
                item = os.fspath(value)
            except TypeError as exc:
                raise RuntimeError("child argv contains a non-path value") from exc
            if not isinstance(item, str) or "\x00" in item:
                raise RuntimeError("child argv contains an invalid value")
            command.append(item)

        env = os.environ if kwargs.get("env") is None else kwargs["env"]
        validate_marked_scope(env)
        cwd = validate_cwd(kwargs.get("cwd"))
        kwargs["cwd"] = str(cwd)
        program = resolve_program(command[0])
        if program is None or not program.is_file():
            raise RuntimeError("child executable is not a resolved, allowlisted program")
        program_key = normalized(program)

        if program_key == expected_exe:
            # Delegate the program whose identity was checked, never a spelling
            # reinterpreted relative to a different child cwd or PATH.
            command[0] = str(program)
            if len(command) >= 4 and command[1:3] == ["-I", "-S"]:
                try:
                    if not Path(command[3]).is_absolute():
                        raise RuntimeError("guarded script operand must be absolute")
                    guarded_entry = Path(command[3]).resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise RuntimeError("guarded child entrypoint cannot be qualified") from exc
                if guarded_entry != runner_path:
                    raise RuntimeError("isolated Python child did not enter the fixed guard runner")
                command[3] = str(guarded_entry)
                tail = command[4:]
                if len(tail) >= 2 and tail[0] == "--child-code":
                    if len(tail) > 2 and (tail[2] != "--" or any(not isinstance(arg, str) for arg in tail[3:])):
                        raise RuntimeError("full-core guarded child arguments are malformed")
                    return command
                if len(tail) == 3 and tail[:2] == ["--pm-stage-check", "--pm-site-packages"]:
                    _venv, site = _validated_pm_site_packages(tail[2], fixture_root)
                    command[6] = str(site)
                    return command
                if len(tail) == 4 and tail[0] == "--pm-worker" and tail[2] == "--pm-site-packages":
                    if not Path(tail[1]).is_absolute():
                        raise RuntimeError("PM worker operand must be absolute")
                    worker = Path(tail[1]).resolve(strict=True)
                    if worker != (core_root / "pm" / "worker.py").resolve(strict=True):
                        raise RuntimeError("guarded PM child is not the exact pinned PM worker")
                    _venv, site = _validated_pm_site_packages(tail[3], fixture_root)
                    command[5], command[7] = str(worker), str(site)
                    return command
                raise RuntimeError("unapproved fixed-runner Python child operation")
            if len(command) < 3 or command[1] != "-c":
                raise RuntimeError("unbootstrapped Python child launch refused")
            rewritten = [sys.executable, "-I", "-S", str(runner_path), "--child-code", command[2]]
            if len(command) > 3:
                rewritten.extend(["--", *command[3:]])
            return rewritten

        if is_within(program, fixture_root) and program.name.casefold() in {"python.exe", "python", "python3"}:
            return rewrite_pm_child(command, str(program))

        tool = selected_tool(program)
        if tool == "git" and valid_git_operation(command):
            command[0] = str(program)
            return command
        if tool == "node" and valid_node_operation(command, cwd):
            command[0] = str(program)
            return command
        if tool == "uv" and valid_uv_operation(command, cwd, env):
            command[0] = str(program)
            return command
        if tool == "cmd.exe" and valid_junction_operation(command):
            command[0] = str(program)
            return command
        operation_summary = tuple(arg for arg in command[1:5] if not Path(arg).is_absolute())
        raise RuntimeError(f"non-Python child executable/operation is outside the explicit allowlist "
                           f"(program={program.name!r}, tool={tool or 'unrecognized'}, operation={operation_summary!r})")

    class GuardedPopen(original_popen):
        def __init__(self, args, *pargs, **kwargs):
            if pargs:
                raise RuntimeError("child process controls must be explicit keyword arguments")
            super().__init__(rewrite(args, kwargs), *pargs, **kwargs)

    subprocess_module.Popen = GuardedPopen


def _prepare_core_environment(helper) -> tuple[Path, str]:
    core_root, core_ref = _qualify_core(helper)
    package_text = str(PACKAGE_DIR)
    if package_text not in sys.path:
        sys.path.insert(0, package_text)
    helper.install_actual_native_fences()
    _install_child_process_fence(helper)
    os.environ["HERMES_TEST_ISOLATION_GUARD_TESTS_PASSED"] = "1"
    return core_root, core_ref


def _main(argv=None) -> int:
    sys.dont_write_bytecode = True
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    child_mode = args.child_code is not None
    pm_child_mode = args.pm_stage_check or args.pm_worker is not None
    if child_mode and pm_child_mode:
        raise RuntimeError("full-core and PM-only child modes are mutually exclusive")
    if args.pm_site_packages is not None and not pm_child_mode:
        raise RuntimeError("PM site-packages are accepted only in a PM-only child")
    if child_mode or pm_child_mode:
        fixture, artifact, metadata, outside, token = _existing_roots()
        _configure_fake_environment(fixture, artifact, metadata, outside, token, create=False)
        run_id = artifact.name.removeprefix("telegram-db-")
    else:
        base = _allowed_base(args.base or os.environ.get("HERMES_TEST_ISOLATION_BASE", ""))
        artifact, fixture, metadata, outside, token, run_id = _create_roots(base)
        _configure_fake_environment(fixture, artifact, metadata, outside, token, create=True)
    helper_module = _load_helper()
    _require_fresh_core_import_state(helper_module)
    fence = helper_module.install_guard(fixture, artifact, token)

    # Child-process denials are first exercised with a PopenRecorder in this
    # isolated stdlib phase. A failed recorder gate exits before any child launch.
    recorder_result = _run_popen_recorder_qualification(fence, args.verbosity)
    if not child_mode and not pm_child_mode:
        print(f"POPEN_RECORDER_QUALIFICATION outcome="
              f"{'PASS' if recorder_result.wasSuccessful() else 'FAIL'} tests={recorder_result.testsRun}")
    if not recorder_result.wasSuccessful():
        return 1
    if args.phase == "isolation" and not child_mode and not pm_child_mode:
        return 0

    if pm_child_mode:
        return _run_pm_child(fence, metadata, args)

    if child_mode:
        core_root, core_ref = _prepare_core_environment(fence)
        sys.argv = ["-c", *args.modules]
        namespace = {"__name__": "__main__", "__builtins__": __builtins__}
        try:
            exec(compile(args.child_code, "<guarded-python-child>", "exec"), namespace, namespace)
        except SystemExit as exc:
            return int(exc.code or 0)
        finally:
            path = metadata / f"child-{os.getpid()}.json"
            path.write_text(json.dumps({
                "schema": "hermes-telegram-bridge-db-isolation-child-v1",
                "parent_run_id": run_id,
                "core_ref": core_ref,
                "core_root": str(core_root),
                "counters": fence.snapshot(),
            }, indent=2) + "\n", encoding="utf-8")
        return 0

    result, _receipt = _run_guard_qualification(fence, metadata, args.verbosity)
    if not result.wasSuccessful():
        return 1
    launch_result, _launch_receipt = _run_child_launch_qualification(fence, metadata, args.verbosity)
    if not launch_result.wasSuccessful():
        return 1
    if args.phase == "native-db":
        return _run_native_db_qualification(fence, metadata)

    core_root, core_ref = _prepare_core_environment(fence)
    if args.phase == "canonical":
        modules = ["test_canonical_integration"]
    else:
        modules = args.modules
        if not modules:
            raise RuntimeError("--phase suite requires explicit unittest module names")
    modules = [name for name in modules if name != "test_db_isolation"]
    source_head = _source_head()
    print(f"GUARDED_TEST_BOOTSTRAP root={fixture} metadata={metadata} "
          f"core_ref={core_ref} bridge_head={source_head} guard_tests=PASS")
    suite_result = _run_modules(modules, args.verbosity)
    import sys as _sys
    scenario_module = _sys.modules.get("test_db_isolation")
    scenarios = list(getattr(scenario_module, "SCENARIO_RECEIPTS", ())) if scenario_module else []
    receipt_path = _write_receipt(
        fence, metadata, phase=args.phase, outcome="PASS" if suite_result.wasSuccessful() else "FAIL",
        tests_run=suite_result.testsRun, source_head=_source_head(), scenario_receipts=scenarios,
        error_kind=None if suite_result.wasSuccessful() else "AssertionError",
    )
    print(f"GUARDED_TEST_RECEIPT phase={args.phase} outcome="
          f"{'PASS' if suite_result.wasSuccessful() else 'FAIL'} tests={suite_result.testsRun} "
          f"epoch_calls={fence.snapshot()['epoch_calls']} "
          f"core_origin_count={fence.loaded_core_origin_count()} receipt={receipt_path}")
    return 0 if suite_result.wasSuccessful() else 1


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except Exception as exc:
        print(f"GUARDED_TEST_BOOTSTRAP_FAIL type={type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
