"""One-shot Telegram inbox for queue-backed native command approvals.

This module owns a separate SQLite inbox. It never answers approvals itself: the
Desktop owns the native route and resolves exactly one frozen queue id with
``approval.respond``. Telegram is only the authenticated input surface.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import math
import os
import re
import secrets
import sqlite3
import time
import unicodedata
from pathlib import Path

VERSION = "1.7.0"
CAPABILITY = "desktop-command-approval-v3"
PREFIX = "hda:"
LEASE = 45
MAX_TTL = 31_536_000
MAX_REVIEW_UNITS = 4096
MAX_ACTIVE = 100
ALLOWED_CHOICES = frozenset({"once", "deny"})
ACTIVE = ("posting", "open", "queued", "applying")
TERMINAL = ("applied", "expired", "rejected", "unknown")
STALE_SEEN = -1e30


def _active_hermes_home():
    from hermes_constants import get_hermes_home
    try:
        return Path(get_hermes_home()).expanduser().resolve(strict=True)
    except Exception as exc:
        raise RuntimeError("hermes_home_unavailable") from exc


ROOT = _active_hermes_home()
STATE = ROOT / "plugin-data" / "session-complete-telegram"
_NOTIFIER = None
# In-memory reject-only aid when the separate inbox is briefly unavailable.
# Values contain identifiers only, never command text or callback capabilities.
_KNOWN_CARDS: set[tuple[int, int, int]] = set()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _response_bytes(value):
    """The native result's exact UTF-8 JSON payload before its ASCII frame."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8", errors="strict")).hexdigest()


def _sibling(name):
    if __package__:
        return importlib.import_module(f".{name}", __package__)
    return importlib.import_module(name)


