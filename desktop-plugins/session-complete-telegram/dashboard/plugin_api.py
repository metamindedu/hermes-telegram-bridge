"""Fixed namespace adapter; native dashboard middleware owns auth/home/secret scope.

No CLI/process launch, fabricated PluginContext, credential-file fallback or poller.
The dispatcher is the actual register(ctx)-bound module, found by physical origin.
This native-loader seam is feature-detected and must be qualified per host version.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import threading

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

TRANSPORT = 'desktop-telegram-api-v1'
PLUGIN = 'session-complete-telegram'
MAX_BODY = 30_100
MAX_RESULT = 24 * 1024
router = APIRouter()
_slots = threading.BoundedSemaphore(4)


class _SlotLease:
    """Cancellation cannot free capacity while a dispatched thread still runs."""
    def __init__(self):
        self.lock = threading.Lock()
        self.state = 'reserved'

    def run(self, token):
        with self.lock:
            if self.state != 'reserved':
                return _envelope({'ok': False, 'reason': 'native_api_cancelled'}, False, 503)
            self.state = 'started'
        try:
            return _dispatch(token)
        finally:
            with self.lock:
                self.state = 'retired'
                _slots.release()

    def cancel_unstarted(self):
        with self.lock:
            if self.state == 'reserved':
                self.state = 'cancelled'
                _slots.release()


def _checked_package_root() -> Path:
    file = Path(__file__).resolve(strict=True)
    package = file.parent.parent
    if file.parent.name != 'dashboard' or package.name != PLUGIN or package.parent.name != 'plugins':
        raise RuntimeError('native_api_location_invalid')
    home = package.parent.parent.resolve(strict=True)
    if home.parent.name.casefold() == 'profiles':
        raise RuntimeError('named_profile_home_refused')
    from hermes_constants import get_hermes_home
    from hermes_cli.profiles import get_active_profile_name
    if Path(get_hermes_home()).resolve(strict=True) != home or get_active_profile_name() != 'default':
        raise RuntimeError('native_api_foreign_home')
    return package


def _registered_package(package: Path):
    # Public discovery is idempotent and home-scoped. Do not force reload,
    # inspect private registries, fabricate contexts, or alias native modules.
    origin = (package / '__init__.py').resolve(strict=True)
    found = _physical_registrations(origin)
    if not found:
        # Cold discovery only. A warm request must not join an unrelated
        # background discovery or repeatedly pay global loader startup.
        from hermes_cli.plugins import discover_plugins
        discover_plugins()
        found = _physical_registrations(origin)
    if len(found) != 1:
        raise RuntimeError('native_api_registration_unavailable')
    return found[0]


def _physical_registrations(origin):
    found = []
    for name, module in tuple(sys.modules.items()):
        if not name.startswith('hermes_plugins.') or module is None:
            continue
        file = getattr(module, '__file__', None)
        if not isinstance(file, str):
            continue
        try:
            same = Path(file).resolve(strict=True) == origin
        except (OSError, RuntimeError):
            continue
        if same and getattr(module, 'API_TRANSPORT', None) == TRANSPORT and \
                callable(getattr(module, 'dispatch_api_request', None)) and \
                callable(getattr(module, 'api_context_ready', None)):
            found.append(module)
    return found


def _reject_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('nonfinite_json')


def _envelope(result, dispatched, status=200):
    return JSONResponse({'transport': TRANSPORT, 'dispatched': dispatched, 'result': result}, status_code=status)


def _capability():
    module = _registered_package(_checked_package_root())
    if not module.api_context_ready():
        raise RuntimeError('native_api_context_unavailable')
    return {'ok': True, 'transport': TRANSPORT, 'plugin': PLUGIN, 'profile': 'default'}


@router.get('/bridge/v1/capabilities')
def capabilities(request: Request):
    try:
        _check_query_owner(request)
        return _capability()
    except Exception:
        # No command dispatch, private path, provider value or exception detail.
        return JSONResponse({'ok': False, 'reason': 'native_api_unavailable'}, status_code=503)


def _check_query_owner(request: Request):
    pairs = list(request.query_params.multi_items())
    if pairs and pairs != [('profile', 'default')]:
        raise RuntimeError('native_api_query_owner_mismatch')


def _dispatch(token):
    try:
        module = _registered_package(_checked_package_root())
    except Exception:
        return _envelope({'ok': False, 'reason': 'native_api_unavailable'}, False, 503)
    try:
        result = module.dispatch_api_request(token)
        if not isinstance(result, dict) or not isinstance(result.get('ok'), bool):
            raise ValueError('invalid_result')
        if result.get('reason') in {'invalid_helper_result', 'helper_result_too_large'}:
            # The shared CLI sanitizer could not retain a post-dispatch result.
            # Do not turn that lost receipt into a known non-writing rejection.
            raise ValueError('sanitized_result_unavailable')
        encoded = json.dumps(result, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf8')
        if len(encoded) > MAX_RESULT:
            raise ValueError('result_too_large')
        return _envelope(result, True)
    except Exception:
        # Once invoked, a missing/invalid result is an uncertain outcome. Never
        # advertise false dispatched or silently try the CLI transport.
        return _envelope({'ok': False, 'reason': 'native_api_result_unavailable'}, True, 500)


@router.post('/bridge/v1/dispatch')
async def dispatch(request: Request):
    try:
        _check_query_owner(request)
    except RuntimeError:
        return _envelope({'ok': False, 'reason': 'native_api_unavailable'}, False, 503)
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
        return _envelope({'ok': False, 'reason': 'json_content_type_required'}, False, 415)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BODY:
            return _envelope({'ok': False, 'reason': 'request_too_large'}, False, 413)
        raw.extend(chunk)
    try:
        value = json.loads(raw.decode('utf8', errors='strict'), object_pairs_hook=_reject_pairs,
                           parse_constant=_reject_constant)
        if not isinstance(value, dict) or set(value) != {'request'} or \
                not isinstance(value['request'], str) or not value['request'] or len(value['request']) > 30_000:
            raise ValueError('invalid_request')
    except (ValueError, UnicodeError, RecursionError):
        return _envelope({'ok': False, 'reason': 'invalid_request'}, False, 400)
    if not _slots.acquire(blocking=False):
        return _envelope({'ok': False, 'reason': 'native_api_busy'}, False, 503)
    lease = _SlotLease()
    try:
        # Native request ContextVars propagate to the framework's threadpool.
        # No process-global home/scope mutation and no owned unbounded worker.
        return await run_in_threadpool(lease.run, value['request'])
    finally:
        lease.cancel_unstarted()
