"""Native PM lifecycle tests inside the explicitly approved disposable container.

This entrypoint never runs canonical/session-authority tests. The host-owned
container has a read-only root/source and one writable scratch volume, no host
profile/auth/DB mounts, no Docker socket and no inherited provider credentials.
The normal canonical guarded_unittest entrypoint and its Popen fence are unchanged.
"""
from __future__ import annotations

import argparse
import importlib.abc
import json
import os
from pathlib import Path
import sys

SUPPORT = Path(__file__).resolve().parent
sys.path.insert(0, str(SUPPORT))
import guarded_unittest as guarded


class CanonicalTestsDenied(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "test_canonical_integration":
            raise RuntimeError("canonical integration requires its normal guarded entrypoint")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", default="test_native_install")
    args = parser.parse_args()
    if args.test != "test_native_install" and not args.test.startswith("test_native_install.NativeInstallTests.test_"):
        raise RuntimeError("this container entrypoint permits native-install tests only")
    if not Path("/.dockerenv").is_file() or os.environ.get("HERMES_BRIDGE_CONTAINER_BOUNDARY") != "native-pm-v1":
        raise RuntimeError("native PM standalone verification requires the owned container boundary")
    # The launcher independently verifies Docker's mounts, read-only root and
    # dropped capabilities before exec. Check the actual writable boundary here.
    if not os.statvfs("/").f_flag & os.ST_RDONLY:
        raise RuntimeError("container root filesystem must be read-only")
    scratch = (guarded.REPO_ROOT / ".ci-scratch").resolve(strict=True)
    expected = Path("/opt/bridge/.ci-scratch")
    if scratch != expected or not os.access(scratch, os.W_OK):
        raise RuntimeError("the sole fixture scratch volume is unavailable")
    os.chdir(guarded.PACKAGE_DIR)
    artifact, fixture, metadata, outside, token, run_id = guarded._create_roots(scratch)
    guarded._configure_fake_environment(fixture, artifact, metadata, outside, token, create=True)
    helper_module = guarded._load_helper()
    guarded._require_fresh_core_import_state(helper_module)
    fence = helper_module.install_guard(fixture, artifact, token)
    sys.meta_path.insert(0, CanonicalTestsDenied())
    qualification, _ = guarded._run_guard_qualification(fence, metadata, 2)
    if not qualification.wasSuccessful():
        return 1
    core_root, core_ref = guarded._qualify_core(fence)
    fence.install_actual_native_fences()
    # Deliberately no global Popen command-rewriting layer in this dedicated
    # native-PM OS boundary. Keep the parent DB/constructor fences and every real
    # PM resolver, scanner, dependency, lease, install/upgrade/rollback assertion.
    print("CONTAINER_NATIVE_PM_START " + json.dumps({
        "core_ref": core_ref, "core_root": str(core_root), "fixture_root": str(fixture),
        "read_only_root": True, "modules": [args.test], "canonical_entrypoint": False,
    }), flush=True)
    result = guarded._run_modules([args.test], 2)
    receipt = guarded._write_receipt(
        fence, metadata, phase="container-native-install",
        outcome="PASS" if result.wasSuccessful() else "FAIL", tests_run=result.testsRun,
        source_head=guarded._source_head(), scenario_receipts=[],
        error_kind=None if result.wasSuccessful() else "AssertionError",
    )
    print("CONTAINER_NATIVE_PM_RESULT " + json.dumps({
        "outcome": "PASS" if result.wasSuccessful() else "FAIL", "tests": result.testsRun,
        "failures": len(result.failures), "errors": len(result.errors),
        "skipped": len(result.skipped), "receipt": str(receipt),
        "epoch_calls": fence.snapshot()["epoch_calls"],
    }), flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
