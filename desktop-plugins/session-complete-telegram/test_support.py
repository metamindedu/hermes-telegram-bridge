"""Shared temporary-root policy for bridge tests."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile


def test_temp_root() -> Path:
    explicit = os.environ.get("HERMES_TEST_TMPDIR")
    if explicit:
        root = Path(explicit).expanduser()
        if not root.is_dir():
            raise RuntimeError("HERMES_TEST_TMPDIR must point to an existing directory")
        return root
    hinted = os.environ.get("TMPDIR")
    if hinted and Path(hinted).is_dir():
        return Path(hinted)
    return Path(tempfile.gettempdir())


def temporary_directory(*, prefix: str):
    return tempfile.TemporaryDirectory(prefix=prefix, dir=test_temp_root())
