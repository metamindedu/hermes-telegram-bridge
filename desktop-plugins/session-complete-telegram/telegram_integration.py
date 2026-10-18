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
VERSION = "1.8.0"
SETTING_DEFAULTS = {
    "service_enabled": True,
    "completion_notifications_enabled": True,
    "attention_notifications_enabled": True,
    "clarify_replies_enabled": True,
    "completion_followups_enabled": True,
    "replies_enabled": False,
    "approvals_enabled": False,
}
SETTING_KEYS = tuple(SETTING_DEFAULTS)
FEATURES = frozenset(SETTING_DEFAULTS)
MODULES = {
    "completion_notifications": ("service_enabled", "completion_notifications_enabled"),
    "attention_notifications": ("service_enabled", "attention_notifications_enabled"),
    "clarify_replies": ("service_enabled", "replies_enabled", "clarify_replies_enabled", "attention_notifications_enabled"),
    "completion_followups": ("service_enabled", "replies_enabled", "completion_followups_enabled", "completion_notifications_enabled"),
    "approvals": ("service_enabled", "approvals_enabled", "attention_notifications_enabled"),
}
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
    raw_settings: dict[str, Any] = {}
    if settings_valid:
        value = entry.get("settings", {})
        settings_valid = isinstance(value, dict)
        if settings_valid:
            raw_settings = value
            settings_valid = (set(raw_settings).issubset(FEATURES) and
                              all(type(raw_settings.get(key, SETTING_DEFAULTS[key])) is bool
                                  for key in FEATURES))
        # The public PluginContext API can fall back to the old `config` subtree.
        # These package switches are deliberately settings-only; legacy values
        # must never be treated as consent for the unified package.
        legacy_config = entry.get("config")
        if isinstance(legacy_config, dict) and set(legacy_config) & FEATURES:
            settings_valid = False
        elif legacy_config is not None and not isinstance(legacy_config, dict):
            settings_valid = False
    display_settings = dict(SETTING_DEFAULTS)
    for key, value in raw_settings.items():
        if key in SETTING_DEFAULTS and type(value) is bool:
            display_settings[key] = value
    validated_settings = dict(SETTING_DEFAULTS)
    validated_settings.update(raw_settings if settings_valid else {})
    return {
        "structural_valid": True,
        "settings_valid": bool(settings_valid),
        "reason": "native_plugin_config_invalid" if not settings_valid else None,
        "active": active,
        "settings": validated_settings if settings_valid else display_settings,
        "primary_enabled": PLUGIN_ID in active,
        "migration_required": LEGACY_PLUGIN_ID in active,
    }


def migration_required(*, root: str | Path) -> bool:
    state = _read_config(root)
    return bool(state.get("structural_valid") and state.get("migration_required"))


def _managed_policy_healthy() -> bool:
    """Distinguish absent policy from an existing policy the core ignores on error."""
    try:
        from hermes_cli.managed_scope import get_managed_dir
        directory = get_managed_dir()
        if directory is None:
            return True
        path = Path(directory) / "config.yaml"
        try:
            path.stat()
        except FileNotFoundError:
            return True
        # Other stat/read/decode/parse failures must not restore raw consent.
        import yaml
        text = path.read_text(encoding="utf-8-sig")
        policy = yaml.load(text, Loader=_unique_loader())
        return isinstance(policy, dict) or (policy is None and not text.strip())
    except Exception:
        return False


def _native_effective_config(root: str | Path) -> tuple[dict[str, Any] | None, str]:
    """Read Hermes' effective config for the exact already-active native home.

    ``load_config_readonly`` applies defaults, environment expansion and the managed
    overlay. It returns a shared cached object; this function only reads it.
    """
    try:
        from hermes_constants import get_hermes_home
        active_home = Path(get_hermes_home()).expanduser().resolve(strict=True)
        expected_home = Path(root).expanduser().resolve(strict=True)
        if active_home != expected_home:
            return None, "native_plugin_home_mismatch"
        if not _managed_policy_healthy():
            return None, "native_plugin_config_invalid"
        from hermes_cli.config import load_config_readonly
        effective = load_config_readonly()
        if not _managed_policy_healthy():
            return None, "native_plugin_config_invalid"
        if not isinstance(effective, dict) or getattr(effective, "read_error", None) is not None:
            return None, "native_plugin_config_invalid"
        plugins = effective.get("plugins", {})
        if not isinstance(plugins, dict):
            return None, "native_plugin_config_invalid"
        enabled = plugins.get("enabled", [])
        disabled = plugins.get("disabled", [])
        if (type(enabled) is not list or any(type(value) is not str for value in enabled) or
                type(disabled) is not list or any(type(value) is not str for value in disabled)):
            return None, "native_plugin_config_invalid"
        entries = plugins.get("entries", {})
        settings_valid = isinstance(entries, dict)
        entry = entries.get(PLUGIN_ID, {}) if settings_valid else {}
        settings_valid = settings_valid and isinstance(entry, dict)
        settings = entry.get("settings", {}) if settings_valid else {}
        legacy_config = entry.get("config") if settings_valid else None
        settings_valid = (settings_valid and isinstance(settings, dict) and
                          set(settings).issubset(FEATURES) and
                          all(type(settings.get(name, SETTING_DEFAULTS[name])) is bool for name in FEATURES) and
                          not (isinstance(legacy_config, dict) and bool(set(legacy_config) & FEATURES)) and
                          (legacy_config is None or isinstance(legacy_config, dict)))
        if not isinstance(settings, dict):
            settings = {}
        effective_settings = dict(SETTING_DEFAULTS)
        if settings_valid:
            effective_settings.update(settings)
        return {
            "active": set(enabled) - set(disabled),
            "settings_valid": bool(settings_valid),
            "settings": effective_settings,
        }, "ready"
    except Exception:
        return None, "native_plugin_config_invalid"


