"""Native Hermes CLI entry for the app-level completion notifier."""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import math
import re
import sys
from typing import Any

from .runtime import PLUGIN_ID, resolve_package_home

MAX_REQUEST_BYTES = 22_500
MAX_REQUEST_TOKEN_CHARS = 30_000
MAX_RESULT_BYTES = 24 * 1024
FRAME_BEGIN = "HERMES_TELEGRAM_BRIDGE_RESULT_V1_BEGIN"
FRAME_END = "HERMES_TELEGRAM_BRIDGE_RESULT_V1_END"
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError("invalid_json_constant")


def _parse_request(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value or len(value) > MAX_REQUEST_TOKEN_CHARS or not _BASE64URL.fullmatch(value):
        raise ValueError("invalid_request")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
        if len(raw) > MAX_REQUEST_BYTES or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value:
            raise ValueError("invalid_request")
        request = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object,
                             parse_constant=_reject_constant)
    except (UnicodeError, ValueError, binascii.Error, RecursionError) as exc:
        raise ValueError("invalid_request") from exc
    if not isinstance(request, dict) or not isinstance(request.get("action"), str):
        raise ValueError("invalid_request")
    return request


def _validate_json_value(value: object, depth: int = 0) -> None:
    if depth > 64:
        raise ValueError("json_depth")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non_finite_number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth + 1)
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("non_string_key")
        for item in value.values():
            _validate_json_value(item, depth + 1)
        return
    raise ValueError("unsupported_json_value")


def _safe_result(result: object) -> dict[str, Any]:
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        return {"ok": False, "reason": "invalid_helper_result"}
    try:
        _validate_json_value(result)
        raw = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        return {"ok": False, "reason": "invalid_helper_result"}
    if len(raw) > MAX_RESULT_BYTES:
        return {"ok": False, "reason": "helper_result_too_large"}
    return result


def _write_frame(result: object) -> None:
    value = _safe_result(result)
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    sys.stdout.write(f"{FRAME_BEGIN}\n{encoded}\n{FRAME_END}\n")
    sys.stdout.flush()


def _setup_cli(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--request", required=True, help=argparse.SUPPRESS)


def _run_cli(args: argparse.Namespace) -> None:
    try:
        home = resolve_package_home(__file__)
        request = _parse_request(getattr(args, "request", None))
        from . import notifier

        result = notifier.dispatch_request(request, root=home, native=True, result_budget=MAX_RESULT_BYTES)
    except Exception as exc:
        # Exception text is never returned: it may include URLs or credentials.
        result = {"ok": False, "reason": type(exc).__name__}
    _write_frame(result)


def register(ctx) -> None:
    """Register only the fixed CLI action dispatcher in this home."""
    if getattr(ctx, "plugin_id", None) != PLUGIN_ID:
        raise RuntimeError("native_plugin_owner_mismatch")
    resolve_package_home(__file__)
    ctx.register_cli_command(
        name="desktop-telegram-bridge",
        help="Deliver Hermes Desktop notification actions to the configured Telegram home",
        setup_fn=_setup_cli,
        handler_fn=_run_cli,
        description="Fixed local bridge command used by the Session Completion → Telegram Desktop plugin.",
    )


__all__ = ["register"]
