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
import json
import re
import sqlite3
import time
from pathlib import Path

VERSION = "1.5.0"
ROOT = Path("C:/Users/ovpow/AppData/Local/hermes")
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


def profile_home(profile, root=ROOT):
    if profile == "default":
        return root
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", profile):
        raise ValueError("invalid_profile")
    home = root / "profiles" / profile
    if not home.is_dir() or home.resolve().parent != (root / "profiles").resolve():
        raise ValueError("unknown_profile")
    return home


def session_context(req, root=ROOT, anchor_row_id=None, require_anchor=False):
    """Shared session/origin gate: home lookup, durable row, source, Desktop origin.

    ``anchor_row_id`` disambiguates the pooled-backend default-alias case by an
    exact persisted row; without it the alias resolves by session id only and
    multiple matches fail closed.
    """
    home = profile_home(req["profile"], root)
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
        for path in (root / "profiles").glob("*/state.db"):
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
    from dotenv import dotenv_values
    values = dotenv_values(root / ".env", interpolate=False)
    token = values.get("TELEGRAM_BOT_TOKEN") or ""
    target = values.get("TELEGRAM_HOME_CHANNEL") or ""
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


def probe(root=ROOT):
    token, target, _ = credentials(root)
    me = telegram(token, "getMe", {})
    chat = telegram(token, "getChat", {"chat_id": target})
    return {"ok": bool(me.get("ok") and chat.get("ok")), "bot_authenticated": bool(me.get("ok")), "home_reachable": bool(chat.get("ok")), "home_type": chat.get("result", {}).get("type"), "version": VERSION}


def _send_verified(key, profile, session_key, text, root, state, api):
    """Claim once, send once, verify by same-text edit; never retry ambiguous sends."""
    token, target, topic = credentials(root)
    with state_db(state) as c:
        c.execute("BEGIN IMMEDIATE")
        existing = c.execute("SELECT status,message_id,verified FROM deliveries WHERE dedupe_key=?", (key,)).fetchone()
        if existing:
            return {"ok": existing[0] == "sent", "duplicate": True, "status": existing[0], "message_id": existing[1], "verified": bool(existing[2])}
        c.execute("INSERT INTO deliveries(dedupe_key,profile,session_key,status,created) VALUES(?,?,?,?,?)", (key, profile, session_key, "sending", time.time()))
    payload = {"chat_id": target, "text": text, "disable_notification": False, "protect_content": True, "link_preview_options": {"is_disabled": True}}
    if topic is not None:
        payload["message_thread_id"] = topic
    answer = api(token, "sendMessage", payload)
    message = answer.get("result", {})
    sent = bool(answer.get("ok") and message.get("message_id") and message.get("chat", {}).get("id") == target and message.get("text") == text)
    mid = message.get("message_id") if sent else None
    verified = False
    if sent:
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


def _register_followup_binding(req, checked, result, *, root, state):
    """Bind only a newly sent, same-text-verified completion and a live v1 receiver."""
    descriptor = req.get("followup")
    if (not isinstance(descriptor, dict) or result.get("duplicate") or result.get("status") != "sent" or
            result.get("verified") is not True or result.get('followup_verified') is not True or not result.get("message_id")):
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
        import followup_bridge
        token, chat, topic = credentials(root)
        if chat <= 0 or not followup_bridge.receiver_info(state=state):
            return result
        bot = int(token.split(":", 1)[0])
        binding = followup_bridge.register_completion(descriptor, bot=bot, chat=chat, topic=topic,
                                                       message_id=int(result["message_id"]), state=state)
        if binding.get("ok"):
            result["binding_id"] = binding["binding_id"]
    except Exception:
        # Ordinary completion delivery remains successful; follow-up stays disabled.
        return result
    return result


