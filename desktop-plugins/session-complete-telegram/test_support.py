"""Shared temporary-root policy for bridge tests."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile


def _owned_temp_root(root: Path) -> Path:
    guard = sys.modules.get("_hermes_telegram_bridge_db_isolation")
    if guard is None:
        if os.environ.get("HERMES_TEST_ISOLATION_ROOT"):
            raise RuntimeError("guarded test environment lost its pre-import DB isolation fence")
        return root
    guard.require_ready()
    resolved = guard.validate_path(root)
    if not resolved.is_dir():
        raise RuntimeError("test temporary root must be an existing directory under the marked fixture root")
    return resolved


def test_temp_root() -> Path:
    explicit = os.environ.get("HERMES_TEST_TMPDIR")
    if explicit:
        root = Path(explicit).expanduser()
        if not root.is_dir():
            raise RuntimeError("HERMES_TEST_TMPDIR must point to an existing directory")
        return _owned_temp_root(root)
    hinted = os.environ.get("TMPDIR")
    if hinted and Path(hinted).is_dir():
        return _owned_temp_root(Path(hinted))
    return _owned_temp_root(Path(tempfile.gettempdir()))


def temporary_directory(*, prefix: str):
    return tempfile.TemporaryDirectory(prefix=prefix, dir=test_temp_root())
