"""Dedicated native mount/auth fixture; run separately through guarded_unittest.

Real native dashboard/PluginContext and namespace middleware, fake home only.
No SessionAuthority, live backend, mock home resolver or production credentials.
"""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import test_support

class NativeNamespaceIntegration(unittest.TestCase):
    def test_native_auth_default_context_and_named_scope_refusal(self):
        self.assertNotIn('hermes_cli.web_server',sys.modules,'dedicated fixture requires cold native dashboard')
        tmp=test_support.temporary_directory(prefix='api-native-')
        self.addCleanup(tmp.cleanup)
        root=Path(tmp.name)
        def close_owned_log_handlers():
            import logging
            import hermes_logging
            # This is a dedicated, credential-free fixture process. Native
            # queued handlers are not attached to root.handlers; drain their
            # public lifecycle before standard logging shutdown and rmtree.
            drain=getattr(hermes_logging,'drain_log_queue',None)
            if callable(drain):drain(timeout=5)
            logging.shutdown()
        self.addCleanup(close_owned_log_handlers)
        home=root/'default-home'
        package=home/'plugins'/'session-complete-telegram'
        shutil.copytree(Path(__file__).resolve().parent,package,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        empty=root/'bundled';empty.mkdir()
        local=root/'localapp';local.mkdir()
        named=home/'profiles'/'arbitrary-api-user';named.mkdir(parents=True)
        config='plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings: {service_enabled: true, approvals_enabled: true}\napprovals:\n  timeout: 300\n'
        (home/'config.yaml').write_text(config,encoding='utf8')
        (named/'config.yaml').write_text(config,encoding='utf8')
        (home/'.env').write_text('TELEGRAM_BOT_TOKEN=765432:api-fixture-token\nTELEGRAM_HOME_CHANNEL=99\n',encoding='utf8')
        env={'HERMES_HOME':str(home),'HERMES_BUNDLED_PLUGINS':str(empty),'LOCALAPPDATA':str(local),'HERMES_DASHBOARD_SESSION_TOKEN':'native-api-fixture-session-token'}
        from hermes_constants import set_hermes_home_override,reset_hermes_home_override
        from hermes_cli.plugins import PluginContext,get_plugin_manager
        from fastapi.testclient import TestClient
        token=lambda value:base64.urlsafe_b64encode(json.dumps(value,separators=(',',':')).encode()).decode().rstrip('=')
        with patch.dict(os.environ,env):
            override=set_hermes_home_override(home)
            try:
                from hermes_cli import web_server
                from hermes_cli import web_server_dashboard
                manager=get_plugin_manager()
                # The actual native API loader owns imports, trust selection,
                # token authentication and profile + secret ContextVars.
                with TestClient(web_server.app) as client:
                    path='/api/plugins/session-complete-telegram/bridge/v1/'
                    self.assertEqual(client.get(path+'capabilities').status_code,401)
                    headers={'X-Hermes-Session-Token':env['HERMES_DASHBOARD_SESSION_TOKEN']}
                    before=hashlib.sha256((home/'config.yaml').read_bytes()).hexdigest()
                    ready=client.get(path+'capabilities',headers=headers)
                    self.assertEqual(ready.status_code,200,ready.text)
                    self.assertEqual(ready.json(),{'ok':True,'transport':'desktop-telegram-api-v1','plugin':'session-complete-telegram','profile':'default'})
                    matched=[m for n,m in tuple(sys.modules.items()) if n.startswith('hermes_plugins.') and m is not None and getattr(m,'__file__',None) and Path(m.__file__).resolve()==(package/'__init__.py').resolve()]
                    self.assertEqual(len(matched),1)
                    self.assertIsInstance(matched[0]._api_binding[1],PluginContext)
                    read=client.post(path+'dispatch',json={'request':token({'action':'settings_status'})},headers=headers)
                    self.assertEqual(read.status_code,200,read.text)
                    self.assertTrue(read.json()['dispatched'])
                    self.assertTrue(read.json()['result']['ok'],read.text)
                    self.assertTrue(read.json()['result']['read_only'])
                    self.assertEqual(read.json()['result']['selection_reason'],'ready')
                    self.assertEqual(hashlib.sha256((home/'config.yaml').read_bytes()).hexdigest(),before)
                    other=client.get(path+'capabilities?profile=arbitrary-api-user',headers=headers)
                    self.assertEqual(other.status_code,503,other.text)
                    # Fixture-only real native writer; no manual YAML writer in API.
                    written=client.post(path+'dispatch',json={'request':token({'action':'settings_set','key':'attention_notifications_enabled','value':False,'expected':True})},headers=headers)
                    self.assertEqual(written.status_code,200,written.text)
                    self.assertTrue(written.json()['result']['ok'],written.text)
                    self.assertEqual(written.json()['result']['readback'],{'key':'attention_notifications_enabled','value':False})
                    foreign=client.post(path+'dispatch?profile=arbitrary-api-user',json={'request':token({'action':'settings_set','key':'service_enabled','value':False})},headers=headers)
                    self.assertEqual(foreign.status_code,503,foreign.text)
                    self.assertIn('service_enabled: true',(named/'config.yaml').read_text(encoding='utf8'))
                    self.joined_frontend(client,headers,root,home)
                    self.joined_deep_approval(client,headers,root,home,matched[0])
            finally:
                reset_hermes_home_override(override)

    def joined_frontend(self,client,headers,root,home,requests=None,choice_hook=None,legacy_module=None):
        import queue,subprocess,threading,time
        repo=Path(__file__).resolve().parents[2]
        peer=repo/'tests'/'support'/'verify_native_api_wire.mjs'
        keys={'PATH','SystemRoot','SYSTEMROOT','WINDIR','PATHEXT','COMSPEC','HOME','USERPROFILE',
              'LOCALAPPDATA','APPDATA','HERMES_HOME','HERMES_CORE_ROOT','HERMES_CORE_REF',
              'HERMES_CORE_GENERATION','HERMES_INSTALL_ROOT','TMPDIR','TMP','TEMP',
              'XDG_CONFIG_HOME','XDG_DATA_HOME','XDG_CACHE_HOME','HERMES_TEST_TMPDIR','RUNNER_TEMP'}
        env={k:v for k,v in os.environ.items() if k in keys or k.startswith('HERMES_TEST_ISOLATION_')}
        ordinary=requests is None
        payloads=requests if requests is not None else [{'action':'settings_status'},
            {'action':'settings_set','key':'attention_notifications_enabled','value':True,'expected':False}]
        stderr_path=root/('node-wire.stderr' if ordinary else 'node-approval-wire.stderr')
        messages=queue.Queue()
        done=None
        frames=[]
        with stderr_path.open('xb') as err:
            process=subprocess.Popen(['node',str(peer)],cwd=Path(__file__).resolve().parent,
                env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,text=True,encoding='utf8',bufsize=1)
            def read_frames():
                for line in process.stdout:messages.put(line)
                messages.put(None)
            reader=threading.Thread(target=read_frames,name='fixture-api-node-reader')
            reader.start()
            try:
                process.stdin.write(json.dumps({'requests':payloads})+'\n')
                process.stdin.flush()
                deadline=time.monotonic()+35
                while done is None:
                    line=messages.get(timeout=max(.01,deadline-time.monotonic()))
                    self.assertIsNotNone(line,stderr_path.read_text(encoding='utf8'))
                    frame=json.loads(line)
                    if frame.get('type')=='done':done=frame;break
                    if frame.get('type')=='registered':
                        self.assertIsNotNone(choice_hook)
                        choice_hook(frame['id'])
                        process.stdin.write(json.dumps({'type':'choice','ok':True})+'\n');process.stdin.flush()
                        continue
                    self.assertEqual(frame.get('type'),'http')
                    self.assertLess(len(frames),len(payloads)+1)
                    req=frame['request'];self.assertEqual(frame['routing'],{'registryConnectionId':'local','backendLifecycle':'not_exercised'})
                    from urllib.parse import parse_qs,urlparse
                    self.assertEqual(parse_qs(urlparse(req['path']).query),{'profile':['default']})
                    self.assertEqual(req['connectionId'],'local');self.assertEqual(req['profile'],'default');self.assertTrue(req['passive'])
                    response=client.request(req['method'],req['path'],json=req.get('body'),headers=headers)
                    frames.append({'method':req['method'],'path':req['path'],'status':response.status_code})
                    reply={'type':'reply','status':response.status_code,'value':response.json()}
                    if legacy_module is not None and isinstance(reply['value'].get('result'),dict):
                        import io,contextlib
                        output=io.StringIO()
                        with contextlib.redirect_stdout(output):legacy_module._write_frame(reply['value']['result'])
                        reply['legacy_frame']=output.getvalue()
                    process.stdin.write(json.dumps(reply)+'\n');process.stdin.flush()
            finally:
                if process.stdin and not process.stdin.closed:process.stdin.close()
                if done is None and process.poll() is None:process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait(timeout=5)
                reader.join(5)
                if process.stdout:process.stdout.close()
        self.assertFalse(reader.is_alive(),'owned stdout reader must retire')
        self.assertEqual(process.returncode,0,stderr_path.read_text(encoding='utf8'))
        self.assertIsNotNone(done)
        self.assertEqual(done['cli_calls'],0)
        self.assertEqual([f['method'] for f in frames],['GET']+['POST']*len(payloads))
        self.assertTrue(all(f['status']==200 for f in frames))
        if ordinary:
            self.assertTrue(done['results'][0]['read_only'])
            self.assertTrue(done['results'][1]['ok'])
            self.assertEqual(done['results'][1]['readback'],{'key':'attention_notifications_enabled','value':True})
        return done

    def joined_deep_approval(self,client,headers,root,home,native):
        import importlib,time
        from test_approval_bridge import ApprovalBridgeTests
        notifier=importlib.import_module(native.__name__+'.notifier')
        bridge=notifier._sibling_module('approval_bridge')
        state=home/'plugin-data'/'session-complete-telegram'
        bridge.bind_receiver(765432,99,None,'api-depth-boot',private_verified=True,state=state,now=time.time())
        request,pending=ApprovalBridgeTests.pair(sid='api-depth-runtime')
        nested=None
        for _ in range(30):nested={'n':nested}
        request['params']['fixture_nested']=nested
        pending['fixture_nested']=nested
        owner='api-depth-owner'
        route={'mode':'local','connectionId':'local','profile':'default','targetProfile':'default'}
        session={'id':'api-depth-runtime','session_key':'api-depth-durable','status':'waiting'}
        descriptor={'owner':owner,**{k:route[k] for k in ['connectionId','profile','targetProfile']},
            'sid':session['id'],'session_key':session['session_key'],'epoch':'api-depth-epoch',
            'request_id':request['id'],'queue_id':pending['request_id'],'dispatch_mode':'legacyqueue',
            'request_fingerprint':bridge._digest(request),'pending_fingerprint':bridge._digest(pending)}
        observation={'id':'$registered','request_fingerprint':descriptor['request_fingerprint'],
            'pending_fingerprint':descriptor['pending_fingerprint'],'authority':bridge._digest(descriptor)}
        def action(name,**kw):return {'action':name,'capability':bridge.CAPABILITY,'owner':owner,**kw}
        payloads=[action('approval_owner_heartbeat'),action('approval_register',descriptor=descriptor,request=request,
            pending=pending,route=route,session=session,epoch=descriptor['epoch']),
            action('approval_poll',observations=[observation],unverified=[],absent=[]),
            action('approval_settle',id='$registered',claim='$claimed',status='applied'),
            action('approval_poll',observations=[observation],unverified=[],absent=[])]
        selected=[];sent=[]
        def telegram(_token,method,payload):
            if method=='sendMessage':sent.append(1)
            return {'ok':True,'result':{'message_id':901,'chat':{'id':99,'type':'private'},
                'text':payload.get('text',''),'from':{'id':765432,'is_bot':True}}}
        def choose(fid):
            self.assertFalse(selected)
            with bridge.connect(state) as c:row=dict(c.execute('SELECT * FROM approvals WHERE id=?',(fid,)).fetchone())
            self.assertEqual(row['status'],'open')
            identity={'bot':765432,'boot':'api-depth-boot','chat':99,'user':99,'topic':None,'mid':901,
                'private':True,'human':True,'bot_author':True,'forwarded':False,'edited':False,
                'inline':False,'inaccessible':False,'message_text':row['review_text'],'event':'api-depth-choice'}
            self.assertTrue(bridge.choose(row['token'],'once',identity,state=state)['ok'])
            selected.append(fid)
        with patch.object(notifier,'telegram',side_effect=telegram):
            done=self.joined_frontend(client,headers,root,home,requests=payloads,choice_hook=choose,legacy_module=native)
        self.assertEqual(sent,[1]);self.assertEqual(len(selected),1)
        self.assertEqual(done['results'][2]['items'][0]['request'],request)
        self.assertTrue(done['results'][3]['ok'])
        self.assertEqual(done['results'][4]['items'],[])
        with bridge.connect(state) as c:
            row=dict(c.execute('SELECT * FROM approvals WHERE id=?',(selected[0],)).fetchone())
            stages=[x[0] for x in c.execute('SELECT stage FROM approval_diagnostics WHERE approval_id=?',(selected[0],))]
        self.assertEqual(row['status'],'applied')
        self.assertEqual(stages.count('claimed'),1)
        self.assertEqual(stages.count('terminal'),1)

if __name__=='__main__':unittest.main()