def local_notifier():
    if _NOTIFIER is not None:
        return _NOTIFIER
    return _sibling("notifier")


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(state=STATE):
    directory = Path(state) / "approval-inbox"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # POSIX permissions are made owner-only; on Windows the directory inherits the
    # existing plugin-data ACL exactly like the sibling inboxes (no ACL broadening).
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    path = directory / "approvals.sqlite3"
    c = sqlite3.connect(path, timeout=8, isolation_level=None, factory=ClosingConnection)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA secure_delete=ON")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS receiver(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS owners(owner TEXT PRIMARY KEY, seen REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS approvals(
      id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, owner TEXT NOT NULL,
      descriptor TEXT NOT NULL, request_json TEXT NOT NULL, pending_json TEXT NOT NULL,
      request_fingerprint TEXT NOT NULL, pending_fingerprint TEXT NOT NULL,
      review_text TEXT, token TEXT UNIQUE NOT NULL, bot INTEGER NOT NULL,
      chat INTEGER NOT NULL, topic INTEGER, boot TEXT NOT NULL, message_id INTEGER,
      created REAL NOT NULL, expires REAL NOT NULL, seen REAL NOT NULL,
      status TEXT NOT NULL, choice TEXT, claim TEXT, deadline REAL,
      dirty INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX IF NOT EXISTS approval_status ON approvals(status, expires);
    CREATE INDEX IF NOT EXISTS approval_owner_status ON approvals(owner, status, created);
    CREATE TABLE IF NOT EXISTS receipts(event TEXT PRIMARY KEY, created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS tombstones(
      bot INTEGER NOT NULL, chat INTEGER NOT NULL, mid INTEGER NOT NULL,
      created REAL NOT NULL, PRIMARY KEY(bot, chat, mid));
    """)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return c


def connect_readonly(state=STATE):
    """Open an existing approval inbox without creating schema or changing rows."""
    path = Path(state) / "approval-inbox" / "approvals.sqlite3"
    if not path.is_file():
        return None
    c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=8,
                        isolation_level=None, factory=ClosingConnection)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    return c


def _retire(c, row, status, now):
    if status not in TERMINAL:
        raise ValueError("invalid_terminal_status")
    if row["message_id"] is not None:
        c.execute("INSERT OR IGNORE INTO tombstones VALUES(?,?,?,?)",
                  (row["bot"], row["chat"], row["message_id"], now))
    c.execute("""UPDATE approvals SET status=?,choice=NULL,claim=NULL,deadline=NULL,
      dirty=CASE WHEN message_id IS NULL THEN dirty ELSE 1 END,
      review_text=NULL,request_json='{}',pending_json='{}' WHERE id=?""", (status, row["id"]))


def _expire_owner(c, owner, now):
    for row in c.execute("SELECT * FROM approvals WHERE owner=? AND status IN ('posting','open','queued','applying')",
                         (owner,)).fetchall():
        status = "unknown" if row["status"] in ("posting", "applying") else (
            "rejected" if row["status"] == "queued" else "expired")
        _retire(c, row, status, now)


def _prune(c, now):
    for row in c.execute("SELECT * FROM approvals WHERE status IN ('posting','open','queued','applying') AND expires<=?",
                         (now,)).fetchall():
        status = "unknown" if row["status"] in ("posting", "applying") else (
            "rejected" if row["status"] == "queued" else "expired")
        _retire(c, row, status, now)
    for row in c.execute("SELECT * FROM approvals WHERE status IN ('posting','open','queued','applying') AND seen<?",
                         (now - LEASE,)).fetchall():
        status = "unknown" if row["status"] in ("posting", "applying") else (
            "rejected" if row["status"] == "queued" else "expired")
        _retire(c, row, status, now)
    # A receiver or owner lease gap is terminal for an already queued choice.
    live_row = c.execute("SELECT value FROM receiver WHERE key='live'").fetchone()
    try:
        live = json.loads(live_row[0]) if live_row else None
        gap = live is not None and (now - float(live["at"]) > LEASE or now < float(live["at"]))
    except (TypeError, ValueError, KeyError):
        live, gap = None, True
    if gap:
        for row in c.execute("SELECT * FROM approvals WHERE status IN ('queued','applying')").fetchall():
            _retire(c, row, "unknown" if row["status"] == "applying" else "rejected", now)
    for owner_row in c.execute("SELECT owner,seen FROM owners WHERE seen<? OR seen>?", (now - LEASE, now)).fetchall():
        _expire_owner(c, owner_row["owner"], now)


def _receiver(c, now):
    row = c.execute("SELECT value FROM receiver WHERE key='live'").fetchone()
    try:
        live = json.loads(row[0]) if row else None
    except (TypeError, ValueError):
        return None
    try:
        valid = (isinstance(live, dict) and live.get("version") == VERSION and
                 live.get("capability") == CAPABILITY and live.get("private_verified") is True and
                 isinstance(live.get("boot"), str) and bool(live["boot"]) and
                 type(live.get("bot")) is int and live["bot"] > 0 and
                 type(live.get("chat")) is int and live["chat"] > 0 and
                 0 <= now - float(live.get("at", -1)) <= LEASE)
    except (TypeError, ValueError, OverflowError):
        return None
    return live if valid else None


def bind_receiver(bot, chat, topic, boot, *, private_verified, state=STATE, now=None,
                  version=VERSION, capability=CAPABILITY):
    now = time.time() if now is None else now
    if (type(bot) is not int or bot <= 0 or type(chat) is not int or chat <= 0 or
            not isinstance(boot, str) or not boot or type(private_verified) is not bool):
        return False
    if topic is not None and (type(topic) is not int or topic <= 0):
        return False
    if not private_verified or version != VERSION or capability != CAPABILITY:
        return False
    c = connect(state)
    try:
        c.execute("BEGIN IMMEDIATE")
        old_row = c.execute("SELECT value FROM receiver WHERE key='live'").fetchone()
        try:
            old = json.loads(old_row[0]) if old_row else None
        except (TypeError, ValueError):
            old = None
        if old and (old.get("boot") != boot or old.get("bot") != bot or old.get("chat") != chat or
                    old.get("topic") != topic or old.get("version") != VERSION or
                    now - float(old.get("at", now)) > LEASE):
            for row in c.execute("SELECT * FROM approvals WHERE status IN ('posting','open','queued','applying')").fetchall():
                status = "unknown" if row["status"] in ("posting", "applying") else (
                    "rejected" if row["status"] == "queued" else "expired")
                _retire(c, row, status, now)
        c.execute("INSERT OR REPLACE INTO receiver VALUES('live',?)",
                  (canonical({"bot": bot, "chat": chat, "topic": topic, "boot": boot,
                              "at": now, "private_verified": True, "version": VERSION,
                              "capability": CAPABILITY}),))
        c.commit()
        return True
    finally:
        c.close()


def receiver_info(*, state=STATE, now=None):
    now = time.time() if now is None else now
    c = connect(state)
    try:
        return _receiver(c, now)
    finally:
        c.close()


def owner_heartbeat(owner, *, state=STATE, now=None):
    now = time.time() if now is None else now
    if not isinstance(owner, str) or not owner or len(owner) > 100:
        return {"ok": False, "feature_ready": False, "reason": "invalid_owner"}
    c = connect(state)
    try:
        c.execute("BEGIN IMMEDIATE")
        _prune(c, now)
        live = _receiver(c, now)
        if live is None:
            c.commit()
            return {"ok": False, "feature_ready": False, "reason": "receiver_unavailable"}
        old = c.execute("SELECT seen FROM owners WHERE owner=?", (owner,)).fetchone()
        if old and (now < old["seen"] or now - old["seen"] > LEASE):
            _expire_owner(c, owner, now)
        c.execute("INSERT INTO owners VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET seen=excluded.seen",
                  (owner, now))
        c.commit()
        return {"ok": True, "feature_ready": True, "version": VERSION, "capability": CAPABILITY}
    finally:
        c.close()


def _owner_live(c, owner, now):
    row = c.execute("SELECT seen FROM owners WHERE owner=?", (owner,)).fetchone()
    return bool(row and 0 <= now - float(row["seen"]) <= LEASE)


def _valid_queue_id(value):
    return isinstance(value, str) and len(value) == 32 and all(ch in "0123456789abcdef" for ch in value)


def supported_pair(request, pending, runtime_sid):
    """Match only the pinned core command-gate queue shape; never generic approvals."""
    if (not isinstance(request, dict) or request.get("method") != "approval" or
            not isinstance(pending, dict) or not isinstance(runtime_sid, str) or not runtime_sid):
        return False
    params = request.get("params")
    if not isinstance(params, dict):
        return False
    try:
        if (not isinstance(request.get("id"), str) or
                not __import__("re").fullmatch(r"srq-[0-9a-f]{4,32}", request["id"]) or
                params.get("session_id") != runtime_sid or
                params.get("request_id") != pending.get("request_id") or
                not _valid_queue_id(pending.get("request_id"))):
            return False
        queue_keys = {"request_id", "command", "description", "pattern_key", "pattern_keys",
                      "allow_permanent", "allow_session"}
        if "smart_denied" in pending:
            queue_keys.add("smart_denied")
        if set(pending) != queue_keys:
            return False
        expected_params = queue_keys | {"session_id", "choices"}
        if set(params) != expected_params:
            return False
        if "smart_denied" in params and params["smart_denied"] is not False:
            return False
        if pending.get("smart_denied") is True or params.get("allow_session") is not True:
            return False
        key = pending.get("pattern_key")
        keys = pending.get("pattern_keys")
        if (not isinstance(key, str) or not key or key.startswith(("plugin_rule:", "tirith:", "execute_code")) or
                not isinstance(keys, list) or keys != [key] or
                params.get("pattern_key") != key or params.get("pattern_keys") != keys):
            return False
        if type(pending.get("allow_permanent")) is not bool or params.get("allow_permanent") is not pending["allow_permanent"]:
            return False
        if params.get("allow_session") is not pending["allow_session"]:
            return False
        choices = params.get("choices")
        expected_choices = ["once", "session", "always", "deny"] if pending["allow_permanent"] else ["once", "session", "deny"]
        if choices != expected_choices:
            return False
        command, description = pending.get("command"), pending.get("description")
        if (not isinstance(command, str) or not command or not isinstance(description, str) or not description or
                params.get("command") != command or params.get("description") != description):
            return False
        # Use the real pinned producer, not a copied classifier or a stub. This
        # excludes execute_code, plugin_rule, Tirith-only, and unknown queue entries.
        from tools.approval_detection import DANGEROUS_PATTERNS, detect_dangerous_command
        known = {description for _, description in DANGEROUS_PATTERNS}
        if key not in known:
            return False
        detected = detect_dangerous_command(command)
        if detected != (True, key, key):
            return False
        return True
    except Exception:
        return False


def _redact_literal(value):
    from agent.redact import redact_sensitive_text
    if not isinstance(value, str):
        raise ValueError("invalid_review_text")
    cleaned = redact_sensitive_text(value, force=True, redact_url_credentials=True)
    if not isinstance(cleaned, str) or redact_sensitive_text(cleaned, force=True, redact_url_credentials=True) != cleaned:
        raise ValueError("redaction_not_stable")
    # If a credential-shaped assignment survived the real core redactor intact,
    # do not expose it in an actionable approval card.
    for match in re.finditer(r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)\s*[:=]\s*([^\s;&|]+)", value):
        secret = match.group(1)
        if len(secret) >= 8 and secret in cleaned:
            raise ValueError("redaction_omission")
    if unicodedata.normalize("NFC", cleaned) != cleaned:
        raise ValueError("review_unicode_ambiguous")
    for char in cleaned:
        category = unicodedata.category(char)
        if (category in {"Cf", "Cs", "Zl", "Zp"} or
                (category == "Cc" and char not in ("\n", "\t"))):
            raise ValueError("review_control_character")
    return cleaned


def review_card(request, timeout_seconds):
    params = request["params"]
    command = _redact_literal(params["command"])
    description = _redact_literal(params["description"])
    if not command.strip() or not description.strip():
        raise ValueError("review_text_missing")
    raw_command, raw_description = params["command"], params["description"]
    if raw_command != raw_description and command == description:
        raise ValueError("redaction_collision")
    # No Markdown parsing, truncation, preview, or hidden command tail. Telegram
    # receives the entire literal redacted command or no actionable card.
    text = ("⚠️ Hermes Desktop 명령 승인\n\n"
            f"설명\n{description}\n\n명령 (전체)\n$ {command}\n\n"
            f"이 명령만 한 번 허용하거나 거부합니다. {int(timeout_seconds)}초 후 만료됩니다.\n"
            "다른 명령·세션에 대한 권한은 저장되지 않습니다.")
    if len(text.encode("utf-16-le", errors="strict")) // 2 > MAX_REVIEW_UNITS:
        raise ValueError("review_card_too_large")
    return text


def _approval_timeout(profile, notifier, root):
    """Mirror the pinned approvals.timeout default/clamp; never apply clarify's 1h floor."""
    config = {}
    try:
        import yaml
        home = notifier.profile_home(profile, root=root)
        config_path = home / "config.yaml"
        if config_path.is_file():
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8-sig"))
            if loaded is None:
                loaded = {}
            if not isinstance(loaded, dict):
                return None
            config = loaded
        approvals = config.get("approvals", {})
        if not isinstance(approvals, dict):
            return None
        raw = approvals.get("timeout", 300)
    except Exception:
        return None
    try:
        seconds = int(raw)
    except (TypeError, ValueError, OverflowError):
        seconds = 300
    try:
        from agent.deadline import MAX_SAFE_TIMEOUT_S
        seconds = min(seconds, int(MAX_SAFE_TIMEOUT_S))
    except Exception:
        seconds = min(seconds, MAX_TTL)
    return max(seconds, 0)


def _identity_base(descriptor):
    keys = ("connectionId", "profile", "targetProfile", "sid", "session_key", "epoch",
            "request_id", "queue_id")
    return {key: descriptor[key] for key in keys}


def validate_snapshot(descriptor, request, pending, route, row, epoch, owner):
    """Full client-side snapshot/route/session authority matrix, fail-closed."""
    required = ("owner", "connectionId", "profile", "targetProfile", "sid", "session_key",
                "epoch", "request_id", "queue_id", "request_fingerprint", "pending_fingerprint")
    if not isinstance(descriptor, dict) or set(descriptor) != set(required):
        return False
    try:
        if descriptor["owner"] != owner or not supported_pair(request, pending, descriptor["sid"]):
            return False
        if request["id"] != descriptor["request_id"] or pending["request_id"] != descriptor["queue_id"]:
            return False
        if _digest(request) != descriptor["request_fingerprint"] or _digest(pending) != descriptor["pending_fingerprint"]:
            return False
        if not isinstance(route, dict) or route.get("mode") != "local":
            return False
        if any(route.get(key) != descriptor[key] for key in ("connectionId", "profile", "targetProfile")):
            return False
        if not isinstance(row, dict) or row.get("id") != descriptor["sid"] or row.get("session_key") != descriptor["session_key"] or row.get("status") != "waiting":
            return False
        if str(epoch) != descriptor["epoch"] or not descriptor["epoch"]:
            return False
        return True
    except (KeyError, TypeError, ValueError, RecursionError):
        return False


def _validate_descriptor(descriptor):
    required = {"owner", "connectionId", "profile", "targetProfile", "sid", "session_key",
                "epoch", "request_id", "queue_id", "request_fingerprint", "pending_fingerprint"}
    if not isinstance(descriptor, dict) or set(descriptor) != required:
        return False
    if any(not isinstance(descriptor.get(key), str) or not descriptor[key] for key in required):
        return False
    if len(canonical(descriptor).encode("utf-8")) > 12000:
        return False
    return (descriptor["request_id"].startswith("srq-") and _valid_queue_id(descriptor["queue_id"]) and
            all(len(descriptor[key]) == 64 and all(c in "0123456789abcdef" for c in descriptor[key])
                for key in ("request_fingerprint", "pending_fingerprint")))


def claim_valid(owner, approval_id, claim, descriptor, authority, deadline, *,
                root=ROOT, state=STATE, now=None):
    """Read-only last freshness gate for one applying approval claim.

    This is a fresh local check, not an atomic server-side fingerprint CAS. It
    never renews, reclaims, settles, or changes the claim and returns no command.
    """
    now = time.time() if now is None else now
    if (not isinstance(owner, str) or not owner or len(owner) > 100 or
            not isinstance(approval_id, str) or not re.fullmatch(r"[0-9a-f]{32}", approval_id) or
            not isinstance(claim, str) or not re.fullmatch(r"[0-9a-f]{32}", claim) or
            not isinstance(authority, str) or not re.fullmatch(r"[0-9a-f]{64}", authority) or
            type(deadline) not in (int, float) or not isinstance(deadline, (int, float)) or
            not math.isfinite(deadline) or not _validate_descriptor(descriptor)):
        return {"ok": False, "reason": "invalid_claim"}
    try:
        expected_authority = _digest(descriptor)
        if authority != expected_authority:
            return {"ok": True, "valid": False, "id": approval_id, "claim": claim,
                    "authority": authority, "deadline": deadline}
        if __package__:
            gateway = importlib.import_module('.telegram_integration', __package__)
        else:
            gateway = importlib.import_module('telegram_integration')
        if not gateway.feature_enabled('approvals_enabled', root=root):
            return {"ok": True, "valid": False, "id": approval_id, "claim": claim,
                    "authority": authority, "deadline": deadline}
        c = connect_readonly(state)
        if c is None:
            return {"ok": True, "valid": False, "id": approval_id, "claim": claim,
                    "authority": authority, "deadline": deadline}
        try:
            row = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            live = _receiver(c, now)
            valid = False
            if row is not None and live is not None:
                try:
                    stored_descriptor = json.loads(row["descriptor"])
                    valid = bool(
                        row["status"] == "applying" and row["owner"] == owner and row["claim"] == claim and
                        row["deadline"] is not None and float(row["deadline"]) == float(deadline) and
                        float(row["deadline"]) > now and float(row["expires"]) > now and
                        0 <= now - float(row["seen"]) <= LEASE and _owner_live(c, owner, now) and
                        row["boot"] == live["boot"] and
                        (row["bot"], row["chat"], row["topic"]) ==
                        (live["bot"], live["chat"], live["topic"]) and
                        stored_descriptor == descriptor and canonical(stored_descriptor) == row["descriptor"] and
                        _digest(stored_descriptor) == authority and
                        row["request_fingerprint"] == descriptor["request_fingerprint"] and
                        row["pending_fingerprint"] == descriptor["pending_fingerprint"])
                except (TypeError, ValueError, KeyError, OverflowError, RecursionError):
                    valid = False
            return {"ok": True, "valid": valid, "id": approval_id, "claim": claim,
                    "authority": authority, "deadline": deadline}
        finally:
            c.close()
    except Exception:
        # No exception, command, descriptor, or credential material crosses the frame.
        return {"ok": False, "reason": "approval_store_unavailable"}


def register(req, notifier, *, root=ROOT, state=STATE, now=None, result_budget=None):
    now = time.time() if now is None else now
    if result_budget is not None and (type(result_budget) is not int or result_budget <= 0):
        return {"ok": False, "reason": "invalid_result_budget"}
    descriptor, request, pending = req.get("descriptor"), req.get("request"), req.get("pending")
    route, row, epoch, owner = req.get("route"), req.get("session"), req.get("epoch"), req.get("owner")
    if not _validate_descriptor(descriptor) or descriptor.get("owner") != owner:
        return {"ok": False, "reason": "invalid_descriptor"}
    if not validate_snapshot(descriptor, request, pending, route, row, epoch, owner):
        return {"ok": False, "reason": "stale_snapshot"}
    if descriptor["request_fingerprint"] != _digest(request) or descriptor["pending_fingerprint"] != _digest(pending):
        return {"ok": False, "reason": "fingerprint_mismatch"}
    timeout = _approval_timeout(descriptor["targetProfile"], notifier, root)
    if timeout is None:
        return {"ok": False, "reason": "timeout_unavailable"}
    if timeout <= 0:
        return {"ok": False, "reason": "approval_expired"}
    expires = now + min(timeout, MAX_TTL)
    try:
        text = review_card(request, timeout)
    except Exception as exc:
        # Exception text may contain command material; return only a stable category.
        return {"ok": False, "reason": str(exc) if str(exc).startswith(("review_", "redaction_")) else "review_unavailable"}
    if result_budget is not None:
        projected_item = {"id": "0" * 32, "claim": "0" * 32,
                          "deadline": min(float(expires), now + LEASE),
                          "descriptor": descriptor, "request": request,
                          "pending": pending, "choice": "once"}
        try:
            if len(_response_bytes({"ok": True, "items": [projected_item], "terminal": []})) > result_budget:
                return {"ok": False, "reason": "approval_result_too_large"}
        except (TypeError, ValueError, UnicodeError, RecursionError):
            return {"ok": False, "reason": "approval_result_too_large"}
    identity = _digest(_identity_base(descriptor))
    c = connect(state)
    try:
        c.execute("BEGIN IMMEDIATE")
        _prune(c, now)
        live = _receiver(c, now)
        if not live:
            c.commit()
            return {"ok": False, "feature_ready": False, "reason": "receiver_unavailable"}
        if not _owner_live(c, owner, now):
            c.commit()
            return {"ok": False, "feature_ready": False, "reason": "owner_unavailable"}
        token, chat, topic = notifier.credentials(root)
        bot = int(token.split(":", 1)[0])
        if (live["bot"], live["chat"], live["topic"]) != (bot, chat, topic) or chat <= 0:
            c.commit()
            return {"ok": False, "feature_ready": False, "reason": "destination_mismatch"}
        prior = c.execute("SELECT * FROM approvals WHERE identity=?", (identity,)).fetchone()
        if prior:
            try:
                prior_descriptor = json.loads(prior["descriptor"])
            except (TypeError, ValueError):
                prior_descriptor = {}
            if (prior_descriptor.get("request_fingerprint") != descriptor["request_fingerprint"] or
                    prior_descriptor.get("pending_fingerprint") != descriptor["pending_fingerprint"]):
                if prior["status"] in ACTIVE:
                    status = "unknown" if prior["status"] in ("posting", "applying") else (
                        "rejected" if prior["status"] == "queued" else "expired")
                    _retire(c, prior, status, now)
                c.commit()
                return {"ok": False, "reason": "immutable_mismatch"}
            if prior["owner"] != owner:
                c.commit()
                return {"ok": False, "reason": "prior_owner"}
            c.commit()
            return {"ok": prior["status"] in ACTIVE, "duplicate": True,
                    "id": prior["id"], "status": prior["status"]}
        if c.execute("SELECT 1 FROM approvals WHERE identity=?", (identity,)).fetchone():
            c.commit()
            return {"ok": False, "reason": "duplicate"}
        if c.execute("SELECT count(*) FROM approvals WHERE status IN ('posting','open','queued','applying')").fetchone()[0] >= MAX_ACTIVE:
            c.commit()
            return {"ok": False, "reason": "capacity"}
        approval_id = secrets.token_hex(16)
        callback_token = secrets.token_urlsafe(18)
        c.execute("INSERT INTO approvals(id,identity,owner,descriptor,request_json,pending_json,request_fingerprint,pending_fingerprint,review_text,token,bot,chat,topic,boot,created,expires,seen,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'posting')",
                  (approval_id, identity, owner, canonical(descriptor), canonical(request), canonical(pending),
                   descriptor["request_fingerprint"], descriptor["pending_fingerprint"], text, callback_token,
                   bot, chat, topic, live["boot"], now, expires, now))
        c.commit()
    except sqlite3.IntegrityError:
        c.rollback()
        return {"ok": False, "reason": "duplicate"}
    finally:
        c.close()
    result = publish(approval_id, notifier, state=state)
    return {**result, "id": approval_id, "status": result.get("status", "unknown"), "expires": expires}


def _keyboard(row):
    return {"inline_keyboard": [[
        {"text": "Allow Once", "callback_data": PREFIX + row["token"] + ":o"},
        {"text": "Deny", "callback_data": PREFIX + row["token"] + ":d"},
    ]]}


def publish(approval_id, notifier, *, state=STATE):
    c = connect(state)
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        now = time.time()
        live = _receiver(c, now)
        if not row or row["status"] != "posting" or not live or live["boot"] != row["boot"]:
            c.rollback()
            if row and row["status"] == "posting":
                with connect(state) as other:
                    other.execute("BEGIN IMMEDIATE")
                    fresh = other.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
                    if fresh and fresh["status"] == "posting":
                        _retire(other, fresh, "unknown", now)
            return {"ok": False, "status": "unknown", "reason": "posting_authority_lost"}
        c.commit()
        c.close()
        text = row["review_text"]
        markup = _keyboard(row)
        payload = {"chat_id": row["chat"], "text": text, "reply_markup": markup,
                   "disable_notification": False, "protect_content": False,
                   "link_preview_options": {"is_disabled": True}}
        if row["topic"] is not None:
            payload["message_thread_id"] = row["topic"]
        answer = notifier.telegram(notifier.credentials()[0], "sendMessage", payload)
        message = answer.get("result") if isinstance(answer, dict) else None
        if not isinstance(message, dict):
            message = {}
        sent = bool(answer.get("ok") is True and type(message.get("message_id")) is int and
                    message.get("message_id") > 0 and message.get("text") == text and
                    message.get("chat", {}).get("id") == row["chat"] and
                    message.get("chat", {}).get("type") == "private" and
                    message.get("from", {}).get("id") == row["bot"] and
                    message.get("from", {}).get("is_bot") is True and
                    message.get("message_thread_id") == row["topic"])
        if not sent:
            _finish_post(approval_id, "unknown" if answer.get("ambiguous") else "rejected", state)
            return {"ok": False, "status": "unknown" if answer.get("ambiguous") else "rejected", "reason": "send_unverified"}
        mid = message["message_id"]
        _KNOWN_CARDS.add((row["bot"], row["chat"], mid))
        # Persist the exact outbound id before verification: after a crash, replies
        # to a card sent in the posting window remain consumed but never actionable.
        c = connect(state)
        try:
            c.execute("BEGIN IMMEDIATE")
            fresh = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if not fresh or fresh["status"] != "posting":
                c.rollback()
                return {"ok": False, "status": "unknown", "reason": "posting_race"}
            c.execute("UPDATE approvals SET message_id=? WHERE id=? AND status='posting'", (mid, approval_id))
            c.commit()
        finally:
            c.close()
        # Same-text edit proves the addressed message still has the exact content
        # and the same keyboard; never drop buttons during verification.
        verify = notifier.telegram(notifier.credentials()[0], "editMessageText",
                                  {"chat_id": row["chat"], "message_id": mid, "text": text,
                                   "reply_markup": markup, "link_preview_options": {"is_disabled": True}})
        verified = bool((verify.get("ok") is True and verify.get("result", {}).get("message_id") == mid) or
                        (verify.get("error_code") == 400 and "message is not modified" in
                         str(verify.get("description", "")).lower()))
        if not verified:
            _finish_post(approval_id, "unknown", state, message_id=mid)
            return {"ok": False, "status": "unknown", "reason": "send_verification_unknown"}
        c = connect(state)
        try:
            c.execute("BEGIN IMMEDIATE")
            fresh = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            live = _receiver(c, time.time())
            if (not fresh or fresh["status"] != "posting" or not live or
                    fresh["boot"] != live["boot"] or fresh["expires"] <= time.time()):
                if fresh and fresh["status"] == "posting":
                    _retire(c, fresh, "unknown", time.time())
                c.commit()
                return {"ok": False, "status": "unknown", "reason": "post_settle_unknown"}
            c.execute("UPDATE approvals SET message_id=?,status='open',seen=?,dirty=0 WHERE id=? AND status='posting'",
                      (mid, time.time(), approval_id))
            c.execute("INSERT OR IGNORE INTO tombstones VALUES(?,?,?,?)", (row["bot"], row["chat"], mid, time.time()))
            # An active row remains discoverable; tombstones only keep its message routed.
            c.execute("DELETE FROM tombstones WHERE bot=? AND chat=? AND mid=?", (row["bot"], row["chat"], mid))
            c.commit()
        finally:
            c.close()
        return {"ok": True, "status": "open", "verified": True, "message_id": mid}
    except Exception:
        _finish_post(approval_id, "unknown", state)
        return {"ok": False, "status": "unknown", "reason": "send_unknown"}
    finally:
        c.close()


def _finish_post(approval_id, status, state, message_id=None):
    c = connect(state)
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row and row["status"] == "posting":
            if message_id is not None:
                c.execute("UPDATE approvals SET message_id=? WHERE id=?", (message_id, approval_id))
                row = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            _retire(c, row, status if status in TERMINAL else "unknown", time.time())
        c.commit()
    finally:
        c.close()


def lookup_message(bot, chat, mid, *, state=STATE):
    if type(bot) is not int or type(chat) is not int or type(mid) is not int:
        return False
    if (bot, chat, mid) in _KNOWN_CARDS:
        return True
    c = connect(state)
    try:
        row = c.execute("SELECT 1 FROM approvals WHERE bot=? AND chat=? AND message_id=? LIMIT 1",
                        (bot, chat, mid)).fetchone()
        if row:
            return True
        return bool(c.execute("SELECT 1 FROM tombstones WHERE bot=? AND chat=? AND mid=?",
                              (bot, chat, mid)).fetchone())
    finally:
        c.close()


def _message_identity_ok(row, identity):
    return bool(row and row["bot"] == identity.get("bot") and row["chat"] == identity.get("chat") == identity.get("user") and
                row["topic"] == identity.get("topic") and identity.get("private") is True and
                identity.get("human") is True and identity.get("bot_author") is True and
                identity.get("forwarded") is False and identity.get("edited") is False and
                identity.get("inline") is False and identity.get("inaccessible") is False and
                identity.get("boot") == row["boot"] and row["message_id"] == identity.get("mid") and
                isinstance(row["review_text"], str) and identity.get("message_text") == row["review_text"])


def choose(token, choice, identity, *, state=STATE, now=None):
    """Accept one button only after every authority check; rejected taps do not write."""
    now = time.time() if now is None else now
    if not isinstance(token, str) or not token or choice not in ALLOWED_CHOICES:
        return {"ok": False, "reason": "invalid_choice"}
    if not isinstance(identity, dict) or not isinstance(identity.get("event"), str) or not identity["event"]:
        return {"ok": False, "reason": "invalid_event"}
    c = connect(state)
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM approvals WHERE token=?", (token,)).fetchone()
        live = _receiver(c, now)
        if (not row or row["status"] != "open" or row["expires"] <= now or
                now < row["seen"] or now - row["seen"] > LEASE or
                not _message_identity_ok(row, identity) or not live or
                live["boot"] != row["boot"] or
                (live["bot"], live["chat"], live["topic"]) != (row["bot"], row["chat"], row["topic"]) or
                not _owner_live(c, row["owner"], now)):
            c.rollback()
            return {"ok": False, "reason": "expired_or_unauthorized"}
        if c.execute("SELECT 1 FROM receipts WHERE event=?", (identity["event"],)).fetchone():
            c.rollback()
            return {"ok": False, "reason": "duplicate_event"}
        c.execute("INSERT INTO receipts VALUES(?,?)", (identity["event"], now))
        c.execute("UPDATE approvals SET status='queued',choice=?,dirty=1 WHERE id=? AND status='open'",
                  (choice, row["id"]))
        if c.total_changes < 2:
            c.rollback()
            return {"ok": False, "reason": "claim_lost"}
        c.commit()
        return {"ok": True, "status": "queued", "id": row["id"]}
    except Exception:
        c.rollback()
        return {"ok": False, "reason": "store_unavailable"}
    finally:
        c.close()


def _observation_matches(row, observation):
    try:
        d = json.loads(row["descriptor"])
        return (isinstance(observation, dict) and observation.get("id") == row["id"] and
                observation.get("request_fingerprint") == row["request_fingerprint"] and
                observation.get("pending_fingerprint") == row["pending_fingerprint"] and
                observation.get("authority") == _digest(d))
    except (TypeError, ValueError, RecursionError):
        return False


def poll(owner, observations, *, unverified=None, absent=None, state=STATE, now=None, result_budget=None):
    now = time.time() if now is None else now
    if (not isinstance(owner, str) or not owner or not isinstance(observations, list) or len(observations) > 100 or
            (unverified is not None and (not isinstance(unverified, list) or len(unverified) > 100)) or
            (absent is not None and (not isinstance(absent, list) or len(absent) > 100)) or
            (result_budget is not None and (type(result_budget) is not int or result_budget <= 0))):
        return {"ok": False, "reason": "invalid_poll", "items": []}
    by_id = {x.get("id"): x for x in observations if isinstance(x, dict) and isinstance(x.get("id"), str)}
    if len(by_id) != len(observations):
        return {"ok": False, "reason": "invalid_observations", "items": []}
    unverifiable = set(x for x in (unverified or []) if isinstance(x, str))
    definitive_absent = set(x for x in (absent or []) if isinstance(x, str))
    c = connect(state)
    try:
        c.execute("BEGIN IMMEDIATE")
        _prune(c, now)
        live = _receiver(c, now)
        if not live or not _owner_live(c, owner, now):
            c.commit()
            return {"ok": False, "feature_ready": False, "reason": "lease_stale", "items": []}
        candidates = []
        for row in c.execute("SELECT * FROM approvals WHERE owner=? AND status IN ('posting','open','queued','applying') ORDER BY created,id",
                             (owner,)).fetchall():
            if row["boot"] != live["boot"]:
                status = "unknown" if row["status"] in ("posting", "applying") else (
                    "rejected" if row["status"] == "queued" else "expired")
                _retire(c, row, status, now)
                continue
            if row["id"] in unverifiable:
                if row["status"] == "queued":
                    _retire(c, row, "rejected", now)
                elif row["status"] == "applying":
                    _retire(c, row, "unknown", now)
                elif row["status"] == "open":
                    c.execute("UPDATE approvals SET seen=? WHERE id=?", (STALE_SEEN, row["id"]))
                continue
            if row["id"] in definitive_absent or row["id"] not in by_id:
                status = "unknown" if row["status"] in ("posting", "applying") else (
                    "rejected" if row["status"] == "queued" else "expired")
                _retire(c, row, status, now)
                continue
            if not _observation_matches(row, by_id[row["id"]]):
                if row["status"] == "queued":
                    _retire(c, row, "rejected", now)
                elif row["status"] == "applying":
                    _retire(c, row, "unknown", now)
                else:
                    _retire(c, row, "expired", now)
                continue
            if row["status"] in ("open", "queued"):
                c.execute("UPDATE approvals SET seen=? WHERE id=?", (now, row["id"]))
            if row["status"] != "queued":
                continue
            claim = secrets.token_hex(16)
            deadline = min(float(row["expires"]), now + LEASE)
            item = {"id": row["id"], "claim": claim, "deadline": deadline,
                    "descriptor": json.loads(row["descriptor"]),
                    "request": json.loads(row["request_json"]),
                    "pending": json.loads(row["pending_json"]), "choice": row["choice"]}
            if (result_budget is not None and
                    len(_response_bytes({"ok": True, "items": [item], "terminal": []})) > result_budget):
                # A single queued row that cannot fit even without terminal IDs
                # cannot become an actionable button. Retire it explicitly; its
                # terminal ID is included below whenever it was observed.
                _retire(c, row, "rejected", now)
                continue
            candidates.append((row, item))
        known_ids = set(by_id) | unverifiable | definitive_absent
        all_terminal = [row["id"] for row in c.execute(
            "SELECT id FROM approvals WHERE owner=? AND status IN ('applied','expired','rejected','unknown') ORDER BY created,id",
            (owner,)).fetchall() if row["id"] in known_ids]

        def fits(value):
            return result_budget is None or len(_response_bytes(value)) <= result_budget

        terminal = []
        for terminal_id in all_terminal:
            trial = {"ok": True, "items": [], "terminal": [*terminal, terminal_id]}
            if fits(trial):
                terminal.append(terminal_id)
        # Do not consume a claim if any known terminal ID cannot be returned in
        # this frame. The omitted IDs remain terminal in SQLite and are reoffered
        # on the next poll; no authority is lost.
        item = None
        if len(terminal) == len(all_terminal):
            for row, candidate in candidates:
                projected = {"ok": True, "items": [candidate], "terminal": terminal}
                if not fits(projected):
                    # The item was checked alone above. If terminal IDs push it
                    # over budget, defer both claim and TTL/claim mutation.
                    break
                updated = c.execute(
                    "UPDATE approvals SET status='applying',claim=?,deadline=?,dirty=1 WHERE id=? AND status='queued'",
                    (candidate["claim"], candidate["deadline"], row["id"]))
                if updated.rowcount == 1:
                    item = candidate
                    break
        c.commit()
        items = [item] if item is not None else []
        return {"ok": True, "items": items, "terminal": terminal}
    except Exception:
        c.rollback()
        return {"ok": False, "reason": "poll_unavailable", "items": []}
    finally:
        c.close()


def settle(approval_id, claim, status, *, state=STATE, now=None):
    now = time.time() if now is None else now
    if status not in TERMINAL or not isinstance(approval_id, str) or not isinstance(claim, str):
        return {"ok": False, "reason": "invalid_settlement"}
    c = connect(state)
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM approvals WHERE id=? AND claim=? AND status='applying'",
                        (approval_id, claim)).fetchone()
        if not row or row["deadline"] is None or row["deadline"] <= now:
            if row:
                _retire(c, row, "unknown", now)
            c.commit()
            return {"ok": False, "reason": "claim_lost"}
        _retire(c, row, status, now)
        c.commit()
        return {"ok": True, "status": status}
    finally:
        c.close()


