"""Security contract tests for the separate native-command approval inbox."""
from __future__ import annotations
import asyncio, copy, json, time, unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import approval_bridge as b
import notifier
import test_support

class ApprovalBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=test_support.temporary_directory(prefix='approval-bridge-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'home';self.root.mkdir()
        (self.root/'config.yaml').write_text('plugins:\n  enabled:\n    - desktop-clarify-telegram\napprovals:\n  timeout: 300\n',encoding='utf-8')
        self.state=self.root/'plugin-data'/'session-complete-telegram';self.now=time.time();self.owner='desktop-owner-fixture'
        self.route={'mode':'local','connectionId':'local-connection','profile':'default','targetProfile':'default'}
        self.session={'id':'runtime-fixture','session_key':'durable-fixture','status':'waiting'}
        self.request,self.pending=self.pair();self.request_fp=b._digest(self.request);self.pending_fp=b._digest(self.pending)
        self.descriptor=self.descriptor_for()
        self.calls=[];self.mid=800
        b.bind_receiver(77,99,None,'boot-fixture',private_verified=True,state=self.state,now=self.now)
        b.owner_heartbeat(self.owner,state=self.state,now=self.now)
        self.cred=patch.object(notifier,'credentials',side_effect=lambda *a,**k:('77:synthetic-token',99,None));self.cred.start()
        self.net=patch.object(notifier,'telegram',side_effect=self.telegram);self.net.start()
        self.addCleanup(self.net.stop);self.addCleanup(self.cred.stop)

    @staticmethod
    def pair(command='rm -rf approval-fixture',description='recursive delete',key='recursive delete',
             queue_id='a'*32,srq='srq-aabbcc112233',method='approval',sid='runtime-fixture',allow=True,smart=None):
        pending={'request_id':queue_id,'command':command,'description':description,'pattern_key':key,
                 'pattern_keys':[key],'allow_permanent':allow,'allow_session':True}
        params={'session_id':sid,'request_id':queue_id,'command':command,'description':description,
                'pattern_key':key,'pattern_keys':[key],'allow_permanent':allow,'allow_session':True,
                'choices':['once','session','always','deny'] if allow else ['once','session','deny']}
        if smart is not None: pending['smart_denied']=params['smart_denied']=smart
        return {'id':srq,'method':method,'params':params},pending

    def descriptor_for(self,request=None,pending=None,owner=None,route=None,session=None,epoch='epoch-fixture'):
        request=request or self.request;pending=pending or self.pending;route=route or self.route;session=session or self.session
        return {'owner':owner or self.owner,'connectionId':route['connectionId'],'profile':route['profile'],
            'targetProfile':route['targetProfile'],'sid':session['id'],'session_key':session['session_key'],
            'epoch':str(epoch),'request_id':request['id'],'queue_id':pending['request_id'],
            'request_fingerprint':b._digest(request),'pending_fingerprint':b._digest(pending)}

    def telegram(self,token,method,payload):
        self.calls.append((method,copy.deepcopy(payload)))
        if method=='sendMessage':
            self.mid+=1
            return {'ok':True,'result':{'message_id':self.mid,'chat':{'id':payload['chat_id'],'type':'private'},
                'text':payload['text'],'message_thread_id':payload.get('message_thread_id'),
                'from':{'id':77,'is_bot':True},'reply_markup':payload.get('reply_markup')}}
        if method=='editMessageText': return {'ok':True,'result':{'message_id':payload['message_id'],'chat':{'id':99,'type':'private'}}}
        return {'ok':False,'error_code':400,'description':'not modified'}

    def register(self,**overrides):
        req={'action':'approval_register','capability':b.CAPABILITY,'owner':self.owner,'descriptor':self.descriptor,
            'request':self.request,'pending':self.pending,'route':self.route,'session':self.session,'epoch':'epoch-fixture'}
        req.update(overrides)
        return b.register(req,notifier,root=self.root,state=self.state,now=self.now)

    def row(self,fid):
        c=b.connect(self.state)
        try:
            value=c.execute('SELECT * FROM approvals WHERE id=?',(fid,)).fetchone()
            return dict(value) if value else None
        finally:c.close()

    def identity(self,fid,**overrides):
        row=self.row(fid)
        ident={'bot':row['bot'],'boot':row['boot'],'chat':row['chat'],'user':row['chat'],'topic':row['topic'],
            'mid':row['message_id'],'private':True,'human':True,'bot_author':True,'forwarded':False,
            'edited':False,'inline':False,'inaccessible':False,'message_text':row['review_text'],'event':'event-default'}
        ident.update(overrides);return ident

    def observe(self,fid):
        row=self.row(fid);d=json.loads(row['descriptor'])
        return {'id':fid,'request_fingerprint':row['request_fingerprint'],
                'pending_fingerprint':row['pending_fingerprint'],'authority':b._digest(d)}

    def receipts(self):
        c=b.connect(self.state)
        try:return c.execute('SELECT count(*) FROM receipts').fetchone()[0]
        finally:c.close()

    def registered(self):
        result=self.register();self.assertTrue(result.get('ok'),result);self.assertEqual(result['status'],'open')
        self.now=time.time()+.01
        return result

    def test_only_real_pinned_command_gate_pair_is_supported(self):
        from tools.approval_detection import detect_dangerous_command
        self.assertEqual(detect_dangerous_command('rm -rf approval-fixture'),(True,'recursive delete','recursive delete'))
        self.assertTrue(b.supported_pair(self.request,self.pending,self.session['id']))
        for key in ('plugin_rule:rule','execute_code','tirith:secret'):
            ask,pending=self.pair(key=key);self.assertFalse(b.supported_pair(ask,pending,self.session['id']))
        ask,pending=self.pair(method='sudo');self.assertFalse(b.supported_pair(ask,pending,self.session['id']))
        ask=copy.deepcopy(self.request);ask['params']['extra']='x';self.assertFalse(b.supported_pair(ask,self.pending,self.session['id']))

    def test_queue_and_open_request_frozen_mismatches_fail_closed(self):
        mutations=[]
        ask=copy.deepcopy(self.request);ask['id']='srq-invalid';mutations.append((ask,self.pending))
        ask=copy.deepcopy(self.request);ask['params']['session_id']='other';mutations.append((ask,self.pending))
        ask=copy.deepcopy(self.request);ask['params']['request_id']='b'*32;mutations.append((ask,self.pending))
        ask=copy.deepcopy(self.request);ask['params']['command']+=' changed';mutations.append((ask,self.pending))
        pending=copy.deepcopy(self.pending);pending['pattern_keys']=['other'];mutations.append((self.request,pending))
        pending=copy.deepcopy(self.pending);pending['allow_session']=False;mutations.append((self.request,pending))
        pending=copy.deepcopy(self.pending);pending['smart_denied']=True;mutations.append((self.request,pending))
        pending=copy.deepcopy(self.pending);pending['tool_name']='execute_code';mutations.append((self.request,pending))
        for ask,pending in mutations:self.assertFalse(b.supported_pair(ask,pending,self.session['id']))

    def test_full_authority_matrix_including_fingerprints(self):
        self.assertTrue(b.validate_snapshot(self.descriptor,self.request,self.pending,self.route,self.session,'epoch-fixture',self.owner))
        fields={'owner':'other','connectionId':'other','profile':'other','targetProfile':'other','sid':'other',
                'session_key':'other','epoch':'other','request_id':'srq-ffeeddccbbaa','queue_id':'f'*32,
                'request_fingerprint':'0'*64,'pending_fingerprint':'1'*64}
        for key,value in fields.items():
            d=dict(self.descriptor);d[key]=value
            self.assertFalse(b.validate_snapshot(d,self.request,self.pending,self.route,self.session,'epoch-fixture',self.owner),key)
        for key in ('connectionId','profile','targetProfile'):
            route=dict(self.route);route[key]='changed'
            self.assertFalse(b.validate_snapshot(self.descriptor,self.request,self.pending,route,self.session,'epoch-fixture',self.owner),key)
        for key,value in (('id','changed'),('session_key','changed'),('status','working')):
            row=dict(self.session);row[key]=value
            self.assertFalse(b.validate_snapshot(self.descriptor,self.request,self.pending,self.route,row,'epoch-fixture',self.owner),key)
        self.assertFalse(b.validate_snapshot(self.descriptor,self.request,self.pending,self.route,self.session,'changed',self.owner))
        self.assertFalse(b.validate_snapshot(self.descriptor,self.request,self.pending,self.route,self.session,'epoch-fixture','changed'))

    def test_full_literal_redacted_review_unsafe_unicode_controls_and_oversize(self):
        ask,_=self.pair(command='rm -rf "project **/cache.py" _name_',description='recursive delete **literal**')
        text=b.review_card(ask,300)
        self.assertIn('rm -rf "project **/cache.py" _name_',text);self.assertIn('recursive delete **literal**',text)
        self.assertIn('명령 (전체)',text);self.assertLessEqual(len(text.encode('utf-16-le'))//2,4096)
        token='sk-'+('A1b2C3d4'*8)
        ask,_=self.pair(command=f'rm -rf approval-fixture && echo {token}')
        self.assertNotIn(token,b.review_card(ask,300))
        ask,_=self.pair(command='rm -rf approval-fixture && echo password=synthetic-secret-value-739102')
        with self.assertRaisesRegex(ValueError,'redaction_omission'):b.review_card(ask,300)
        prefix='A1b2C3d4'*2;suffix='9Z8y7X6w'*2
        left='rm -rf x && sk-'+prefix+'AAAA1234BBBB5678'+suffix
        right='rm -rf x && sk-'+prefix+'CCCC9876DDDD5432'+suffix
        ask,_=self.pair(command=left,description=right)
        with self.assertRaisesRegex(ValueError,'redaction_collision'):b.review_card(ask,300)
        ask,_=self.pair(command='rm -rf '+'x'*5000)
        with self.assertRaisesRegex(ValueError,'review_card_too_large'):b.review_card(ask,300)
        ask,_=self.pair(command='rm -rf x\u202eabc')
        with self.assertRaisesRegex(ValueError,'review_control_character'):b.review_card(ask,300)
        ask,_=self.pair(description='e\u0301 recursive delete')
        with self.assertRaisesRegex(ValueError,'review_unicode_ambiguous'):b.review_card(ask,300)
        ask,_=self.pair(command='rm -rf x\x01')
        with self.assertRaisesRegex(ValueError,'review_control_character'):b.review_card(ask,300)

    def test_registration_sends_once_protect_content_false_and_opaque_buttons(self):
        result=self.registered();row=self.row(result['id'])
        self.assertEqual(row['status'],'open');self.assertEqual(row['request_fingerprint'],self.request_fp)
        sends=[p for method,p in self.calls if method=='sendMessage'];self.assertEqual(len(sends),1)
        self.assertIs(sends[0]['protect_content'],False);self.assertNotIn('parse_mode',sends[0])
        self.assertEqual(sends[0]['text'],row['review_text'])
        keyboard=sends[0]['reply_markup']['inline_keyboard']
        self.assertEqual([[x['text'] for x in line] for line in keyboard],[['Allow Once','Deny']])
        for button in keyboard[0]:
            data=button['callback_data'];self.assertLessEqual(len(data.encode()),64)
            self.assertRegex(data,r'^hda:[A-Za-z0-9_-]+:[od]$')
            for secret in (self.session['id'],self.session['session_key'],self.request['id'],self.pending['request_id'],self.request['params']['command']):
                self.assertNotIn(secret,data)
        self.assertEqual([x[0] for x in self.calls],['sendMessage','editMessageText'])
        self.assertEqual(self.calls[1][1]['reply_markup'],sends[0]['reply_markup'])

    def test_unsafe_review_gets_notification_only_guidance_and_never_action_buttons(self):
        command='rm -rf approval-fixture && echo password=synthetic-secret-value-739102'
        self.request,self.pending=self.pair(command=command)
        self.request_fp=b._digest(self.request);self.pending_fp=b._digest(self.pending);self.descriptor=self.descriptor_for()
        rejected=self.register()
        self.assertFalse(rejected['ok']);self.assertEqual(rejected['reason'],'redaction_omission')
        self.assertFalse(any(method=='sendMessage' for method,_ in self.calls))
        notice=notifier.dispatch_request({'action':'approval_notice','capability':b.CAPABILITY,'owner':self.owner,
            'descriptor':self.descriptor,'request':self.request,'pending':self.pending,'reason':'redaction_omission'},
            root=self.root,state=self.state,native=True,result_budget=24*1024)
        self.assertTrue(notice['ok']);self.assertTrue(notice['notification_only'])
        payload=next(payload for method,payload in self.calls if method=='sendMessage')
        self.assertIs(payload['protect_content'],False);self.assertNotIn(command,payload['text'])
        self.assertIn('Desktop에서',payload['text']);self.assertNotIn('reply_markup',payload)

    def test_actual_timeout_is_used_without_clarify_ttl_floor(self):
        (self.root/'config.yaml').write_text('approvals:\n  timeout: 23\n',encoding='utf-8')
        result=self.registered();self.assertLessEqual(result['expires']-self.now,23.01)
        self.assertLess(result['expires']-self.now,3600)
        (self.root/'config.yaml').write_text('approvals:\n  timeout: 0\n',encoding='utf-8')
        fresh=self.register();self.assertEqual(fresh['reason'],'approval_expired')

    def test_once_and_deny_queue_exact_id_one_shot_and_all_false_not_inherited(self):
        for index,choice in enumerate(('once','deny')):
            with self.subTest(choice=choice):
                if index:
                    self.request,self.pending=self.pair(queue_id='b'*32,srq='srq-112233aabbcc')
                    self.request_fp=b._digest(self.request);self.pending_fp=b._digest(self.pending)
                    self.descriptor=self.descriptor_for()
                result=self.registered();row=self.row(result['id']);data=b.PREFIX+row['token']+(':'+('o' if choice=='once' else 'd'))
                self.assertLessEqual(len(data.encode()),64)
                identity=self.identity(result['id'],event='tap-'+choice)
                accepted=b.choose(row['token'],choice,identity,state=self.state,now=self.now+.1)
                self.assertTrue(accepted.get('ok'),accepted);self.assertEqual(accepted.get('status'),'queued')
                self.assertFalse(b.choose(row['token'],'deny' if choice=='once' else 'once',
                    {**identity,'event':'second-'+choice},state=self.state,now=self.now+.2)['ok'])
                self.assertEqual(self.receipts(),index+1)
                polled=b.poll(self.owner,[self.observe(result['id'])],state=self.state,now=self.now+.3,result_budget=24*1024)
                item=polled['items'][0];self.assertEqual(item['choice'],choice)
                self.assertEqual(item['descriptor']['request_id'],self.request['id'])
                self.assertEqual(item['descriptor']['queue_id'],self.pending['request_id'])
                self.assertEqual(b.poll(self.owner,[self.observe(result['id'])],state=self.state,now=self.now+.4)['items'],[])
                self.assertTrue(b.settle(result['id'],item['claim'],'applied',state=self.state,now=self.now+.5)['ok'])
                self.assertEqual(self.row(result['id'])['status'],'applied')

    def test_rejected_taps_have_no_receipt_or_row_mutation_for_authority_matrix(self):
        result=self.registered();before=self.row(result['id']);base=self.identity(result['id'])
        cases={'bot':{'bot':78},'chat':{'chat':98},'user':{'user':98},'topic':{'topic':5},'message':{'mid':999},
          'private':{'private':False},'human':{'human':False},'author':{'bot_author':False},'forwarded':{'forwarded':True},
          'edited':{'edited':True},'inline':{'inline':True},'inaccessible':{'inaccessible':True},
          'text':{'message_text':before['review_text']+' altered'},'boot':{'boot':'other'}}
        for label,changes in cases.items():
            with self.subTest(label=label):
                self.assertFalse(b.choose(before['token'],'once',{**base,**changes,'event':'bad-'+label},
                                          state=self.state,now=self.now+.1)['ok'])
                self.assertEqual(self.receipts(),0);after=self.row(result['id'])
                self.assertEqual((after['status'],after['choice'],after['seen']),(before['status'],before['choice'],before['seen']))

    def test_expired_stale_and_changed_snapshot_taps_are_not_consumed_or_replayed(self):
        result=self.registered();row=self.row(result['id'])
        self.assertFalse(b.choose(row['token'],'once',self.identity(result['id']),state=self.state,now=result['expires']+1)['ok'])
        self.assertEqual(self.receipts(),0)
        self.assertFalse(b.choose(row['token'],'once',{**self.identity(result['id']),'event':'stale'},state=self.state,now=self.now+b.LEASE+1)['ok'])
        self.assertEqual(self.receipts(),0)
        self.assertTrue(b.choose(row['token'],'once',{**self.identity(result['id']),'event':'click'},state=self.state,now=self.now+.1)['ok'])
        observed=self.observe(result['id']);observed['pending_fingerprint']='f'*64
        result_poll=b.poll(self.owner,[observed],state=self.state,now=self.now+.2)
        self.assertEqual(result_poll['items'],[]);self.assertEqual(self.row(result['id'])['status'],'rejected')
        self.assertEqual(b.poll(self.owner,[self.observe(result['id'])],state=self.state,now=self.now+.3)['items'],[])

    def test_posting_crash_and_applying_crash_are_terminal_no_replay(self):
        with patch.object(b,'publish',side_effect=RuntimeError('synthetic posting crash')):
            with self.assertRaises(RuntimeError):self.register()
        c=b.connect(self.state)
        try:approval_id=c.execute('SELECT id FROM approvals').fetchone()[0]
        finally:c.close()
        duplicate=self.register();self.assertTrue(duplicate['duplicate']);self.assertEqual(duplicate['status'],'posting')
        self.assertEqual(self.calls,[])
        self.assertIn(approval_id,b.poll(self.owner,[],absent=[approval_id],state=self.state,now=self.now+.1)['terminal'])
        self.assertEqual(self.row(approval_id)['status'],'unknown')

        self.request,self.pending=self.pair(queue_id='b'*32,srq='srq-112233aabbcc')
        self.request_fp=b._digest(self.request);self.pending_fp=b._digest(self.pending);self.descriptor=self.descriptor_for()
        result=self.registered();row=self.row(result['id'])
        applied_choice=b.choose(row['token'],'deny',{**self.identity(result['id']),'event':'apply'},state=self.state,now=self.now+.1)
        self.assertTrue(applied_choice.get('ok'),applied_choice)
        polled=b.poll(self.owner,[self.observe(result['id'])],state=self.state,now=self.now+.2)
        self.assertTrue(polled['items'],polled)
        item=polled['items'][0]
        self.assertEqual(self.row(result['id'])['status'],'applying')
        self.assertEqual(b.poll(self.owner,[self.observe(result['id'])],state=self.state,now=self.now+b.LEASE+1)['items'],[])
        self.assertEqual(self.row(result['id'])['status'],'unknown')

    def test_new_receiver_and_new_owner_cannot_re_adopt_old_card(self):
        result=self.registered()
        b.bind_receiver(77,99,None,'new-boot',private_verified=True,state=self.state,now=self.now+1)
        self.assertEqual(self.row(result['id'])['status'],'expired')
        new_owner='new-desktop-owner';b.owner_heartbeat(new_owner,state=self.state,now=self.now+1)
        self.now+=2;descriptor=dict(self.descriptor,owner=new_owner)
        duplicate=self.register(owner=new_owner,descriptor=descriptor)
        self.assertEqual(duplicate['reason'],'prior_owner')

    def test_ambiguous_send_never_retries(self):
        calls=[]
        with patch.object(notifier,'telegram',side_effect=lambda t,m,p:(calls.append(m) or {'ok':False,'ambiguous':True,'error_code':0})):
            result=self.register();duplicate=self.register()
        self.assertEqual(result['status'],'unknown');self.assertTrue(duplicate['duplicate']);self.assertEqual(calls,['sendMessage'])

    def test_native_dispatcher_has_explicit_approval_actions_and_bounded_claim(self):
        heartbeat=notifier.dispatch_request({'action':'approval_owner_heartbeat','capability':b.CAPABILITY,'owner':self.owner},
            root=self.root,state=self.state,native=True,result_budget=24*1024)
        self.assertTrue(heartbeat['feature_ready'])
        registered=notifier.dispatch_request({'action':'approval_register','capability':b.CAPABILITY,'owner':self.owner,
            'descriptor':self.descriptor,'request':self.request,'pending':self.pending,'route':self.route,
            'session':self.session,'epoch':'epoch-fixture'},root=self.root,state=self.state,native=True,result_budget=24*1024)
        self.assertTrue(registered['ok'],registered);row=self.row(registered['id']);self.now=time.time()+.01
        b.choose(row['token'],'once',{**self.identity(registered['id']),'event':'dispatch'},state=self.state,now=self.now+.1)
        poll=notifier.dispatch_request({'action':'approval_poll','capability':b.CAPABILITY,'owner':self.owner,
            'observations':[self.observe(registered['id'])],'unverified':[],'absent':[]},root=self.root,
            state=self.state,native=True,result_budget=24*1024)
        self.assertTrue(poll['ok'],poll);self.assertEqual(poll['items'][0]['choice'],'once')
        self.assertLessEqual(len(json.dumps(poll,ensure_ascii=False).encode()),24*1024)
        self.assertEqual(notifier.dispatch_request({'action':'approval_always'},root=self.root,state=self.state)['reason'],'unknown_action')

    def test_real_pinned_queue_first_settlement_and_coalesced_once_follower(self):
        import threading
        from tools import approval, approval_gateway_wait
        session='approval-test-no-command-exec-'+'a'*12;queue_id='c'*32
        entry=approval_gateway_wait._ApprovalEntry({'request_id':queue_id,'command':'rm -rf /tmp/test',
            'pattern_key':'recursive delete','pattern_keys':['recursive delete'],'description':'recursive delete'})
        with approval._lock:approval._gateway_queues[session]=[entry]
        try:
            self.assertEqual(approval.resolve_gateway_approval(session,'once',request_id=queue_id),1)
            self.assertEqual(approval.resolve_gateway_approval(session,'deny',request_id=queue_id),0)
            self.assertEqual(entry.result,'once');self.assertTrue(entry.event.is_set())
            follower=type('Entry',(),{})();follower.event=threading.Event();follower.result='once';follower.reason=None;follower.cancelled=None;follower.event.set()
            payload={'command':'rm -rf /tmp/test','description':'recursive delete','pattern_key':'recursive delete',
                'pattern_keys':['recursive delete'],'session_key':session,'surface':'gateway'}
            self.assertIsNone(approval_gateway_wait._await_coalesced_leader(session,follower,payload))
        finally:
            with approval._lock:approval._gateway_queues.pop(session,None)

    def test_real_pinned_queue_first_settlement_supports_deny_without_execution(self):
        from tools import approval, approval_gateway_wait
        session='approval-test-deny-no-command-exec-'+'b'*12;queue_id='d'*32
        entry=approval_gateway_wait._ApprovalEntry({'request_id':queue_id,'command':'rm -rf approval-fixture',
            'pattern_key':'recursive delete','pattern_keys':['recursive delete'],'description':'recursive delete'})
        with approval._lock:approval._gateway_queues[session]=[entry]
        try:
            self.assertEqual(approval.resolve_gateway_approval(session,'deny',request_id=queue_id),1)
            self.assertEqual(approval.resolve_gateway_approval(session,'once',request_id=queue_id),0)
            self.assertEqual(entry.result,'deny')
            self.assertTrue(entry.event.is_set())
        finally:
            with approval._lock:approval._gateway_queues.pop(session,None)

    def test_real_ptb_application_process_update_handles_callback_and_text_no_fallthrough(self):
        from telegram import Update
        from telegram.ext import Application, TypeHandler
        async def run():
            result=self.registered();row=self.row(result['id'])
            callback_data=self.calls[0][1]['reply_markup']['inline_keyboard'][0][0]['callback_data']
            app=Application.builder().token('77:synthetic-token').build()
            with patch.object(type(app.bot),'initialize',new=AsyncMock()), patch.object(type(app.bot),'shutdown',new=AsyncMock()):
                await app.initialize()
                self.assertTrue(b.wire(app,notifier,77,99,None,'boot-fixture',enabled=lambda:True,state=self.state))
                later=[]
                async def downstream(update,context):later.append(update)
                app.add_handler(TypeHandler(Update,downstream),group=1)
                update=Update.de_json({'update_id':1,'callback_query':{'id':'query-fixture',
                'from':{'id':99,'is_bot':False,'first_name':'owner'},'chat_instance':'fixture','data':callback_data,
                'message':{'message_id':row['message_id'],'date':1,'chat':{'id':99,'type':'private'},
                    'from':{'id':77,'is_bot':True,'first_name':'bot'},'text':row['review_text']}}},app.bot)
                row_now=self.row(result['id']);now=time.time();c=b.connect(self.state)
                try:
                    live=b._receiver(c,now);owner_live=b._owner_live(c,self.owner,now)
                finally:c.close()
                self.assertTrue(row_now['expires']>now and now>=row_now['seen'] and now-row_now['seen']<=b.LEASE,
                                repr({k:row_now[k] for k in ('status','seen','expires')}))
                self.assertTrue(live and live['boot']=='boot-fixture' and owner_live,repr((live,owner_live)))
                answer=AsyncMock();choice_results=[];real_choose=b.choose
                def capture_choice(*args,**kwargs):
                    result=real_choose(*args,**kwargs);choice_results.append(result);return result
                with patch.object(b,'choose',side_effect=capture_choice) as chooser:
                    with patch.object(type(update.callback_query),'answer',new=answer):
                        await app.process_update(update)
                identity=chooser.call_args.args[2]
                self.assertTrue(b._message_identity_ok(self.row(result['id']),identity),repr(identity))
                actual=self.row(result['id'])
                self.assertEqual(actual['status'],'queued')
                self.assertEqual(chooser.call_args.args[0],actual['token'])
                self.assertEqual(chooser.call_args.args[1],'once')
                self.assertEqual(choice_results,[{'ok':True,'status':'queued','id':result['id']}])
                self.assertGreater(answer.await_count,0)
                self.assertEqual(answer.await_args.args[0],'응답을 받았습니다.')
                self.assertEqual(later,[])
                self.assertEqual(self.receipts(),1)
                await app.shutdown()
        asyncio.run(run())

    def test_free_text_approval_card_reply_consumed_even_when_store_fails_other_card_not(self):
        from telegram import Update
        from telegram.ext import Application, TypeHandler
        async def run():
            result=self.registered();row=self.row(result['id'])
            app=Application.builder().token('77:synthetic-token').build()
            with patch.object(type(app.bot),'initialize',new=AsyncMock()), patch.object(type(app.bot),'shutdown',new=AsyncMock()):
                await app.initialize()
                self.assertTrue(b.wire(app,notifier,77,99,None,'boot-fixture',enabled=lambda:True,state=self.state))
                later=[]
                async def downstream(update,context):later.append(update)
                app.add_handler(TypeHandler(Update,downstream),group=1)
                free=Update.de_json({'update_id':2,'message':{'message_id':901,'date':1,
                    'chat':{'id':99,'type':'private'},'from':{'id':99,'is_bot':False,'first_name':'owner'},
                    'text':'free text','reply_to_message':{'message_id':row['message_id'],'date':1,
                      'chat':{'id':99,'type':'private'},'from':{'id':77,'is_bot':True,'first_name':'bot'},
                      'text':row['review_text']}}},app.bot)
                reply_text=AsyncMock()
                with patch.object(type(free.message),'reply_text',new=reply_text):
                    with patch.object(b,'lookup_message',side_effect=OSError('private inbox unavailable')):
                        await app.process_update(free)
                self.assertGreater(reply_text.await_count,0);self.assertEqual(later,[])
                other=Update.de_json({'update_id':3,'message':{'message_id':902,'date':1,
                    'chat':{'id':99,'type':'private'},'from':{'id':99,'is_bot':False,'first_name':'owner'},
                    'text':'follow-up','reply_to_message':{'message_id':700,'date':1,
                      'chat':{'id':99,'type':'private'},'from':{'id':77,'is_bot':True,'first_name':'bot'},
                      'text':'✅ Hermes Desktop 세션 작업 종료'}}},app.bot)
                with patch.object(b,'lookup_message',side_effect=OSError('private inbox unavailable')):
                    await app.process_update(other)
                self.assertEqual(len(later),1);self.assertEqual(self.row(result['id'])['status'],'open')
                await app.shutdown()
        asyncio.run(run())

    def test_native_claim_validity_is_read_only_and_checks_live_authority(self):
        result=self.registered();row=self.row(result['id'])
        self.assertTrue(b.choose(row['token'],'once',{**self.identity(result['id']),'event':'claim-gate'},
                                state=self.state,now=self.now+.1)['ok'])
        observation=self.observe(result['id'])
        polled=notifier.dispatch_request({'action':'approval_poll','capability':b.CAPABILITY,'owner':self.owner,
            'observations':[observation],'unverified':[],'absent':[]},root=self.root,state=self.state,
            native=True,result_budget=24*1024)
        self.assertTrue(polled['ok'],polled);self.assertEqual(len(polled['items']),1)
        item=polled['items'][0];before=self.row(item['id'])
        request={'action':'approval_claim_valid','capability':b.CAPABILITY,'owner':self.owner,
            'id':item['id'],'claim':item['claim'],'descriptor':item['descriptor'],
            'authority':b._digest(item['descriptor']),'deadline':item['deadline']}
        valid=notifier.dispatch_request(request,root=self.root,state=self.state,native=True,result_budget=24*1024)
        self.assertEqual(valid,{'ok':True,'valid':True,'id':item['id'],'claim':item['claim'],
            'authority':b._digest(item['descriptor']),'deadline':item['deadline']})
        self.assertEqual(self.row(item['id']),before,'read gate must not renew or mutate the claim')
        for label,changes in (
            ('owner',{'owner':'different-owner'}),('id',{'id':'f'*32}),
            ('claim',{'claim':'0'*32}),('authority',{'authority':'0'*64}),
            ('descriptor',{'descriptor':{**item['descriptor'],'epoch':'changed'}})):
            with self.subTest(label=label):
                denied=notifier.dispatch_request({**request,**changes},root=self.root,state=self.state,
                    native=True,result_budget=24*1024)
                self.assertIs(denied.get('valid'),False,denied)
                self.assertEqual(self.row(item['id']),before)

        c=b.connect(self.state)
        try:
            saved_live=json.loads(c.execute("SELECT value FROM receiver WHERE key='live'").fetchone()[0])
        finally:c.close()
        for label,changes in (
            ('receiver-bot',{'bot':78}),('receiver-chat',{'chat':98}),('receiver-topic',{'topic':5}),
            ('receiver-boot',{'boot':'other-boot'}),('receiver-version',{'version':'1.6.0'}),
            ('receiver-capability',{'capability':'desktop-command-approval-v1'})):
            with self.subTest(label=label):
                c=b.connect(self.state)
                try:c.execute("UPDATE receiver SET value=? WHERE key='live'",(b.canonical({**saved_live,**changes}),))
                finally:c.close()
                denied=notifier.dispatch_request(request,root=self.root,state=self.state,native=True,result_budget=24*1024)
                self.assertIs(denied.get('valid'),False,denied)
                self.assertEqual(self.row(item['id']),before)
                c=b.connect(self.state)
                try:c.execute("UPDATE receiver SET value=? WHERE key='live'",(b.canonical(saved_live),))
                finally:c.close()

        c=b.connect(self.state)
        try:c.execute('UPDATE owners SET seen=? WHERE owner=?',(time.time()-b.LEASE-1,self.owner))
        finally:c.close()
        stale_owner=notifier.dispatch_request(request,root=self.root,state=self.state,native=True,result_budget=24*1024)
        self.assertIs(stale_owner.get('valid'),False)
        c=b.connect(self.state)
        try:
            c.execute('UPDATE owners SET seen=? WHERE owner=?',(time.time(),self.owner))
            c.execute('UPDATE approvals SET deadline=? WHERE id=?',(time.time()-1,item['id']))
        finally:c.close()
        expired=notifier.dispatch_request(request,root=self.root,state=self.state,native=True,result_budget=24*1024)
        self.assertIs(expired.get('valid'),False)
        c=b.connect(self.state)
        try:c.execute('UPDATE approvals SET deadline=?,expires=? WHERE id=?',(item['deadline'],time.time()-1,item['id']))
        finally:c.close()
        absolute_expired=notifier.dispatch_request(request,root=self.root,state=self.state,native=True,result_budget=24*1024)
        self.assertIs(absolute_expired.get('valid'),False)
        self.assertEqual(self.row(item['id'])['status'],'applying','read gate must neither retire nor renew')
        self.assertEqual(self.row(item['id']),{**before,'deadline':item['deadline'],'expires':self.row(item['id'])['expires']})

    def test_claim_gate_barrier_rejects_real_sqlite_boot_change_and_ingress_disable_without_replay(self):
        import threading
        for index,cause in enumerate(('boot-change','ingress-disable')):
            if index:
                self.now=time.time()
                self.assertTrue(b.bind_receiver(77,99,None,'boot-fixture',private_verified=True,
                    state=self.state,now=self.now))
                b.owner_heartbeat(self.owner,state=self.state,now=self.now)
                self.request,self.pending=self.pair(queue_id='b'*32,srq='srq-112233aabbcc')
                self.request_fp=b._digest(self.request);self.pending_fp=b._digest(self.pending)
                self.descriptor=self.descriptor_for()
            result=self.registered();row=self.row(result['id'])
            self.assertTrue(b.choose(row['token'],'deny',{**self.identity(result['id']),'event':'barrier-'+cause},
                                     state=self.state,now=self.now+.1)['ok'])
            observation=self.observe(result['id'])
            first=notifier.dispatch_request({'action':'approval_poll','capability':b.CAPABILITY,'owner':self.owner,
                'observations':[observation],'unverified':[],'absent':[]},root=self.root,state=self.state,
                native=True,result_budget=24*1024)
            self.assertEqual(len(first['items']),1)
            item=first['items'][0]
            at_snapshot=threading.Event();resume=threading.Event();receipts=[];respond=[]
            def paused_renderer_tail():
                at_snapshot.set()
                if not resume.wait(timeout=5):raise AssertionError('claim-validity barrier was not resumed')
                receipt=notifier.dispatch_request({'action':'approval_claim_valid','capability':b.CAPABILITY,
                    'owner':self.owner,'id':item['id'],'claim':item['claim'],'descriptor':item['descriptor'],
                    'authority':b._digest(item['descriptor']),'deadline':item['deadline']},
                    root=self.root,state=self.state,native=True,result_budget=24*1024)
                receipts.append(receipt)
                if receipt.get('valid') is True:respond.append('approval.respond')
            thread=threading.Thread(target=paused_renderer_tail,daemon=True);thread.start()
            self.assertTrue(at_snapshot.wait(timeout=5))
            if cause=='boot-change':
                self.assertTrue(b.bind_receiver(77,99,None,'boot-after-claim',private_verified=True,
                    state=self.state,now=time.time()))
                self.assertEqual(self.row(item['id'])['status'],'unknown')
            else:
                (self.root/'config.yaml').write_text('plugins:\n  enabled: []\napprovals:\n  timeout: 300\n',encoding='utf-8')
                self.assertEqual(self.row(item['id'])['status'],'applying')
            resume.set();thread.join(timeout=5);self.assertFalse(thread.is_alive())
            self.assertEqual(len(receipts),1,cause);self.assertIs(receipts[0].get('valid'),False,cause)
            self.assertEqual(respond,[],cause)
            if cause=='ingress-disable':
                (self.root/'config.yaml').write_text('plugins:\n  enabled:\n    - desktop-clarify-telegram\napprovals:\n  timeout: 300\n',encoding='utf-8')
            replay=notifier.dispatch_request({'action':'approval_poll','capability':b.CAPABILITY,'owner':self.owner,
                'observations':[observation],'unverified':[],'absent':[]},root=self.root,state=self.state,
                native=True,result_budget=24*1024)
            self.assertEqual(replay.get('items'),[],cause)

    def test_exact_poll_projection_defers_unicode_claim_with_terminal_ids_without_consuming(self):
        large_command='rm -rf approval-fixture # '+'가'*3000+'🙂'*300
        self.request,self.pending=self.pair(command=large_command)
        self.request_fp=b._digest(self.request);self.pending_fp=b._digest(self.pending);self.descriptor=self.descriptor_for()
        register_req={'action':'approval_register','capability':b.CAPABILITY,'owner':self.owner,
            'descriptor':self.descriptor,'request':self.request,'pending':self.pending,'route':self.route,
            'session':self.session,'epoch':'epoch-fixture'}
        raw=json.dumps(register_req,ensure_ascii=False,separators=(',',':')).encode('utf-8')
        self.assertLessEqual(len(raw),22_500,'preserve the native request envelope')
        queued=notifier.dispatch_request(register_req,root=self.root,state=self.state,native=True,result_budget=24*1024)
        self.assertTrue(queued.get('ok'),queued);self.assertEqual(queued['status'],'open')
        queued_row=self.row(queued['id'])
        self.assertTrue(b.choose(queued_row['token'],'once',{**self.identity(queued['id']),'event':'large-unicode'},
                                 state=self.state,now=time.time()+.1)['ok'])

        second_request,second_pending=self.pair(queue_id='b'*32,srq='srq-112233aabbcc')
        second_descriptor=self.descriptor_for(second_request,second_pending)
        other=notifier.dispatch_request({'action':'approval_register','capability':b.CAPABILITY,'owner':self.owner,
            'descriptor':second_descriptor,'request':second_request,'pending':second_pending,'route':self.route,
            'session':self.session,'epoch':'epoch-fixture'},root=self.root,state=self.state,native=True,result_budget=24*1024)
        self.assertTrue(other.get('ok'),other)
        c=b.connect(self.state)
        try:
            c.execute('UPDATE approvals SET seen=? WHERE id=?',(time.time()-5,other['id']))
            terminal_ids=[f'{i+1:032x}' for i in range(200)]
            for terminal_id in terminal_ids:
                c.execute("INSERT INTO approvals(id,identity,owner,descriptor,request_json,pending_json,request_fingerprint,pending_fingerprint,review_text,token,bot,chat,topic,boot,created,expires,seen,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'applied')",
                    (terminal_id,'terminal:'+terminal_id,self.owner,'{}','{}','{}','0'*64,'1'*64,None,
                     'terminal-token:'+terminal_id,77,99,None,'boot-fixture',self.now,self.now-1,self.now))
        finally:c.close()
        observations=[self.observe(queued['id']),self.observe(other['id'])]
        stale_seen=self.row(other['id'])['seen']
        item_projection={'id':queued['id'],'claim':'f'*32,'deadline':time.time()+b.LEASE,
            'descriptor':self.descriptor,'request':self.request,'pending':self.pending,'choice':'once'}
        complete_projection={'ok':True,'items':[item_projection],'terminal':terminal_ids}
        combined_bytes=len(json.dumps(complete_projection,ensure_ascii=False,separators=(',',':')).encode('utf-8'))
        terminal_bytes=len(json.dumps({'ok':True,'items':[],'terminal':terminal_ids},ensure_ascii=False,
            separators=(',',':')).encode('utf-8'))
        self.assertGreater(combined_bytes,24*1024);self.assertLessEqual(terminal_bytes,24*1024)
        first=notifier.dispatch_request({'action':'approval_poll','capability':b.CAPABILITY,'owner':self.owner,
            'observations':observations,'unverified':terminal_ids[:100],'absent':terminal_ids[100:]},
            root=self.root,state=self.state,native=True,result_budget=24*1024)
        self.assertTrue(first.get('ok'),first);self.assertEqual(first['items'],[])
        self.assertCountEqual(first['terminal'],terminal_ids)
        self.assertLessEqual(len(json.dumps(first,ensure_ascii=False,separators=(',',':')).encode('utf-8')),24*1024)
        self.assertEqual(self.row(queued['id'])['status'],'queued','budget check must precede the claim mutation')
        self.assertGreater(self.row(other['id'])['seen'],stale_seen,msg='all active observations are checked before claim selection')
        import base64
        import importlib.util
        import io
        from contextlib import redirect_stdout
        native_name='approval_native_frame_fixture'
        native_path=Path(__file__).with_name('__init__.py')
        spec=importlib.util.spec_from_file_location(native_name,native_path,submodule_search_locations=[str(native_path.parent)])
        native_entry=importlib.util.module_from_spec(spec);__import__('sys').modules[native_name]=native_entry
        spec.loader.exec_module(native_entry)
        stdout=io.StringIO()
        with redirect_stdout(stdout):native_entry._write_frame(first)
        frame=stdout.getvalue();self.assertLessEqual(len(frame),48_000)
        encoded=frame.splitlines()[1]
        frame_json=base64.urlsafe_b64decode(encoded+'='*((4-len(encoded)%4)%4))
        self.assertLessEqual(len(frame_json),24*1024);self.assertEqual(json.loads(frame_json),first)
        second=notifier.dispatch_request({'action':'approval_poll','capability':b.CAPABILITY,'owner':self.owner,
            'observations':observations,'unverified':[],'absent':[]},root=self.root,state=self.state,
            native=True,result_budget=24*1024)
        self.assertEqual(len(second['items']),1,second)
        self.assertEqual(second['items'][0]['id'],queued['id'])
        self.assertEqual(self.row(queued['id'])['status'],'applying')

    def test_single_claim_that_cannot_fit_native_result_is_rejected_before_buttons(self):
        register_req={'action':'approval_register','capability':b.CAPABILITY,'owner':self.owner,
            'descriptor':self.descriptor,'request':self.request,'pending':self.pending,'route':self.route,
            'session':self.session,'epoch':'epoch-fixture'}
        rejected=notifier.dispatch_request(register_req,root=self.root,state=self.state,native=True,result_budget=256)
        self.assertEqual(rejected,{'ok':False,'reason':'approval_result_too_large'})
        c=b.connect(self.state)
        try:self.assertEqual(c.execute('SELECT count(*) FROM approvals').fetchone()[0],0)
        finally:c.close()
        self.assertFalse(any(method=='sendMessage' for method,_ in self.calls))
        notice=notifier.dispatch_request({'action':'approval_notice','capability':b.CAPABILITY,'owner':self.owner,
            'descriptor':self.descriptor,'request':self.request,'pending':self.pending,
            'reason':'approval_result_too_large'},root=self.root,state=self.state,native=True,result_budget=24*1024)
        self.assertTrue(notice.get('ok'),notice)
        payload=next(payload for method,payload in self.calls if method=='sendMessage')
        self.assertNotIn(self.request['params']['command'],payload['text'])
        self.assertNotIn('reply_markup',payload)
        self.assertIn('Desktop에서',payload['text'])

    def test_unsafe_notice_reasons_are_command_free_deduplicated_via_native_dispatch(self):
        command='rm -rf approval-fixture && echo password=synthetic-secret-value-739102'
        self.request,self.pending=self.pair(command=command)
        self.request_fp=b._digest(self.request);self.pending_fp=b._digest(self.pending);self.descriptor=self.descriptor_for()
        calls=[]
        reason='invalid_review_text'
        for _ in range(2):
            response=notifier.dispatch_request({'action':'approval_notice','capability':b.CAPABILITY,
                'owner':self.owner,'descriptor':self.descriptor,'request':self.request,'pending':self.pending,
                'reason':reason},root=self.root,state=self.state,native=True,result_budget=24*1024)
            self.assertTrue(response.get('ok'),response)
            calls.append(response)
        sends=[payload for method,payload in self.calls if method=='sendMessage']
        self.assertEqual(len(sends),1)
        self.assertIs(calls[1].get('duplicate'),True)
        self.assertNotIn(command,sends[0]['text']);self.assertNotIn('synthetic-secret-value-739102',sends[0]['text'])
        self.assertNotIn('reply_markup',sends[0]);self.assertIn('Desktop에서',sends[0]['text'])

    def test_ptb_process_update_rejects_actual_edit_date_even_when_card_text_matches(self):
        from telegram import Update
        from telegram.ext import Application, TypeHandler
        async def run():
            def same_text_not_modified(token,method,payload):
                if method=='editMessageText':
                    self.calls.append((method,copy.deepcopy(payload)))
                    return {'ok':False,'error_code':400,'description':'Bad Request: message is not modified'}
                return self.telegram(token,method,payload)
            with patch.object(notifier,'telegram',side_effect=same_text_not_modified):
                result=self.registered()
            self.assertEqual(result['status'],'open','same-text 400 not modified is not an edit of the card')
            row=self.row(result['id']);before=(row['status'],row['choice'],row['seen'])
            callback_data=self.calls[0][1]['reply_markup']['inline_keyboard'][0][0]['callback_data']
            app=Application.builder().token('77:synthetic-token').build()
            with patch.object(type(app.bot),'initialize',new=AsyncMock()),patch.object(type(app.bot),'shutdown',new=AsyncMock()):
                await app.initialize()
                self.assertTrue(b.wire(app,notifier,77,99,None,'boot-fixture',enabled=lambda:True,state=self.state))
                later=[]
                async def downstream(update,context):later.append(update)
                app.add_handler(TypeHandler(Update,downstream),group=1)
                update=Update.de_json({'update_id':90,'callback_query':{'id':'edited-query',
                    'from':{'id':99,'is_bot':False,'first_name':'owner'},'chat_instance':'fixture','data':callback_data,
                    'message':{'message_id':row['message_id'],'date':1,'edit_date':2,
                        'chat':{'id':99,'type':'private'},'from':{'id':77,'is_bot':True,'first_name':'bot'},
                        'text':row['review_text']}}},app.bot)
                answers=AsyncMock();real_choose=b.choose;identities=[]
                def capture_choose(token,choice,identity,**kwargs):
                    identities.append(identity);return real_choose(token,choice,identity,**kwargs)
                with patch.object(b,'choose',side_effect=capture_choose),patch.object(type(update.callback_query),'answer',new=answers):
                    await app.process_update(update)
                self.assertEqual(len(identities),1)
                self.assertIs(identities[0]['edited'],True)
                after=self.row(result['id'])
                self.assertEqual((after['status'],after['choice'],after['seen']),before)
                self.assertEqual(self.receipts(),0);self.assertEqual(later,[])
                self.assertEqual(answers.await_args.args[0],'만료되었거나 현재 요청과 일치하지 않습니다. Desktop에서 확인해 주세요.')
                await app.shutdown()
        asyncio.run(run())

if __name__=='__main__':unittest.main(verbosity=2)

