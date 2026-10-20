"""Bounded settings status and single-key native settings writes.

This module intentionally has no YAML writer, arbitrary path interface, or state
store. User configuration is changed only through the captured PluginContext's
public ``set_config`` method after owner/home/policy checks.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .runtime import PLUGIN_ID, resolve_package_home
from . import telegram_integration as gateway

VERSION = "1.8.1"


def _context_reason(ctx: Any, root: str | Path) -> str | None:
    if getattr(ctx, "plugin_id", None) != PLUGIN_ID:
        return "native_plugin_owner_mismatch"
    try:
        package_home = resolve_package_home(__file__)
        expected_home = Path(root).expanduser().resolve(strict=True)
        if package_home != expected_home:
            return "native_plugin_home_mismatch"
        from hermes_constants import get_hermes_home
        if Path(get_hermes_home()).expanduser().resolve(strict=True) != expected_home:
            return "native_plugin_home_mismatch"
        from hermes_cli.profiles import get_active_profile_name
        if get_active_profile_name() != "default":
            return "default_home_required"
    except Exception:
        return "native_plugin_home_mismatch"
    if not gateway._managed_policy_healthy():
        return "native_plugin_config_invalid"
    return None


def _read_only_db(path: Path):
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                     timeout=2, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection
    except Exception:
        return None


def _receiver_summary(state: Path, effective: dict[str, bool]) -> dict[str, Any]:
    """Read existing receiver heartbeats only; never create an inbox or expose its IDs."""
    required = [name for name in ("clarify_replies", "completion_followups", "approvals")
                if effective.get(name) is True]
    if not required:
        return {"version": VERSION, "heartbeat_ready": False}
    now = time.time()
    ready: dict[str, bool] = {name: False for name in required}

    if "clarify_replies" in required:
        conn = _read_only_db(state / "reply-inbox" / "replies.sqlite3")
        if conn is not None:
            try:
                row = conn.execute("SELECT value FROM receiver WHERE key='live'").fetchone()
                live = json.loads(row["value"]) if row else None
                age = now - float(live.get("at", -1)) if isinstance(live, dict) else -1
                ready["clarify_replies"] = bool(isinstance(live, dict) and
                    isinstance(live.get("boot"), str) and live.get("boot") and 0 <= age <= 45)
            except Exception:
                pass
            finally:
                conn.close()

    if "completion_followups" in required:
        conn = _read_only_db(state / "followup-inbox" / "followups.sqlite3")
        if conn is not None:
            try:
                row = conn.execute("SELECT seen,private_verified,feature FROM receiver WHERE singleton=1").fetchone()
                if row:
                    ready["completion_followups"] = bool(row["private_verified"] == 1 and
                        type(row["feature"]) is int and row["feature"] == 1 and
                        0 <= now - float(row["seen"]) <= 45)
            except Exception:
                pass
            finally:
                conn.close()

    if "approvals" in required:
        conn = _read_only_db(state / "approval-inbox" / "approvals.sqlite3")
        if conn is not None:
            try:
                row = conn.execute("SELECT value FROM receiver WHERE key='live'").fetchone()
                live = json.loads(row["value"]) if row else None
                age = now - float(live.get("at", -1)) if isinstance(live, dict) else -1
                from . import approval_bridge
                ready["approvals"] = bool(isinstance(live, dict) and
                    live.get("version") == approval_bridge.VERSION and
                    live.get("capability") == approval_bridge.CAPABILITY and
                    live.get("private_verified") is True and
                    isinstance(live.get("boot"), str) and live.get("boot") and 0 <= age <= 45)
            except Exception:
                pass
            finally:
                conn.close()

    return {"version": VERSION, "heartbeat_ready": bool(ready) and all(ready.values())}


def _sender_summary(state: Path) -> dict[str, Any]:
    """Read metadata-only sender recovery counts; never opens the receiver ledger."""
    empty = {"version": VERSION, "available": True, "awaiting_recovery": 0,
             "uncertain_delivery": 0, "sent": 0, "expired": 0}
    try:
        from . import completion_recovery
        return {**empty, **completion_recovery.diagnostics(state)}
    except Exception:
        return {**empty, "available": False}


def _status(ctx: Any, root: str | Path, state: str | Path, *, read_only: bool) -> dict[str, Any]:
    reason = _context_reason(ctx, root)
    if reason is None:
        snapshot = gateway.settings_state(root=root, ctx=ctx)
        if not snapshot["ok"]:
            reason = snapshot["selection_reason"]
    else:
        snapshot = {"ok": False, "settings": dict(gateway.SETTING_DEFAULTS),
                    "effective_settings": {}, "selection_reason": reason}
    settings = snapshot.get("settings")
    if not isinstance(settings, dict):
        settings = dict(gateway.SETTING_DEFAULTS)
    settings = {key: settings.get(key, default) if type(settings.get(key, default)) is bool else default
                for key, default in gateway.SETTING_DEFAULTS.items()}
    selected = snapshot.get("selection_reason") == "ready"
    effective = gateway.effective_modules(settings, selected=selected and snapshot.get("ok") is True)
    state_path = Path(state)
    receiver = _receiver_summary(state_path, effective) if snapshot.get("ok") else {
        "version": VERSION, "heartbeat_ready": False}
    selection_reason = snapshot.get("selection_reason", reason or "native_plugin_config_invalid")
    if reason is not None:
        selection_reason = reason
    return {
        "ok": reason is None and snapshot.get("ok") is True,
        "version": VERSION,
        "settings": settings,
        "effective": effective,
        "read_only": read_only,
        "selection_reason": selection_reason,
        "receiver": receiver,
        "sender": _sender_summary(state_path),
    }


def settings_status(ctx: Any, *, root: str | Path, state: str | Path) -> dict[str, Any]:
    """Return a fixed, read-only DTO with no credentials, destination, or inbox content."""
    return _status(ctx, root, state, read_only=True)


def _cancel_for_setting(key: str) -> tuple[bool, bool, bool, bool]:
    """Return clarify/followup/approval/completion-recovery retirement for OFF."""
    clarify = key in {"service_enabled", "attention_notifications_enabled", "clarify_replies_enabled", "replies_enabled"}
    followups = key in {"service_enabled", "completion_notifications_enabled", "completion_followups_enabled", "replies_enabled"}
    approvals = key in {"service_enabled", "attention_notifications_enabled", "approvals_enabled"}
    completion = key in {"service_enabled", "completion_notifications_enabled"}
    return clarify, followups, approvals, completion


def _retire_unexecuted(key: str, state: Path) -> dict[str, Any]:
    clarify, followups, approvals, completion = _cancel_for_setting(key)
    results: dict[str, Any] = {"ok": True, "clarify": 0, "followups": 0, "approvals": 0,
                               "completion_recovery": 0, "outcomes": {}}

    def attempt(store: str, retire) -> None:
        try:
            count = retire()
            if type(count) is not int or count < 0:
                raise ValueError("invalid_cleanup_result")
            results[store] = count
            results["outcomes"][store] = {"ok": True, "retired": count}
        except Exception:
            results["ok"] = False
            results["reason"] = "cleanup_incomplete"
            results["outcomes"][store] = {"ok": False, "reason": "cleanup_incomplete"}

    if clarify:
        def retire_clarify():
            from . import reply_bridge
            return reply_bridge.retire_unexecuted(state=state)
        attempt("clarify", retire_clarify)
    if followups:
        def retire_followups():
            from . import followup_bridge
            return followup_bridge.retire_unexecuted(state=state)
        attempt("followups", retire_followups)
    if approvals:
        def retire_approvals():
            from . import approval_bridge
            return approval_bridge.retire_unexecuted(state=state)
        attempt("approvals", retire_approvals)
    if completion:
        def retire_completion_recovery():
            from . import completion_recovery
            return completion_recovery.retire_unexecuted(state)
        attempt("completion_recovery", retire_completion_recovery)
    return results


def settings_set(request: object, ctx: Any, *, root: str | Path, state: str | Path) -> dict[str, Any]:
    """Write exactly one known boolean via native PluginContext.set_config and read it back."""
    denied = {"ok": False, "version": VERSION, "reason": "invalid_settings_request",
              "read_only": False}
    if (not isinstance(request, dict) or set(request) - {"action", "key", "value", "expected"} or
            request.get("action") != "settings_set" or type(request.get("key")) is not str or
            request["key"] not in gateway.SETTING_DEFAULTS or type(request.get("value")) is not bool or
            ("expected" in request and type(request["expected"]) is not bool)):
        return denied
    reason = _context_reason(ctx, root)
    if reason is not None:
        return {**denied, "reason": reason}
    snapshot = gateway.settings_state(root=root, ctx=ctx)
    if not snapshot["ok"]:
        return {**denied, "reason": snapshot["selection_reason"]}
    if snapshot["selection_reason"] not in {"ready", "service_disabled"}:
        return {**denied, "reason": snapshot["selection_reason"]}
    key = request["key"]
    value = request["value"]
    current = snapshot["settings"][key]
    if "expected" in request and request["expected"] is not current:
        return {**denied, "reason": "settings_changed", "readback": {"key": key, "value": current}}
    writer = getattr(ctx, "set_config", None)
    if not callable(writer):
        return {**denied, "reason": "native_settings_writer_unavailable"}

    cleanup = {"ok": True, "clarify": 0, "followups": 0, "approvals": 0, "completion_recovery": 0,
               "outcomes": {}}
    pre_cleanup_done = False
    if value is False or (current is False and value is True):
        cleanup = _retire_unexecuted(key, Path(state))
        pre_cleanup_done = True
        # ON never crosses an incomplete retirement. OFF still writes the user's
        # requested fail-closed value even if cleanup needs repair.
        if value is True and cleanup.get("ok") is not True:
            result = _status(ctx, root, state, read_only=False)
            result["write_status"] = "not_attempted"
            result["readback"] = {"key": key, "value": current}
            result["cleanup"] = cleanup
            result["ok"] = False
            result["reason"] = "settings_cleanup_incomplete"
            return result

    write_error = False
    if current is not value:
        try:
            writer(key, value)
        except Exception:
            write_error = True

    # Read after the public writer even when it raised: a partial/uncertain write
    # is never retried blindly or reported as successful.
    readback = gateway.settings_state(root=root, ctx=ctx)
    actual = readback.get("settings", {}).get(key)
    matches = readback.get("ok") is True and type(actual) is bool and actual is value
    if value is False and type(actual) is bool and actual is False and not pre_cleanup_done:
        cleanup = _retire_unexecuted(key, Path(state))

    # Reconcile the stored generation to the verified effective ON/OFF state
    # after the public writer. OFF itself was already fenced before the write.
    if key in {"service_enabled", "completion_notifications_enabled"} and readback.get("ok") is True:
        try:
            completion_allowed, _ = gateway.feature_status("completion_notifications_enabled", root=root, ctx=ctx)
            from . import completion_recovery
            completion_policy = completion_recovery.policy(state, enabled=completion_allowed)
            if completion_policy.get("ok") is not True:
                raise RuntimeError("completion_policy_unavailable")
        except Exception:
            cleanup["ok"] = False
            cleanup["reason"] = "cleanup_incomplete"
            cleanup.setdefault("outcomes", {})["completion_policy"] = {"ok": False,
                "reason": "cleanup_incomplete"}

    result = _status(ctx, root, state, read_only=False)
    result["write_status"] = "failed" if write_error else ("verified" if matches else "readback_mismatch")
    result["readback"] = {"key": key, "value": actual if type(actual) is bool else None}
    result["cleanup"] = cleanup
    result["ok"] = bool(matches and not write_error and cleanup.get("ok") is True and result["ok"])
    if write_error:
        result["reason"] = "native_settings_write_failed"
    elif not matches:
        result["reason"] = "settings_readback_mismatch"
    elif not cleanup.get("ok"):
        result["reason"] = "settings_cleanup_incomplete"
    elif not result["ok"]:
        result["reason"] = result["selection_reason"]
    return result


__all__ = ["VERSION", "settings_status", "settings_set"]