def _selection_reason(raw: dict[str, Any], effective: dict[str, Any] | None) -> str:
    if effective is None:
        return "native_plugin_config_invalid"
    if raw.get("migration_required") or LEGACY_PLUGIN_ID in effective["active"]:
        return "legacy_package_migration_required"
    if not raw.get("primary_enabled") or PLUGIN_ID not in effective["active"]:
        return "session_complete_plugin_disabled"
    return "ready"


def effective_modules(settings: dict[str, bool], *, selected: bool = True) -> dict[str, bool]:
    """Compute bounded per-module eligibility without mutating the user's choices."""
    return {name: bool(selected and all(settings.get(key) is True for key in gates))
            for name, gates in MODULES.items()}


def settings_state(*, root: str | Path, ctx=None) -> dict[str, Any]:
    """Compare raw, native-effective, and captured PluginContext settings read-only."""
    raw = _read_config(root)
    settings = dict(SETTING_DEFAULTS)
    if isinstance(raw.get("settings"), dict):
        for key, value in raw["settings"].items():
            if key in SETTING_DEFAULTS and type(value) is bool:
                settings[key] = value
    if not raw.get("structural_valid") or not raw.get("settings_valid"):
        return {"ok": False, "settings": settings, "effective_settings": {},
                "selection_reason": "native_plugin_config_invalid"}
    if ctx is not None and getattr(ctx, "plugin_id", None) != PLUGIN_ID:
        return {"ok": False, "settings": settings, "effective_settings": {},
                "selection_reason": "native_plugin_owner_mismatch"}
    effective, reason = _native_effective_config(root)
    if effective is None:
        return {"ok": False, "settings": settings, "effective_settings": {},
                "selection_reason": reason}
    selection = _selection_reason(raw, effective)
    if not effective.get("settings_valid"):
        return {"ok": False, "settings": settings, "effective_settings": {},
                "selection_reason": "native_plugin_config_invalid"}
    for key in SETTING_KEYS:
        raw_value = raw["settings"].get(key, SETTING_DEFAULTS[key])
        effective_value = effective["settings"].get(key, SETTING_DEFAULTS[key])
        if type(raw_value) is not bool or type(effective_value) is not bool or raw_value is not effective_value:
            return {"ok": False, "settings": settings, "effective_settings": {},
                    "selection_reason": "native_plugin_config_invalid"}
        if ctx is not None:
            getter = getattr(ctx, "get_config", None)
            if not callable(getter):
                return {"ok": False, "settings": settings, "effective_settings": {},
                        "selection_reason": "native_plugin_config_invalid"}
            try:
                api_value = getter(key, default=SETTING_DEFAULTS[key])
            except Exception:
                return {"ok": False, "settings": settings, "effective_settings": {},
                        "selection_reason": "native_plugin_config_invalid"}
            if type(api_value) is not bool or api_value is not raw_value:
                return {"ok": False, "settings": settings, "effective_settings": {},
                        "selection_reason": "native_plugin_config_invalid"}
    if selection == "ready" and settings["service_enabled"] is not True:
        selection = "service_disabled"
    return {"ok": True, "settings": settings, "effective_settings": effective["settings"],
            "selection_reason": selection}


