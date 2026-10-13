"""Completion-reply → same-live-session bridge, independent of clarify storage.

The SQLite claim is at-most-once delivery-attempt, not exactly-once execution.
No Telegram polling, second Application, or message send lives in this module.
"""
from __future__ import annotations
import json
import math
import secrets
import sqlite3
import time
from pathlib import Path


def _active_hermes_home():
    from hermes_constants import get_hermes_home

    try:
        return Path(get_hermes_home()).expanduser().resolve(strict=True)
    except Exception as exc:
        raise RuntimeError("hermes_home_unavailable") from exc


ROOT = _active_hermes_home()
STATE = ROOT / "plugin-data" / "session-complete-telegram"
VERSION = 1
BINDING_TTL = 24 * 60 * 60
LEASE = 45
MAX_TEXT = 2000
_INITIAL_REGISTRATION_EPOCH = "initial"
_EXPECTED_EPOCH_UNSET = object()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(state=STATE):
    directory = Path(state) / "followup-inbox"
    directory.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(directory / "followups.sqlite3", timeout=8,
                        isolation_level=None, factory=ClosingConnection)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA secure_delete=ON")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS receiver (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1), bot INTEGER NOT NULL,
      owner_user INTEGER NOT NULL, chat INTEGER NOT NULL, topic INTEGER,
      boot TEXT NOT NULL, seen REAL NOT NULL, private_verified INTEGER NOT NULL,
      feature INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS owners(owner TEXT PRIMARY KEY, seen REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS bindings (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, descriptor TEXT NOT NULL,
      bot INTEGER NOT NULL, owner_user INTEGER NOT NULL, chat INTEGER NOT NULL,
      topic INTEGER, outbound_mid INTEGER NOT NULL, created REAL NOT NULL,
      expires REAL NOT NULL, owner_seen REAL NOT NULL, status TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS outbound (
      bot INTEGER NOT NULL, chat INTEGER NOT NULL, mid INTEGER NOT NULL,
      binding_id TEXT, kind TEXT NOT NULL, created REAL NOT NULL,
      PRIMARY KEY(bot,chat,mid));
    CREATE TABLE IF NOT EXISTS receipts (
      bot INTEGER NOT NULL, chat INTEGER NOT NULL, message_id INTEGER NOT NULL,
      update_id INTEGER, binding_id TEXT, status TEXT NOT NULL, claim TEXT,
      text TEXT, created REAL NOT NULL, updated REAL NOT NULL,
      PRIMARY KEY(bot,chat,message_id), UNIQUE(bot,update_id));
    CREATE INDEX IF NOT EXISTS receipt_status ON receipts(status,created);
    CREATE TABLE IF NOT EXISTS feedback(bot INTEGER,chat INTEGER,mid INTEGER,PRIMARY KEY(bot,chat,mid));
    """)
    return c


def _valid_registration_epoch(value):
    return type(value) is str and len(value) == 32 and all(char in "0123456789abcdef" for char in value)


def _registration_epoch(c):
    """Read the reserved revocation marker; malformed metadata is never authority."""
    rows = c.execute("""SELECT bot,chat,mid,binding_id,kind,created FROM outbound
      WHERE bot<=0 OR chat<=0 OR mid<=0 OR kind='revocation'
         OR typeof(bot)!='integer' OR typeof(chat)!='integer' OR typeof(mid)!='integer'""").fetchall()
    if not rows:
        return _INITIAL_REGISTRATION_EPOCH
    if len(rows) != 1:
        return None
    row = rows[0]
    created = row["created"]
    if (type(row["bot"]) is not int or row["bot"] != 0 or
            type(row["chat"]) is not int or row["chat"] != 0 or
            type(row["mid"]) is not int or row["mid"] != 0 or
            not _valid_registration_epoch(row["binding_id"]) or row["kind"] != "revocation" or
            type(created) not in (int, float) or not math.isfinite(created)):
        return None
    return row["binding_id"]


def capture_registration_epoch(*, state):
    """Read the current registration generation without creating inbox state.

    ``initial`` is the stable legacy generation when no revocation marker exists.
    ``None`` means an existing store or marker was malformed/unreadable and must
    be treated as fail-closed by ``register_completion``.
    """
    path = Path(state) / "followup-inbox" / "followups.sqlite3"
    if not path.is_file():
        return _INITIAL_REGISTRATION_EPOCH
    try:
        c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                            timeout=2, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA query_only=ON")
        try:
            return _registration_epoch(c)
        finally:
            c.close()
    except Exception:
        return None


def _expire(c, now):
    c.execute("UPDATE receipts SET status='unknown',text=NULL,claim=NULL,updated=? WHERE status IN ('applying','submitting') AND updated<?", (now, now-LEASE))
    c.execute("""UPDATE receipts SET status='expired',text=NULL,updated=? WHERE status='pending' AND binding_id IN
      (SELECT id FROM bindings WHERE status!='active' OR expires<=? OR owner_seen<?)""", (now, now, now-LEASE))
    # The original owner may renew before TTL; never revive terminal bindings.
    c.execute("UPDATE bindings SET status='expired' WHERE status='active' AND expires<=?", (now,))


def retire_unexecuted(*, state=STATE, now=None):
    """Expire bindings, reject queued text, and preserve attempted claims as unknown."""
    path = Path(state) / 'followup-inbox' / 'followups.sqlite3'
    if not path.is_file():
        return 0
    now = time.time() if now is None else now
    c = connect(state)
    try:
        c.execute('BEGIN IMMEDIATE')
        bindings = c.execute("UPDATE bindings SET status='expired' WHERE status='active'").rowcount
        rejected = c.execute("UPDATE receipts SET status='rejected',text=NULL,claim=NULL,updated=? WHERE status='pending'", (now,)).rowcount
        c.execute("UPDATE receipts SET status='unknown',text=NULL,claim=NULL,updated=? WHERE status IN ('applying','submitting')", (now,))
        # Reserved metadata uses impossible Telegram coordinates (0,0,0). It
        # shares the existing outbound primary key and never creates a binding.
        epoch = secrets.token_hex(16)
        c.execute("INSERT OR REPLACE INTO outbound VALUES(0,0,0,?,'revocation',?)", (epoch, now))
        c.commit()
        return max(0, bindings) + max(0, rejected)
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()



def bind_receiver(bot, chat, topic, boot, *, private_verified=False, state=STATE, now=None):
    now = time.time() if now is None else now
    if type(bot) is not int or bot <= 0 or type(chat) is not int or chat <= 0 or not isinstance(boot, str) or not boot:
        return False
    c = connect(state)
    try:
        c.execute("BEGIN IMMEDIATE")
        old = c.execute("SELECT boot FROM receiver WHERE singleton=1").fetchone()
        if old and old["boot"] != boot:
            c.execute("UPDATE bindings SET status='expired' WHERE status='active'")
            c.execute("UPDATE receipts SET status='expired',text=NULL,claim=NULL,updated=? WHERE status='pending'", (now,))
        c.execute("""INSERT OR REPLACE INTO receiver VALUES(1,?,?,?,?,?,?,?,?)""",
                  (bot, chat, chat, topic, boot, now, int(private_verified), VERSION if private_verified else 0))
        c.commit()
        return bool(private_verified)
    finally:
        c.close()


def receiver_info(*, state=STATE, now=None):
    now = time.time() if now is None else now
    c = connect(state)
    try:
        r = c.execute("SELECT * FROM receiver WHERE singleton=1").fetchone()
        if not r or now-r["seen"] < 0 or now-r["seen"] > LEASE or not r["private_verified"] or r["feature"] != VERSION:
            return None
        return dict(r)
    finally:
        c.close()


def heartbeat_owner(owner, *, state=STATE, now=None):
    now = time.time() if now is None else now
    if not isinstance(owner, str) or not owner or len(owner) > 100:
        return {"ok": False, "reason": "invalid_owner"}
    c = connect(state)
    try:
        c.execute("BEGIN IMMEDIATE")
        _expire(c, now)
        live = c.execute("SELECT * FROM receiver WHERE singleton=1").fetchone()
        ready = bool(live and live["private_verified"] and live["feature"] == VERSION and 0 <= now-live["seen"] <= LEASE)
        if ready:
            c.execute("INSERT INTO owners VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET seen=excluded.seen", (owner, now))
            c.execute("UPDATE bindings SET owner_seen=? WHERE owner=? AND status='active' AND expires>?", (now, owner, now))
        c.commit()
        return {"ok": True, "feature_ready": ready}
    finally:
        c.close()


def _receiver_mapping_matches(live, bot, chat, topic):
    return bool(
        live and type(live["bot"]) is int and live["bot"] > 0 and
        type(live["chat"]) is int and live["chat"] > 0 and
        type(live["owner_user"]) is int and live["owner_user"] > 0 and
        live["bot"] == bot and live["chat"] == chat and live["owner_user"] == chat and
        live["topic"] == topic and live["private_verified"] == 1 and live["feature"] == VERSION
    )


def _receiver_is_live(live, now):
    return bool(live and 0 <= now-live["seen"] <= LEASE)


def _record_reject_only(c, bot, chat, message_id, now):
    c.execute("INSERT INTO outbound VALUES(?,?,?,NULL,'legacy',?)", (bot, chat, message_id, now))


def register_completion(descriptor, *, bot, chat, topic, message_id, state=STATE, now=None,
                        expected_epoch=_EXPECTED_EPOCH_UNSET, policy=None):
    """Bind a sent card only within the captured revocation generation and live policy.

    The optional keyword arguments preserve pre-revocation unit callers. Omitting
    ``expected_epoch`` means the legacy ``initial`` generation only; after any
    retirement the sent card is mapped reject-only instead of gaining authority.
    Runtime producers must supply a captured epoch and current policy callback.
    """
    now = time.time() if now is None else now
    required = ("owner", "connectionId", "profile", "targetProfile", "sid", "session_key", "epoch", "started_at", "final_row_id")
    if not isinstance(descriptor, dict) or any(k not in descriptor for k in required):
        return {"ok": False, "reason": "invalid_descriptor"}
    if any(not isinstance(descriptor[k], str) or not descriptor[k] for k in required[:-1]):
        return {"ok": False, "reason": "invalid_descriptor"}
    if type(descriptor["final_row_id"]) is not int or not descriptor["owner"] or descriptor["targetProfile"] != descriptor["profile"]:
        return {"ok": False, "reason": "identity_mismatch"}
    if (type(bot) is not int or bot <= 0 or type(chat) is not int or chat <= 0 or
            type(message_id) is not int or message_id <= 0):
        return {"ok": False, "reason": "invalid_send_mapping"}
    c = connect(state)
    try:
        c.execute("BEGIN IMMEDIATE")
        live = c.execute("SELECT * FROM receiver WHERE singleton=1").fetchone()
        # Only a positively validated receiver mapping may retain a sent card.
        # Freshness/policy failures below keep it reject-only, never actionable.
        if not _receiver_mapping_matches(live, bot, chat, topic):
            c.rollback()
            return {"ok": False, "reason": "feature_unavailable"}

        policy_ok = True
        if policy is not None:
            try:
                policy_ok = callable(policy) and policy() is True
            except Exception:
                policy_ok = False
        try:
            current_epoch = _registration_epoch(c)
        except Exception:
            current_epoch = None
        expected = _INITIAL_REGISTRATION_EPOCH if expected_epoch is _EXPECTED_EPOCH_UNSET else expected_epoch
        expected_valid = (type(expected) is str and
                          (expected == _INITIAL_REGISTRATION_EPOCH or _valid_registration_epoch(expected)))
        if current_epoch is None:
            denied_reason = "revocation_state_invalid"
        elif not expected_valid:
            denied_reason = "invalid_expected_epoch"
        elif current_epoch != expected:
            denied_reason = "registration_revoked"
        elif not policy_ok:
            denied_reason = "feature_disabled"
        elif not _receiver_is_live(live, now):
            denied_reason = "feature_unavailable"
        else:
            owner = c.execute("SELECT seen FROM owners WHERE owner=?", (descriptor["owner"],)).fetchone()
            if not owner or now-owner["seen"] < 0 or now-owner["seen"] > LEASE:
                denied_reason = "owner_unavailable"
            else:
                denied_reason = None

        if denied_reason is not None:
            try:
                _record_reject_only(c, bot, chat, message_id, now)
            except sqlite3.IntegrityError:
                c.rollback()
                return {"ok": False, "reason": "duplicate_outbound"}
            c.commit()
            return {"ok": False, "reason": denied_reason}

        binding_id = secrets.token_hex(16)
        bound_descriptor = {**descriptor, "receiver_boot": live["boot"], "bot": bot,
                            "owner_user": live["owner_user"], "chat": chat, "topic": topic,
                            "notification_message_id": message_id}
        c.execute("INSERT INTO bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,'active')",
                  (binding_id, descriptor["owner"], canonical(bound_descriptor), bot, live["owner_user"], chat, topic,
                   message_id, now, now+BINDING_TTL, owner["seen"]))
        c.execute("INSERT INTO outbound VALUES(?,?,?,?,?,?)", (bot, chat, message_id, binding_id, "completion", now))
        c.commit()
        return {"ok": True, "binding_id": binding_id}
    except sqlite3.IntegrityError:
        c.rollback()
        return {"ok": False, "reason": "duplicate_outbound"}
    finally:
        c.close()


def lookup_outbound(bot, chat, mid, *, state=STATE):
    c = connect(state)
    try:
        r = c.execute("SELECT * FROM outbound WHERE bot=? AND chat=? AND mid=?", (bot, chat, mid)).fetchone()
        if not r:
            return None
        # The reserved (0,0,0) epoch marker and every malformed/non-positive
        # destination are metadata or invalid rows, never Telegram cards.
        if (r["kind"] == "revocation" or type(r["bot"]) is not int or r["bot"] <= 0 or
                type(r["chat"]) is not int or r["chat"] <= 0 or
                type(r["mid"]) is not int or r["mid"] <= 0):
            return None
        binding = c.execute("SELECT * FROM bindings WHERE id=?", (r["binding_id"],)).fetchone() if r["binding_id"] else None
        return {"kind": r["kind"], "binding": dict(binding) if binding else None}
    finally:
        c.close()


def _reject(c, bot, chat, message_id, update_id, binding_id, now):
    c.execute("INSERT OR IGNORE INTO receipts(bot,chat,message_id,update_id,binding_id,status,text,created,updated) VALUES(?,?,?,?,?,'rejected',NULL,?,?)",
              (bot, chat, message_id, update_id, binding_id, now, now))


def consume_legacy(bot, chat, message_id, update_id=None, *, state=STATE, now=None):
    """Permanent consume-only marker for legacy/ambiguous completion replies."""
    now = time.time() if now is None else now
    c = connect(state)
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT OR IGNORE INTO outbound VALUES(?,?,?,NULL,'legacy',?)", (bot, chat, message_id, now))
        _reject(c, bot, chat, message_id, update_id, None, now)
        c.commit()
        return {"ok": False, "reason": "legacy_reject_only"}
    finally:
        c.close()


def ingest(bot, chat, message_id, update_id, binding_id, identity, *, state=STATE, now=None):
    """Recognized inbound reply: terminally consume every rejected original or edit."""
    now = time.time() if now is None else now
    text = identity.get("text") if isinstance(identity, dict) else None
    c = connect(state)
    try:
        c.execute("BEGIN IMMEDIATE")
        _expire(c, now)
        out = c.execute("SELECT binding_id,kind FROM outbound WHERE bot=? AND chat=? AND mid=?", (bot, chat, message_id)).fetchone()
        if not out:
            c.rollback()
            return {"ok": False, "reason": "not_recognized"}
        inbound_id = identity.get("inbound_id")
        if type(inbound_id) is not int or inbound_id <= 0:
            c.commit()
            return {"ok": False, "reason": "invalid_message_id"}
        if c.execute("SELECT 1 FROM receipts WHERE bot=? AND chat=? AND message_id=?", (bot, chat, inbound_id)).fetchone():
            c.commit()
            return {"ok": False, "duplicate": True, "reason": "duplicate_message"}
        inserted = c.execute("INSERT OR IGNORE INTO receipts(bot,chat,message_id,update_id,binding_id,status,text,created,updated) VALUES(?,?,?,?,?,'rejected',NULL,?,?)",
                  (bot, chat, inbound_id, update_id, out["binding_id"], now, now))
        if inserted.rowcount != 1:
            c.commit()
            return {"ok": False, "duplicate": True, "reason": "duplicate_update"}
        if out["kind"] != "completion" or not out["binding_id"]:
            c.commit()
            return {"ok": False, "reason": "legacy_reject_only"}
        b = c.execute("SELECT * FROM bindings WHERE id=?", (out["binding_id"],)).fetchone()
        live = c.execute("SELECT * FROM receiver WHERE singleton=1").fetchone()
        owner = c.execute("SELECT seen FROM owners WHERE owner=?", (b["owner"],)).fetchone() if b else None
        valid_identity = bool(
            identity.get("bot_id") == bot and identity.get("author_bot_id") == bot and
            identity.get("user_id") == chat == identity.get("chat_id") and identity.get("private") is True and
            identity.get("sender_chat") is False and identity.get("topic") == (b["topic"] if b else None) and
            identity.get("reply_topic") == (b["topic"] if b else None) and
            identity.get("forwarded") is False and identity.get("edited") is False and
            identity.get("business") is False and identity.get("command") is False and
            isinstance(text, str) and text.strip() and len(text) <= MAX_TEXT and
            not text.lstrip().startswith("/") and b and live and live["boot"] == identity.get("boot") and
            live["bot"] == bot and live["chat"] == chat and live["owner_user"] == identity.get("user_id") and
            live["private_verified"] and live["feature"] == VERSION and 0 <= now-live["seen"] <= LEASE and
            b["status"] == "active" and b["expires"] > now and b["owner_seen"] > now-LEASE and owner and
            0 <= now-owner["seen"] <= LEASE and b["owner_user"] == identity.get("user_id") and
            b["chat"] == chat and b["bot"] == bot)
        if not valid_identity:
            c.commit()
            return {"ok": False, "reason": "rejected"}
        c.execute("UPDATE receipts SET status='pending',text=?,updated=? WHERE bot=? AND chat=? AND message_id=? AND status='rejected'",
                  (text, now, bot, chat, inbound_id))
        c.commit()
        return {"ok": True, "status": "pending"}
    finally:
        c.close()


def ready(owner, *, state=STATE, now=None):
    now = time.time() if now is None else now
    c = connect(state)
    try:
        c.execute("BEGIN IMMEDIATE")
        _expire(c, now)
        live = c.execute("SELECT * FROM receiver WHERE singleton=1").fetchone()
        own = c.execute("SELECT seen FROM owners WHERE owner=?", (owner,)).fetchone()
        if not live or not own or not live["private_verified"] or live["feature"] != VERSION or not 0 <= now-live["seen"] <= LEASE or not 0 <= now-own["seen"] <= LEASE:
            c.commit()
            return {"ok": False, "items": []}
        rows = c.execute("""SELECT r.bot,r.chat,r.message_id,r.binding_id,b.descriptor,b.expires,b.status
          FROM receipts r JOIN bindings b ON b.id=r.binding_id
          WHERE r.status='pending' AND b.owner=? AND b.status='active' AND b.expires>? AND b.owner_seen>?""",
                         (owner, now, now-LEASE)).fetchall()
        c.commit()
        return {"ok": True, "items": [dict(x) for x in rows]}
    finally:
        c.close()


def claim(owner, bot, chat, inbound_id, binding_id, expected_descriptor, *, state=STATE, now=None):
    now = time.time() if now is None else now
    c = connect(state)
    try:
        c.execute("BEGIN IMMEDIATE")
        _expire(c, now)
        row = c.execute("""SELECT r.*,b.owner,b.descriptor,b.expires,b.owner_seen,b.status AS binding_status
          FROM receipts r JOIN bindings b ON b.id=r.binding_id
          WHERE r.bot=? AND r.chat=? AND r.message_id=? AND r.binding_id=?""", (bot, chat, inbound_id, binding_id)).fetchone()
        live = c.execute("SELECT * FROM receiver WHERE singleton=1").fetchone()
        own = c.execute("SELECT seen FROM owners WHERE owner=?", (owner,)).fetchone()
        expected = json.loads(expected_descriptor) if isinstance(expected_descriptor, str) else expected_descriptor
        if (not row or row["status"] != "pending" or row["owner"] != owner or
                not isinstance(expected, dict) or canonical(json.loads(row["descriptor"])) != canonical(expected) or
                row["binding_status"] != "active" or row["expires"] <= now or
                row["owner_seen"] <= now-LEASE or not own or now-own["seen"] > LEASE or
                not live or not live["private_verified"] or not 0 <= now-live["seen"] <= LEASE or live["feature"] != VERSION or live["boot"] != expected.get("receiver_boot")):
            c.commit()
            return {"ok": False, "reason": "claim_rejected"}
        text = row["text"]
        claim_id = secrets.token_hex(16)
        c.execute("UPDATE receipts SET status='applying',claim=?,text=NULL,updated=? WHERE bot=? AND chat=? AND message_id=? AND status='pending'",
                  (claim_id, now, bot, chat, inbound_id))
        c.commit()
        return {"ok": True, "claim": claim_id, "text": text, "descriptor": json.loads(row["descriptor"]), "binding_id": binding_id}
    finally:
        c.close()


def begin_submit(owner, bot, chat, inbound_id, claim_id, *, state=STATE, now=None, policy=None):
    """Durably record unknown-before-write; crashes can never return this text to pending."""
    now = time.time() if now is None else now
    def policy_live():
        if policy is None:
            return True
        try:
            return policy() is True
        except Exception:
            return False
    policy_ok = policy_live()
    c = connect(state)
    try:
        c.execute("BEGIN IMMEDIATE")
        _expire(c, now)
        row = c.execute("SELECT r.status,r.updated,b.owner,b.status AS binding_status,b.expires,b.owner_seen,b.descriptor FROM receipts r JOIN bindings b ON b.id=r.binding_id WHERE r.bot=? AND r.chat=? AND r.message_id=? AND r.claim=?",
                        (bot, chat, inbound_id, claim_id)).fetchone()
        live = c.execute('SELECT * FROM receiver WHERE singleton=1').fetchone()
        if (not row or row["status"] != "applying" or row["owner"] != owner or
            row['binding_status'] != 'active' or row['expires'] <= now or not 0 <= now-row['owner_seen'] <= LEASE or
            not live or not live['private_verified'] or live['feature'] != VERSION or not 0 <= now-live['seen'] <= LEASE or
            live['boot'] != json.loads(row['descriptor'])['receiver_boot']):
            c.commit()
            return {"ok": False, "reason": "claim_lost"}
        if not policy_ok or not policy_live():
            c.execute("UPDATE receipts SET status='unknown',text=NULL,claim=NULL,updated=? WHERE bot=? AND chat=? AND message_id=? AND claim=? AND status='applying'",
                      (now, bot, chat, inbound_id, claim_id))
            c.commit()
            return {"ok": False, "reason": "feature_disabled"}
        c.execute("UPDATE receipts SET status='submitting',updated=? WHERE bot=? AND chat=? AND message_id=? AND claim=?",
                  (now, bot, chat, inbound_id, claim_id))
        c.commit()
        return {"ok": True, "status": "unknown", "deadline": min(row['expires'], row['updated']+LEASE)}
    finally:
        c.close()


def reject_pending(owner, bot, chat, inbound_id, binding_id, *, state=STATE, now=None):
    now = time.time() if now is None else now
    c = connect(state)
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT r.status,b.owner FROM receipts r JOIN bindings b ON b.id=r.binding_id WHERE r.bot=? AND r.chat=? AND r.message_id=? AND r.binding_id=?",
                        (bot, chat, inbound_id, binding_id)).fetchone()
        if not row or row["status"] != "pending" or row["owner"] != owner:
            c.commit()
            return {"ok": False, "reason": "receipt_not_pending"}
        c.execute("UPDATE receipts SET status='rejected',text=NULL,updated=? WHERE bot=? AND chat=? AND message_id=?",
                  (now, bot, chat, inbound_id))
        c.commit()
        return {"ok": True, "status": "rejected"}
    finally:
        c.close()


def settle(owner, bot, chat, inbound_id, claim_id, status, *, state=STATE, now=None):
    now = time.time() if now is None else now
    allowed = {"accepted_streaming", "accepted_queued", "rejected", "unknown"}
    if status not in allowed:
        return {"ok": False, "reason": "invalid_status"}
    c = connect(state)
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT r.status,b.owner FROM receipts r JOIN bindings b ON b.id=r.binding_id WHERE r.bot=? AND r.chat=? AND r.message_id=? AND r.claim=?",
                        (bot, chat, inbound_id, claim_id)).fetchone()
        if (not row or row["status"] not in ("applying", "submitting") or row["owner"] != owner or
                (status.startswith("accepted_") and row["status"] != "submitting")):
            c.commit()
            return {"ok": False, "reason": "claim_lost"}
        c.execute("UPDATE receipts SET status=?,text=NULL,updated=? WHERE bot=? AND chat=? AND message_id=? AND claim=?",
                  (status, now, bot, chat, inbound_id, claim_id))
        c.commit()
        return {"ok": True, "status": status}
    finally:
        c.close()


def consume_known(bot, chat, inbound_id, *, state=STATE, now=None):
    """Record a recognized but non-actionable inbound message, including edits/media/commands."""
    now = time.time() if now is None else now
    c = connect(state)
    try:
        c.execute("INSERT OR IGNORE INTO receipts(bot,chat,message_id,status,created,updated) VALUES(?,?,?,'rejected',?,?)",
                  (bot, chat, inbound_id, now, now))
        return {"ok": False, "reason": "rejected"}
    finally:
        c.close()


def notify_receipt(owner,bot,chat,mid,notifier,*,state=STATE,root=None):
    """One attempt at generic owner feedback; never echo the prompt or secrets."""
    if not notifier.action_enabled('completion_followups_enabled',root=root):return
    token,target,topic=notifier.credentials()
    if target != chat or int(token.split(':',1)[0]) != bot:return
    words={'accepted_streaming':'✅ 원래 Desktop 세션에 후속 메시지를 입력했습니다.',
           'accepted_queued':'⏳ 원래 Desktop 세션의 후속 메시지 대기열에 등록했습니다.',
           'rejected':'후속 입력을 거절했습니다. 세션 상태 또는 연결을 Desktop에서 확인해 주세요.',
           'unknown':'⚠️ 입력 결과를 확인하지 못했습니다. 중복 방지를 위해 재전송하지 않습니다. Desktop에서 확인해 주세요.'}
    with connect(state) as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT r.status,b.topic FROM receipts r JOIN bindings b ON b.id=r.binding_id WHERE r.bot=? AND r.chat=? AND r.message_id=? AND b.owner=?',(bot,chat,mid,owner)).fetchone()
        if not row or row['status'] not in words or row['topic'] != topic:return
        claim=c.execute('INSERT OR IGNORE INTO feedback VALUES(?,?,?)',(bot,chat,mid))
        if claim.rowcount != 1:return
        c.commit()
    payload={'chat_id':chat,'text':words[row['status']],'reply_parameters':{'message_id':mid,'allow_sending_without_reply':False}}
    if topic is not None:payload['message_thread_id']=topic
    if not notifier.action_enabled('completion_followups_enabled',root=root):return
    notifier.telegram(token,'sendMessage',payload)


def _add_owned_handler(application, handler, group, owned_handlers):
    if owned_handlers is not None:
        owned_handlers.append((handler, group))
    application.add_handler(handler, group=group)


def wire(application, notifier, bot, chat, topic, boot, *, state=STATE, enabled=lambda: True,
         _owned_handlers=None):
    """Install completion-only ingress on the existing PTB Application."""
    from telegram.ext import MessageHandler, filters, ApplicationHandlerStop

    def lookup(message):
        reply = getattr(message, "reply_to_message", None)
        if reply is None:
            return None
        mid = getattr(reply, "message_id", None)
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        try:
            found = lookup_outbound(bot, chat_id, mid, state=state) if type(mid) is int else None
        except Exception:
            # Store failure: replies to this bot fail closed, not into its agent.
            if getattr(getattr(reply, 'from_user', None), 'id', None) == bot:
                return {'kind':'unavailable', 'binding':None}
            found = None
        if found:
            return found
        # Old ledger rows lack authoritative destination/kind provenance. They
        # and conservative marker matches are recognized only to reject/consume.
        try:
            if notifier.legacy_message_id(mid) and chat_id == chat:
                return {"kind": "legacy", "binding": None}
        except Exception:
            pass
        old_text = getattr(reply, "text", None)
        if chat_id == chat and getattr(getattr(reply, "from_user", None), "id", None) == bot and isinstance(old_text, str) and old_text.startswith("✅ Hermes Desktop 세션 작업 종료"):
            return {"kind": "legacy", "binding": None}
        return None

    class RecognizedCompletion(filters.MessageFilter):
        def filter(self, message):
            return lookup(message) is not None

    async def handler(update, context):
        msg = getattr(update, "effective_message", None)
        try:
            if msg is not None:
                reply = getattr(msg, "reply_to_message", None)
                chat_id = getattr(getattr(msg, "chat", None), "id", None)
                inbound_id = getattr(msg, "message_id", None)
                found = lookup(msg)
                if found and type(inbound_id) is int and type(chat_id) is int:
                    if not enabled() or found["kind"] != "completion" or not found.get("binding"):
                        consume_legacy(bot, chat_id, inbound_id, getattr(update, "update_id", None), state=state)
                    else:
                        entities = getattr(msg, "entities", None) or []
                        command = any(getattr(e, "type", None) == "bot_command" for e in entities)
                        author = getattr(reply, "from_user", None)
                        user = getattr(msg, "from_user", None)
                        identity = {
                            "inbound_id": inbound_id, "bot_id": bot, "author_bot_id": getattr(author, "id", None),
                            "user_id": getattr(user, "id", None), "chat_id": chat_id,
                            "private": getattr(getattr(msg, "chat", None), "type", None) == "private",
                            "sender_chat": getattr(msg, "sender_chat", None) is not None,
                            "topic": getattr(msg, "message_thread_id", None),
                            "reply_topic": getattr(reply, "message_thread_id", None),
                            "forwarded": bool(getattr(msg, "forward_origin", None) or getattr(reply, "forward_origin", None)),
                            "edited": getattr(update, "edited_message", None) is not None,
                            "business": bool(getattr(msg, "business_connection_id", None)),
                            "command": command or (isinstance(getattr(msg, "text", None), str) and msg.text.lstrip().startswith("/")),
                            "text": getattr(msg, "text", None), "boot": boot,
                        }
                        if getattr(user, "is_bot", True):
                            identity["user_id"] = None
                        result=ingest(bot, chat_id, reply.message_id, getattr(update, "update_id", None), found["binding"]["id"], identity, state=state)
                        if user and user.id == chat == chat_id and not result.get('duplicate') and enabled() is True:
                            try:
                                await msg.reply_text('후속 메시지를 받았습니다. Desktop 연결을 확인 중입니다.' if result.get('ok') else '이 완료 알림으로 후속 입력을 할 수 없습니다. Desktop에서 확인해 주세요.')
                            except Exception:pass
        except Exception:
            # Known replies are always stopped even if persistence/validation fails.
            pass
        raise ApplicationHandlerStop

    _add_owned_handler(application, MessageHandler(RecognizedCompletion(), handler), 0, _owned_handlers)
    return True


__all__ = ["BINDING_TTL", "LEASE", "MAX_TEXT", "STATE", "bind_receiver", "canonical", "capture_registration_epoch", "claim", "connect", "consume_known", "consume_legacy", "heartbeat_owner", "ingest", "lookup_outbound", "ready", "receiver_info", "register_completion", "settle", "notify_receipt", "wire"]
