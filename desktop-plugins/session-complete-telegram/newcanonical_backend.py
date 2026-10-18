"""Bounded plugin-side compatibility adapter for canonical approval RPC.

The only core symbols used are exported ``hermes_cli.gateway_client`` and
``hermes_cli.gateway_runtime`` utilities. This is a version/protocol-qualified
compatibility path, not a claim that those imports are in the plugin catalog's
stable documented-import allowlist. Missing or incompatible hosts fail closed.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any

PROTOCOL = "hermes-gateway-v1"
CAPABILITY = "session-authority-v1"
MAX_PROMPT_BYTES = 22_500
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
OBSERVE_BUDGET = 18.0
APPLY_BUDGET = 22.0
MAX_PROMPT_DEPTH = 32
PROMPT_FIELDS = frozenset({"kind", "prompt_id", "execution_generation", "command",
                           "description", "choices", "edit", "timeout_s"})
REQUIRED_PROMPT_FIELDS = frozenset({"kind", "prompt_id", "execution_generation", "command",
                                    "description", "choices"})
DESCRIPTOR_FIELDS = frozenset({
    "dispatch_mode", "owner", "connectionId", "profile", "targetProfile",
    "root_binding", "target_home_binding", "sid", "session_key", "instance_id",
    "authority_epoch", "replay_epoch", "transport_protocol", "prompt_id",
    "execution_generation", "request_fingerprint", "pending_fingerprint",
})


@dataclass(frozen=True)
class NativeAPI:
    """Only public, non-underscore native client/discovery exports."""
    protocol: str
    client_type: type
    rpc_error_type: type
    connect_gateway: Any
    discover_gateway_endpoint: Any


def load_compatibility() -> NativeAPI | None:
    """Return the known canonical WebSocket adapter, or ``None`` on unsupported hosts."""
    try:
        client_module = importlib.import_module("hermes_cli.gateway_client")
        runtime_module = importlib.import_module("hermes_cli.gateway_runtime")
        protocol = getattr(client_module, "GATEWAY_WS_PROTOCOL", None)
        client_type = getattr(client_module, "GatewayClient", None)
        rpc_error_type = getattr(client_module, "GatewayRPCError", None)
        connect_gateway = getattr(client_module, "connect_gateway", None)
        discover = getattr(runtime_module, "discover_gateway_endpoint", None)
        if (protocol != PROTOCOL or not isinstance(client_type, type) or
                not callable(getattr(client_type, "rpc", None)) or
                not isinstance(rpc_error_type, type) or not callable(connect_gateway) or
                not callable(discover)):
            return None
        return NativeAPI(protocol, client_type, rpc_error_type, connect_gateway, discover)
    except Exception:
        return None


def _json_value(value: object, depth: int = 0) -> bool:
    if depth > MAX_PROMPT_DEPTH:
        return False
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_value(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return all(type(key) is str and _json_value(item, depth + 1)
                   for key, item in value.items())
    return False


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8", errors="strict")).hexdigest()


def validate_prompt(prompt: object) -> bool:
    """Validate the current native ``CanonicalPendingPrompt`` shape without rewriting IDs."""
    if (not isinstance(prompt, dict) or not REQUIRED_PROMPT_FIELDS.issubset(prompt) or
            not set(prompt).issubset(PROMPT_FIELDS) or not _json_value(prompt) or
            prompt.get("kind") != "approval"):
        return False
    prompt_id = prompt.get("prompt_id")
    generation = prompt.get("execution_generation")
    if (type(prompt_id) is not str or not prompt_id or len(prompt_id) > 2048 or
            type(generation) is not int or generation < 1 or
            type(prompt.get("command")) is not str or not prompt["command"] or
            type(prompt.get("description")) is not str or not prompt["description"]):
        return False
    choices = prompt.get("choices")
    allowed = {"once", "session", "always", "deny"}
    if (not isinstance(choices, list) or len(choices) < 2 or len(choices) > 4 or
            any(type(choice) is not str or choice not in allowed for choice in choices) or
            len(set(choices)) != len(choices) or "once" not in choices or "deny" not in choices):
        return False
    if "edit" in prompt and not isinstance(prompt["edit"], dict):
        return False
    if "timeout_s" in prompt and (type(prompt["timeout_s"]) is not int or prompt["timeout_s"] < 0):
        return False
    try:
        return len(_canonical(prompt).encode("utf-8", errors="strict")) <= MAX_PROMPT_BYTES
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return False


def prompt_identity(prompt: object) -> tuple[str, int] | None:
    if not validate_prompt(prompt):
        return None
    # Native prompt IDs are opaque. No UUID/srq regex, trimming, or re-encoding.
    return prompt["prompt_id"], prompt["execution_generation"]


def root_binding(root: str | Path) -> str:
    resolved = Path(root).expanduser().resolve(strict=True)
    return hashlib.sha256(os.path.normcase(os.fspath(resolved)).encode("utf-8", errors="strict")).hexdigest()


def _path_binding(path: Path) -> str:
    return hashlib.sha256(os.path.normcase(os.fspath(path.resolve(strict=True))).encode(
        "utf-8", errors="strict")).hexdigest()


def _route(req: object, notifier, root: Path) -> tuple[dict[str, str], Path] | None:
    if not isinstance(req, dict):
        return None
    route = req.get("route")
    if (not isinstance(route, dict) or set(route) != {"mode", "connectionId", "profile", "targetProfile"} or
            route.get("mode") != "local" or
            any(type(route.get(key)) is not str or not route[key] for key in
                ("connectionId", "profile", "targetProfile"))):
        return None
    try:
        # Both names must resolve through the existing root/homes policy. The helper itself
        # still executes only in the default package; it never imports a named-home package.
        notifier.profile_home(route["profile"], root=root)
        target_home = notifier.profile_home(route["targetProfile"], root=root)
    except Exception:
        return None
    return route, target_home


def _validate_descriptor(descriptor: object) -> bool:
    if not isinstance(descriptor, dict) or set(descriptor) != DESCRIPTOR_FIELDS:
        return False
    string_fields = DESCRIPTOR_FIELDS - {"authority_epoch", "execution_generation"}
    if any(type(descriptor.get(key)) is not str or not descriptor[key] for key in string_fields):
        return False
    if (descriptor.get("dispatch_mode") != "canonical_native" or
            descriptor.get("transport_protocol") != PROTOCOL or
            type(descriptor.get("authority_epoch")) is not int or descriptor["authority_epoch"] < 1 or
            type(descriptor.get("execution_generation")) is not int or descriptor["execution_generation"] < 1):
        return False
    if descriptor.get("prompt_id") == "":
        return False
    for key in ("root_binding", "target_home_binding", "request_fingerprint", "pending_fingerprint"):
        value = descriptor[key]
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            return False
    try:
        return len(_canonical(descriptor).encode("utf-8")) <= 12_000
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return False


def _endpoint_parts(discovery: object, target_home: Path, root: Path) -> tuple[object, str | None]:
    if getattr(discovery, "state", None) != "ready":
        reason = getattr(discovery, "reason_code", None)
        return None, reason if isinstance(reason, str) else "owner_unavailable"
    endpoint = getattr(discovery, "endpoint", None)
    if endpoint is None:
        return None, "owner_unavailable"
    try:
        endpoint_home = Path(getattr(endpoint, "profile_id")).expanduser().resolve(strict=True)
        if endpoint_home != target_home.resolve(strict=True):
            return None, "profile_mismatch"
        instance_id = getattr(endpoint, "instance_id")
        authority_epoch = getattr(endpoint, "authority_epoch")
        runtime_protocol = getattr(endpoint, "runtime_protocol")
        capabilities = getattr(endpoint, "capabilities")
        control_home = getattr(endpoint, "control_home", None)
        if (type(instance_id) is not str or not instance_id or type(authority_epoch) is not int or
                authority_epoch < 1 or type(runtime_protocol) is not int or runtime_protocol != 1 or
                CAPABILITY not in capabilities):
            return None, "owner_identity_invalid"
        if control_home is not None and Path(control_home).expanduser().resolve(strict=True) != root.resolve(strict=True):
            return None, "foreign_control_home"
        return endpoint, None
    except Exception:
        return None, "owner_identity_invalid"


def _snapshot_size(snapshot: object) -> int | None:
    try:
        return len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"),
                              allow_nan=False).encode("utf-8", errors="strict"))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return None


def snapshot_matches(snapshot: object, descriptor: object, prompt: object) -> bool:
    """Compare a full native resume snapshot to the frozen review; history is never projected."""
    if not _validate_descriptor(descriptor) or not validate_prompt(prompt):
        return False
    if not isinstance(snapshot, dict):
        return False
    snapshot_size = _snapshot_size(snapshot)
    if snapshot_size is None or snapshot_size > MAX_SNAPSHOT_BYTES:
        return False
    generation = snapshot.get("execution_generation")
    epoch = snapshot.get("authority_epoch")
    prompts = snapshot.get("prompts")
    if (snapshot.get("session_id") != descriptor["sid"] or
            snapshot.get("stored_session_id") != descriptor["session_key"] or
            snapshot.get("running") is not True or type(epoch) is not int or
            epoch != descriptor["authority_epoch"] or
            snapshot.get("replay_epoch") != descriptor["replay_epoch"] or
            type(generation) is not int or generation != descriptor["execution_generation"] or
            not isinstance(snapshot.get("subscription_id"), str) or not snapshot["subscription_id"] or
            not isinstance(snapshot.get("info"), dict) or
            snapshot["info"].get("desktop_protocol") != PROTOCOL or
            not isinstance(prompts, list) or len(prompts) > 100):
        return False
    matches = [item for item in prompts if isinstance(item, dict) and
               item.get("prompt_id") == descriptor["prompt_id"]]
    if len(matches) != 1 or not validate_prompt(matches[0]):
        return False
    try:
        return (_canonical(matches[0]) == _canonical(prompt) and
                matches[0]["prompt_id"] == descriptor["prompt_id"] and
                matches[0]["execution_generation"] == descriptor["execution_generation"] and
                _digest(matches[0]) == descriptor["request_fingerprint"] and
                pending_matches(descriptor, matches[0], _pending_binding(
                    matches[0], descriptor["sid"], descriptor["instance_id"],
                    descriptor["authority_epoch"], descriptor["replay_epoch"])))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return False


def _pending_binding(prompt: dict, session_id: str, instance_id: str,
                     authority_epoch: int, replay_epoch: str) -> dict[str, object]:
    return {"session_id": session_id, "prompt_id": prompt["prompt_id"],
            "execution_generation": prompt["execution_generation"],
            "instance_id": instance_id, "authority_epoch": authority_epoch,
            "replay_epoch": replay_epoch}


def pending_matches(descriptor: object, prompt: object, pending: object) -> bool:
    if not _validate_descriptor(descriptor) or not validate_prompt(prompt) or not isinstance(pending, dict):
        return False
    expected = _pending_binding(prompt, descriptor["sid"], descriptor["instance_id"],
                                descriptor["authority_epoch"], descriptor["replay_epoch"])
    try:
        return pending == expected and _digest(pending) == descriptor["pending_fingerprint"]
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return False


def _build_descriptor(*, owner: str, route: dict[str, str], root: Path, target_home: Path,
                      session_id: str, session_key: str, endpoint: object,
                      replay_epoch: str, prompt: dict) -> tuple[dict, dict]:
    pending = _pending_binding(prompt, session_id, endpoint.instance_id,
                               endpoint.authority_epoch, replay_epoch)
    descriptor = {
        "dispatch_mode": "canonical_native", "owner": owner,
        "connectionId": route["connectionId"], "profile": route["profile"],
        "targetProfile": route["targetProfile"], "root_binding": root_binding(root),
        "target_home_binding": _path_binding(target_home), "sid": session_id,
        "session_key": session_key, "instance_id": endpoint.instance_id,
        "authority_epoch": endpoint.authority_epoch, "replay_epoch": replay_epoch,
        "transport_protocol": PROTOCOL, "prompt_id": prompt["prompt_id"],
        "execution_generation": prompt["execution_generation"],
        "request_fingerprint": _digest(prompt), "pending_fingerprint": _digest(pending),
    }
    return descriptor, pending


def _rpc_budget(deadline: float, requested: float = 8.0) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("native_helper_deadline")
    return max(0.05, min(requested, remaining))


async def _observe_once(api: NativeAPI, *, route: dict[str, str], target_home: Path,
                        root: Path, session_id: str, descriptor: dict | None = None,
                        prompt: dict | None = None, before_dispatch=None,
                        choice: str | None = None, deadline: float | None = None) -> dict:
    """Attach once to the explicit ready owner, read one snapshot, optionally respond once."""
    deadline = deadline or (time.monotonic() + OBSERVE_BUDGET)
    discovery = await asyncio.to_thread(api.discover_gateway_endpoint, target_home,
                                        timeout=min(2.0, _rpc_budget(deadline, 2.0)))
    endpoint, error = _endpoint_parts(discovery, target_home, root)
    if endpoint is None:
        return {"ok": False, "phase": "prewrite", "reason": error or "owner_unavailable"}
    if descriptor is not None and (
            endpoint.instance_id != descriptor["instance_id"] or
            endpoint.authority_epoch != descriptor["authority_epoch"]):
        return {"ok": False, "phase": "prewrite", "reason": "authority_changed"}
    subscription_id = None
    write_started = False
    result = None
    try:
        manager = api.connect_gateway(endpoint)
        async with asyncio.timeout(_rpc_budget(deadline, APPLY_BUDGET if choice else OBSERVE_BUDGET)):
            async with manager as client:
                snapshot = await client.rpc("session.resume", session_id=session_id,
                                           _timeout=_rpc_budget(deadline))
                if isinstance(snapshot, dict):
                    subscription_id = snapshot.get("subscription_id")
                snapshot_size = _snapshot_size(snapshot) if isinstance(snapshot, dict) else None
                if not isinstance(snapshot, dict):
                    result = {"ok": False, "phase": "prewrite", "reason": "snapshot_incomplete"}
                elif snapshot_size is None:
                    result = {"ok": False, "phase": "prewrite", "reason": "snapshot_unavailable"}
                elif snapshot_size > MAX_SNAPSHOT_BYTES:
                    result = {"ok": False, "phase": "prewrite", "reason": "snapshot_oversized"}
                elif descriptor is None:
                    if (type(snapshot.get("authority_epoch")) is not int or
                            type(snapshot.get("replay_epoch")) is not str or
                            not snapshot.get("replay_epoch") or
                            type(snapshot.get("stored_session_id")) is not str or
                            not snapshot.get("stored_session_id") or
                            not isinstance(snapshot.get("prompts"), list)):
                        result = {"ok": False, "phase": "prewrite", "reason": "snapshot_incomplete"}
                    else:
                        matches = [value for value in snapshot["prompts"]
                                   if isinstance(value, dict) and value.get("prompt_id") == prompt.get("prompt_id")]
                        if (len(matches) != 1 or not validate_prompt(matches[0]) or
                                _canonical(matches[0]) != _canonical(prompt)):
                            result = {"ok": False, "phase": "prewrite", "reason": "prompt_stale"}
                        else:
                            desc, pending = _build_descriptor(
                                owner=route["owner"], route={key: route[key] for key in
                                    ("mode", "connectionId", "profile", "targetProfile")},
                                root=root, target_home=target_home, session_id=session_id,
                                session_key=snapshot["stored_session_id"], endpoint=endpoint,
                                replay_epoch=snapshot["replay_epoch"], prompt=matches[0])
                            if not snapshot_matches(snapshot, desc, matches[0]):
                                result = {"ok": False, "phase": "prewrite", "reason": "snapshot_stale"}
                            else:
                                result = {"ok": True, "descriptor": desc, "request": matches[0],
                                          "pending": pending}
                elif not snapshot_matches(snapshot, descriptor, prompt):
                    result = {"ok": False, "phase": "prewrite", "reason": "snapshot_stale"}
                elif choice is None:
                    pending = _pending_binding(prompt, session_id, endpoint.instance_id,
                                               endpoint.authority_epoch, descriptor["replay_epoch"])
                    if _digest(pending) != descriptor["pending_fingerprint"]:
                        result = {"ok": False, "phase": "prewrite", "reason": "binding_changed"}
                    else:
                        result = {"ok": True, "id": descriptor.get("approval_id"),
                                  "request_fingerprint": descriptor["request_fingerprint"],
                                  "pending_fingerprint": descriptor["pending_fingerprint"],
                                  "authority": _digest(descriptor)}
                else:
                    if choice not in {"once", "deny"} or before_dispatch is None:
                        result = {"ok": False, "phase": "prewrite", "reason": "invalid_choice"}
                    else:
                        gate = before_dispatch()
                        if not isinstance(gate, dict) or gate.get("ok") is not True:
                            result = {"ok": False, "phase": "prewrite",
                                      "reason": gate.get("reason", "claim_lost") if isinstance(gate, dict) else "claim_lost"}
                        else:
                            # Durable SQLite CAS has committed. Any exception from this
                            # point onward is terminal unknown; this is the one native write.
                            write_started = True
                            receipt = await client.rpc(
                                "approval.respond", session_id=descriptor["sid"],
                                execution_generation=descriptor["execution_generation"],
                                prompt_id=descriptor["prompt_id"], choice=choice,
                                _timeout=_rpc_budget(deadline, 8.0))
                            if (isinstance(receipt, dict) and set(receipt) == {"status", "prompt_id"} and
                                    receipt.get("prompt_id") == descriptor["prompt_id"] and
                                    receipt.get("status") == "resolved"):
                                result = {"ok": True, "phase": "postwrite", "status": "applied",
                                          "receipt": "decision_resolved"}
                            elif (isinstance(receipt, dict) and set(receipt) == {"status", "prompt_id"} and
                                  receipt.get("prompt_id") == descriptor["prompt_id"] and
                                  receipt.get("status") == "already_resolved"):
                                result = {"ok": False, "phase": "postwrite", "status": "expired",
                                          "reason": "already_resolved"}
                            else:
                                result = {"ok": False, "phase": "postwrite", "status": "unknown",
                                          "reason": "response_unverified"}
                if subscription_id and isinstance(subscription_id, str):
                    owned_subscription = subscription_id
                    subscription_id = None
                    # Detach while the acknowledged connection is still open. The server also
                    # releases this actor's subscription on socket close if the ACK was lost.
                    try:
                        await client.rpc("session.detach", session_id=session_id,
                                         subscription_id=owned_subscription,
                                         _timeout=_rpc_budget(deadline, 2.0))
                    except Exception:
                        pass
    except api.rpc_error_type as exc:
        reason = str(exc)
        if write_started:
            result = {"ok": False, "phase": "postwrite", "status": "rejected"
                      if reason in {"permission_denied", "profile_mismatch", "stale_generation",
                                    "runtime_retired", "not_found", "invalid_params"}
                      else "unknown", "reason": reason if reason.isidentifier() else "request_failed"}
        else:
            result = {"ok": False, "phase": "prewrite", "reason": reason if reason.isidentifier() else "request_failed"}
    except (asyncio.TimeoutError, TimeoutError):
        result = {"ok": False, "phase": "postwrite" if write_started else "prewrite",
                  "status": "unknown" if write_started else "rejected", "reason": "native_helper_deadline"}
    except Exception:
        result = {"ok": False, "phase": "postwrite" if write_started else "prewrite",
                  "status": "unknown" if write_started else "rejected", "reason": "native_transport_unavailable"}
    if result is None:
        result = {"ok": False, "phase": "prewrite", "reason": "snapshot_unavailable"}
    return result


def _run(coro) -> dict:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(coro)
        except Exception:
            return {"ok": False, "phase": "prewrite", "reason": "native_transport_unavailable"}
    coro.close()
    return {"ok": False, "phase": "prewrite", "reason": "async_runtime_unavailable"}


def observe(req: object, *, root: str | Path, notifier) -> dict:
    """Fresh read-only canonical attach for a Desktop ``approval.request`` candidate."""
    if (not isinstance(req, dict) or set(req) != {"action", "owner", "route", "session_id", "prompt"} or
            req.get("action") != "canonical_approval_observe" or
            type(req.get("owner")) is not str or not req["owner"] or len(req["owner"]) > 128 or
            type(req.get("session_id")) is not str or not req["session_id"] or len(req["session_id"]) > 512 or
            not validate_prompt(req.get("prompt"))):
        return {"ok": False, "phase": "prewrite", "reason": "invalid_native_candidate"}
    try:
        resolved_root = Path(root).expanduser().resolve(strict=True)
    except Exception:
        return {"ok": False, "phase": "prewrite", "reason": "default_home_required"}
    resolved = _route({"route": req["route"]}, notifier, resolved_root)
    if resolved is None:
        return {"ok": False, "phase": "prewrite", "reason": "profile_mismatch"}
    route, target_home = resolved
    route = {**route, "owner": req["owner"]}
    api = load_compatibility()
    if api is None:
        return {"ok": False, "phase": "prewrite", "reason": "canonical_api_unsupported"}
    return _run(_observe_once(api, route=route, target_home=target_home, root=resolved_root,
                              session_id=req["session_id"], prompt=req["prompt"]))


def observe_bound(descriptor: object, prompt: object, *, root: str | Path,
                  notifier) -> dict:
    """Recheck a persisted native card using its stored full binding."""
    if not _validate_descriptor(descriptor) or not validate_prompt(prompt):
        return {"ok": False, "phase": "prewrite", "reason": "invalid_descriptor"}
    try:
        resolved_root = Path(root).expanduser().resolve(strict=True)
        resolved = _route({"route": {"mode": "local", "connectionId": descriptor["connectionId"],
            "profile": descriptor["profile"], "targetProfile": descriptor["targetProfile"]}},
            notifier, resolved_root)
        if resolved is None:
            return {"ok": False, "phase": "prewrite", "reason": "profile_mismatch"}
        route, target_home = resolved
        if (root_binding(resolved_root) != descriptor["root_binding"] or
                _path_binding(target_home) != descriptor["target_home_binding"]):
            return {"ok": False, "phase": "prewrite", "reason": "root_binding_changed"}
    except Exception:
        return {"ok": False, "phase": "prewrite", "reason": "default_home_required"}
    api = load_compatibility()
    if api is None:
        return {"ok": False, "phase": "prewrite", "reason": "canonical_api_unsupported"}
    return _run(_observe_once(api, route=route, target_home=target_home, root=resolved_root,
                              session_id=descriptor["sid"], descriptor=descriptor, prompt=prompt))


def apply_bound(descriptor: object, prompt: object, *, choice: str,
                before_dispatch, root: str | Path, notifier,
                deadline_monotonic: float) -> dict:
    """Revalidate, CAS dispatching, then issue exactly one frozen four-field response."""
    if (not _validate_descriptor(descriptor) or not validate_prompt(prompt) or
            choice not in {"once", "deny"} or not callable(before_dispatch)):
        return {"ok": False, "phase": "prewrite", "status": "rejected", "reason": "invalid_native_claim"}
    try:
        resolved_root = Path(root).expanduser().resolve(strict=True)
        resolved = _route({"route": {"mode": "local", "connectionId": descriptor["connectionId"],
            "profile": descriptor["profile"], "targetProfile": descriptor["targetProfile"]}},
            notifier, resolved_root)
        if resolved is None:
            return {"ok": False, "phase": "prewrite", "status": "rejected", "reason": "profile_mismatch"}
        route, target_home = resolved
        if (root_binding(resolved_root) != descriptor["root_binding"] or
                _path_binding(target_home) != descriptor["target_home_binding"]):
            return {"ok": False, "phase": "prewrite", "status": "rejected", "reason": "root_binding_changed"}
    except Exception:
        return {"ok": False, "phase": "prewrite", "status": "rejected", "reason": "default_home_required"}
    api = load_compatibility()
    if api is None:
        return {"ok": False, "phase": "prewrite", "status": "rejected", "reason": "canonical_api_unsupported"}
    return _run(_observe_once(api, route=route, target_home=target_home, root=resolved_root,
                              session_id=descriptor["sid"], descriptor=descriptor, prompt=prompt,
                              before_dispatch=before_dispatch, choice=choice,
                              deadline=deadline_monotonic))


__all__ = ["PROTOCOL", "CAPABILITY", "MAX_SNAPSHOT_BYTES", "NativeAPI", "load_compatibility",
           "validate_prompt", "prompt_identity", "root_binding", "snapshot_matches", "observe",
           "observe_bound", "apply_bound"]