def deliver(req, *, root=ROOT, state=STATE, api=telegram, test=False):
    checked = {"ok": True, "lineage": "installation-test", "profile": "default", "title": "설치 확인"} if test else inspect_session(req, root=root, include_preview=True)
    if not checked["ok"]:
        # A settled turn that ended with a question is input-needed: notify as an
        # attention question, never as success completion and never silently.
        if not test and checked.get("reason") == "awaiting_answer":
            return deliver_attention({**req, "kind": "question"}, root=root, state=state, api=api)
        return checked
    profile, lineage = checked["profile"], checked["lineage"]
    # One claim per fully settled final response, not the session lifetime.
    # Preserve legacy installation-test identity and all prior ledger rows.
    identity = [str(root.resolve()), profile, lineage]
    if not test:
        identity += ["completion-v2", req["session_key"], checked["final_row_id"]]
    key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    if test:
        text = "🔔 Hermes Desktop 완료 알림 설치 확인\n\n모든 기존·신규 프로필에 적용됩니다.\n중간 턴은 제외하고, 요청 작업이 끝나고 대기 작업 없이 60초간 멈출 때마다 한 번 알립니다.\n이 메시지는 전송 경로 확인용 테스트입니다."
    else:
        # Only the validated persisted final is previewed, never earlier turns,
        # hidden reasoning, tool output, or untrusted renderer-supplied text.
        text = notification_text(checked)
    result = _send_verified(key, profile, lineage, text, root, state, api)
    return _register_followup_binding(req, checked, result, root=root, state=state) if not test else result


def deliver_attention(req, *, root=ROOT, state=STATE, api=telegram):
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
    result = _send_verified(key, profile, req.get("session_key"), attention_text(checked), root, state, api)
    return {**result, "kind": kind}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    try:
        if len(args.request) > 30000:
            raise ValueError("oversized_request")
        req = json.loads(base64.urlsafe_b64decode(args.request).decode("utf-8"))
        action = req.get("action")
        if action == "probe":
            result = probe()
        elif action == "test":
            result = deliver(req, test=True)
        elif action == "send":
            result = deliver(req)
        elif action == "attention":
            result = deliver_attention(req)
        elif action in ("reply_register", "reply_poll", "reply_settle"):
            import reply_bridge
            import sys
            result = reply_bridge.helper(req, sys.modules[__name__])
        elif isinstance(action, str) and action.startswith("followup_"):
            import followup_bridge
            import reply_bridge
            if not reply_bridge.enabled():
                print(json.dumps({'ok':False,'feature_ready':False,'reason':'disabled'}))
                return
            owner = req.get("owner")
            if action == "followup_owner_heartbeat":
                result = followup_bridge.heartbeat_owner(owner)
            elif action == "followup_ready":
                result = followup_bridge.ready(owner)
                for item in result.get("items", []):
                    item["descriptor"] = json.loads(item["descriptor"])
            elif action == "followup_claim":
                result = followup_bridge.claim(owner, req.get("bot"), req.get("chat"), req.get("inbound_id"),
                    req.get("binding_id"), req.get("descriptor"))
            elif action == "followup_begin_submit":
                result = followup_bridge.begin_submit(owner, req.get("bot"), req.get("chat"), req.get("inbound_id"), req.get("claim"))
            elif action == "followup_reject":
                result = followup_bridge.reject_pending(owner, req.get("bot"), req.get("chat"), req.get("inbound_id"), req.get("binding_id"))
            elif action == "followup_settle":
                result = followup_bridge.settle(owner, req.get("bot"), req.get("chat"), req.get("inbound_id"), req.get("claim"), req.get("status"))
            else:
                result = {"ok": False, "reason": "unknown_followup_action"}
            if action in ('followup_settle','followup_reject') and result.get('ok'):
                import sys
                try:followup_bridge.notify_receipt(owner,req.get('bot'),req.get('chat'),req.get('inbound_id'),sys.modules[__name__])
                except Exception:pass
        elif action == "inspect":
            result = inspect_session(req)
        elif action == "loaded":
            diagnostic("desktop_loaded", {"version": VERSION, "plugin_version": req.get("version"), "profiles": req.get("profiles", []), "quiet_seconds": QUIET_SECONDS})
            result = {"ok": True, "version": VERSION}
        elif action == "diagnostic":
            data = {k: req[k] for k in ("profile", "session_key", "reason", "version") if k in req}
            diagnostic("last_check", data)
            result = {"ok": True}
        else:
            result = {"ok": False, "reason": "unknown_action"}
    except Exception as exc:
        result = {"ok": False, "reason": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
