"""Clarify-only Telegram inbox. Existing gateway owns all Telegram updates.
Credentials stay in the local helper; exact live request validation stays in Desktop.
"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import importlib
import json
import logging
import secrets
import sqlite3
import sys
import time
from pathlib import Path

PREFIX = 'hdq:'
# Question-card TTL policy: follow the asking profile's clarify wait (the same
# ``agent.clarify_timeout`` knob that decides how long the Desktop ask stays
# open), so a card is never dead while the agent is still waiting. Floor 1h per
# user policy; cap 24h — the store keeps form content one day past expiry.
DEFAULT_TTL = 3600
MIN_TTL = 3600
MAX_TTL = 86400
LEASE = 45
FRAGMENT_CHUNK_BYTES = 16 * 1024
MAX_FRAGMENT_BYTES = 256 * 1024
MAX_FRAGMENT_CHUNKS = MAX_FRAGMENT_BYTES // FRAGMENT_CHUNK_BYTES
# One native apply claim per poll keeps fragment reads from aging sibling claims.
MAX_NATIVE_POLL_ITEMS = 1
ACTIVE = ('posting', 'open', 'queued', 'applying')
STALE_SEEN = -1e30


def _active_hermes_home():
    from hermes_constants import get_hermes_home

    try:
        return Path(get_hermes_home()).expanduser().resolve(strict=True)
    except Exception as exc:
        raise RuntimeError('hermes_home_unavailable') from exc


ROOT = _active_hermes_home()
STATE = ROOT / 'plugin-data/session-complete-telegram'
_NOTIFIER = None
_WIRED_MARKER = "_newmetaw_session_complete_telegram_wired"
_WIRING_STATE = "_newmetaw_session_complete_telegram_wiring_state"
_QUARANTINE_GROUP = -10000
logger = logging.getLogger("hermes.plugin.session_complete_telegram")


def _add_owned_handler(application, handler, group, owned_handlers):
    if owned_handlers is not None:
        owned_handlers.append((handler, group))
    application.add_handler(handler, group=group)


def _has_untracked_owned_handlers(application, wiring) -> bool:
    """Read public PTB handler registrations and recognize only this package's code files."""
    handlers = getattr(application, "handlers", None)
    if not isinstance(handlers, dict):
        return False
    package_root = Path(__file__).resolve().parent
    known = {id(handler) for handler, _group in wiring.get("owned_handlers", ())}
    quarantine = wiring.get("quarantine_handler")
    if quarantine is not None:
        known.add(id(quarantine))
    for group_handlers in handlers.values():
        if not isinstance(group_handlers, (list, tuple)):
            continue
        for handler in group_handlers:
            if id(handler) in known:
                continue
            callback = getattr(handler, "callback", None)
            module = sys.modules.get(getattr(callback, "__module__", ""))
            module_file = getattr(module, "__file__", None)
            if not module_file:
                continue
            try:
                source_path = Path(module_file).resolve()
                if (source_path.parent == package_root and
                        source_path.name in {"reply_bridge.py", "approval_bridge.py", "followup_bridge.py"}):
                    return True
            except (OSError, RuntimeError, ValueError):
                continue
    return False


def local_notifier():
    if _NOTIFIER is not None:
        return _NOTIFIER
    if __package__:
        return importlib.import_module('.notifier', __package__)
    return importlib.import_module('notifier')

def _resolve_clarify_timeout(cfg):
    """Mirror of ``tools.clarify_gateway.resolve_clarify_timeout``: legacy
    ``clarify.timeout`` wins when explicitly set, else ``agent.clarify_timeout``,
    else 3600. Kept inline so this plugin stays core-independent; the plugin
    test suite pins parity with the core resolver."""
    raw = (cfg.get('clarify') or {}).get('timeout')
    if raw is None:
        raw = (cfg.get('agent') or {}).get('clarify_timeout', DEFAULT_TTL)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TTL

def form_ttl(target_profile, root=ROOT):
    """Question-card lifetime for an ask from ``target_profile``: follow that
    profile's clarify wait (the Desktop ask's own deadline), floored at 1 hour
    and capped at 24 hours. Non-positive (unlimited ask) is capped too — the
    store keeps form content one day past expiry."""
    cfg = {}
    try:
        import yaml
        home = local_notifier().profile_home(target_profile, root=root)
        loaded = yaml.safe_load((home / 'config.yaml').read_text(encoding='utf-8-sig')) or {}
        if isinstance(loaded, dict):
            cfg = loaded
    except Exception:
        cfg = {}
    value = _resolve_clarify_timeout(cfg)
    if value <= 0:
        return MAX_TTL
    return max(MIN_TTL, min(value, MAX_TTL))