def feature_status(name: str, *, root: str | Path, ctx=None) -> tuple[bool, str]:
    """Return (eligible, bounded reason), requiring exact raw/native/API agreement."""
    if name not in FEATURES and name not in MODULES:
        return False, "unknown_feature"
    state = settings_state(root=root, ctx=ctx)
    if not state["ok"]:
        return False, state["selection_reason"]
    settings = state["settings"]
    effective = state["effective_settings"]
    selection = state["selection_reason"]
    if selection != "ready":
        return False, selection
    if name == "replies_enabled":
        gates = ("service_enabled", "replies_enabled")
    elif name in {"clarify_replies_enabled", "clarify_replies"}:
        gates = MODULES["clarify_replies"]
    elif name in {"completion_followups_enabled", "completion_followups"}:
        gates = MODULES["completion_followups"]
    elif name in {"approvals_enabled", "approvals"}:
        gates = MODULES["approvals"]
    elif name in {"completion_notifications_enabled", "completion_notifications"}:
        gates = MODULES["completion_notifications"]
    elif name in {"attention_notifications_enabled", "attention_notifications"}:
        gates = MODULES["attention_notifications"]
    else:
        gates = ("service_enabled", name)
    for key in gates:
        if settings.get(key) is not True:
            return False, key.replace("_enabled", "_disabled")
        if effective.get(key, settings.get(key)) is not True:
            return False, "native_plugin_config_invalid"
    return True, "ready"


def feature_enabled(name: str, *, root: str | Path, ctx=None) -> bool:
    return feature_status(name, root=root, ctx=ctx)[0]


# Native receiver bindings belong only to the public SDK Application/adapter pair.
# Weak values do not keep a disposed application or adapter alive.
import hmac
import re
import weakref
import time

_NATIVE_BINDINGS = weakref.WeakValueDictionary()
_NATIVE_BINDING_KEY = "_newmetaw_session_complete_native_transport"


class _NativeNotifier:
    """Explicit receiver-only facade over an already host-bound transport.

    This is not a secret-scope fallback: CLI credentials still use get_secret.
    The current host's public runner/config identity must prove default ownership;
    unsupported hosts, named adapters and changed native bindings are refused.
    """
    def __init__(self, base, application, adapter, home):
        self._base, self._application, self._adapter, self._home = base, application, adapter, home
        self._retired = False
        self._ever_running = False
        self._startup_deadline = time.monotonic() + 180
        self._identity = self._snapshot()

    def _snapshot(self):
        if self._retired or resolve_package_home(__file__) != self._home:
            raise RuntimeError("native_transport_binding_unavailable")
        adapter, application = self._adapter, self._application
        if application.running:
            self._ever_running = True
        elif self._ever_running or time.monotonic() >= self._startup_deadline:
            raise RuntimeError("native_application_not_live")
        platform = getattr(adapter, "platform", None)
        if getattr(platform, "value", platform) != "telegram":
            raise RuntimeError("native_transport_platform_invalid")
        config = getattr(adapter, "config", None)
        runner = getattr(adapter, "gateway_runner", None)
        platforms = getattr(getattr(runner, "config", None), "platforms", None)
        if (not isinstance(platforms, dict) or platforms.get(platform) is not config or config is None
                or getattr(config, "enabled", None) is not True):
            raise RuntimeError("native_default_transport_unverified")
        token = getattr(config, "token", None)
        native_token = getattr(getattr(application, "bot", None), "token", None)
        if (not isinstance(token, str) or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token)
                or not isinstance(native_token, str) or not hmac.compare_digest(token, native_token)):
            raise RuntimeError("native_transport_identity_mismatch")
        destination = getattr(config, "home_channel", None)
        if getattr(destination, "platform", None) != platform:
            raise RuntimeError("native_destination_platform_invalid")
        raw_chat = getattr(destination, "chat_id", None)
        match = re.fullmatch(r"([0-9]+)(?::([0-9]+))?", raw_chat) if isinstance(raw_chat, str) else None
        if not match or int(match[1]) <= 0:
            raise RuntimeError("native_private_destination_invalid")
        chat, inline_topic = int(match[1]), int(match[2]) if match[2] else None
        raw_topic = getattr(destination, "thread_id", None)
        if raw_topic is not None and (type(raw_topic) not in (str, int) or not re.fullmatch(r"[0-9]+", str(raw_topic))):
            raise RuntimeError("native_topic_invalid")
        topic = int(raw_topic) if raw_topic is not None else inline_topic
        if inline_topic is not None and raw_topic is not None and inline_topic != topic:
            raise RuntimeError("native_topic_conflict")
        # If the host DID supply a scope, its stricter current credential policy
        # must agree too. An unscoped ordinary helper is never made permissive.
        from agent.secret_scope import current_secret_scope
        if current_secret_scope() is not None and self._base.credentials() != (token, chat, topic):
            raise RuntimeError("native_scoped_destination_mismatch")
        return token, chat, topic

    def credentials(self, root=None):
        if root is not None and Path(root).resolve() != self._home:
            raise RuntimeError("native_transport_home_mismatch")
        try:
            current = self._snapshot()
        except RuntimeError as exc:
            if exc.args and exc.args[0] in {
                "native_application_not_live", "native_transport_platform_invalid",
                "native_default_transport_unverified", "native_transport_identity_mismatch",
                "native_destination_platform_invalid", "native_private_destination_invalid",
                "native_topic_invalid", "native_topic_conflict",
            }:
                self.retire()
            raise
        if not hmac.compare_digest(current[0], self._identity[0]) or current[1:] != self._identity[1:]:
            self.retire()
            raise RuntimeError("native_transport_binding_changed")
        return self._identity

    def binding_valid(self):
        try:
            self.credentials()
            return True
        except Exception:
            return False

    def retire(self):
        self._retired = True
        state = self._application.bot_data.get("_newmetaw_session_complete_telegram_wiring_state", {})
        state["ready"] = False
        task = state.get("task")
        if task is not None and not task.done():
            task.get_loop().call_soon_threadsafe(task.cancel)

    @property
    def ROOT(self):
        return self._base.ROOT

    @property
    def STATE(self):
        return self._base.STATE

    def action_enabled(self, *args, **kwargs):
        return self.binding_valid() and self._base.action_enabled(*args, **kwargs)

    def telegram(self, token, method, payload):
        expected, chat, topic = self.credentials()
        if (not isinstance(token, str) or not hmac.compare_digest(token, expected)
                or not isinstance(payload, dict) or payload.get("chat_id") != chat
                or ("message_thread_id" in payload and payload["message_thread_id"] != topic)):
            return {"ok": False, "reason": "native_transport_destination_mismatch"}
        return self._base.telegram(token, method, payload)

    def inspect_ask(self, *args, **kwargs):
        return self._base.inspect_ask(*args, **kwargs)

    def legacy_message_id(self, *args, **kwargs):
        return self._base.legacy_message_id(*args, **kwargs)

    def final_preview(self, *args, **kwargs):
        return self._base.final_preview(*args, **kwargs)

    def profile_home(self, *args, **kwargs):
        return self._base.profile_home(*args, **kwargs)

    def state_db(self, *args, **kwargs):
        return self._base.state_db(*args, **kwargs)