def flush(notifier, *, state=STATE):
    c = connect(state)
    try:
        _prune(c, time.time())
        rows = [dict(r) for r in c.execute("SELECT * FROM approvals WHERE dirty=1 AND message_id IS NOT NULL LIMIT 20")]
    finally:
        c.close()
    if not rows:
        return
    token, chat, topic = notifier.credentials()
    for row in rows:
        if (row["chat"], row["topic"]) != (chat, topic):
            continue
        if row["status"] == "open" and row["review_text"]:
            text, markup = row["review_text"], _keyboard(row)
        else:
            text = "⚠️ Hermes Desktop 명령 승인\n\n요청은 종료되었습니다. 필요한 경우 Desktop에서 현재 승인 상태를 다시 확인해 주세요."
            if row["status"] == "queued":
                text = "⚠️ Hermes Desktop 명령 승인\n\n⏳ 응답을 받았습니다. Desktop 연결과 현재 승인을 확인 중입니다."
            elif row["status"] == "applying":
                text = "⚠️ Hermes Desktop 명령 승인\n\n⏳ Desktop에 응답을 전달하고 있습니다. 완료 여부를 확인 중입니다."
            elif row["status"] == "applied":
                text = "⚠️ Hermes Desktop 명령 승인\n\n✅ Desktop이 이 명령 승인 응답을 받았습니다. 실행 완료 여부는 별도로 확인해 주세요."
            elif row["status"] == "unknown":
                text = "⚠️ Hermes Desktop 명령 승인\n\n⚠️ 응답 전달 결과를 확인하지 못했습니다. Desktop에서 확인해 주세요. 다시 누르거나 재전송하지 않습니다."
            elif row["status"] == "expired":
                text = "⚠️ Hermes Desktop 명령 승인\n\n⌛ 이 명령 승인 요청은 만료되었거나 다른 표면에서 이미 종료되었습니다."
            elif row["status"] == "rejected":
                text = "⚠️ Hermes Desktop 명령 승인\n\n⛔ 이 명령 승인 응답은 안전하게 전달되지 않아 폐기되었습니다. Desktop에서 현재 요청을 확인해 주세요."
            markup = {"inline_keyboard": []}
        response = notifier.telegram(token, "editMessageText", {
            "chat_id": chat, "message_id": row["message_id"], "text": text,
            "reply_markup": markup, "link_preview_options": {"is_disabled": True}})
        verified = bool(response.get("ok") is True or
                        (response.get("error_code") == 400 and "message is not modified" in
                         str(response.get("description", "")).lower()))
        if verified:
            c = connect(state)
            try:
                c.execute("UPDATE approvals SET dirty=0 WHERE id=? AND status=? AND dirty=1",
                          (row["id"], row["status"]))
            finally:
                c.close()


