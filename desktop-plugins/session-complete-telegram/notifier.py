"""Local sender for the app-wide Session Completion Telegram Desktop plugin.

No credentials cross the renderer/RPC boundary. Only the configured default
profile's existing Telegram destination is used. No polling/getUpdates.

Two notification kinds ship: ``send`` (a settled completed request cycle) and
``attention`` (the agent is blocked on a human — clarify/approval/sudo/secret/
vault prompts — or a settled turn ended with a question). Renderer-relayed ask
payloads are formatted and redacted HERE, never trusted as display text.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import importlib
import json
import os
import re
import sqlite3
import stat
import sys
import time
from pathlib import Path

VERSION = "1.8.1"
APPROVAL_COMPAT_VERSION = "1.8.0"
APPROVAL_CAPABILITY = "desktop-command-approval-v6"
RECOVERY_CAPABILITY = "desktop-completion-recovery-v1"


def _action_status(name, root=None, ctx=None):
    """Fresh server-side policy read used immediately before every outbound side effect."""
    try:
        root = ROOT if root is None else root
        gateway = _sibling_module("telegram_integration")
        return gateway.feature_status(name, root=root, ctx=ctx)
    except Exception:
        return False, "native_plugin_config_invalid"


def action_enabled(name, root=None, ctx=None):
    return _action_status(name, root=root, ctx=ctx)[0]


def _completion_recovery_policy(state, *, root=None, ctx=None):
    """Return generation authority only after native settings have been verified."""
    root = ROOT if root is None else root
    try:
        allowed, reason = _action_status("completion_notifications_enabled", root=root, ctx=ctx)
        if not allowed and reason not in {"service_disabled", "completion_notifications_disabled"}:
            return {"ok": False, "reason": reason if reason in {
                "native_plugin_config_invalid", "native_plugin_owner_mismatch", "legacy_package_migration_required",
                "session_complete_plugin_disabled", "default_home_required"} else "native_plugin_config_invalid"}
        recovery = _sibling_module("completion_recovery")
        return recovery.policy(state, enabled=allowed)
    except Exception:
        return {"ok": False, "reason": "recovery_policy_unavailable"}


def _pre_dispatch_retry(reason):
    return {"ok": False, "phase": "pre_dispatch", "retryable": True, "reason": reason}


def _pre_dispatch_terminal(reason):
    return {"ok": False, "phase": "pre_dispatch", "retryable": False, "reason": reason}


def _policy_action_result(value):
    if value.get("ok") is not True and value.get("reason") == "recovery_policy_unavailable":
        return _pre_dispatch_retry("recovery_storage_unavailable")
    return value


def _storage_reason(exc, *, fallback="recovery_storage_unavailable"):
    recovery = _sibling_module("completion_recovery")
    if isinstance(exc, recovery.RecoveryError):
        code = str(exc)
        if code in {"credentials_unavailable"}:
            return "credentials_unavailable"
        if code in {"source_storage_unavailable", "recovery_database_unavailable", "recovery_schema_unavailable"}:
            return "source_storage_unavailable" if code == "source_storage_unavailable" else fallback
        if code in {"source_identity_unavailable"}:
            return "source_identity_unavailable"
    return fallback



def _active_hermes_home():
    from hermes_constants import get_hermes_home

    try:
        return Path(get_hermes_home()).expanduser().resolve(strict=True)
    except Exception as exc:
        raise RuntimeError("hermes_home_unavailable") from exc


ROOT = _active_hermes_home()
STATE = ROOT / "plugin-data" / "session-complete-telegram"
QUIET_SECONDS = 60
PREVIEW_CHARS = 1200
PREVIEW_MARKER = "\n\n… 중간 생략 …\n\n"
SRQ_RE = re.compile(r"srq-[0-9a-f]{4,32}")
# Server→client request kinds that block the agent on a HUMAN decision.
# GUI bridges (terminal.read / preview.act / window.read / tour) are excluded:
# those are agent↔renderer automation, not questions for the user.
ASK_LABELS = {
    "clarify": "❓ 질문/선택 요청",
    "approval": "🔐 명령 승인 요청",
    "sudo": "🔐 관리자 권한 요청",
    "secret": "🔑 비밀정보 입력 요청",
    "vault.unlock_prompt": "🔓 비밀번호 관리자 잠금 해제 요청",
    "vault.save_login": "💾 로그인 저장 확인 요청",
    "vault.code": "🔢 인증 코드 입력 요청",
}


def _sibling_module(name):
    if __package__:
        return importlib.import_module(f".{name}", __package__)
    return importlib.import_module(name)


def markdown_plain(text):
    """Render Markdown as readable plain text, preserving code literally."""
    from markdown_it import MarkdownIt
    parser = MarkdownIt('commonmark', {'html': False}).enable(['table', 'strikethrough'])

    def inline(tokens):
        parts, links = [], []
        for t in tokens or []:
            if t.type in ('text', 'code_inline'):
                parts.append(t.content)
            elif t.type in ('softbreak', 'hardbreak'):
                parts.append('\n')
            elif t.type == 'image':
                parts.append(inline(t.children) or t.content)
            elif t.type == 'link_open':
                links.append((t.attrGet('href') or '', len(parts)))
            elif t.type == 'link_close' and links:
                href, start = links.pop()
                if href and ''.join(parts[start:]) != href:
                    parts.append(' (' + href + ')')
        return ''.join(parts)

    out, lists = [], []
    for t in parser.parse(text):
        if t.type == 'inline':
            out.append(inline(t.children))
        elif t.type in ('fence', 'code_block'):
            out.append(t.content + '\n')
        elif t.type in ('bullet_list_open', 'ordered_list_open'):
            lists.append(int(t.attrGet('start') or 1) if t.type == 'ordered_list_open' else None)
        elif t.type in ('bullet_list_close', 'ordered_list_close'):
            lists.pop()
            out.append('\n')
        elif t.type == 'list_item_open':
            if lists and lists[-1] is not None:
                out.append(str(lists[-1]) + ') ')
                lists[-1] += 1
            else:
                out.append('• ')
        elif t.type == 'list_item_close':
            out.append('\n')
        elif t.type in ('paragraph_close', 'heading_close'):
            out.append('\n' if lists else '\n\n')
        elif t.type in ('th_close', 'td_close'):
            out.append(' | ')
        elif t.type == 'tr_close':
            out.append('\n')
        elif t.type == 'hr':
            out.append('\n')
    return ''.join(out).strip()


def final_preview(text, *, markdown=True):
    from agent.redact import redact_sensitive_text
    # Redact before parsing/cutting, and again after entity/escape decoding.
    text = redact_sensitive_text(text, force=True, redact_url_credentials=True)
    if markdown:
        text = markdown_plain(text)
        text = redact_sensitive_text(text, force=True, redact_url_credentials=True)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= PREVIEW_CHARS:
        return text
    head = 900
    tail = PREVIEW_CHARS - head - len(PREVIEW_MARKER)
    return text[:head] + PREVIEW_MARKER + text[-tail:]


def notification_text(checked):
    title = final_preview(str(checked["title"])).replace("\n", " ")[:100]
    return (f"✅ Hermes Desktop 세션 작업 종료\n프로필: {checked['profile']}\n세션: {title}"
            f"\n\n마지막 답변\n{checked['final_preview']}"
            "\n\n최종 응답 후 60초간 실행·대기 작업 없음 · 전체 내용은 Desktop에서 확인")


def ask_preview(method, params):
    """Format a pending human ask's own fields into bounded, redacted text."""
    def clean(value):
        return str(value if value is not None else "")[:2000]
    lines = []
    if method == "clarify":
        questions = params.get("questions")
        if not (isinstance(questions, list) and questions):
            questions = [{"question": params.get("question"), "choices": params.get("choices")}]
        for q in questions:
            if not isinstance(q, dict):
                continue
            question = clean(q.get("question")).strip()
            if question:
                lines.append(question)
            choices = q.get("choices") if isinstance(q.get("choices"), list) else []
            for i, choice in enumerate(choices):
                lines.append(f"  {i + 1}) {clean(choice)}")
    elif method == "approval":
        if params.get("description"):
            lines.append(clean(params.get("description")))
        if params.get("command"):
            lines.append("$ " + clean(params.get("command")))
    elif method == "sudo":
        if params.get("command"):
            lines.append("$ " + clean(params.get("command")))
    elif method == "secret":
        if params.get("prompt"):
            lines.append(clean(params.get("prompt")))
        if params.get("env_var"):
            lines.append("(" + clean(params.get("env_var")) + ")")
    elif method == "vault.unlock_prompt":
        tail = " (" + clean(params.get("backend")) + ")" if params.get("backend") else ""
        lines.append(clean(params.get("display_name")) + tail)
    elif method == "vault.save_login":
        tail = " (" + clean(params.get("origin")) + ")" if params.get("origin") else ""
        lines.append(clean(params.get("site")) + tail)
    elif method == "vault.code":
        lines.append(" — ".join(x for x in (clean(params.get("site")), clean(params.get("hint"))) if x))
    text = "\n".join(line for line in lines if line.strip())
    # Approval/sudo commands are literal shell syntax, never Markdown.
    return final_preview(text, markdown=method == 'clarify') if text.strip() else ""


