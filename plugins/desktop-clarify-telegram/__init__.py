"""Opt-in Telegram ingress; the existing gateway remains the sole update owner."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import sys
import types
from pathlib import Path

PLUGIN_ID = "desktop-clarify-telegram"
FRONTEND_ID = "session-complete-telegram"
_HELPER_FILES = ("__init__.py", "runtime.py", "notifier.py", "reply_bridge.py", "followup_bridge.py", "approval_bridge.py")
FRONTEND_VERSION = "1.6.0"


def _validated_home(ctx) -> Path:
    if getattr(ctx, "plugin_id", None) != PLUGIN_ID:
        raise RuntimeError("native_plugin_owner_mismatch")
    package = Path(__file__).resolve(strict=True).parent
    if package.name != PLUGIN_ID or package.parent.name != "plugins":
        raise RuntimeError("native_plugin_location_invalid")
    home = package.parent.parent.resolve(strict=True)
    if home.parent.name.casefold() == "profiles":
        raise RuntimeError("named_profile_home_refused")
    try:
        from hermes_constants import get_hermes_home

        active_home = Path(get_hermes_home()).expanduser().resolve(strict=True)
    except Exception as exc:
        raise RuntimeError("hermes_home_unavailable") from exc
    if active_home != home:
        raise RuntimeError("foreign_hermes_home_refused")
    return home


def _sibling_package(home: Path):
    plugins_root = (home / "plugins").resolve(strict=True)
    candidate = home / "plugins" / FRONTEND_ID
    if candidate.is_symlink():
        raise RuntimeError("frontend_package_symlink_refused")
    package = candidate.resolve(strict=True)
    if package.parent != plugins_root or not package.is_dir():
        raise RuntimeError("frontend_package_location_invalid")
    manifest_path = package / "plugin.yaml"
    if not manifest_path.is_file():
        raise RuntimeError("frontend_package_manifest_missing")
    import hermes_yaml

    manifest = hermes_yaml.safe_load(manifest_path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(manifest, dict) or manifest.get("name") != FRONTEND_ID:
        raise RuntimeError("frontend_package_owner_mismatch")

    digest = hashlib.sha256()
    for filename in _HELPER_FILES:
        path = package / filename
        if not path.is_file():
            raise RuntimeError("frontend_package_file_missing")
        digest.update(filename.encode("ascii"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    home_key = hashlib.sha256(os.path.normcase(str(home)).encode("utf-8")).hexdigest()[:16]
    package_key = digest.hexdigest()[:16]
    namespace = f"_hermes_telegram_bridge_{home_key}_{package_key}"
    module_name = f"{namespace}.{FRONTEND_ID.replace('-', '_')}"
    cached = sys.modules.get(module_name)
    if cached is not None:
        if Path(getattr(cached, "__file__", "")).resolve() != (package / "__init__.py").resolve():
            raise RuntimeError("frontend_package_namespace_conflict")
        return cached

    parent = sys.modules.get(namespace)
    if parent is None:
        parent = types.ModuleType(namespace)
        parent.__package__ = namespace
        parent.__path__ = []
        sys.modules[namespace] = parent
    elif getattr(parent, "__package__", None) != namespace:
        raise RuntimeError("frontend_package_namespace_conflict")

    spec = importlib.util.spec_from_file_location(
        module_name, package / "__init__.py", submodule_search_locations=[str(package)])
    if spec is None or spec.loader is None:
        raise RuntimeError("frontend_package_loader_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        for name in tuple(sys.modules):
            if name == module_name or name.startswith(module_name + "."):
                sys.modules.pop(name, None)
        raise
    if getattr(module, "PLUGIN_ID", None) != FRONTEND_ID:
        for name in tuple(sys.modules):
            if name == module_name or name.startswith(module_name + "."):
                sys.modules.pop(name, None)
        raise RuntimeError("frontend_package_owner_mismatch")
    return module


def register(ctx) -> None:
    """Declare one factory without reading credentials, homes or bridge state."""
    def telegram_factory(application, adapter):
        # Registration is only a declaration (including admission probes).
        # Runtime owner/location/profile checks remain mandatory before loading
        # any helper or touching credentials, storage or Telegram handlers.
        home = _validated_home(ctx)
        package = _sibling_package(home)
        notifier = importlib.import_module(f"{package.__package__}.notifier")
        reply_bridge = importlib.import_module(f"{package.__package__}.reply_bridge")
        followup_bridge = importlib.import_module(f"{package.__package__}.followup_bridge")
        approval_bridge = importlib.import_module(f"{package.__package__}.approval_bridge")
        manifest = __import__("hermes_yaml").safe_load((Path(package.__path__[0]) / "plugin.yaml").read_text(encoding="utf-8-sig"))
        approval_ready = bool(isinstance(manifest, dict) and manifest.get("version") == FRONTEND_VERSION and
                              notifier.VERSION == FRONTEND_VERSION and approval_bridge.VERSION == FRONTEND_VERSION and
                              approval_bridge.CAPABILITY == "desktop-command-approval-v1")
        reply_bridge._NOTIFIER = notifier
        reply_bridge.wire(application, adapter, followup_bridge, approval_bridge if approval_ready else None)

    ctx.register_telegram_handler(telegram_factory)


__all__ = ["register"]