def approval_notice(req, notifier, *, root=ROOT, state=STATE, api=None):
    """Notification-only message for a supported but unsafe-to-render approval."""
    request = req.get("request")
    pending = req.get("pending")
    descriptor = req.get("descriptor")
    if not supported_pair(request, pending, (request or {}).get("params", {}).get("session_id")):
        return {"ok": False, "reason": "not_supported"}
    if (not _validate_descriptor(descriptor) or descriptor.get("request_id") != request.get("id") or
            descriptor.get("queue_id") != pending.get("request_id") or
            descriptor.get("request_fingerprint") != _digest(request) or
            descriptor.get("pending_fingerprint") != _digest(pending)):
        return {"ok": False, "reason": "invalid_descriptor"}
    kind = req.get("reason")
    safe_reasons = {"review_text_missing", "invalid_review_text", "redaction_not_stable", "redaction_omission", "redaction_collision",
                    "review_unicode_ambiguous", "review_control_character", "review_card_too_large",
                    "review_unavailable", "approval_result_too_large", "timeout_unavailable", "receiver_unavailable", "owner_unavailable"}
    if kind not in safe_reasons:
        kind = "review_unavailable"
    profile = descriptor["targetProfile"]
    # Intentionally omit the command from this fallback; the Telegram card is
    # notification-only and clearly sends the user to Desktop for full review.
    text = (f"⚠️ Hermes Desktop — 명령 승인 알림\n프로필: {profile}\n\n"
            "텔레그램에서 이 명령을 안전하고 완전하게 표시할 수 없어 승인 버튼을 제공하지 않았습니다.\n"
            "명령 전체와 현재 권한 상태를 Desktop에서 확인해 주세요.")
    key = _digest([str(Path(root).resolve()), profile, descriptor["session_key"],
                   descriptor["request_id"], descriptor["queue_id"], "approval-notice-v1"])
    # Reuse the existing same-text at-most-once sender; never attach approval controls.
    token, target, topic = notifier.credentials(root)
    with notifier.state_db(state) as c:
        c.execute("BEGIN IMMEDIATE")
        existing = c.execute("SELECT status,message_id,verified FROM deliveries WHERE dedupe_key=?", (key,)).fetchone()
        if existing:
            return {"ok": existing[0] == "sent", "duplicate": True,
                    "status": existing[0], "message_id": existing[1], "verified": bool(existing[2])}
        c.execute("INSERT INTO deliveries(dedupe_key,profile,session_key,status,created) VALUES(?,?,?,?,?)",
                  (key, profile, descriptor["session_key"], "sending", time.time()))
    payload = {"chat_id": target, "text": text, "disable_notification": False,
               "protect_content": False, "link_preview_options": {"is_disabled": True}}
    if topic is not None:
        payload["message_thread_id"] = topic
    response = (api or notifier.telegram)(token, "sendMessage", payload)
    message = response.get("result", {})
    sent = bool(response.get("ok") and message.get("message_id") and message.get("chat", {}).get("id") == target and
                message.get("text") == text)
    mid = message.get("message_id") if sent else None
    verified = False
    if sent:
        check = (api or notifier.telegram)(token, "editMessageText", {"chat_id": target, "message_id": mid,
                 "text": text, "link_preview_options": {"is_disabled": True}})
        verified = bool(check.get("ok") and check.get("result", {}).get("message_id") == mid) or (
            check.get("error_code") == 400 and "message is not modified" in str(check.get("description", "")).lower())
    status = "sent" if sent else ("unknown" if response.get("ambiguous") else "failed")
    with notifier.state_db(state) as c:
        c.execute("UPDATE deliveries SET status=?,message_id=?,verified=? WHERE dedupe_key=?",
                  (status, mid, int(verified), key))
    return {"ok": sent, "status": status, "verified": verified, "notification_only": True}