def attention_text(checked):
    title = final_preview(str(checked["title"])).replace("\n", " ")[:100]
    body = f"\n{checked['ask_preview']}" if checked.get("ask_preview") else ""
    return (f"⏳ Hermes Desktop — 입력을 기다립니다\n프로필: {checked['profile']}\n세션: {title}"
            f"\n\n{checked['ask_label']}{body}"
            "\n\n응답하면 작업이 이어집니다 · 응답은 Desktop에서 입력")


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def db_read(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5, factory=ClosingConnection)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    return c


def _is_state_file(path):
    """Missing is a verified absence; other filesystem errors stay observable."""
    try:
        return stat.S_ISREG(Path(path).stat().st_mode)
    except FileNotFoundError:
        return False


def _profile_state_files(root):
    """Enumerate aliases without pathlib.glob's suppressed scandir/entry errors."""
    profiles = Path(root) / "profiles"
    try:
        metadata = profiles.lstat()
    except FileNotFoundError:
        return
    if (stat.S_ISLNK(metadata.st_mode) or
            getattr(metadata, "st_reparse_tag", None) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1)):
        raise ValueError("profile_home_retargeted")
    with os.scandir(profiles) as entries:
        for entry in entries:
            # Do not use DirEntry/Path convenience predicates that suppress I/O.
            if not stat.S_ISDIR(entry.stat(follow_symlinks=True).st_mode):
                continue
            path = Path(entry.path) / "state.db"
            if _is_state_file(path):
                yield path


def profile_home(profile, root=ROOT):
    root = Path(root).expanduser().resolve(strict=True)
    if profile == "default":
        state_db = root / "state.db"
        is_junction = getattr(state_db, "is_junction", lambda: False)()
        if state_db.is_symlink() or is_junction or (_is_state_file(state_db) and state_db.resolve(strict=True).parent != root):
            raise ValueError("profile_home_retargeted")
        return root
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", profile):
        raise ValueError("invalid_profile")
    profiles = root / "profiles"
    home = profiles / profile
    try:
        is_junction = getattr(home, "is_junction", lambda: False)()
        parent_junction = getattr(profiles, "is_junction", lambda: False)()
        resolved_profiles = profiles.resolve(strict=True)
        resolved = home.resolve(strict=True)
        expected = Path(os.path.abspath(os.fspath(home)))
    except FileNotFoundError as exc:
        raise ValueError("unknown_profile") from exc
    state_db = resolved / "state.db"
    db_junction = getattr(state_db, "is_junction", lambda: False)()
    if (not stat.S_ISDIR(home.stat().st_mode) or home.is_symlink() or is_junction or profiles.is_symlink() or
            parent_junction or db_junction or state_db.is_symlink() or
            os.path.normcase(os.fspath(resolved)) != os.path.normcase(os.fspath(expected)) or
            resolved.parent != resolved_profiles or
            (_is_state_file(state_db) and state_db.resolve(strict=True).parent != resolved)):
        raise ValueError("profile_home_retargeted")
    return resolved


def session_context(req, root=ROOT, anchor_row_id=None, require_anchor=False):
    """Shared session/origin gate: home lookup, durable row, source, Desktop origin.

    ``anchor_row_id`` disambiguates the pooled-backend default-alias case by an
    exact persisted row; without it the alias resolves by session id only and
    multiple matches fail closed.
    """
    try:
        home = profile_home(req["profile"], root)
    except OSError:
        return {"ok": False, "reason": "source_storage_unavailable"}
    except ValueError:
        return {"ok": False, "reason": "source_identity_unavailable"}
    sid = req.get("session_key")
    if not isinstance(sid, str) or not sid or len(sid) > 200:
        return {"ok": False, "reason": "invalid_session"}
    if require_anchor and (not isinstance(anchor_row_id, int) or isinstance(anchor_row_id, bool)):
        return {"ok": False, "reason": "unproven_final_row"}
    with db_read(home / "state.db") as c:
        row = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not row and req["profile"] == "default":
        # A pooled Desktop backend can label its launch-home route default
        # even when that home is a named profile. Resolve only this alias,
        # by exact persisted identity, never by recency.
        matches = []
        for path in _profile_state_files(root):
            name = path.parent.name
            candidate_home = profile_home(name, root)
            with db_read(candidate_home / "state.db") as other:
                if anchor_row_id is not None:
                    found = other.execute("SELECT 1 FROM messages WHERE session_id=? AND id=? AND active=1", (sid, anchor_row_id)).fetchone()
                else:
                    found = other.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()
            if found:
                matches.append(name)
        if len(matches) == 1:
            return session_context({**req, "profile": matches[0]}, root, anchor_row_id, require_anchor)
        if len(matches) > 1:
            return {"ok": False, "reason": "ambiguous_default_profile"}
    if not row or row["source"] not in ("desktop", "tui", "cli", "telegram", "discord", "slack"):
        return {"ok": False, "reason": "not_desktop_session"}
    # A Desktop-resumed human chat may retain its original source. A child
    # watch mirror cannot turn into a top-level notification.
    if req.get("desktop_observed") is not True:
        return {"ok": False, "reason": "unproven_desktop_origin"}
    return {"ok": True, "home": home, "row": dict(row), "profile": req["profile"]}