def _native_notifier(base, application, adapter, home):
    existing = application.bot_data.get(_NATIVE_BINDING_KEY)
    if existing is not None:
        if (not isinstance(existing, _NativeNotifier) or existing._adapter is not adapter
                or not existing.binding_valid()):
            raise RuntimeError("native_transport_reuse_refused")
        return existing
    binding = _NativeNotifier(base, application, adapter, home)
    previous = _NATIVE_BINDINGS.get(id(adapter))
    if previous is not None and previous._application is not application:
        previous.retire()
    _NATIVE_BINDINGS[id(adapter)] = binding
    application.bot_data[_NATIVE_BINDING_KEY] = binding
    return binding


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

        raw = _read_config(home)
        if not raw.get("structural_valid"):
            logger.warning("Telegram gateway factory refused: native_plugin_config_invalid")
            return
        if raw.get("migration_required"):
            logger.warning("Telegram gateway factory refused: legacy_package_migration_required")
            return
        if not raw.get("primary_enabled"):
            logger.warning("Telegram gateway factory refused: session_complete_plugin_disabled")
            return
        effective, effective_reason = _native_effective_config(home)
        selection = _selection_reason(raw, effective)
        if effective is None:
            # The host can continue ordinary ingress when managed policy is
            # invalid. Keep card consumers installed; all dynamic action gates
            # still require a healthy native effective policy and deny here.
            logger.warning("Telegram gateway interactions refused: %s", effective_reason)
        elif selection != "ready":
            logger.warning("Telegram gateway factory refused: %s", selection)
            return

        # Optional settings may be malformed or disabled. The scoped consumers
        # still install to quarantine known cards; every action closure re-reads
        # strict raw and native effective policy through the captured context.
        from . import approval_bridge, followup_bridge, notifier, reply_bridge

        try:
            native_notifier = _native_notifier(notifier, application, adapter, home)
        except Exception as exc:
            logger.warning("Telegram gateway transport binding refused: %s", type(exc).__name__)
            return False
        return reply_bridge.wire(
            application,
            adapter,
            followup_bridge,
            approval_bridge,
            notifier=native_notifier,
            replies_enabled=lambda: feature_enabled("clarify_replies_enabled", root=home, ctx=ctx),
            followups_enabled=lambda: feature_enabled("completion_followups_enabled", root=home, ctx=ctx),
            approvals_enabled=lambda: feature_enabled("approvals_enabled", root=home, ctx=ctx),
        )

    return telegram_factory


def register(ctx) -> None:
    """Declare the existing Telegram integration without reading home/config/state."""
    captured_owner = getattr(ctx, "plugin_id", None)
    ctx.register_telegram_handler(_factory(ctx, captured_owner))


__all__ = ["FEATURES", "SETTING_DEFAULTS", "SETTING_KEYS", "MODULES", "VERSION", "LEGACY_PLUGIN_ID",
           "effective_modules", "feature_enabled", "feature_status", "settings_state", "migration_required", "register"]
