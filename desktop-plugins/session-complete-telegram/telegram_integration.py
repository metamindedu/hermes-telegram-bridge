"""Native Telegram handler integration and strict per-feature configuration gates.

Registration is a metadata-only declaration. The captured plugin owner, physical
package, active default home, configured bot, and live settings are checked only
when Hermes invokes the factory or a helper action.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

if __package__:
    from .runtime import PLUGIN_ID, resolve_package_home
else:
    from runtime import PLUGIN_ID, resolve_package_home

LEGACY_PLUGIN_ID = "desktop-clarify-telegram"
FEATURES = frozenset({"replies_enabled", "approvals_enabled"})
logger = logging.getLogger("hermes.plugin.session_complete_telegram")


class _UniqueKeySafeLoader:
    """Marker class populated lazily from PyYAML to reject ambiguous duplicate keys."""


def _unique_loader():
    import yaml

    class UniqueKeySafeLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node, deep=False):
        loader.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                duplicate = key in result
            except TypeError as exc:
                raise ValueError("unhashable_config_key") from exc
            if duplicate:
                raise ValueError("duplicate_config_key")
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    UniqueKeySafeLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    return UniqueKeySafeLoader


def _read_config(root: str | Path) -> dict[str, Any]:
    """Read only native config structure; invalid flags are distinguished from unsafe roots."""
    try:
        import yaml

        config_path = Path(root) / "config.yaml"
        raw_text = config_path.read_text(encoding="utf-8-sig")
        raw = yaml.load(raw_text, Loader=_unique_loader())
        if not isinstance(raw, dict):
            return {"structural_valid": False, "settings_valid": False, "reason": "native_plugin_config_invalid"}
        plugins = raw.get("plugins", {})
        if not isinstance(plugins, dict):
            return {"structural_valid": False, "settings_valid": False, "reason": "native_plugin_config_invalid"}
        enabled = plugins.get("enabled", [])
        disabled = plugins.get("disabled", [])
        if (type(enabled) is not list or any(type(value) is not str for value in enabled) or
                type(disabled) is not list or any(type(value) is not str for value in disabled)):
            return {"structural_valid": False, "settings_valid": False, "reason": "native_plugin_config_invalid"}
        entries = plugins.get("entries", {})
        if not isinstance(entries, dict):
            return {"structural_valid": False, "settings_valid": False, "reason": "native_plugin_config_invalid"}
    except Exception:
        return {"structural_valid": False, "settings_valid": False, "reason": "native_plugin_config_invalid"}

    active = set(enabled) - set(disabled)
    entry = entries.get(PLUGIN_ID, {})
    settings_valid = isinstance(entry, dict)
    settings: dict[str, Any] = {}
    if settings_valid:
        raw_settings = entry.get("settings", {})
        settings_valid = isinstance(raw_settings, dict)
        if settings_valid:
            settings = raw_settings
            settings_valid = (set(settings).issubset(FEATURES) and
                              all(type(settings.get(key, False)) is bool for key in FEATURES))
        # The public PluginContext API can fall back to the old `config` subtree.
        # These package switches are deliberately settings-only; legacy values
        # must never be treated as consent for the unified package.
        legacy_config = entry.get("config")
        if isinstance(legacy_config, dict) and set(legacy_config) & FEATURES:
            settings_valid = False
        elif legacy_config is not None and not isinstance(legacy_config, dict):
            settings_valid = False
    return {
        "structural_valid": True,
        "settings_valid": bool(settings_valid),
        "reason": "native_plugin_config_invalid" if not settings_valid else None,
        "active": active,
        "settings": settings if settings_valid else {},
        "primary_enabled": PLUGIN_ID in active,
        "migration_required": LEGACY_PLUGIN_ID in active,
    }


def migration_required(*, root: str | Path) -> bool:
    state = _read_config(root)
    return bool(state.get("structural_valid") and state.get("migration_required"))


def feature_status(name: str, *, root: str | Path, ctx=None) -> tuple[bool, str]:
    """Return (enabled, bounded reason), strictly honoring native plugin settings."""
    if name not in FEATURES:
        return False, "unknown_feature"
    state = _read_config(root)
    if not state.get("structural_valid") or not state.get("settings_valid"):
        return False, "native_plugin_config_invalid"
    if state.get("migration_required"):
        return False, "legacy_package_migration_required"
    if not state.get("primary_enabled"):
        return False, "session_complete_plugin_disabled"
    if ctx is not None:
        if getattr(ctx, "plugin_id", None) != PLUGIN_ID:
            return False, "native_plugin_owner_mismatch"
        getter = getattr(ctx, "get_config", None)
        if not callable(getter):
            return False, "native_plugin_config_invalid"
        try:
            api_value = getter(name, default=False)
        except Exception:
            return False, "native_plugin_config_invalid"
        if type(api_value) is not bool or api_value is not state["settings"].get(name, False):
            return False, "native_plugin_config_invalid"
    if state["settings"].get(name, False) is not True:
        return False, name.replace("_enabled", "_disabled")
    return True, "ready"


def feature_enabled(name: str, *, root: str | Path, ctx=None) -> bool:
    return feature_status(name, root=root, ctx=ctx)[0]


def _factory(ctx, captured_owner: object):
    def telegram_factory(application, adapter):
        if captured_owner != PLUGIN_ID or getattr(ctx, "plugin_id", None) != PLUGIN_ID:
            logger.warning("Telegram gateway factory refused: native_plugin_owner_mismatch")
            return
        try:
            home = resolve_package_home(__file__)
        except Exception as exc:
            logger.warning("Telegram gateway factory refused: %s", type(exc).__name__)
            return

        state = _read_config(home)
        if not state.get("structural_valid"):
            logger.warning("Telegram gateway factory refused: native_plugin_config_invalid")
            return
        if state.get("migration_required"):
            logger.warning("Telegram gateway factory refused: legacy_package_migration_required")
            return
        if not state.get("primary_enabled"):
            logger.warning("Telegram gateway factory refused: session_complete_plugin_disabled")
            return

        # A malformed optional feature flag is not authority to accept replies or
        # approvals, but handlers still install to consume known stale/tombstoned
        # cards rather than passing those replies into the ordinary agent prompt.
        feature_status("replies_enabled", root=home, ctx=ctx)
        feature_status("approvals_enabled", root=home, ctx=ctx)
        from . import approval_bridge, followup_bridge, notifier, reply_bridge

        reply_bridge._NOTIFIER = notifier
        reply_bridge.wire(
            application,
            adapter,
            followup_bridge,
            approval_bridge,
            replies_enabled=lambda: feature_enabled("replies_enabled", root=home),
            approvals_enabled=lambda: feature_enabled("approvals_enabled", root=home),
        )

    return telegram_factory


def register(ctx) -> None:
    """Declare the existing Telegram integration without reading home/config/state."""
    captured_owner = getattr(ctx, "plugin_id", None)
    ctx.register_telegram_handler(_factory(ctx, captured_owner))


__all__ = ["FEATURES", "LEGACY_PLUGIN_ID", "feature_enabled", "feature_status", "migration_required", "register"]
