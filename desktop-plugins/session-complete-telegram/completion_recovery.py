"""Durable, metadata-only recovery for freshly observed completion notices.

The existing deliveries ledger remains the send/deduplication authority. This
module adds only a bounded candidate state machine in the same SQLite file; it
never stores notification text, titles, previews, Telegram destinations, or
credentials.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator

VERSION = "1.8.1"
SCHEMA_VERSION = 1
TTL_SECONDS = 30 * 60
QUIET_SECONDS = 60
MAX_ACTIVE = 256
MAX_TOTAL = 1024
CLAIM_SECONDS = 20
MAX_PAGE_ITEMS = 16
MAX_RESULT_BYTES = 24 * 1024

ACTIVE = ("pending", "read_blocked", "claimed", "unsent_retryable", "dispatching")
PRE_DISPATCH = ("pending", "read_blocked", "claimed", "unsent_retryable")
TERMINAL = ("sent", "unknown", "rejected", "cancelled", "expired", "disabled")
ALL_STATES = frozenset((*ACTIVE, *TERMINAL))
REASONS = frozenset({
    "", "awaiting_snapshot", "snapshot_unavailable", "snapshot_incomplete",
    "runtime_absent", "identity_changed", "new_work", "settings_disabled",
    "ttl_expired", "candidate_limit", "storage_full", "delivery_interference",
    "existing_delivery_sent", "existing_delivery_unknown", "existing_delivery_failed",
    "connect_failure", "bot_api_rejected", "ambiguous_transport", "claim_expired",
    "generation_changed", "admission_fence_active",
    "quiet_period", "source_storage_unavailable", "source_identity_unavailable",
    "credentials_unavailable", "recovery_storage_unavailable", "attention_required",
    "completion_disabled", "invalid_recovery_cursor", "recovery_page_item_too_large",
})
PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
GENERATION_RE = re.compile(r"^[0-9a-f]{32}$")
TABLE = "completion_candidates"
META_TABLE = "completion_recovery_meta"

_COLUMNS = (
    "candidate_id", "schema_version", "connection_id", "profile", "target_profile",
    "resolved_profile", "runtime_id", "session_key", "started_at", "epoch", "generation",
    "seq", "bookkeeping_seq", "final_row_id",
    "created", "quiet_at", "expires", "source_binding", "destination_binding",
    "state", "revision", "reason", "claim_token", "claim_until", "dispatch_token",
    "proof_attempt", "updated",
)
_CREATE = """CREATE TABLE IF NOT EXISTS completion_candidates (
    candidate_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    connection_id TEXT NOT NULL DEFAULT '',
    profile TEXT NOT NULL DEFAULT '',
    target_profile TEXT NOT NULL DEFAULT '',
    resolved_profile TEXT NOT NULL DEFAULT '',
    runtime_id TEXT NOT NULL DEFAULT '',
    session_key TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    epoch TEXT NOT NULL DEFAULT '',
    generation TEXT NOT NULL DEFAULT '',
    seq INTEGER NOT NULL DEFAULT 0,
    bookkeeping_seq INTEGER NOT NULL DEFAULT 0,
    final_row_id INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    quiet_at REAL NOT NULL,
    expires REAL NOT NULL,
    source_binding TEXT NOT NULL DEFAULT '',
    destination_binding TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    revision INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    claim_token TEXT,
    claim_until REAL,
    dispatch_token TEXT,
    proof_attempt TEXT,
    updated REAL NOT NULL
)"""
_CREATE_META = f"""CREATE TABLE IF NOT EXISTS {META_TABLE} (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    generation TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    fence_until REAL NOT NULL DEFAULT 0
)"""


class RecoveryError(RuntimeError):
    """A fixed-code recovery storage or input failure (no private values)."""


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


@contextlib.contextmanager
def _connection(state: str | Path, *, create: bool) -> Iterator[sqlite3.Connection | None]:
    path = Path(state) / "notifications.sqlite3"
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        yield None
        return
    try:
        if create:
            c = sqlite3.connect(path, timeout=5, isolation_level=None, factory=ClosingConnection)
        else:
            c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2,
                                isolation_level=None, factory=ClosingConnection)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=5000")
        if not create:
            c.execute("PRAGMA query_only=ON")
        try:
            yield c
        finally:
            c.close()
    except sqlite3.Error as exc:
        raise RecoveryError("recovery_database_unavailable") from exc


def _ensure_schema(c: sqlite3.Connection) -> None:
    try:
        c.execute(_CREATE)
        actual = tuple(row[1] for row in c.execute(f"PRAGMA table_info({TABLE})"))
        if actual != _COLUMNS:
            raise RecoveryError("recovery_schema_mismatch")
        c.execute(_CREATE_META)
        c.execute("CREATE INDEX IF NOT EXISTS completion_candidates_state_expiry ON completion_candidates(state, expires)")
        c.execute("CREATE INDEX IF NOT EXISTS completion_candidates_updated ON completion_candidates(updated)")
        c.execute("CREATE INDEX IF NOT EXISTS completion_candidates_route_key ON completion_candidates(connection_id, profile, target_profile, candidate_id)")
    except sqlite3.Error as exc:
        raise RecoveryError("recovery_schema_unavailable") from exc


def _safe_text(value: object, *, name: str, maximum: int, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise RecoveryError("invalid_recovery_identity")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise RecoveryError("invalid_recovery_identity")
    return value


def _number(value: object, *, name: str, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecoveryError("invalid_recovery_identity")
    result = float(value)
    if not (minimum <= result < float("inf")):
        raise RecoveryError("invalid_recovery_identity")
    return result


def _route(route: object) -> dict[str, str]:
    if not isinstance(route, dict) or set(route) != {"mode", "connectionId", "profile", "targetProfile"}:
        raise RecoveryError("invalid_recovery_route")
    if route.get("mode") != "local":
        raise RecoveryError("recovery_local_route_required")
    return {
        "connection_id": _safe_text(route.get("connectionId"), name="connectionId", maximum=128),
        "profile": _safe_text(route.get("profile"), name="profile", maximum=80, pattern=PROFILE_RE),
        "target_profile": _safe_text(route.get("targetProfile"), name="targetProfile", maximum=80, pattern=PROFILE_RE),
    }


def _identity(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RecoveryError("invalid_recovery_identity")
    route = _route(value.get("route"))
    seq = value.get("seq")
    final_id = value.get("final_row_id")
    if type(seq) is not int or seq < 1 or type(final_id) is not int or final_id < 1:
        raise RecoveryError("invalid_recovery_identity")
    generation = _safe_text(value.get("generation"), name="generation", maximum=32,
                            pattern=GENERATION_RE)
    return {
        **route,
        "runtime_id": _safe_text(value.get("runtime_id"), name="runtime_id", maximum=200),
        "session_key": _safe_text(value.get("session_key"), name="session_key", maximum=200),
        "started_at": _safe_text(value.get("started_at"), name="started_at", maximum=100),
        "epoch": _safe_text(value.get("epoch"), name="epoch", maximum=100),
        "generation": generation,
        "seq": seq,
        "final_row_id": final_id,
    }


def _source_binding(root: str | Path) -> str:
    try:
        resolved = str(Path(root).expanduser().resolve(strict=True))
    except OSError as exc:
        raise RecoveryError("source_storage_unavailable") from exc
    except Exception as exc:
        raise RecoveryError("source_identity_unavailable") from exc
    return hashlib.sha256(resolved.casefold().encode("utf-8", errors="strict")).hexdigest()


def _retire_rows(c: sqlite3.Connection, now: float) -> int:
    return int(c.execute("""UPDATE completion_candidates SET state='disabled',revision=revision+1,
        reason='settings_disabled',claim_token=NULL,claim_until=NULL,updated=?
        WHERE state IN ('pending','read_blocked','claimed','unsent_retryable')""", (now,)).rowcount)


def _policy_generation(c: sqlite3.Connection, now: float, enabled: bool, *, force_rotate: bool = False) -> str:
    row = c.execute(f"SELECT generation,enabled FROM {META_TABLE} WHERE singleton=1").fetchone()
    if row is None:
        generation = secrets.token_hex(16)
        c.execute(f"INSERT INTO {META_TABLE}(singleton,generation,enabled,fence_until) VALUES(1,?,?,0)",
                  (generation, int(enabled)))
        if not enabled:
            _retire_rows(c, now)
        return generation
    current = bool(row["enabled"])
    if current != enabled or force_rotate:
        generation = secrets.token_hex(16)
        c.execute(f"UPDATE {META_TABLE} SET generation=?,enabled=? WHERE singleton=1",
                  (generation, int(enabled)))
        if not enabled:
            _retire_rows(c, now)
        return generation
    return str(row["generation"])


def policy(state: str | Path, *, enabled: bool, now: float | None = None,
           force_rotate: bool = False) -> dict[str, Any]:
    """Read/advance the persisted admission generation without enabling settings."""
    if type(enabled) is not bool or type(force_rotate) is not bool:
        raise RecoveryError("invalid_recovery_policy")
    now = time.time() if now is None else _number(now, name="now")
    with _connection(state, create=True) as c:
        assert c is not None
        _ensure_schema(c)
        c.execute("BEGIN IMMEDIATE")
        try:
            generation = _policy_generation(c, now, enabled, force_rotate=force_rotate)
            c.commit()
            return {"ok": True, "enabled": enabled, "generation": generation}
        except Exception:
            c.rollback()
            raise


def destination_binding_for(token: str, chat: int, topic: int | None) -> str:
    if not isinstance(token, str) or not token or type(chat) is not int or (topic is not None and type(topic) is not int):
        raise RecoveryError("credentials_unavailable")
    message = json.dumps([chat, topic], separators=(",", ":")).encode("ascii")
    # The bot token is only an HMAC key in memory; neither it nor the raw
    # destination is persisted or returned by this module.
    return hmac.new(token.encode("utf-8"), b"desktop-completion-recovery-v1\\0" + message,
                    hashlib.sha256).hexdigest()


def _destination_binding(root: str | Path) -> str:
    try:
        from . import notifier
    except ImportError:
        import notifier
    try:
        token, chat, topic = notifier.credentials(Path(root))
        return destination_binding_for(token, chat, topic)
    except Exception as exc:
        raise RecoveryError("credentials_unavailable") from exc


def _resolve_source(root: str | Path, identity: dict[str, Any]) -> tuple[str, str]:
    try:
        from . import notifier
    except ImportError:
        import notifier
    resolved = notifier.resolve_recovery_source(identity, root=root)
    if not isinstance(resolved, dict) or resolved.get("ok") is not True:
        reason = resolved.get("reason") if isinstance(resolved, dict) else None
        if reason in {"source_storage_unavailable", "source_identity_unavailable"}:
            raise RecoveryError(reason)
        raise RecoveryError("source_identity_unavailable")
    profile = _safe_text(resolved.get("profile"), name="resolved_profile", maximum=80,
                         pattern=PROFILE_RE)
    binding = _safe_text(resolved.get("binding"), name="source_binding", maximum=64,
                         pattern=HEX64_RE)
    return profile, binding


def _candidate_id(source_binding: str, identity: dict[str, Any]) -> str:
    natural = [source_binding, identity["connection_id"], identity["profile"],
               identity["target_profile"], identity["runtime_id"], identity["final_row_id"]]
    return hashlib.sha256(json.dumps(natural, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _route_key(route: dict[str, str], runtime_id: str, final_row_id: int) -> tuple[Any, ...]:
    return (route["connection_id"], route["profile"], route["target_profile"], runtime_id, final_row_id)


def _route_fence_id(route: dict[str, str], runtime_id: str, final_row_id: int) -> str:
    raw = ["route-fence", *_route_key(route, runtime_id, final_row_id)]
    return hashlib.sha256(json.dumps(raw, separators=(",", ":")).encode("utf-8")).hexdigest()


def candidate_id(root: str | Path, identity: object) -> str:
    """Return the opaque deterministic key for the native-resolved source."""
    normalized = _identity(identity)
    _profile, source = _resolve_source(root, normalized)
    return _candidate_id(source, normalized)


def _terminalize(c: sqlite3.Connection, row: sqlite3.Row, state: str, reason: str, now: float) -> None:
    if state not in TERMINAL or reason not in REASONS:
        raise RecoveryError("invalid_recovery_transition")
    if row["state"] in TERMINAL:
        return
    if row["state"] == "dispatching":
        # A write may already be in flight. It cannot be cancelled or replayed.
        return
    c.execute("""UPDATE completion_candidates SET state=?,revision=revision+1,reason=?,
        claim_token=NULL,claim_until=NULL,updated=? WHERE candidate_id=? AND revision=?""",
        (state, reason, now, row["candidate_id"], row["revision"]))


def _cleanup(c: sqlite3.Connection, now: float, *, route: dict[str, str] | None = None,
             candidate_id_value: str | None = None) -> None:
    """Reclaim only elapsed metadata in the optional scope; live fences remain."""
    clauses: list[str] = []
    params: list[Any] = []
    if route is not None:
        clauses.extend(("connection_id=?", "profile=?", "target_profile=?"))
        params.extend((route["connection_id"], route["profile"], route["target_profile"]))
    if candidate_id_value is not None:
        clauses.append("candidate_id=?")
        params.append(candidate_id_value)
    scope = " AND " + " AND ".join(clauses) if clauses else ""
    c.execute("""UPDATE completion_candidates SET state=CASE WHEN state='dispatching' THEN 'unknown' ELSE 'expired' END,
        revision=revision+1,reason='ttl_expired',claim_token=NULL,claim_until=NULL,dispatch_token=NULL,updated=?
        WHERE state IN ('pending','read_blocked','claimed','unsent_retryable','dispatching') AND expires<=?""" + scope,
        (now, now, *params))
    c.execute("DELETE FROM completion_candidates WHERE state IN ('sent','unknown','rejected','cancelled','expired','disabled') AND expires<=?" + scope,
              (now, *params))


def _compare_identity(row: sqlite3.Row, identity: dict[str, Any]) -> bool:
    fields = ("connection_id", "profile", "target_profile", "runtime_id", "session_key",
              "started_at", "epoch", "final_row_id")
    return all(row[field] == identity[field] for field in fields)


def record(state: str | Path, root: str | Path, request: object, *, now: float | None = None) -> dict[str, Any]:
    """Admit one fresh completion under the generation and resolved-source fence."""
    now = time.time() if now is None else _number(now, name="now")
    identity = _identity(request)
    created = _number(request.get("created_at"), name="created_at") if isinstance(request, dict) else 0
    quiet = _number(request.get("quiet_at", created), name="quiet_at") if isinstance(request, dict) else 0
    if created > now + 30 or now - created > TTL_SECONDS or quiet < created or quiet > now + 30 or quiet > created + TTL_SECONDS:
        return {"ok": False, "reason": "candidate_expired"}
    resolved_profile, source = _resolve_source(root, identity)
    destination = _destination_binding(root)
    ident = _candidate_id(source, identity)
    expiry = created + TTL_SECONDS
    with _connection(state, create=True) as c:
        assert c is not None
        _ensure_schema(c)
        c.execute("BEGIN IMMEDIATE")
        try:
            # Admission capacity is global. Reclaim only elapsed metadata from
            # every route; live identities/fences and deliveries are untouched.
            _cleanup(c, now)
            meta = c.execute(f"SELECT generation,enabled,fence_until FROM {META_TABLE} WHERE singleton=1").fetchone()
            if meta is None or not bool(meta["enabled"]):
                result = {"ok": False, "reason": "settings_disabled"}
            elif meta["generation"] != identity["generation"]:
                result = {"ok": False, "reason": "generation_changed"}
            elif float(meta["fence_until"]) > now:
                result = {"ok": False, "reason": "admission_fence_active"}
            else:
                current_profile, current_source = _resolve_source(root, identity)
                if current_profile != resolved_profile or current_source != source:
                    result = {"ok": False, "reason": "identity_changed"}
                else:
                    route_fence = c.execute("""SELECT state,reason,revision FROM completion_candidates
                        WHERE connection_id=? AND profile=? AND target_profile=? AND runtime_id=? AND final_row_id=?
                          AND source_binding='' AND state IN ('cancelled','disabled','expired','rejected','sent','unknown')
                        ORDER BY candidate_id LIMIT 1""",
                        _route_key(identity, identity["runtime_id"], identity["final_row_id"])).fetchone()
                    existing = c.execute("SELECT * FROM completion_candidates WHERE candidate_id=?", (ident,)).fetchone()
                    if route_fence:
                        result = {"ok": False, "reason": route_fence["reason"] or "candidate_terminal",
                                  "candidate_id": ident, "state": route_fence["state"],
                                  "revision": route_fence["revision"], "generation": identity["generation"],
                                  "bookkeeping_seq": identity["seq"]}
                    elif existing:
                        if existing["state"] in TERMINAL:
                            result = {"ok": False, "reason": existing["reason"] or "candidate_terminal",
                                      "candidate_id": ident, "state": existing["state"],
                                      "revision": existing["revision"], "generation": existing["generation"],
                                      "bookkeeping_seq": existing["bookkeeping_seq"]}
                        elif existing["generation"] != identity["generation"]:
                            _terminalize(c, existing, "disabled", "settings_disabled", now)
                            result = {"ok": False, "reason": "generation_changed", "candidate_id": ident,
                                      "state": "disabled", "revision": existing["revision"] + 1,
                                      "generation": existing["generation"],
                                      "bookkeeping_seq": existing["bookkeeping_seq"]}
                        elif (not _compare_identity(existing, identity) or
                              existing["resolved_profile"] != resolved_profile or
                              existing["source_binding"] != source or
                              existing["destination_binding"] != destination):
                            _terminalize(c, existing, "cancelled", "identity_changed", now)
                            result = {"ok": False, "reason": "identity_changed", "candidate_id": ident,
                                      "state": "cancelled", "revision": existing["revision"] + 1}
                        else:
                            result = {"ok": True, "duplicate": True, "candidate_id": ident,
                                      "revision": existing["revision"], "state": existing["state"],
                                      "route": {"mode": "local", "connectionId": existing["connection_id"],
                                                "profile": existing["profile"], "targetProfile": existing["target_profile"]},
                                      "runtime_id": existing["runtime_id"], "final_row_id": existing["final_row_id"],
                                      "created_at": existing["created"], "quiet_at": existing["quiet_at"],
                                      "expires_at": existing["expires"], "seq": existing["seq"],
                                      "bookkeeping_seq": existing["bookkeeping_seq"],
                                      "generation": existing["generation"], "epoch": existing["epoch"],
                                      "started_at": existing["started_at"], "session_key": existing["session_key"]}
                    else:
                        active = int(c.execute("SELECT count(*) FROM completion_candidates WHERE state IN (?,?,?,?,?)",
                                               ACTIVE).fetchone()[0])
                        total = int(c.execute("SELECT count(*) FROM completion_candidates").fetchone()[0])
                        if active >= MAX_ACTIVE:
                            result = {"ok": False, "reason": "candidate_limit"}
                        elif total >= MAX_TOTAL:
                            result = {"ok": False, "reason": "storage_full"}
                        else:
                            c.execute("""INSERT INTO completion_candidates(
                                candidate_id,schema_version,connection_id,profile,target_profile,resolved_profile,
                                runtime_id,session_key,started_at,epoch,generation,seq,bookkeeping_seq,final_row_id,
                                created,quiet_at,expires,source_binding,destination_binding,state,revision,reason,
                                claim_token,claim_until,dispatch_token,proof_attempt,updated
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (ident, SCHEMA_VERSION, identity["connection_id"], identity["profile"],
                                 identity["target_profile"], resolved_profile, identity["runtime_id"],
                                 identity["session_key"], identity["started_at"], identity["epoch"],
                                 identity["generation"], identity["seq"], identity["seq"], identity["final_row_id"],
                                 created, quiet, expiry, source, destination, "pending", 1, "", None, None,
                                 None, None, now))
                            result = {"ok": True, "duplicate": False, "candidate_id": ident,
                                      "revision": 1, "state": "pending",
                                      "route": {"mode": "local", "connectionId": identity["connection_id"],
                                                "profile": identity["profile"], "targetProfile": identity["target_profile"]},
                                      "runtime_id": identity["runtime_id"], "final_row_id": identity["final_row_id"],
                                      "created_at": created, "quiet_at": quiet,
                                      "expires_at": expiry, "seq": identity["seq"],
                                      "bookkeeping_seq": identity["seq"], "generation": identity["generation"],
                                      "epoch": identity["epoch"], "started_at": identity["started_at"],
                                      "session_key": identity["session_key"]}
            c.commit()
            return result
        except Exception:
            c.rollback()
            raise