def resolve_recovery_source(identity, *, root=ROOT):
    """Resolve a candidate's persisted session home natively, never from caller paths."""
    if not isinstance(identity, dict):
        return {"ok": False, "reason": "source_identity_unavailable"}
    route_value = identity.get("route")
    if isinstance(route_value, dict):
        route = route_value
    else:
        route = {"mode": "local", "connectionId": identity.get("connection_id"),
                 "profile": identity.get("profile"), "targetProfile": identity.get("target_profile")}
    target = route.get("targetProfile") if isinstance(route, dict) else None
    session_key = identity.get("session_key")
    final_id = identity.get("final_row_id")
    if (not isinstance(target, str) or not isinstance(session_key, str) or not session_key or
            type(final_id) is not int or final_id < 1):
        return {"ok": False, "reason": "source_identity_unavailable"}
    try:
        context = session_context({"profile": target, "session_key": session_key,
                                   "desktop_observed": True}, root=root,
                                 anchor_row_id=final_id, require_anchor=True)
    except (sqlite3.Error, OSError):
        return {"ok": False, "reason": "source_storage_unavailable"}
    except Exception:
        return {"ok": False, "reason": "source_identity_unavailable"}
    if context.get("ok") is not True:
        return {"ok": False, "reason": "source_storage_unavailable" if context.get("reason") == "source_storage_unavailable" else "source_identity_unavailable"}
    try:
        recovery = _sibling_module("completion_recovery")
        binding = recovery._source_binding(context["home"])
    except OSError:
        return {"ok": False, "reason": "source_storage_unavailable"}
    except Exception as exc:
        return {"ok": False, "reason": "source_storage_unavailable" if str(exc) == "source_storage_unavailable" else "source_identity_unavailable"}
    return {"ok": True, "profile": context["profile"], "binding": binding}


def resolve_recovery_cancel_source(identity, *, root=ROOT):
    """Resolve the cancellation's persisted source by runtime/final row, failing closed on ambiguity."""
    if not isinstance(identity, dict):
        return {"ok": False, "reason": "source_identity_unavailable"}
    route_value = identity.get("route")
    if isinstance(route_value, dict):
        target = route_value.get("targetProfile")
    else:
        target = identity.get("target_profile")
    runtime_id = identity.get("runtime_id")
    final_id = identity.get("final_row_id")
    if (not isinstance(target, str) or not isinstance(runtime_id, str) or not runtime_id or
            type(final_id) is not int or final_id < 1):
        return {"ok": False, "reason": "source_identity_unavailable"}
    try:
        owner = Path(root).expanduser().resolve(strict=True)
        homes = []
        if target == "default":
            homes.append(("default", profile_home("default", owner)))
            for path in _profile_state_files(owner):
                homes.append((path.parent.name, profile_home(path.parent.name, owner)))
        else:
            homes.append((target, profile_home(target, owner)))
        matches = []
        for profile, home in homes:
            db_path = home / "state.db"
            if not _is_state_file(db_path):
                continue
            try:
                with db_read(db_path) as db:
                    session = db.execute("SELECT source FROM sessions WHERE id=?", (runtime_id,)).fetchone()
                    message = db.execute("SELECT 1 FROM messages WHERE session_id=? AND id=? AND active=1",
                                         (runtime_id, final_id)).fetchone()
            except (sqlite3.Error, OSError):
                return {"ok": False, "reason": "source_storage_unavailable"}
            if session and session["source"] in ("desktop", "tui", "cli", "telegram", "discord", "slack") and message:
                matches.append((profile, home))
        if len(matches) != 1:
            return {"ok": False, "reason": "source_identity_unavailable"}
        profile, home = matches[0]
        recovery = _sibling_module("completion_recovery")
        return {"ok": True, "profile": profile, "binding": recovery._source_binding(home)}
    except (sqlite3.Error, OSError):
        return {"ok": False, "reason": "source_storage_unavailable"}
    except Exception:
        return {"ok": False, "reason": "source_identity_unavailable"}


def todo_snapshot(value):
    """Find actual todo result snapshots, not the model's input arguments."""
    if isinstance(value, dict):
        if isinstance(value.get("todos"), list) and "revision" in value:
            return value["todos"]
        for key in ("result", "results", "content"):
            if key in value:
                found = todo_snapshot(value[key])
                if found is not None:
                    return found
    elif isinstance(value, list):
        for item in reversed(value):
            found = todo_snapshot(item)
            if found is not None:
                return found
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            return todo_snapshot(json.loads(value))
        except (ValueError, RecursionError):
            return None
    return None