def helper(req, notifier, *, root=ROOT, state=STATE, result_budget=None):
    global _NOTIFIER
    _NOTIFIER = notifier
    action = req.get("action") if isinstance(req, dict) else None
    if not isinstance(req, dict) or req.get("capability") != CAPABILITY:
        return {"ok": False, "reason": "capability_mismatch"}
    if action == "approval_notice":
        return approval_notice(req, notifier, root=root, state=state)
    if action not in {"approval_owner_heartbeat", "approval_register", "approval_poll", "approval_settle",
                       "approval_claim_valid"}:
        return {"ok": False, "reason": "unknown_approval_action"}
    if __package__:
        gateway = importlib.import_module('.telegram_integration', __package__)
    else:
        gateway = importlib.import_module('telegram_integration')
    allowed, reason = gateway.feature_status('approvals_enabled', root=root)
    if not allowed:
        if action == "approval_poll":
            return {"ok": False, "feature_ready": False, "reason": reason, "items": []}
        if action == "approval_claim_valid":
            return {"ok": True, "valid": False, "id": req.get("id"), "claim": req.get("claim"),
                    "authority": req.get("authority"), "deadline": req.get("deadline"), "reason": reason}
        return {"ok": False, "feature_ready": False, "reason": reason}
    try:
        if action == "approval_claim_valid":
            return claim_valid(req.get("owner"), req.get("id"), req.get("claim"), req.get("descriptor"),
                               req.get("authority"), req.get("deadline"), root=root, state=state)
        if action == "approval_owner_heartbeat":
            return owner_heartbeat(req.get("owner"), state=state)
        if action == "approval_register":
            result = register(req, notifier, root=root, state=state, result_budget=result_budget)
            if result.get("status") in ("open", "queued", "applying", *TERMINAL):
                flush(notifier, state=state)
            return result
        if action == "approval_poll":
            result = poll(req.get("owner"), req.get("observations"), unverified=req.get("unverified"),
                          absent=req.get("absent"), state=state, result_budget=result_budget)
            flush(notifier, state=state)
            return result
        result = settle(req.get("id"), req.get("claim"), req.get("status"), state=state)
        if result.get("ok"):
            flush(notifier, state=state)
        return result
    except Exception:
        # No command, session identity, token, or exception text in diagnostics.
        return {"ok": False, "reason": "approval_store_unavailable"}


