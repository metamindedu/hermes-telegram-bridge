import asyncio
import base64
import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
import test_support
import reply_bridge as b


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=test_support.temporary_directory(prefix='reply-test-');self.addCleanup(self.tmp.cleanup)
        self.state=Path(self.tmp.name);self.now=1000.0;self.event=0
        b.bind_receiver(77,99,None,'boot',state=self.state,now=self.now)
        self.ask={'id':'srq-aabbcc112233','method':'clarify','params':{'session_id':'runtime1','questions':[{'qid':'q1','question':'어떤 **방식**?','choices':['첫 번째','두 번째'],'multi_select':False}]}}
        self.d={'owner':'desktop1','connectionId':'local','profile':'default','targetProfile':'default','sid':'runtime1','session_key':'persisted1','epoch':'epoch1','request_id':self.ask['id'],'fingerprint':b.canonical(self.ask)}
    def create(self,ask=None,ttl=300):
        ask=ask or self.ask;self.d['fingerprint']=b.canonical(ask)
        out=b.register({'descriptor':self.d,'ask':ask},state=self.state,now=self.now,ttl=ttl)
        self.fid=out['id'];qs=b.shape(ask)[0]
        c=b.connect(self.state)
        with c:c.execute("UPDATE forms SET status='open',mids=?,texts=? WHERE id=?",(b.canonical({str(i):100+i for i in range(len(qs))}),b.canonical({str(i):'test' for i in range(len(qs))}),self.fid))
        c.close();return out
    def ident(self,**kw):
        self.event+=1
        return dict(bot=77,boot='boot',chat=99,user=99,topic=None,mid=100,private=True,human=True,bot_author=True,forwarded=False,edited=False,event='event'+str(self.event),**kw)
    def take(self,action='c',value=0,index=0,identity=None,now=None):
        return b.intake(self.fid,index,action,value,identity or self.ident(),state=self.state,now=now or self.now)
    def claim(self,observations=None,owner='desktop1',now=None):
        return b.poll(owner,observations if observations is not None else [{'id':self.fid,'descriptor':self.d}],state=self.state,now=now or self.now)
    def test_single_batch_choice_and_purge(self):
        self.create();self.assertEqual(self.take()['status'],'queued')
        item=self.claim()['items'][0];self.assertEqual(item['result'],{'answers':{'q1':'첫 번째'}})
        self.assertFalse(self.claim()['items'])
        self.assertTrue(b.settle(self.fid,item['claim'],'applied',state=self.state)['ok'])
        row=b.read_form(self.fid,self.state);self.assertIsNone(row['answer']);self.assertEqual(row['answers'],'{}')
    def test_native_one_claim_still_observes_every_later_form(self):
        for fragmented in (False,True):
            for mode in ('unverified','absent','observed','queued','applying'):
                with self.subTest(fragmented=fragmented,mode=mode):
                    state=self.state/f'{fragmented}-{mode}'
                    b.bind_receiver(77,99,None,'boot',state=state,now=self.now)
                    def make(tag,count,created,mid):
                        ask=copy.deepcopy(self.ask);ask['id']='srq-'+('a'*12 if tag=='A' else 'b'*12)
                        ask['params']['questions']=[{'qid':f'q{i}','question':f'{tag} question {i}?','choices':[],'multi_select':False} for i in range(count)]
                        d={**self.d,'request_id':ask['id'],'fingerprint':b.canonical(ask)}
                        fid=b.register({'descriptor':d,'ask':ask},state=state,now=created,ttl=3600)['id']
                        c=b.connect(state)
                        with c:c.execute("UPDATE forms SET status='open',mids=?,texts=? WHERE id=?",(b.canonical({str(i):mid+i for i in range(count)}),b.canonical({str(i):'fixture' for i in range(count)}),fid))
                        c.close();return fid
                    def answer(fid,index,value,mid,when):
                        identity=self.ident();identity['mid']=mid
                        return b.intake(fid,index,'text',value,identity,state=state,now=when)
                    first=make('A',5 if fragmented else 1,self.now,100)
                    later=make('B',1,self.now+1,200)
                    applying_before=None
                    if mode in ('queued','applying'):
                        self.assertTrue(answer(later,0,'B answer',200,self.now+1.1)['ok'])
                    observations=[{'id':first},{'id':later}]
                    if mode=='applying':
                        prior=b.poll('desktop1',observations,state=state,now=self.now+1.2,result_budget=24*1024)
                        self.assertEqual(prior['items'][0]['id'],later)
                        applying_before=b.read_form(later,state)
                    for index in range(5 if fragmented else 1):
                        self.assertTrue(answer(first,index,'😊'*2000 if fragmented else 'A answer',100+index,self.now+1.5)['ok'])
                    if mode in ('unverified','absent'):observations=[{'id':first}]
                    result=b.poll('desktop1',observations,state=state,now=self.now+2,
                                  unverified=[later] if mode=='unverified' else [],result_budget=24*1024)
                    self.assertEqual(len(result['items']),1)
                    self.assertEqual(result['items'][0]['id'],first)
                    self.assertEqual('fragment' in result['items'][0],fragmented)
                    row=b.read_form(later,state)
                    if mode in ('unverified','absent'):
                        self.assertEqual(row['seen'],b.STALE_SEEN) if mode=='unverified' else self.assertEqual(row['status'],'expired')
                        identity=self.ident();identity['mid']=200
                        before=(row['answers'],row['selected'])
                        self.assertFalse(b.intake(later,0,'text','must not consume',identity,state=state,now=self.now+2.1)['ok'])
                        after=b.read_form(later,state)
                        self.assertEqual((after['answers'],after['selected']),before)
                        c=b.connect(state)
                        self.assertEqual(c.execute('SELECT count(*) FROM receipts WHERE event=?',(identity['event'],)).fetchone()[0],0)
                        c.close()
                    elif mode=='observed':
                        self.assertEqual(row['seen'],self.now+2)
                        self.assertTrue(answer(later,0,'observed answer',200,self.now+2.1)['ok'])
                    elif mode=='queued':
                        self.assertEqual(row['seen'],self.now+2)
                        self.assertEqual(row['status'],'queued');self.assertIsNone(row['claim'])
                    else:
                        self.assertEqual((row['status'],row['seen'],row['expires'],row['claim']),
                                         (applying_before['status'],applying_before['seen'],applying_before['expires'],applying_before['claim']))
                        first_before=b.read_form(first,state)
                        def race(_):return b.poll('desktop1',observations,state=state,now=self.now+2.2,result_budget=24*1024)
                        with ThreadPoolExecutor(max_workers=2) as pool:
                            raced=list(pool.map(race,range(2)))
                        self.assertTrue(all(not item['items'] for item in raced))
                        for fid,prior_row in ((first,first_before),(later,applying_before)):
                            current=b.read_form(fid,state)
                            self.assertEqual((current['seen'],current['expires'],current['claim']),
                                             (prior_row['seen'],prior_row['expires'],prior_row['claim']))
    def test_legacy_single_free_text(self):
        ask=copy.deepcopy(self.ask);ask['params']={'session_id':'runtime1','question':'내용?','choices':[]}
        self.create(ask);self.take('text','답장 내용');self.assertEqual(self.claim()['items'][0]['result'],{'answer':'답장 내용'})
    def test_multiple_questions_wait_for_all(self):
        ask=copy.deepcopy(self.ask);ask['params']['questions'].append({'qid':'q2','question':'이유?','choices':[]})
        self.create(ask);self.assertEqual(self.take()['status'],'partial');self.assertEqual(self.claim()['items'],[])
        ident=self.ident();ident['mid']=101
        self.assertEqual(self.take('text','설명',1,ident)['status'],'queued')
        self.assertEqual(self.claim()['items'][0]['result']['answers'],{'q1':'첫 번째','q2':'설명'})
    def test_multi_toggle_submit_and_duplicate(self):
        ask=copy.deepcopy(self.ask);ask['params']['questions'][0]['multi_select']=True;self.create(ask)
        ident=self.ident();self.take(identity=ident);self.assertFalse(self.take(identity=ident)['ok'])
        self.take(value=1);self.assertEqual(self.take('s')['status'],'queued')
        answer=self.claim()['items'][0]['result']['answers']['q1'];self.assertEqual(json.loads(answer),['첫 번째','두 번째'])
    def test_empty_multiselect_rejected(self):
        ask=copy.deepcopy(self.ask);ask['params']['questions'][0]['multi_select']=True;self.create(ask)
        self.assertFalse(self.take('s')['ok'])
    def test_authorization_matrix(self):
        self.create()
        for key,value in [('bot',78),('boot','other'),('chat',100),('user',100),('topic',1),('mid',101),('private',False),('human',False),('bot_author',False),('forwarded',True),('edited',True)]:
            with self.subTest(key=key):
                ident=self.ident();ident[key]=value;self.assertFalse(self.take(identity=ident)['ok'])
        self.assertEqual(b.read_form(self.fid,self.state)['status'],'open')
    def test_authorization_matches_legacy_truth_table_for_fresh_stale_and_message_identity(self):
        self.create(ttl=3600)
        original=dict(b.read_form(self.fid,self.state))
        base=self.ident()
        def legacy(row,identity,live,now):
            return bool(row and row['bot']==identity.get('bot') and row['chat']==identity.get('chat')==identity.get('user') and
                row['topic']==identity.get('topic') and identity.get('private') is True and
                identity.get('human') is True and identity.get('bot_author') is True and
                not identity.get('forwarded') and not identity.get('edited') and
                row['boot']==identity.get('boot') and live and row['boot']==live['boot'] and
                row['status']=='open' and row['expires']>now and now-row['seen']<=b.LEASE)
        mutations=((None,None),('bot',78),('chat',100),('user',100),('topic',1),('boot','other'),
                   ('private',False),('human',False),('bot_author',False),('forwarded',True),
                   ('edited',True),('mid',999))
        for stale in (False,True):
            row=dict(original);row['seen']=self.now-b.LEASE-1 if stale else self.now
            for key,value in mutations:
                ident=dict(base)
                if key is not None:ident[key]=value
                with self.subTest(stale=stale,key=key):
                    self.assertEqual(b._authorized(row,ident,{'boot':'boot'},self.now),legacy(row,ident,{'boot':'boot'},self.now))
        # The legacy gate intentionally omitted message identity; intake checks
        # it after authorization, and stale lease diagnostics must not reveal it.
        wrong=self.ident();wrong['mid']=999
        self.assertEqual(b.intake(self.fid,0,'c',0,wrong,state=self.state,now=self.now)['reason'],'wrong_message')
        c=b.connect(self.state)
        with c:c.execute('UPDATE forms SET seen=? WHERE id=?',(self.now-b.LEASE-1,self.fid))
        c.close()
        stale_wrong=self.ident();stale_wrong['mid']=999
        self.assertEqual(b.intake(self.fid,0,'c',0,stale_wrong,state=self.state,now=self.now)['reason'],'expired_or_unauthorized')
    def test_concurrent_poll_and_settle_are_one_shot_without_replay(self):
        self.create(ttl=3600);self.take()
        def poll():return b.poll('desktop1',[{'id':self.fid}],state=self.state,now=self.now+1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            polled=list(pool.map(lambda _:poll(),range(2)))
        claimed=[item for response in polled for item in response['items']]
        self.assertEqual(len(claimed),1)
        item=claimed[0]
        def settle(status):return b.settle(self.fid,item['claim'],status,state=self.state)
        with ThreadPoolExecutor(max_workers=2) as pool:
            settled=list(pool.map(settle,('applied','unknown')))
        self.assertEqual(sum(result['ok'] for result in settled),1)
        self.assertEqual(b.poll('desktop1',[{'id':self.fid}],state=self.state,now=self.now+2)['items'],[])
    def test_excluded_methods(self):
        for method in ('approval','sudo','secret','vault.code','vault.save_login','vault.unlock_prompt'):
            ask=copy.deepcopy(self.ask);ask['method']=method
            with self.subTest(method=method),self.assertRaises(ValueError):b.shape(ask)
    def test_changed_definition_rejected(self):
        bad=copy.deepcopy(self.ask);bad['params']['questions'][0]['question']='different'
        with self.assertRaises(ValueError):b.register({'descriptor':self.d,'ask':bad},state=self.state,now=self.now)
    def test_double_registration_dedupes(self):
        self.create();r=b.register({'descriptor':self.d,'ask':self.ask},state=self.state,now=self.now,ttl=300)
        self.assertTrue(r['duplicate']);self.assertEqual(r['id'],self.fid)
    def test_descriptor_fingerprint_is_immutable_even_when_receiver_is_stale(self):
        for case,(status,mutation) in enumerate((('open','question'),('queued','choices'),('applying','partial_answers')),start=1):
            with self.subTest(status=status,mutation=mutation):
                ask=copy.deepcopy(self.ask);ask['id']=f'srq-immutable-{status}'
                self.d['request_id']=ask['id'];b.bind_receiver(77,99,None,'boot',state=self.state,now=self.now);self.create(ask,ttl=3600)
                original=self.fid
                c=b.connect(self.state)
                with c:
                    c.execute('UPDATE forms SET status=?,seen=? WHERE id=?',(status,self.now+40,original))
                    value=json.loads(c.execute("SELECT value FROM receiver WHERE key='live'").fetchone()[0])
                    value['at']=self.now-46
                    c.execute("UPDATE receiver SET value=? WHERE key='live'",(b.canonical(value),))
                c.close()
                changed=copy.deepcopy(ask)
                q=changed['params']['questions'][0]
                if mutation=='question':q['question']='changed question'
                elif mutation=='choices':q['choices']=['C','D']
                else:changed['params']['answers']={'q1':'local partial'}
                self.d['fingerprint']=b.canonical(changed)
                rejected=b.register({'descriptor':self.d,'ask':changed},state=self.state,now=self.now+1,ttl=3600)
                self.assertEqual(rejected,{'ok':False,'reason':'immutable_mismatch'})
                expected='unknown' if status=='applying' else 'expired'
                retired=b.read_form(original,self.state)
                self.assertEqual(retired['status'],expected)
                self.assertEqual(retired['answers'],'{}')
                again=b.register({'descriptor':self.d,'ask':changed},state=self.state,now=self.now+2,ttl=3600)
                self.assertEqual(again,{'ok':False,'reason':'immutable_mismatch'})
                c=b.connect(self.state)
                self.assertEqual(c.execute('SELECT count(*) FROM send_claims').fetchone()[0],case)
                self.assertEqual(c.execute('SELECT count(*) FROM forms').fetchone()[0],case)
                c.close()
    def test_duplicate_registration_never_adopts_prior_owner(self):
        self.create(ttl=3600)
        other=dict(self.d,owner='new-monitor')
        result=b.register({'descriptor':other,'ask':self.ask},state=self.state,now=self.now,ttl=3600)
        self.assertEqual(result,{'ok':False,'reason':'prior_owner'})
        self.assertEqual(b.read_form(self.fid,self.state)['status'],'open')
        c=b.connect(self.state)
        self.assertEqual(c.execute('SELECT count(*) FROM send_claims').fetchone()[0],1)
        c.close()
    def test_receiver_restart_invalidates(self):
        self.create();b.bind_receiver(77,99,None,'new',state=self.state,now=self.now)
        self.assertFalse(self.take()['ok']);self.assertEqual(b.read_form(self.fid,self.state)['status'],'expired')
    def test_missing_heartbeat_rejects(self):
        self.create();blocked=self.take(now=self.now+46)
        self.assertFalse(blocked['ok'])
        self.assertEqual(blocked['reason'],'lease_stale')      # blocked, not expired
        self.assertEqual(b.read_form(self.fid,self.state)['status'],'open')
    def test_lease_gap_blocks_answer_but_same_owner_renews(self):
        self.create(ttl=3600);self.now+=b.LEASE+5
        blocked=self.take(now=self.now)
        self.assertFalse(blocked['ok']);self.assertEqual(blocked['reason'],'lease_stale')
        self.assertEqual(b.read_form(self.fid,self.state)['status'],'open')     # gap never expires the card
        b.bind_receiver(77,99,None,'boot',state=self.state,now=self.now)        # same boot renews the receiver lease
        self.assertEqual(self.claim(now=self.now)['items'],[])                  # observed again; seen renews
        self.take(action='c',value=1,now=self.now+1)                           # same owner answers after recovery
        self.assertEqual(self.claim(now=self.now+2)['items'][0]['result'],{'answers':{'q1':'두 번째'}})
    def test_receiver_lease_gap_blocks_claim_but_card_survives(self):
        self.create(ttl=3600);self.take()
        held=self.claim(now=self.now+50)                                       # receiver heartbeat stale: blocked
        self.assertEqual(held['items'],[])
        self.assertEqual(b.read_form(self.fid,self.state)['status'],'queued')   # never expired by the gap
        b.bind_receiver(77,99,None,'boot',state=self.state,now=self.now+55)    # same boot: lease recovers
        self.assertEqual(self.claim(now=self.now+56)['items'][0]['result'],{'answers':{'q1':'첫 번째'}})
    def test_unverified_observation_blocks_but_never_expires(self):
        self.create(ttl=3600);self.take()
        held=b.poll('desktop1',[],state=self.state,now=self.now+10,unverified=[self.fid])
        self.assertEqual(held['items'],[])
        self.assertEqual(b.read_form(self.fid,self.state)['status'],'queued')   # held, not expired
        self.assertEqual(self.claim(now=self.now+11)['items'][0]['result'],{'answers':{'q1':'첫 번째'}})
    def test_unverified_does_not_apply_open_queued_sentinel_to_inflight_states(self):
        for status in ('posting','applying'):
            with self.subTest(status=status):
                ask=copy.deepcopy(self.ask);ask['id']='srq-unverified-inflight-'+status
                self.d['request_id']=ask['id'];self.create(ask,ttl=3600)
                c=b.connect(self.state)
                with c:c.execute('UPDATE forms SET status=?,seen=? WHERE id=?',(status,self.now,self.fid))
                c.close()
                b.poll('desktop1',[],state=self.state,now=self.now+10,unverified=[self.fid])
                row=b.read_form(self.fid,self.state)
                self.assertEqual(row['status'],status)
                self.assertEqual(row['seen'],self.now)
    def test_unverified_immediately_revokes_ingress_without_consuming_retry_event(self):
        for action,value in (('c',0),('text','same answer')):
            with self.subTest(action=action):
                ask=copy.deepcopy(self.ask);ask['id']='srq-ingress-'+action
                self.d['request_id']=ask['id'];self.create(ask,ttl=3600)
                identity=self.ident();identity['event']='same-event-'+action
                self.assertEqual(self.claim(now=self.now+1)['items'],[])
                held=b.poll('desktop1',[],state=self.state,now=self.now+10,unverified=[self.fid])
                self.assertEqual(held['items'],[])
                before=b.read_form(self.fid,self.state)
                self.assertEqual(before['seen'],-1e30)
                rejected=b.intake(self.fid,0,action,value,identity,state=self.state,now=self.now+10)
                self.assertEqual(rejected,{'ok':False,'reason':'lease_stale'})
                after=b.read_form(self.fid,self.state)
                self.assertEqual(after['status'],'open')
                self.assertEqual(after['answers'],before['answers'])
                self.assertEqual(after['selected'],before['selected'])
                c=b.connect(self.state)
                self.assertIsNone(c.execute('SELECT 1 FROM receipts WHERE event=?',(identity['event'],)).fetchone())
                c.close()
                self.assertEqual(self.claim(now=self.now+11)['items'],[])  # same owner observation renews
                accepted=b.intake(self.fid,0,action,value,identity,state=self.state,now=self.now+12)
                self.assertTrue(accepted['ok'])
                self.assertEqual(accepted['status'],'queued')
                c=b.connect(self.state)
                self.assertIsNotNone(c.execute('SELECT 1 FROM receipts WHERE event=?',(identity['event'],)).fetchone())
                c.close()
    def test_unverified_lease_reason_does_not_leak_for_wrong_identity_or_message(self):
        self.create(ttl=3600)
        b.poll('desktop1',[],state=self.state,now=self.now+10,unverified=[self.fid])
        wrong_identity=self.ident();wrong_identity['event']='wrong-user';wrong_identity['user']=123
        wrong_message=self.ident();wrong_message['event']='wrong-mid';wrong_message['mid']=999
        for identity in (wrong_identity,wrong_message):
            with self.subTest(identity=identity['event']):
                result=b.intake(self.fid,0,'c',0,identity,state=self.state,now=self.now+10)
                self.assertEqual(result['reason'],'expired_or_unauthorized')
        row=b.read_form(self.fid,self.state)
        self.assertEqual(row['status'],'open');self.assertEqual(row['answers'],'{}')
        c=b.connect(self.state)
        self.assertEqual(c.execute('SELECT count(*) FROM receipts').fetchone()[0],0)
        c.close()
    def test_applying_poll_does_not_extend_claim_lease(self):
        self.create(ttl=3600);self.take()
        item=self.claim(now=self.now+1)['items'][0]
        claimed=b.read_form(self.fid,self.state)
        self.assertEqual(claimed['status'],'applying')
        self.assertEqual(self.claim(now=self.now+10)['items'],[])
        observed=b.read_form(self.fid,self.state)
        self.assertEqual(observed['seen'],claimed['seen'])
        self.assertEqual(observed['claim'],item['claim'])
        self.assertEqual(self.claim(now=self.now+b.LEASE+2)['items'],[])
        self.assertEqual(b.read_form(self.fid,self.state)['status'],'unknown')
        self.assertFalse(b.settle(self.fid,item['claim'],'applied',state=self.state)['ok'])
    def test_native_fragment_read_requires_exact_live_claim_and_never_renews_it(self):
        self.create(ttl=3600);self.take()
        item=self.claim(now=self.now+1)['items'][0]
        before=b.read_form(self.fid,self.state)
        raw=b._frozen_reply_bytes(json.loads(before['descriptor']),json.loads(before['answer']))
        digest=hashlib.sha256(raw).hexdigest()
        part=b.read_fragment('desktop1',self.fid,item['claim'],digest,0,state=self.state,now=self.now+1)
        self.assertTrue(part['ok'],part)
        self.assertEqual(base64.urlsafe_b64decode(part['data']+'='*((4-len(part['data'])%4)%4)),raw)
        self.assertFalse(b.read_fragment('other-owner',self.fid,item['claim'],digest,0,state=self.state,now=self.now+1)['ok'])
        self.assertEqual(b.read_fragment('desktop1',self.fid,item['claim'],digest,True,state=self.state,now=self.now+1)['reason'],'invalid_fragment')
        expired=b.read_fragment('desktop1',self.fid,item['claim'],digest,0,state=self.state,now=self.now+b.LEASE+2)
        self.assertEqual(expired['reason'],'claim_expired')
        after=b.read_form(self.fid,self.state)
        self.assertEqual((after['status'],after['claim'],after['seen'],after['expires']),
                         (before['status'],before['claim'],before['seen'],before['expires']))
        self.assertFalse(self.claim(now=self.now+b.LEASE+2)['items'])
        self.assertEqual(b.read_form(self.fid,self.state)['status'],'unknown')
    def test_applying_lease_lapse_is_terminal_unknown_no_replay(self):
        self.create(ttl=3600);self.take()
        item=self.claim(now=self.now+1)['items'][0]
        self.assertEqual(self.claim(now=self.now+2)['items'],[])               # one-shot claim
        self.assertEqual(self.claim(now=self.now+b.LEASE+10)['items'],[])      # lapse is terminal for the attempt
        self.assertEqual(b.read_form(self.fid,self.state)['status'],'unknown')
        self.assertFalse(b.settle(self.fid,item['claim'],'applied',state=self.state)['ok'])
        self.assertEqual(b.read_form(self.fid,self.state)['status'],'unknown') # never replayed
    def test_absolute_expiry(self):
        self.create();b.bind_receiver(77,99,None,'boot',state=self.state,now=self.now+301)
        self.assertFalse(self.take(now=self.now+301)['ok'])
    def test_absolute_expiry_at_long_ttl(self):
        self.create(ttl=b.MIN_TTL);b.bind_receiver(77,99,None,'boot',state=self.state,now=self.now+b.MIN_TTL+1)
        self.assertFalse(self.take(now=self.now+b.MIN_TTL+1)['ok'])
    def test_ttl_boundary_is_terminal_for_every_active_state_and_unverified_poll(self):
        for status in ('posting','open','queued','applying'):
            with self.subTest(status=status):
                ask=copy.deepcopy(self.ask);ask['id']='srq-ttl-'+status
                self.d['request_id']=ask['id'];b.bind_receiver(77,99,None,'boot',state=self.state,now=self.now);self.create(ask,ttl=30)
                expires=self.now+30
                c=b.connect(self.state)
                with c:c.execute('UPDATE forms SET status=?,expires=?,seen=? WHERE id=?',(status,expires,self.now,self.fid))
                c.close()
                b.bind_receiver(77,99,None,'boot',state=self.state,now=expires-1)
                result=b.poll('desktop1',[],state=self.state,now=expires,unverified=[self.fid])
                self.assertEqual(result['items'],[])
                expected='unknown' if status=='applying' else 'expired'
                self.assertEqual(b.read_form(self.fid,self.state)['status'],expected)
    def test_stale_receiver_terminalizes_explicit_absence_but_holds_observed_or_unverified(self):
        for status in ('open','queued','applying'):
            with self.subTest(status=status):
                ask=copy.deepcopy(self.ask);ask['id']='srq-absence-'+status
                self.d['request_id']=ask['id'];self.create(ask,ttl=3600)
                c=b.connect(self.state)
                with c:c.execute('UPDATE forms SET status=?,seen=? WHERE id=?',(status,self.now+40,self.fid))
                c.close()
                absent=b.poll('desktop1',[],state=self.state,now=self.now+46)
                self.assertEqual(absent['items'],[])
                expected='unknown' if status=='applying' else 'expired'
                self.assertEqual(b.read_form(self.fid,self.state)['status'],expected)
        for status,kind in (('open','observed'),('queued','observed'),('open','unverified'),('queued','unverified')):
            with self.subTest(status=status,kind=kind):
                ask=copy.deepcopy(self.ask);ask['id']=f'srq-hold-{status}-{kind}'
                self.d['request_id']=ask['id'];self.create(ask,ttl=3600)
                c=b.connect(self.state)
                with c:c.execute('UPDATE forms SET status=?,seen=? WHERE id=?',(status,self.now+40,self.fid))
                c.close()
                allowed=[{'id':self.fid}] if kind=='observed' else []
                unverified=[self.fid] if kind=='unverified' else []
                held=b.poll('desktop1',allowed,state=self.state,now=self.now+46,unverified=unverified)
                self.assertEqual(held['items'],[])
                self.assertEqual(b.read_form(self.fid,self.state)['status'],status)
    def test_known_boot_mismatch_invalidates_even_with_stale_receiver(self):
        self.create(ttl=3600)
        c=b.connect(self.state)
        with c:
            value=json.loads(c.execute("SELECT value FROM receiver WHERE key='live'").fetchone()[0])
            value.update(boot='new-boot',at=self.now)
            c.execute("UPDATE receiver SET value=? WHERE key='live'",(b.canonical(value),))
            c.execute('UPDATE forms SET seen=? WHERE id=?',(self.now+40,self.fid))
        c.close()
        b.poll('desktop1',[{'id':self.fid}],state=self.state,now=self.now+46)
        self.assertEqual(b.read_form(self.fid,self.state)['status'],'expired')
    def test_local_answer_or_changed_epoch_invalidates(self):
        self.create();self.take();d=dict(self.d,epoch='new')
        self.assertFalse(self.claim([])['items'])
        self.assertEqual(b.read_form(self.fid,self.state)['status'],'expired')
    def test_other_owner_cannot_claim(self):
        self.create();self.take();self.assertFalse(self.claim(owner='foreign')['items'])
    def test_ambiguous_apply_never_reclaims(self):
        self.create();self.take();item=self.claim()['items'][0]
        b.settle(self.fid,item['claim'],'unknown',state=self.state)
        self.assertFalse(self.claim()['items']);self.assertEqual(b.read_form(self.fid,self.state)['status'],'unknown')
    def test_tombstone_reply_stays_recognizable(self):
        self.create();self.claim([])
        self.assertEqual(b.lookup_reply(77,99,100,self.state),(self.fid,0))
        self.assertIsNone(b.lookup_reply(77,99,987,self.state))
    def test_invalid_text_and_payload(self):
        self.create()
        for text in ('','   ','x'*2001,None):self.assertFalse(self.take('text',text)['ok'])
        self.assertFalse(self.take(value=7)['ok'])
    def test_preexisting_batch_locks_preserved(self):
        ask=copy.deepcopy(self.ask);ask['params']['questions'].append({'qid':'q2','question':'설명?','choices':[]});ask['params']['answers']={'q1':'local'}
        self.create(ask);ident=self.ident();ident['mid']=101;self.take('text','remote',1,ident)
        self.assertEqual(self.claim()['items'][0]['result']['answers'],{'q1':'local','q2':'remote'})
    def test_real_backend_settlement_first_wins(self):
        from tui_gateway import server_requests as sr
        sr.bind_sinks(lambda f:None,lambda *a:None,lambda sid:True)
        req=sr.ServerRequest('runtime1','clarify',{'questions':[{'qid':'q1','question':'Q','choices':['A','B'],'multi_select':False}]},qids=['q1'])
        sr._register(req)
        self.addCleanup(sr.cancel,'runtime1')
        self.assertTrue(sr.resolve_response({'jsonrpc':'2.0','id':req.id,'result':{'answers':{'q1':'A'}}}))
        self.assertFalse(sr.resolve_response({'jsonrpc':'2.0','id':req.id,'result':{'answers':{'q1':'B'}}}))
        self.assertEqual(req.result['answers'],{'q1':'A'})
    def test_publish_verifies_with_same_keyboard(self):
        self.d['fingerprint']=b.canonical(self.ask)
        fid=b.register({'descriptor':self.d,'ask':self.ask},state=self.state,now=self.now,ttl=300)['id']
        calls=[]
        def api(token,method,p):
            calls.append((method,p));return {'ok':True,'result':{'message_id':55,'chat':{'id':99},'text':p['text']}}
        n=SimpleNamespace(credentials=lambda:('test',99,None),telegram=api,final_preview=lambda x:x)
        b.publish(fid,n,state=self.state)
        self.assertEqual(calls[0][1]['reply_markup'],calls[1][1]['reply_markup'])
        self.assertIn('5분 후 만료됩니다',calls[0][1]['text'])
        self.assertEqual(b.read_form(fid,self.state)['status'],'open')
    def test_publish_text_states_profile_ttl(self):
        self.d['fingerprint']=b.canonical(self.ask)
        fid=b.register({'descriptor':self.d,'ask':self.ask},state=self.state,now=self.now,ttl=5400)['id']
        calls=[]
        def api(token,method,p):
            calls.append(p);return {'ok':True,'result':{'message_id':55,'chat':{'id':99},'text':p['text']}}
        n=SimpleNamespace(credentials=lambda:('test',99,None),telegram=api,final_preview=lambda x:x)
        b.publish(fid,n,state=self.state)
        self.assertIn('1시간 30분 후 만료됩니다',calls[0]['text'])
    def test_ambiguous_send_not_retried(self):
        fid=b.register({'descriptor':self.d,'ask':self.ask},state=self.state,now=self.now,ttl=300)['id']
        n=SimpleNamespace(credentials=lambda:('test',99,None),telegram=lambda *a:{'ok':False,'ambiguous':True},final_preview=lambda x:x)
        b.publish(fid,n,state=self.state)
        self.assertEqual(b.read_form(fid,self.state)['status'],'unknown')
        r=b.register({'descriptor':self.d,'ask':self.ask},state=self.state,now=self.now,ttl=300)
        self.assertTrue(r['duplicate']);self.assertFalse(r.get('new',False))
    def test_other_owners_cannot_adopt_active_or_terminal_bridge(self):
        self.create();first=self.fid
        for owner in ('other-window','reloaded-window'):
            d=dict(self.d,owner=owner)
            r=b.register({'descriptor':d,'ask':self.ask},state=self.state,now=self.now,ttl=300)
            self.assertEqual(r,{'ok':False,'reason':'prior_owner'})
            self.assertEqual(b.read_form(first,self.state)['status'],'open')
        c=b.connect(self.state)
        with c:b._retire(c,first,'unknown')
        c.close()
        d=dict(self.d,owner='third')
        self.assertEqual(b.register({'descriptor':d,'ask':self.ask},state=self.state,now=self.now,ttl=300),{'ok':False,'reason':'prior_owner'})
    def test_send_claim_survives_content_prune_and_receiver_restart(self):
        for outcome in ('open','unknown'):
            with self.subTest(outcome=outcome):
                ask=copy.deepcopy(self.ask);ask['id']='srq-'+outcome
                self.d['request_id']=ask['id'];self.create(ask);old=self.fid
                c=b.connect(self.state)
                with c:
                    c.execute('UPDATE forms SET status=? WHERE id=?',(outcome,old))
                    b._prune(c,self.now+90000)
                c.close()
                b.bind_receiver(77,99,None,'restart',state=self.state,now=self.now+90000)
                ask['params']['questions'][0]['question']='changed display'
                d=dict(self.d,owner='new-owner',fingerprint=b.canonical(ask))
                r=b.register({'descriptor':d,'ask':ask},state=self.state,now=self.now+90000,ttl=300)
                self.assertTrue(r['duplicate']);self.assertFalse(r.get('new',False));self.assertEqual(r['id'],old)
                b.bind_receiver(77,99,None,'boot',state=self.state,now=self.now)
        ask=copy.deepcopy(self.ask);ask['id']='srq-new-request';self.d['request_id']=ask['id'];self.d['fingerprint']=b.canonical(ask)
        self.assertTrue(b.register({'descriptor':self.d,'ask':ask},state=self.state,now=self.now,ttl=300)['new'])
    def test_tombstone_survives_content_prune(self):
        self.create();c=b.connect(self.state)
        with c:b._prune(c,self.now+90000)
        c.close()
        self.assertIsNone(b.read_form(self.fid,self.state))
        self.assertEqual(b.lookup_reply(77,99,100,self.state),(self.fid,0))
    def test_complete_numbered_choices_and_collision_refusal(self):
        ask=copy.deepcopy(self.ask);ask['params']['questions'][0]['choices']=['가'*70+'A','가'*70+'B']
        self.create(ask);row=b.read_form(self.fid,self.state)
        labels=[x[0]['text'] for x in b.keyboard(row,0)['inline_keyboard'][:2]]
        self.assertTrue(labels[0].startswith('1) '));self.assertTrue(labels[1].startswith('2) '))
        ask['params']['questions'][0]['choices']=['**동일**','동일'];self.d['fingerprint']=b.canonical(ask)
        with self.assertRaisesRegex(ValueError,'indistinguishable'):b.register({'descriptor':self.d,'ask':ask},state=self.state,now=self.now)
    def test_claim_has_deadline(self):
        self.create();self.take();item=self.claim()['items'][0]
        self.assertEqual(item['deadline'],self.now+b.LEASE)
    def test_concurrent_partial_lock_policy_full_submission_wins(self):
        from tui_gateway import server_requests as sr
        sr.bind_sinks(lambda f:None,lambda *a:None,lambda sid:True)
        qs=[{'qid':q,'question':q,'choices':['A','B'],'multi_select':False} for q in ('q1','q2')]
        req=sr.ServerRequest('race','clarify',{'questions':qs},qids=['q1','q2']);sr._register(req)
        self.addCleanup(sr.cancel,'race')
        sr.lock_answer(req.id,'q1','local-draft')
        sr.resolve_response({'jsonrpc':'2.0','id':req.id,'result':{'answers':{'q1':'remote','q2':'remote'}}})
        self.assertEqual(req.result['answers'],{'q1':'remote','q2':'remote'})
        # Partial locks are editable drafts. First complete request settlement
        # wins; no claim of atomic preservation of a racing partial draft.
    def test_real_ptb_dispatch_stale_content_never_reaches_agent(self):
        self.create();c=b.connect(self.state)
        with c:b._prune(c,self.now+90000)
        c.close()
        async def scenario():
            import notifier
            from telegram import Update
            from telegram.ext import Application,ExtBot,MessageHandler,filters
            from telegram.request import BaseRequest
            class Request(BaseRequest):
                @property
                def read_timeout(self):return 1
                async def initialize(self):pass
                async def shutdown(self):pass
                async def do_request(self,url,method,request_data=None,**kwargs):
                    if url.endswith('getMe'):result={'id':77,'is_bot':True,'first_name':'test','username':'test_bot'}
                    else:result={'message_id':900,'date':1,'chat':{'id':99,'type':'private'},'text':'reply'}
                    return 200,json.dumps({'ok':True,'result':result}).encode()
            app=Application.builder().bot(ExtBot('77:test',request=Request())).build()
            await app.initialize();tasks=set(asyncio.all_tasks());core=[];errors=[]
            async def ordinary(update,context):core.append(update.update_id)
            async def error(update,context):errors.append(type(context.error).__name__)
            app.add_error_handler(error)
            lookup=b.lookup_reply;take=b.intake
            with patch.object(notifier,'credentials',return_value=('77:test',99,None)),patch.object(b.secrets,'token_hex',return_value='boot'),patch.object(b,'enabled',return_value=True),patch.object(b,'lookup_reply',side_effect=lambda bot,chat,mid:lookup(bot,chat,mid,self.state)),patch.object(b,'intake',side_effect=lambda *a:take(*a,state=self.state,now=self.now)),patch.object(b,'flush'):
                b.wire(app,None);app.add_handler(MessageHandler(filters.ALL,ordinary),group=0)
                base={'message_id':200,'date':1,'chat':{'id':99,'type':'private'},'from':{'id':99,'is_bot':False,'first_name':'owner'},'reply_to_message':{'message_id':100,'date':1,'chat':{'id':99,'type':'private'},'from':{'id':77,'is_bot':True,'first_name':'test'}}}
                for i,kind in enumerate(('text','command','photo','edited')):
                    msg=copy.deepcopy(base)
                    if kind=='photo':msg['photo']=[{'file_id':'f','file_unique_id':'u','width':1,'height':1}]
                    else:msg['text']='/dangerous' if kind=='command' else 'stale answer'
                    if kind=='command':msg['entities']=[{'type':'bot_command','offset':0,'length':10}]
                    update=Update.de_json({'update_id':i+1,'edited_message' if kind=='edited' else 'message':msg},app.bot)
                    await app.process_update(update)
                msg=copy.deepcopy(base);msg['reply_to_message']['message_id']=999;msg['text']='ordinary'
                await app.process_update(Update.de_json({'update_id':9,'message':msg},app.bot))
                self.assertEqual(core,[9]);self.assertEqual(errors,[])
            for task in set(asyncio.all_tasks())-tasks:task.cancel()
            await asyncio.gather(*(set(asyncio.all_tasks())-tasks),return_exceptions=True)
            await app.shutdown()
        asyncio.run(scenario())
    def test_native_dispatch_scope(self):
        self.create()
        async def scenario():
            import notifier
            from telegram.ext import ApplicationHandlerStop
            handlers=[]
            app=SimpleNamespace(bot=SimpleNamespace(token='77:test'),running=False,add_handler=lambda h,group:handlers.append(h))
            tasks=set(asyncio.all_tasks())
            lookup=b.lookup_reply;intake=b.intake
            with patch.object(notifier,'credentials',return_value=('77:test',99,None)),patch.object(b.secrets,'token_hex',return_value='boot'),patch.object(b,'enabled',return_value=True),patch.object(b,'lookup_reply',side_effect=lambda bot,chat,mid:lookup(bot,chat,mid,self.state)),patch.object(b,'intake',side_effect=lambda *a:intake(*a,state=self.state,now=self.now)),patch.object(b,'flush'):
                b.wire(app,None)
                self.assertEqual(len(handlers),2)
                ordinary=SimpleNamespace(reply_to_message=None)
                self.assertFalse(handlers[1].filters.filter(ordinary))
                human=SimpleNamespace(id=99,is_bot=False);bot=SimpleNamespace(id=77,is_bot=True)
                msg=SimpleNamespace(chat=SimpleNamespace(id=99,type='private'),message_id=100,from_user=bot,message_thread_id=None,forward_origin=None)
                query=SimpleNamespace(id='callback-native',data=f'hdq:{self.fid}:0:c:1',message=msg,from_user=human,answer=AsyncMock())
                with self.assertRaises(ApplicationHandlerStop):await handlers[0].callback(SimpleNamespace(callback_query=query),None)
                self.assertEqual(b.read_form(self.fid,self.state)['status'],'queued')
                known=SimpleNamespace(chat=msg.chat,reply_to_message=SimpleNamespace(message_id=100))
                self.assertTrue(handlers[1].filters.filter(known))
            for task in set(asyncio.all_tasks())-tasks:task.cancel()
            await asyncio.gather(*(set(asyncio.all_tasks())-tasks),return_exceptions=True)
        asyncio.run(scenario())

class TtlPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=test_support.temporary_directory(prefix='reply-test-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
    def write_profile(self,name,text):
        home=self.root if name=='default' else self.root/'profiles'/name
        home.mkdir(parents=True,exist_ok=True)
        (home/'config.yaml').write_text(text,encoding='utf-8')
    def test_follows_profile_clarify_timeout(self):
        self.write_profile('alice','agent:\n  clarify_timeout: 7200\n')
        self.assertEqual(b.form_ttl('alice',root=self.root),7200)
    def test_legacy_clarify_timeout_wins(self):
        self.write_profile('alice','clarify:\n  timeout: 5400\nagent:\n  clarify_timeout: 7200\n')
        self.assertEqual(b.form_ttl('alice',root=self.root),5400)
    def test_floors_at_one_hour(self):
        self.write_profile('alice','agent:\n  clarify_timeout: 300\n')
        self.assertEqual(b.form_ttl('alice',root=self.root),b.MIN_TTL)
    def test_unlimited_and_oversized_capped_at_one_day(self):
        for value in (0,-1,900000,'junk'):
            with self.subTest(value=value):
                self.write_profile('alice',f'agent:\n  clarify_timeout: {value}\n')
                expected=b.MAX_TTL if value!='junk' else b.DEFAULT_TTL
                self.assertEqual(b.form_ttl('alice',root=self.root),expected)
    def test_missing_profile_or_config_defaults_to_one_hour(self):
        self.write_profile('alice','agent:\n  clarify_timeout: 7200\n')
        self.assertEqual(b.form_ttl('ghost',root=self.root),b.DEFAULT_TTL)
        self.write_profile('alice','')
        self.assertEqual(b.form_ttl('alice',root=self.root),b.DEFAULT_TTL)
    def test_resolver_matches_core_clarify_timeout(self):
        from tools.clarify_gateway import resolve_clarify_timeout
        for cfg in ({},{'agent':{'clarify_timeout':7200}},{'clarify':{'timeout':60},'agent':{'clarify_timeout':7200}},
                    {'agent':{'clarify_timeout':'bad'}},{'agent':{'clarify_timeout':0}}):
            with self.subTest(cfg=cfg):
                self.assertEqual(b._resolve_clarify_timeout(cfg),resolve_clarify_timeout(cfg))
    def test_expiry_label(self):
        self.assertEqual(b.expiry_label(300),'5분')
        self.assertEqual(b.expiry_label(3600),'1시간')
        self.assertEqual(b.expiry_label(5400),'1시간 30분')
        self.assertEqual(b.expiry_label(86400),'24시간')
        self.assertEqual(b.expiry_label(45),'45초')

if __name__=='__main__':unittest.main(verbosity=2)
