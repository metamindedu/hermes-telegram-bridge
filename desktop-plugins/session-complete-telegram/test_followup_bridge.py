import asyncio, copy, json, os, stat, time, unittest
from pathlib import Path
from unittest.mock import patch
import test_support
import followup_bridge as b

class FollowupTests(unittest.TestCase):
 def setUp(self):
  self.tmp=test_support.temporary_directory(prefix='followup-test-');self.addCleanup(self.tmp.cleanup);self.state=Path(self.tmp.name);self.now=time.time()
  b.bind_receiver(77,99,None,'boot',private_verified=True,state=self.state,now=self.now)
  b.heartbeat_owner('owner',state=self.state,now=self.now)
  self.d=dict(owner='owner',connectionId='local',profile='default',targetProfile='default',sid='sid',session_key='durable',epoch='epoch',started_at='100',final_row_id=42)
  out=b.register_completion(self.d,bot=77,chat=99,topic=None,message_id=100,state=self.state,now=self.now);self.assertTrue(out['ok']);self.bid=out['binding_id']
  self.ident=dict(inbound_id=200,bot_id=77,author_bot_id=77,user_id=99,chat_id=99,private=True,sender_chat=False,topic=None,reply_topic=None,forwarded=False,edited=False,business=False,command=False,text='후속 입력',boot='boot')
 def test_private_inbox_paths_are_owner_only_on_posix(self):
  if os.name!='posix':self.skipTest('POSIX mode bits are not Windows NTFS ACLs')
  state=self.state/'permission-mode-fixture'
  b.bind_receiver(77,99,None,'permission-boot',private_verified=True,state=state,now=self.now)
  b.heartbeat_owner('permission-owner',state=state,now=self.now)
  inbox=state/'followup-inbox';database=inbox/'followups.sqlite3'
  self.assertEqual(stat.S_IMODE(inbox.stat().st_mode),0o700)
  self.assertEqual(stat.S_IMODE(database.stat().st_mode),0o600)
 def test_private_inbox_data_survives_reopen_and_path_errors_remain_visible(self):
  state=self.state/'permission-fixture'
  b.bind_receiver(77,99,None,'permission-boot',private_verified=True,state=state,now=self.now)
  b.heartbeat_owner('permission-owner',state=state,now=self.now)
  with b.connect(state) as reopened:
   receiver=reopened.execute('SELECT boot,chat,private_verified FROM receiver WHERE singleton=1').fetchone()
   owner=reopened.execute('SELECT seen FROM owners WHERE owner=?',('permission-owner',)).fetchone()
  self.assertEqual(tuple(receiver),('permission-boot',99,1))
  self.assertEqual(owner['seen'],self.now)
  blocked=self.state/'not-a-directory';blocked.write_text('fixture',encoding='utf-8')
  with self.assertRaises(OSError):b.connect(blocked)
 def ingest(self,**kw):return b.ingest(77,99,100,201,self.bid,{**self.ident,**kw},state=self.state,now=self.now)
 def claim(self):
  row=b.ready('owner',state=self.state,now=self.now)['items'][0]
  return b.claim('owner',77,99,200,self.bid,row['descriptor'],state=self.state,now=self.now)
 def test_legacy_register_api_is_allowed_only_before_revocation(self):
  self.assertEqual(b.lookup_outbound(77,99,100,state=self.state)["kind"],"completion")
  b.retire_unexecuted(state=self.state,now=self.now+1)
  result=b.register_completion(self.d,bot=77,chat=99,topic=None,message_id=101,state=self.state,now=self.now+1)
  self.assertFalse(result["ok"],result)
  self.assertEqual(b.lookup_outbound(77,99,101,state=self.state),{"kind":"legacy","binding":None})
  with b.connect(self.state) as db:
   self.assertEqual(db.execute("SELECT count(*) FROM bindings WHERE outbound_mid=101").fetchone()[0],0)

 def test_register_and_one_attempt_purge(self):
  self.assertTrue(self.ingest()['ok']);self.assertTrue(self.ingest()['duplicate']);c=self.claim();self.assertTrue(c['ok'])
  self.assertFalse(b.claim('owner',77,99,200,self.bid,c['descriptor'],state=self.state,now=self.now)['ok'])
  with b.connect(self.state) as db:self.assertIsNone(db.execute('SELECT text FROM receipts').fetchone()[0])
  self.assertTrue(b.begin_submit('owner',77,99,200,c['claim'],state=self.state,now=self.now)['ok'])
  self.assertTrue(b.settle('owner',77,99,200,c['claim'],'accepted_queued',state=self.state,now=self.now)['ok'])
 def test_reject_all_identity_mutations(self):
  for key,value in [('bot_id',88),('author_bot_id',88),('user_id',88),('chat_id',88),('private',False),('sender_chat',True),('topic',1),('reply_topic',1),('forwarded',True),('edited',True),('business',True),('command',True),('text',' /stop'),('text',None),('text','x'*2001),('boot','other')]:
   with self.subTest(key=key,value=value):
    with b.connect(self.state) as c:c.execute('DELETE FROM receipts')
    self.assertFalse(self.ingest(**{key:value})['ok'])
    with b.connect(self.state) as c:self.assertIsNone(c.execute('SELECT text FROM receipts').fetchone()[0])
 def test_distinct_reply_allowed_duplicate_update_not_executed(self):
  self.ingest();self.assertFalse(self.ingest(inbound_id=201)['ok'])
 def test_receiver_restart_expires(self):
  self.ingest();b.bind_receiver(77,99,None,'new',private_verified=True,state=self.state,now=self.now)
  self.assertEqual(b.ready('owner',state=self.state,now=self.now)['items'],[])
 def test_expired_claim_not_retried(self):
  self.ingest();c=self.claim()
  self.assertFalse(b.begin_submit('owner',77,99,200,c['claim'],state=self.state,now=self.now+46)['ok'])
  with b.connect(self.state) as db:self.assertEqual(db.execute('SELECT status,text FROM receipts').fetchone()[:],('unknown',None))
 def test_begin_submit_rechecks_policy_and_marks_revoked_claim_unknown(self):
  self.ingest();claimed=self.claim();checks=iter((True,False))
  result=b.begin_submit('owner',77,99,200,claimed['claim'],state=self.state,now=self.now,
                        policy=lambda:next(checks))
  self.assertFalse(result['ok']);self.assertEqual(result['reason'],'feature_disabled')
  with b.connect(self.state) as db:
   self.assertEqual(tuple(db.execute('SELECT status,text,claim FROM receipts').fetchone()),('unknown',None,None))
  self.assertEqual(b.ready('owner',state=self.state,now=self.now)['items'],[])

 def test_no_owner_takeover_or_duplicate_card(self):
  self.assertFalse(b.register_completion(self.d,bot=77,chat=99,topic=None,message_id=100,state=self.state,now=self.now)['ok'])
  self.ingest();self.assertEqual(b.ready('another',state=self.state,now=self.now)['items'],[])
 def test_unverified_receiver_fails_closed(self):
  b.bind_receiver(77,99,None,'boot',private_verified=False,state=self.state,now=self.now)
  self.assertFalse(self.ingest()['ok'])
 def test_stale_binding_consumed(self):
  self.now+=b.BINDING_TTL+1;self.assertFalse(self.ingest()['ok']);self.assertIsNotNone(b.lookup_outbound(77,99,100,state=self.state))
 def test_same_owner_lease_gap_recovers_card_not_rejected_reply(self):
  expires=self.now+b.BINDING_TTL
  self.now+=b.LEASE+5
  b.bind_receiver(77,99,None,'boot',private_verified=True,state=self.state,now=self.now)
  self.assertFalse(self.ingest()['ok'])
  with b.connect(self.state) as db:
   row=db.execute('SELECT status,expires FROM bindings WHERE id=?',(self.bid,)).fetchone()
   self.assertEqual(tuple(row),('active',expires))
   self.assertEqual(tuple(db.execute('SELECT status,text FROM receipts').fetchone()),('rejected',None))
  self.assertTrue(b.heartbeat_owner('owner',state=self.state,now=self.now)['feature_ready'])
  self.assertTrue(self.ingest()['duplicate'])
  self.assertTrue(b.ingest(77,99,100,203,self.bid,{**self.ident,'inbound_id':202},state=self.state,now=self.now)['ok'])
  row=b.ready('owner',state=self.state,now=self.now)['items'][0]
  claimed=b.claim('owner',77,99,202,self.bid,row['descriptor'],state=self.state,now=self.now)
  self.assertTrue(claimed['ok'])
  self.assertTrue(b.begin_submit('owner',77,99,202,claimed['claim'],state=self.state,now=self.now)['ok'])
  self.assertFalse(b.claim('owner',77,99,202,self.bid,row['descriptor'],state=self.state,now=self.now)['ok'])
  with b.connect(self.state) as db:self.assertEqual(db.execute('SELECT expires FROM bindings').fetchone()[0],expires)
 def test_first_recovery_heartbeat_does_not_expire_card(self):
  expires=self.now+b.BINDING_TTL
  self.now+=b.LEASE+5
  b.bind_receiver(77,99,None,'boot',private_verified=True,state=self.state,now=self.now)
  self.assertTrue(b.heartbeat_owner('owner',state=self.state,now=self.now)['feature_ready'])
  with b.connect(self.state) as db:
   self.assertEqual(tuple(db.execute('SELECT status,expires,owner_seen FROM bindings').fetchone()),('active',expires,self.now))
  self.assertTrue(self.ingest()['ok'])
 def test_pending_attempt_expires_but_card_can_recover(self):
  self.assertTrue(self.ingest()['ok'])
  descriptor=b.ready('owner',state=self.state,now=self.now)['items'][0]['descriptor']
  self.now+=b.LEASE+5
  b.bind_receiver(77,99,None,'boot',private_verified=True,state=self.state,now=self.now)
  self.assertFalse(b.claim('owner',77,99,200,self.bid,descriptor,state=self.state,now=self.now)['ok'])
  b.heartbeat_owner('owner',state=self.state,now=self.now)
  self.assertEqual(b.ready('owner',state=self.state,now=self.now)['items'],[])
  with b.connect(self.state) as db:
   self.assertEqual(tuple(db.execute('SELECT status,text FROM receipts').fetchone()),('expired',None))
   self.assertEqual(db.execute('SELECT status FROM bindings').fetchone()[0],'active')
 def test_ttl_and_receiver_restart_still_terminal(self):
  self.now+=b.BINDING_TTL
  b.bind_receiver(77,99,None,'boot',private_verified=True,state=self.state,now=self.now)
  b.heartbeat_owner('owner',state=self.state,now=self.now)
  with b.connect(self.state) as db:self.assertEqual(db.execute('SELECT status FROM bindings').fetchone()[0],'expired')
  self.assertFalse(self.ingest()['ok'])
  self.assertIsNotNone(b.lookup_outbound(77,99,100,state=self.state))
 def test_new_owner_cannot_renew_old_card(self):
  seen=self.now
  self.now+=b.LEASE+5
  b.bind_receiver(77,99,None,'boot',private_verified=True,state=self.state,now=self.now)
  b.heartbeat_owner('different-owner',state=self.state,now=self.now)
  self.assertFalse(self.ingest()['ok'])
  with b.connect(self.state) as db:
   self.assertEqual(tuple(db.execute('SELECT status,owner,owner_seen FROM bindings').fetchone()),('active','owner',seen))
  self.assertEqual(b.ready('different-owner',state=self.state,now=self.now)['items'],[])
 def test_submitting_timeout_stays_unknown_after_recovery(self):
  self.ingest();claimed=self.claim()
  self.assertTrue(b.begin_submit('owner',77,99,200,claimed['claim'],state=self.state,now=self.now)['ok'])
  self.now+=b.LEASE+5
  b.bind_receiver(77,99,None,'boot',private_verified=True,state=self.state,now=self.now)
  b.heartbeat_owner('owner',state=self.state,now=self.now)
  with b.connect(self.state) as db:
   self.assertEqual(tuple(db.execute('SELECT status,text FROM receipts').fetchone()),('unknown',None))
   self.assertEqual(db.execute('SELECT status FROM bindings').fetchone()[0],'active')
  self.assertFalse(b.begin_submit('owner',77,99,200,claimed['claim'],state=self.state,now=self.now)['ok'])
  self.assertEqual(b.ready('owner',state=self.state,now=self.now)['items'],[])
 def test_combined_heartbeat_recovery(self):
  import reply_bridge as rb
  from types import SimpleNamespace
  async def run():
   ticks=[];probes=[];attempts=[];wired=[];clock=[0];real_sleep=asyncio.sleep
   registry={}
   def add_handler(handler,group=0):registry.setdefault(group,[]).append(handler)
   def remove_handler(handler,group=0):
    if handler in registry.get(group,[]):registry[group].remove(handler)
   app=SimpleNamespace(running=True,bot_data={},handlers=registry,bot=SimpleNamespace(token='77:test'),add_handler=add_handler,remove_handler=remove_handler)
   def api(token,method,payload):
    probes.append(method)
    return {'ok':len(probes)>1,'result':{'id':99,'type':'private'}}
   def follow_tick(*a,**kw):
    attempts.append(1)
    if len(attempts)==1:raise OSError('synthetic store outage')
   def follow_wire(*a,**kw):
    wired.append(1)
    return True
   notifier=SimpleNamespace(credentials=lambda:('77:test',99,None),telegram=api)
   follow=SimpleNamespace(bind_receiver=follow_tick,wire=follow_wire)
   async def thread(fn,*a,**kw):return fn(*a,**kw)
   async def sleep(seconds):
    clock[0]+=30
    if len(ticks)>=12:app.running=False
    await real_sleep(0)
   before=set(asyncio.all_tasks())
   with patch.object(rb,'_NOTIFIER',notifier),patch.object(rb,'bind_receiver',side_effect=lambda *a:ticks.append(1)),patch.object(rb,'time',SimpleNamespace(monotonic=lambda:clock[0])),patch.object(rb.asyncio,'to_thread',thread),patch.object(rb.asyncio,'sleep',sleep):
    rb.wire(app,None,follow,replies_enabled=lambda:True,followups_enabled=lambda:True)
    created=set(asyncio.all_tasks())-before
    await asyncio.gather(*created)
   self.assertEqual(len(ticks),12);self.assertEqual(len(probes),2);self.assertGreaterEqual(len(attempts),2);self.assertEqual(wired,[1])
  asyncio.run(run())
 def test_real_ptb_dispatch(self):
  async def run():
   from telegram import Update
   from telegram.ext import Application,ExtBot,MessageHandler,filters
   from telegram.request import BaseRequest
   class Request(BaseRequest):
    @property
    def read_timeout(self):return 1
    async def initialize(self):pass
    async def shutdown(self):pass
    async def do_request(self,url,method,request_data=None,**kwargs):
     return 200,json.dumps({'ok':True,'result':{'id':77,'is_bot':True,'first_name':'test','username':'test_bot'}}).encode()
   class Notifier:
    @staticmethod
    def legacy_message_id(mid):return mid==55
    @staticmethod
    def credentials():return ('77:test',99,None)
   app=Application.builder().bot(ExtBot('77:test',request=Request())).build();await app.initialize();core=[];errors=[]
   async def ordinary(u,c):core.append(u.update_id)
   async def error(u,c):errors.append(type(c.error).__name__)
   import reply_bridge as rb
   from contextlib import ExitStack
   from types import SimpleNamespace
   context=ExitStack();self.addCleanup(context.close);clarify=[];before=set(asyncio.all_tasks())
   context.enter_context(patch.object(rb,'_NOTIFIER',Notifier))
   context.enter_context(patch.object(rb.secrets,'token_hex',return_value='boot'))
   context.enter_context(patch.object(rb,'lookup_reply',side_effect=lambda bot,chat,mid:('form',0) if mid==50 else None))
   context.enter_context(patch.object(rb,'intake',side_effect=lambda *a:(clarify.append(a) or {'ok':True})))
   context.enter_context(patch.object(rb,'flush'))
   follow=SimpleNamespace(wire=lambda *a,**kw:b.wire(*a,state=self.state,**kw))
   rb.wire(app,None,follow,replies_enabled=lambda:True,followups_enabled=lambda:True)
   app.add_handler(MessageHandler(filters.ALL,ordinary),group=0);app.add_handler(MessageHandler(filters.ALL,ordinary),group=99);app.add_error_handler(error)
   base={'message_id':210,'date':1,'chat':{'id':99,'type':'private'},'from':{'id':99,'is_bot':False,'first_name':'owner'},'text':'followup','reply_to_message':{'message_id':100,'date':1,'chat':{'id':99,'type':'private'},'from':{'id':77,'is_bot':True,'first_name':'bot'}}}
   for i,kind in enumerate(['text','edited','media','command','unauth','legacy','outage','clarify_outage']):
    msg=copy.deepcopy(base);msg['message_id']+=i
    if kind=='media':msg.pop('text');msg['photo']=[{'file_id':'f','file_unique_id':'u','width':1,'height':1}]
    if kind=='command':msg['text']='/stop'
    if kind=='unauth':msg['from']['id']=55
    if kind=='legacy':msg['reply_to_message']['message_id']=55
    if kind=='clarify_outage':msg['reply_to_message']['message_id']=50
    u=Update.de_json({'update_id':300+i,'edited_message' if kind=='edited' else 'message':msg},app.bot)
    if kind in ('outage','clarify_outage'):
     with patch.object(b,'lookup_outbound',side_effect=OSError):await app.process_update(u)
    else:await app.process_update(u)
   msg=copy.deepcopy(base);msg['message_id']=299;msg['reply_to_message']['message_id']=999
   await app.process_update(Update.de_json({'update_id':999,'message':msg},app.bot))
   self.assertEqual(core,[999,999]);self.assertEqual(errors,[]);self.assertEqual(len(clarify),1)
   with b.connect(self.state) as c:self.assertEqual(c.execute("SELECT count(*) FROM receipts WHERE status='pending'").fetchone()[0],1)
   # A mapped card remains consumed across stale transport, TTL expiry, and missing binding.
   for i,kind in enumerate(('stale','expired','missing')):
    with b.connect(self.state) as c:
     if kind=='stale':c.execute('UPDATE bindings SET owner_seen=?',(time.time()-b.LEASE-5,))
     elif kind=='expired':c.execute('UPDATE bindings SET expires=?',(time.time()-1,))
     else:c.execute('DELETE FROM bindings')
    msg=copy.deepcopy(base);msg['message_id']=400+i
    await app.process_update(Update.de_json({'update_id':400+i,'message':msg},app.bot))
   self.assertEqual(core,[999,999]);self.assertEqual(errors,[])
   msg=copy.deepcopy(base);msg['message_id']=500;msg.pop('reply_to_message')
   await app.process_update(Update.de_json({'update_id':500,'message':msg},app.bot))
   self.assertEqual(core,[999,999,500,500])
   await app.shutdown()
   tasks=set(asyncio.all_tasks())-before
   for task in tasks:task.cancel()
   await asyncio.gather(*tasks,return_exceptions=True)
   context.close()
  asyncio.run(run())

if __name__=='__main__':unittest.main()