def expiry_label(seconds):
    seconds = int(seconds)
    hours, minutes = divmod(seconds // 60, 60)
    if hours and minutes: return f'{hours}시간 {minutes}분'
    if hours: return f'{hours}시간'
    if minutes: return f'{minutes}분'
    return f'{seconds}초'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def connect(state=STATE):
    state = Path(state) / 'reply-inbox'
    state.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(state / 'replies.sqlite3', timeout=8, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA secure_delete=ON')
    c.executescript('''
    CREATE TABLE IF NOT EXISTS receiver (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS forms (
      id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, owner TEXT NOT NULL,
      descriptor TEXT NOT NULL, params TEXT NOT NULL, questions TEXT NOT NULL,
      bot INTEGER NOT NULL, boot TEXT NOT NULL, chat INTEGER NOT NULL, topic INTEGER,
      created REAL NOT NULL, expires REAL NOT NULL, seen REAL NOT NULL,
      status TEXT NOT NULL, mids TEXT NOT NULL DEFAULT '{}', texts TEXT NOT NULL DEFAULT '{}',
      answers TEXT NOT NULL DEFAULT '{}', selected TEXT NOT NULL DEFAULT '{}',
      answer TEXT, claim TEXT, dirty INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX IF NOT EXISTS form_status ON forms(status, expires);
    CREATE TABLE IF NOT EXISTS receipts (event TEXT PRIMARY KEY, created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS send_claims (identity TEXT PRIMARY KEY, form_id TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS tombstones (bot INTEGER, chat INTEGER, mid INTEGER, form_id TEXT, question_index INTEGER, PRIMARY KEY(bot,chat,mid));
    ''')
    return c


def _feature_status(root, ctx=None):
    if __package__:
        gateway = importlib.import_module('.telegram_integration', __package__)
    else:
        gateway = importlib.import_module('telegram_integration')
    return gateway.feature_status('replies_enabled', root=root, ctx=ctx)


def replies_enabled(root=ROOT):
    return _feature_status(root)[0]


def _retire(c, form_id, status):
    c.execute("UPDATE forms SET status=?,answer=NULL,answers='{}',selected='{}',claim=NULL,dirty=1 WHERE id=?", (status, form_id))


def _prune(c, now):
    for old in c.execute('SELECT id,bot,chat,mids FROM forms WHERE expires<?',(now-86400,)).fetchall():
        for i,mid in json.loads(old['mids']).items():
            c.execute('INSERT OR IGNORE INTO tombstones VALUES(?,?,?,?,?)',(old['bot'],old['chat'],mid,old['id'],int(i)))
    # Absolute TTL is terminal for every state.
    for r in c.execute("SELECT id,status FROM forms WHERE status IN ('posting','open','queued','applying') AND expires<=?", (now,)).fetchall():
        _retire(c, r['id'], 'unknown' if r['status']=='applying' else 'expired')
    # A transport lease gates delivery, not card lifetime. A stale lease blocks
    # new answers/claims while the same owner may renew before the original TTL.
    # Only an in-flight send/claim lapses terminally: its outcome is unknowable
    # and must never be replayed.
    for r in c.execute("SELECT id,status FROM forms WHERE status IN ('posting','applying') AND seen<?", (now-LEASE,)).fetchall():
        _retire(c, r['id'], 'unknown' if r['status']=='applying' else 'expired')
    c.execute('DELETE FROM forms WHERE expires<?', (now-86400,))
    c.execute('DELETE FROM receipts WHERE created<?', (now-86400,))


def bind_receiver(bot, chat, topic, boot, state=STATE, now=None):
    now = time.time() if now is None else now
    if type(bot) is not int or bot <= 0 or type(chat) is not int or chat <= 0:
        raise ValueError('private_identity_required')
    with connect(state) as c:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute("SELECT value FROM receiver WHERE key='live'").fetchone()
        if old and json.loads(old[0])['boot'] != boot:
            for r in c.execute("SELECT id,status FROM forms WHERE status IN ('posting','open','queued','applying')").fetchall():
                _retire(c, r[0], 'unknown' if r['status']=='applying' else 'expired')
        c.execute("INSERT OR REPLACE INTO receiver VALUES('live',?)", (canonical(dict(bot=bot, chat=chat, topic=topic, boot=boot, at=now)),))
    c.close()


def receiver(c, now):
    row = c.execute("SELECT value FROM receiver WHERE key='live'").fetchone()
    live = json.loads(row[0]) if row else None
    return live if live and 0 <= now-live['at'] <= LEASE else None


def shape(ask):
    if not isinstance(ask, dict) or ask.get('method') != 'clarify':
        raise ValueError('clarify_only')
    p = ask.get('params')
    if not isinstance(p, dict) or not isinstance(p.get('session_id'), str):
        raise ValueError('session_required')
    batch = isinstance(p.get('questions'), list) and bool(p['questions'])
    qs = p['questions'] if batch else [p]
    if not 1 <= len(qs) <= 5:
        raise ValueError('question_limit')
    out = []
    for i, q in enumerate(qs):
        if not isinstance(q, dict) or not isinstance(q.get('question'), str) or not q['question'].strip() or len(q['question']) > 2500:
            raise ValueError('invalid_question')
        choices = q.get('choices') or []
        if not isinstance(choices, list) or len(choices)>4 or any(not isinstance(x,str) or not x.strip() or len(x)>500 for x in choices):
            raise ValueError('invalid_choices')
        qid = q.get('qid') if batch else 'single'
        if not isinstance(qid,str) or not qid or len(qid)>100 or any(x['qid']==qid for x in out):
            raise ValueError('invalid_qid')
        out.append(dict(qid=qid, question=q['question'], choices=choices, multi_select=q.get('multi_select') is True))
    locks = p.get('answers') or {}
    if not isinstance(locks,dict) or any(k not in [q['qid'] for q in out] or not isinstance(v,str) for k,v in locks.items()):
        raise ValueError('invalid_locks')
    return out, batch, locks


def keyboard(row, i):
    qs = json.loads(row['questions']); q = qs[i]
    answers = json.loads(row['answers']); selected = json.loads(row['selected']).get(str(i), [])
    if row['status'] != 'open' or q['qid'] in answers:
        return {'inline_keyboard': []}
    notifier = local_notifier()
    rows = []
    for n, choice in enumerate(q['choices']):
        label = str(n+1) + ') ' + notifier.final_preview(choice).replace('\n',' ')[:50]
        if q['multi_select'] and n in selected: label = '✓ ' + label
        rows.append([{'text':label,'callback_data':f'{PREFIX}{row["id"]}:{i}:c:{n}'}])
    if q['multi_select']:
        rows.append([{'text':'선택 제출','callback_data':f'{PREFIX}{row["id"]}:{i}:s:0'}])
    rows.append([{'text':'직접 답변 안내','callback_data':f'{PREFIX}{row["id"]}:{i}:t:0'}])
    return {'inline_keyboard': rows}


def register(req, *, state=STATE, now=None, ttl=None, root=ROOT):
    now = time.time() if now is None else now
    d = req.get('descriptor'); ask = req.get('ask')
    qs,batch,locks = shape(ask)
    n=local_notifier()
    for q in qs:
        labels=[n.final_preview(x) for x in q['choices']]
        if len(set(labels))!=len(labels) or any(not x.strip() for x in labels):
            raise ValueError('indistinguishable_choices')
        if len((n.final_preview(q['question'])+'\n'.join(labels)).encode('utf-16-le'))//2>2800:
            raise ValueError('question_display_too_large')
    keys = ('owner','connectionId','profile','targetProfile','sid','session_key','epoch','request_id','fingerprint')
    if not isinstance(d,dict) or any(not isinstance(d.get(k),str) or not d[k] for k in keys) or len(canonical(d))>30000:
        raise ValueError('invalid_descriptor')
    if d['sid']!=ask['params']['session_id'] or d['request_id']!=ask.get('id') or d['fingerprint']!=canonical(ask):
        raise ValueError('identity_mismatch')
    if ttl is None:
        ttl = form_ttl(d['targetProfile'], root=root)
    c=connect(state)
    try:
        c.execute('BEGIN IMMEDIATE'); _prune(c,now)
        immutable={k:d[k] for k in ('connectionId','profile','targetProfile','sid','session_key','epoch','request_id')}
        identity=hashlib.sha256(canonical(immutable).encode()).hexdigest()
        prior=c.execute('SELECT * FROM forms WHERE identity=?',(identity,)).fetchone()
        if prior:
            try: prior_descriptor=json.loads(prior['descriptor'])
            except (TypeError,ValueError): prior_descriptor={}
            if prior_descriptor.get('fingerprint') != d['fingerprint']:
                if prior['status'] in ACTIVE:
                    _retire(c,prior['id'],'unknown' if prior['status']=='applying' else 'expired')
                c.commit();return {'ok':False,'reason':'immutable_mismatch'}
            if prior['owner'] != d['owner']:
                c.commit();return {'ok':False,'reason':'prior_owner'}
            c.commit(); return {'ok':True,'duplicate':True,'id':prior['id'],'status':prior['status']}
        claimed=c.execute('SELECT form_id FROM send_claims WHERE identity=?',(identity,)).fetchone()
        if claimed:
            c.commit();return {'ok':True,'duplicate':True,'id':claimed[0],'status':'expired'}
        live=receiver(c,now)
        if not live: return {'ok':False,'reason':'receiver_unavailable'}
        # Different observers never mint a second send claim or take over an
        # ambiguous/application claim. A vanished owner expires, not replays.
        if c.execute("SELECT count(*) FROM forms WHERE status IN ('posting','open','queued','applying')").fetchone()[0]>=50:
            return {'ok':False,'reason':'capacity'}
        fid=secrets.token_hex(12)
        c.execute('INSERT INTO send_claims VALUES(?,?)',(identity,fid))
        c.execute('INSERT INTO forms(id,identity,owner,descriptor,params,questions,bot,boot,chat,topic,created,expires,seen,status,answers) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
          (fid,identity,d['owner'],canonical(d),canonical(ask),canonical(qs),live['bot'],live['boot'],live['chat'],live['topic'],now,now+ttl,now,'posting',canonical(locks)))
        c.commit();return {'ok':True,'id':fid,'new':True,'batch':batch,'ttl':ttl}
    finally: c.close()


def read_form(fid,state=STATE):
    c=connect(state)
    try:
        r=c.execute('SELECT * FROM forms WHERE id=?',(fid,)).fetchone()
        return dict(r) if r else None
    finally:c.close()


def _identity_ok(row, identity):
    return bool(row and row['bot']==identity.get('bot') and row['chat']==identity.get('chat')==identity.get('user') and
        row['topic']==identity.get('topic') and identity.get('private') is True and
        identity.get('human') is True and identity.get('bot_author') is True and
        not identity.get('forwarded') and not identity.get('edited') and
        row['boot']==identity.get('boot'))


def _authorized(row, identity, live, now):
    return bool(_identity_ok(row,identity) and live and row['boot']==live['boot'] and
        row['status']=='open' and row['expires']>now and now-row['seen']<=LEASE)


def _message_matches(row,index,identity):
    try:
        mids=json.loads(row['mids']);qs=json.loads(row['questions'])
        return type(index) is int and 0<=index<len(qs) and mids.get(str(index))==identity.get('mid')
    except (TypeError,ValueError,KeyError):
        return False


def intake(fid, index, action, value, identity, *, state=STATE, now=None):
    now=time.time() if now is None else now
    c=connect(state)
    try:
        c.execute('BEGIN IMMEDIATE');_prune(c,now)
        row=c.execute('SELECT * FROM forms WHERE id=?',(fid,)).fetchone()
        live=receiver(c,now)
        if not _authorized(row,identity,live,now):
            # A stale transport lease blocks this delivery attempt; the card
            # stays alive for the same owner to renew before its original TTL.
            receiver_row=c.execute("SELECT value FROM receiver WHERE key='live'").fetchone()
            try: receiver_boot=json.loads(receiver_row[0]).get('boot') if receiver_row else None
            except (TypeError,ValueError,AttributeError): receiver_boot=None
            lease_gap=bool(row and _identity_ok(row,identity) and _message_matches(row,index,identity) and
                row['status']=='open' and row['expires']>now and
                (receiver_boot is None or receiver_boot==row['boot']) and
                (not live or row['boot']==live['boot']) and
                (not live or now-row['seen']>LEASE))
            c.commit()
            return {'ok':False,'reason':'lease_stale' if lease_gap else 'expired_or_unauthorized'}
        event=identity.get('event')
        if not isinstance(event,str) or not event or len(event)>160:
            return {'ok':False,'reason':'missing_event'}
        if c.execute('SELECT 1 FROM receipts WHERE event=?',(event,)).fetchone():
            return {'ok':False,'reason':'duplicate_event'}
        mids=json.loads(row['mids']);qs=json.loads(row['questions'])
        if type(index) is not int or not 0<=index<len(qs) or mids.get(str(index))!=identity.get('mid'):
            return {'ok':False,'reason':'wrong_message'}
        q=qs[index];answers=json.loads(row['answers']);selected=json.loads(row['selected'])
        if q['qid'] in answers: return {'ok':False,'reason':'already_answered'}
        if action=='t': return {'ok':True,'status':'text_help'}
        c.execute('INSERT INTO receipts VALUES(?,?)',(event,now))
        if action=='c':
            if type(value) is not int or not 0<=value<len(q['choices']):return {'ok':False,'reason':'choice'}
            if q['multi_select']:
                chosen=selected.get(str(index),[])
                chosen.remove(value) if value in chosen else chosen.append(value)
                selected[str(index)]=sorted(chosen)
                c.execute('UPDATE forms SET selected=?,dirty=1 WHERE id=?',(canonical(selected),fid))
                c.commit();return {'ok':True,'status':'selected'}
            answer=q['choices'][value]
        elif action=='s' and q['multi_select']:
            chosen=selected.get(str(index),[])
            if not chosen:return {'ok':False,'reason':'empty_selection'}
            answer=canonical([q['choices'][n] for n in chosen])
        elif action=='text':
            if not isinstance(value,str) or not value.strip() or len(value)>2000:return {'ok':False,'reason':'invalid_text'}
            answer=value
        else:return {'ok':False,'reason':'action'}
        answers[q['qid']]=answer
        complete=all(x['qid'] in answers for x in qs)
        batch=isinstance(json.loads(row['params'])['params'].get('questions'),list)
        result={'answers':answers} if batch else {'answer':answer}
        c.execute('UPDATE forms SET answers=?,status=?,answer=?,dirty=1 WHERE id=?',
          (canonical(answers),'queued' if complete else 'open',canonical(result) if complete else None,fid))
        c.commit();return {'ok':True,'status':'queued' if complete else 'partial'}
    finally:c.close()


def lookup_reply(bot,chat,mid,state=STATE):
    c=connect(state)
    try:
        old=c.execute('SELECT form_id,question_index FROM tombstones WHERE bot=? AND chat=? AND mid=?',(bot,chat,mid)).fetchone()
        if old:return old[0],old[1]
        for row in c.execute('SELECT id,mids FROM forms WHERE bot=? AND chat=?',(bot,chat)):
            for index,value in json.loads(row['mids']).items():
                if value==mid:return row['id'],int(index)
        return None
    finally:c.close()


def _wire_json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _frozen_reply_bytes(descriptor, result):
    return canonical({'descriptor': descriptor, 'result': result}).encode('utf-8')


def poll(owner, observations, *, state=STATE, now=None, unverified=None, result_budget=None):
    now=time.time() if now is None else now
    if not isinstance(owner,str) or not owner or not isinstance(observations,list) or len(observations)>50:
        raise ValueError('invalid_poll')
    if unverified is not None and (not isinstance(unverified,list) or len(unverified)>50):
        raise ValueError('invalid_poll')
    if result_budget is not None and (type(result_budget) is not int or result_budget <= 0):
        raise ValueError('invalid_poll')
    allowed={x['id'] for x in observations if isinstance(x,dict) and isinstance(x.get('id'),str)}
    unverifiable={x for x in (unverified or []) if isinstance(x,str)}
    c=connect(state)
    try:
        c.execute('BEGIN IMMEDIATE');_prune(c,now); live=receiver(c,now)
        receiver_row=c.execute("SELECT value FROM receiver WHERE key='live'").fetchone()
        try:
            receiver_value=json.loads(receiver_row[0]) if receiver_row else None
        except (TypeError,ValueError):
            receiver_value=None
        receiver_boot=receiver_value.get('boot') if isinstance(receiver_value,dict) and isinstance(receiver_value.get('boot'),str) else None
        result=[]
        for row in c.execute("SELECT * FROM forms WHERE owner=? AND status IN ('posting','open','queued','applying') ORDER BY created,id",(owner,)).fetchall():
            # An explicitly unverifiable transport observation revokes ingress
            # freshness immediately without consuming queued work or extending
            # TTL. In-flight attempts retain their terminal lapse semantics.
            if row['id'] in unverifiable:
                if row['status'] in ('open','queued'):
                    c.execute('UPDATE forms SET seen=? WHERE id=?',(STALE_SEEN,row['id']))
                continue
            # A known boot change and a healthy explicit absence are terminal
            # even when the receiver heartbeat itself is stale.
            if receiver_boot is not None and row['boot']!=receiver_boot:
                _retire(c,row['id'],'unknown' if row['status']=='applying' else 'expired');continue
            if row['id'] not in allowed:
                _retire(c,row['id'],'unknown' if row['status']=='applying' else 'expired');continue
            # Observed rows survive a temporary receiver lease gap. They are
            # not renewed until the receiver is live again.
            if not live:
                continue
            if row['boot']!=live['boot']:
                _retire(c,row['id'],'unknown' if row['status']=='applying' else 'expired');continue
            if row['status']!='applying':
                c.execute('UPDATE forms SET seen=? WHERE id=?',(now,row['id']))
            if row['status']=='queued':
                # The claim limit never skips another form's observation gate.
                if result_budget is not None and len(result)>=MAX_NATIVE_POLL_ITEMS:
                    continue
                claim=secrets.token_hex(12)
                deadline=min(row['expires'],now+LEASE)
                descriptor=json.loads(row['descriptor'])
                answer=json.loads(row['answer'])
                ordinary={'id':row['id'],'claim':claim,'deadline':deadline,'descriptor':descriptor,'result':answer}
                selected=ordinary

                if result_budget is not None:
                    if len(_wire_json_bytes({'ok':True,'items':result+[ordinary]})) > result_budget:
                        if result or len(_wire_json_bytes({'ok':True,'items':[ordinary]})) <= result_budget:
                            continue
                        frozen=_frozen_reply_bytes(descriptor,answer)
                        if len(frozen)>MAX_FRAGMENT_BYTES:
                            continue
                        chunks=(len(frozen)+FRAGMENT_CHUNK_BYTES-1)//FRAGMENT_CHUNK_BYTES
                        if not chunks or chunks>MAX_FRAGMENT_CHUNKS:
                            continue
                        digest=hashlib.sha256(frozen).hexdigest()
                        selected={'id':row['id'],'claim':claim,'deadline':deadline,
                                  'fragment':{'sha256':digest,'bytes':len(frozen),'chunks':chunks,
                                              'chunk_bytes':FRAGMENT_CHUNK_BYTES}}
                        if len(_wire_json_bytes({'ok':True,'items':result+[selected]})) > result_budget:
                            continue
                c.execute("UPDATE forms SET status='applying',claim=? WHERE id=?",(claim,row['id']))
                result.append(selected)
        c.commit();return {'ok':True,'items':result}
    finally:c.close()


def read_fragment(owner, fid, claim, digest, index, *, state=STATE, now=None):
    """Read one bounded chunk of an existing frozen applying claim; never renew it."""
    now=time.time() if now is None else now
    if (not isinstance(owner,str) or not owner or not isinstance(fid,str) or not fid or
        not isinstance(claim,str) or not claim or not isinstance(digest,str) or len(digest)!=64 or
        any(ch not in '0123456789abcdef' for ch in digest) or type(index) is not int or index<0):
        return {'ok':False,'reason':'invalid_fragment'}
    path=Path(state)/'reply-inbox'/'replies.sqlite3'
    if not path.is_file():
        return {'ok':False,'reason':'fragment_unavailable'}
    c=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    c.row_factory=sqlite3.Row
    try:
        c.execute('PRAGMA query_only=ON')
        row=c.execute("SELECT id,owner,descriptor,answer,boot,status,claim,seen,expires FROM forms WHERE id=? AND owner=? AND claim=? AND status='applying'",(fid,owner,claim)).fetchone()
        receiver_row=c.execute("SELECT value FROM receiver WHERE key='live'").fetchone()
        if not row or not receiver_row:
            return {'ok':False,'reason':'fragment_unavailable'}
        try:
            live=json.loads(receiver_row['value'])
            receiver_age=now-float(live['at'])
            deadline=min(float(row['expires']),float(row['seen'])+LEASE)
        except (TypeError,ValueError,KeyError,AttributeError):
            return {'ok':False,'reason':'fragment_unavailable'}
        if (float(row['expires'])<=now or deadline<=now or receiver_age<0 or receiver_age>LEASE or
                not isinstance(live,dict) or live.get('boot')!=row['boot']):
            return {'ok':False,'reason':'claim_expired'}
        try:
            frozen=_frozen_reply_bytes(json.loads(row['descriptor']),json.loads(row['answer']))
        except (TypeError,ValueError,RecursionError):
            return {'ok':False,'reason':'fragment_corrupt'}
        if len(frozen)>MAX_FRAGMENT_BYTES or hashlib.sha256(frozen).hexdigest()!=digest:
            return {'ok':False,'reason':'fragment_corrupt'}
        chunks=(len(frozen)+FRAGMENT_CHUNK_BYTES-1)//FRAGMENT_CHUNK_BYTES
        if not chunks or chunks>MAX_FRAGMENT_CHUNKS or index>=chunks:
            return {'ok':False,'reason':'invalid_fragment'}
        start=index*FRAGMENT_CHUNK_BYTES
        data=base64.urlsafe_b64encode(frozen[start:start+FRAGMENT_CHUNK_BYTES]).rstrip(b'=').decode('ascii')
        return {'ok':True,'id':fid,'claim':claim,'sha256':digest,'index':index,
                'bytes':len(frozen),'chunks':chunks,'chunk_bytes':FRAGMENT_CHUNK_BYTES,'data':data}
    finally:
        c.close()


def settle(fid,claim,status,*,state=STATE):
    if status not in ('applied','expired','unknown','rejected'):raise ValueError('invalid_status')
    c=connect(state)
    try:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute("SELECT id FROM forms WHERE id=? AND claim=? AND status='applying'",(fid,claim)).fetchone()
        if not row:return {'ok':False,'reason':'claim_lost'}
        _retire(c,fid,status);c.commit();return {'ok':True,'status':status}
    finally:c.close()


def publish(fid,notifier,*,state=STATE):
    row=read_form(fid,state);token,chat,topic=notifier.credentials()
    if not row or row['status']!='posting':return
    if row['chat']!=chat or row['topic']!=topic:raise ValueError('destination_changed')
    d=json.loads(row['descriptor']);qs=json.loads(row['questions'])
    ttl_text=expiry_label(row['expires']-row['created'])
    for i,q in enumerate(qs):
        if q['qid'] in json.loads(row['answers']):continue
        text=(f'❓ Hermes Desktop — 질문 {i+1}/{len(qs)}\n프로필: {d["targetProfile"]}\n'
              f'세션: {d["session_key"]}\n\n'+notifier.final_preview(q['question'])+
              ''.join(f'\n{n+1}) '+notifier.final_preview(choice) for n,choice in enumerate(q['choices']))+
              f'\n\n버튼을 선택하거나 이 메시지에 답장해 주세요.\n모든 질문에 답하면 작업이 이어집니다. {ttl_text} 후 만료됩니다.')
        row['status']='open';markup=keyboard(row,i)
        payload={'chat_id':chat,'text':text,'reply_markup':markup,'protect_content':False,'disable_notification':False,'link_preview_options':{'is_disabled':True}}
        if topic is not None:payload['message_thread_id']=topic
        response=notifier.telegram(token,'sendMessage',payload)
        msg=response.get('result') or {}
        if not response.get('ok') or msg.get('chat',{}).get('id')!=chat or msg.get('text')!=text or not msg.get('message_id'):
            c=connect(state)
            with c:_retire(c,fid,'unknown')
            c.close();return
        c=connect(state)
        try:
            c.execute('BEGIN IMMEDIATE')
            fresh=c.execute('SELECT mids,texts FROM forms WHERE id=?',(fid,)).fetchone()
            mids=json.loads(fresh['mids']);texts=json.loads(fresh['texts'])
            mids[str(i)]=msg['message_id'];texts[str(i)]=text
            c.execute('INSERT OR IGNORE INTO tombstones VALUES(?,?,?,?,?)',(row['bot'],chat,msg['message_id'],fid,i))
            c.execute('UPDATE forms SET mids=?,texts=? WHERE id=?',(canonical(mids),canonical(texts),fid));c.commit()
        finally:c.close()
        # Read-back exact message; keep the keyboard during verification.
        notifier.telegram(token,'editMessageText',{'chat_id':chat,'message_id':msg['message_id'],'text':text,'reply_markup':markup,'link_preview_options':{'is_disabled':True}})
        if i+1<len(qs):time.sleep(1.05)
    c=connect(state)
    with c:c.execute("UPDATE forms SET status='open' WHERE id=? AND status='posting'",(fid,))
    c.close()


def flush(notifier,*,state=STATE):
    c=connect(state)
    try:
        _prune(c,time.time())
        rows=[dict(r) for r in c.execute('SELECT * FROM forms WHERE dirty=1 LIMIT 20')]
    finally:c.close()
    if not rows:return
    token,chat,topic=notifier.credentials()
    for row in rows:
        if (row['chat'],row['topic'])!=(chat,topic):continue
        done=True;answers=json.loads(row['answers']);qs=json.loads(row['questions'])
        for index,mid in json.loads(row['mids']).items():
            i=int(index);text=json.loads(row['texts'])[index]
            status=row['status']
            if status=='open' and qs[i]['qid'] in answers:tail='\n\n✓ 이 질문의 답변을 받았습니다. 나머지 질문에 답해 주세요.'
            elif status in ('queued','applying'):tail='\n\n⏳ 답변을 받았습니다. Desktop 반영을 확인 중입니다.'
            elif status=='applied':tail='\n\n✅ Desktop에 응답을 반영했습니다.'
            elif status=='unknown':tail='\n\n⚠️ 반영 여부를 확인하지 못했습니다. Desktop에서 확인해 주세요.'
            elif status in ('expired','rejected'):tail='\n\n⌛ 종료되었거나 만료된 질문입니다. Desktop에서 확인해 주세요.'
            else:tail=''
            response=notifier.telegram(token,'editMessageText',{'chat_id':chat,'message_id':mid,'text':text+tail,'reply_markup':keyboard(row,i),'link_preview_options':{'is_disabled':True}})
            done=done and (response.get('ok') is True or (response.get('error_code')==400 and 'not modified' in str(response.get('description',''))))
        if done:
            c=connect(state)
            with c:c.execute('UPDATE forms SET dirty=0 WHERE id=? AND status=? AND answers=? AND selected=?',(row['id'],row['status'],row['answers'],row['selected']))
            c.close()


def helper(req, notifier, *, root=ROOT, state=STATE, result_budget=None):
    global _NOTIFIER
    _NOTIFIER = notifier
    allowed, reason = _feature_status(root)
    if not allowed:
        return {'ok': False, 'feature_ready': False, 'reason': reason}
    action=req['action']
    if action=='reply_register':
        checked=notifier.inspect_ask({'ask_method':'clarify','ask_id':req.get('ask',{}).get('id'),'ask_params':req.get('ask',{}).get('params',{}),'profile':req.get('descriptor',{}).get('targetProfile'),'session_key':req.get('descriptor',{}).get('session_key'),'desktop_observed':True},root=root)
        if not checked.get('ok'):return checked
        result=register(req,state=state,root=root)
        if result.get('new'):publish(result['id'],notifier,state=state)
    elif action=='reply_poll':result=poll(req.get('owner'),req.get('observations'),unverified=req.get('unverified'),state=state,result_budget=result_budget)
    elif action=='reply_fragment':
        return read_fragment(req.get('owner'),req.get('id'),req.get('claim'),req.get('sha256'),req.get('index'),state=state)
    elif action=='reply_settle':result=settle(req.get('id'),req.get('claim'),req.get('status'),state=state)
    else:return {'ok':False,'reason':'unknown_reply_action'}
    flush(notifier,state=state)
    return result


def wire(application, adapter, followup=None, approval=None, *, replies_enabled=None, approvals_enabled=None):
    """Install scoped consumers transactionally on the existing PTB Application."""
    from telegram.ext import CallbackQueryHandler, MessageHandler, filters, ApplicationHandlerStop
    bot_data = getattr(application, "bot_data", None)
    if not isinstance(bot_data, dict):
        try:
            application.bot_data = {}
            bot_data = application.bot_data
        except Exception:
            return False
    prior = bot_data.get(_WIRING_STATE)
    if isinstance(prior, dict) and prior.get("untracked_existing") is True:
        return False
    if bot_data.get(_WIRED_MARKER) is True and isinstance(prior, dict) and prior.get("ready") is True:
        return True
    notifier = local_notifier()
    try:
        token, chat, topic = notifier.credentials()
        if application.bot.token != token or chat <= 0:
            return False
        bot = int(token.split(":", 1)[0])
    except Exception:
        logger.warning("Telegram gateway wiring refused: configured_destination_unavailable")
        return False
    if isinstance(prior, dict) and prior.get("bot") == bot and prior.get("chat") == chat:
        wiring = prior
    else:
        wiring = {"bot": bot, "chat": chat, "ready": False, "quarantine_handler": None, "task": None}
        bot_data[_WIRING_STATE] = wiring
    wiring["ready"] = False
    untracked_marker = (bot_data.get(_WIRED_MARKER) is True and
                        not (isinstance(prior, dict) and prior.get("ready") is True))
    safe_approval = approval
    if safe_approval is None:
        try:
            safe_approval = (importlib.import_module(".approval_bridge", __package__)
                             if __package__ else importlib.import_module("approval_bridge"))
        except Exception:
            safe_approval = None
    quarantine = wiring.get("quarantine_handler")
    if quarantine is None:
        def known_approval(reply, chat_id, mid):
            try:
                if safe_approval is not None and safe_approval.lookup_message(
                        bot, chat_id, mid, state=safe_approval.STATE):
                    return True
            except Exception:
                pass
            text = getattr(reply, "text", None)
            author = getattr(reply, "from_user", None)
            return bool(chat_id == chat and getattr(author, "id", None) == bot and
                        getattr(author, "is_bot", False) is True and isinstance(text, str) and
                        text.startswith("⚠️ Hermes Desktop 명령 승인"))

        def known_clarify(reply, chat_id, mid):
            try:
                if lookup_reply(bot, chat_id, mid, state=STATE) is not None:
                    return True
            except Exception:
                pass
            text = getattr(reply, "text", None)
            return bool(isinstance(text, str) and text.startswith("❓ Hermes Desktop — 질문"))

        def known_followup(reply, chat_id, mid):
            try:
                if followup is not None and followup.lookup_outbound(
                        bot, chat_id, mid, state=followup.STATE) is not None:
                    return True
            except Exception:
                pass
            try:
                if notifier.legacy_message_id(mid, state=getattr(notifier, "STATE", STATE)):
                    return True
            except Exception:
                pass
            text = getattr(reply, "text", None)
            author = getattr(reply, "from_user", None)
            return bool(chat_id == chat and getattr(author, "id", None) == bot and
                        getattr(author, "is_bot", False) is True and isinstance(text, str) and
                        text.startswith("✅ Hermes Desktop 세션 작업 종료"))

        class KnownCardReply(filters.MessageFilter):
            def filter(self, message):
                reply = getattr(message, "reply_to_message", None)
                msg_chat = getattr(message, "chat", None)
                author = getattr(reply, "from_user", None) if reply is not None else None
                mid = getattr(reply, "message_id", None) if reply is not None else None
                chat_id = getattr(msg_chat, "id", None)
                if (reply is None or chat_id != chat or getattr(msg_chat, "type", None) != "private" or
                        getattr(author, "id", None) != bot or getattr(author, "is_bot", False) is not True or
                        type(mid) is not int):
                    return False
                if wiring.get("ready") is True:
                    return ((approval is None and known_approval(reply, chat_id, mid)) or
                            (followup is None and known_followup(reply, chat_id, mid)))
                return (known_approval(reply, chat_id, mid) or known_clarify(reply, chat_id, mid) or
                        known_followup(reply, chat_id, mid))

        async def quarantine_known_reply(update, context):
            raise ApplicationHandlerStop

        quarantine = MessageHandler(KnownCardReply(), quarantine_known_reply)
        wiring["quarantine_handler"] = quarantine
        try:
            application.add_handler(quarantine, group=_QUARANTINE_GROUP)
        except Exception:
            try:
                application.remove_handler(quarantine, group=_QUARANTINE_GROUP)
            except Exception:
                logger.warning("Telegram gateway quarantine cleanup failed")
            wiring["quarantine_handler"] = None
            logger.warning("Telegram gateway quarantine install failed")
            return False
    if untracked_marker or _has_untracked_owned_handlers(application, wiring):
        wiring["untracked_existing"] = True
        bot_data[_WIRED_MARKER] = False
        logger.warning("Telegram gateway wiring refused: untracked_existing_handlers")
        return False

    bot = int(token.split(":", 1)[0]); boot = secrets.token_hex(16)
    owned_handlers = []
    task = None

    def reply_gate():
        if callable(replies_enabled):
            try:return replies_enabled() is True
            except Exception:return False
        return _feature_status(ROOT)[0]

    def approval_gate():
        if callable(approvals_enabled):
            try:return approvals_enabled() is True
            except Exception:return False
        if __package__:
            gateway = importlib.import_module(".telegram_integration", __package__)
        else:
            gateway = importlib.import_module("telegram_integration")
        return gateway.feature_enabled("approvals_enabled", root=ROOT)

    def verify_private_home():
        try:
            result=notifier.telegram(token,"getChat",{"chat_id":chat})
            value=result.get("result") or {}
            return bool(result.get("ok") and value.get("type")=="private" and value.get("id")==chat)
        except Exception:
            return False

    async def heartbeat_loop():
        private_verified=False
        verification=None
        retry_at=0
        try:
            while application.running:
                replies_live=reply_gate()
                approvals_live=approval_gate()
                if replies_live:
                    try:await asyncio.to_thread(bind_receiver,bot,chat,topic,boot)
                    except Exception:pass
                if (followup is not None and replies_live) or (approval is not None and approvals_live):
                    if verification is not None and verification.done():
                        try:private_verified=verification.result()
                        except Exception:private_verified=False
                        verification=None
                        retry_at=time.monotonic()+60
                    if not private_verified and verification is None and time.monotonic()>=retry_at:
                        verification=asyncio.create_task(asyncio.to_thread(verify_private_home))
                if private_verified and followup is not None and replies_live:
                    try:await asyncio.to_thread(followup.bind_receiver,bot,chat,topic,boot,private_verified=True)
                    except Exception:pass
                if private_verified and approval is not None and approvals_live:
                    try:
                        await asyncio.to_thread(approval.bind_receiver, bot, chat, topic, boot,
                            private_verified=True, version=approval.VERSION, capability=approval.CAPABILITY)
                    except Exception:
                        pass
                await asyncio.sleep(8)
        finally:
            if verification is not None:
                verification.cancel()
                await asyncio.gather(verification,return_exceptions=True)

    async def ready():
        while not application.running:await asyncio.sleep(1)
        await heartbeat_loop()

    def identity(message,user,mid,author):
        return dict(bot=bot,boot=boot,chat=getattr(message.chat,'id',None),user=getattr(user,'id',None),
          topic=getattr(message,'message_thread_id',None),mid=mid,private=getattr(message.chat,'type',None)=='private',
          human=not getattr(user,'is_bot',True),bot_author=getattr(author,'id',None)==bot and getattr(author,'is_bot',False),
          forwarded=bool(getattr(message,'forward_origin',None)),edited=False)

    async def callback(update,context):
        query=update.callback_query
        try:
            bits=query.data.split(':');fid=bits[1];index=int(bits[2]);action=bits[3];value=int(bits[4])
            msg=query.message
            if not reply_gate() or msg is None:raise ValueError('unavailable')
            ident=identity(msg,query.from_user,msg.message_id,msg.from_user)
            ident['event']='callback:'+query.id
            result=await asyncio.to_thread(intake,fid,index,action,value,ident)
            wording='이 메시지를 길게 누르고 답장으로 내용을 입력해 주세요.' if result.get('status')=='text_help' else ('선택을 받았습니다.' if result.get('ok') else ('Desktop 연결을 확인한 뒤 다시 눌러 주세요.' if result.get('reason')=='lease_stale' else '만료되었거나 사용할 수 없는 질문입니다.'))
            await query.answer(wording,show_alert=result.get('status')=='text_help')
            await asyncio.to_thread(flush,notifier)
        except Exception:
            try:await query.answer('처리하지 못했습니다. Desktop에서 확인해 주세요.',show_alert=True)
            except Exception:pass
        raise ApplicationHandlerStop

    class ExactReply(filters.MessageFilter):
        def filter(self,message):
            reply=getattr(message,'reply_to_message',None)
            if reply is None:return False
            return lookup_reply(bot,message.chat.id,reply.message_id) is not None

    async def text_reply(update,context):
        msg=update.message
        try:
            if not reply_gate() or msg is None or update.edited_message is not None:raise ValueError('unavailable')
            reply=msg.reply_to_message;found=lookup_reply(bot,msg.chat.id,reply.message_id)
            ident=identity(msg,msg.from_user,reply.message_id,reply.from_user)
            ident['event']=f'text:{msg.chat.id}:{msg.message_id}'
            ident['forwarded']=ident['forwarded'] or bool(getattr(reply,'forward_origin',None))
            result=await asyncio.to_thread(intake,found[0],found[1],'text',msg.text,ident)
            if not result.get('ok'):
                await msg.reply_text('Desktop 연결을 확인한 뒤 다시 답장해 주세요.' if result.get('reason')=='lease_stale' else '이미 답했거나 만료된 질문입니다. Desktop에서 확인해 주세요.')
            await asyncio.to_thread(flush,notifier)
        except Exception:
            if msg is not None:await msg.reply_text('답변을 반영하지 못했습니다. Desktop에서 확인해 주세요.')
        raise ApplicationHandlerStop

    try:
        if approval is not None:
            if not callable(getattr(approval,"wire",None)):
                raise RuntimeError("approval_consumer_unavailable")
            result=approval.wire(application,notifier,bot,chat,topic,boot,
                approvals_enabled=approval_gate,_owned_handlers=owned_handlers)
            if result is not True:raise RuntimeError("approval_consumer_install_incomplete")
        _add_owned_handler(application,CallbackQueryHandler(callback,pattern=r"^hdq:"),0,owned_handlers)
        _add_owned_handler(application,MessageHandler(ExactReply(),text_reply),0,owned_handlers)
        if followup is not None:
            result=followup.wire(application,notifier,bot,chat,topic,boot,
                enabled=reply_gate,_owned_handlers=owned_handlers)
            if result is not True:raise RuntimeError("followup_consumer_install_incomplete")
        ready_coro=ready()
        try:task=asyncio.get_running_loop().create_task(ready_coro)
        except Exception:
            ready_coro.close()
            raise
        wiring["owned_handlers"]=tuple(owned_handlers)
        wiring["task"]=task
        wiring["ready"]=True
        bot_data[_WIRED_MARKER]=True
        return True
    except Exception as exc:
        if task is not None and not task.done():task.cancel()
        cleanup_failed=False
        for handler,group in reversed(owned_handlers):
            try:application.remove_handler(handler,group=group)
            except Exception:cleanup_failed=True
        wiring["owned_handlers"]=()
        wiring["task"]=None
        wiring["ready"]=False
        bot_data[_WIRED_MARKER]=False
        logger.warning("Telegram gateway wiring incomplete: %s%s",type(exc).__name__,
            " (owned-handler cleanup incomplete)" if cleanup_failed else "")
        return False
