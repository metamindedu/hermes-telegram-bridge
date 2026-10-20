"""Security contract tests for the separate native-command approval inbox."""
from __future__ import annotations
import asyncio, copy, importlib, importlib.util, json, os, subprocess, time, unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import approval_bridge as b
import notifier
import test_support

CORE_GENERATION_REFS={
    'legacy':'74306c1d1b080837744faca3e371267dd09dda4b',
    'current':'297552a9d646119871391d202509822cd86330df',
    'installed':'d923a5eb487a17c0a68801bba9ef0f56c81040a8',
}

class NativeApprovalRegistrationRecoveryTests(unittest.TestCase):
    def test_registered_cli_lost_registration_lookup_observe_choose_and_settle_once(self):
        self._run_native_registration_recovery()

    def test_post_publication_flush_failure_is_recovered_without_another_registration(self):
        self._run_native_registration_recovery(fail_flush=True)

    def _run_native_registration_recovery(self,fail_flush=False):
        fixture_module=importlib.import_module('test_completion_recovery')
        native=fixture_module.CompletionRecoveryTests();self.addCleanup(native.doCleanups);native.setUp()
        (native.home/'config.yaml').write_text('plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings:\n        approvals_enabled: true\napprovals:\n  timeout: 300\n',encoding='utf-8')
        bridge=importlib.import_module(native.name+'.approval_bridge')
        now=time.time();owner='native-registration-owner'
        bridge.bind_receiver(123456,99,None,'native-boot',private_verified=True,state=native.state,now=now)
        bridge.owner_heartbeat(owner,state=native.state,now=now)
        request,pending=ApprovalBridgeTests.pair(sid='native-runtime')
        route={'mode':'local','connectionId':'local','profile':'default','targetProfile':'default'}
        session={'id':'native-runtime','session_key':native.sid,'status':'waiting'}
        descriptor={'owner':owner,'connectionId':'local','profile':'default','targetProfile':'default',
                    'sid':session['id'],'session_key':session['session_key'],'epoch':'native-epoch',
                    'request_id':request['id'],'queue_id':pending['request_id'],'dispatch_mode':'legacyqueue',
                    'request_fingerprint':bridge._digest(request),'pending_fingerprint':bridge._digest(pending)}
        sent=[]
        def telegram(_token,method,payload):
            if method=='sendMessage':sent.append(1)
            return {'ok':True,'result':{'message_id':801,'chat':{'id':99,'type':'private'},
                    'text':payload['text'],'from':{'id':123456,'is_bot':True}}}
        def invoke(action,**fields):
            return native.invoke({'action':action,'capability':bridge.CAPABILITY,'owner':owner,**fields})
        with patch.object(native.notifier,'credentials',return_value=(fixture_module.TOKEN,99,None)),patch.object(native.notifier,'telegram',side_effect=telegram):
            # Discard the caller receipt after the real native handler published.
            with patch.object(bridge,'flush',side_effect=RuntimeError('synthetic post-publication failure')) if fail_flush else patch.object(bridge,'flush',wraps=bridge.flush):
                receipt=invoke('approval_register',descriptor=descriptor,request=request,pending=pending,route=route,session=session,epoch='native-epoch')
            if fail_flush:self.assertEqual(receipt,{'ok':False,'reason':'approval_store_unavailable'})
            with bridge.connect(native.state) as c:
                row=dict(c.execute('SELECT * FROM approvals').fetchone())
            self.assertEqual(row['status'],'open');fid=row['id']
            invoke('approval_poll',observations=[],unverified=[],absent=[])
            invoke('approval_owner_heartbeat')
            path=native.state/'approval-inbox/approvals.sqlite3';before=path.read_bytes()
            found=invoke('approval_registration_lookup',descriptor=descriptor,request=request,pending=pending)
            self.assertEqual(found,{'ok':True,'found':True,'id':fid,'status':'open','expires':row['expires']})
            self.assertEqual(path.read_bytes(),before)
            observation={'id':fid,'request_fingerprint':descriptor['request_fingerprint'],
                         'pending_fingerprint':descriptor['pending_fingerprint'],'authority':bridge._digest(descriptor)}
            invoke('approval_poll',observations=[observation],unverified=[],absent=[])
            identity={'bot':123456,'boot':'native-boot','chat':99,'user':99,'topic':None,'mid':801,
                      'private':True,'human':True,'bot_author':True,'forwarded':False,'edited':False,
                      'inline':False,'inaccessible':False,'message_text':row['review_text'],'event':'native-choice-once'}
            self.assertTrue(bridge.choose(row['token'],'once',identity,state=native.state)['ok'])
            item=invoke('approval_poll',observations=[observation],unverified=[],absent=[])['items'][0]
            self.assertEqual(item['descriptor'],descriptor)
            self.assertTrue(invoke('approval_settle',id=fid,claim=item['claim'],status='applied')['ok'])
            self.assertFalse(invoke('approval_registration_lookup',descriptor=descriptor,request=request,pending=pending)['found'])
            self.assertEqual(sent,[1])


class ApprovalBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=test_support.temporary_directory(prefix='approval-bridge-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'home';self.root.mkdir()
        self.home_patch=patch.dict('os.environ',{'HERMES_HOME':str(self.root)})
        self.home_patch.start();self.addCleanup(self.home_patch.stop)
        (self.root/'config.yaml').write_text('plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings:\n        approvals_enabled: true\napprovals:\n  timeout: 300\n',encoding='utf-8')
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

    def _assert_core_generation(self):
        import agent.redact as redact

        configured_root=os.environ.get('HERMES_CORE_ROOT')
        root=Path(configured_root).resolve() if configured_root else Path(redact.__file__).resolve().parents[1]
        self.assertTrue(root.is_dir(),f'bound Hermes core root is unavailable: {root}')
        modules=[redact]
        for name in ('tools.approval','tui_gateway.server','tui_gateway.server_requests'):
            modules.append(importlib.import_module(name))
        for module in modules:
            origin=Path(module.__file__).resolve()
            self.assertTrue(origin.is_relative_to(root),
                f'{module.__name__} escaped the declared Hermes core: {origin} not under {root}')

        editor_path=root/'gateway'/'session_local_editor.py'
        inferred='current' if editor_path.is_file() else 'legacy'
        generation=os.environ.get('HERMES_CORE_GENERATION',inferred)
        self.assertIn(generation,CORE_GENERATION_REFS)
        api_generation='legacy' if generation=='installed' else generation
        self.assertEqual(api_generation,inferred,
            f'declared generation {generation} contradicts actual editor producer availability at {editor_path}')
        expected_ref=CORE_GENERATION_REFS[generation]
        declared_ref=os.environ.get('HERMES_CORE_REF')
        if declared_ref is not None:
            self.assertEqual(declared_ref,expected_ref)
        if (root/'.git').exists():
            revision=subprocess.run(['git','-C',str(root),'rev-parse','HEAD'],check=True,
                capture_output=True,text=True).stdout.strip()
            if declared_ref is not None:
                self.assertEqual(revision,declared_ref,'checked-out core revision differs from its declared generation')
        elif os.environ.get('CI','').lower()=='true':
            self.fail('CI core checkout must expose Git metadata for immutable-ref verification')

        if api_generation=='current':
            self.assertTrue(editor_path.is_file())
            editor=importlib.import_module('gateway.session_local_editor')
            self.assertEqual(Path(editor.__file__).resolve(),editor_path.resolve())
        else:
            self.assertFalse(editor_path.exists(),'legacy core unexpectedly contains the editor producer')
            self.assertIsNone(importlib.util.find_spec('gateway.session_local_editor'),
                'a newer namespace module leaked into the exact legacy-core test')
        return api_generation,root

    @staticmethod
    def pair(command='rm -rf approval-fixture',description='recursive delete',key='recursive delete',
             queue_id='a'*32,srq='srq-aabbcc112233',method='approval',sid='runtime-fixture',allow=True,smart=None):
        pending={'request_id':queue_id,'command':command,'description':description,'pattern_key':key,
                 'pattern_keys':[key],'allow_permanent':False if smart is True else allow,
                 'allow_session':False if smart is True else True}
        params={'session_id':sid,'request_id':queue_id,'command':command,'description':description,
                'pattern_key':key,'pattern_keys':[key],'allow_permanent':False if smart is True else allow,
                'allow_session':False if smart is True else True,
                'choices':['once','deny'] if smart is True else
                    (['once','session','always','deny'] if allow else ['once','session','deny'])}
        if smart is not None: pending['smart_denied']=params['smart_denied']=smart
        return {'id':srq,'method':method,'params':params},pending

    def descriptor_for(self,request=None,pending=None,owner=None,route=None,session=None,epoch='epoch-fixture'):
        request=request or self.request;pending=pending or self.pending;route=route or self.route;session=session or self.session
        return {'owner':owner or self.owner,'connectionId':route['connectionId'],'profile':route['profile'],
            'targetProfile':route['targetProfile'],'sid':session['id'],'session_key':session['session_key'],
            'epoch':str(epoch),'request_id':request['id'],'queue_id':pending['request_id'],
            'dispatch_mode':'legacyqueue','request_fingerprint':b._digest(request),'pending_fingerprint':b._digest(pending)}

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

    def test_unmentioned_open_card_is_unknown_and_heartbeat_does_not_expire_it(self):
        result=self.register();fid=result['id'];before=self.row(fid)
        held=b.poll(self.owner,[],state=self.state,now=self.now+1)
        self.assertEqual(held['items'],[])
        self.assertEqual(self.row(fid)['status'],'open')
        self.assertEqual(self.row(fid)['seen'],b.STALE_SEEN)
        self.assertFalse(b.choose(before['token'],'once',self.identity(fid),state=self.state,now=self.now+2)['ok'])
        b.owner_heartbeat(self.owner,state=self.state,now=self.now+3)
        self.assertEqual(self.row(fid)['status'],'open')
        renewed=b.poll(self.owner,[self.observe(fid)],state=self.state,now=self.now+4)
        self.assertEqual(renewed['items'],[])
        self.assertEqual(self.row(fid)['expires'],before['expires'])
        self.assertEqual(self.row(fid)['seen'],self.now+4)
        self.assertEqual(len([x for x in self.calls if x[0]=='sendMessage']),1)

    def test_unverified_open_card_survives_next_heartbeat_but_not_explicit_absence(self):
        result=self.register();fid=result['id'];before=self.row(fid)
        b.poll(self.owner,[],unverified=[fid],state=self.state,now=self.now+1)
        b.owner_heartbeat(self.owner,state=self.state,now=self.now+2)
        self.assertEqual(self.row(fid)['status'],'open')
        self.assertEqual(self.row(fid)['expires'],before['expires'])
        b.poll(self.owner,[],absent=[fid],state=self.state,now=self.now+3)
        self.assertEqual(self.row(fid)['status'],'expired')

    def test_registration_lookup_is_read_only_exact_owner_and_never_revives(self):
        fid=self.register()['id'];before=self.row(fid)
        req={'owner':self.owner,'descriptor':self.descriptor,'request':self.request,'pending':self.pending}
        path=self.state/'approval-inbox/approvals.sqlite3';original=path.read_bytes()
        found=b.lookup_registration(req,notifier,root=self.root,state=self.state,now=self.now+1)
        self.assertEqual(found,{'ok':True,'found':True,'id':fid,'status':'open','expires':before['expires']})
        self.assertEqual(path.read_bytes(),original)
        for field in self.descriptor:
            changed=copy.deepcopy(req);changed['descriptor'][field]='foreign'
            self.assertFalse(b.lookup_registration(changed,notifier,root=self.root,state=self.state,now=self.now+1)['found'])
        self.assertEqual(path.read_bytes(),original)
        for field,value in [('status','posting'),('status','applying'),('status','dispatching'),
                            ('status','expired'),('status','unknown'),('message_id',None),('boot','foreign')]:
            with b.connect(self.state) as c:c.execute('UPDATE approvals SET '+field+'=? WHERE id=?',(value,fid))
            original=path.read_bytes()
            self.assertFalse(b.lookup_registration(req,notifier,root=self.root,state=self.state,now=self.now+1)['found'])
            self.assertEqual(path.read_bytes(),original)
            with b.connect(self.state) as c:c.execute('UPDATE approvals SET '+field+'=? WHERE id=?',(before[field],fid))
        self.assertFalse(b.lookup_registration(req,notifier,root=self.root,state=self.state,now=self.now+b.LEASE+1)['found'])
        self.assertFalse(b.lookup_registration(req,notifier,root=self.root,state=self.state/'missing',now=self.now)['found'])
        self.assertFalse((self.state/'missing').exists())

    def test_native_approval_pair_uses_live_request_and_queue_not_detector_membership(self):
        from tools.approval_detection import detect_dangerous_command
        self.assertEqual(detect_dangerous_command('rm -rf approval-fixture'),(True,'recursive delete','recursive delete'))
        self.assertTrue(b.supported_pair(self.request,self.pending,self.session['id']))
        for key in ('plugin_rule:rule','execute_code','tirith:secret','future_native_key'):
            ask,pending=self.pair(key=key)
            self.assertTrue(b.supported_pair(ask,pending,self.session['id']),key)
        ask,pending=self.pair(method='sudo');self.assertFalse(b.supported_pair(ask,pending,self.session['id']))
        ask=copy.deepcopy(self.request);ask['params']['extra']='x';self.assertFalse(b.supported_pair(ask,self.pending,self.session['id']))

    def test_server_request_authority_requires_valid_approval_and_exact_empty_queue_marker(self):
        marker={'request_id':self.request['params']['request_id'],'missing':True}
        self.assertTrue(b.supported_pair(self.request,marker,self.session['id'],'legacyserver_request'))
        for request in [
            {**self.request,'id':'srq-short'},
            {**self.request,'method':'sudo'},
            {**self.request,'params':{**self.request['params'],'choices':['once','deny','deny']}},
            {**self.request,'params':{**self.request['params'],'allow_session':False}},
        ]:
            self.assertFalse(b.supported_pair(request,marker,self.session['id'],'legacyserver_request'))
        self.assertFalse(b.supported_pair(self.request,{},self.session['id'],'legacyserver_request'))

    def test_native_queue_snapshot_classifier_distinguishes_absence_from_malformed_and_conflict(self):
        queue=b.classify_approval_authority(self.request,{'approvals':[self.pending]},self.session['id'])
        self.assertEqual(queue,{'dispatch_mode':'legacyqueue','pending':self.pending})
        absent=b.classify_approval_authority(self.request,{'approvals':[]},self.session['id'])
        self.assertEqual(absent,{'dispatch_mode':'legacyserver_request',
            'pending':{'request_id':self.request['params']['request_id'],'missing':True}})
        for snapshot in (None,{},False,{'approvals':'bad'}, {'approvals':[{}]},
                         {'approvals':[self.pending,self.pending]},
                         {'approvals':[dict(self.pending,command='changed')]},
                         {'approvals':[dict(self.pending,request_id='b'*32)]}):
            self.assertIsNone(b.classify_approval_authority(self.request,snapshot,self.session['id']),repr(snapshot))

    def test_type_scope_choice_contradictions_and_uuid_shape_fail_closed(self):
        mutations=[]
        ask,pending=self.pair(allow=False);self.assertTrue(b.supported_pair(ask,pending,self.session['id']))
        ask['params']['choices']=['once','always','deny'];pending['choices']=ask['params']['choices'];mutations.append((ask,pending))
        ask,pending=self.pair(smart=True);self.assertTrue(b.supported_pair(ask,pending,self.session['id']))
        ask['params']['choices']=['once','session','deny'];pending['choices']=ask['params']['choices'];mutations.append((ask,pending))
        ask,pending=self.pair();pending['request_id']='not-a-native-uuid';ask['params']['request_id']=pending['request_id'];mutations.append((ask,pending))
        for ask,pending in mutations:self.assertFalse(b.supported_pair(ask,pending,self.session['id']))

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
                'dispatch_mode':'changed','request_fingerprint':'0'*64,'pending_fingerprint':'1'*64}
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

    def test_native_review_renders_full_supplied_params_and_does_not_invent_missing_arguments(self):
        request,pending=self.pair(key='protected_instruction_file',command='<write to AGENTS.md>',
            description='Protected instruction file write')
        request['params'].update({'allow_permanent':False,'allow_session':False,'choices':['once','deny'],
            'future_native_field':{'nested':['complete','payload']}})
        pending.update({'allow_permanent':False,'allow_session':False})
        text=b.review_card(request,300)
        self.assertIn('future_native_field',text)
        self.assertIn('complete',text)
        self.assertIn('Protected instruction file write',text)
        self.assertIn('AGENTS.md',text)
        self.assertIn('요청에 없는',text)
        self.assertNotIn('patch contents',text.lower())

        editor,_=self.pair(key='editor-'+'e'*32,command='Edit src/file.py',description='Approve proposed file change')
        editor['params'].update({'allow_permanent':False,'allow_session':False,'choices':['once','deny'],
            'edit':{'path':'src/file.py','old_text':'before\\n','new_text':'after\\n',
                    'diff':'-before\\n+after'}})
        rendered=b.review_card(editor,300)
        self.assertIn('src/file.py',rendered)
        self.assertIn('-before',rendered)
        self.assertIn('+after',rendered)

        python,_=self.pair(key='execute_code',command="execute_code <<'PY'\\n"+'x'*5000+'\\nPY',
            description='Full Python script')
        with self.assertRaisesRegex(ValueError,'review_card_too_large'):
            b.review_card(python,300)

    def test_structured_native_review_redacts_raw_leaves_before_json_serialization(self):
        self._assert_core_generation()
        secrets_to_hide=[
            'synthetic-editor-old-password-739102',
            'synthetic-editor-old-api-key-739102',
            'synthetic-editor-new-password-739102',
            'synthetic-editor-new-access-token-739102',
            'synthetic-editor-standalone-password-739102',
            'synthetic-editor-nested-api-key-739102',
            'synthetic-editor-array-secret-739102',
        ]
        request,_=self.pair(command='Edit synthetic config',description='Synthetic editor review',
                            key='editor-'+'b'*32,allow=False)
        request['params'].update({'allow_session':False,'choices':['once','deny']})
        request['params']['edit']={
            'old_text':'old config line\nsecond old line',
            'new_text':'new config line\nsecond new line',
            'arguments':{
                'password':secrets_to_hide[4],
                'nested':[{'api_key':secrets_to_hide[5],
                           'metadata':{'event':'synthetic-event-metadata-visible-739102'}},
                          [{'secret':secrets_to_hide[6]}]],
                'metadata':{'note':'preserve this native metadata','attempt':3},
            },
        }
        review=b.review_card(request,300)
        for value in secrets_to_hide:
            self.assertNotIn(value,review)
        for field in ('old_text','new_text','arguments','nested','password','api_key','secret'):
            self.assertIn(field,review)
        self.assertIn('"password": "***"',review)
        self.assertIn('"api_key": "***"',review)
        self.assertIn('"secret": "***"',review)
        self.assertIn('synthetic-event-metadata-visible-739102',review)
        self.assertIn('preserve this native metadata',review)

        assignment,_=self.pair(command='Edit synthetic config',description='Synthetic editor assignment review',
                               key='editor-'+'c'*32,allow=False)
        assignment['params'].update({'allow_session':False,'choices':['once','deny'],
            'edit':{'old_text':f'password: {secrets_to_hide[0]}\nAPI_KEY={secrets_to_hide[1]}',
                    'new_text':f'password: {secrets_to_hide[2]}\nACCESS_TOKEN={secrets_to_hide[3]}'}})
        with self.assertRaisesRegex(ValueError,'redaction_(?:not_stable|omission)'):
            b.review_card(assignment,300)

    def test_structured_native_review_rejects_unsafe_or_redacted_object_keys(self):
        self._assert_core_generation()
        unsafe_keys=('unsafe\u202e-key','e\u0301', 'sk-'+('A1b2C3d4'*3))
        for index,key in enumerate(unsafe_keys):
            with self.subTest(key_index=index):
                request,_=self.pair(command='Edit synthetic config',description='Synthetic key validation',
                                    key='editor-'+format(index+1,'032x'),allow=False)
                request['params'].update({'allow_session':False,'choices':['once','deny'],
                    'edit':{'arguments':{key:'preserve-or-reject-without-rewriting'}}})
                with self.assertRaises(ValueError):
                    b.review_card(request,300)

    def test_server_request_card_registration_freezes_mode_and_terminal_unknown_never_replays(self):
        marker={'request_id':self.request['params']['request_id'],'missing':True}
        self.descriptor=self.descriptor_for(self.request,marker)
        self.descriptor['dispatch_mode']='legacyserver_request'
        self.descriptor['pending_fingerprint']=b._digest(marker)
        result=self.register(pending=marker,descriptor=self.descriptor)
        self.assertTrue(result.get('ok'),result)
        row=self.row(result['id'])
        self.assertEqual(json.loads(row['descriptor'])['dispatch_mode'],'legacyserver_request')
        tap_now=max(self.now+.1,time.time()+.1)
        tapped=b.choose(row['token'],'deny',self.identity(result['id'],event='server-request-deny'),
            state=self.state,now=tap_now)
        self.assertTrue(tapped.get('ok'),tapped)
        observation=self.observe(result['id'])
        polled=b.poll(self.owner,[observation],state=self.state,now=tap_now+.1)
        self.assertEqual(polled['items'][0]['descriptor']['dispatch_mode'],'legacyserver_request')
        item=polled['items'][0]
        self.assertTrue(b.settle(result['id'],item['claim'],'unknown',state=self.state,now=tap_now+.2)['ok'])
        self.assertEqual(self.row(result['id'])['status'],'unknown')
        self.assertIsNone(self.row(result['id'])['review_text'])
        self.assertEqual(b.poll(self.owner,[observation],state=self.state,now=self.now+.4)['items'],[])

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

    def test_malformed_approval_blocks_are_unavailable_without_review_or_buttons(self):
        command='rm -rf approval-fixture && echo password=synthetic-secret-value-739102'
        self.request,self.pending=self.pair(command=command)
        self.descriptor=self.descriptor_for()
        req={'action':'approval_register','capability':b.CAPABILITY,'owner':self.owner,
            'descriptor':self.descriptor,'request':self.request,'pending':self.pending,
            'route':self.route,'session':self.session,'epoch':'epoch-fixture'}
        for block in ('malformed', ['malformed'], False, 0, '', [], None):
            with self.subTest(block=block):
                (self.root/'config.yaml').write_text(
                    'plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings:\n        approvals_enabled: true\napprovals: '+json.dumps(block)+'\n',encoding='utf-8')
                result=notifier.dispatch_request(req,root=self.root,state=self.state,native=True,result_budget=24*1024)
                self.assertEqual(result,{'ok':False,'reason':'timeout_unavailable'})
                self.assertEqual(self.calls,[])
                c=b.connect(self.state)
                try:self.assertEqual(c.execute('SELECT count(*) FROM approvals').fetchone()[0],0)
                finally:c.close()
        (self.root/'config.yaml').write_text('plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings:\n        approvals_enabled: true\n',encoding='utf-8')
        self.assertEqual(b._approval_timeout('default',notifier,self.root),300)

    def test_actual_timeout_is_used_without_clarify_ttl_floor(self):
        consent='plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings:\n        approvals_enabled: true\n'
        (self.root/'config.yaml').write_text(consent+'approvals:\n  timeout: 23\n',encoding='utf-8')
        result=self.registered();self.assertLessEqual(result['expires']-self.now,23.01)
        self.assertLess(result['expires']-self.now,3600)
        (self.root/'config.yaml').write_text(consent+'approvals:\n  timeout: 0\n',encoding='utf-8')
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

    def test_notice_uses_fresh_attention_gate_for_claim_send_and_verification(self):
        req={'action':'approval_notice','capability':b.CAPABILITY,'owner':self.owner,
            'descriptor':self.descriptor,'request':self.request,'pending':self.pending,
            'reason':'review_unavailable'}
        gates=[]
        def attention_only(name,**_kwargs):
            gates.append(name)
            return name=='attention_notifications_enabled'
        with patch.object(notifier,'action_enabled',side_effect=attention_only):
            result=b.helper(req,notifier,root=self.root,state=self.state)
        self.assertTrue(result['ok'] and result['verified'] and result['notification_only'],result)
        self.assertEqual(gates,['attention_notifications_enabled']*4)
        sends=[payload for method,payload in self.calls if method=='sendMessage']
        self.assertEqual(len(sends),1)
        self.assertNotIn(self.request['params']['command'],sends[0]['text'])
        self.assertNotIn('reply_markup',sends[0])
        self.assertEqual([method for method,_ in self.calls],['sendMessage','editMessageText'])

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

    def _run_real_native_approval_producer(self, label, producer, *, approval_mode='on', smart_denied=False,
                                           mcp=False, answer_method='approval.respond', isolate_tirith=False,
                                           expect_registration=True):
        import uuid, threading
        from contextlib import ExitStack
        from tools import approval as native_approval, approval_context
        from tui_gateway import server, server_requests
        # Legacy emission lazily imports the real Gateway redactor and its dependency
        # tree. Qualify that setup before starting the five-second publication oracle;
        # otherwise a cold import can be mistaken for a missing native approval.
        prepared = server._approval_request_payload({'command':'synthetic redactor preparation'})
        self.assertEqual(prepared['command'],'synthetic redactor preparation')

        sid='native-approval-'+uuid.uuid4().hex[:12]
        session_key='native-session-key-'+uuid.uuid4().hex[:12]
        server._sessions[sid]={'session_key':session_key}
        notified=threading.Event();captured=[];outcome=[];thread_error=[]
        def notify(data):
            server._emit_approval_request(sid,data)
            captured.extend(server_requests.open_requests(sid))
            notified.set()
        native_approval._gateway_notify_cbs[session_key]=notify
        def invoke():
            try:outcome.append(producer())
            except BaseException as exc:thread_error.append(exc)

        thread=None
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.dict('os.environ',{'USERPROFILE':str(self.root),'HOME':str(self.root)}))
                stack.enter_context(patch.object(server_requests,'_write',lambda _frame:None))
                stack.enter_context(patch.object(server_requests,'_emit',lambda *_args,**_kwargs:None))
                stack.enter_context(patch.object(native_approval,'_presence',return_value=(None,False,True,False)))
                stack.enter_context(patch.object(native_approval,'get_current_session_key',return_value=session_key))
                stack.enter_context(patch.object(approval_context,'get_current_session_key',return_value=session_key))
                stack.enter_context(patch.object(native_approval,'_unattended_contexts',return_value=[]))
                stack.enter_context(patch.object(approval_context,'_get_approval_mode',return_value=approval_mode))
                if isolate_tirith and hasattr(native_approval,'_tirith_scan'):
                    stack.enter_context(patch.object(native_approval,'_tirith_scan',return_value={
                        'action':'allow','findings':[],'summary':''}))
                if smart_denied:
                    stack.enter_context(patch.object(native_approval,'_smart_gate',return_value=(None,True)))
                if mcp:
                    prompt=__import__('tools.approval_prompt',fromlist=['request_elicitation_consent'])
                    stack.enter_context(patch.object(prompt._ctx,'_is_gateway_approval_context',return_value=True))
                    stack.enter_context(patch.object(prompt._ctx,'_is_single_query_approval_context',return_value=False))
                    stack.enter_context(patch.object(prompt._ctx,'_is_cron_approval_context',return_value=False))
                thread=threading.Thread(target=invoke,name='native-approval-producer-'+label,daemon=True)
                thread.start()
                published = notified.wait(5)
                if not published:
                    import sys, traceback
                    frame = sys._current_frames().get(thread.ident)
                    observed = [(item.name, item.lineno) for item in traceback.extract_stack(frame)] if frame else []
                    self.fail(f'{label} did not emit a native approval request: errors={thread_error!r}; outcome={outcome!r}; thread_frames={observed!r}')
                self.assertFalse(thread_error,repr(thread_error))
                self.assertTrue(captured, label)
                request=json.loads(json.dumps(captured[-1],ensure_ascii=False))
                queue=native_approval.list_gateway_approvals(session_key)
                self.assertEqual(len(queue),1,label)
                pending=json.loads(json.dumps(queue[0],ensure_ascii=False))
                self.assertEqual(request['method'],'approval',label)
                self.assertRegex(request['id'],r'^srq-[0-9a-f]{12}$',label)
                self.assertRegex(pending['request_id'],r'^[0-9a-f]{32}$',label)
                self.assertTrue(b.supported_pair(request,pending,sid),f'{label}: request={request!r}; pending={pending!r}')
                native_session={'id':sid,'session_key':session_key,'status':'waiting'}
                descriptor=self.descriptor_for(request,pending,route=self.route,session=native_session,epoch='native-epoch')
                self.assertTrue(b.validate_snapshot(descriptor,request,pending,self.route,native_session,
                    'native-epoch',self.owner),f'{label}: native producer snapshot was not frozen')
                registered=b.register({'action':'approval_register','capability':b.CAPABILITY,'owner':self.owner,
                    'descriptor':descriptor,'request':request,'pending':pending,'route':self.route,
                    'session':native_session,'epoch':'native-epoch'},notifier,root=self.root,state=self.state,now=self.now)
                if expect_registration:
                    self.assertTrue(registered.get('ok'),f'{label}: {registered}')
                    row=self.row(registered['id'])
                    self.assertEqual(row['status'],'open',label)
                    review=row['review_text']
                    self.assertIsInstance(review,str,label)
                else:
                    self.assertFalse(registered.get('ok'),f'{label}: unsafe review must fail closed: {registered}')
                    row=None
                    review=None
                entry=native_approval._gateway_queues[session_key][0]
                if answer_method=='request.answer':
                    receipt=server._methods['request.answer']('native-request-answer',
                        {'id':request['id'],'result':{'choice':'deny','all':False}})
                    self.assertEqual(receipt['result'],{'status':'ok'},label)
                    self.assertEqual(entry.result,'deny',label)
                    late=server._methods['approval.respond']('native-late-approval-respond',
                        {'session_id':sid,'request_id':pending['request_id'],'choice':'once','all':False})
                    self.assertEqual(late['result']['resolved'],0,label)
                else:
                    receipt=server._methods['approval.respond']('native-approval-respond',
                        {'session_id':sid,'request_id':pending['request_id'],'choice':'deny','all':False})
                    self.assertEqual(receipt['result']['resolved'],1,label)
                    self.assertEqual(entry.result,'deny',label)
                    late=server._methods['approval.respond']('native-late-approval-respond',
                        {'session_id':sid,'request_id':pending['request_id'],'choice':'once','all':False})
                    self.assertEqual(late['result']['resolved'],0,label)
                thread.join(5)
                self.assertFalse(thread.is_alive(),label)
                self.assertFalse(thread_error,repr(thread_error))
                self.assertTrue(entry.event.is_set(),label)
                return request,pending,receipt,outcome,review,descriptor,native_session,registered
        finally:
            with patch.object(server_requests,'_emit',lambda *_args,**_kwargs:None):
                # The synthetic sid is owned entirely by this fixture. Use the public
                # two-argument API shared by both immutable core generations.
                server_requests.cancel(sid,'interrupted')
            if thread is not None:thread.join(5)
            with native_approval._lock:
                native_approval._gateway_queues.pop(session_key,None)
                native_approval._gateway_notify_cbs.pop(session_key,None)
            server._sessions.pop(sid,None)

    def test_real_native_producers_and_both_native_dispatchers_cover_all_approval_families(self):
        generation,core_root=self._assert_core_generation()
        from tools import approval as native_approval
        from tools.file_tools_write_guards import _request_protected_instruction_approval, _check_approval_required_write
        from tools.computer_use import tool as computer_use
        from tools.approval_prompt import request_elicitation_consent

        def ssh_config_producer():
            return _check_approval_required_write([str(self.root/'.ssh'/'config')],task_id='default')
        producers=[
            ('shell-danger',lambda:native_approval.check_dangerous_command('rm -rf /tmp/approval-fixture','local')),
            ('python-cli-shell',lambda:native_approval.check_dangerous_command(
                'python -c "print(1)"','local')),
            ('execute-code',lambda:native_approval.check_execute_code_guard('print("complete Python body")','local')),
            ('protected-instruction',lambda:_request_protected_instruction_approval(['AGENTS.md'])),
            ('ssh-config',ssh_config_producer),
            ('plugin-rule',lambda:native_approval.request_tool_approval('fixture_tool','Synthetic plugin approval',rule_key='fixture-rule')),
            ('computer-use',lambda:computer_use._request_approval('click',{'delivery_mode':'foreground','x':41,'y':23})),
            ('smart-denied',lambda:native_approval.check_all_command_guards('rm -rf /tmp/approval-fixture','local')),
            ('mcp-elicitation',lambda:request_elicitation_consent('Synthetic MCP action','Synthetic consent summary')),
        ]
        if generation=='current':
            editor_module=importlib.import_module('gateway.session_local_editor')
            from acp_adapter.edit_approval import EditProposal
            producers.append(('editor-edit',lambda:editor_module.request_editor_edit(
                EditProposal('edit_file','src/example.py','old line\n','new line\n',
                             {'diff':'-old line\n+new line'}))))
        else:
            editor_path=core_root/'gateway'/'session_local_editor.py'
            self.assertFalse(editor_path.exists())
            self.assertIsNone(importlib.util.find_spec('gateway.session_local_editor'))
        for label,producer in producers:
            with self.subTest(producer=label):
                request,pending,receipt,outcome,review,descriptor,native_session,registered=self._run_real_native_approval_producer(
                    label,producer,approval_mode='smart' if label=='smart-denied' else 'on',
                    smart_denied=label=='smart-denied',mcp=label=='mcp-elicitation',
                    answer_method='request.answer' if label=='execute-code' else 'approval.respond',
                    isolate_tirith=label=='smart-denied')
                self.assertIn('once',request['params']['choices'],label)
                self.assertIn('deny',request['params']['choices'],label)
                self.assertIn('요청에 없는',review,label)
                if label=='execute-code':
                    self.assertIn('complete Python body',request['params']['command'])
                    self.assertIn('complete Python body',review)
                if label=='shell-danger':
                    self.assertEqual(request['params']['command'],'rm -rf /tmp/approval-fixture')
                if label=='python-cli-shell':
                    self.assertEqual(request['params']['command'],'python -c "print(1)"')
                    self.assertIn('script execution via -e/-c flag',request['params']['pattern_key'])
                if label=='protected-instruction':
                    self.assertIs(request['params']['allow_session'],False)
                    self.assertIs(request['params']['allow_permanent'],False)
                    self.assertIn('AGENTS.md',review)
                if label=='plugin-rule':
                    self.assertNotIn('"arguments"',review)
                if label=='smart-denied':
                    self.assertIs(request['params']['smart_denied'],True)
                    self.assertIs(request['params']['allow_session'],False)
                    self.assertEqual(request['params']['choices'],['once','deny'])
                if label=='mcp-elicitation':
                    self.assertNotIn('allow_session',request['params'])
                    self.assertNotIn('allow_permanent',request['params'])
                if label=='editor-edit':
                    self.assertRegex(request['params']['pattern_key'],r'^editor-[0-9a-f]{32}$')
                    self.assertIn('diff',request['params']['edit']['arguments'])
                self.assertEqual(len(outcome),1,label)
        self.assertEqual(len(producers),10 if generation=='current' else 9)
        editor_coverage='actual producer executed' if generation=='current' else 'unavailable; exact legacy absence asserted'
        print(f"native approval producer coverage: {generation} ({len(producers)} families); "
              f"editor={editor_coverage}; root={core_root}")

    def test_real_editor_producer_redacts_structured_fields_before_telegram_egress(self):
        generation,core_root=self._assert_core_generation()
        if generation=='legacy':
            editor_path=core_root/'gateway'/'session_local_editor.py'
            self.assertFalse(editor_path.exists())
            self.assertIsNone(importlib.util.find_spec('gateway.session_local_editor'))
            print('editor redaction egress integration unavailable on exact legacy core; absence verified')
            return

        editor_module=importlib.import_module('gateway.session_local_editor')
        from acp_adapter.edit_approval import EditProposal
        markers=[
            'synthetic-editor-old-password-egress-739102',
            'synthetic-editor-old-api-key-egress-739102',
            'synthetic-editor-new-password-egress-739102',
            'synthetic-editor-new-token-egress-739102',
            'synthetic-editor-standalone-password-egress-739102',
            'synthetic-editor-nested-api-key-egress-739102',
            'synthetic-editor-array-secret-egress-739102',
        ]
        metadata='synthetic-editor-event-metadata-visible-739102'
        old_text=f'password: {markers[0]}\nAPI_KEY={markers[1]}'
        new_text=f'password: {markers[2]}\nACCESS_TOKEN={markers[3]}'
        arguments={
            'path':'src/synthetic-settings.yaml',
            'password':markers[4],
            'nested':[{'api_key':markers[5],
                       'metadata':{'event':metadata,'attempt':2}},
                      [{'secret':markers[6]}]],
            'metadata':{'note':'preserve editor argument metadata','attempt':3},
        }
        proposal=EditProposal('edit_file','src/synthetic-settings.yaml',old_text,new_text,arguments)
        request,pending,receipt,outcome,review,descriptor,native_session,registered=self._run_real_native_approval_producer(
            'editor-redaction',lambda:editor_module.request_editor_edit(proposal),expect_registration=False)
        self.assertFalse(outcome[0],'the fixture explicitly denies the native edit; no file is changed')
        self.assertFalse(registered.get('ok'),registered)
        self.assertIn(registered.get('reason'),{'redaction_not_stable','redaction_omission'},registered)
        self.assertIsNone(review,'unstable structured redaction must not create an actionable card')
        self.assertEqual(self.calls,[],'the unsafe review is rejected before any Telegram send/edit')
        self.assertEqual(request['params']['edit']['old_text'],old_text)
        self.assertEqual(request['params']['edit']['new_text'],new_text)
        self.assertEqual(request['params']['edit']['arguments'],arguments)

        notice=b.approval_notice({'request':request,'pending':pending,'descriptor':descriptor,
            'reason':'review_unavailable'},notifier,root=self.root,state=self.state)
        self.assertTrue(notice.get('ok'),notice)
        self.assertTrue(notice.get('notification_only'),notice)
        outbound=json.dumps([payload for _method,payload in self.calls],ensure_ascii=False)
        receipts=json.dumps({'registered':registered,'native_receipt':receipt,'producer_outcome':outcome,
                             'notice_receipt':notice},ensure_ascii=False)
        for marker in markers:
            self.assertNotIn(marker,outbound)
            self.assertNotIn(marker,receipts)
        notices=[payload for method,payload in self.calls if method=='sendMessage' and 'reply_markup' not in payload]
        self.assertEqual(len(notices),1)
        for marker in markers:
            self.assertNotIn(marker,notices[0]['text'])
        self.assertIn('Desktop에서',notices[0]['text'])

    def test_real_compute_host_request_answer_ok_is_only_a_weak_gateway_ack(self):
        import uuid, threading
        from contextlib import ExitStack
        from tui_gateway import server
        sid='compute-host-approval-'+uuid.uuid4().hex[:12]
        request_id='srq-'+uuid.uuid4().hex[:12]
        mirror={'id':request_id,'method':'approval','params':{'session_id':sid,'request_id':'a'*32}}
        server._sessions[sid]={'_compute_host_open_request':mirror,'history_lock':threading.RLock()}
        class FailingChildRelay:
            def respond(self,*_args,**_kwargs):raise RuntimeError('synthetic child relay loss')
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(server,'_session_uses_compute_host',return_value=True))
                stack.enter_context(patch.object(server,'_get_compute_host_supervisor',return_value=FailingChildRelay()))
                receipt=server._methods['request.answer']('compute-host-request-answer',
                    {'id':request_id,'result':{'choice':'deny','all':False}})
            self.assertEqual(receipt['result'],{'status':'ok'})
            self.assertNotIn('_compute_host_open_request',server._sessions[sid])
        finally:
            server._sessions.pop(sid,None)

    def test_real_ptb_application_process_update_handles_callback_and_text_no_fallthrough(self):
        from telegram import Update
        from telegram.ext import Application, TypeHandler
        async def run():
            result=self.registered();row=self.row(result['id'])
            callback_data=self.calls[0][1]['reply_markup']['inline_keyboard'][0][0]['callback_data']
            app=Application.builder().token('77:synthetic-token').build()
            with patch.object(type(app.bot),'initialize',new=AsyncMock()), patch.object(type(app.bot),'shutdown',new=AsyncMock()):
                await app.initialize()
                self.assertTrue(b.wire(app,notifier,77,99,None,'boot-fixture',approvals_enabled=lambda:True,state=self.state))
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
                self.assertTrue(b.wire(app,notifier,77,99,None,'boot-fixture',approvals_enabled=lambda:True,state=self.state))
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

    def test_claim_gate_barrier_rejects_boot_change_and_approval_revocation_without_replay(self):
        import threading
        for index,cause in enumerate(('boot-change','approval-disable')):
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
                (self.root/'config.yaml').write_text('plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings:\n        replies_enabled: true\n        approvals_enabled: false\napprovals:\n  timeout: 300\n',encoding='utf-8')
                self.assertEqual(self.row(item['id'])['status'],'applying')
            resume.set();thread.join(timeout=5);self.assertFalse(thread.is_alive())
            self.assertEqual(len(receipts),1,cause);self.assertIs(receipts[0].get('valid'),False,cause)
            self.assertEqual(respond,[],cause)
            if cause=='approval-disable':
                (self.root/'config.yaml').write_text('plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings:\n        approvals_enabled: true\napprovals:\n  timeout: 300\n',encoding='utf-8')
            replay=notifier.dispatch_request({'action':'approval_poll','capability':b.CAPABILITY,'owner':self.owner,
                'observations':[observation],'unverified':[],'absent':[]},root=self.root,state=self.state,
                native=True,result_budget=24*1024)
            self.assertEqual(replay.get('items'),[],cause)

    def test_exact_poll_projection_defers_unicode_claim_with_terminal_ids_without_consuming(self):
        large_command='rm -rf approval-fixture # '+'가'*2800+'🙂'*300
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
                self.assertTrue(b.wire(app,notifier,77,99,None,'boot-fixture',approvals_enabled=lambda:True,state=self.state))
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