def cancel(state: str | Path, root: str | Path, request: object, *, reason: str = "new_work",
           now: float | None = None) -> dict[str, Any]:
    """Cancel by exact source when known; otherwise install a bounded route fence."""
    now = time.time() if now is None else _number(now, name="now")
    if reason not in REASONS or reason not in {"runtime_absent", "identity_changed", "new_work", "settings_disabled", "ttl_expired"}:
        raise RecoveryError("invalid_recovery_reason")
    if not isinstance(request, dict):
        raise RecoveryError("invalid_recovery_identity")
    route = _route(request.get("route"))
    runtime_id = _safe_text(request.get("runtime_id"), name="runtime_id", maximum=200)
    final_id = request.get("final_row_id")
    if type(final_id) is not int or final_id < 1:
        raise RecoveryError("invalid_recovery_identity")
    fence_id = _route_fence_id(route, runtime_id, final_id)
    natural = {**route, "runtime_id": runtime_id, "final_row_id": final_id}
    with _connection(state, create=True) as c:
        assert c is not None
        _ensure_schema(c)
        c.execute("BEGIN IMMEDIATE")
        try:
            _cleanup(c, now, route=route)
            meta = c.execute(f"SELECT generation FROM {META_TABLE} WHERE singleton=1").fetchone()
            generation = str(meta["generation"]) if meta else _policy_generation(c, now, False)
            rows = c.execute("""SELECT * FROM completion_candidates WHERE connection_id=? AND profile=?
                AND target_profile=? AND runtime_id=? AND final_row_id=? AND candidate_id<>?""",
                (*_route_key(route, runtime_id, final_id), fence_id)).fetchall()

            selected: list[sqlite3.Row] = []
            source_binding = ""
            resolved_profile = ""
            if rows:
                bindings = {(str(row["source_binding"]), str(row["resolved_profile"])) for row in rows
                            if row["source_binding"]}
                if len(bindings) == 1:
                    source_binding, resolved_profile = next(iter(bindings))
                    selected = [row for row in rows if row["source_binding"] == source_binding]
            else:
                try:
                    from . import notifier
                except ImportError:
                    import notifier
                resolved = notifier.resolve_recovery_cancel_source({"route": request.get("route"),
                    "runtime_id": runtime_id, "final_row_id": final_id}, root=root)
                if resolved.get("reason") == "source_storage_unavailable":
                    raise RecoveryError("source_storage_unavailable")
                if resolved.get("ok") is True:
                    source_binding = _safe_text(resolved.get("binding"), name="source_binding", maximum=64,
                                                pattern=HEX64_RE)
                    resolved_profile = _safe_text(resolved.get("profile"), name="resolved_profile", maximum=80,
                                                  pattern=PROFILE_RE)

            if selected:
                dispatching = next((row for row in selected if row["state"] == "dispatching"), None)
                if dispatching:
                    result = {"ok": False, "reason": "already_dispatching", "candidate_id": dispatching["candidate_id"],
                              "state": "dispatching", "revision": dispatching["revision"]}
                else:
                    latest = selected[0]
                    for row in selected:
                        if row["state"] in TERMINAL:
                            latest = row
                            continue
                        c.execute("""UPDATE completion_candidates SET state='cancelled',revision=revision+1,reason=?,
                            claim_token=NULL,claim_until=NULL,updated=? WHERE candidate_id=? AND revision=?
                            AND state IN ('pending','read_blocked','claimed','unsent_retryable')""",
                            (reason, now, row["candidate_id"], row["revision"]))
                        latest = c.execute("SELECT * FROM completion_candidates WHERE candidate_id=?",
                                            (row["candidate_id"],)).fetchone()
                    current = c.execute("SELECT state,revision FROM completion_candidates WHERE candidate_id=?",
                                         (latest["candidate_id"],)).fetchone()
                    result = {"ok": True, "candidate_id": latest["candidate_id"],
                              "state": current["state"], "revision": current["revision"]}
            else:
                if source_binding:
                    ident = _candidate_id(source_binding, natural)
                    fence_source = source_binding
                else:
                    # No unique native source can be selected. This single
                    # route-scoped fence prevents a delayed ambiguous record;
                    # distinct routes remain untouched.
                    ident = fence_id
                    fence_source = ""
                existing = c.execute("SELECT state,revision FROM completion_candidates WHERE candidate_id=?",
                                     (ident,)).fetchone()
                if existing:
                    result = {"ok": True, "candidate_id": ident, "state": existing["state"],
                              "revision": existing["revision"]}
                else:
                    # Capacity for a new cancellation fence is global too.
                    _cleanup(c, now)
                    total = int(c.execute("SELECT count(*) FROM completion_candidates").fetchone()[0])
                    if total >= MAX_TOTAL:
                        c.execute(f"UPDATE {META_TABLE} SET fence_until=max(fence_until,?) WHERE singleton=1",
                                  (now + TTL_SECONDS,))
                        result = {"ok": True, "candidate_id": ident, "state": "cancelled",
                                  "revision": 1, "admission_fenced": True}
                    else:
                        c.execute("""INSERT INTO completion_candidates(
                            candidate_id,schema_version,connection_id,profile,target_profile,resolved_profile,
                            runtime_id,session_key,generation,seq,bookkeeping_seq,final_row_id,created,quiet_at,
                            expires,source_binding,destination_binding,state,revision,reason,updated
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (ident, SCHEMA_VERSION, route["connection_id"], route["profile"], route["target_profile"],
                             resolved_profile, runtime_id, runtime_id, generation, 0, 0, final_id, now, now,
                             now + TTL_SECONDS, fence_source, "", "cancelled", 1, reason, now))
                        result = {"ok": True, "candidate_id": ident, "state": "cancelled", "revision": 1}
            c.commit()
            return result
        except Exception:
            c.rollback()
            raise


def _binding_matches(row: sqlite3.Row, route: dict[str, str], source: str, destination: str,
                     resolved_profile: str, generation: str | None = None) -> bool:
    return (row["connection_id"] == route["connection_id"] and row["profile"] == route["profile"] and
            row["target_profile"] == route["target_profile"] and row["resolved_profile"] == resolved_profile and
            row["source_binding"] == source and row["destination_binding"] == destination and
            row["schema_version"] == SCHEMA_VERSION and
            (generation is None or row["generation"] == generation))


def _public_candidate(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "candidate_id": row["candidate_id"], "revision": row["revision"],
        "route": {"mode": "local", "connectionId": row["connection_id"],
                  "profile": row["profile"], "targetProfile": row["target_profile"]},
        "runtime_id": row["runtime_id"], "session_key": row["session_key"],
        "started_at": row["started_at"], "epoch": row["epoch"], "seq": row["seq"],
        "bookkeeping_seq": row["bookkeeping_seq"], "generation": row["generation"],
        "final_row_id": row["final_row_id"], "created_at": row["created"],
        "quiet_at": row["quiet_at"], "expires_at": row["expires"],
        "state": row["state"], "reason": row["reason"],
    }


def _row_identity(row: sqlite3.Row, route: dict[str, str] | None = None) -> dict[str, Any]:
    value = {"connection_id": row["connection_id"], "profile": row["profile"],
             "target_profile": row["target_profile"], "runtime_id": row["runtime_id"],
             "session_key": row["session_key"], "final_row_id": row["final_row_id"]}
    if route is not None and (value["connection_id"] != route["connection_id"] or
            value["profile"] != route["profile"] or value["target_profile"] != route["target_profile"]):
        raise RecoveryError("invalid_recovery_route")
    return value


def _json_bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def list_candidates(state: str | Path, root: str | Path, route_value: object, *, cursor: object = None,
                    result_budget: int = MAX_RESULT_BYTES, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else _number(now, name="now")
    route = _route(route_value)
    if cursor is not None and (not isinstance(cursor, str) or not HEX64_RE.fullmatch(cursor)):
        raise RecoveryError("invalid_recovery_cursor")
    if type(result_budget) is not int or not 512 <= result_budget <= MAX_RESULT_BYTES:
        raise RecoveryError("invalid_recovery_cursor")
    destination = _destination_binding(root)
    base = {"ok": True, "version": VERSION, "items": [], "cursor": None}
    if _json_bytes(base) > result_budget:
        raise RecoveryError("recovery_page_item_too_large")
    with _connection(state, create=True) as c:
        assert c is not None
        _ensure_schema(c)
        c.execute("BEGIN IMMEDIATE")
        try:
            _cleanup(c, now, route=route)
            meta = c.execute(f"SELECT generation,enabled FROM {META_TABLE} WHERE singleton=1").fetchone()
            if not meta or not bool(meta["enabled"]):
                c.commit()
                return base
            rows = c.execute("""SELECT * FROM completion_candidates
                WHERE connection_id=? AND profile=? AND target_profile=?
                  AND state IN ('pending','read_blocked','claimed','unsent_retryable')
                  AND expires>? AND candidate_id>? ORDER BY candidate_id ASC LIMIT ?""",
                (route["connection_id"], route["profile"], route["target_profile"], now,
                 cursor or "", MAX_ACTIVE + 1)).fetchall()
            items: list[dict[str, Any]] = []
            scan_cursor = cursor
            has_more = False
            for index, row in enumerate(rows):
                scan_cursor = row["candidate_id"]
                if row["generation"] != meta["generation"]:
                    _terminalize(c, row, "disabled", "settings_disabled", now)
                    continue
                try:
                    resolved_profile, source = _resolve_source(root, _row_identity(row, route))
                except RecoveryError as exc:
                    if str(exc) in {"source_storage_unavailable", "recovery_database_unavailable"}:
                        raise
                    _terminalize(c, row, "cancelled", "identity_changed", now)
                    continue
                if not _binding_matches(row, route, source, destination, resolved_profile, str(meta["generation"])):
                    _terminalize(c, row, "cancelled", "identity_changed", now)
                    continue
                candidate = _public_candidate(row)
                if len(items) >= MAX_PAGE_ITEMS:
                    has_more = True
                    scan_cursor = items[-1]["candidate_id"]
                    break
                trial = {"ok": True, "version": VERSION, "items": [*items, candidate],
                         "cursor": candidate["candidate_id"]}
                if _json_bytes(trial) > result_budget:
                    if not items:
                        raise RecoveryError("recovery_page_item_too_large")
                    has_more = True
                    scan_cursor = items[-1]["candidate_id"]
                    break
                items.append(candidate)
            else:
                has_more = len(rows) > 0 and len(rows) == MAX_ACTIVE + 1
            next_cursor = scan_cursor if has_more else None
            result = {"ok": True, "version": VERSION, "items": items, "cursor": next_cursor}
            if _json_bytes(result) > result_budget:
                raise RecoveryError("recovery_page_item_too_large")
            c.commit()
            return result
        except Exception:
            c.rollback()
            raise


def get_candidate(state: str | Path, root: str | Path, candidate_id_value: object, route_value: object,
                  *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else _number(now, name="now")
    ident = _safe_text(candidate_id_value, name="candidate_id", maximum=64, pattern=HEX64_RE)
    route = _route(route_value)
    with _connection(state, create=True) as c:
        assert c is not None
        _ensure_schema(c)
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT * FROM completion_candidates WHERE candidate_id=?", (ident,)).fetchone()
            if not row or (row["connection_id"], row["profile"], row["target_profile"]) != (
                    route["connection_id"], route["profile"], route["target_profile"]):
                result = {"ok": True, "version": VERSION, "item": None}
            else:
                _cleanup(c, now, route=route, candidate_id_value=ident)
                row = c.execute("SELECT * FROM completion_candidates WHERE candidate_id=?", (ident,)).fetchone()
                if row is None:
                    result = {"ok": True, "version": VERSION, "item": None}
                elif row["state"] in PRE_DISPATCH:
                    destination = _destination_binding(root)
                    try:
                        resolved_profile, source = _resolve_source(root, _row_identity(row, route))
                    except RecoveryError as exc:
                        if str(exc) in {"source_storage_unavailable", "recovery_database_unavailable"}:
                            raise
                        _terminalize(c, row, "cancelled", "identity_changed", now)
                        row = c.execute("SELECT * FROM completion_candidates WHERE candidate_id=?", (ident,)).fetchone()
                    if row["state"] in PRE_DISPATCH and not _binding_matches(
                            row, route, source, destination, resolved_profile):
                        _terminalize(c, row, "cancelled", "identity_changed", now)
                        row = c.execute("SELECT * FROM completion_candidates WHERE candidate_id=?", (ident,)).fetchone()
                    result = {"ok": True, "version": VERSION,
                              "item": _public_candidate(row) if row is not None else None}
                else:
                    result = {"ok": True, "version": VERSION, "item": _public_candidate(row)}
            c.commit()
            return result
        except Exception:
            c.rollback()
            raise


def claim(state: str | Path, root: str | Path, candidate: object, route_value: object, *,
          now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else _number(now, name="now")
    if (not isinstance(candidate, dict) or set(candidate) not in
            ({"candidate_id", "revision"}, {"candidate_id", "revision", "generation"})):
        raise RecoveryError("invalid_recovery_claim")
    ident = _safe_text(candidate.get("candidate_id"), name="candidate_id", maximum=64, pattern=HEX64_RE)
    revision = candidate.get("revision")
    candidate_generation = candidate.get("generation")
    if type(revision) is not int or revision < 1:
        raise RecoveryError("invalid_recovery_claim")
    if candidate_generation is not None:
        candidate_generation = _safe_text(candidate_generation, name="generation", maximum=32,
                                          pattern=GENERATION_RE)
    route = _route(route_value)
    destination = _destination_binding(root)
    token = secrets.token_hex(16)
    with _connection(state, create=True) as c:
        assert c is not None
        _ensure_schema(c)
        c.execute("BEGIN IMMEDIATE")
        try:
            _cleanup(c, now, route=route, candidate_id_value=ident)
            row = c.execute("SELECT * FROM completion_candidates WHERE candidate_id=?", (ident,)).fetchone()
            if not row or (row["connection_id"], row["profile"], row["target_profile"]) != (
                    route["connection_id"], route["profile"], route["target_profile"]):
                result = {"ok": False, "reason": "candidate_absent"}
            elif row["revision"] != revision:
                result = {"ok": False, "reason": "candidate_changed", "state": row["state"],
                          "revision": row["revision"], "generation": row["generation"],
                          "bookkeeping_seq": row["bookkeeping_seq"], "quiet_at": row["quiet_at"]}
            elif candidate_generation is not None and candidate_generation != row["generation"]:
                result = {"ok": False, "reason": "candidate_changed", "state": row["state"],
                          "revision": row["revision"], "generation": row["generation"],
                          "bookkeeping_seq": row["bookkeeping_seq"], "quiet_at": row["quiet_at"]}
            elif row["expires"] <= now:
                _terminalize(c, row, "expired", "ttl_expired", now)
                result = {"ok": False, "reason": "candidate_expired", "state": "expired",
                          "revision": row["revision"] + 1, "generation": row["generation"],
                          "bookkeeping_seq": row["bookkeeping_seq"]}
            else:
                meta = c.execute(f"SELECT generation,enabled,fence_until FROM {META_TABLE} WHERE singleton=1").fetchone()
                if not meta or not bool(meta["enabled"]) or row["generation"] != meta["generation"]:
                    _terminalize(c, row, "disabled", "settings_disabled", now)
                    result = {"ok": False, "reason": "settings_disabled", "state": "disabled",
                              "revision": row["revision"] + 1, "generation": row["generation"],
                              "bookkeeping_seq": row["bookkeeping_seq"]}
                elif float(meta["fence_until"]) > now:
                    result = {"ok": False, "reason": "admission_fence_active", "state": row["state"],
                              "revision": row["revision"], "retry_at": meta["fence_until"],
                              "generation": row["generation"], "bookkeeping_seq": row["bookkeeping_seq"]}
                else:
                    try:
                        resolved_profile, source = _resolve_source(root, _row_identity(row, route))
                    except RecoveryError as exc:
                        if str(exc) in {"source_storage_unavailable", "recovery_database_unavailable"}:
                            raise
                        _terminalize(c, row, "cancelled", "identity_changed", now)
                        result = {"ok": False, "reason": "identity_changed", "state": "cancelled",
                                  "revision": row["revision"] + 1, "generation": row["generation"],
                                  "bookkeeping_seq": row["bookkeeping_seq"]}
                    else:
                        if not _binding_matches(row, route, source, destination, resolved_profile,
                                               str(meta["generation"])):
                            _terminalize(c, row, "cancelled", "identity_changed", now)
                            result = {"ok": False, "reason": "identity_changed", "state": "cancelled",
                                      "revision": row["revision"] + 1, "generation": row["generation"],
                                      "bookkeeping_seq": row["bookkeeping_seq"]}
                        elif row["state"] == "claimed" and row["claim_until"] is not None and row["claim_until"] > now:
                            result = {"ok": False, "reason": "claim_busy", "state": row["state"],
                                      "revision": row["revision"], "retry_at": row["claim_until"],
                                      "generation": row["generation"], "bookkeeping_seq": row["bookkeeping_seq"]}
                        elif row["state"] in {"pending", "read_blocked", "unsent_retryable", "claimed"}:
                            deadline = min(now + CLAIM_SECONDS, row["expires"])
                            c.execute("""UPDATE completion_candidates SET state='claimed',revision=revision+1,reason='',
                                claim_token=?,claim_until=?,updated=? WHERE candidate_id=? AND revision=?
                                AND state IN (?,?,?,?)""",
                                (token, deadline, now, ident, revision, "pending", "read_blocked", "unsent_retryable", "claimed"))
                            latest = c.execute("SELECT * FROM completion_candidates WHERE candidate_id=?", (ident,)).fetchone()
                            result = {"ok": True, "candidate_id": ident, "revision": latest["revision"],
                                      "claim_token": token, "claim_until": deadline, "state": latest["state"],
                                      "proof_attempt": latest["proof_attempt"], "generation": latest["generation"],
                                      "bookkeeping_seq": latest["bookkeeping_seq"], "quiet_at": latest["quiet_at"],
                                      "expires_at": latest["expires"]}
                        else:
                            result = {"ok": False, "reason": "candidate_not_claimable", "state": row["state"],
                                      "revision": row["revision"], "generation": row["generation"],
                                      "bookkeeping_seq": row["bookkeeping_seq"]}
            c.commit()
            return result
        except Exception:
            c.rollback()
            raise


def mark_read_blocked(state: str | Path, candidate: object, *, reason: str = "snapshot_unavailable",
                      now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else _number(now, name="now")
    if (not isinstance(candidate, dict) or set(candidate) not in
            ({"candidate_id", "revision"}, {"candidate_id", "revision", "generation"})):
        raise RecoveryError("invalid_recovery_candidate")
    ident = _safe_text(candidate.get("candidate_id"), name="candidate_id", maximum=64, pattern=HEX64_RE)
    revision = candidate.get("revision")
    supplied_generation = candidate.get("generation")
    if type(revision) is not int or revision < 1 or reason not in {"snapshot_unavailable", "snapshot_incomplete", "awaiting_snapshot"}:
        raise RecoveryError("invalid_recovery_candidate")
    if supplied_generation is not None:
        supplied_generation = _safe_text(supplied_generation, name="generation", maximum=32,
                                         pattern=GENERATION_RE)
    with _connection(state, create=True) as c:
        assert c is not None
        _ensure_schema(c)
        c.execute("BEGIN IMMEDIATE")
        try:
            _cleanup(c, now, candidate_id_value=ident)
            row = c.execute("SELECT * FROM completion_candidates WHERE candidate_id=?", (ident,)).fetchone()
            if (not row or row["revision"] != revision or row["state"] not in {"pending", "read_blocked"} or
                    (supplied_generation is not None and supplied_generation != row["generation"])):
                result = {"ok": False, "reason": "candidate_changed",
                          **({"state": row["state"], "revision": row["revision"],
                              "generation": row["generation"], "bookkeeping_seq": row["bookkeeping_seq"]} if row else {})}
            else:
                meta = c.execute(f"SELECT generation,enabled FROM {META_TABLE} WHERE singleton=1").fetchone()
                if not meta or not bool(meta["enabled"]) or meta["generation"] != row["generation"]:
                    _terminalize(c, row, "disabled", "settings_disabled", now)
                    result = {"ok": False, "reason": "settings_disabled", "state": "disabled",
                              "revision": row["revision"] + 1, "generation": row["generation"],
                              "bookkeeping_seq": row["bookkeeping_seq"]}
                else:
                    c.execute("UPDATE completion_candidates SET state='read_blocked',revision=revision+1,reason=?,updated=? WHERE candidate_id=? AND revision=?",
                              (reason, now, ident, revision))
                    latest = c.execute("SELECT revision,state,generation,bookkeeping_seq FROM completion_candidates WHERE candidate_id=?", (ident,)).fetchone()
                    result = {"ok": True, "candidate_id": ident, "revision": latest["revision"],
                              "state": latest["state"], "generation": latest["generation"],
                              "bookkeeping_seq": latest["bookkeeping_seq"]}
            c.commit()
            return result
        except Exception:
            c.rollback()
            raise


def touch(state: str | Path, candidate: object, *, bookkeeping_seq: object,
          quiet_at: float, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else _number(now, name="now")
    quiet = _number(quiet_at, name="quiet_at")
    if (not isinstance(candidate, dict) or set(candidate) not in
            ({"candidate_id", "revision"}, {"candidate_id", "revision", "generation"})):
        raise RecoveryError("invalid_recovery_candidate")
    ident = _safe_text(candidate.get("candidate_id"), name="candidate_id", maximum=64, pattern=HEX64_RE)
    revision = candidate.get("revision")
    supplied_generation = candidate.get("generation")
    if type(revision) is not int or revision < 1 or type(bookkeeping_seq) is not int or bookkeeping_seq < 1:
        raise RecoveryError("invalid_recovery_candidate")
    if supplied_generation is not None:
        supplied_generation = _safe_text(supplied_generation, name="generation", maximum=32,
                                         pattern=GENERATION_RE)
    with _connection(state, create=True) as c:
        assert c is not None
        _ensure_schema(c)
        c.execute("BEGIN IMMEDIATE")
        try:
            _cleanup(c, now, candidate_id_value=ident)
            row = c.execute("SELECT * FROM completion_candidates WHERE candidate_id=?", (ident,)).fetchone()
            if not row:
                result = {"ok": False, "reason": "candidate_absent"}
            elif row["state"] not in PRE_DISPATCH:
                result = {"ok": False, "reason": "candidate_changed", "state": row["state"],
                          "revision": row["revision"], "generation": row["generation"],
                          "bookkeeping_seq": row["bookkeeping_seq"], "quiet_at": row["quiet_at"]}
            elif row["expires"] <= now:
                _terminalize(c, row, "expired", "ttl_expired", now)
                result = {"ok": False, "reason": "candidate_expired", "state": "expired",
                          "revision": row["revision"] + 1, "generation": row["generation"],
                          "bookkeeping_seq": row["bookkeeping_seq"]}
            elif supplied_generation is not None and supplied_generation != row["generation"]:
                result = {"ok": False, "reason": "candidate_changed", "state": row["state"],
                          "revision": row["revision"], "generation": row["generation"],
                          "bookkeeping_seq": row["bookkeeping_seq"], "quiet_at": row["quiet_at"]}
            elif bookkeeping_seq <= row["bookkeeping_seq"]:
                result = {"ok": True, "candidate_id": ident, "revision": row["revision"],
                          "quiet_at": row["quiet_at"], "expires_at": row["expires"],
                          "generation": row["generation"], "bookkeeping_seq": row["bookkeeping_seq"],
                          "state": row["state"]}
            elif row["revision"] != revision:
                result = {"ok": False, "reason": "candidate_changed", "state": row["state"],
                          "revision": row["revision"], "generation": row["generation"],
                          "bookkeeping_seq": row["bookkeeping_seq"], "quiet_at": row["quiet_at"]}
            else:
                meta = c.execute(f"SELECT generation,enabled FROM {META_TABLE} WHERE singleton=1").fetchone()
                if not meta or not bool(meta["enabled"]) or meta["generation"] != row["generation"]:
                    _terminalize(c, row, "disabled", "settings_disabled", now)
                    result = {"ok": False, "reason": "settings_disabled", "state": "disabled",
                              "revision": row["revision"] + 1, "generation": row["generation"],
                              "bookkeeping_seq": row["bookkeeping_seq"]}
                elif quiet < row["created"] or quiet > now + 30 or quiet > row["expires"]:
                    raise RecoveryError("invalid_recovery_candidate")
                else:
                    c.execute("""UPDATE completion_candidates SET state='pending',quiet_at=max(quiet_at,?),
                        bookkeeping_seq=?,revision=revision+1,reason='',claim_token=NULL,claim_until=NULL,updated=?
                        WHERE candidate_id=? AND revision=?""",
                        (quiet, bookkeeping_seq, now, ident, revision))
                    latest = c.execute("SELECT revision,quiet_at,expires,generation,bookkeeping_seq FROM completion_candidates WHERE candidate_id=?", (ident,)).fetchone()
                    result = {"ok": True, "candidate_id": ident, "revision": latest["revision"],
                              "quiet_at": latest["quiet_at"], "expires_at": latest["expires"],
                              "generation": latest["generation"], "bookkeeping_seq": latest["bookkeeping_seq"],
                              "state": "pending"}
            c.commit()
            return result
        except Exception:
            c.rollback()
            raise


def begin_dispatch(state: str | Path, root: str | Path, candidate: object, route_value: object,
                   *, dedupe_key: str, profile: str, session_key: str,
                   runtime_id: str | None = None, started_at: str | None = None,
                   epoch: str | None = None, final_row_id: int | None = None,
                   destination_binding: str | None = None, now: float | None = None) -> dict[str, Any]:
    """Atomically validate generation, quiet, source, claim and existing delivery ledger."""
    now = time.time() if now is None else _number(now, name="now")
    if (not isinstance(candidate, dict) or set(candidate) not in
            ({"candidate_id", "revision", "claim_token"},
             {"candidate_id", "revision", "claim_token", "generation"})):
        raise RecoveryError("invalid_recovery_dispatch")
    ident = _safe_text(candidate.get("candidate_id"), name="candidate_id", maximum=64, pattern=HEX64_RE)
    revision = candidate.get("revision")
    token = _safe_text(candidate.get("claim_token"), name="claim_token", maximum=32,
                       pattern=re.compile(r"^[0-9a-f]{32}$"))
    supplied_generation = candidate.get("generation")
    if type(revision) is not int or revision < 1 or not HEX64_RE.fullmatch(dedupe_key):
        raise RecoveryError("invalid_recovery_dispatch")
    if supplied_generation is not None:
        supplied_generation = _safe_text(supplied_generation, name="generation", maximum=32,
                                         pattern=GENERATION_RE)
    route = _route(route_value)
    destination = _destination_binding(root)
    if destination_binding is not None and destination_binding != destination:
        return {"ok": False, "reason": "destination_changed"}
    profile = _safe_text(profile, name="profile", maximum=80, pattern=PROFILE_RE)
    session_key = _safe_text(session_key, name="session_key", maximum=200)
    runtime_id = _safe_text(runtime_id, name="runtime_id", maximum=200)
    started_at = _safe_text(started_at, name="started_at", maximum=100)
    epoch = _safe_text(epoch, name="epoch", maximum=100)
    if type(final_row_id) is not int or final_row_id < 1:
        raise RecoveryError("invalid_recovery_dispatch")
    source_identity = {**route, "runtime_id": runtime_id, "session_key": session_key,
                       "final_row_id": final_row_id}
    resolved_profile, source = _resolve_source(root, source_identity)
    if resolved_profile != profile:
        return {"ok": False, "reason": "identity_changed"}
    attempt = secrets.token_hex(16)
    with _connection(state, create=True) as c:
        assert c is not None
        _ensure_schema(c)
        c.execute("BEGIN IMMEDIATE")
        try:
            _cleanup(c, now, route=route, candidate_id_value=ident)
            row = c.execute("SELECT * FROM completion_candidates WHERE candidate_id=?", (ident,)).fetchone()
            if not row or (row["connection_id"], row["profile"], row["target_profile"]) != (
                    route["connection_id"], route["profile"], route["target_profile"]):
                result = {"ok": False, "reason": "candidate_absent"}
            elif row["revision"] != revision or row["state"] != "claimed" or row["claim_token"] != token:
                result = {"ok": False, "reason": "candidate_changed", "state": row["state"],
                          "revision": row["revision"], "generation": row["generation"],
                          "bookkeeping_seq": row["bookkeeping_seq"], "quiet_at": row["quiet_at"]}
            elif supplied_generation is not None and supplied_generation != row["generation"]:
                result = {"ok": False, "reason": "candidate_changed", "state": row["state"],
                          "revision": row["revision"], "generation": row["generation"],
                          "bookkeeping_seq": row["bookkeeping_seq"], "quiet_at": row["quiet_at"]}
            elif row["expires"] <= now:
                _terminalize(c, row, "expired", "ttl_expired", now)
                result = {"ok": False, "reason": "candidate_expired", "state": "expired",
                          "revision": row["revision"] + 1, "generation": row["generation"],
                          "bookkeeping_seq": row["bookkeeping_seq"]}
            elif row["claim_until"] is None or row["claim_until"] <= now:
                # An expired lock is not evidence of new work. Keep a still-valid
                # candidate claimable by the next window; refuse only this token.
                result = {"ok": False, "reason": "claim_expired", "state": row["state"],
                          "revision": row["revision"], "generation": row["generation"],
                          "bookkeeping_seq": row["bookkeeping_seq"]}
            else:
                meta = c.execute(f"SELECT generation,enabled,fence_until FROM {META_TABLE} WHERE singleton=1").fetchone()
                if not meta or not bool(meta["enabled"]) or row["generation"] != meta["generation"]:
                    _terminalize(c, row, "disabled", "settings_disabled", now)
                    result = {"ok": False, "reason": "settings_disabled", "state": "disabled",
                              "revision": row["revision"] + 1, "generation": row["generation"],
                              "bookkeeping_seq": row["bookkeeping_seq"]}
                elif float(meta["fence_until"]) > now:
                    result = {"ok": False, "reason": "admission_fence_active", "state": row["state"],
                              "revision": row["revision"], "retry_at": meta["fence_until"],
                              "generation": row["generation"], "bookkeeping_seq": row["bookkeeping_seq"]}
                elif (_resolve_source(root, source_identity) != (resolved_profile, source) or
                      not _binding_matches(row, route, source, destination, resolved_profile,
                                             str(meta["generation"])) or row["resolved_profile"] != profile or
                      row["session_key"] != session_key or row["runtime_id"] != runtime_id or
                      row["started_at"] != started_at or row["epoch"] != epoch or row["final_row_id"] != final_row_id):
                    _terminalize(c, row, "cancelled", "identity_changed", now)
                    result = {"ok": False, "reason": "identity_changed", "state": "cancelled",
                              "revision": row["revision"] + 1, "generation": row["generation"],
                              "bookkeeping_seq": row["bookkeeping_seq"]}
                elif float(row["quiet_at"]) + QUIET_SECONDS > now:
                    result = {"ok": False, "phase": "pre_dispatch", "retryable": True,
                              "reason": "quiet_period", "state": row["state"],
                              "revision": row["revision"], "quiet_at": row["quiet_at"],
                              "generation": row["generation"], "bookkeeping_seq": row["bookkeeping_seq"]}
                else:
                    delivery = c.execute("SELECT status FROM deliveries WHERE dedupe_key=?", (dedupe_key,)).fetchone()
                    if delivery is None and row["proof_attempt"]:
                        c.execute("UPDATE completion_candidates SET state='rejected',revision=revision+1,reason='delivery_interference',updated=? WHERE candidate_id=? AND revision=?",
                                  (now, ident, revision))
                        result = {"ok": False, "reason": "delivery_interference", "state": "rejected"}
                    elif delivery is None:
                        c.execute("INSERT INTO deliveries(dedupe_key,profile,session_key,status,created) VALUES(?,?,?,?,?)",
                                  (dedupe_key, profile, session_key, "sending", now))
                        result = None
                    elif delivery["status"] == "unsent_retryable" and row["proof_attempt"]:
                        changed = c.execute("UPDATE deliveries SET status='sending',created=?,message_id=NULL,verified=0 WHERE dedupe_key=? AND status='unsent_retryable'",
                                            (now, dedupe_key)).rowcount
                        result = None if changed == 1 else {"ok": False, "reason": "delivery_interference"}
                    else:
                        current = str(delivery["status"])
                        terminal = "sent" if current == "sent" else "unknown" if current in {"sending", "unknown"} else "rejected"
                        terminal_reason = ("existing_delivery_sent" if terminal == "sent" else
                                           "existing_delivery_unknown" if terminal == "unknown" else
                                           "existing_delivery_failed")
                        c.execute("""UPDATE completion_candidates SET state=?,revision=revision+1,reason=?,
                            claim_token=NULL,claim_until=NULL,updated=? WHERE candidate_id=? AND revision=?""",
                            (terminal, terminal_reason, now, ident, revision))
                        result = {"ok": True, "duplicate": True, "status": current, "candidate_state": terminal,
                                  "generation": row["generation"], "bookkeeping_seq": row["bookkeeping_seq"]}
                    if result is None:
                        c.execute("""UPDATE completion_candidates SET state='dispatching',revision=revision+1,
                            reason='',claim_token=NULL,claim_until=NULL,dispatch_token=?,updated=?
                            WHERE candidate_id=? AND revision=? AND state='claimed' AND claim_token=?""",
                            (attempt, now, ident, revision, token))
                        if c.execute("SELECT changes()").fetchone()[0] != 1:
                            raise RecoveryError("recovery_dispatch_cas_failed")
                        latest = c.execute("SELECT revision,generation,bookkeeping_seq,quiet_at FROM completion_candidates WHERE candidate_id=?", (ident,)).fetchone()
                        result = {"ok": True, "duplicate": False, "candidate_id": ident,
                                  "revision": latest["revision"], "attempt_id": attempt, "status": "dispatching",
                                  "generation": latest["generation"], "bookkeeping_seq": latest["bookkeeping_seq"],
                                  "quiet_at": latest["quiet_at"]}
            c.commit()
            return result
        except Exception:
            c.rollback()
            raise


def finish_dispatch(state: str | Path, candidate_id_value: str, attempt_id: str, dedupe_key: str,
                    outcome: str, *, message_id: int | None = None, verified: bool = False,
                    now: float | None = None) -> dict[str, Any]:
    """Persist exact-attempt evidence; only proven pre-request connect failure is retryable."""
    now = time.time() if now is None else _number(now, name="now")
    ident = _safe_text(candidate_id_value, name="candidate_id", maximum=64, pattern=HEX64_RE)
    attempt = _safe_text(attempt_id, name="attempt_id", maximum=32, pattern=re.compile(r"^[0-9a-f]{32}$"))
    if not HEX64_RE.fullmatch(dedupe_key) or outcome not in {"proven_unsent", "sent", "rejected", "unknown"}:
        raise RecoveryError("invalid_recovery_receipt")
    if message_id is not None and (type(message_id) is not int or message_id < 1):
        message_id = None
    verified_value = int(bool(verified))
    next_state = {"proven_unsent": "unsent_retryable", "sent": "sent", "rejected": "rejected", "unknown": "unknown"}[outcome]
    reason = {"proven_unsent": "connect_failure", "sent": "", "rejected": "bot_api_rejected", "unknown": "ambiguous_transport"}[outcome]
    with _connection(state, create=True) as c:
        assert c is not None
        _ensure_schema(c)
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT * FROM completion_candidates WHERE candidate_id=?", (ident,)).fetchone()
            if not row or row["state"] != "dispatching" or row["dispatch_token"] != attempt:
                result = {"ok": False, "reason": "attempt_receipt_mismatch"}
            else:
                delivery_status = "unsent_retryable" if outcome == "proven_unsent" else next_state
                changed = c.execute("UPDATE deliveries SET status=?,message_id=?,verified=? WHERE dedupe_key=? AND status='sending'",
                                    (delivery_status, message_id if outcome == "sent" else None,
                                     verified_value if outcome == "sent" else 0, dedupe_key)).rowcount
                if changed != 1:
                    # Do not overwrite a row that no longer belongs to this attempt.
                    result = {"ok": False, "reason": "delivery_receipt_mismatch"}
                else:
                    c.execute("""UPDATE completion_candidates SET state=?,revision=revision+1,reason=?,
                        dispatch_token=NULL,proof_attempt=?,claim_token=NULL,claim_until=NULL,updated=?
                        WHERE candidate_id=? AND state='dispatching' AND dispatch_token=?""",
                        (next_state, reason, attempt if outcome == "proven_unsent" else None,
                         now, ident, attempt))
                    latest = c.execute("SELECT revision,state,generation,bookkeeping_seq FROM completion_candidates WHERE candidate_id=?", (ident,)).fetchone()
                    result = {"ok": True, "candidate_id": ident, "revision": latest["revision"],
                              "state": latest["state"], "generation": latest["generation"],
                              "bookkeeping_seq": latest["bookkeeping_seq"],
                              "attempt_id": attempt if outcome == "proven_unsent" else None}
            c.commit()
            return result
        except Exception:
            c.rollback()
            raise


def proves_pre_dispatch(state: str | Path, candidate: object, route_value: object,
                        dedupe_key: str, *, now: float | None = None) -> bool:
    """Read-only proof that this exact claim never crossed the durable send boundary."""
    now = time.time() if now is None else _number(now, name="now")
    try:
        if not isinstance(candidate, dict):
            return False
        ident = _safe_text(candidate.get("candidate_id"), name="candidate_id", maximum=64, pattern=HEX64_RE)
        revision = candidate.get("revision")
        token = _safe_text(candidate.get("claim_token"), name="claim_token", maximum=32,
                           pattern=re.compile(r"^[0-9a-f]{32}$"))
        route = _route(route_value)
        if type(revision) is not int or revision < 1 or not HEX64_RE.fullmatch(dedupe_key):
            return False
        with _connection(state, create=False) as c:
            if c is None:
                return False
            actual = tuple(row[1] for row in c.execute(f"PRAGMA table_info({TABLE})"))
            if actual != _COLUMNS:
                return False
            row = c.execute("SELECT * FROM completion_candidates WHERE candidate_id=?", (ident,)).fetchone()
            if (not row or row["state"] != "claimed" or row["revision"] != revision or
                    row["claim_token"] != token or row["expires"] <= now or
                    (row["connection_id"], row["profile"], row["target_profile"]) !=
                    (route["connection_id"], route["profile"], route["target_profile"])):
                return False
            columns = {item[1] for item in c.execute("PRAGMA table_info(deliveries)")}
            if not {"dedupe_key", "status"} <= columns:
                return False
            delivery = c.execute("SELECT status FROM deliveries WHERE dedupe_key=?", (dedupe_key,)).fetchone()
            if delivery is None:
                return row["proof_attempt"] is None
            return delivery["status"] == "unsent_retryable" and bool(row["proof_attempt"])
    except Exception:
        return False


def retire_unexecuted(state: str | Path, *, now: float | None = None) -> int:
    """Rotate the admission fence even without rows; never alter dispatching or deliveries."""
    now = time.time() if now is None else _number(now, name="now")
    with _connection(state, create=True) as c:
        assert c is not None
        _ensure_schema(c)
        c.execute("BEGIN IMMEDIATE")
        try:
            before = int(c.execute("SELECT count(*) FROM completion_candidates WHERE state IN (?,?,?,?)",
                                   PRE_DISPATCH).fetchone()[0])
            _policy_generation(c, now, False, force_rotate=True)
            c.commit()
            return before
        except Exception:
            c.rollback()
            raise


def diagnostics(state: str | Path, *, now: float | None = None) -> dict[str, int]:
    """Read-only sender counts with an expiry projection; never creates or mutates state."""
    now = time.time() if now is None else _number(now, name="now")
    result = {"awaiting_recovery": 0, "uncertain_delivery": 0, "sent": 0, "expired": 0}
    with _connection(state, create=False) as c:
        if c is None:
            return result
        table = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
        if table:
            actual = tuple(row[1] for row in c.execute(f"PRAGMA table_info({TABLE})"))
            if actual != _COLUMNS:
                raise RecoveryError("recovery_schema_mismatch")
            for row in c.execute("SELECT state,expires FROM completion_candidates"):
                state_value = str(row["state"])
                if state_value in PRE_DISPATCH:
                    if float(row["expires"]) <= now:
                        result["expired"] += 1
                    else:
                        result["awaiting_recovery"] += 1
                elif state_value == "expired":
                    result["expired"] += 1
                elif state_value in {"dispatching", "unknown"}:
                    result["uncertain_delivery"] += 1
                elif state_value == "sent":
                    result["sent"] += 1
        deliveries = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deliveries'").fetchone()
        if deliveries:
            columns = {row[1] for row in c.execute("PRAGMA table_info(deliveries)")}
            if {"status"} <= columns:
                ledger_uncertain = int(c.execute("SELECT count(*) FROM deliveries WHERE status IN ('sending','unknown')").fetchone()[0])
                ledger_sent = int(c.execute("SELECT count(*) FROM deliveries WHERE status='sent'").fetchone()[0])
                result["uncertain_delivery"] = max(result["uncertain_delivery"], ledger_uncertain)
                result["sent"] = max(result["sent"], ledger_sent)
    return result


__all__ = [
    "VERSION", "SCHEMA_VERSION", "TTL_SECONDS", "MAX_ACTIVE", "MAX_TOTAL",
    "RecoveryError", "candidate_id", "destination_binding_for", "policy", "record", "cancel",
    "list_candidates", "get_candidate", "claim", "mark_read_blocked", "touch", "begin_dispatch",
    "finish_dispatch", "proves_pre_dispatch", "retire_unexecuted", "diagnostics",
]