def inspect_session(req, root=ROOT, now=None, *, include_preview=False):
    """Independent persisted-state gate immediately before sending; read-only."""
    now = time.time() if now is None else now
    ctx = session_context(req, root, req.get("final_row_id"), require_anchor=True)
    if not ctx["ok"]:
        return ctx
    home, row, sid = ctx["home"], ctx["row"], req["session_key"]
    final_id = req["final_row_id"]
    text = ""
    lineage = sid
    with db_read(home / "state.db") as c:
        if c.execute("SELECT 1 FROM session_turn_leases WHERE conversation_id=? AND expires_at>?", (sid, now)).fetchone():
            return {"ok": False, "reason": "active_turn_lease"}
        if c.execute("SELECT 1 FROM async_delegations WHERE (parent_session_id=? OR origin_session_id=?) AND (state NOT IN ('completed','failed','interrupted','cancelled') OR delivery_state NOT IN ('delivered','dropped')) LIMIT 1", (sid, sid)).fetchone():
            return {"ok": False, "reason": "pending_delegation_delivery"}
        tip = c.execute("SELECT id,role,content,tool_calls,timestamp FROM messages WHERE session_id=? AND active=1 ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
        if not tip or tip["id"] != final_id or tip["role"] != "assistant" or tip["tool_calls"] not in (None, "", "[]", "null"):
            return {"ok": False, "reason": "newer_or_unfinished_work"}
        if now - float(tip["timestamp"] or now) < QUIET_SECONDS:
            return {"ok": False, "reason": "quiet_period"}
        text = tip["content"] or ""
        if not text.strip():
            return {"ok": False, "reason": "empty_final"}
        # Pending approval/questions are not completion. Conservative: an optional
        # final question also suppresses rather than manufacturing success.
        if text.rstrip().endswith(("?", "？")):
            return {"ok": False, "reason": "awaiting_answer"}
        for tool in c.execute("SELECT content FROM messages WHERE session_id=? AND active=1 AND role='tool' AND content LIKE '%todos%' ORDER BY id DESC LIMIT 100", (sid,)):
            tasks = todo_snapshot(tool["content"])
            if tasks is not None:
                if any(not isinstance(t, dict) or t.get("status") not in ("completed", "cancelled") for t in tasks):
                    return {"ok": False, "reason": "unfinished_todos"}
                break
        # Follow compression ancestry only, never unrelated subagent parents.
        seen = set()
        current = row
        while current.get("parent_session_id") and lineage not in seen:
            seen.add(lineage)
            parent = c.execute("SELECT * FROM sessions WHERE id=?", (current["parent_session_id"],)).fetchone()
            if not parent or parent["end_reason"] not in ("compression", "compressed", "context_compression"):
                break
            lineage, current = parent["id"], dict(parent)
    result = {"ok": True, "lineage": lineage, "title": row["title"] or "제목 없는 세션", "profile": ctx["profile"], "final_row_id": final_id}
    # Used only inside the local sender; RPC inspect/receipts omit content.
    if include_preview:
        result["final_preview"] = final_preview(text)
    return result


def inspect_ask(req, root=ROOT, now=None):
    """Gate one renderer-relayed pending ask; the ask id is the dedup identity."""
    ctx = session_context(req, root)
    if not ctx["ok"]:
        return ctx
    ask_id = req.get("ask_id")
    method = req.get("ask_method")
    if not isinstance(ask_id, str) or not SRQ_RE.fullmatch(ask_id):
        return {"ok": False, "reason": "invalid_ask"}
    if method not in ASK_LABELS:
        return {"ok": False, "reason": "not_a_human_ask"}
    params = req.get("ask_params")
    if not isinstance(params, dict) or len(json.dumps(params, ensure_ascii=False)) > 8000:
        return {"ok": False, "reason": "invalid_ask_params"}
    return {"ok": True, "kind": "ask", "profile": ctx["profile"], "title": ctx["row"]["title"] or "제목 없는 세션",
            "session_key": req["session_key"], "ask_id": ask_id,
            "ask_label": ASK_LABELS[method], "ask_preview": ask_preview(method, params)}


def inspect_question(req, root=ROOT, now=None):
    """Gate one settled turn that ended in a question: input is still needed."""
    now = time.time() if now is None else now
    ctx = session_context(req, root, req.get("final_row_id"), require_anchor=True)
    if not ctx["ok"]:
        return ctx
    home, sid, final_id = ctx["home"], req["session_key"], req["final_row_id"]
    with db_read(home / "state.db") as c:
        if c.execute("SELECT 1 FROM session_turn_leases WHERE conversation_id=? AND expires_at>?", (sid, now)).fetchone():
            return {"ok": False, "reason": "active_turn_lease"}
        tip = c.execute("SELECT id,role,content,tool_calls,timestamp FROM messages WHERE session_id=? AND active=1 ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    if not tip or tip["id"] != final_id or tip["role"] != "assistant" or tip["tool_calls"] not in (None, "", "[]", "null"):
        return {"ok": False, "reason": "newer_or_unfinished_work"}
    text = (tip["content"] or "").strip()
    if not text.endswith(("?", "？")):
        return {"ok": False, "reason": "not_a_question"}
    if now - float(tip["timestamp"] or now) < QUIET_SECONDS:
        return {"ok": False, "reason": "quiet_period"}
    return {"ok": True, "kind": "question", "profile": ctx["profile"], "title": ctx["row"]["title"] or "제목 없는 세션",
            "session_key": sid, "final_row_id": final_id,
            "ask_label": "❓ 질문이 있습니다", "ask_preview": final_preview(text)}


def state_db(state=STATE):
    state.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(state / "notifications.sqlite3", timeout=10, factory=ClosingConnection)
    c.execute("CREATE TABLE IF NOT EXISTS deliveries (dedupe_key TEXT PRIMARY KEY, profile TEXT, session_key TEXT, status TEXT, created REAL, message_id INTEGER, verified INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS diagnostics (name TEXT PRIMARY KEY, data TEXT, updated REAL)")
    c.commit()
    return c


def diagnostic(name, data, state=STATE):
    with state_db(state) as c:
        c.execute("INSERT OR REPLACE INTO diagnostics VALUES (?,?,?)", (name, json.dumps(data, ensure_ascii=False), time.time()))


def credentials(root=ROOT):
    """Read only the active Hermes secret scope; ``root`` is caller authority/state context, not a credential file path."""
    from agent.secret_scope import get_secret

    token = get_secret("TELEGRAM_BOT_TOKEN") or ""
    target = get_secret("TELEGRAM_HOME_CHANNEL") or ""
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ValueError("telegram_token_missing")
    # Notifications are to the user's already configured home, never a supplied
    # chat id. Topic routing is optional and stays backend-only.
    match = re.fullmatch(r"(-?[0-9]+)(?::([0-9]+))?", target)
    if not match:
        raise ValueError("telegram_home_missing_or_unsupported")
    return token, int(match[1]), int(match[2]) if match[2] else None


def telegram(token, method, payload):
    import httpx
    # Exception strings can contain the token-bearing URL: never return/log them.
    try:
        with httpx.Client(timeout=8, follow_redirects=False, trust_env=False) as client:
            response = client.post(f"https://api.telegram.org/bot{token}/{method}", json=payload)
        value = response.json()
        if not isinstance(value, dict):
            return {"ok": False, "error_code": 0, "ambiguous": True}
        return value
    except Exception:
        return {"ok": False, "error_code": 0, "ambiguous": True}


def completion_telegram(token, method, payload):
    """One fresh direct request for completion recovery; never retry transport.

    Only httpx ConnectError/ConnectTimeout from the request phase prove that
    Telegram could not have received this attempt. Everything after connect,
    including malformed receipts and response/read/write failures, is unknown.
    """
    import httpx
    try:
        transport = httpx.HTTPTransport(retries=0)
        with httpx.Client(timeout=8, follow_redirects=False, trust_env=False,
                          transport=transport) as client:
            try:
                response = client.post(f"https://api.telegram.org/bot{token}/{method}", json=payload)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                return {"kind": "proven_unsent"}
            except Exception:
                return {"kind": "unknown"}
            try:
                value = response.json()
            except Exception:
                return {"kind": "unknown"}
            if not isinstance(value, dict) or type(value.get("ok")) is not bool:
                return {"kind": "unknown"}
            if value.get("ok") is False:
                if type(value.get("error_code")) is int:
                    return {"kind": "rejected", "response": value}
                return {"kind": "unknown"}
            return {"kind": "response", "response": value}
    except Exception:
        return {"kind": "unknown"}


def probe(root=ROOT):
    token, target, _ = credentials(root)
    me = telegram(token, "getMe", {})
    chat = telegram(token, "getChat", {"chat_id": target})
    return {"ok": bool(me.get("ok") and chat.get("ok")), "bot_authenticated": bool(me.get("ok")), "home_reachable": bool(chat.get("ok")), "home_type": chat.get("result", {}).get("type"), "version": VERSION}


def _send_verified(key, profile, session_key, text, root, state, api, *, gate, ctx=None):
    """Claim once, recheck the module immediately before each Telegram write, never retry."""
    allowed, reason = _action_status(gate, root=root, ctx=ctx)
    if not allowed:
        return {"ok": False, "feature_ready": False, "reason": reason}
    token, target, topic = credentials(root)
    with state_db(state) as c:
        c.execute("BEGIN IMMEDIATE")
        existing = c.execute("SELECT status,message_id,verified FROM deliveries WHERE dedupe_key=?", (key,)).fetchone()
        if existing:
            return {"ok": existing[0] == "sent", "duplicate": True, "status": existing[0], "message_id": existing[1], "verified": bool(existing[2])}
        c.execute("INSERT INTO deliveries(dedupe_key,profile,session_key,status,created) VALUES(?,?,?,?,?)", (key, profile, session_key, "sending", time.time()))
    allowed, reason = _action_status(gate, root=root, ctx=ctx)
    if not allowed:
        with state_db(state) as c:
            c.execute("UPDATE deliveries SET status='failed' WHERE dedupe_key=? AND status='sending'", (key,))
        return {"ok": False, "feature_ready": False, "reason": reason, "status": "failed"}
    payload = {"chat_id": target, "text": text, "disable_notification": False, "protect_content": False, "link_preview_options": {"is_disabled": True}}
    if topic is not None:
        payload["message_thread_id"] = topic
    answer = api(token, "sendMessage", payload)
    message = answer.get("result", {})
    sent = bool(answer.get("ok") and message.get("message_id") and message.get("chat", {}).get("id") == target and message.get("text") == text)
    mid = message.get("message_id") if sent else None
    verified = False
    if sent and _action_status(gate, root=root, ctx=ctx)[0]:
        # Bot API has no getMessage. An idempotent same-text edit addresses the
        # exact message: "not modified" proves it still exists with this text.
        check = api(token, "editMessageText", {"chat_id": target, "message_id": mid, "text": text, "link_preview_options": {"is_disabled": True}})
        verified = bool(check.get("ok") and check.get("result", {}).get("message_id") == mid) or (check.get("error_code") == 400 and "message is not modified" in str(check.get("description", "")).lower())
    # No automatic retry after a timeout/ambiguous delivery: Telegram has no
    # idempotency key and retrying could create the very duplicate we promise not to send.
    status = "sent" if sent else ("unknown" if answer.get("ambiguous") else "failed")
    with state_db(state) as c:
        c.execute("UPDATE deliveries SET status=?,message_id=?,verified=? WHERE dedupe_key=?", (status, mid, int(verified), key))
    followup_verified = bool(sent and verified and target > 0 and
        str(message.get('from', {}).get('id')) == token.split(':',1)[0] and
        message.get('from', {}).get('is_bot') is True and message.get('chat', {}).get('type') == 'private' and
        message.get('message_thread_id') == topic)
    return {"ok": sent, "status": status, "message_id": mid, "verified": verified, "followup_verified": followup_verified, "error_code": None if sent else answer.get("error_code")}


def legacy_message_id(message_id, state=STATE):
    """Read-only reject-only recognition for pre-1.5 notification ledger IDs."""
    path = Path(state) / "notifications.sqlite3"
    if type(message_id) is not int or not path.is_file():
        return False
    try:
        with db_read(path) as c:
            row = c.execute("SELECT 1 FROM deliveries WHERE message_id=? AND status='sent' AND verified=1 LIMIT 1", (message_id,)).fetchone()
        return bool(row)
    except Exception:
        return False


def _register_followup_binding(req, checked, result, *, root, state, ctx=None, expected_epoch=None,
                               expected_destination_binding=None):
    """Bind a verified send only under its pre-send epoch and current module policy."""
    descriptor = req.get("followup")
    if (not isinstance(descriptor, dict) or result.get("duplicate") or result.get("status") != "sent" or
            result.get("verified") is not True or result.get('followup_verified') is not True or
            not result.get("message_id") or expected_epoch is None):
        return result
    if (checked.get("profile") != descriptor.get("targetProfile") or
            descriptor.get("profile") != descriptor.get("targetProfile") or
            req.get("profile") != descriptor.get("targetProfile") or
            req.get("session_key") != descriptor.get("session_key") or
            checked.get("final_row_id") != descriptor.get("final_row_id")):
        return result
    inbox = Path(state) / "followup-inbox" / "followups.sqlite3"
    if not inbox.is_file():
        return result
    try:
        followup_bridge = _sibling_module("followup_bridge")
        token, chat, topic = credentials(root)
        if expected_destination_binding is not None:
            recovery = _sibling_module("completion_recovery")
            if recovery.destination_binding_for(token, chat, topic) != expected_destination_binding:
                return result
        if chat <= 0 or not followup_bridge.receiver_info(state=state):
            return result
        bot = int(token.split(":", 1)[0])
        binding = followup_bridge.register_completion(descriptor, bot=bot, chat=chat, topic=topic,
            message_id=int(result["message_id"]), state=state, expected_epoch=expected_epoch,
            policy=lambda: action_enabled("completion_followups_enabled", root=root, ctx=ctx))
        if binding.get("ok"):
            result["binding_id"] = binding["binding_id"]
    except Exception:
        # Ordinary completion delivery remains successful; follow-up stays disabled.
        return result
    return result


def completion_dedupe_key(root, profile, lineage, session_key, final_row_id):
    """The unchanged completion-v2 dedupe identity shared by old and recovery sends."""
    identity = [str(Path(root).resolve()), profile, lineage,
                "completion-v2", session_key, final_row_id]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def deliver_completion_recovery(req, *, root=ROOT, state=STATE, ctx=None):
    """Send only an exactly claimed completion; uncertain outcomes are never replayed."""
    recovery = _sibling_module("completion_recovery")
    policy = _completion_recovery_policy(state, root=root, ctx=ctx)
    if policy.get("ok") is not True:
        return _pre_dispatch_terminal(policy.get("reason", "native_plugin_config_invalid"))
    if policy.get("enabled") is not True:
        return _pre_dispatch_terminal("settings_disabled")

    candidate = req.get("candidate")
    route = req.get("route")
    if (not isinstance(candidate, dict) or set(candidate) !=
            {"candidate_id", "revision", "claim_token", "generation"} or
            not isinstance(route, dict) or req.get("desktop_observed") is not True or
            req.get("profile") != route.get("targetProfile")):
        return {"ok": False, "reason": "invalid_recovery_request"}
    if candidate.get("generation") != policy.get("generation"):
        return _pre_dispatch_terminal("generation_changed")

    try:
        checked = inspect_session(req, root=root, include_preview=True)
    except sqlite3.Error:
        return _pre_dispatch_retry("source_storage_unavailable")
    except OSError:
        return _pre_dispatch_retry("source_storage_unavailable")
    except Exception:
        return _pre_dispatch_terminal("classification_unavailable")
    if not checked.get("ok"):
        reason = checked.get("reason")
        if reason == "source_storage_unavailable":
            return _pre_dispatch_retry("source_storage_unavailable")
        if reason == "awaiting_answer":
            # Preserve the original ordinary attention route. Never send a
            # question through the completion recovery transport.
            return _pre_dispatch_terminal("attention_required")
        if reason == "quiet_period":
            return _pre_dispatch_retry("quiet_period")
        try:
            recovery.cancel(state, root, {"route": route, "runtime_id": req.get("runtime_id"),
                "final_row_id": req.get("final_row_id")}, reason="new_work")
        except Exception:
            pass
        return checked

    source = resolve_recovery_source({"route": route, "session_key": req.get("session_key"),
                                     "final_row_id": req.get("final_row_id")}, root=root)
    if source.get("ok") is not True:
        if source.get("reason") == "source_storage_unavailable":
            return _pre_dispatch_retry("source_storage_unavailable")
        try:
            recovery.cancel(state, root, {"route": route, "runtime_id": req.get("runtime_id"),
                "final_row_id": req.get("final_row_id")}, reason="identity_changed")
        except Exception:
            pass
        return {"ok": False, "reason": "identity_changed"}
    if source.get("profile") != checked.get("profile"):
        return {"ok": False, "reason": "identity_changed"}

    key = completion_dedupe_key(root, checked["profile"], checked["lineage"],
                                req["session_key"], checked["final_row_id"])
    try:
        token, target, topic = credentials(root)
        destination_binding = recovery.destination_binding_for(token, target, topic)
    except Exception:
        return _pre_dispatch_retry("credentials_unavailable")
    try:
        with state_db(state):
            pass
    except Exception:
        return _pre_dispatch_retry("recovery_storage_unavailable")

    # Capture follow-up producer revocation state before any network dispatch.
    registration_epoch = None
    if (isinstance(req.get("followup"), dict) and
            action_enabled("completion_followups_enabled", root=root, ctx=ctx) and
            (Path(state) / "followup-inbox" / "followups.sqlite3").is_file()):
        try:
            registration_epoch = _sibling_module("followup_bridge").capture_registration_epoch(state=state)
        except Exception:
            registration_epoch = None

    try:
        begun = recovery.begin_dispatch(state, root, candidate, route, dedupe_key=key,
            profile=checked["profile"], session_key=req["session_key"],
            runtime_id=req.get("runtime_id"), started_at=req.get("started_at"),
            epoch=req.get("epoch"), final_row_id=req.get("final_row_id"),
            destination_binding=destination_binding)
    except Exception as exc:
        if isinstance(exc, recovery.RecoveryError) and str(exc) == "source_identity_unavailable":
            try:
                recovery.cancel(state, root, {"route": route, "runtime_id": req.get("runtime_id"),
                    "final_row_id": req.get("final_row_id")}, reason="identity_changed")
            except Exception:
                pass
            return {"ok": False, "reason": "identity_changed"}
        if recovery.proves_pre_dispatch(state, candidate, route, key):
            retryable_codes = {"credentials_unavailable", "source_storage_unavailable",
                               "recovery_database_unavailable", "recovery_schema_unavailable"}
            proven_failure = isinstance(exc, (sqlite3.Error, OSError)) or (
                isinstance(exc, recovery.RecoveryError) and str(exc) in retryable_codes)
            if proven_failure:
                return _pre_dispatch_retry(_storage_reason(exc))
        return {"ok": False, "status": "unknown", "reason": "dispatch_state_unavailable"}
    if begun.get("ok") is not True:
        return begun
    if begun.get("duplicate"):
        delivery_status = begun.get("status")
        status = "sent" if delivery_status == "sent" else "unknown" if delivery_status in {"sending", "unknown"} else "failed"
        return {"ok": status == "sent", "duplicate": True, "status": status,
                "verified": False, "reason": "existing_delivery"}

    attempt = begun.get("attempt_id")
    candidate_id_value = begun.get("candidate_id")
    if not isinstance(attempt, str) or not isinstance(candidate_id_value, str):
        return {"ok": False, "status": "unknown", "reason": "invalid_attempt_receipt"}

    # Recheck both native setting and its opaque generation after the durable
    # boundary and immediately before the one network send.
    current_policy = _completion_recovery_policy(state, root=root, ctx=ctx)
    if (current_policy.get("ok") is not True or current_policy.get("enabled") is not True or
            current_policy.get("generation") != begun.get("generation")):
        try:
            recovery.finish_dispatch(state, candidate_id_value, attempt, key, "rejected")
        except Exception:
            pass
        return _pre_dispatch_terminal("settings_disabled" if current_policy.get("enabled") is not True
                                      else "generation_changed")

    text = notification_text(checked)
    payload = {"chat_id": target, "text": text, "disable_notification": False,
               "protect_content": False, "link_preview_options": {"is_disabled": True}}
    if topic is not None:
        payload["message_thread_id"] = topic
    response = completion_telegram(token, "sendMessage", payload)
    if response.get("kind") == "proven_unsent":
        try:
            receipt = recovery.finish_dispatch(state, candidate_id_value, attempt, key, "proven_unsent")
            if receipt.get("ok"):
                return {"ok": False, "status": "unsent_retryable", "reason": "connect_failure",
                        "revision": receipt["revision"], "generation": receipt["generation"],
                        "bookkeeping_seq": receipt["bookkeeping_seq"]}
        except Exception:
            pass
        return {"ok": False, "status": "unknown", "reason": "receipt_unavailable"}
    if response.get("kind") == "rejected":
        answer = response.get("response", {})
        try:
            receipt = recovery.finish_dispatch(state, candidate_id_value, attempt, key, "rejected")
            if receipt.get("ok"):
                return {"ok": False, "status": "failed", "error_code": answer.get("error_code")}
        except Exception:
            pass
        return {"ok": False, "status": "unknown", "reason": "receipt_unavailable"}
    if response.get("kind") != "response":
        try:
            receipt = recovery.finish_dispatch(state, candidate_id_value, attempt, key, "unknown")
            if receipt.get("ok"):
                return {"ok": False, "status": "unknown", "error_code": 0}
        except Exception:
            pass
        return {"ok": False, "status": "unknown", "reason": "receipt_unavailable"}

    answer = response.get("response", {})
    message = answer.get("result") if isinstance(answer, dict) else None
    message = message if isinstance(message, dict) else {}
    message_id = message.get("message_id")
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    sent = (answer.get("ok") is True and type(message_id) is int and message_id > 0 and
            type(chat.get("id")) is int and chat.get("id") == target and message.get("text") == text)
    if not sent:
        try:
            receipt = recovery.finish_dispatch(state, candidate_id_value, attempt, key, "unknown")
            if receipt.get("ok"):
                return {"ok": False, "status": "unknown", "error_code": 0}
        except Exception:
            pass
        return {"ok": False, "status": "unknown", "reason": "receipt_unavailable"}

    verified = False
    if _action_status("completion_notifications_enabled", root=root, ctx=ctx)[0]:
        check_result = completion_telegram(token, "editMessageText", {
            "chat_id": target, "message_id": message_id, "text": text,
            "link_preview_options": {"is_disabled": True}})
        if check_result.get("kind") == "response":
            check = check_result.get("response", {})
            checked_message = check.get("result") if isinstance(check.get("result"), dict) else {}
            verified = bool(check.get("ok") is True and checked_message.get("message_id") == message_id)
            if not verified and check.get("error_code") == 400 and "message is not modified" in str(check.get("description", "")).lower():
                verified = True
        elif check_result.get("kind") == "rejected":
            check = check_result.get("response", {})
            verified = bool(check.get("error_code") == 400 and "message is not modified" in str(check.get("description", "")).lower())

    try:
        receipt = recovery.finish_dispatch(state, candidate_id_value, attempt, key, "sent",
                                          message_id=message_id, verified=verified)
    except Exception:
        receipt = {"ok": False}
    if receipt.get("ok") is not True:
        return {"ok": False, "status": "unknown", "reason": "receipt_unavailable"}
    followup_verified = bool(verified and target > 0 and
        str(message.get("from", {}).get("id")) == token.split(":", 1)[0] and
        message.get("from", {}).get("is_bot") is True and chat.get("type") == "private" and
        message.get("message_thread_id") == topic)
    result = {"ok": True, "status": "sent", "message_id": message_id,
              "verified": verified, "followup_verified": followup_verified}
    return _register_followup_binding(req, checked, result, root=root, state=state,
                                      ctx=ctx, expected_epoch=registration_epoch,
                                      expected_destination_binding=destination_binding)


def classify_completion_recovery(req, *, root=ROOT, ctx=None):
    """Read-only native classification used before recovery or ordinary attention send."""
    if (not isinstance(req, dict) or not isinstance(req.get("route"), dict) or
            req.get("desktop_observed") is not True or
            req.get("profile") != req["route"].get("targetProfile")):
        return {"ok": False, "reason": "invalid_recovery_request"}
    try:
        checked = inspect_session(req, root=root)
    except sqlite3.Error:
        return _pre_dispatch_retry("source_storage_unavailable")
    except OSError:
        return _pre_dispatch_retry("source_storage_unavailable")
    except Exception:
        return _pre_dispatch_terminal("classification_unavailable")
    if not checked.get("ok"):
        reason = checked.get("reason")
        if reason == "source_storage_unavailable":
            return _pre_dispatch_retry("source_storage_unavailable")
        if reason == "quiet_period":
            return _pre_dispatch_retry("quiet_period")
        if reason == "awaiting_answer":
            attention_allowed, _ = _action_status("attention_notifications_enabled", root=root, ctx=ctx)
            return {"ok": True, "kind": "attention" if attention_allowed else "disabled"}
        return {"ok": False, "reason": reason if isinstance(reason, str) else "classification_unavailable"}
    completion_allowed, _ = _action_status("completion_notifications_enabled", root=root, ctx=ctx)
    return {"ok": True, "kind": "completion" if completion_allowed else "disabled"}


def deliver(req, *, root=ROOT, state=STATE, api=telegram, test=False, ctx=None):
    completion_allowed, completion_reason = _action_status(
        "completion_notifications_enabled", root=root, ctx=ctx)
    if test:
        if not completion_allowed:
            return {"ok": False, "feature_ready": False, "reason": completion_reason}
        checked = {"ok": True, "lineage": "installation-test", "profile": "default", "title": "설치 확인"}
    else:
        attention_allowed, attention_reason = _action_status(
            "attention_notifications_enabled", root=root, ctx=ctx)
        # Both native/raw/effective policy checks precede any durable session read.
        # Either notification class may authorize classification, but only the
        # exact persisted-final class may pass its fresh delivery gate below.
        if not completion_allowed and not attention_allowed:
            return {"ok": False, "feature_ready": False, "reason": completion_reason}
        checked = inspect_session(req, root=root, include_preview=completion_allowed)
    if not checked["ok"]:
        # A settled turn that ended with a question is input-needed: notify as an
        # attention question, never as success completion and never silently.
        if not test and checked.get("reason") == "awaiting_answer":
            if attention_allowed:
                return deliver_attention({**req, "kind": "question"}, root=root, state=state, api=api, ctx=ctx)
            return {"ok": False, "feature_ready": False, "reason": attention_reason}
        return checked
    if not completion_allowed:
        return {"ok": False, "feature_ready": False, "reason": completion_reason}
    profile, lineage = checked["profile"], checked["lineage"]
    # One claim per fully settled final response, not the session lifetime.
    # Preserve legacy installation-test identity and all prior ledger rows.
    if test:
        identity = [str(root.resolve()), profile, lineage]
        key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    else:
        key = completion_dedupe_key(root, profile, lineage, req["session_key"], checked["final_row_id"])
    if test:
        text = "🔔 Hermes Desktop 완료 알림 설치 확인\n\n모든 기존·신규 프로필에 적용됩니다.\n중간 턴은 제외하고, 요청 작업이 끝나고 대기 작업 없이 60초간 멈출 때마다 한 번 알립니다.\n이 메시지는 전송 경로 확인용 테스트입니다."
    else:
        # Only the validated persisted final is previewed, never earlier turns,
        # hidden reasoning, tool output, or untrusted renderer-supplied text.
        text = notification_text(checked)
    registration_epoch = None
    if (not test and isinstance(req.get('followup'), dict) and
            action_enabled('completion_followups_enabled', root=root, ctx=ctx) and
            (Path(state) / 'followup-inbox' / 'followups.sqlite3').is_file()):
        try:
            registration_epoch = _sibling_module('followup_bridge').capture_registration_epoch(state=state)
        except Exception:
            # No fresh capability may be inferred from missing/unreadable evidence.
            registration_epoch = None
    result = _send_verified(key, profile, lineage, text, root, state, api,
                            gate="completion_notifications_enabled", ctx=ctx)
    return _register_followup_binding(req, checked, result, root=root, state=state,
        ctx=ctx, expected_epoch=registration_epoch) if not test else result


def deliver_attention(req, *, root=ROOT, state=STATE, api=telegram, ctx=None):
    allowed, reason = _action_status("attention_notifications_enabled", root=root, ctx=ctx)
    if not allowed:
        return {"ok": False, "feature_ready": False, "reason": reason}
    kind = req.get("kind")
    if kind == "ask":
        checked = inspect_ask(req, root=root)
    elif kind == "question":
        checked = inspect_question(req, root=root)
    else:
        return {"ok": False, "reason": "unknown_attention_kind"}
    if not checked["ok"]:
        return checked
    profile = checked["profile"]
    anchor = checked.get("ask_id") if kind == "ask" else checked.get("final_row_id")
    # Ask ids are unique per question; question claims are per final row.
    identity = [str(root.resolve()), profile, "attention-v1", req.get("session_key"), kind, anchor]
    key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    result = _send_verified(key, profile, req.get("session_key"), attention_text(checked), root, state, api,
                            gate="attention_notifications_enabled", ctx=ctx)
    return {**result, "kind": kind}


def dispatch_request(req, *, root=ROOT, state=STATE, native=False, result_budget=None, ctx=None):
    """Dispatch one bounded native or direct-launch request through existing actions."""
    try:
        if not isinstance(req, dict):
            return {"ok": False, "reason": "invalid_request"}
        action = req.get("action")
        if action in {"settings_status", "settings_set"}:
            if action == "settings_status" and set(req) != {"action"}:
                return {"ok": False, "reason": "invalid_settings_request"}
            if not native or ctx is None:
                return {"ok": False, "reason": "native_settings_context_required"}
            settings_control = _sibling_module("settings_control")
            if action == "settings_status":
                return settings_control.settings_status(ctx, root=root, state=state)
            return settings_control.settings_set(req, ctx, root=root, state=state)
        if action == "probe":
            result = probe(root=root)
        elif action == "test":
            result = deliver(req, root=root, state=state, test=True, ctx=ctx)
        elif action == "send":
            result = deliver(req, root=root, state=state, ctx=ctx)
        elif action == "completion_recovery_policy":
            result = _policy_action_result(_completion_recovery_policy(state, root=root, ctx=ctx))
        elif action == "completion_recovery_classify":
            result = classify_completion_recovery(req, root=root, ctx=ctx)
        elif action == "completion_recovery_record":
            policy = _completion_recovery_policy(state, root=root, ctx=ctx)
            if policy.get("ok") is not True:
                result = _policy_action_result(policy)
            elif policy.get("enabled") is not True:
                result = {"ok": False, "feature_ready": False, "reason": "completion_notifications_disabled",
                          "generation": policy.get("generation")}
            else:
                recovery = _sibling_module("completion_recovery")
                result = recovery.record(state, root, req.get("candidate"))
        elif action == "completion_recovery_list":
            policy = _completion_recovery_policy(state, root=root, ctx=ctx)
            if policy.get("ok") is not True:
                result = {**_policy_action_result(policy), "items": [], "cursor": None}
            elif policy.get("enabled") is not True:
                result = {"ok": False, "feature_ready": False, "reason": "completion_notifications_disabled",
                          "items": [], "cursor": None}
            else:
                recovery = _sibling_module("completion_recovery")
                result = recovery.list_candidates(state, root, req.get("route"),
                    cursor=req.get("cursor"), result_budget=result_budget if native else 24 * 1024)
        elif action == "completion_recovery_get":
            recovery = _sibling_module("completion_recovery")
            result = recovery.get_candidate(state, root, req.get("candidate_id"), req.get("route"))
        elif action == "completion_recovery_cancel":
            recovery = _sibling_module("completion_recovery")
            result = recovery.cancel(state, root, req.get("identity"), reason=req.get("reason", "new_work"))
        elif action == "completion_recovery_touch":
            recovery = _sibling_module("completion_recovery")
            result = recovery.touch(state, req.get("candidate"), bookkeeping_seq=req.get("bookkeeping_seq"),
                                   quiet_at=req.get("quiet_at"))
        elif action == "completion_recovery_read_blocked":
            recovery = _sibling_module("completion_recovery")
            result = recovery.mark_read_blocked(state, req.get("candidate"), reason=req.get("reason", "snapshot_unavailable"))
        elif action == "completion_recovery_claim":
            policy = _completion_recovery_policy(state, root=root, ctx=ctx)
            if policy.get("ok") is not True:
                result = _policy_action_result(policy)
            elif policy.get("enabled") is not True:
                result = {"ok": False, "feature_ready": False, "reason": "completion_notifications_disabled"}
            else:
                recovery = _sibling_module("completion_recovery")
                result = recovery.claim(state, root, req.get("candidate"), req.get("route"))
        elif action == "completion_recovery_send":
            result = deliver_completion_recovery(req, root=root, state=state, ctx=ctx)
        elif action == "attention":
            result = deliver_attention(req, root=root, state=state, ctx=ctx)
        elif action in ("reply_register", "reply_registration_lookup", "reply_poll", "reply_settle", "reply_fragment", "reply_claim_valid"):
            gateway = _sibling_module("telegram_integration")
            allowed, reason = gateway.feature_status("clarify_replies_enabled", root=root, ctx=ctx)
            if not allowed and action not in {"reply_settle"}:
                if action == "reply_claim_valid":
                    return {"ok": True, "valid": False, "id": req.get("id"), "claim": req.get("claim"),
                            "authority": req.get("authority"), "deadline": req.get("deadline"), "reason": reason}
                return {"ok": False, "feature_ready": False, "reason": reason}
            reply_bridge = _sibling_module("reply_bridge")
            if action == "reply_claim_valid":
                result = reply_bridge.claim_valid(req.get("owner"), req.get("id"), req.get("claim"),
                    req.get("descriptor"), req.get("authority"), req.get("deadline"), root=root, state=state, ctx=ctx)
            else:
                result = reply_bridge.helper(req, sys.modules[__name__], root=root, state=state,
                                             result_budget=result_budget if native else None)
        elif action in {"approval_owner_heartbeat", "approval_register", "approval_poll", "approval_settle",
                        "approval_notice", "approval_claim_valid", "canonical_approval_observe",
                        "approval_native_observe", "approval_native_apply"}:
            gateway = _sibling_module("telegram_integration")
            gate = ("attention_notifications_enabled" if action == "approval_notice"
                    else "approvals_enabled")
            allowed, reason = gateway.feature_status(gate, root=root, ctx=ctx)
            if not allowed and action not in {"approval_settle", "approval_native_apply"}:
                if action == "approval_poll":
                    return {"ok": False, "feature_ready": False, "reason": reason, "items": []}
                if action == "approval_claim_valid":
                    return {"ok": True, "valid": False, "id": req.get("id"), "claim": req.get("claim"),
                            "authority": req.get("authority"), "deadline": req.get("deadline"),
                            "reason": reason}
                return {"ok": False, "feature_ready": False, "reason": reason}
            approval_bridge = _sibling_module("approval_bridge")
            result = approval_bridge.helper(req, sys.modules[__name__], root=root, state=state,
                                            result_budget=result_budget if native else None)
        elif isinstance(action, str) and action.startswith("followup_"):
            gateway = _sibling_module("telegram_integration")
            allowed, reason = gateway.feature_status("completion_followups_enabled", root=root, ctx=ctx)
            if not allowed and action not in {"followup_settle", "followup_reject"}:
                return {"ok": False, "feature_ready": False, "reason": reason}
            followup_bridge = _sibling_module("followup_bridge")
            owner = req.get("owner")
            if action == "followup_owner_heartbeat":
                result = followup_bridge.heartbeat_owner(owner, state=state)
            elif action == "followup_ready":
                result = followup_bridge.ready(owner, state=state)
                for item in result.get("items", []):
                    item["descriptor"] = json.loads(item["descriptor"])
            elif action == "followup_claim":
                result = followup_bridge.claim(owner, req.get("bot"), req.get("chat"), req.get("inbound_id"),
                    req.get("binding_id"), req.get("descriptor"), state=state)
            elif action == "followup_begin_submit":
                result = followup_bridge.begin_submit(owner, req.get("bot"), req.get("chat"), req.get("inbound_id"),
                    req.get("claim"), state=state,
                    policy=lambda: action_enabled("completion_followups_enabled", root=root, ctx=ctx))
            elif action == "followup_reject":
                result = followup_bridge.reject_pending(owner, req.get("bot"), req.get("chat"), req.get("inbound_id"),
                    req.get("binding_id"), state=state)
            elif action == "followup_settle":
                result = followup_bridge.settle(owner, req.get("bot"), req.get("chat"), req.get("inbound_id"),
                    req.get("claim"), req.get("status"), state=state)
            else:
                result = {"ok": False, "reason": "unknown_followup_action"}
            if action in ("followup_settle", "followup_reject") and result.get("ok"):
                try:
                    followup_bridge.notify_receipt(owner, req.get("bot"), req.get("chat"), req.get("inbound_id"),
                                                   sys.modules[__name__], state=state, root=root)
                except Exception:
                    pass
        elif action == "inspect":
            result = inspect_session(req, root=root)
        elif action == "loaded":
            diagnostic("desktop_loaded", {"version": VERSION, "plugin_version": req.get("version"),
                "profiles": req.get("profiles", []), "quiet_seconds": QUIET_SECONDS}, state=state)
            capabilities = []
            approval_enabled = False
            approval_reason = "approval_feature_unavailable"
            try:
                gateway = _sibling_module("telegram_integration")
                approval_enabled, approval_reason = gateway.feature_status("approvals_enabled", root=root)
                approval_bridge = _sibling_module("approval_bridge")
                if (approval_bridge.VERSION == APPROVAL_COMPAT_VERSION and
                        approval_bridge.CAPABILITY == APPROVAL_CAPABILITY):
                    capabilities.append(approval_bridge.CAPABILITY)
                recovery = _sibling_module("completion_recovery")
                if (recovery.VERSION == VERSION and recovery.SCHEMA_VERSION == 1):
                    capabilities.append(RECOVERY_CAPABILITY)
            except Exception:
                approval_enabled = False
            result = {"ok": True, "version": APPROVAL_COMPAT_VERSION,
                      "package_version": VERSION, "capabilities": capabilities,
                      "approval_feature_reason": approval_reason,
                      "completion_policy": _completion_recovery_policy(state, root=root, ctx=ctx)}
            # A disabled feature/migration block is not a missing code protocol;
            # it never authorizes intake or an approval decision.
        elif action == "diagnostic":
            data = {k: req[k] for k in ("profile", "session_key", "reason", "version") if k in req}
            diagnostic("last_check", data, state=state)
            result = {"ok": True}
        else:
            result = {"ok": False, "reason": "unknown_action"}
    except Exception as exc:
        # Exception text is intentionally omitted; it may contain credential URLs.
        action = req.get("action") if isinstance(req, dict) else None
        recovery_actions = {"completion_recovery_policy", "completion_recovery_classify",
            "completion_recovery_record", "completion_recovery_list", "completion_recovery_get",
            "completion_recovery_cancel", "completion_recovery_touch", "completion_recovery_read_blocked",
            "completion_recovery_claim"}
        if action in recovery_actions:
            recovery = _sibling_module("completion_recovery")
            if isinstance(exc, recovery.RecoveryError):
                code = str(exc)
                if code in {"credentials_unavailable", "source_storage_unavailable",
                            "recovery_database_unavailable", "recovery_schema_unavailable"}:
                    reason = "credentials_unavailable" if code == "credentials_unavailable" else (
                        "source_storage_unavailable" if code == "source_storage_unavailable" else
                        "recovery_storage_unavailable")
                    result = _pre_dispatch_retry(reason)
                else:
                    result = {"ok": False, "reason": code if code in recovery.REASONS else "recovery_action_failed"}
            elif isinstance(exc, (sqlite3.Error, OSError)):
                result = _pre_dispatch_retry("recovery_storage_unavailable")
            else:
                result = {"ok": False, "reason": "recovery_action_failed"}
        elif action == "completion_recovery_send":
            result = {"ok": False, "status": "unknown", "reason": "dispatch_state_unavailable"}
        else:
            result = {"ok": False, "reason": type(exc).__name__}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    try:
        if len(args.request) > 30000:
            raise ValueError("oversized_request")
        req = json.loads(base64.urlsafe_b64decode(args.request).decode("utf-8"))
    except Exception as exc:
        result = {"ok": False, "reason": type(exc).__name__}
    else:
        result = dispatch_request(req)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