def wire(application, notifier, bot, chat, topic, boot, *, approvals_enabled=lambda: False, state=STATE):
    """Attach approval callbacks to the existing PTB Application, never poll it."""
    from telegram.ext import CallbackQueryHandler, MessageHandler, filters, ApplicationHandlerStop

    try:
        token, configured_chat, configured_topic = notifier.credentials()
        if (application.bot.token != token or configured_chat != chat or configured_topic != topic or
                bot != int(token.split(":", 1)[0]) or chat <= 0):
            return False
    except Exception:
        return False

    def identity(message, user, mid, author, *, event, edited=False, inline=False, forwarded=False):
        msg_chat = getattr(message, "chat", None)
        msg_user = getattr(user, "id", None)
        author_id = getattr(author, "id", None)
        return {
            "bot": bot, "boot": boot, "chat": getattr(msg_chat, "id", None), "user": msg_user,
            "topic": getattr(message, "message_thread_id", None), "mid": mid,
            "private": getattr(msg_chat, "type", None) == "private",
            "human": getattr(user, "is_bot", True) is False,
            "bot_author": author_id == bot and getattr(author, "is_bot", False) is True,
            "forwarded": bool(forwarded or getattr(message, "forward_origin", None) or
                              getattr(message, "forward_from", None)),
            "edited": bool(edited or getattr(message, "edit_date", None)), "inline": bool(inline),
            "inaccessible": msg_chat is None or getattr(message, "text", None) is None,
            "message_text": getattr(message, "text", None), "event": event,
        }

    async def callback(update, context):
        query = update.callback_query
        try:
            msg = getattr(query, "message", None)
            data = getattr(query, "data", None)
            match = re.fullmatch(r"hda:([A-Za-z0-9_-]{16,32}):([od])", data or "")
            if (not approvals_enabled() or msg is None or getattr(query, "inline_message_id", None) is not None or
                    not isinstance(data, str) or len(data.encode("utf-8")) > 64 or match is None):
                result = {"ok": False, "reason": "invalid_callback"}
            else:
                ident = identity(msg, query.from_user, getattr(msg, "message_id", None),
                                 getattr(msg, "from_user", None), event="callback:" + str(query.id),
                                 inline=False)
                choice = "once" if match.group(2) == "o" else "deny"
                result = await asyncio.to_thread(choose, match.group(1), choice, ident, state=state)
                if result.get("ok"):
                    await asyncio.to_thread(flush, notifier, state=state)
            try:
                await query.answer("응답을 받았습니다." if result.get("ok") else
                                   "만료되었거나 현재 요청과 일치하지 않습니다. Desktop에서 확인해 주세요.")
            except Exception:
                pass
        except Exception:
            try:
                await query.answer("처리하지 못했습니다. Desktop에서 현재 승인을 확인해 주세요.", show_alert=True)
            except Exception:
                pass
        raise ApplicationHandlerStop

    class ExactApprovalReply(filters.MessageFilter):
        def filter(self, message):
            reply = getattr(message, "reply_to_message", None)
            if reply is None:
                return False
            try:
                if lookup_message(bot, getattr(getattr(message, "chat", None), "id", None),
                                  getattr(reply, "message_id", None), state=state):
                    return True
            except Exception:
                pass
            # Even during a store outage, recognize only our exact private-home
            # approval-card header so free text cannot become a fresh agent prompt.
            author = getattr(reply, "from_user", None)
            msg_chat = getattr(message, "chat", None)
            return bool(getattr(author, "id", None) == bot and getattr(author, "is_bot", False) is True and
                        getattr(msg_chat, "id", None) == chat and getattr(msg_chat, "type", None) == "private" and
                        isinstance(getattr(reply, "text", None), str) and
                        reply.text.startswith("⚠️ Hermes Desktop 명령 승인"))
    async def consume_text_reply(update, context):
        msg = getattr(update, "effective_message", None)
        try:
            if msg is not None:
                # Free text is never converted to once/deny or forwarded to the
                # agent. This handler only consumes the known card reply.
                await msg.reply_text("명령 승인은 버튼으로만 처리됩니다. Desktop에서 현재 승인 상태를 확인해 주세요.")
        except Exception:
            pass
        raise ApplicationHandlerStop

    # Install before clarify handlers so even rejected/expired approval-card
    # replies cannot enter the general clarify or agent-prompt path.
    application.add_handler(CallbackQueryHandler(callback, pattern=r"^hda:"), group=0)
    application.add_handler(MessageHandler(ExactApprovalReply(), consume_text_reply), group=0)
    return True


__all__ = ["VERSION", "CAPABILITY", "ALLOWED_CHOICES", "supported_pair", "validate_snapshot",
           "bind_receiver", "receiver_info", "owner_heartbeat", "register", "publish", "lookup_message",
           "choose", "poll", "settle", "flush", "helper", "wire"]
